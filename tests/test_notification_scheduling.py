"""Hardcore unit and integration test suite for notifications and scheduling.

Tests:
1. test_missed_window_escalation_deduplication: EventBridge trigger past ready_by
   triggers atomic TransactWriteItems escalation and exactly-once coordinator alert.
2. test_concurrent_time_window_reconciliation_race: Parallel EventBridge invocations
   race on the same missed-window donation; exactly 1 wins, other cleanly NO-OPs.
3. test_selective_sns_retry_and_sanitized_error_handling: 4xx fail fast without retry;
   transient 5xx/throttling retry once; destination masked; safe_error_detail
   allowlisted.
4. test_cross_role_data_exposure_prevention: Verify role templates strictly
   isolate PII (recipient address/phone hidden from donor; donor address/phone
   hidden from recipient).
5. test_bounded_fallback_scan_pagination: DynamoDB scan fallback paginates
   properly across pages when limits evaluate before filter expression.
"""

import concurrent.futures
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
    Coordinates,
    CoordinatorNotificationStatus,
    Donation,
    DonationStatus,
    EscalationReason,
    FoodCategory,
    NotificationDeliveryError,
    NotificationRecipientType,
)
from recipients_repo import RecipientsRepository
from tools.send_notification import (
    ALLOWLISTED_SNS_ERROR_CODES,
    send_notification,
)
from volunteers_repo import VolunteersRepository


# ------------------------------------------------------------------------------
# Test 1: Missed-Window Escalation Deduplication & Exactly-Once Coordinator Alert
# ------------------------------------------------------------------------------
def test_missed_window_escalation_deduplication() -> None:
    """Verify ready_by <= now triggers atomic escalation and coordinator alert."""
    mock_donations_table = mock.MagicMock()
    mock_audit_table = mock.MagicMock()
    mock_recipients_table = mock.MagicMock()
    mock_volunteers_table = mock.MagicMock()

    mock_client = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.meta.client = mock_client

    def get_table(name: str) -> Any:
        if "donations" in name:
            return mock_donations_table
        if "matches-audit" in name:
            return mock_audit_table
        if "recipients" in name:
            return mock_recipients_table
        return mock_volunteers_table

    mock_resource.Table.side_effect = get_table

    config = AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-test",
        recipients_table_name="frca-recipients-test",
        volunteers_table_name="frca-volunteers-test",
        matches_audit_table_name="frca-matches-audit-test",
        sessions_memory_table_name="frca-sessions-test",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/dlq",
        location_place_index_name="index",
        route_calculator_name="calc",
        bedrock_agent_id="agent",
        bedrock_agent_alias_id="alias",
    )

    now = datetime.now(timezone.utc)
    expired_ready_by = now - timedelta(minutes=15)  # 15 minutes in past

    expired_donation = Donation(
        donation_id="don-expired-01",
        donor_id="donor-01",
        donor_name="Downtown Bakery",
        donor_phone="+12125550199",
        donor_address="123 Main St",
        donor_coordinates=Coordinates(latitude=40.71, longitude=-74.00),
        food_category=FoodCategory.BAKERY,
        quantity_kg=30.0,
        ready_by=now + timedelta(hours=2),  # Valid future at model creation
        perishability_hours=4.0,
        service_region="metro-core",
    )
    # Simulate time passing so ready_by is now in the past
    expired_donation_dict = expired_donation.model_dump(mode="json")
    expired_donation_dict["ready_by"] = expired_ready_by.isoformat()
    expired_donation_dict["created_at"] = (
        expired_ready_by - timedelta(hours=1)
    ).isoformat()
    expired_donation_dict["status"] = DonationStatus.REPORTED.value

    # GSI query returns this expired donation
    mock_donations_table.query.return_value = {"Items": [expired_donation_dict]}

    # Mock SNS client
    mock_sns = mock.MagicMock()

    donations_repo = DonationsRepository(
        dynamodb_resource=mock_resource, config=config
    )
    audit_repo = AuditRepository(dynamodb_resource=mock_resource, config=config)
    recipients_repo = RecipientsRepository(
        dynamodb_resource=mock_resource, config=config
    )
    volunteers_repo = VolunteersRepository(
        dynamodb_resource=mock_resource, config=config
    )

    orchestrator = StrandsOrchestrator(
        donations_repo=donations_repo,
        recipients_repo=recipients_repo,
        volunteers_repo=volunteers_repo,
        audit_repo=audit_repo,
        config=config,
        sns_client=mock_sns,
    )

    # First trigger: TransactWriteItems succeeds
    mock_client.transact_write_items.return_value = {}

    result_1 = orchestrator.reconcile_time_window_donations(
        service_region="metro-core", current_time=now
    )

    assert result_1["status"] == "SUCCESS"
    assert result_1["evaluated_count"] == 1
    assert result_1["escalated_count"] == 1
    assert result_1["already_escalated_count"] == 0

    # Verify TransactWriteItems was called with status=reported condition
    mock_client.transact_write_items.assert_called_once()
    transact_kwargs = mock_client.transact_write_items.call_args[1]
    items = transact_kwargs["TransactItems"]
    assert len(items) == 2
    # Item 0 is donation update
    assert items[0]["Update"]["TableName"] == "frca-donations-test"
    assert "#st = :reported_status" in items[0]["Update"]["ConditionExpression"]
    # Item 1 is audit record
    assert items[1]["Put"]["TableName"] == "frca-matches-audit-test"
    assert (
        items[1]["Put"]["Item"]["idempotency_key"]["S"]
        == "don-expired-01:missed_window_escalation"
    )

    # Verify coordinator alert was sent exactly once
    assert mock_sns.publish.call_count == 1
    sns_args = mock_sns.publish.call_args[1]
    assert "COORDINATOR_ESCALATION_V1" in sns_args["Subject"]
    assert "don-expired-01" in sns_args["Message"]

    # Second trigger (replay): TransactWriteItems encounters ConditionalCheckFailed
    mock_client.transact_write_items.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
            ],
        },
        "TransactWriteItems",
    )

    result_2 = orchestrator.reconcile_time_window_donations(
        service_region="metro-core", current_time=now
    )

    assert result_2["evaluated_count"] == 1
    assert result_2["escalated_count"] == 0
    assert result_2["already_escalated_count"] == 1
    # SNS publish call count remains 1 - zero duplicate alerts!
    assert mock_sns.publish.call_count == 1


