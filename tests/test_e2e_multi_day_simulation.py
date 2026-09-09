"""Phase 9 E2E Multi-Day Operational Simulation Test Suite.

Simulates a realistic 3-day operational coordination lifecycle across:
- 4 Donors (Bakery, Supermarket, Caterer, Hotel)
- 4 Recipients (Soup Kitchen, Community Shelter, Youth Center, Family Pantry)
- 5 Volunteers (2 Bikes, 2 Cars, 1 Van)
- Day 1: Autonomous multi-party surge routing, atomic deductions, masked PII.
- Day 2: Midnight rollover, depleted recipient bypass, REST capacity restock,
  volunteer shift change.
- Day 3: Expiry boundary (<60m shelf life), capacity exhaustion, volunteer
  unavailability rollback, and coordinator manual resolution.

Enforces zero manual intervention except at defined escalation points.
"""

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

from botocore.exceptions import ClientError

from agent.orchestrator import StrandsOrchestrator
from agent.session_manager import AgentSessionManager
from audit_repo import AuditRepository
from config import AppConfig
from donations_repo import DonationsRepository
from frontend_api import FrontendApiService
from models import (
    Coordinates,
    Donation,
    DonationStatus,
    EscalationReason,
    FoodCategory,
    PipelineStep,
    Recipient,
    RecipientStatus,
    Volunteer,
    VolunteerStatus,
)
from recipients_repo import RecipientsRepository
from tools.distance_calculator import GeodesicDistanceCalculator
from volunteers_repo import VolunteersRepository


