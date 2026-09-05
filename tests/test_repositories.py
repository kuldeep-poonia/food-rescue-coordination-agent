"""Hardcore test suite for repository layer: race conditions, and throttling."""

import concurrent.futures
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

import pytest
from botocore.exceptions import ClientError

from audit_repo import AuditRepository
from donations_repo import DonationClaimConflictError, DonationsRepository
from models import (
    AuditEvent,
    Coordinates,
    Donation,
    FoodCategory,
    Recipient,
    Volunteer,
)
from recipients_repo import InsufficientCapacityError, RecipientsRepository
from volunteers_repo import VolunteersRepository, VolunteerUnavailableError


class MockDynamoDBTable:
    """Thread-safe in-memory DynamoDB table emulating atomic conditional writes."""

    def __init__(self, name: str) -> None:
        self.name: str = name
        self._items: dict[str, dict[str, Any]] = {}
        self._lock: threading.Lock = threading.Lock()

    def put_item(
        self, Item: dict[str, Any], ConditionExpression: str | None = None
    ) -> None:
        with self._lock:
            pk_name = (
                "idempotency_key"
                if "idempotency_key" in Item
                else next(iter(Item.keys()))
            )
            pk_val = str(Item[pk_name])
            if (
                ConditionExpression
                and "attribute_not_exists" in ConditionExpression
                and pk_val in self._items
            ):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "PutItem",
                )
            self._items[pk_val] = Item.copy()

    def get_item(
        self, Key: dict[str, Any], ConsistentRead: bool = True
    ) -> dict[str, Any]:
        del ConsistentRead
        with self._lock:
            pk_val = str(next(iter(Key.values())))
            item = self._items.get(pk_val)
            return {"Item": item.copy()} if item else {}

    def update_item(
        self,
        Key: dict[str, Any],
        UpdateExpression: str,
        ConditionExpression: str | None = None,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del UpdateExpression, ExpressionAttributeNames
        with self._lock:
            pk_val = str(next(iter(Key.values())))
            item = self._items.get(pk_val)
            if not item:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "UpdateItem",
                )

            # Evaluate conditional checks for donations claim
            claim_cond = "attribute_not_exists(matched_recipient_id)"
            if (
                ConditionExpression
                and claim_cond in ConditionExpression
                and item.get("matched_recipient_id") is not None
            ):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "UpdateItem",
                )

            # Evaluate conditional checks for capacity deduction
            cap_cond = "capacity_kg_remaining >= :qty"
            if ConditionExpression and cap_cond in ConditionExpression:
                needed = (
                    ExpressionAttributeValues.get(":qty", 0)
                    if ExpressionAttributeValues
                    else 0
                )
                if item.get("capacity_kg_remaining", 0) < needed:
                    raise ClientError(
                        {"Error": {"Code": "ConditionalCheckFailedException"}},
                        "UpdateItem",
                    )

            # Evaluate conditional checks for volunteer availability
            vol_cond = "is_available = :expected_state"
            if ConditionExpression and vol_cond in ConditionExpression:
                expected = (
                    ExpressionAttributeValues.get(":expected_state")
                    if ExpressionAttributeValues
                    else None
                )
                if item.get("is_available") != expected:
                    raise ClientError(
                        {"Error": {"Code": "ConditionalCheckFailedException"}},
                        "UpdateItem",
                    )

            # Apply state updates
            if ExpressionAttributeValues:
                if ":recipient_id" in ExpressionAttributeValues:
                    rec_id = ExpressionAttributeValues[":recipient_id"]
                    item["matched_recipient_id"] = rec_id
                    item["status"] = "matched"
                if ":qty" in ExpressionAttributeValues:
                    item["capacity_kg_remaining"] -= ExpressionAttributeValues[":qty"]
                if ":new_state" in ExpressionAttributeValues:
                    item["is_available"] = ExpressionAttributeValues[":new_state"]

            return {"Attributes": item.copy()}

    def query(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs  # Unused in mock table
        with self._lock:
            return {"Items": [v.copy() for v in self._items.values()]}


class MockDynamoDBResource:
    """Mock DynamoDB client returning isolated mock tables."""

    def __init__(self) -> None:
        self.tables: dict[str, MockDynamoDBTable] = {}

    def Table(self, name: str) -> MockDynamoDBTable:
        if name not in self.tables:
            self.tables[name] = MockDynamoDBTable(name)
        return self.tables[name]


@pytest.fixture
def mock_dynamodb() -> MockDynamoDBResource:
    """Provide a clean mock DynamoDB resource fixture for tests."""
    return MockDynamoDBResource()


def test_concurrent_donation_claim_race_condition(
    mock_dynamodb: MockDynamoDBResource,
) -> None:
    """Fire 20 concurrent claim writes at same donation ID to prove atomicity.

    Asserts exactly one succeeds and 19 get clean conflict responses without corruption.
    """
    repo = DonationsRepository(dynamodb_resource=mock_dynamodb)
    donation = Donation(
        donation_id="don-race-test",
        donor_id="donor-100",
        donor_name="Central Bakery",
        donor_phone="+12125550199",
        donor_address="100 Broadway",
        donor_coordinates=Coordinates(latitude=40.71, longitude=-74.00),
        food_category=FoodCategory.BAKERY,
        quantity_kg=50.0,
        ready_by=datetime.now(timezone.utc) + timedelta(hours=3),
        perishability_hours=24.0,
    )
    repo.create_donation(donation)

    results: list[str] = []
    lock = threading.Lock()

    def attempt_claim(worker_id: int) -> None:
        recipient_id = f"rec-candidate-{worker_id}"
        try:
            success = repo.claim_donation(donation.donation_id, recipient_id)
            if success:
                with lock:
                    results.append("success")
        except DonationClaimConflictError:
            with lock:
                results.append("conflict")

    # Launch 20 concurrent claim workers
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(attempt_claim, i) for i in range(20)]
        concurrent.futures.wait(futures)

    # Exactly 1 success, 19 clean conflicts
    successful_claims = [r for r in results if r == "success"]
    conflict_claims = [r for r in results if r == "conflict"]

    assert len(successful_claims) == 1, "Exactly one recipient claim must succeed"
    assert len(conflict_claims) == 19, "All 19 competing claims must receive conflict"

    # Verify record integrity
    claimed_record = repo.get_donation(donation.donation_id)
    assert claimed_record is not None
    assert claimed_record.status.value == "matched"
    assert claimed_record.matched_recipient_id is not None
    assert claimed_record.matched_recipient_id.startswith("rec-candidate-")


