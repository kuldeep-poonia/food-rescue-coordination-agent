"""Idempotent volunteer assignment tool with race fallback and notification recovery."""

import uuid
from typing import Any

from audit_repo import AuditRepository
from donations_repo import DonationsRepository
from idempotency import build_idempotency_key
from models import (
    AuditEvent,
    Donation,
    DonationStatus,
    InfrastructureConsistencyError,
    NotificationRecipientType,
    Volunteer,
    VolunteerAssignment,
)
from tools.distance_calculator import DistanceCalculator, GeodesicDistanceCalculator
from tools.logging_utils import get_structured_logger
from tools.send_notification import send_notification
from volunteers_repo import VolunteersRepository, VolunteerUnavailableError

LOGGER = get_structured_logger(__name__)


def _dispatch_assignment_notification(
    donation: Donation,
    volunteer: Volunteer,
    audit_repo: AuditRepository,
    sns_client: Any | None,
    correlation_id: str,
) -> None:
    """Send templated SMS notification to assigned volunteer and record audit log."""
    send_notification(
        recipient_type=NotificationRecipientType.VOLUNTEER,
        destination=volunteer.phone,
        template_id="VOLUNTEER_ASSIGNMENT_V1",
        parameters={
            "volunteer_name": volunteer.volunteer_name,
            "quantity_kg": donation.quantity_kg,
            "food_category": donation.food_category.value,
            "donor_name": donation.donor_name,
            "donor_address": donation.donor_address,
            "ready_by": donation.ready_by.strftime("%Y-%m-%d %H:%M UTC"),
            "recipient_name": donation.matched_recipient_id or "Recipient Shelter",
            "recipient_address": "Designated Delivery Location",
        },
        correlation_id=correlation_id,
        sns_client=sns_client,
    )

    notif_audit_event = AuditEvent(
        event_id=f"evt-{uuid.uuid4().hex[:12]}",
        donation_id=donation.donation_id,
        action="NOTIFICATION_DISPATCHED",
        actor="strands_orchestrator",
        idempotency_key=f"{donation.donation_id}:notify_volunteer",
        details={
            "recipient_type": NotificationRecipientType.VOLUNTEER.value,
            "destination": volunteer.phone,
            "correlation_id": correlation_id,
        },
    )
    audit_repo.record_audit_event(notif_audit_event)


def _handle_replay(
    donation: Donation,
    volunteers_repo: VolunteersRepository,
    audit_repo: AuditRepository,
    sns_client: Any | None,
    correlation_id: str,
) -> VolunteerAssignment:
    """Process an idempotent replay when donation is already assigned."""
    assert donation.assigned_volunteer_id is not None
    volunteer = volunteers_repo.get_volunteer(
        donation.assigned_volunteer_id, consistent_read=True
    )
    if not volunteer:
        raise ValueError(
            f"Assigned volunteer {donation.assigned_volunteer_id} not found"
        )

    # Recovery check: Did an earlier crash happen between DB mutation & notification?
    trail = audit_repo.query_audit_trail_by_donation(donation.donation_id)
    notif_dispatched = any(
        evt.action == "NOTIFICATION_DISPATCHED"
        and evt.idempotency_key == f"{donation.donation_id}:notify_volunteer"
        for evt in trail
    )

    if not notif_dispatched:
        LOGGER.info(
            "Replay detected missing notification for donation %s; recovering dispatch",
            donation.donation_id,
        )
        _dispatch_assignment_notification(
            donation=donation,
            volunteer=volunteer,
            audit_repo=audit_repo,
            sns_client=sns_client,
            correlation_id=correlation_id,
        )
    else:
        LOGGER.info(
            "Replay detected donation %s assignment complete; returning cleanly",
            donation.donation_id,
        )

    return VolunteerAssignment(
        assignment_id=f"asgn-{donation.donation_id}",
        donation_id=donation.donation_id,
        volunteer_id=donation.assigned_volunteer_id,
        recipient_id=donation.matched_recipient_id or "",
        status="assigned",
        assigned_at=donation.updated_at,
    )