# ------------------------------------------------------------------------------
# In-Memory Stateful DynamoDB Mock Harness
# ------------------------------------------------------------------------------
class StatefulMockTable:
    """Thread-safe in-memory table emulating atomic operations and conditions."""

    def __init__(self, name: str) -> None:
        self.name: str = name
        self.items: dict[str, dict[str, Any]] = {}
        self._lock: threading.Lock = threading.Lock()

    def _extract_pk(self, item_or_key: dict[str, Any]) -> str:
        if "audit" in self.name:
            if "idempotency_key" in item_or_key:
                val = item_or_key["idempotency_key"]
                return val.get("S", val) if isinstance(val, dict) else str(val)
            if "event_id" in item_or_key:
                val = item_or_key["event_id"]
                return val.get("S", val) if isinstance(val, dict) else str(val)
        for k in (
            "donation_id",
            "recipient_id",
            "volunteer_id",
            "PK",
            "idempotency_key",
            "event_id",
        ):
            if k in item_or_key:
                val = item_or_key[k]
                return val.get("S", val) if isinstance(val, dict) else str(val)
        return str(next(iter(item_or_key.values())))

    def put_item(
        self, Item: dict[str, Any], ConditionExpression: str | None = None
    ) -> None:
        with self._lock:
            pk = self._extract_pk(Item)
            if (
                ConditionExpression
                and "attribute_not_exists" in ConditionExpression
                and pk in self.items
            ):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
                )
            self.items[pk] = json.loads(json.dumps(Item, default=str))

    def get_item(
        self, Key: dict[str, Any], ConsistentRead: bool = True
    ) -> dict[str, Any]:
        del ConsistentRead
        with self._lock:
            pk = self._extract_pk(Key)
            item = self.items.get(pk)
            return {"Item": json.loads(json.dumps(item))} if item else {}

    def update_item(
        self,
        Key: dict[str, Any],
        UpdateExpression: str,
        ConditionExpression: str | None = None,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
        ReturnValues: str | None = None,
    ) -> dict[str, Any]:
        del ExpressionAttributeNames, ReturnValues
        with self._lock:
            pk = self._extract_pk(Key)
            item = self.items.get(pk)
            if item is None and "attribute_exists" in (ConditionExpression or ""):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
            if item is None:
                item = {next(iter(Key.keys())): pk}
                self.items[pk] = item

            vals = ExpressionAttributeValues or {}

            # Evaluate conditional checks
            if ConditionExpression:
                if (
                    "attribute_not_exists(matched_recipient_id)" in ConditionExpression
                    and item.get("matched_recipient_id")
                ):
                    raise ClientError(
                        {"Error": {"Code": "ConditionalCheckFailedException"}},
                        "UpdateItem",
                    )
                if "capacity_kg_remaining >= :qty" in ConditionExpression:
                    qty = float(vals.get(":qty", 0))
                    if float(item.get("capacity_kg_remaining", 0)) < qty:
                        raise ClientError(
                            {"Error": {"Code": "ConditionalCheckFailedException"}},
                            "UpdateItem",
                        )
                if "#st = :expected_state" in ConditionExpression:
                    exp = vals.get(":expected_state")
                    if item.get("status") != exp:
                        raise ClientError(
                            {"Error": {"Code": "ConditionalCheckFailedException"}},
                            "UpdateItem",
                        )
                if (
                    "#avi" in ConditionExpression
                    and "attribute_not_exists(#avi)" in ConditionExpression
                    and item.get("assigned_volunteer_id")
                ):
                    raise ClientError(
                        {"Error": {"Code": "ConditionalCheckFailedException"}},
                        "UpdateItem",
                    )

            # Apply state mutations based on UpdateExpression
            if "ADD #cnt :one" in UpdateExpression:
                item["request_count"] = int(item.get("request_count", 0)) + int(
                    vals.get(":one", 1)
                )

            if ":recipient_id" in vals:
                item["matched_recipient_id"] = str(vals[":recipient_id"])
            if "REMOVE matched_recipient_id" in UpdateExpression:
                item["matched_recipient_id"] = None
                item["status"] = DonationStatus.REPORTED.value

            if ":volunteer_id" in vals:
                item["assigned_volunteer_id"] = str(vals[":volunteer_id"])

            if "assigned_status" in UpdateExpression and ":assigned_status" in vals:
                item["status"] = str(vals[":assigned_status"])
            elif "matched_status" in UpdateExpression and ":matched_status" in vals:
                item["status"] = str(vals[":matched_status"])
            elif "escalated_status" in UpdateExpression and ":escalated_status" in vals:
                item["status"] = str(vals[":escalated_status"])
                item["escalation_reason"] = vals.get(":reason")
            elif "new_state" in UpdateExpression and ":new_state" in vals:
                item["status"] = str(vals[":new_state"])
            elif ":status" in vals and "#st = :status" in UpdateExpression:
                item["status"] = str(vals[":status"])

            if ":cap" in vals:
                item["capacity_kg_remaining"] = float(vals[":cap"])
            elif ":qty" in vals:
                qty = float(vals[":qty"])
                if "+ :qty" in UpdateExpression:
                    item["capacity_kg_remaining"] = (
                        float(item.get("capacity_kg_remaining", 0.0)) + qty
                    )
                elif "- :qty" in UpdateExpression:
                    item["capacity_kg_remaining"] = (
                        float(item.get("capacity_kg_remaining", 0.0)) - qty
                    )

            if ":date_status" in vals:
                item["date_status"] = str(vals[":date_status"])

            return {"Attributes": json.loads(json.dumps(item))}

    def query(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args
        with self._lock:
            items = [json.loads(json.dumps(v)) for v in self.items.values()]
            idx = str(kwargs.get("IndexName", ""))
            filtered = []
            for it in items:
                st = str(it.get("status", "")).lower()
                if (
                    "volunteers" in self.name
                    and "region-status-index" in idx
                    and st != "available"
                ):
                    continue
                if (
                    "recipients" in self.name
                    and "region-status-index" in idx
                    and st != "active"
                ):
                    continue
                if "donations" in self.name and "status-ready_by-index" in idx:
                    cond = kwargs.get("KeyConditionExpression")
                    vals_str = repr(cond) + " " + str(getattr(cond, "_values", ""))
                    target_st = (
                        "escalated" if "escalated" in vals_str.lower() else "reported"
                    )
                    if st != target_st:
                        continue
                if "audit" in self.name:
                    cond = kwargs.get("KeyConditionExpression") or kwargs.get(
                        "FilterExpression"
                    )
                    cond_repr = repr(cond) + " " + str(getattr(cond, "_values", ""))
                    d_id = it.get("donation_id")
                    if d_id and d_id not in cond_repr:
                        continue
                filtered.append(it)
            return {"Items": filtered}

    def scan(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args
        with self._lock:
            items = [json.loads(json.dumps(v)) for v in self.items.values()]
            filtered = []
            filter_expr = str(kwargs.get("FilterExpression", ""))
            filter_obj = kwargs.get("FilterExpression")
            cond_repr = (
                filter_expr
                + " "
                + repr(filter_obj)
                + " "
                + str(getattr(filter_obj, "_values", ""))
            )
            for it in items:
                st = str(it.get("status", "")).lower()
                if (
                    "volunteers" in self.name
                    and "available" in filter_expr
                    and st != "available"
                ):
                    continue
                if (
                    "recipients" in self.name
                    and "active" in filter_expr
                    and st != "active"
                ):
                    continue
                if "audit" in self.name:
                    d_id = it.get("donation_id")
                    if d_id and d_id not in cond_repr:
                        continue
                filtered.append(it)
            return {"Items": filtered}


class StatefulMockDynamoDBResource:
    """Mock DynamoDB resource with atomic TransactWriteItems engine."""

    def __init__(self) -> None:
        self.tables: dict[str, StatefulMockTable] = {}
        self.meta: Any = mock.MagicMock()
        self.meta.client = self

    def Table(self, name: str) -> StatefulMockTable:
        if name not in self.tables:
            self.tables[name] = StatefulMockTable(name)
        return self.tables[name]

    def transact_write_items(
        self, ClientRequestToken: str, TransactItems: list[dict[str, Any]]
    ) -> dict[str, Any]:
        del ClientRequestToken
        # Step 1: Pre-validation of all conditions
        cancellation_reasons = []
        has_failure = False

        for item_op in TransactItems:
            if "Update" in item_op:
                u = item_op["Update"]
                table_name = u["TableName"]
                target_table = self.Table(table_name)
                key_dict = u["Key"]
                pk = next(iter(key_dict.values()))
                pk_val = pk.get("S", pk) if isinstance(pk, dict) else str(pk)
                cond = u.get("ConditionExpression", "")
                existing = target_table.items.get(pk_val)

                failed = False
                if "attribute_not_exists(matched_recipient_id)" in cond:
                    if not existing or existing.get("matched_recipient_id") is not None:
                        failed = True
                    st = existing.get("status") if existing else None
                    if st not in (DonationStatus.REPORTED.value, "REPORTED"):
                        failed = True
                if "capacity_kg_remaining >= :qty" in cond:
                    qty = float(u["ExpressionAttributeValues"][":qty"]["N"])
                    curr_cap = (
                        float(existing.get("capacity_kg_remaining", 0.0))
                        if existing
                        else 0.0
                    )
                    if curr_cap < qty:
                        failed = True

                if failed:
                    cancellation_reasons.append({"Code": "ConditionalCheckFailed"})
                    has_failure = True
                else:
                    cancellation_reasons.append({"Code": "None"})

        if has_failure:
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": cancellation_reasons,
                },
                "TransactWriteItems",
            )

        # Step 2: Atomic commit of all mutations
        for item_op in TransactItems:
            if "Update" in item_op:
                u = item_op["Update"]
                target_table = self.Table(u["TableName"])
                key_dict = u["Key"]
                pk = next(iter(key_dict.values()))
                pk_val = pk.get("S", pk) if isinstance(pk, dict) else str(pk)
                item = target_table.items[pk_val]
                vals = u.get("ExpressionAttributeValues", {})
                update_expr = u.get("UpdateExpression", "")

                if ":recipient_id" in vals:
                    item["matched_recipient_id"] = vals[":recipient_id"]["S"]
                if ":matched_status" in vals:
                    item["status"] = vals[":matched_status"]["S"]
                if ":date_status" in vals:
                    item["date_status"] = vals[":date_status"]["S"]
                if ":qty" in vals:
                    qty = float(vals[":qty"]["N"])
                    if "+ :qty" in update_expr:
                        item["capacity_kg_remaining"] = (
                            float(item["capacity_kg_remaining"]) + qty
                        )
                    elif "- :qty" in update_expr:
                        item["capacity_kg_remaining"] = (
                            float(item["capacity_kg_remaining"]) - qty
                        )

                if "REMOVE matched_recipient_id" in update_expr:
                    item["matched_recipient_id"] = None
                    item["status"] = DonationStatus.REPORTED.value

        return {}