def test_dynamodb_throttling_retry_with_backoff(
    mock_dynamodb: MockDynamoDBResource,
) -> None:
    """Simulate ProvisionedThroughputExceededException and confirm retry succeeds."""
    repo = DonationsRepository(dynamodb_resource=mock_dynamodb)
    donation = Donation(
        donation_id="don-throttle-test",
        donor_id="donor-200",
        donor_name="Metro Grocers",
        donor_phone="+12125550199",
        donor_address="200 Main St",
        donor_coordinates=Coordinates(latitude=40.72, longitude=-74.01),
        food_category=FoodCategory.PRODUCE,
        quantity_kg=20.0,
        ready_by=datetime.now(timezone.utc) + timedelta(hours=4),
        perishability_hours=48.0,
    )

    table = mock_dynamodb.Table(repo._config.donations_table_name)
    real_put_item = table.put_item
    call_count = 0

    def throttled_put_item(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "PutItem",
            )
        real_put_item(*args, **kwargs)

    with (
        mock.patch.object(table, "put_item", side_effect=throttled_put_item),
        mock.patch("time.sleep") as mock_sleep,
    ):
        repo.create_donation(donation)

        assert call_count == 3, "Operation should retry and succeed on 3rd attempt"
        assert mock_sleep.call_count == 2, "Backoff sleep should execute twice"


def test_recipient_capacity_atomic_deduction(
    mock_dynamodb: MockDynamoDBResource,
) -> None:
    """Verify capacity deduction succeeds when available and raises error."""
    repo = RecipientsRepository(dynamodb_resource=mock_dynamodb)
    recipient = Recipient(
        recipient_id="rec-cap-test",
        organization_name="Harbor Shelter",
        contact_name="Bob Miller",
        contact_phone="+12125550188",
        address="300 River St",
        coordinates=Coordinates(latitude=40.73, longitude=-74.02),
        capacity_kg_remaining=100.0,
        service_region="metro-core",
    )
    repo.create_recipient(recipient)

    # First deduction within capacity: 60kg
    assert repo.deduct_capacity(recipient.recipient_id, 60.0) is True

    # Second deduction exceeding remaining capacity (attempt 50kg when only 40kg left)
    with pytest.raises(InsufficientCapacityError):
        repo.deduct_capacity(recipient.recipient_id, 50.0)


def test_volunteer_availability_conditional_transition(
    mock_dynamodb: MockDynamoDBResource,
) -> None:
    """Verify volunteer availability transitions atomically and prevents duplicates."""
    repo = VolunteersRepository(dynamodb_resource=mock_dynamodb)
    volunteer = Volunteer(
        volunteer_id="vol-test-1",
        volunteer_name="Carlos Vega",
        phone="+12125550177",
        address="500 Park Ave",
        coordinates=Coordinates(latitude=40.74, longitude=-74.03),
        is_available=True,
        max_capacity_kg=80.0,
        vehicle_type="van",
        service_region="metro-core",
    )
    repo.create_volunteer(volunteer)

    # Transition from available (True) to busy (False)
    assert (
        repo.set_volunteer_availability(volunteer.volunteer_id, is_available=False)
        is True
    )

    # Duplicate transition attempt when already unavailable must raise conflict
    with pytest.raises(VolunteerUnavailableError):
        repo.set_volunteer_availability(volunteer.volunteer_id, is_available=False)


def test_audit_event_idempotency(mock_dynamodb: MockDynamoDBResource) -> None:
    """Verify duplicate audit events with same idempotency_key are handled idempotently.

    Proves that even when a retry generates a new event_id, the partition key
    (idempotency_key) guarantees table-wide deduplication without duplicate records.
    """
    repo = AuditRepository(dynamodb_resource=mock_dynamodb)
    event_initial = AuditEvent(
        event_id="evt-001",
        donation_id="don-audit-test",
        action="MATCH_FOUND",
        actor="strands_orchestrator",
        idempotency_key="idemp-key-001",
        details={"score": 0.95},
    )

    # Initial write succeeds
    assert repo.record_audit_event(event_initial) is True

    # Replay with exact same event model returns False cleanly
    assert repo.record_audit_event(event_initial) is False

    # Retry that generated a different event_id but SAME idempotency_key
    event_retry_diff_id = AuditEvent(
        event_id="evt-999-new-id",
        donation_id="don-audit-test",
        action="MATCH_FOUND",
        actor="strands_orchestrator",
        idempotency_key="idemp-key-001",
        details={"score": 0.95},
    )
    assert repo.record_audit_event(event_retry_diff_id) is False

    # Trail verification confirms exactly one audit event is persisted
    trail = repo.query_audit_trail_by_donation("don-audit-test")
    assert len(trail) == 1
    assert trail[0].event_id == "evt-001"