def assign_volunteer(
    donation_id: str,
    service_region: str,
    correlation_id: str = "unassigned",
    donations_repo: DonationsRepository | None = None,
    volunteers_repo: VolunteersRepository | None = None,
    audit_repo: AuditRepository | None = None,
    distance_calculator: DistanceCalculator | None = None,
    sns_client: Any | None = None,
) -> VolunteerAssignment | None:
    """Assign an available volunteer to transport matched surplus food.

    Enforces:
    1. Volunteer-Agnostic Replay: Returns existing assignment if already assigned,
       recovering missing notifications if earlier process crashed mid-flight.
    2. Candidate Race Fallback Loop: If nearest candidate was claimed concurrently
       (VolunteerUnavailableError), cleanly advances to next candidate.
    3. Atomic Mutations: Sets volunteer unavailable, then links donation.
       If donation link fails, rolls back volunteer status and immediately halts
       the loop returning None (no further candidates attempted), allowing caller
       to escalate via NO_MATCH_WITHIN_WINDOW.
    4. Post-Mutation Audit & Notification: Records audit trail and dispatches SNS.

    Args:
        donation_id: Target donation identifier.
        service_region: Geographic operational area.
        correlation_id: Unique trace identifier.
        donations_repo: Optional DonationsRepository instance.
        volunteers_repo: Optional VolunteersRepository instance.
        audit_repo: Optional AuditRepository instance.
        distance_calculator: Optional DistanceCalculator instance.
        sns_client: Optional boto3 SNS client.

    Returns:
        VolunteerAssignment on success, or None if no eligible volunteer available.

    Raises:
        ValueError: If donation not found or in invalid state.
        InfrastructureConsistencyError: If compensation rollback fails.
    """
    d_repo = donations_repo or DonationsRepository()
    v_repo = volunteers_repo or VolunteersRepository()
    a_repo = audit_repo or AuditRepository()
    calc = distance_calculator or GeodesicDistanceCalculator()

    # 1. Strongly consistent read on target donation
    donation = d_repo.get_donation(donation_id, consistent_read=True)
    if not donation:
        raise ValueError(f"Donation {donation_id} not found")

    # Idempotent replay check
    if donation.assigned_volunteer_id is not None:
        return _handle_replay(
            donation=donation,
            volunteers_repo=v_repo,
            audit_repo=a_repo,
            sns_client=sns_client,
            correlation_id=correlation_id,
        )

    if donation.status != DonationStatus.MATCHED:
        raise ValueError(
            f"Cannot assign volunteer to donation {donation_id} with status "
            f"'{donation.status.value}'. Donation must be in 'matched' state."
        )

    # 2. Query available volunteers in service region
    candidates = v_repo.query_available_volunteers_by_region(service_region)
    eligible = [v for v in candidates if v.max_capacity_kg >= donation.quantity_kg]
    if not eligible:
        LOGGER.warning(
            "No eligible volunteers found for donation %s in %s (needed %.1fkg)",
            donation_id,
            service_region,
            donation.quantity_kg,
        )
        return None

    # Rank eligible volunteers by proximity to donor location
    eligible.sort(
        key=lambda v: calc.calculate_distance_km(
            donation.donor_coordinates, v.coordinates
        )
    )

    # 3. Candidate Race Fallback Loop
    secured_volunteer: Volunteer | None = None
    for candidate in eligible:
        try:
            v_repo.set_volunteer_availability(
                candidate.volunteer_id, is_available=False
            )
        except VolunteerUnavailableError:
            LOGGER.info(
                "Candidate %s claimed concurrently, advancing to next candidate",
                candidate.volunteer_id,
            )
            continue

        # Volunteer secured! Link to donation
        link_success = d_repo.assign_volunteer(donation_id, candidate.volunteer_id)
        if not link_success:
            # Revert volunteer availability
            try:
                v_repo.set_volunteer_availability(
                    candidate.volunteer_id, is_available=True
                )
            except Exception as comp_exc:
                LOGGER.critical(
                    "INFRASTRUCTURE_INCONSISTENCY: Failed to revert volunteer %s "
                    "after donation %s link failure",
                    candidate.volunteer_id,
                    donation_id,
                    extra={
                        "details": {
                            "volunteer_id": candidate.volunteer_id,
                            "donation_id": donation_id,
                            "error": str(comp_exc),
                        }
                    },
                )
                raise InfrastructureConsistencyError(
                    f"Compensation failed for volunteer {candidate.volunteer_id}"
                ) from comp_exc

            LOGGER.warning(
                "Donation %s assignment condition failed for volunteer %s",
                donation_id,
                candidate.volunteer_id,
            )
            return None

        secured_volunteer = candidate
        break

    if secured_volunteer is None:
        LOGGER.warning(
            "All candidate volunteers exhausted concurrently for donation %s",
            donation_id,
        )
        return None

    # 4. Record audit event
    audit_key = build_idempotency_key(donation_id, "assign_volunteer")
    audit_event = AuditEvent(
        event_id=f"evt-{uuid.uuid4().hex[:12]}",
        donation_id=donation_id,
        action="VOLUNTEER_ASSIGNED",
        actor="strands_orchestrator",
        idempotency_key=audit_key,
        details={
            "volunteer_id": secured_volunteer.volunteer_id,
            "vehicle_type": secured_volunteer.vehicle_type,
            "correlation_id": correlation_id,
        },
    )
    a_repo.record_audit_event(audit_event)

    # 5. Dispatch notification
    _dispatch_assignment_notification(
        donation=donation,
        volunteer=secured_volunteer,
        audit_repo=a_repo,
        sns_client=sns_client,
        correlation_id=correlation_id,
    )

    return VolunteerAssignment(
        assignment_id=f"asgn-{donation_id}",
        donation_id=donation_id,
        volunteer_id=secured_volunteer.volunteer_id,
        recipient_id=donation.matched_recipient_id or "",
        status="assigned",
    )