# ------------------------------------------------------------------------------
# Test Environment Setup
# ------------------------------------------------------------------------------
def create_simulation_environment() -> tuple[
    StrandsOrchestrator,
    FrontendApiService,
    AgentSessionManager,
    StatefulMockDynamoDBResource,
    AppConfig,
]:
    """Wire up stateful repositories and service layers for simulation."""
    config = AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-sim",
        recipients_table_name="frca-recipients-sim",
        volunteers_table_name="frca-volunteers-sim",
        matches_audit_table_name="frca-audit-sim",
        sessions_memory_table_name="frca-sessions-sim",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:sim-notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:sim-escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/sim-dlq",
        location_place_index_name="sim-index",
        route_calculator_name="sim-calc",
        bedrock_agent_id="sim-agent",
        bedrock_agent_alias_id="sim-alias",
        coordinator_api_key="sim-coordinator-secret-key-32chars",
    )
    mock_dynamo = StatefulMockDynamoDBResource()

    donations_repo = DonationsRepository(dynamodb_resource=mock_dynamo, config=config)
    recipients_repo = RecipientsRepository(dynamodb_resource=mock_dynamo, config=config)
    volunteers_repo = VolunteersRepository(dynamodb_resource=mock_dynamo, config=config)
    audit_repo = AuditRepository(dynamodb_resource=mock_dynamo, config=config)
    session_manager = AgentSessionManager(dynamodb_resource=mock_dynamo, config=config)

    mock_sns = mock.MagicMock()
    mock_sns.publish.return_value = {"MessageId": "msg-sim-001"}

    distance_calc = GeodesicDistanceCalculator()

    orchestrator = StrandsOrchestrator(
        donations_repo=donations_repo,
        recipients_repo=recipients_repo,
        volunteers_repo=volunteers_repo,
        audit_repo=audit_repo,
        distance_calculator=distance_calc,
        sns_client=mock_sns,
        config=config,
    )

    frontend_api = FrontendApiService(
        donations_repo=donations_repo,
        recipients_repo=recipients_repo,
        volunteers_repo=volunteers_repo,
        audit_repo=audit_repo,
        orchestrator=orchestrator,
        config=config,
        dynamodb_resource=mock_dynamo,
    )

    return orchestrator, frontend_api, session_manager, mock_dynamo, config


