"""Tests for StrandsOrchestrator crash-recovery and resume-matrix logic."""

from datetime import datetime, timedelta, timezone
from unittest import mock

from agent.orchestrator import StrandsOrchestrator
from audit_repo import AuditRepository
from donations_repo import (
    DonationClaimConflictError,
    DonationsRepository,
)
from models import (
    Coordinates,
    Donation,
    DonationStatus,
    EscalationReason,
    EscalationTicket,
    FoodCategory,
    MatchCandidate,
    MatchResult,
    PipelineStep,
    VolunteerAssignment,
)
from recipients_repo import InsufficientCapacityError, RecipientsRepository
from volunteers_repo import VolunteersRepository


def make_test_donation(
    donation_id: str = "don-rec-01",
    status: DonationStatus = DonationStatus.REPORTED,
    matched_recipient_id: str | None = None,
    assigned_volunteer_id: str | None = None,
    perishability_hours: float = 8.0,
    quantity_kg: float = 25.0,
) -> Donation:
    """Create a valid Donation domain model for testing."""
    now = datetime.now(timezone.utc)
    return Donation(
        donation_id=donation_id,
        donor_id="donor-123",
        donor_name="Test Donor",
        donor_phone="+15555551234",
        donor_address="123 Main St",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=quantity_kg,
        ready_by=now + timedelta(hours=1),
        perishability_hours=perishability_hours,
        status=status,
        matched_recipient_id=matched_recipient_id,
        assigned_volunteer_id=assigned_volunteer_id,
    )


def test_terminal_status_escalated_and_completed_no_op() -> None:
    """Verify donations in terminal state return clean no-op without steps."""
    mock_donations = mock.create_autospec(DonationsRepository, instance=True)
    orchestrator = StrandsOrchestrator(donations_repo=mock_donations)

    # 1. ESCALATED state
    mock_donations.get_donation.return_value = make_test_donation(
        donation_id="don-term-01",
        status=DonationStatus.ESCALATED,
    )
    result = orchestrator.coordinate_donation("don-term-01")
    assert result.status == DonationStatus.ESCALATED
    assert result.steps_completed == []

    # 2. CLOSED state
    mock_donations.get_donation.return_value = make_test_donation(
        donation_id="don-term-02",
        status=DonationStatus.CLOSED,
    )
    result = orchestrator.coordinate_donation("don-term-02")
    assert result.status == DonationStatus.CLOSED
    assert result.steps_completed == []


def test_replay_recovery_for_assigned_status() -> None:
    """Verify ASSIGNED status triggers replay recovery without re-dispatching."""
    mock_donations = mock.create_autospec(DonationsRepository, instance=True)
    mock_volunteers = mock.create_autospec(VolunteersRepository, instance=True)
    mock_audit = mock.create_autospec(AuditRepository, instance=True)
    orchestrator = StrandsOrchestrator(
        donations_repo=mock_donations,
        volunteers_repo=mock_volunteers,
        audit_repo=mock_audit,
    )

    donation = make_test_donation(
        donation_id="don-assigned-01",
        status=DonationStatus.ASSIGNED,
        matched_recipient_id="rec-01",
        assigned_volunteer_id="vol-01",
    )
    mock_donations.get_donation.return_value = donation

    with mock.patch("agent.orchestrator.assign_volunteer") as mock_assign:
        mock_assign.return_value = VolunteerAssignment(
            assignment_id="asg-replay-01",
            donation_id="don-assigned-01",
            volunteer_id="vol-01",
            recipient_id="rec-01",
        )
        result = orchestrator.coordinate_donation("don-assigned-01")

        assert result.status == DonationStatus.ASSIGNED
        assert result.assigned_volunteer_id == "vol-01"
        assert result.steps_completed == [PipelineStep.ASSIGN_VOLUNTEER]
        mock_assign.assert_called_once()