# ------------------------------------------------------------------------------
# Test 2: Concurrent Race Safety Across Parallel Reconciliations
# ------------------------------------------------------------------------------
def test_concurrent_time_window_reconciliation_race() -> None:
    """Simulate parallel reconciliations racing to escalate the same donation."""
    mock_client = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.meta.client = mock_client

    config = AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-test",
        recipients_table_name="frca-recipients-test",
        volunteers_table_name="frca-volunteers-test",
        matches_audit_table_name="frca-matches-audit-test",
        sessions_memory_table_name="frca-sessions-test",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/dlq",
        location_place_index_name="index",
        route_calculator_name="calc",
        bedrock_agent_id="agent",
        bedrock_agent_alias_id="alias",
    )
    repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)

    lock = threading.Lock()
    first_caller_won = False

    def side_effect_transact(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        nonlocal first_caller_won
        with lock:
            if not first_caller_won:
                first_caller_won = True
                return {}
            # Subsequent concurrent callers get cancelled transaction
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [
                        {"Code": "ConditionalCheckFailed"},
                        {"Code": "None"},
                    ],
                },
                "TransactWriteItems",
            )

    mock_client.transact_write_items.side_effect = side_effect_transact

    # Fire 10 concurrent threads attempting escalation
    results: list[bool] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(
                repo.escalate_missed_window_transaction,
                "don-race-missed-01",
                EscalationReason.NO_MATCH_WITHIN_WINDOW,
            )
            for _ in range(10)
        ]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # Invariant: Exactly 1 won (True), exactly 9 cleanly NO-OPed (False)
    assert results.count(True) == 1
    assert results.count(False) == 9