def seed_entities(
    recipients_repo: RecipientsRepository,
    volunteers_repo: VolunteersRepository,
) -> None:
    """Seed 4 diverse recipients and 5 multi-capacity volunteers."""
    # 4 Recipients
    recipients = [
        Recipient(
            recipient_id="rec-soup-kitchen-01",
            organization_name="Downtown Soup Kitchen",
            contact_name="Sarah Director",
            contact_phone="+15555552001",
            address="100 Market St, Metro Core",
            coordinates=Coordinates(latitude=37.7752, longitude=-122.4180),
            capacity_kg_remaining=100.0,
            dietary_requirements=[],
            status=RecipientStatus.ACTIVE,
            service_region="metro-core",
            auth_token_hash=hashlib.sha256(
                b"rec-secret-rec-soup-kitchen-01"
            ).hexdigest(),
        ),
        Recipient(
            recipient_id="rec-shelter-01",
            organization_name="Community Hope Shelter",
            contact_name="James Manager",
            contact_phone="+15555552002",
            address="200 Mission St, Metro Core",
            coordinates=Coordinates(latitude=37.7800, longitude=-122.4200),
            capacity_kg_remaining=60.0,
            dietary_requirements=[],
            status=RecipientStatus.ACTIVE,
            service_region="metro-core",
            auth_token_hash=hashlib.sha256(b"rec-secret-rec-shelter-01").hexdigest(),
        ),
        Recipient(
            recipient_id="rec-youth-center-01",
            organization_name="City Youth Center",
            contact_name="Elena Coordinator",
            contact_phone="+15555552003",
            address="300 Howard St, Metro Core",
            coordinates=Coordinates(latitude=37.7700, longitude=-122.4300),
            capacity_kg_remaining=40.0,
            dietary_requirements=[],
            status=RecipientStatus.ACTIVE,
            service_region="metro-core",
            auth_token_hash=hashlib.sha256(
                b"rec-secret-rec-youth-center-01"
            ).hexdigest(),
        ),
        Recipient(
            recipient_id="rec-family-pantry-01",
            organization_name="Family Support Pantry",
            contact_name="David Steward",
            contact_phone="+15555552004",
            address="400 Folsom St, Metro Core",
            coordinates=Coordinates(latitude=37.7850, longitude=-122.4100),
            capacity_kg_remaining=80.0,
            dietary_requirements=[],
            status=RecipientStatus.ACTIVE,
            service_region="metro-core",
            auth_token_hash=hashlib.sha256(
                b"rec-secret-rec-family-pantry-01"
            ).hexdigest(),
        ),
    ]
    for r in recipients:
        recipients_repo.create_recipient(r)

    # 5 Volunteers (2 Bikes, 2 Cars, 1 Van)
    volunteers = [
        Volunteer(
            volunteer_id="vol-bike-01",
            volunteer_name="Alex Courier",
            phone="+15555553001",
            address="10 Bike Hub, Metro Core",
            coordinates=Coordinates(latitude=37.7760, longitude=-122.4170),
            status=VolunteerStatus.AVAILABLE,
            max_capacity_kg=15.0,
            vehicle_type="bike",
            service_region="metro-core",
            auth_token_hash=hashlib.sha256(b"vol-secret-vol-bike-01").hexdigest(),
        ),
        Volunteer(
            volunteer_id="vol-bike-02",
            volunteer_name="Taylor Bike",
            phone="+15555553002",
            address="20 Bike Lane, Metro Core",
            coordinates=Coordinates(latitude=37.7770, longitude=-122.4190),
            status=VolunteerStatus.AVAILABLE,
            max_capacity_kg=15.0,
            vehicle_type="bike",
            service_region="metro-core",
            auth_token_hash=hashlib.sha256(b"vol-secret-vol-bike-02").hexdigest(),
        ),
        Volunteer(
            volunteer_id="vol-car-01",
            volunteer_name="Jordan Driver",
            phone="+15555553003",
            address="30 Garage Way, Metro Core",
            coordinates=Coordinates(latitude=37.7810, longitude=-122.4150),
            status=VolunteerStatus.AVAILABLE,
            max_capacity_kg=60.0,
            vehicle_type="car",
            service_region="metro-core",
            auth_token_hash=hashlib.sha256(b"vol-secret-vol-car-01").hexdigest(),
        ),
        Volunteer(
            volunteer_id="vol-car-02",
            volunteer_name="Casey Transit",
            phone="+15555553004",
            address="40 Auto Blvd, Metro Core",
            coordinates=Coordinates(latitude=37.7830, longitude=-122.4140),
            status=VolunteerStatus.AVAILABLE,
            max_capacity_kg=60.0,
            vehicle_type="car",
            service_region="metro-core",
            auth_token_hash=hashlib.sha256(b"vol-secret-vol-car-02").hexdigest(),
        ),
        Volunteer(
            volunteer_id="vol-van-01",
            volunteer_name="Morgan Van",
            phone="+15555553005",
            address="50 Depot Ave, Metro Core",
            coordinates=Coordinates(latitude=37.7870, longitude=-122.4080),
            status=VolunteerStatus.AVAILABLE,
            max_capacity_kg=250.0,
            vehicle_type="van",
            service_region="metro-core",
            auth_token_hash=hashlib.sha256(b"vol-secret-vol-van-01").hexdigest(),
        ),
    ]
    for v in volunteers:
        volunteers_repo.create_volunteer(v)


