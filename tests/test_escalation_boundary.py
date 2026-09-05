"""Tests verifying strict enforcement of all 4 human escalation boundaries."""

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
from recipients_repo import RecipientsRepository
from volunteers_repo import VolunteersRepository


def make_test_donation(
    donation_id: str = "don-esc-01",
    perishability_hours: float = 6.0,
    status: DonationStatus = DonationStatus.REPORTED,
) -> Donation:
    """Create a valid Donation domain model for boundary testing."""
    now = datetime.now(timezone.utc)
    return Donation(
        donation_id=donation_id,
        donor_id="donor-esc-01",
        donor_name="Boundary Donor",
        donor_phone="+15555557890",
        donor_address="456 Boundary Way",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=20.0,
        ready_by=now + timedelta(hours=1),
        perishability_hours=perishability_hours,
        status=status,
    )


def test_escalation_on_food_safety_threshold_breach() -> None:
    """Verify shelf life under 60m triggers FOOD_SAFETY_THRESHOLD_BREACH."""
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

    now = datetime.now(timezone.utc)
    # ready_by in 5 minutes + 30m perishability = 35 minutes total shelf life (<60m)
    donation = Donation(
        donation_id="don-safety-01",
        donor_id="donor-esc-01",
        donor_name="Boundary Donor",
        donor_phone="+15555557890",
        donor_address="456 Boundary Way",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=20.0,
        ready_by=now + timedelta(minutes=5),
        perishability_hours=0.5,
        status=DonationStatus.REPORTED,
    )
    mock_donations.get_donation.return_value = donation

    with mock.patch("agent.orchestrator.flag_for_human") as mock_flag:
        mock_flag.return_value = EscalationTicket(
            ticket_id="tkt-safety-01",
            donation_id="don-safety-01",
            reason=EscalationReason.FOOD_SAFETY_THRESHOLD_BREACH,
            details={"shelf_life_hours": 0.5},
        )

        result = orchestrator.coordinate_donation("don-safety-01")

        mock_flag.assert_called_once()
        assert result.status == DonationStatus.ESCALATED
        assert result.escalation_ticket is not None
        assert (
            result.escalation_ticket.reason
            == EscalationReason.FOOD_SAFETY_THRESHOLD_BREACH
        )
        assert PipelineStep.CLASSIFY in result.steps_completed
        assert PipelineStep.MATCH not in result.steps_completed


def test_escalation_on_no_match_within_window() -> None:
    """Verify lack of eligible recipients triggers NO_MATCH_WITHIN_WINDOW."""
    mock_donations = mock.create_autospec(DonationsRepository, instance=True)
    mock_recipients = mock.create_autospec(RecipientsRepository, instance=True)
    orchestrator = StrandsOrchestrator(
        donations_repo=mock_donations,
        recipients_repo=mock_recipients,
    )

    donation = make_test_donation(
        donation_id="don-nomatch-01",
        perishability_hours=8.0,
    )
    mock_donations.get_donation.return_value = donation

    with mock.patch("agent.orchestrator.get_recipient_capacity") as mock_cap, \
         mock.patch("agent.orchestrator.find_best_match") as mock_match, \
         mock.patch("agent.orchestrator.flag_for_human") as mock_flag:

        mock_cap.return_value = []
        mock_match.return_value = MatchResult(
            donation_id="don-nomatch-01",
            ranked_candidates=[],
            best_match=None,
            rejection_reason="No recipients meet dietary or capacity requirements",
        )
        mock_flag.return_value = EscalationTicket(
            ticket_id="tkt-nomatch-01",
            donation_id="don-nomatch-01",
            reason=EscalationReason.NO_MATCH_WITHIN_WINDOW,
            details={"rejection_reason": "No recipients meet requirements"},
        )

        result = orchestrator.coordinate_donation("don-nomatch-01")

        mock_flag.assert_called_once()
        assert result.status == DonationStatus.ESCALATED
        assert result.escalation_ticket is not None
        assert (
            result.escalation_ticket.reason
            == EscalationReason.NO_MATCH_WITHIN_WINDOW
        )