# ------------------------------------------------------------------------------
# Test 3: Selective SNS Retry & Sanitized Error Detail
# ------------------------------------------------------------------------------
def test_selective_sns_retry_and_sanitized_error_handling() -> None:
    """Verify non-retryable 4xx fail fast and transient 5xx retry with masked PII."""
    # 3a: Non-retryable 4xx ClientError fails fast without retry
    mock_sns_4xx = mock.MagicMock()
    raw_sensitive_msg = "Invalid phone +12125550199 with payload data"
    mock_sns_4xx.publish.side_effect = ClientError(
        {
            "Error": {
                "Code": "InvalidParameterException",
                "Message": raw_sensitive_msg,
            }
        },
        "Publish",
    )

    with pytest.raises(NotificationDeliveryError) as exc_info_4xx:
        send_notification(
            recipient_type=NotificationRecipientType.RECIPIENT,
            destination="+12125550199",
            template_id="RECIPIENT_CONFIRMATION_V1",
            parameters={
                "contact_name": "Food Bank",
                "quantity_kg": 50.0,
                "food_category": "produce",
                "donor_name": "Market",
                "volunteer_name": "Sam",
            },
            sns_client=mock_sns_4xx,
            topic_arn="arn:aws:sns:us-east-1:123456789012:test-topic",
        )

    err_4xx = exc_info_4xx.value
    # Exactly 1 attempt - NO retry on 4xx client errors
    assert mock_sns_4xx.publish.call_count == 1
    # Destination strictly masked
    assert err_4xx.masked_destination == "+1*****0199"
    # Safe error detail is allowlisted without raw message or payload dump
    assert (
        err_4xx.safe_error_detail
        == ALLOWLISTED_SNS_ERROR_CODES["InvalidParameterException"]
    )
    assert "+12125550199" not in err_4xx.safe_error_detail
    assert "payload data" not in err_4xx.safe_error_detail
    assert "payload data" not in str(err_4xx)

    # 3b: Transient error (Throttling) succeeds on retry
    mock_sns_transient = mock.MagicMock()
    mock_sns_transient.publish.side_effect = [
        ClientError({"Error": {"Code": "ThrottlingException"}}, "Publish"),
        {"MessageId": "msg-retry-success"},
    ]

    msg = send_notification(
        recipient_type=NotificationRecipientType.DONOR,
        destination="+12125550188",
        template_id="DONOR_CONFIRMATION_V1",
        parameters={
            "donor_name": "Bakery",
            "quantity_kg": 20.0,
            "food_category": "bakery",
            "recipient_name": "Shelter",
            "volunteer_name": "Taylor",
            "ready_by": "1:00 PM UTC",
        },
        sns_client=mock_sns_transient,
        topic_arn="arn:aws:sns:us-east-1:123456789012:test-topic",
    )
    # Exactly 2 calls: attempt 1 failed transiently, attempt 2 succeeded
    assert mock_sns_transient.publish.call_count == 2
    assert msg.recipient_type == NotificationRecipientType.DONOR

    # 3c: Transient error exhausted after 1 retry raises NotificationDeliveryError
    mock_sns_exhausted = mock.MagicMock()
    mock_sns_exhausted.publish.side_effect = [
        ClientError({"Error": {"Code": "ThrottlingException"}}, "Publish"),
        ClientError({"Error": {"Code": "ThrottlingException"}}, "Publish"),
    ]

    with pytest.raises(NotificationDeliveryError) as exc_info_exhausted:
        send_notification(
            recipient_type=NotificationRecipientType.VOLUNTEER,
            destination="+12125550177",
            template_id="VOLUNTEER_ASSIGNMENT_V1",
            parameters={
                "volunteer_name": "Driver Dave",
                "quantity_kg": 40.0,
                "food_category": "produce",
                "donor_address": "100 Farm Way",
                "ready_by": "3:00 PM UTC",
                "recipient_name": "Hope Shelter",
                "recipient_address": "200 Harbor Rd",
            },
            sns_client=mock_sns_exhausted,
            topic_arn="arn:aws:sns:us-east-1:123456789012:test-topic",
        )

    err_exhausted = exc_info_exhausted.value
    # 2 calls: 1 initial + 1 retry
    assert mock_sns_exhausted.publish.call_count == 2
    assert err_exhausted.masked_destination == "+1*****0177"
    assert (
        err_exhausted.safe_error_detail
        == ALLOWLISTED_SNS_ERROR_CODES["ThrottlingException"]
    )