# ------------------------------------------------------------------------------
# Day 1: Autonomous Multi-Party Surge
# ------------------------------------------------------------------------------
def test_day_1_autonomous_multi_party_surge() -> None:
    """Verify Day 1 multi-party surge coordinates 4 donations autonomously."""
    orchestrator, _, session_mgr, mock_dynamo, config = create_simulation_environment()
    donations_repo = DonationsRepository(dynamodb_resource=mock_dynamo, config=config)
    recipients_repo = RecipientsRepository(dynamodb_resource=mock_dynamo, config=config)
    volunteers_repo = VolunteersRepository(dynamodb_resource=mock_dynamo, config=config)
    audit_repo = AuditRepository(dynamodb_resource=mock_dynamo, config=config)

    seed_entities(recipients_repo, volunteers_repo)

    now = datetime.now(timezone.utc)

    # 4 Surge Donations across morning, afternoon, and evening
    donations_data = [
        # Morning 1: Sunrise Bakery (15kg, bakery) -> Fits Bike 1
        Donation(
            donation_id="don-day1-01",
            donor_id="donor-bakery-01",
            donor_name="Sunrise Artisan Bakery",
            donor_phone="+15555551001",
            donor_address="10 Baker St, Metro Core",
            donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
            food_category=FoodCategory.BAKERY,
            quantity_kg=15.0,
            ready_by=now + timedelta(hours=1),
            perishability_hours=6.0,
            service_region="metro-core",
            created_at=now,
        ),
        # Morning 2: FreshFields Supermarket (50kg, produce) -> Needs Car
        Donation(
            donation_id="don-day1-02",
            donor_id="donor-supermarket-01",
            donor_name="FreshFields Supermarket",
            donor_phone="+15555551002",
            donor_address="20 Market St, Metro Core",
            donor_coordinates=Coordinates(latitude=37.7833, longitude=-122.4167),
            food_category=FoodCategory.PRODUCE,
            quantity_kg=50.0,
            ready_by=now + timedelta(hours=2),
            perishability_hours=12.0,
            service_region="metro-core",
            created_at=now + timedelta(hours=1),
        ),
        # Afternoon: Grand Banquet Caterer (30kg, prepared meals) -> Needs Car
        Donation(
            donation_id="don-day1-03",
            donor_id="donor-caterer-01",
            donor_name="Grand Banquet Caterer",
            donor_phone="+15555551003",
            donor_address="30 Chef Blvd, Metro Core",
            donor_coordinates=Coordinates(latitude=37.7690, longitude=-122.4467),
            food_category=FoodCategory.PREPARED_MEALS,
            quantity_kg=30.0,
            ready_by=now + timedelta(hours=5),
            perishability_hours=4.0,
            service_region="metro-core",
            created_at=now + timedelta(hours=4),
        ),
        # Evening: Metropolitan Hotel (45kg, prepared meals) -> Needs Car or Van
        Donation(
            donation_id="don-day1-04",
            donor_id="donor-hotel-01",
            donor_name="Metropolitan Hotel",
            donor_phone="+15555551004",
            donor_address="40 Plaza Way, Metro Core",
            donor_coordinates=Coordinates(latitude=37.7880, longitude=-122.4075),
            food_category=FoodCategory.PREPARED_MEALS,
            quantity_kg=45.0,
            ready_by=now + timedelta(hours=9),
            perishability_hours=5.0,
            service_region="metro-core",
            created_at=now + timedelta(hours=8),
        ),
    ]

    for donation in donations_data:
        donations_repo.create_donation(donation)
        res = orchestrator.coordinate_donation(
            donation_id=donation.donation_id,
            correlation_id=f"session-day1-{donation.donation_id}",
        )
        assert res.status == DonationStatus.ASSIGNED
        assert res.escalation_ticket is None
        assert PipelineStep.ASSIGN_VOLUNTEER in res.steps_completed
        assert PipelineStep.DISPATCH_NOTIFICATIONS in res.steps_completed

        # Record outcome in session memory store
        session_mgr.record_donation_outcome(
            service_region="metro-core",
            quantity_kg=donation.quantity_kg,
            outcome=res.status.value,
            recipient_id=res.matched_recipient_id or "",
            volunteer_id=res.assigned_volunteer_id or "",
        )

    # Verify All 4 Donations are Persisted as ASSIGNED
    for donation in donations_data:
        persisted = donations_repo.get_donation(donation.donation_id)
        assert persisted is not None
        assert persisted.status == DonationStatus.ASSIGNED
        assert persisted.matched_recipient_id is not None
        assert persisted.assigned_volunteer_id is not None
        assert persisted.date_status is not None
        assert "assigned" in persisted.date_status

    # Verify Audit Trail and Masked PII
    for donation in donations_data:
        audit_events = audit_repo.query_audit_trail_by_donation(donation.donation_id)
        assert len(audit_events) >= 2
        for evt in audit_events:
            # PII must never be raw in audit details
            if "destination" in evt.details:
                dest = evt.details["destination"]
                assert "*" in dest
                assert dest != donation.donor_phone

    # Verify Recipient Capacities were atomically deducted across recipients
    final_total_cap = sum(
        recipients_repo.get_recipient(r_id).capacity_kg_remaining
        for r_id in (
            "rec-soup-kitchen-01",
            "rec-shelter-01",
            "rec-youth-center-01",
            "rec-family-pantry-01",
        )
    )
    total_surge_kg = sum(d.quantity_kg for d in donations_data)
    assert round(280.0 - final_total_cap, 2) == round(total_surge_kg, 2)