def test_escalation_on_recipient_claim_conflict() -> None:
    """Verify concurrent claim collision triggers RECIPIENT_CLAIM_CONFLICT."""
    mock_donations = mock.create_autospec(DonationsRepository, instance=True)
    mock_recipients = mock.create_autospec(RecipientsRepository, instance=True)
    orchestrator = StrandsOrchestrator(
        donations_repo=mock_donations,
        recipients_repo=mock_recipients,
    )

    donation = make_test_donation(
        donation_id="don-conflict-01",
        perishability_hours=8.0,
    )
    mock_donations.get_donation.return_value = donation

    cand = MatchCandidate(
        recipient_id="rec-01",
        recipient_name="Food Pantry 1",
        score=0.9,
        distance_km=2.0,
        capacity_match_kg=20.0,
        dietary_fit=True,
        reason="Good fit",
    )

    mock_donations.claim_and_deduct_recipient.side_effect = (
        DonationClaimConflictError("Donation don-conflict-01 already claimed")
    )

    with mock.patch("agent.orchestrator.get_recipient_capacity") as mock_cap, \
         mock.patch("agent.orchestrator.find_best_match") as mock_match, \
         mock.patch("agent.orchestrator.flag_for_human") as mock_flag:

        mock_cap.return_value = []
        mock_match.return_value = MatchResult(
            donation_id="don-conflict-01",
            ranked_candidates=[cand],
            best_match=cand,
            rejection_reason=None,
        )
        mock_flag.return_value = EscalationTicket(
            ticket_id="tkt-conflict-01",
            donation_id="don-conflict-01",
            reason=EscalationReason.RECIPIENT_CLAIM_CONFLICT,
            details={},
        )

        result = orchestrator.coordinate_donation("don-conflict-01")

        mock_flag.assert_called_once()
        assert result.status == DonationStatus.ESCALATED
        assert result.escalation_ticket is not None
        assert (
            result.escalation_ticket.reason
            == EscalationReason.RECIPIENT_CLAIM_CONFLICT
        )


def test_escalation_on_input_validation_failure() -> None:
    """Verify missing donation record triggers INPUT_VALIDATION_FAILURE."""
    mock_donations = mock.create_autospec(DonationsRepository, instance=True)
    orchestrator = StrandsOrchestrator(donations_repo=mock_donations)

    mock_donations.get_donation.return_value = None

    with mock.patch("agent.orchestrator.flag_for_human") as mock_flag:
        mock_flag.return_value = EscalationTicket(
            ticket_id="tkt-inv-01",
            donation_id="don-nonexistent",
            reason=EscalationReason.INPUT_VALIDATION_FAILURE,
            details={"error": "Donation don-nonexistent not found"},
        )

        result = orchestrator.coordinate_donation("don-nonexistent")

        mock_flag.assert_called_once()
        assert result.status == DonationStatus.ESCALATED
        assert result.escalation_ticket is not None
        assert (
            result.escalation_ticket.reason
            == EscalationReason.INPUT_VALIDATION_FAILURE
        )


def test_zero_false_positives_on_clean_match() -> None:
    """Verify successful end-to-end match NEVER invokes flag_for_human."""
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
        donation_id="don-clean-01",
        perishability_hours=10.0,
    )
    mock_donations.get_donation.return_value = donation

    cand = MatchCandidate(
        recipient_id="rec-clean-01",
        recipient_name="St. Mary's Dining Room",
        score=0.98,
        distance_km=1.0,
        capacity_match_kg=20.0,
        dietary_fit=True,
        reason="Optimal candidate",
    )

    mock_donations.claim_and_deduct_recipient.return_value = True

    with mock.patch("agent.orchestrator.get_recipient_capacity") as mock_cap, \
         mock.patch("agent.orchestrator.find_best_match") as mock_match, \
         mock.patch("agent.orchestrator.assign_volunteer") as mock_assign, \
         mock.patch("agent.orchestrator.send_notification"), \
         mock.patch("agent.orchestrator.flag_for_human") as mock_flag:

        mock_cap.return_value = []
        mock_match.return_value = MatchResult(
            donation_id="don-clean-01",
            ranked_candidates=[cand],
            best_match=cand,
            rejection_reason=None,
        )
        mock_assign.return_value = VolunteerAssignment(
            assignment_id="asg-clean-01",
            donation_id="don-clean-01",
            volunteer_id="vol-clean-01",
            recipient_id="rec-clean-01",
        )

        result = orchestrator.coordinate_donation("don-clean-01")

        # Zero false positives: flag_for_human must NEVER be called
        mock_flag.assert_not_called()
        assert result.status == DonationStatus.ASSIGNED
        assert result.matched_recipient_id == "rec-clean-01"
        assert result.assigned_volunteer_id == "vol-clean-01"
        assert result.escalation_ticket is None