def test_resume_from_matched_status_skips_match_and_deduct() -> None:
    """Verify MATCHED status skips classify/capacity/match and resumes at Step 8."""
    mock_donations = mock.create_autospec(DonationsRepository, instance=True)
    mock_recipients = mock.create_autospec(RecipientsRepository, instance=True)
    mock_volunteers = mock.create_autospec(VolunteersRepository, instance=True)
    mock_audit = mock.create_autospec(AuditRepository, instance=True)
    mock_audit.record_audit_event.return_value = True

    orchestrator = StrandsOrchestrator(
        donations_repo=mock_donations,
        recipients_repo=mock_recipients,
        volunteers_repo=mock_volunteers,
        audit_repo=mock_audit,
    )

    donation = make_test_donation(
        donation_id="don-matched-01",
        status=DonationStatus.MATCHED,
        matched_recipient_id="rec-saved-01",
    )
    mock_donations.get_donation.return_value = donation

    with mock.patch("agent.orchestrator.assign_volunteer") as mock_assign, \
         mock.patch("agent.orchestrator.classify_donation") as mock_classify, \
         mock.patch("agent.orchestrator.find_best_match") as mock_match, \
         mock.patch("agent.orchestrator.send_notification") as mock_notify:

        mock_assign.return_value = VolunteerAssignment(
            assignment_id="asg-resumed-01",
            donation_id="don-matched-01",
            volunteer_id="vol-saved-01",
            recipient_id="rec-saved-01",
        )

        result = orchestrator.coordinate_donation("don-matched-01")

        # Must skip classify, capacity query, matching, and claim deduction
        mock_classify.assert_not_called()
        mock_match.assert_not_called()
        mock_donations.claim_and_deduct_recipient.assert_not_called()

        # Resumed at Step 8 (assign_volunteer) and Step 9 (send_notification)
        mock_assign.assert_called_once()
        mock_notify.assert_called()

        assert result.status == DonationStatus.ASSIGNED
        assert result.matched_recipient_id == "rec-saved-01"
        assert result.assigned_volunteer_id == "vol-saved-01"
        assert PipelineStep.ASSIGN_VOLUNTEER in result.steps_completed
        assert PipelineStep.DISPATCH_NOTIFICATIONS in result.steps_completed
        assert PipelineStep.MATCH not in result.steps_completed


def test_candidate_fallback_loop_on_insufficient_capacity() -> None:
    """Verify fallback loop moves to next candidate when first lacks capacity."""
    mock_donations = mock.create_autospec(DonationsRepository, instance=True)
    mock_recipients = mock.create_autospec(RecipientsRepository, instance=True)
    mock_volunteers = mock.create_autospec(VolunteersRepository, instance=True)
    mock_audit = mock.create_autospec(AuditRepository, instance=True)
    mock_audit.record_audit_event.return_value = True

    orchestrator = StrandsOrchestrator(
        donations_repo=mock_donations,
        recipients_repo=mock_recipients,
        volunteers_repo=mock_volunteers,
        audit_repo=mock_audit,
    )

    donation = make_test_donation(
        donation_id="don-fb-01",
        status=DonationStatus.REPORTED,
        quantity_kg=30.0,
    )
    mock_donations.get_donation.return_value = donation

    cand1 = MatchCandidate(
        recipient_id="rec-full-01",
        recipient_name="Full Shelter",
        score=0.95,
        distance_km=1.2,
        capacity_match_kg=30.0,
        dietary_fit=True,
        reason="Closest match",
    )
    cand2 = MatchCandidate(
        recipient_id="rec-avail-02",
        recipient_name="Available Pantry",
        score=0.85,
        distance_km=2.0,
        capacity_match_kg=30.0,
        dietary_fit=True,
        reason="Second best match",
    )

    # First candidate throws InsufficientCapacityError, second succeeds
    mock_donations.claim_and_deduct_recipient.side_effect = [
        InsufficientCapacityError("Recipient rec-full-01 lacks capacity"),
        True,
    ]

    with mock.patch("agent.orchestrator.get_recipient_capacity") as mock_get_cap, \
         mock.patch("agent.orchestrator.find_best_match") as mock_match, \
         mock.patch("agent.orchestrator.assign_volunteer") as mock_assign, \
         mock.patch("agent.orchestrator.send_notification"):

        mock_get_cap.return_value = []
        mock_match.return_value = MatchResult(
            donation_id="don-fb-01",
            ranked_candidates=[cand1, cand2],
            best_match=cand1,
            rejection_reason=None,
        )
        mock_assign.return_value = VolunteerAssignment(
            assignment_id="asg-fb-01",
            donation_id="don-fb-01",
            volunteer_id="vol-01",
            recipient_id="rec-avail-02",
        )

        result = orchestrator.coordinate_donation("don-fb-01")

        assert mock_donations.claim_and_deduct_recipient.call_count == 2
        assert mock_donations.claim_and_deduct_recipient.call_args_list[0][1] == {
            "donation_id": "don-fb-01",
            "recipient_id": "rec-full-01",
            "quantity_kg": 30.0,
        }
        assert mock_donations.claim_and_deduct_recipient.call_args_list[1][1] == {
            "donation_id": "don-fb-01",
            "recipient_id": "rec-avail-02",
            "quantity_kg": 30.0,
        }

        assert result.status == DonationStatus.ASSIGNED
        assert result.matched_recipient_id == "rec-avail-02"


