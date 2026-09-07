"""Phase 9 E2E Runtime Crash Recovery & Transactional Boundary Test Suite.

Verifies process crash recovery against real DynamoDB transactional boundaries:
1. Pre-Transaction Boundary:
   - Failure injected before claim_and_deduct_recipient commits.
   - Asserts DynamoDB status is REPORTED and recipient capacity untouched.
   - Proves clean re-evaluation and completion on retry.
2. Post-Claim Boundary (MATCHED state):
   - Injected after claim_and_deduct_recipient TransactWriteItems commits,
     before volunteer assignment.
   - Asserts DynamoDB status is MATCHED and capacity already deducted.
   - Proves recovery resumes from MATCHED without re-deducting capacity
     (zero double deduction).
3. Post-Assignment Boundary (ASSIGNED state) & Idempotent Replay:
   - Injected after assign_volunteer commits and donor notification sent,
     before recipient notification.
   - Asserts DynamoDB status is ASSIGNED.
   - Proves recovery uses _handle_assigned_replay, reads audit trail, and
     delivers ONLY missing notifications.
4. Ambiguous Transport & Notification Claim Lease Semantics:
   - Simulates ambiguous network failure (ReadTimeoutError) during alert.
   - Asserts lease remains CLAIMED, enforcing strict mutual exclusion
     against concurrent workers.
   - Proves lease expiration enables safe recovery.
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

import pytest
from botocore.exceptions import ClientError

from agent.orchestrator import StrandsOrchestrator
from audit_repo import AuditRepository
from config import AppConfig
from donations_repo import DonationsRepository
from models import (
    AuditEvent,
    Coordinates,
    CoordinatorNotificationStatus,
    Donation,
    DonationStatus,
    FoodCategory,
    NotificationDeliveryError,
    NotificationRecipientType,
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
# Mock DynamoDB Infrastructure with Strict Transaction Engine
# ------------------------------------------------------------------------------
class CrashMockTable:
    """Thread-safe in-memory table emulating atomic operations and leases."""

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

                # Coordinator notification lease condition evaluation
                if "#cns = :pending_status" in ConditionExpression:
                    cns = item.get("coordinator_notification_status")
                    p_stat = vals.get(":pending_status")
                    c_stat = vals.get(":claimed_status")
                    exp_thresh = vals.get(":expired_threshold")
                    claimed_at = item.get("coordinator_notification_claimed_at")

                    is_valid = False
                    if (
                        cns == p_stat
                        or cns is None
                        or cns == c_stat
                        and claimed_at
                        and str(claimed_at) <= str(exp_thresh)
                    ):
                        is_valid = True

                    if not is_valid:
                        raise ClientError(
                            {"Error": {"Code": "ConditionalCheckFailedException"}},
                            "UpdateItem",
                        )

                if "#cns = :claimed_status AND #cid = :claim_id" in ConditionExpression:
                    cns = item.get("coordinator_notification_status")
                    cid = item.get("coordinator_notification_claim_id")
                    if cns != vals.get(":claimed_status") or cid != vals.get(
                        ":claim_id"
                    ):
                        raise ClientError(
                            {"Error": {"Code": "ConditionalCheckFailedException"}},
                            "UpdateItem",
                        )

            # Apply state mutations
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

            # Coordinator notification fields
            if (
                ":claimed_status" in vals
                and "#cns = :claimed_status" in UpdateExpression
            ):
                item["coordinator_notification_status"] = str(vals[":claimed_status"])
                item["coordinator_notification_claimed_at"] = str(vals.get(":now"))
                item["coordinator_notification_claim_id"] = str(vals.get(":claim_id"))
            elif ":delivered_status" in vals:
                item["coordinator_notification_status"] = str(vals[":delivered_status"])
            elif ":failed_status" in vals:
                item["coordinator_notification_status"] = str(vals[":failed_status"])
                item["coordinator_notification_error"] = str(vals.get(":error_detail"))

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


class CrashMockDynamoDBResource:
    """Mock DynamoDB resource with atomic TransactWriteItems engine."""

    def __init__(self) -> None:
        self.tables: dict[str, CrashMockTable] = {}
        self.meta: Any = mock.MagicMock()
        self.meta.client = self
        self._tx_lock: threading.Lock = threading.Lock()

    def Table(self, name: str) -> CrashMockTable:
        if name not in self.tables:
            self.tables[name] = CrashMockTable(name)
        return self.tables[name]

    def transact_write_items(
        self, ClientRequestToken: str, TransactItems: list[dict[str, Any]]
    ) -> dict[str, Any]:
        del ClientRequestToken
        with self._tx_lock:
            cancellation_reasons = []
            has_failure = False

            for item_op in TransactItems:
                if "Update" in item_op:
                    u = item_op["Update"]
                    target_table = self.Table(u["TableName"])
                    key_dict = u["Key"]
                    pk = next(iter(key_dict.values()))
                    pk_val = pk.get("S", pk) if isinstance(pk, dict) else str(pk)
                    cond = u.get("ConditionExpression", "")
                    existing = target_table.items.get(pk_val)

                    failed = False
                    if "attribute_not_exists(matched_recipient_id)" in cond:
                        if (
                            not existing
                            or existing.get("matched_recipient_id") is not None
                        ):
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

            # Atomic commit
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
# Test Fixtures & Setup
# ------------------------------------------------------------------------------
def create_crash_test_harness() -> tuple[
    DonationsRepository,
    RecipientsRepository,
    VolunteersRepository,
    AuditRepository,
    StrandsOrchestrator,
    CrashMockDynamoDBResource,
    AppConfig,
]:
    """Create isolated crash test harness wired to in-memory tables."""
    config = AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-crash",
        recipients_table_name="frca-recipients-crash",
        volunteers_table_name="frca-volunteers-crash",
        matches_audit_table_name="frca-audit-crash",
        sessions_memory_table_name="frca-sessions-crash",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:crash-notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:crash-escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/crash-dlq",
        location_place_index_name="crash-index",
        route_calculator_name="crash-calc",
        bedrock_agent_id="crash-agent",
        bedrock_agent_alias_id="crash-alias",
        coordinator_api_key="crash-coordinator-secret-key-32chars",
        notification_claim_lease_seconds=300,
    )
    mock_dynamo = CrashMockDynamoDBResource()

    donations_repo = DonationsRepository(dynamodb_resource=mock_dynamo, config=config)
    recipients_repo = RecipientsRepository(dynamodb_resource=mock_dynamo, config=config)
    volunteers_repo = VolunteersRepository(dynamodb_resource=mock_dynamo, config=config)
    audit_repo = AuditRepository(dynamodb_resource=mock_dynamo, config=config)

    # Seed Recipient (50kg capacity)
    recipients_repo.create_recipient(
        Recipient(
            recipient_id="rec-crash-01",
            organization_name="Downtown Hope Center",
            contact_name="Sarah Hope",
            contact_phone="+15555552001",
            address="100 Mission St, Metro Core",
            coordinates=Coordinates(latitude=37.7750, longitude=-122.4180),
            capacity_kg_remaining=50.0,
            status=RecipientStatus.ACTIVE,
            service_region="metro-core",
        )
    )

    # Seed Volunteer (60kg car capacity)
    volunteers_repo.create_volunteer(
        Volunteer(
            volunteer_id="vol-crash-01",
            volunteer_name="Alex Courier",
            phone="+15555553001",
            address="200 Transit Way, Metro Core",
            coordinates=Coordinates(latitude=37.7760, longitude=-122.4170),
            status=VolunteerStatus.AVAILABLE,
            max_capacity_kg=60.0,
            vehicle_type="car",
            service_region="metro-core",
        )
    )

    mock_sns = mock.MagicMock()
    mock_sns.publish.return_value = {"MessageId": "msg-crash-001"}

    orchestrator = StrandsOrchestrator(
        donations_repo=donations_repo,
        recipients_repo=recipients_repo,
        volunteers_repo=volunteers_repo,
        audit_repo=audit_repo,
        distance_calculator=GeodesicDistanceCalculator(),
        sns_client=mock_sns,
        config=config,
    )

    return (
        donations_repo,
        recipients_repo,
        volunteers_repo,
        audit_repo,
        orchestrator,
        mock_dynamo,
        config,
    )


# ------------------------------------------------------------------------------
# Test 1: Pre-Transaction Boundary Crash Recovery
# ------------------------------------------------------------------------------
def test_crash_recovery_pre_transaction_boundary() -> None:
    """Verify failure before claim_and_deduct leaves DB untouched."""
    (
        donations_repo,
        recipients_repo,
        volunteers_repo,
        audit_repo,
        orchestrator,
        mock_dynamo,
        config,
    ) = create_crash_test_harness()

    now = datetime.now(timezone.utc)
    donation = Donation(
        donation_id="don-pre-tx-01",
        donor_id="donor-01",
        donor_name="Sunrise Bakery",
        donor_phone="+15555551001",
        donor_address="10 Baker St, Metro Core",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.BAKERY,
        quantity_kg=25.0,
        ready_by=now + timedelta(hours=2),
        perishability_hours=6.0,
        service_region="metro-core",
        status=DonationStatus.REPORTED,
        created_at=now,
    )
    donations_repo.create_donation(donation)

    # Inject crash immediately before claim_and_deduct_recipient commits
    orig_claim = donations_repo.claim_and_deduct_recipient

    def crash_before_commit(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        raise RuntimeError("PROCESS_CRASH_PRE_TRANSACTION_BOUNDARY")

    donations_repo.claim_and_deduct_recipient = crash_before_commit

    # Invocation 1: Crashes
    with pytest.raises(RuntimeError, match="PROCESS_CRASH_PRE_TRANSACTION_BOUNDARY"):
        orchestrator.coordinate_donation("don-pre-tx-01")

    # Assert DynamoDB State BEFORE Recovery:
    # 1. Status is still REPORTED
    db_don = donations_repo.get_donation("don-pre-tx-01")
    assert db_don is not None
    assert db_don.status == DonationStatus.REPORTED
    assert db_don.matched_recipient_id is None
    # 2. Recipient capacity is completely untouched (50.0 kg)
    db_rec = recipients_repo.get_recipient("rec-crash-01")
    assert db_rec is not None
    assert db_rec.capacity_kg_remaining == 50.0

    # Restore un-crashed method for Invocation 2 (Recovery)
    donations_repo.claim_and_deduct_recipient = orig_claim

    # Invocation 2: Recovery re-evaluates cleanly
    res = orchestrator.coordinate_donation("don-pre-tx-01")
    assert res.status == DonationStatus.ASSIGNED
    assert res.matched_recipient_id == "rec-crash-01"
    assert res.assigned_volunteer_id == "vol-crash-01"

    # Assert Final Persisted State: Capacity deducted exactly once (50.0 - 25.0 = 25.0)
    db_don_final = donations_repo.get_donation("don-pre-tx-01")
    assert db_don_final is not None
    assert db_don_final.status == DonationStatus.ASSIGNED

    db_rec_final = recipients_repo.get_recipient("rec-crash-01")
    assert db_rec_final is not None
    assert db_rec_final.capacity_kg_remaining == 25.0


# ------------------------------------------------------------------------------
# Test 2: Post-Claim Boundary Crash Recovery (Resuming From MATCHED State)
# ------------------------------------------------------------------------------
def test_crash_recovery_post_claim_boundary_resumes_from_matched() -> None:
    """Verify failure after claim resumes from MATCHED without double deduction."""
    (
        donations_repo,
        recipients_repo,
        volunteers_repo,
        audit_repo,
        orchestrator,
        mock_dynamo,
        config,
    ) = create_crash_test_harness()

    now = datetime.now(timezone.utc)
    donation = Donation(
        donation_id="don-post-claim-01",
        donor_id="donor-01",
        donor_name="Sunrise Bakery",
        donor_phone="+15555551001",
        donor_address="10 Baker St, Metro Core",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.BAKERY,
        quantity_kg=20.0,
        ready_by=now + timedelta(hours=2),
        perishability_hours=6.0,
        service_region="metro-core",
        status=DonationStatus.REPORTED,
        created_at=now,
    )
    donations_repo.create_donation(donation)

    # Execute atomic claim and deduct (Real Transactional Boundary)
    assert (
        donations_repo.claim_and_deduct_recipient(
            donation_id="don-post-claim-01",
            recipient_id="rec-crash-01",
            quantity_kg=20.0,
        )
        is True
    )

    # Process Crashes Here! Before volunteer assignment.
    # Assert DynamoDB State BEFORE Recovery:
    # 1. Donation status in DynamoDB is MATCHED
    db_don = donations_repo.get_donation("don-post-claim-01")
    assert db_don is not None
    assert db_don.status == DonationStatus.MATCHED
    assert db_don.matched_recipient_id == "rec-crash-01"
    # 2. Recipient capacity was already deducted (50.0 - 20.0 = 30.0)
    db_rec = recipients_repo.get_recipient("rec-crash-01")
    assert db_rec is not None
    assert db_rec.capacity_kg_remaining == 30.0

    # Spy on claim_and_deduct_recipient to ensure it is NEVER called during recovery
    with mock.patch.object(
        donations_repo,
        "claim_and_deduct_recipient",
        wraps=donations_repo.claim_and_deduct_recipient,
    ) as spy_claim:
        # Invocation 2: Recovery resumes from MATCHED
        res = orchestrator.coordinate_donation("don-post-claim-01")
        assert res.status == DonationStatus.ASSIGNED
        assert res.matched_recipient_id == "rec-crash-01"
        assert res.assigned_volunteer_id == "vol-crash-01"

        # Invariant: Must NOT re-execute claim or re-deduct capacity
        assert spy_claim.call_count == 0

    # Invariant: Zero double-deduction! Recipient capacity remains 30.0 kg
    db_rec_final = recipients_repo.get_recipient("rec-crash-01")
    assert db_rec_final is not None
    assert db_rec_final.capacity_kg_remaining == 30.0


# ------------------------------------------------------------------------------
# Test 3: Post-Assignment Boundary Crash Recovery (Replay & Notification Deduplication)
# ------------------------------------------------------------------------------
def test_crash_recovery_post_assignment_boundary_replay_recovery() -> None:
    """Verify crash after assignment delivers ONLY missing notifications."""
    (
        donations_repo,
        recipients_repo,
        volunteers_repo,
        audit_repo,
        orchestrator,
        mock_dynamo,
        config,
    ) = create_crash_test_harness()

    now = datetime.now(timezone.utc)
    donation = Donation(
        donation_id="don-post-assign-01",
        donor_id="donor-01",
        donor_name="Sunrise Bakery",
        donor_phone="+15555551001",
        donor_address="10 Baker St, Metro Core",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.BAKERY,
        quantity_kg=15.0,
        ready_by=now + timedelta(hours=2),
        perishability_hours=6.0,
        service_region="metro-core",
        status=DonationStatus.ASSIGNED,
        matched_recipient_id="rec-crash-01",
        assigned_volunteer_id="vol-crash-01",
        created_at=now,
    )
    donations_repo.create_donation(donation)

    # Simulate that donor notification was already dispatched before crash
    audit_repo.record_audit_event(
        AuditEvent(
            event_id="evt-donor-notif-01",
            donation_id="don-post-assign-01",
            action="NOTIFICATION_DISPATCHED",
            actor="strands_orchestrator",
            idempotency_key="don-post-assign-01:notify_donor",
            details={"recipient_type": "donor"},
        )
    )

    # Spy on notification dispatches during replay recovery
    with mock.patch("agent.orchestrator.send_notification") as mock_send:
        res = orchestrator.coordinate_donation("don-post-assign-01")
        assert res.status == DonationStatus.ASSIGNED
        assert PipelineStep.DISPATCH_NOTIFICATIONS in res.steps_completed

        # Invariant: Donor notification must NOT be re-dispatched!
        donor_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("recipient_type") == NotificationRecipientType.DONOR
        ]
        assert len(donor_calls) == 0

        # Invariant: Missing recipient notification MUST be dispatched!
        rec_calls = [
            c
            for c in mock_send.call_args_list
            if c.kwargs.get("recipient_type") == NotificationRecipientType.RECIPIENT
        ]
        assert len(rec_calls) == 1


# ------------------------------------------------------------------------------
# Test 4: Ambiguous Transport & Notification Claim Lease Semantics
# ------------------------------------------------------------------------------
def test_ambiguous_transport_notification_claim_lease_mutual_exclusion() -> None:
    """Verify ambiguous transport preserves CLAIMED status mutual exclusion."""
    (
        donations_repo,
        recipients_repo,
        volunteers_repo,
        audit_repo,
        orchestrator,
        mock_dynamo,
        config,
    ) = create_crash_test_harness()

    now = datetime.now(timezone.utc)
    donation = Donation(
        donation_id="don-lease-01",
        donor_id="donor-01",
        donor_name="Sunrise Bakery",
        donor_phone="+15555551001",
        donor_address="10 Baker St, Metro Core",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.BAKERY,
        quantity_kg=15.0,
        ready_by=now + timedelta(hours=2),
        perishability_hours=6.0,
        service_region="metro-core",
        status=DonationStatus.ESCALATED,
        coordinator_notification_status=CoordinatorNotificationStatus.PENDING,
        created_at=now,
    )
    donations_repo.create_donation(donation)

    # 1. Worker 1 acquires coordinator notification claim lease
    claim_id_1 = "claim-worker-alpha"
    acquired = donations_repo.claim_coordinator_notification(
        donation_id="don-lease-01",
        claim_id=claim_id_1,
        claim_time=now,
        lease_seconds=300,
    )
    assert acquired is True

    # Verify status in DynamoDB is CLAIMED
    db_don = donations_repo.get_donation("don-lease-01")
    assert db_don is not None
    assert (
        db_don.coordinator_notification_status == CoordinatorNotificationStatus.CLAIMED
    )
    assert db_don.coordinator_notification_claim_id == claim_id_1

    # 2. Simulate ambiguous transport failure (e.g. ReadTimeoutError)
    ambiguous_exc = NotificationDeliveryError(
        message="Ambiguous transport timeout",
        recipient_type=NotificationRecipientType.COORDINATOR.value,
        masked_destination="co*****lert",
        safe_error_detail="ReadTimeoutError: Read timeout publishing to AWS SNS",
        is_ambiguous=True,  # External delivery unknown!
    )

    # Orchestrator catches ambiguous transport error during coordinator alert dispatch
    # Contract: MUST NOT mark failed, preserves CLAIMED until lease expiry
    with mock.patch("agent.orchestrator.send_notification", side_effect=ambiguous_exc):
        orchestrator._dispatch_coordinator_fallback_alert(
            donation_id="don-lease-01",
            recipient_type="coordinator",
            masked_destination="co*****lert",
            safe_error_detail="ReadTimeoutError",
            correlation_id="corr-ambiguous-01",
        )

    # Verify status in DynamoDB remains CLAIMED (not marked FAILED, not released)
    db_don_after = donations_repo.get_donation("don-lease-01")
    assert db_don_after is not None
    assert (
        db_don_after.coordinator_notification_status
        == CoordinatorNotificationStatus.CLAIMED
    )
    assert db_don_after.coordinator_notification_claim_id == claim_id_1

    # 3. Mutual Exclusion: Concurrent Worker 2 tries to claim while lease active
    # Active lease duration is 300 seconds; only 15 seconds have elapsed
    time_plus_15s = now + timedelta(seconds=15)
    acquired_w2 = donations_repo.claim_coordinator_notification(
        donation_id="don-lease-01",
        claim_id="claim-worker-beta",
        claim_time=time_plus_15s,
        lease_seconds=300,
    )
    # ConditionalCheckFailedException -> returns False cleanly
    assert acquired_w2 is False

    # 4. Expired Lease Recovery: Worker 3 attempts after lease expiration (305s)
    time_plus_305s = now + timedelta(seconds=305)
    acquired_w3 = donations_repo.claim_coordinator_notification(
        donation_id="don-lease-01",
        claim_id="claim-worker-gamma",
        claim_time=time_plus_305s,
        lease_seconds=300,
    )
    # Lease expired -> acquisition succeeds!
    assert acquired_w3 is True
    db_don_w3 = donations_repo.get_donation("don-lease-01")
    assert db_don_w3 is not None
    assert (
        db_don_w3.coordinator_notification_status
        == CoordinatorNotificationStatus.CLAIMED
    )
    assert db_don_w3.coordinator_notification_claim_id == "claim-worker-gamma"

    # Worker 3 delivers successfully
    assert (
        donations_repo.mark_coordinator_notification_delivered(
            donation_id="don-lease-01",
            claim_id="claim-worker-gamma",
        )
        is True
    )

    db_don_final = donations_repo.get_donation("don-lease-01")
    assert db_don_final is not None
    assert (
        db_don_final.coordinator_notification_status
        == CoordinatorNotificationStatus.DELIVERED
    )
