"""Unit and integration tests for session continuity and reconciliation."""

import threading
from datetime import datetime, timezone
from typing import Any
from unittest import mock

import pytest
from botocore.exceptions import ClientError

from agent.session_manager import AgentSessionManager
from donations_repo import DonationsRepository
from models import Coordinates, Donation, FoodCategory, Recipient, RunningSummary
from tools.find_best_match import find_best_match


class MockSessionDynamoDBTable:
    """Thread-safe mock DynamoDB table for session and memory store testing."""

    def __init__(self, name: str) -> None:
        self.name: str = name
        self._items: dict[str, dict[str, Any]] = {}
        self._lock: threading.Lock = threading.Lock()

    def put_item(
        self, Item: dict[str, Any], ConditionExpression: str | None = None
    ) -> None:
        with self._lock:
            pk = str(Item["PK"])
            sk = str(Item.get("SK", "METADATA"))
            key = f"{pk}#{sk}"
            if (
                ConditionExpression
                and "attribute_not_exists" in ConditionExpression
                and key in self._items
            ):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "PutItem",
                )
            self._items[key] = Item.copy()

    def get_item(
        self, Key: dict[str, Any], ConsistentRead: bool = True
    ) -> dict[str, Any]:
        del ConsistentRead
        with self._lock:
            pk = str(Key["PK"])
            sk = str(Key.get("SK", "METADATA"))
            key = f"{pk}#{sk}"
            item = self._items.get(key)
            return {"Item": item.copy()} if item else {}

    def update_item(
        self,
        Key: dict[str, Any],
        UpdateExpression: str,
        ExpressionAttributeValues: dict[str, Any] | None = None,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ReturnValues: str | None = None,
    ) -> dict[str, Any]:
        del ReturnValues
        with self._lock:
            pk = str(Key["PK"])
            sk = str(Key.get("SK", "METADATA"))
            key = f"{pk}#{sk}"
            item = self._items.get(key)
            if not item:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "UpdateItem",
                )

            vals = ExpressionAttributeValues or {}
            names = ExpressionAttributeNames or {}

            # Handle SET expressions
            if ":now" in vals:
                item["updated_at"] = vals[":now"]
            if ":auth_kg" in vals:
                item["total_kg_routed"] = float(vals[":auth_kg"])
            if ":auth_count" in vals:
                item["total_donations_processed"] = int(vals[":auth_count"])

            # Handle ADD expressions
            if ":one" in vals and "total_donations_processed :one" in UpdateExpression:
                item["total_donations_processed"] = (
                    int(item.get("total_donations_processed", 0)) + 1
                )
            if ":qty" in vals and "total_kg_routed :qty" in UpdateExpression:
                item["total_kg_routed"] = round(
                    float(item.get("total_kg_routed", 0.0)) + float(vals[":qty"]), 2
                )
            if ":one" in vals and "active_escalations_count :one" in UpdateExpression:
                item["active_escalations_count"] = (
                    int(item.get("active_escalations_count", 0)) + 1
                )

            # Handle String Set ADD and DELETE
            if ":rec_set" in vals:
                current_set = set(item.get("recipients_near_capacity", []))
                current_set.update(vals[":rec_set"])
                item["recipients_near_capacity"] = sorted(current_set)

            if ":del_rec_set" in vals:
                current_set = set(item.get("recipients_near_capacity", []))
                current_set.difference_update(vals[":del_rec_set"])
                item["recipients_near_capacity"] = sorted(current_set)

            # Handle volunteer assignments map update
            if (
                "#vol" in names
                and "recent_volunteer_assignments.#vol" in UpdateExpression
            ):
                vol_id = names["#vol"]
                rva = item.setdefault("recent_volunteer_assignments", {})
                rva[vol_id] = rva.get(vol_id, 0) + 1

            return {"Attributes": item.copy()}


class MockSessionDynamoDBResource:
    """Mock DynamoDB client returning isolated mock session tables."""

    def __init__(self) -> None:
        self.tables: dict[str, MockSessionDynamoDBTable] = {}

    def Table(self, name: str) -> MockSessionDynamoDBTable:
        if name not in self.tables:
            self.tables[name] = MockSessionDynamoDBTable(name)
        return self.tables[name]


@pytest.fixture
def mock_session_dynamo() -> MockSessionDynamoDBResource:
    """Provide isolated mock DynamoDB resource for session manager."""
    return MockSessionDynamoDBResource()


def test_session_lifecycle_and_ttl(
    mock_session_dynamo: MockSessionDynamoDBResource,
) -> None:
    """Verify session initialization, partition key format, and 24h TTL."""
    manager = AgentSessionManager(dynamodb_resource=mock_session_dynamo)
    region = "metro-core"
    date_str = "2026-09-05"

    session = manager.get_or_create_session(region, date_str)

    assert session.session_id == f"sess-{region}-{date_str}"
    assert session.service_region == region
    assert session.session_date == date_str
    assert session.total_donations_processed == 0
    assert session.total_kg_routed == 0.0
    assert session.recipients_near_capacity == []

    # Verify 24 hour TTL (approx 86400 seconds from now)
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    assert session.ttl_epoch >= now_epoch + 86000
    assert session.ttl_epoch <= now_epoch + 86500

    # Verify strongly consistent reload
    loaded = manager.load_session(region, date_str, consistent_read=True)
    assert loaded is not None
    assert loaded.session_id == session.session_id


