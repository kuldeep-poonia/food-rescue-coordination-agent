"""Test suite for assign_volunteer: idempotency, crash recovery, and races."""

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from audit_repo import AuditRepository
from donations_repo import DonationsRepository
from models import (
    AuditEvent,
    Coordinates,
    Donation,
    DonationStatus,
    FoodCategory,
    InfrastructureConsistencyError,
    Volunteer,
    VolunteerStatus,
)
from tools.assign_volunteer import assign_volunteer
from volunteers_repo import VolunteersRepository, VolunteerUnavailableError


def make_donation(
    donation_id: str = "don-assign-01",
    status: DonationStatus = DonationStatus.MATCHED,
    assigned_volunteer_id: str | None = None,
    quantity_kg: float = 30.0,
) -> Donation:
    """Helper constructing test donation."""
    future = datetime.now(timezone.utc) + timedelta(hours=4)
    return Donation(
        donation_id=donation_id,
        donor_id="donor-1",
        donor_name="Sunrise Cafe",
        donor_phone="+12125550199",
        donor_address="100 Broadway",
        donor_coordinates=Coordinates(latitude=40.7128, longitude=-74.0060),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=quantity_kg,
        ready_by=future,
        perishability_hours=6.0,
        service_region="metro-core",
        status=status,
        matched_recipient_id="rec-001",
        assigned_volunteer_id=assigned_volunteer_id,
    )


def make_volunteer(
    volunteer_id: str,
    name: str,
    max_capacity_kg: float = 50.0,
    status: VolunteerStatus = VolunteerStatus.AVAILABLE,
    latitude: float = 40.7150,
    longitude: float = -74.0080,
) -> Volunteer:
    """Helper constructing test volunteer."""
    return Volunteer(
        volunteer_id=volunteer_id,
        volunteer_name=name,
        phone="+12125550177",
        address="300 Volunteer Way",
        coordinates=Coordinates(latitude=latitude, longitude=longitude),
        status=status,
        max_capacity_kg=max_capacity_kg,
        vehicle_type="van",
        service_region="metro-core",
    )


def test_assign_volunteer_happy_path_and_clean_replay() -> None:
    """Hardcore requirement: clean initial assignment followed by idempotent replay.

    Replay must return existing assignment without duplicate mutations or duplicate
    notifications.
    """
    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_v_repo = mock.create_autospec(VolunteersRepository, instance=True)
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)
    mock_sns = mock.MagicMock()

    don_initial = make_donation()
    vol = make_volunteer("vol-001", "Alex Driver")

    # Initial state: donation unassigned
    mock_d_repo.get_donation.return_value = don_initial
    mock_v_repo.query_available_volunteers_by_region.return_value = [vol]
    mock_v_repo.set_volunteer_availability.return_value = True
    mock_d_repo.assign_volunteer.return_value = True
    mock_a_repo.record_audit_event.return_value = True

    # 1. First invocation
    result1 = assign_volunteer(
        donation_id="don-assign-01",
        service_region="metro-core",
        donations_repo=mock_d_repo,
        volunteers_repo=mock_v_repo,
        audit_repo=mock_a_repo,
        sns_client=mock_sns,
    )
    assert result1 is not None
    assert result1.volunteer_id == "vol-001"
    assert mock_sns.publish.call_count == 1
    assert mock_v_repo.set_volunteer_availability.call_count == 1
    assert mock_d_repo.assign_volunteer.call_count == 1

    # 2. Replay invocation: donation is now recorded as assigned in DB
    don_assigned = make_donation(assigned_volunteer_id="vol-001")
    mock_d_repo.get_donation.return_value = don_assigned
    mock_v_repo.get_volunteer.return_value = vol
    # Audit trail contains the notification event from attempt 1
    mock_a_repo.query_audit_trail_by_donation.return_value = [
        AuditEvent(
            event_id="evt-notif-01",
            donation_id="don-assign-01",
            action="NOTIFICATION_DISPATCHED",
            actor="strands_orchestrator",
            idempotency_key="don-assign-01:notify_volunteer",
            details={},
        )
    ]

    result2 = assign_volunteer(
        donation_id="don-assign-01",
        service_region="metro-core",
        donations_repo=mock_d_repo,
        volunteers_repo=mock_v_repo,
        audit_repo=mock_a_repo,
        sns_client=mock_sns,
    )
    assert result2 is not None
    assert result2.volunteer_id == "vol-001"

    # Verify no second notification was published and no second mutation ran
    assert mock_sns.publish.call_count == 1
    assert mock_v_repo.set_volunteer_availability.call_count == 1
    assert mock_d_repo.assign_volunteer.call_count == 1


