"""Tests verifying dry-run simulation mode with zero side-effects."""

from datetime import datetime, timedelta, timezone
from unittest import mock

from agent.orchestrator import StrandsOrchestrator
from audit_repo import AuditRepository
from donations_repo import DonationsRepository
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
)
from recipients_repo import RecipientsRepository
from volunteers_repo import VolunteersRepository


def make_test_donation(
    donation_id: str = "don-sim-01",
    perishability_hours: float = 8.0,
) -> Donation:
    """Create a valid Donation domain model for simulation testing."""
    now = datetime.now(timezone.utc)
    return Donation(
        donation_id=donation_id,
        donor_id="donor-sim-01",
        donor_name="Simulation Donor",
        donor_phone="+15555556789",
        donor_address="789 Simulation Blvd",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=15.0,
        ready_by=now + timedelta(hours=1),
        perishability_hours=perishability_hours,
        status=DonationStatus.REPORTED,
    )


def test_dry_run_executes_all_pipeline_steps_without_mutations() -> None:
    """Verify dry_run=True simulates all pipeline steps with zero persistence."""
    mock_donations = mock.create_autospec(DonationsRepository, instance=True)
    mock_recipients = mock.create_autospec(RecipientsRepository, instance=True)
    mock_volunteers = mock.create_autospec(VolunteersRepository, instance=True)
    mock_audit = mock.create_autospec(AuditRepository, instance=True)

    orchestrator = StrandsOrchestrator(
        donations_repo=mock_donations,
        recipients_repo=mock_recipients,
        volunteers_repo=mock_volunteers,
        audit_repo=mock_audit,
    )

    donation = make_test_donation("don-sim-01")
    mock_donations.get_donation.return_value = donation

    cand = MatchCandidate(
        recipient_id="rec-sim-01",
        recipient_name="Simulation Shelter",
        score=0.94,
        distance_km=1.1,
        capacity_match_kg=15.0,
        dietary_fit=True,
        reason="Best match for simulation",
    )

    with mock.patch("agent.orchestrator.get_recipient_capacity") as mock_cap, \
         mock.patch("agent.orchestrator.find_best_match") as mock_match, \
         mock.patch("agent.orchestrator.assign_volunteer") as mock_assign, \
         mock.patch("agent.orchestrator.send_notification") as mock_notify:

        mock_cap.return_value = []
        mock_match.return_value = MatchResult(
            donation_id="don-sim-01",
            ranked_candidates=[cand],
            best_match=cand,
            rejection_reason=None,
        )

        result = orchestrator.coordinate_donation("don-sim-01", dry_run=True)

        # Simulation output guarantees
        assert result.is_dry_run is True
        assert result.status == DonationStatus.ASSIGNED
        assert result.matched_recipient_id == "rec-sim-01"
        assert result.assigned_volunteer_id is not None
        assert result.escalation_ticket is None

        # All 8 forward pipeline steps executed
        assert PipelineStep.INTAKE in result.steps_completed
        assert PipelineStep.CLASSIFY in result.steps_completed
        assert PipelineStep.FETCH_CAPACITY in result.steps_completed
        assert PipelineStep.MATCH in result.steps_completed
        assert PipelineStep.CLAIM_RECIPIENT in result.steps_completed
        assert PipelineStep.ASSIGN_VOLUNTEER in result.steps_completed
        assert PipelineStep.DISPATCH_NOTIFICATIONS in result.steps_completed
        assert PipelineStep.COMPLETE in result.steps_completed

        # Zero side-effects: no mutations or external dispatches
        mock_donations.claim_and_deduct_recipient.assert_not_called()
        mock_donations.unclaim_and_restore_recipient.assert_not_called()
        mock_audit.record_audit_event.assert_not_called()
        mock_assign.assert_not_called()
        mock_notify.assert_not_called()


def test_dry_run_with_food_safety_breach_escalates_without_mutations() -> None:
    """Verify dry_run simulation respects food safety escalation boundaries."""
    mock_donations = mock.create_autospec(DonationsRepository, instance=True)
    mock_audit = mock.create_autospec(AuditRepository, instance=True)

    orchestrator = StrandsOrchestrator(
        donations_repo=mock_donations,
        audit_repo=mock_audit,
    )

    now = datetime.now(timezone.utc)
    donation = Donation(
        donation_id="don-sim-safety-01",
        donor_id="donor-sim-02",
        donor_name="Short Life Donor",
        donor_phone="+15555556789",
        donor_address="789 Simulation Blvd",
        donor_coordinates=Coordinates(latitude=37.7749, longitude=-122.4194),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=10.0,
        ready_by=now + timedelta(minutes=5),
        perishability_hours=0.5,
        status=DonationStatus.REPORTED,
    )
    mock_donations.get_donation.return_value = donation

    with mock.patch("agent.orchestrator.flag_for_human") as mock_flag:
        mock_flag.return_value = EscalationTicket(
            ticket_id="tkt-sim-01",
            donation_id="don-sim-safety-01",
            reason=EscalationReason.FOOD_SAFETY_THRESHOLD_BREACH,
            details={},
        )
        result = orchestrator.coordinate_donation(
            "don-sim-safety-01", dry_run=True
        )

        assert result.is_dry_run is True
        assert result.status == DonationStatus.ESCALATED
        mock_flag.assert_called_once()
        call_kwargs = mock_flag.call_args[1]
        assert (
            call_kwargs["reason"]
            == EscalationReason.FOOD_SAFETY_THRESHOLD_BREACH
        )
        mock_donations.claim_and_deduct_recipient.assert_not_called()
        mock_audit.record_audit_event.assert_not_called()