def test_session_record_donation_outcome_atomic_counters(
    mock_session_dynamo: MockSessionDynamoDBResource,
) -> None:
    """Verify atomic outcome recording updates counts and metrics."""
    manager = AgentSessionManager(dynamodb_resource=mock_session_dynamo)
    region = "metro-core"
    date_str = "2026-09-05"

    manager.record_donation_outcome(
        service_region=region,
        quantity_kg=45.5,
        outcome="matched",
        recipient_id="rec-alpha",
        volunteer_id="vol-01",
        near_capacity=True,
        date_str=date_str,
    )

    session = manager.load_session(region, date_str)
    assert session is not None
    assert session.total_donations_processed == 1
    assert session.total_kg_routed == 45.5
    assert session.active_escalations_count == 0
    assert session.recipients_near_capacity == ["rec-alpha"]
    assert session.recent_volunteer_assignments == {"vol-01": 1}

    # Record second outcome with escalation
    manager.record_donation_outcome(
        service_region=region,
        quantity_kg=20.0,
        outcome="escalated",
        date_str=date_str,
    )

    session = manager.load_session(region, date_str)
    assert session is not None
    assert session.total_donations_processed == 2
    assert session.total_kg_routed == 65.5
    assert session.active_escalations_count == 1


def test_session_warning_threshold_capacity_signal(
    mock_session_dynamo: MockSessionDynamoDBResource,
) -> None:
    """Verify recipient enters and leaves recipients_near_capacity set."""
    manager = AgentSessionManager(dynamodb_resource=mock_session_dynamo)
    region = "metro-core"
    date_str = "2026-09-05"

    # Step 1: Mark rec-01 as near capacity
    manager.record_donation_outcome(
        service_region=region,
        quantity_kg=30.0,
        outcome="matched",
        recipient_id="rec-01",
        near_capacity=True,
        date_str=date_str,
    )
    s1 = manager.load_session(region, date_str)
    assert s1 is not None
    assert "rec-01" in s1.recipients_near_capacity

    # Step 2: Capacity restored or refreshed -> near_capacity is False
    manager.record_donation_outcome(
        service_region=region,
        quantity_kg=0.0,
        outcome="capacity_refreshed",
        recipient_id="rec-01",
        near_capacity=False,
        date_str=date_str,
    )
    s2 = manager.load_session(region, date_str)
    assert s2 is not None
    assert "rec-01" not in s2.recipients_near_capacity


def test_session_authoritative_reconciliation(
    mock_session_dynamo: MockSessionDynamoDBResource,
) -> None:
    """Verify reconcile_session_metrics synchronizes session with GSI daily summary."""
    manager = AgentSessionManager(dynamodb_resource=mock_session_dynamo)
    region = "metro-core"
    date_str = "2026-09-05"

    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_d_repo.get_authoritative_daily_summary.return_value = RunningSummary(
        service_region=region,
        date_str=date_str,
        total_kg_routed=250.0,
        meals_equivalent=500,
        organizations_served=5,
        donations_count=8,
    )

    reconciled = manager.reconcile_session_metrics(
        service_region=region,
        date_str=date_str,
        donations_repo=mock_d_repo,
    )

    assert reconciled.total_kg_routed == 250.0
    assert reconciled.total_donations_processed == 8
    mock_d_repo.get_authoritative_daily_summary.assert_called_once_with(
        region, date_str
    )


def test_session_near_capacity_has_zero_disqualification_power() -> None:
    """Verify session advisory near_capacity set does NOT block matching."""
    donation = Donation(
        donation_id="don-adv-01",
        donor_id="donor-1",
        donor_name="Bakery One",
        donor_phone="+12125550199",
        donor_address="100 Main St",
        donor_coordinates=Coordinates(latitude=40.7128, longitude=-74.0060),
        food_category=FoodCategory.BAKERY,
        quantity_kg=20.0,
        ready_by=datetime(2026, 9, 5, 15, 0, 0, tzinfo=timezone.utc),
        perishability_hours=4.0,
        service_region="metro-core",
    )

    # Recipient has 25kg capacity left (< 30kg warning threshold, so near-capacity).
    # But 25kg >= donation quantity (20kg), so recipient remains fully eligible!
    recipient = Recipient(
        recipient_id="rec-near-cap-01",
        organization_name="Advisory Shelter",
        contact_name="Alice",
        contact_phone="+12125550188",
        address="102 Main St",
        coordinates=Coordinates(latitude=40.7130, longitude=-74.0062),
        capacity_kg_remaining=25.0,  # Below 30kg warning threshold, sufficient for 20kg
        service_region="metro-core",
    )

    match = find_best_match(donation, [recipient], classification=None)
    assert match is not None
    assert match.best_match is not None
    assert match.best_match.recipient_id == "rec-near-cap-01"
    assert match.best_match.capacity_match_kg == 25.0