def test_assign_volunteer_recovers_missing_notification_after_crash() -> None:
    """Verify replay detects missing notification when crash occurred before send."""
    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_v_repo = mock.create_autospec(VolunteersRepository, instance=True)
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)
    mock_sns = mock.MagicMock()

    # Partial failure state: donation assigned, but no notification recorded
    don_partial = make_donation(assigned_volunteer_id="vol-001")
    vol = make_volunteer("vol-001", "Alex Driver")

    mock_d_repo.get_donation.return_value = don_partial
    mock_v_repo.get_volunteer.return_value = vol
    mock_a_repo.query_audit_trail_by_donation.return_value = []

    result = assign_volunteer(
        donation_id="don-assign-01",
        service_region="metro-core",
        donations_repo=mock_d_repo,
        volunteers_repo=mock_v_repo,
        audit_repo=mock_a_repo,
        sns_client=mock_sns,
    )
    assert result is not None
    assert result.volunteer_id == "vol-001"
    # Verification: Missing notification was recovered and dispatched!
    assert mock_sns.publish.call_count == 1
    # No extra mutations were attempted
    assert mock_d_repo.assign_volunteer.call_count == 0


def test_candidate_race_fallback_loop() -> None:
    """Verify race fallback advances to next candidate without compensation error."""
    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_v_repo = mock.create_autospec(VolunteersRepository, instance=True)
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)
    mock_sns = mock.MagicMock()

    don = make_donation()
    vol_primary = make_volunteer("vol-primary", "Primary Choice", latitude=40.7130)
    vol_secondary = make_volunteer(
        "vol-secondary", "Secondary Choice", latitude=40.7160
    )

    mock_d_repo.get_donation.return_value = don
    mock_v_repo.query_available_volunteers_by_region.return_value = [
        vol_primary,
        vol_secondary,
    ]

    # Primary volunteer fails (claimed concurrently); secondary succeeds
    mock_v_repo.set_volunteer_availability.side_effect = [
        VolunteerUnavailableError("Claimed concurrently"),
        True,
    ]
    mock_d_repo.assign_volunteer.return_value = True

    result = assign_volunteer(
        donation_id="don-assign-01",
        service_region="metro-core",
        donations_repo=mock_d_repo,
        volunteers_repo=mock_v_repo,
        audit_repo=mock_a_repo,
        sns_client=mock_sns,
    )

    assert result is not None
    assert result.volunteer_id == "vol-secondary"
    # Primary flip failed, secondary flip succeeded -> exactly 2 calls
    assert mock_v_repo.set_volunteer_availability.call_count == 2
    mock_d_repo.assign_volunteer.assert_called_once_with(
        "don-assign-01", "vol-secondary"
    )


def test_compensation_on_donation_link_failure() -> None:
    """Verify volunteer availability is rolled back if donation link condition fails."""
    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_v_repo = mock.create_autospec(VolunteersRepository, instance=True)
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)

    don = make_donation()
    vol = make_volunteer("vol-001", "Alex Driver")

    mock_d_repo.get_donation.return_value = don
    mock_v_repo.query_available_volunteers_by_region.return_value = [vol]
    mock_v_repo.set_volunteer_availability.return_value = True
    # Donation assignment fails (concurrent claim or condition mismatch)
    mock_d_repo.assign_volunteer.return_value = False

    result = assign_volunteer(
        donation_id="don-assign-01",
        service_region="metro-core",
        donations_repo=mock_d_repo,
        volunteers_repo=mock_v_repo,
        audit_repo=mock_a_repo,
    )

    assert result is None
    # 1st call: set unavailable (False), 2nd call: revert to available (True)
    assert mock_v_repo.set_volunteer_availability.call_count == 2
    mock_v_repo.set_volunteer_availability.assert_has_calls(
        [
            mock.call("vol-001", is_available=False),
            mock.call("vol-001", is_available=True),
        ]
    )