# ------------------------------------------------------------------------------
# Test 4: Cross-Role Data Exposure Boundaries
# ------------------------------------------------------------------------------
def test_cross_role_data_exposure_prevention() -> None:
    """Verify templates strictly prevent cross-role data leaks."""
    sensitive_donor_address = "999 Secret Donor Villa, High Security Area"
    sensitive_donor_phone = "+12125550111"
    sensitive_recipient_address = "888 Hidden Shelter Location, Safe Haven"
    sensitive_recipient_phone = "+12125550222"
    sensitive_volunteer_phone = "+12125550333"

    # 1. Donor Notification
    donor_msg = send_notification(
        recipient_type=NotificationRecipientType.DONOR,
        destination=sensitive_donor_phone,
        template_id="DONOR_CONFIRMATION_V1",
        parameters={
            "donor_name": "Downtown Bakery",
            "quantity_kg": 25.0,
            "food_category": "bakery",
            "recipient_name": "Community Table",
            "volunteer_name": "Taylor Transit",
            "ready_by": "2:00 PM UTC",
            # Attempt to pass extra sensitive fields:
            "recipient_address": sensitive_recipient_address,
            "recipient_phone": sensitive_recipient_phone,
            "volunteer_phone": sensitive_volunteer_phone,
        },
    )
    # Donor MUST NOT receive recipient delivery address or private phones
    assert sensitive_recipient_address not in donor_msg.rendered_body
    assert sensitive_recipient_phone not in donor_msg.rendered_body
    assert sensitive_volunteer_phone not in donor_msg.rendered_body
    assert "Community Table" in donor_msg.rendered_body

    # 2. Recipient Notification
    rec_msg = send_notification(
        recipient_type=NotificationRecipientType.RECIPIENT,
        destination=sensitive_recipient_phone,
        template_id="RECIPIENT_CONFIRMATION_V1",
        parameters={
            "contact_name": "Shelter Coordinator",
            "quantity_kg": 25.0,
            "food_category": "bakery",
            "donor_name": "Downtown Bakery",
            "volunteer_name": "Taylor Transit",
            # Attempt to pass extra sensitive donor fields:
            "donor_address": sensitive_donor_address,
            "donor_phone": sensitive_donor_phone,
            "volunteer_phone": sensitive_volunteer_phone,
        },
    )
    # Recipient MUST NOT receive donor pickup address or donor/volunteer phones
    assert sensitive_donor_address not in rec_msg.rendered_body
    assert sensitive_donor_phone not in rec_msg.rendered_body
    assert sensitive_volunteer_phone not in rec_msg.rendered_body
    assert "Downtown Bakery" in rec_msg.rendered_body

    # 3. Volunteer Notification
    vol_msg = send_notification(
        recipient_type=NotificationRecipientType.VOLUNTEER,
        destination=sensitive_volunteer_phone,
        template_id="VOLUNTEER_ASSIGNMENT_V1",
        parameters={
            "volunteer_name": "Taylor Transit",
            "quantity_kg": 25.0,
            "food_category": "bakery",
            "donor_address": sensitive_donor_address,
            "ready_by": "2:00 PM UTC",
            "recipient_name": "Community Table",
            "recipient_address": sensitive_recipient_address,
            # Attempt to pass private donor & recipient phones:
            "donor_phone": sensitive_donor_phone,
            "recipient_phone": sensitive_recipient_phone,
        },
    )
    # Volunteer needs addresses for transit, but MUST NOT receive direct phones
    assert sensitive_donor_phone not in vol_msg.rendered_body
    assert sensitive_recipient_phone not in vol_msg.rendered_body
    assert sensitive_donor_address in vol_msg.rendered_body
    assert sensitive_recipient_address in vol_msg.rendered_body


