"""Unit tests for flag_for_human escalation tool boundary enforcement."""

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from audit_repo import AuditRepository
from donations_repo import DonationsRepository
from models import (
    AuditEvent,
    Coordinates,
    Donation,
    DonationStateConflictError,
    DonationStatus,
    EscalationReason,
    FoodCategory,
)
from tools.flag_for_human import flag_for_human


def test_flag_for_human_all_four_valid_reasons() -> None:
    """Verify every one of the 4 defined escalation reasons succeeds."""
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)
    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_a_repo.query_audit_trail_by_donation.return_value = []
    mock_d_repo.get_donation.return_value = None
    mock_sns = mock.MagicMock()

    reasons = [
        EscalationReason.NO_MATCH_WITHIN_WINDOW,
        EscalationReason.RECIPIENT_CLAIM_CONFLICT,
        EscalationReason.FOOD_SAFETY_THRESHOLD_BREACH,
        EscalationReason.INPUT_VALIDATION_FAILURE,
    ]

    for reason in reasons:
        ticket = flag_for_human(
            donation_id="don-esc-01",
            reason=reason,
            summary=f"Testing escalation for {reason.value}",
            details={"metric": 42},
            correlation_id="corr-esc-test",
            audit_repo=mock_a_repo,
            donations_repo=mock_d_repo,
            sns_client=mock_sns,
        )

        assert ticket.donation_id == "don-esc-01"
        assert ticket.reason == reason
        assert ticket.ticket_id.startswith("tkt-")

    # Assert 8 audit records (4 HUMAN_ESCALATION + 4 NOTIFICATION_DISPATCHED)
    # and 4 notifications
    assert mock_a_repo.record_audit_event.call_count == 8
    assert mock_sns.publish.call_count == 4


def test_flag_for_human_rejects_generic_reasons() -> None:
    """Verify non-enum strings and arbitrary reasons are strictly rejected."""
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)

    with pytest.raises(ValueError, match="Invalid escalation reason"):
        # Passing an arbitrary string instead of EscalationReason enum
        flag_for_human(
            donation_id="don-esc-02",
            reason="generic_unhandled_exception",  # type: ignore[arg-type]
            summary="Arbitrary failure message",
            audit_repo=mock_a_repo,
        )


def test_flag_for_human_escalates_existing_donation_in_repo() -> None:
    """Verify d_repo.escalate_donation is called when donation exists in repo."""
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)
    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_a_repo.query_audit_trail_by_donation.return_value = []

    future_time = datetime.now(timezone.utc) + timedelta(hours=3)
    mock_d_repo.get_donation.return_value = Donation(
        donation_id="don-esc-03",
        donor_id="donor-01",
        donor_name="Central Bakery",
        donor_phone="+14155550199",
        donor_address="101 Market St",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.PRODUCE,
        quantity_kg=25.0,
        ready_by=future_time,
        perishability_hours=6.0,
        service_region="metro-core",
        status=DonationStatus.REPORTED,
    )

    ticket = flag_for_human(
        donation_id="don-esc-03",
        reason=EscalationReason.NO_MATCH_WITHIN_WINDOW,
        summary="No recipient available",
        audit_repo=mock_a_repo,
        donations_repo=mock_d_repo,
    )

    assert ticket.donation_id == "don-esc-03"
    mock_d_repo.escalate_donation.assert_called_once_with(
        "don-esc-03", EscalationReason.NO_MATCH_WITHIN_WINDOW
    )


def test_flag_for_human_rejects_post_dispatch_escalation() -> None:
    """Verify escalation fails if donation is in post-dispatch state."""
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)
    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_d_repo.get_donation.return_value = mock.MagicMock()
    mock_d_repo.escalate_donation.side_effect = DonationStateConflictError(
        "Cannot escalate donation don-esc-04 in post-dispatch state assigned"
    )

    with pytest.raises(DonationStateConflictError, match="post-dispatch state"):
        flag_for_human(
            donation_id="don-esc-04",
            reason=EscalationReason.NO_MATCH_WITHIN_WINDOW,
            summary="Late escalation attempt",
            audit_repo=mock_a_repo,
            donations_repo=mock_d_repo,
        )


def test_flag_for_human_suppresses_duplicate_coordinator_alert() -> None:
    """Verify SNS alert is not published if already in audit trail."""
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)
    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_d_repo.get_donation.return_value = None
    mock_sns = mock.MagicMock()

    # Pre-existing coordinator notification in audit trail
    mock_a_repo.query_audit_trail_by_donation.return_value = [
        AuditEvent(
            event_id="evt-prior",
            donation_id="don-esc-05",
            action="NOTIFICATION_DISPATCHED",
            actor="strands_orchestrator",
            idempotency_key="don-esc-05:notify_coordinator_escalation",
            details={},
        )
    ]

    ticket = flag_for_human(
        donation_id="don-esc-05",
        reason=EscalationReason.FOOD_SAFETY_THRESHOLD_BREACH,
        summary="Temperature exceeded",
        audit_repo=mock_a_repo,
        donations_repo=mock_d_repo,
        sns_client=mock_sns,
    )

    assert ticket.donation_id == "don-esc-05"
    mock_sns.publish.assert_not_called()
    # Only HUMAN_ESCALATION was recorded, not duplicate NOTIFICATION_DISPATCHED
    assert mock_a_repo.record_audit_event.call_count == 1