def test_donation_link_failure_halts_candidate_loop() -> None:
    """Verify donation link failure halts loop without trying other candidates."""
    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_v_repo = mock.create_autospec(VolunteersRepository, instance=True)
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)

    don = make_donation()
    vol1 = make_volunteer("vol-01", "Driver One", latitude=40.7130)
    vol2 = make_volunteer("vol-02", "Driver Two", latitude=40.7140)
    vol3 = make_volunteer("vol-03", "Driver Three", latitude=40.7150)

    mock_d_repo.get_donation.return_value = don
    mock_v_repo.query_available_volunteers_by_region.return_value = [
        vol1,
        vol2,
        vol3,
    ]
    mock_v_repo.set_volunteer_availability.return_value = True
    # Donation link fails (e.g. simulated DynamoDB condition mismatch)
    mock_d_repo.assign_volunteer.return_value = False

    result = assign_volunteer(
        donation_id="don-assign-01",
        service_region="metro-core",
        donations_repo=mock_d_repo,
        volunteers_repo=mock_v_repo,
        audit_repo=mock_a_repo,
    )

    assert result is None
    # Assert loop halted: only vol1 was touched (once to claim, once to compensate)
    assert mock_v_repo.set_volunteer_availability.call_count == 2
    mock_v_repo.set_volunteer_availability.assert_has_calls(
        [
            mock.call("vol-01", is_available=False),
            mock.call("vol-01", is_available=True),
        ]
    )
    # Subsequent candidates vol2 and vol3 were never called
    mock_d_repo.assign_volunteer.assert_called_once_with("don-assign-01", "vol-01")


def test_double_failure_raises_infrastructure_consistency_error() -> None:
    """Verify compensation double-failure raises InfrastructureConsistencyError."""
    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_v_repo = mock.create_autospec(VolunteersRepository, instance=True)
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)

    don = make_donation()
    vol = make_volunteer("vol-001", "Alex Driver")

    mock_d_repo.get_donation.return_value = don
    mock_v_repo.query_available_volunteers_by_region.return_value = [vol]
    mock_d_repo.assign_volunteer.return_value = False

    # First flip succeeds, rollback flip fails
    mock_v_repo.set_volunteer_availability.side_effect = [
        True,
        RuntimeError("DynamoDB connection lost during rollback"),
    ]

    with pytest.raises(InfrastructureConsistencyError, match="Compensation failed"):
        assign_volunteer(
            donation_id="don-assign-01",
            service_region="metro-core",
            donations_repo=mock_d_repo,
            volunteers_repo=mock_v_repo,
            audit_repo=mock_a_repo,
        )


def test_concurrent_donations_competing_for_single_volunteer() -> None:
    """Verify 2 concurrent donations targeting 1 available volunteer.

    Assert exactly one donation secures the volunteer, and the second cleanly
    receives None (for NO_MATCH_WITHIN_WINDOW escalation) without raising any
    compensation error or corrupting state.
    """
    import concurrent.futures
    import threading

    mock_d_repo = mock.create_autospec(DonationsRepository, instance=True)
    mock_v_repo = mock.create_autospec(VolunteersRepository, instance=True)
    mock_a_repo = mock.create_autospec(AuditRepository, instance=True)

    don_a = make_donation(donation_id="don-A")
    don_b = make_donation(donation_id="don-B")
    vol_single = make_volunteer("vol-single", "Solo Driver")

    def mock_get_donation(
        donation_id: str, *args: object, **kwargs: object
    ) -> Donation:
        del args, kwargs
        return don_a if donation_id == "don-A" else don_b

    mock_d_repo.get_donation.side_effect = mock_get_donation
    mock_v_repo.query_available_volunteers_by_region.return_value = [vol_single]
    mock_d_repo.assign_volunteer.return_value = True
    mock_a_repo.record_audit_event.return_value = True

    # Emulate atomic DynamoDB conditional write for volunteer availability
    lock = threading.Lock()
    is_claimed = False

    def atomic_set_volunteer_availability(
        volunteer_id: str, is_available: bool, *args: object, **kwargs: object
    ) -> bool:
        del volunteer_id, args, kwargs
        nonlocal is_claimed
        with lock:
            if not is_available:
                if is_claimed:
                    raise VolunteerUnavailableError("Claimed concurrently")
                is_claimed = True
                return True
            is_claimed = False
            return True

    mock_v_repo.set_volunteer_availability.side_effect = (
        atomic_set_volunteer_availability
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        fut_a = executor.submit(
            assign_volunteer,
            donation_id="don-A",
            service_region="metro-core",
            donations_repo=mock_d_repo,
            volunteers_repo=mock_v_repo,
            audit_repo=mock_a_repo,
        )
        fut_b = executor.submit(
            assign_volunteer,
            donation_id="don-B",
            service_region="metro-core",
            donations_repo=mock_d_repo,
            volunteers_repo=mock_v_repo,
            audit_repo=mock_a_repo,
        )

        res_a = fut_a.result()
        res_b = fut_b.result()

    # Exactly one donation must secure the volunteer; the other receives None cleanly
    results = [res_a, res_b]
    successful_assignments = [r for r in results if r is not None]
    unmatched_results = [r for r in results if r is None]

    assert len(successful_assignments) == 1
    assert len(unmatched_results) == 1
    assert successful_assignments[0].volunteer_id == "vol-single"