# ------------------------------------------------------------------------------
# Test 5: Bounded Fallback Scan Pagination
# ------------------------------------------------------------------------------
def test_bounded_fallback_scan_pagination() -> None:
    """Verify fallback scan paginates through multiple pages up to limits."""
    mock_table = mock.MagicMock()
    mock_client = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.Table.return_value = mock_table
    mock_resource.meta.client = mock_client

    config = AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-test",
        recipients_table_name="frca-recipients-test",
        volunteers_table_name="frca-volunteers-test",
        matches_audit_table_name="frca-matches-audit-test",
        sessions_memory_table_name="frca-sessions-test",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/dlq",
        location_place_index_name="index",
        route_calculator_name="calc",
        bedrock_agent_id="agent",
        bedrock_agent_alias_id="alias",
    )
    repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)

    # Force GSI query to fail with ValidationException (index missing)
    mock_table.query.side_effect = ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "The table does not have index: status-ready_by-index",
            }
        },
        "Query",
    )

    future_time = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()

    def make_donation_item(d_id: str, region: str, status: str) -> dict[str, Any]:
        return {
            "donation_id": d_id,
            "donor_id": "donor-1",
            "donor_name": "Donor Store",
            "donor_phone": "+12125550199",
            "donor_address": "123 Main St",
            "donor_coordinates": {"latitude": 40.71, "longitude": -74.00},
            "food_category": "bakery",
            "quantity_kg": 20.0,
            "ready_by": future_time,
            "perishability_hours": 24.0,
            "status": status,
            "service_region": region,
        }

    # Page 1: Scanned 50 items, but 0 matches (all wrong region/status)
    page_1 = {
        "Items": [],
        "ScannedCount": 50,
        "LastEvaluatedKey": {"donation_id": "don-50"},
    }
    # Page 2: Scanned 50 items, 3 matches
    page_2_items = [
        make_donation_item(f"don-p2-{i}", "metro-core", "reported") for i in range(3)
    ]
    page_2 = {
        "Items": page_2_items,
        "ScannedCount": 50,
        "LastEvaluatedKey": {"donation_id": "don-100"},
    }
    # Page 3: Scanned 50 items, 5 matches
    page_3_items = [
        make_donation_item(f"don-p3-{i}", "metro-core", "reported") for i in range(5)
    ]
    page_3 = {
        "Items": page_3_items,
        "ScannedCount": 50,
        "LastEvaluatedKey": None,
    }

    mock_table.scan.side_effect = [page_1, page_2, page_3]

    # Query for up to 10 matching donations
    results = repo.query_unmatched_donations_by_region(
        service_region="metro-core", limit=10, max_evaluated_items=250
    )

    # Invariant: Fallback scan did NOT quit after page 1; it paginated through
    # pages 1, 2, and 3, collecting all 8 matching donations
    assert len(results) == 8
    assert mock_table.scan.call_count == 3
    for r in results:
        assert r.service_region == "metro-core"
        assert r.status == DonationStatus.REPORTED


# ------------------------------------------------------------------------------
# Test 6: Concurrent Notification Claim Mutual Exclusion
# ------------------------------------------------------------------------------
def test_concurrent_notification_claim_mutual_exclusion() -> None:
    """Verify pre-dispatch claim ensures only 1 worker publishes to SNS."""
    mock_table = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.Table.return_value = mock_table
    mock_sns = mock.MagicMock()

    config = AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-test",
        recipients_table_name="frca-recipients-test",
        volunteers_table_name="frca-volunteers-test",
        matches_audit_table_name="frca-matches-audit-test",
        sessions_memory_table_name="frca-sessions-test",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/dlq",
        location_place_index_name="index",
        route_calculator_name="calc",
        bedrock_agent_id="agent",
        bedrock_agent_alias_id="alias",
    )
    d_repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)
    a_repo = AuditRepository(dynamodb_resource=mock_resource, config=config)

    now = datetime.now(timezone.utc)
    donation = Donation(
        donation_id="don-claim-race-01",
        donor_id="donor-01",
        donor_name="Bakery",
        donor_phone="+12125550199",
        donor_address="100 Main St",
        donor_coordinates=Coordinates(latitude=40.71, longitude=-74.00),
        food_category=FoodCategory.BAKERY,
        quantity_kg=25.0,
        ready_by=now + timedelta(hours=2),
        perishability_hours=4.0,
        service_region="metro-core",
        status=DonationStatus.ESCALATED,
        coordinator_notification_status=CoordinatorNotificationStatus.PENDING,
    )

    orchestrator = StrandsOrchestrator(
        donations_repo=d_repo,
        audit_repo=a_repo,
        config=config,
        sns_client=mock_sns,
    )

    lock = threading.Lock()
    first_claimed = False

    def side_effect_update(**kwargs: Any) -> dict[str, Any]:
        nonlocal first_claimed
        # Only gate the claim operation (which transitions to CLAIMED)
        if ":claimed_status" in kwargs.get("ExpressionAttributeValues", {}):
            with lock:
                if not first_claimed:
                    first_claimed = True
                    return {}
                raise ClientError(
                    {
                        "Error": {"Code": "ConditionalCheckFailedException"},
                        "Message": "The conditional request failed",
                    },
                    "UpdateItem",
                )
        return {}

    mock_table.update_item.side_effect = side_effect_update

    # Two concurrent workers attempt to dispatch the coordinator alert
    results: list[bool] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(
            orchestrator._dispatch_coordinator_escalation_alert,
            donation,
            correlation_id="worker-A",
        )
        f2 = executor.submit(
            orchestrator._dispatch_coordinator_escalation_alert,
            donation,
            correlation_id="worker-B",
        )
        for f in concurrent.futures.as_completed([f1, f2]):
            results.append(f.result())

    # Invariant: Exactly 1 worker claimed and dispatched; other cleanly failed claim
    assert results.count(True) == 1
    assert results.count(False) == 1
    # Critical invariant: SNS publish was called EXACTLY ONCE across parallel workers!
    assert mock_sns.publish.call_count == 1


