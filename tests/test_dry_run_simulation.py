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
    Recipient,
)
from recipients_repo import RecipientsRepository
from volunteers_repo import VolunteersRepository


def make_test_donation(
    donation_id: str = "don-sim-01",
    perishability_hours: float = 8.0,
    quantity_kg: float = 15.0,
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
        quantity_kg=quantity_kg,
        ready_by=now + timedelta(hours=1),
        perishability_hours=perishability_hours,
        service_region="metro-core",
        status=DonationStatus.REPORTED,
    )


def make_test_recipient(
    recipient_id: str = "rec-sim-01",
    capacity_kg_remaining: float = 50.0,
) -> Recipient:
    """Create a valid Recipient domain model for simulation testing."""
    return Recipient(
        recipient_id=recipient_id,
        organization_name=f"Shelter {recipient_id}",
        contact_name="Coordinator",
        contact_phone="+15555554321",
        address="100 Shelter Lane",
        coordinates=Coordinates(latitude=37.77, longitude=-122.41),
        capacity_kg_remaining=capacity_kg_remaining,
        service_region="metro-core",
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
    mock_recipients.get_recipient.return_value = make_test_recipient(
        "rec-sim-01", 50.0
    )

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


def test_dry_run_simulates_candidate_capacity_fallback_loop() -> None:
    """Verify dry_run checks capacity read-only and falls back to second candidate."""
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

    donation = make_test_donation("don-sim-fb-01", quantity_kg=20.0)
    mock_donations.get_donation.return_value = donation

    cand1 = MatchCandidate(
        recipient_id="rec-low-01",
        recipient_name="Low Capacity Shelter",
        score=0.95,
        distance_km=1.0,
        capacity_match_kg=20.0,
        dietary_fit=True,
        reason="First choice",
    )
    cand2 = MatchCandidate(
        recipient_id="rec-ok-02",
        recipient_name="High Capacity Shelter",
        score=0.88,
        distance_km=2.0,
        capacity_match_kg=20.0,
        dietary_fit=True,
        reason="Second choice",
    )

    # Candidate 1 has only 5kg (< 20kg), Candidate 2 has 30kg (>= 20kg)
    rec1 = make_test_recipient("rec-low-01", 5.0)
    rec2 = make_test_recipient("rec-ok-02", 30.0)

    def mock_get_rec(rec_id: str, consistent_read: bool = False) -> Recipient | None:
        del consistent_read
        if rec_id == "rec-low-01":
            return rec1
        if rec_id == "rec-ok-02":
            return rec2
        return None

    mock_recipients.get_recipient.side_effect = mock_get_rec

    with mock.patch("agent.orchestrator.get_recipient_capacity") as mock_cap, \
         mock.patch("agent.orchestrator.find_best_match") as mock_match:

        mock_cap.return_value = []
        mock_match.return_value = MatchResult(
            donation_id="don-sim-fb-01",
            ranked_candidates=[cand1, cand2],
            best_match=cand1,
            rejection_reason=None,
        )

        result = orchestrator.coordinate_donation("don-sim-fb-01", dry_run=True)

        assert result.is_dry_run is True
        assert result.status == DonationStatus.ASSIGNED
        # Successfully fell back to Candidate 2 in read-only simulation
        assert result.matched_recipient_id == "rec-ok-02"
        mock_donations.claim_and_deduct_recipient.assert_not_called()


def test_dry_run_simulates_exhaustion_when_all_candidates_lack_capacity() -> None:
    """Verify dry_run escalates to human when all candidates fail capacity check."""
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

    donation = make_test_donation("don-sim-ex-01", quantity_kg=25.0)
    mock_donations.get_donation.return_value = donation

    cand1 = MatchCandidate(
        recipient_id="rec-low-01",
        recipient_name="Low Shelter 1",
        score=0.95,
        distance_km=1.0,
        capacity_match_kg=25.0,
        dietary_fit=True,
        reason="First choice",
    )

    # Candidate only has 10kg (< 25kg)
    mock_recipients.get_recipient.return_value = make_test_recipient(
        "rec-low-01", 10.0
    )

    with mock.patch("agent.orchestrator.get_recipient_capacity") as mock_cap, \
         mock.patch("agent.orchestrator.find_best_match") as mock_match, \
         mock.patch("agent.orchestrator.flag_for_human") as mock_flag:

        mock_cap.return_value = []
        mock_match.return_value = MatchResult(
            donation_id="don-sim-ex-01",
            ranked_candidates=[cand1],
            best_match=cand1,
            rejection_reason=None,
        )
        mock_flag.return_value = EscalationTicket(
            ticket_id="tkt-ex-01",
            donation_id="don-sim-ex-01",
            reason=EscalationReason.NO_MATCH_WITHIN_WINDOW,
            details={"summary": "All candidates lack capacity"},
        )

        result = orchestrator.coordinate_donation("don-sim-ex-01", dry_run=True)

        assert result.is_dry_run is True
        assert result.status == DonationStatus.ESCALATED
        mock_flag.assert_called_once()
        assert (
            mock_flag.call_args[1]["reason"]
            == EscalationReason.NO_MATCH_WITHIN_WINDOW
        )
        mock_donations.claim_and_deduct_recipient.assert_not_called()


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
        service_region="metro-core",
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