# ------------------------------------------------------------------------------
# Day 2: Midnight Rollover, Depleted Bypass, Restocking, and Shifts
# ------------------------------------------------------------------------------
def test_day_2_rollover_depleted_bypass_restock_and_shifts() -> None:
    """Verify Day 2 date rollover, depleted bypass, REST restock, and shifts."""
    orchestrator, frontend_api, _, mock_dynamo, config = create_simulation_environment()
    donations_repo = DonationsRepository(dynamodb_resource=mock_dynamo, config=config)
    recipients_repo = RecipientsRepository(dynamodb_resource=mock_dynamo, config=config)
    volunteers_repo = VolunteersRepository(dynamodb_resource=mock_dynamo, config=config)

    seed_entities(recipients_repo, volunteers_repo)

    day2_date = datetime.now(timezone.utc)
    day1_date = day2_date - timedelta(days=1)
    day1_str = day1_date.strftime("%Y-%m-%d")

    # 1. Simulate Day 1 completed donation with Day 1 date_status
    don_day1 = Donation(
        donation_id="don-day1-legacy",
        donor_id="donor-bakery-01",
        donor_name="Sunrise Artisan Bakery",
        donor_phone="+15555551001",
        donor_address="10 Baker St, Metro Core",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.BAKERY,
        quantity_kg=15.0,
        ready_by=day1_date + timedelta(hours=1),
        perishability_hours=6.0,
        service_region="metro-core",
        status=DonationStatus.ASSIGNED,
        matched_recipient_id="rec-youth-center-01",
        assigned_volunteer_id="vol-bike-01",
        date_status=f"{day1_str}#assigned",
        created_at=day1_date,
    )
    donations_repo.create_donation(don_day1)

    # Deplete Youth Center capacity to only 5kg
    youth_center = recipients_repo.get_recipient("rec-youth-center-01")
    assert youth_center is not None
    recipients_repo.update_recipient_capacity(
        recipient_id="rec-youth-center-01",
        capacity_kg_remaining=5.0,
    )

    # 2. Midnight Rollover: EventBridge daily reconciliation query
    daily_summary = donations_repo.get_authoritative_daily_summary(
        "metro-core", day1_str
    )
    assert daily_summary.total_kg_routed >= 15.0

    # 3. Depleted Recipient Bypass:
    # A new 35kg donation arrives; Youth Center (5kg) bypassed for capacity
    don_day2_01 = Donation(
        donation_id="don-day2-01",
        donor_id="donor-caterer-01",
        donor_name="Grand Banquet Caterer",
        donor_phone="+15555551003",
        donor_address="30 Chef Blvd, Metro Core",
        donor_coordinates=Coordinates(latitude=37.7690, longitude=-122.4467),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=35.0,
        ready_by=day2_date + timedelta(hours=2),
        perishability_hours=6.0,
        service_region="metro-core",
        created_at=day2_date,
    )
    donations_repo.create_donation(don_day2_01)

    res1 = orchestrator.coordinate_donation("don-day2-01")
    assert res1.status == DonationStatus.ASSIGNED
    assert res1.matched_recipient_id != "rec-youth-center-01"  # Bypassed!
    assert res1.matched_recipient_id in (
        "rec-soup-kitchen-01",
        "rec-shelter-01",
        "rec-family-pantry-01",
    )

    # 4. Mid-Day Capacity Restock via Frontend API:
    # Youth Center restocks capacity to 90.0 kg via POST /api/recipients/{id}/capacity
    restock_event = {
        "httpMethod": "POST",
        "path": "/api/recipients/rec-youth-center-01/capacity",
        "headers": {
            "X-Recipient-Token": "rec-secret-rec-youth-center-01",
            "Content-Type": "application/json",
        },
        "body": json.dumps({"capacity_kg_remaining": 90.0}),
    }
    restock_resp = frontend_api.handle_request(restock_event)
    assert restock_resp["statusCode"] == 200
    updated_yc = recipients_repo.get_recipient("rec-youth-center-01")
    assert updated_yc is not None
    assert updated_yc.capacity_kg_remaining == 90.0

    # 5. Volunteer Shift Toggle via Frontend API:
    # Active Volunteer Casey Transit (vol-car-02) clocks off shift
    shift_event = {
        "httpMethod": "POST",
        "path": "/api/volunteers/vol-car-02/availability",
        "headers": {
            "X-Volunteer-Token": "vol-secret-vol-car-02",
            "Content-Type": "application/json",
        },
        "body": json.dumps({"status": "UNAVAILABLE"}),
    }
    shift_resp = frontend_api.handle_request(shift_event)
    assert shift_resp["statusCode"] == 200
    v2 = volunteers_repo.get_volunteer("vol-car-02")
    assert v2 is not None
    assert v2.status == VolunteerStatus.UNAVAILABLE

    # 6. Coordinate new car-sized donation:
    # Orchestrator must bypass busy vol-car-01 and vol-car-02, dispatching vol-van-01
    don_day2_02 = Donation(
        donation_id="don-day2-02",
        donor_id="donor-supermarket-01",
        donor_name="FreshFields Supermarket",
        donor_phone="+15555551002",
        donor_address="20 Market St, Metro Core",
        donor_coordinates=Coordinates(latitude=37.7833, longitude=-122.4167),
        food_category=FoodCategory.PRODUCE,
        quantity_kg=40.0,
        ready_by=day2_date + timedelta(hours=4),
        perishability_hours=8.0,
        service_region="metro-core",
        created_at=day2_date + timedelta(hours=1),
    )
    donations_repo.create_donation(don_day2_02)

    res2 = orchestrator.coordinate_donation("don-day2-02")
    assert res2.status == DonationStatus.ASSIGNED
    assert res2.assigned_volunteer_id == "vol-van-01"


