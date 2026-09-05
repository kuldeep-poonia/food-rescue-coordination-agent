"""Concurrency and race condition tests for AgentSessionManager."""

import concurrent.futures
from datetime import datetime, timezone

from agent.session_manager import AgentSessionManager
from tests.test_session_continuity import MockSessionDynamoDBResource


def test_concurrent_donation_outcomes_no_lost_updates() -> None:
    """Fire 20 concurrent outcome writes at same session to prove counter atomicity.

    Proves that DynamoDB ADD expression prevents the lost-update race condition
    where concurrent read-modify-write patterns silently drop increments.
    """
    mock_dynamo = MockSessionDynamoDBResource()
    manager = AgentSessionManager(dynamodb_resource=mock_dynamo)
    region = "metro-core"
    date_str = "2026-09-05"

    # Pre-initialize session
    manager.get_or_create_session(region, date_str)

    num_threads = 20
    quantity_per_run = 15.25

    def worker_record(worker_id: int) -> None:
        manager.record_donation_outcome(
            service_region=region,
            quantity_kg=quantity_per_run,
            outcome="matched",
            recipient_id=f"rec-worker-{worker_id % 3}",
            volunteer_id=f"vol-{worker_id % 4}",
            near_capacity=(worker_id % 2 == 0),
            date_str=date_str,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker_record, i) for i in range(num_threads)]
        concurrent.futures.wait(futures)

    # Validate final state
    session = manager.load_session(region, date_str, consistent_read=True)
    assert session is not None
    assert session.total_donations_processed == num_threads, (
        f"Expected {num_threads} donations, got {session.total_donations_processed}"
    )
    expected_total_kg = round(num_threads * quantity_per_run, 2)
    assert session.total_kg_routed == expected_total_kg, (
        f"Expected {expected_total_kg}kg, got {session.total_kg_routed}kg"
    )

    # Check that volunteer counts sum to 20
    total_assigned = sum(session.recent_volunteer_assignments.values())
    assert total_assigned == num_threads


def test_concurrent_session_creation_race() -> None:
    """Fire 10 concurrent requests creating the same uninitialized daily session.

    Proves that ConditionalCheckFailedException on attribute_not_exists(PK) is
    caught and gracefully falls back to strongly consistent load_session.
    """
    mock_dynamo = MockSessionDynamoDBResource()
    manager = AgentSessionManager(dynamodb_resource=mock_dynamo)
    region = "metro-north"
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    num_threads = 10
    results: list[str] = []

    def worker_create(_worker_id: int) -> str:
        sess = manager.get_or_create_session(region, date_str)
        return sess.session_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker_create, i) for i in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    assert len(results) == num_threads
    expected_session_id = f"sess-{region}-{date_str}"
    assert all(sid == expected_session_id for sid in results)

    # Exactly one item exists in the table for this session
    final_session = manager.load_session(region, date_str, consistent_read=True)
    assert final_session is not None
    assert final_session.session_id == expected_session_id
