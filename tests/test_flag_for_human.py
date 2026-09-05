"""Unit tests for flag_for_human escalation tool boundary enforcement."""

from unittest import mock

import pytest

from audit_repo import AuditRepository
from donations_repo import DonationsRepository
from models import EscalationReason
from tools.flag_for_human import flag_for_human


def test_flag_for_human_all_four_valid_reasons() -> None:
    """Verify every one of the 4 defined escalation reasons succeeds."""
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)
    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
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

    # Assert 4 audit records and 4 notifications were sent
    assert mock_a_repo.record_audit_event.call_count == 4
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