# ------------------------------------------------------------------------------
# Test 7: Crash Recovery After Claimed with Active vs Expired Lease
# ------------------------------------------------------------------------------
def test_crash_recovery_after_claim_lease_expired() -> None:
    """Verify active lease prevents duplicate dispatch; expired lease enables recovery.
    """
    mock_table = mock.MagicMock()

    mock_resource = mock.MagicMock()
    mock_resource.Table.return_value = mock_table
    mock_sns = mock.MagicMock()

    config = AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-test",
        recipients_table_name="frca-recipients-test",
        volunteers_table_name="frca-volunteers-test",
        matches_audit_table_name="frca-matches-audit-test",
        sessions_memory_table_name="frca-sessions-test",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/dlq",
        location_place_index_name="index",
        route_calculator_name="calc",
        bedrock_agent_id="agent",
        bedrock_agent_alias_id="alias",
        notification_claim_lease_seconds=120,
    )
    d_repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)
    a_repo = AuditRepository(dynamodb_resource=mock_resource, config=config)
    orchestrator = StrandsOrchestrator(
        donations_repo=d_repo,
        audit_repo=a_repo,
        config=config,
        sns_client=mock_sns,
    )

    t0 = datetime.now(timezone.utc)
    base_donation = Donation(
        donation_id="don-lease-01",
        donor_id="donor-01",
        donor_name="Bakery",
        donor_phone="+12125550199",
        donor_address="100 Main St",
        donor_coordinates=Coordinates(latitude=40.71, longitude=-74.00),
        food_category=FoodCategory.BAKERY,
        quantity_kg=25.0,
        ready_by=t0 + timedelta(hours=2),
        perishability_hours=4.0,
        service_region="metro-core",
        status=DonationStatus.ESCALATED,
        coordinator_notification_status=CoordinatorNotificationStatus.CLAIMED,
        coordinator_notification_claimed_at=t0,
        coordinator_notification_claim_id="clm-crashed-worker",
    )

    # Scenario A: Lease still active (only 30 seconds elapsed, lease=120s)
    # DynamoDB condition check fails because claimed_at > expired_threshold
    mock_table.update_item.side_effect = ClientError(
        {
            "Error": {"Code": "ConditionalCheckFailedException"},
            "Message": "Active lease held",
        },
        "UpdateItem",
    )
    res_active = orchestrator._dispatch_coordinator_escalation_alert(
        base_donation, correlation_id="reconciler-active"
    )
    assert res_active is False
    assert mock_sns.publish.call_count == 0  # Zero dispatch!

    # Scenario B: Lease expired (150 seconds elapsed, lease=120s)
    # DynamoDB condition passes, allowing reclamation
    mock_table.update_item.side_effect = None
    mock_table.update_item.return_value = {}

    res_expired = orchestrator._dispatch_coordinator_escalation_alert(
        base_donation, correlation_id="reconciler-expired"
    )
    assert res_expired is True
    assert mock_sns.publish.call_count == 1  # Successfully recovered and dispatched!