# ------------------------------------------------------------------------------
# Day 3: Boundaries, Exhaustion, and Coordinator Resolution
# ------------------------------------------------------------------------------
def test_day_3_boundaries_exhaustion_and_coordinator_resolution() -> None:
    """Verify Day 3 near-expiry escalation, exhaustion, and coordinator resolution."""
    orchestrator, frontend_api, _, mock_dynamo, config = create_simulation_environment()
    donations_repo = DonationsRepository(dynamodb_resource=mock_dynamo, config=config)
    recipients_repo = RecipientsRepository(dynamodb_resource=mock_dynamo, config=config)
    volunteers_repo = VolunteersRepository(dynamodb_resource=mock_dynamo, config=config)
    audit_repo = AuditRepository(dynamodb_resource=mock_dynamo, config=config)

    seed_entities(recipients_repo, volunteers_repo)

    now_utc = datetime.now(timezone.utc)

    # 1. Food Safety Expiry Boundary: Shelf life < 60 minutes (0.5 hours)
    # ready_by in future (5m); remaining shelf life = 5m + 30m = 35 minutes (<60m)
    near_expiry_don = Donation(
        donation_id="don-day3-expiry",
        donor_id="donor-caterer-01",
        donor_name="Grand Banquet Caterer",
        donor_phone="+15555551003",
        donor_address="30 Chef Blvd, Metro Core",
        donor_coordinates=Coordinates(latitude=37.7690, longitude=-122.4467),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=20.0,
        ready_by=now_utc + timedelta(minutes=5),
        perishability_hours=0.5,  # 30 minutes! < 60 min threshold
        service_region="metro-core",
        created_at=now_utc,
    )
    donations_repo.create_donation(near_expiry_don)

    res_expiry = orchestrator.coordinate_donation("don-day3-expiry")
    assert res_expiry.status == DonationStatus.ESCALATED
    assert res_expiry.escalation_ticket is not None
    assert (
        res_expiry.escalation_ticket.reason
        == EscalationReason.FOOD_SAFETY_THRESHOLD_BREACH
    )
    persisted_expiry = donations_repo.get_donation("don-day3-expiry")
    assert persisted_expiry is not None
    assert persisted_expiry.status == DonationStatus.ESCALATED

    # 2. Total Recipient Capacity Exhaustion:
    # Exhaust all recipients to 0 kg remaining
    for rec_id in (
        "rec-soup-kitchen-01",
        "rec-shelter-01",
        "rec-youth-center-01",
        "rec-family-pantry-01",
    ):
        recipients_repo.update_recipient_capacity(rec_id, capacity_kg_remaining=0.0)

    massive_don = Donation(
        donation_id="don-day3-exhausted",
        donor_id="donor-hotel-01",
        donor_name="Metropolitan Hotel",
        donor_phone="+15555551004",
        donor_address="40 Plaza Way, Metro Core",
        donor_coordinates=Coordinates(latitude=37.7880, longitude=-122.4075),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=50.0,
        ready_by=now_utc + timedelta(hours=3),
        perishability_hours=8.0,
        service_region="metro-core",
        created_at=now_utc,
    )
    donations_repo.create_donation(massive_don)

    res_exhausted = orchestrator.coordinate_donation("don-day3-exhausted")
    assert res_exhausted.status == DonationStatus.ESCALATED
    assert res_exhausted.escalation_ticket is not None
    assert (
        res_exhausted.escalation_ticket.reason
        == EscalationReason.NO_MATCH_WITHIN_WINDOW
    )

    # 3. Volunteer Unavailability Rollback:
    # Restore one recipient capacity, but set all volunteers to UNAVAILABLE
    recipients_repo.update_recipient_capacity(
        "rec-soup-kitchen-01", capacity_kg_remaining=50.0
    )
    for vol_id in (
        "vol-bike-01",
        "vol-bike-02",
        "vol-car-01",
        "vol-car-02",
        "vol-van-01",
    ):
        volunteers_repo.set_volunteer_availability(vol_id, is_available=False)

    no_vol_don = Donation(
        donation_id="don-day3-novol",
        donor_id="donor-bakery-01",
        donor_name="Sunrise Artisan Bakery",
        donor_phone="+15555551001",
        donor_address="10 Baker St, Metro Core",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.BAKERY,
        quantity_kg=15.0,
        ready_by=now_utc + timedelta(hours=2),
        perishability_hours=6.0,
        service_region="metro-core",
        created_at=now_utc,
    )
    donations_repo.create_donation(no_vol_don)

    res_novol = orchestrator.coordinate_donation("don-day3-novol")
    assert res_novol.status == DonationStatus.ESCALATED
    assert res_novol.escalation_ticket is not None
    assert res_novol.escalation_ticket.reason == EscalationReason.NO_MATCH_WITHIN_WINDOW
    # Recipient capacity must be restored back to 50.0 kg (unclaim rollback)
    soup_kitchen_check = recipients_repo.get_recipient("rec-soup-kitchen-01")
    assert soup_kitchen_check is not None
    assert soup_kitchen_check.capacity_kg_remaining == 50.0

    # 4. Coordinator Login and Escalation Resolution:
    # Coordinator logs in
    login_event = {
        "httpMethod": "POST",
        "path": "/api/coordinator/login",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"api_key": config.coordinator_api_key}),
    }
    login_resp = frontend_api.handle_request(login_event)
    assert login_resp["statusCode"] == 200
    token_data = json.loads(login_resp["body"])
    bearer_token = token_data["token"]

    # Coordinator queries escalations queue
    list_event = {
        "httpMethod": "GET",
        "path": "/api/coordinator/escalations",
        "headers": {"Authorization": f"Bearer {bearer_token}"},
    }
    list_resp = frontend_api.handle_request(list_event)
    assert list_resp["statusCode"] == 200
    escalations_list = json.loads(list_resp["body"])["escalations"]
    assert len(escalations_list) >= 3

    # Coordinator resolves near-expiry donation manually
    resolve_event = {
        "httpMethod": "POST",
        "path": "/api/coordinator/escalations/don-day3-expiry/resolve",
        "headers": {
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        },
        "body": json.dumps(
            {
                "resolution_action": "assign_recipient",
                "target_id": "rec-soup-kitchen-01",
                "notes": "Emergency distribution approved directly with on-site staff",
            }
        ),
    }
    resolve_resp = frontend_api.handle_request(resolve_event)
    assert resolve_resp["statusCode"] == 200

    resolved_don = donations_repo.get_donation("don-day3-expiry")
    assert resolved_don is not None
    assert resolved_don.status == DonationStatus.MATCHED
    assert resolved_don.matched_recipient_id == "rec-soup-kitchen-01"

    # Verify coordinator manual resolution audit record
    audit_events = audit_repo.query_audit_trail_by_donation("don-day3-expiry")
    actions = [evt.action for evt in audit_events]
    assert "COORDINATOR_MANUAL_RESOLUTION" in actions