def test_candidate_claim_race_conflict_escalates_immediately() -> None:
    """Verify DonationClaimConflictError triggers immediate escalation without loop."""
    mock_donations = mock.create_autospec(DonationsRepository, instance=True)
    mock_recipients = mock.create_autospec(RecipientsRepository, instance=True)
    orchestrator = StrandsOrchestrator(
        donations_repo=mock_donations,
        recipients_repo=mock_recipients,
    )

    donation = make_test_donation(
        donation_id="don-conflict-01",
        status=DonationStatus.REPORTED,
    )
    mock_donations.get_donation.return_value = donation

    cand1 = MatchCandidate(
        recipient_id="rec-01",
        recipient_name="Shelter A",
        score=0.95,
        distance_km=1.2,
        capacity_match_kg=25.0,
        dietary_fit=True,
        reason="Best match",
    )
    cand2 = MatchCandidate(
        recipient_id="rec-02",
        recipient_name="Shelter B",
        score=0.85,
        distance_km=2.0,
        capacity_match_kg=25.0,
        dietary_fit=True,
        reason="Second best match",
    )

    mock_donations.claim_and_deduct_recipient.side_effect = (
        DonationClaimConflictError("Donation don-conflict-01 already claimed")
    )

    with mock.patch("agent.orchestrator.get_recipient_capacity") as mock_get_cap, \
         mock.patch("agent.orchestrator.find_best_match") as mock_match, \
         mock.patch("agent.orchestrator.flag_for_human") as mock_flag:

        mock_get_cap.return_value = []
        mock_match.return_value = MatchResult(
            donation_id="don-conflict-01",
            ranked_candidates=[cand1, cand2],
            best_match=cand1,
            rejection_reason=None,
        )
        mock_flag.return_value = EscalationTicket(
            ticket_id="tkt-conflict-01",
            donation_id="don-conflict-01",
            reason=EscalationReason.RECIPIENT_CLAIM_CONFLICT,
            details={"summary": "Donation don-conflict-01 already claimed"},
        )

        result = orchestrator.coordinate_donation("don-conflict-01")

        # Must not attempt candidate 2
        assert mock_donations.claim_and_deduct_recipient.call_count == 1
        mock_flag.assert_called_once()
        assert result.status == DonationStatus.ESCALATED
        assert result.escalation_ticket is not None
        assert (
            result.escalation_ticket.reason
            == EscalationReason.RECIPIENT_CLAIM_CONFLICT
        )


def test_volunteer_exhaustion_triggers_atomic_unwind_and_escalation() -> None:
    """Verify exhausted volunteers triggers atomic unwind of claimed recipient."""
    mock_donations = mock.create_autospec(DonationsRepository, instance=True)
    mock_recipients = mock.create_autospec(RecipientsRepository, instance=True)
    mock_volunteers = mock.create_autospec(VolunteersRepository, instance=True)
    mock_audit = mock.create_autospec(AuditRepository, instance=True)
    mock_audit.record_audit_event.return_value = True

    orchestrator = StrandsOrchestrator(
        donations_repo=mock_donations,
        recipients_repo=mock_recipients,
        volunteers_repo=mock_volunteers,
        audit_repo=mock_audit,
    )

    donation = make_test_donation(
        donation_id="don-unwind-01",
        status=DonationStatus.REPORTED,
        quantity_kg=40.0,
    )
    mock_donations.get_donation.return_value = donation

    cand = MatchCandidate(
        recipient_id="rec-exhaust-01",
        recipient_name="Exhaust Shelter",
        score=0.95,
        distance_km=1.2,
        capacity_match_kg=40.0,
        dietary_fit=True,
        reason="Eligible candidate",
    )

    mock_donations.claim_and_deduct_recipient.return_value = True
    mock_donations.unclaim_and_restore_recipient.return_value = True

    with mock.patch("agent.orchestrator.get_recipient_capacity") as mock_get_cap, \
         mock.patch("agent.orchestrator.find_best_match") as mock_match, \
         mock.patch("agent.orchestrator.assign_volunteer") as mock_assign, \
         mock.patch("agent.orchestrator.flag_for_human") as mock_flag:

        mock_get_cap.return_value = []
        mock_match.return_value = MatchResult(
            donation_id="don-unwind-01",
            ranked_candidates=[cand],
            best_match=cand,
            rejection_reason=None,
        )
        # All volunteers unavailable
        mock_assign.return_value = None

        mock_flag.return_value = EscalationTicket(
            ticket_id="tkt-exhaust-01",
            donation_id="don-unwind-01",
            reason=EscalationReason.NO_MATCH_WITHIN_WINDOW,
            details={"summary": "No available volunteers in service region"},
        )

        result = orchestrator.coordinate_donation("don-unwind-01")

        # Recipient was claimed first
        mock_donations.claim_and_deduct_recipient.assert_called_once_with(
            donation_id="don-unwind-01",
            recipient_id="rec-exhaust-01",
            quantity_kg=40.0,
        )

        # Due to volunteer exhaustion, atomic unwind was executed
        mock_donations.unclaim_and_restore_recipient.assert_called_once_with(
            donation_id="don-unwind-01",
            recipient_id="rec-exhaust-01",
            quantity_kg=40.0,
        )

        # Flagged for human review
        mock_flag.assert_called_once()
        assert result.status == DonationStatus.ESCALATED
        assert result.matched_recipient_id is None