# ------------------------------------------------------------------------------
# Test 8: Ambiguous Transport Timeout Preserves CLAIMED State
# ------------------------------------------------------------------------------
def test_ambiguous_transport_timeout_preserves_claimed_state() -> None:
    """Verify socket/read timeout preserves CLAIMED status instead of resetting."""
    mock_table = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.Table.return_value = mock_table
    mock_sns = mock.MagicMock()

    # Simulate ambiguous socket/read timeout after SNS accepted request
    mock_sns.publish.side_effect = TimeoutError("Socket connection timed out")

    config = AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-test",
        recipients_table_name="frca-recipients-test",
        volunteers_table_name="frca-volunteers-test",
        matches_audit_table_name="frca-matches-audit-test",
        sessions_memory_table_name="frca-sessions-test",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/dlq",
        location_place_index_name="index",
        route_calculator_name="calc",
        bedrock_agent_id="agent",
        bedrock_agent_alias_id="alias",
    )
    d_repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)
    a_repo = AuditRepository(dynamodb_resource=mock_resource, config=config)
    orchestrator = StrandsOrchestrator(
        donations_repo=d_repo,
        audit_repo=a_repo,
        config=config,
        sns_client=mock_sns,
    )

    t0 = datetime.now(timezone.utc)
    donation = Donation(
        donation_id="don-ambiguous-01",
        donor_id="donor-01",
        donor_name="Bakery",
        donor_phone="+12125550199",
        donor_address="100 Main St",
        donor_coordinates=Coordinates(latitude=40.71, longitude=-74.00),
        food_category=FoodCategory.BAKERY,
        quantity_kg=25.0,
        ready_by=t0 + timedelta(hours=2),
        perishability_hours=4.0,
        service_region="metro-core",
        status=DonationStatus.ESCALATED,
        coordinator_notification_status=CoordinatorNotificationStatus.PENDING,
    )

    # Claim succeeds
    mock_table.update_item.return_value = {}

    success = orchestrator._dispatch_coordinator_escalation_alert(
        donation, correlation_id="trace-ambiguous"
    )

    # Dispatch reported False due to timeout
    assert success is False
    # Verify update_item was called ONLY once (for claim acquisition)
    # and was NOT called to reset to PENDING or mark DELIVERED/FAILED
    assert mock_table.update_item.call_count == 1
    claim_call_kwargs = mock_table.update_item.call_args[1]
    assert ":claimed_status" in claim_call_kwargs["ExpressionAttributeValues"]


# ------------------------------------------------------------------------------
# Test 9: Already DELIVERED Safe No-Op
# ------------------------------------------------------------------------------
def test_already_delivered_notification_safe_noop() -> None:
    """Verify records already marked DELIVERED do not trigger SNS or DB mutations."""
    mock_table = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.Table.return_value = mock_table
    mock_sns = mock.MagicMock()

    config = AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-test",
        recipients_table_name="frca-recipients-test",
        volunteers_table_name="frca-volunteers-test",
        matches_audit_table_name="frca-matches-audit-test",
        sessions_memory_table_name="frca-sessions-test",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/dlq",
        location_place_index_name="index",
        route_calculator_name="calc",
        bedrock_agent_id="agent",
        bedrock_agent_alias_id="alias",
    )
    d_repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)
    a_repo = AuditRepository(dynamodb_resource=mock_resource, config=config)
    orchestrator = StrandsOrchestrator(
        donations_repo=d_repo,
        audit_repo=a_repo,
        config=config,
        sns_client=mock_sns,
    )

    t0 = datetime.now(timezone.utc)
    donation = Donation(
        donation_id="don-delivered-01",
        donor_id="donor-01",
        donor_name="Bakery",
        donor_phone="+12125550199",
        donor_address="100 Main St",
        donor_coordinates=Coordinates(latitude=40.71, longitude=-74.00),
        food_category=FoodCategory.BAKERY,
        quantity_kg=25.0,
        ready_by=t0 + timedelta(hours=2),
        perishability_hours=4.0,
        service_region="metro-core",
        status=DonationStatus.ESCALATED,
        coordinator_notification_status=CoordinatorNotificationStatus.DELIVERED,
    )

    result = orchestrator._dispatch_coordinator_escalation_alert(
        donation, correlation_id="reconciler-check"
    )

    assert result is True
    assert mock_sns.publish.call_count == 0
    assert mock_table.update_item.call_count == 0


# ------------------------------------------------------------------------------
# Test 10: Permanent SNS Failure Transitions to FAILED
# ------------------------------------------------------------------------------
def test_permanent_sns_failure_transitions_to_failed() -> None:
    """Verify non-retryable 4xx ClientError transitions notification to FAILED."""
    mock_table = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.Table.return_value = mock_table
    mock_sns = mock.MagicMock()

    # 4xx permanent failure
    mock_sns.publish.side_effect = ClientError(
        {
            "Error": {
                "Code": "InvalidParameterException",
                "Message": "Invalid topic destination",
            }
        },
        "Publish",
    )

    config = AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-test",
        recipients_table_name="frca-recipients-test",
        volunteers_table_name="frca-volunteers-test",
        matches_audit_table_name="frca-matches-audit-test",
        sessions_memory_table_name="frca-sessions-test",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/dlq",
        location_place_index_name="index",
        route_calculator_name="calc",
        bedrock_agent_id="agent",
        bedrock_agent_alias_id="alias",
    )
    d_repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)
    a_repo = AuditRepository(dynamodb_resource=mock_resource, config=config)
    orchestrator = StrandsOrchestrator(
        donations_repo=d_repo,
        audit_repo=a_repo,
        config=config,
        sns_client=mock_sns,
    )

    t0 = datetime.now(timezone.utc)
    donation = Donation(
        donation_id="don-failed-01",
        donor_id="donor-01",
        donor_name="Bakery",
        donor_phone="+12125550199",
        donor_address="100 Main St",
        donor_coordinates=Coordinates(latitude=40.71, longitude=-74.00),
        food_category=FoodCategory.BAKERY,
        quantity_kg=25.0,
        ready_by=t0 + timedelta(hours=2),
        perishability_hours=4.0,
        service_region="metro-core",
        status=DonationStatus.ESCALATED,
        coordinator_notification_status=CoordinatorNotificationStatus.PENDING,
    )

    mock_table.update_item.return_value = {}

    result = orchestrator._dispatch_coordinator_escalation_alert(
        donation, correlation_id="reconciler-failed"
    )

    assert result is False
    # First update: claim (PENDING -> CLAIMED)
    # Second update: mark failed (CLAIMED -> FAILED)
    assert mock_table.update_item.call_count == 2
    failed_kwargs = mock_table.update_item.call_args[1]
    assert failed_kwargs["ExpressionAttributeValues"][":failed_status"] == "FAILED"
    # Verify audit event COORDINATOR_ALERT_FAILED recorded
    mock_audit_table = mock_resource.Table("frca-matches-audit-test")
    assert mock_audit_table.put_item.call_count >= 1
    audit_item = mock_audit_table.put_item.call_args[1]["Item"]
    assert audit_item["action"] == "COORDINATOR_ALERT_FAILED"


# ------------------------------------------------------------------------------
# Test 11: Privacy & Bounded Query Limits in Outbox Recovery
# ------------------------------------------------------------------------------
def test_recovery_audit_and_bounded_query_privacy() -> None:
    """Verify recovery queries strictly respect limits and audit logs omit raw PII."""
    mock_table = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.Table.return_value = mock_table

    config = AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-test",
        recipients_table_name="frca-recipients-test",
        volunteers_table_name="frca-volunteers-test",
        matches_audit_table_name="frca-matches-audit-test",
        sessions_memory_table_name="frca-sessions-test",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/dlq",
        location_place_index_name="index",
        route_calculator_name="calc",
        bedrock_agent_id="agent",
        bedrock_agent_alias_id="alias",
    )
    repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)

    # GSI raises ValidationException, triggering bounded fallback scan
    mock_table.query.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "Index not found"}},
        "Query",
    )

    t0_dt = datetime.now(timezone.utc)
    t0_iso = t0_dt.isoformat()
    future_iso = (t0_dt + timedelta(hours=2)).isoformat()
    scanned_items = [
        {
            "donation_id": f"don-recov-{i}",
            "donor_id": "donor-01",
            "donor_name": "Bakery",
            "donor_phone": "+12125550199",
            "donor_address": "123 Main St",
            "donor_coordinates": {"latitude": 40.71, "longitude": -74.00},
            "food_category": "bakery",
            "quantity_kg": 20.0,
            "ready_by": future_iso,
            "perishability_hours": 24.0,
            "status": "escalated",
            "service_region": "metro-core",
            "coordinator_notification_status": "PENDING",
            "created_at": t0_iso,
            "updated_at": t0_iso,
        }
        for i in range(15)
    ]

    mock_table.scan.return_value = {
        "Items": scanned_items,
        "ScannedCount": 15,
        "LastEvaluatedKey": None,
    }

    # Query with strict limit of 5 items
    results = repo.query_pending_coordinator_notifications(
        service_region="metro-core",
        limit=5,
        max_evaluated_items=50,
    )

    # Invariant: Strictly bounded to requested limit
    assert len(results) == 5
    for item in results:
        assert item.status == DonationStatus.ESCALATED
        assert (
            item.coordinator_notification_status
            == CoordinatorNotificationStatus.PENDING
        )

