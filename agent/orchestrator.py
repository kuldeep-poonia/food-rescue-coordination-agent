"""Autonomous Strands Agent Orchestrator for food rescue coordination.

Connects classification, capacity query, candidate ranking, atomic claims,
volunteer dispatch, and notification tools into an end-to-end autonomous pipeline.
Enforces decision boundaries and crash-recovery resume points in application code.
"""

import uuid
from typing import Any

from agent.decision_guardrail import DecisionGuardrail
from audit_repo import AuditRepository
from config import AppConfig, load_app_configuration
from donations_repo import (
    DonationClaimConflictError,
    DonationsRepository,
)
from models import (
    AuditEvent,
    CoordinatorNotificationStatus,
    Donation,
    DonationClassification,
    DonationStatus,
    EscalationReason,
    MatchCandidate,
    NotificationDeliveryError,
    NotificationRecipientType,
    OrchestrationResult,
    PipelineStep,
)
from recipients_repo import (
    InsufficientCapacityError,
    RecipientsRepository,
)
from tools.assign_volunteer import assign_volunteer
from tools.classify_donation import classify_donation
from tools.distance_calculator import DistanceCalculator, GeodesicDistanceCalculator
from tools.find_best_match import find_best_match
from tools.flag_for_human import flag_for_human
from tools.get_recipient_capacity import get_recipient_capacity
from tools.logging_utils import get_structured_logger
from tools.send_notification import mask_destination, send_notification
from volunteers_repo import VolunteersRepository

LOGGER = get_structured_logger(__name__)


class StrandsOrchestrator:
    """Orchestrates end-to-end surplus food donation coordination."""

    def __init__(
        self,
        donations_repo: DonationsRepository | None = None,
        recipients_repo: RecipientsRepository | None = None,
        volunteers_repo: VolunteersRepository | None = None,
        audit_repo: AuditRepository | None = None,
        distance_calculator: DistanceCalculator | None = None,
        sns_client: Any | None = None,
        config: AppConfig | None = None,
    ) -> None:
        """Initialize orchestrator with repositories and dependencies.

        Args:
            donations_repo: Optional DonationsRepository instance.
            recipients_repo: Optional RecipientsRepository instance.
            volunteers_repo: Optional VolunteersRepository instance.
            audit_repo: Optional AuditRepository instance.
            distance_calculator: Optional DistanceCalculator instance.
            sns_client: Optional boto3 SNS client.
            config: Optional application configuration instance.
        """
        self._config: AppConfig = config or load_app_configuration()
        self._donations_repo = donations_repo or DonationsRepository(
            config=self._config
        )
        self._recipients_repo = recipients_repo or RecipientsRepository(
            config=self._config
        )
        self._volunteers_repo = volunteers_repo or VolunteersRepository(
            config=self._config
        )
        self._audit_repo = audit_repo or AuditRepository(config=self._config)
        self._distance_calculator = distance_calculator or GeodesicDistanceCalculator()
        self._sns_client = sns_client

    def _dispatch_donor_notification(
        self,
        donation: Donation,
        recipient_name: str,
        volunteer_name: str,
        correlation_id: str,
        dispatched_keys: set[str],
    ) -> None:
        """Dispatch donor confirmation notification if not previously sent."""
        idempotency_key = f"{donation.donation_id}:notify_donor"
        if idempotency_key in dispatched_keys:
            LOGGER.info(
                "Donor notification already dispatched for donation %s (key: %s)",
                donation.donation_id,
                idempotency_key,
                extra={"correlation_id": correlation_id},
            )
            return

        masked_dest = mask_destination(donation.donor_phone)
        try:
            send_notification(
                recipient_type=NotificationRecipientType.DONOR,
                destination=donation.donor_phone,
                template_id="DONOR_CONFIRMATION_V1",
                parameters={
                    "donor_name": donation.donor_name,
                    "quantity_kg": donation.quantity_kg,
                    "food_category": donation.food_category.value,
                    "recipient_name": recipient_name,
                    "volunteer_name": volunteer_name,
                    "ready_by": donation.ready_by.isoformat(),
                },
                correlation_id=correlation_id,
                sns_client=self._sns_client,
            )
            audit_event = AuditEvent(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                donation_id=donation.donation_id,
                action="NOTIFICATION_DISPATCHED",
                actor="strands_orchestrator",
                idempotency_key=idempotency_key,
                details={
                    "recipient_type": NotificationRecipientType.DONOR.value,
                    "destination": masked_dest,
                    "correlation_id": correlation_id,
                },
            )
            self._audit_repo.record_audit_event(audit_event)
            dispatched_keys.add(idempotency_key)
        except NotificationDeliveryError as exc:
            LOGGER.warning(
                "Donor notification failed for %s: %s; alerting coordinator",
                donation.donation_id,
                exc.safe_error_detail,
                extra={"correlation_id": correlation_id},
            )
            fail_event = AuditEvent(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                donation_id=donation.donation_id,
                action="NOTIFICATION_DELIVERY_FAILED",
                actor="strands_orchestrator",
                idempotency_key=f"{donation.donation_id}:donor_notif_failed",
                details={
                    "recipient_type": NotificationRecipientType.DONOR.value,
                    "masked_destination": exc.masked_destination,
                    "safe_error_detail": exc.safe_error_detail,
                    "correlation_id": correlation_id,
                },
            )
            self._audit_repo.record_audit_event(fail_event)
            self._dispatch_coordinator_fallback_alert(
                donation_id=donation.donation_id,
                recipient_type="donor",
                masked_destination=exc.masked_destination,
                safe_error_detail=exc.safe_error_detail,
                correlation_id=correlation_id,
            )

    def _dispatch_recipient_notification(
        self,
        donation: Donation,
        recipient_id: str | None,
        contact_name: str,
        volunteer_name: str,
        correlation_id: str,
        dispatched_keys: set[str],
    ) -> None:
        """Dispatch recipient confirmation notification if not previously sent."""
        idempotency_key = f"{donation.donation_id}:notify_recipient"
        if idempotency_key in dispatched_keys:
            LOGGER.info(
                "Recipient notification already sent for donation %s (key: %s)",
                donation.donation_id,
                idempotency_key,
                extra={"correlation_id": correlation_id},
            )
            return

        rec_entity = (
            self._recipients_repo.get_recipient(recipient_id) if recipient_id else None
        )
        rec_phone = rec_entity.contact_phone if rec_entity else donation.donor_phone
        target_contact = rec_entity.contact_name if rec_entity else contact_name
        masked_rec_dest = mask_destination(rec_phone)

        try:
            send_notification(
                recipient_type=NotificationRecipientType.RECIPIENT,
                destination=rec_phone,
                template_id="RECIPIENT_CONFIRMATION_V1",
                parameters={
                    "contact_name": target_contact,
                    "quantity_kg": donation.quantity_kg,
                    "food_category": donation.food_category.value,
                    "donor_name": donation.donor_name,
                    "volunteer_name": volunteer_name,
                },
                correlation_id=correlation_id,
                sns_client=self._sns_client,
            )
            audit_event = AuditEvent(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                donation_id=donation.donation_id,
                action="NOTIFICATION_DISPATCHED",
                actor="strands_orchestrator",
                idempotency_key=idempotency_key,
                details={
                    "recipient_type": NotificationRecipientType.RECIPIENT.value,
                    "destination": masked_rec_dest,
                    "correlation_id": correlation_id,
                },
            )
            self._audit_repo.record_audit_event(audit_event)
            dispatched_keys.add(idempotency_key)
        except NotificationDeliveryError as exc:
            LOGGER.warning(
                "Recipient notification failed for %s: %s; alerting coordinator",
                donation.donation_id,
                exc.safe_error_detail,
                extra={"correlation_id": correlation_id},
            )
            fail_event = AuditEvent(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                donation_id=donation.donation_id,
                action="NOTIFICATION_DELIVERY_FAILED",
                actor="strands_orchestrator",
                idempotency_key=f"{donation.donation_id}:recipient_notif_failed",
                details={
                    "recipient_type": NotificationRecipientType.RECIPIENT.value,
                    "masked_destination": exc.masked_destination,
                    "safe_error_detail": exc.safe_error_detail,
                    "correlation_id": correlation_id,
                },
            )
            self._audit_repo.record_audit_event(fail_event)
            self._dispatch_coordinator_fallback_alert(
                donation_id=donation.donation_id,
                recipient_type="recipient",
                masked_destination=exc.masked_destination,
                safe_error_detail=exc.safe_error_detail,
                correlation_id=correlation_id,
            )

    def _dispatch_coordinator_fallback_alert(
        self,
        donation_id: str,
        recipient_type: str,
        masked_destination: str,
        safe_error_detail: str,
        correlation_id: str,
    ) -> None:
        """Publish coordinator alert when downstream notification delivery fails."""
        coord_key = f"{donation_id}:coordinator_notif_fallback_{recipient_type}"
        try:
            send_notification(
                recipient_type=NotificationRecipientType.COORDINATOR,
                destination="coordinator-alert",
                template_id="COORDINATOR_ESCALATION_V1",
                parameters={
                    "donation_id": donation_id,
                    "escalation_reason": "notification_delivery_failure",
                    "summary": (
                        f"Delivery to {recipient_type} ({masked_destination}) "
                        f"failed: {safe_error_detail}"
                    ),
                },
                correlation_id=correlation_id,
                sns_client=self._sns_client,
            )
            coord_event = AuditEvent(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                donation_id=donation_id,
                action="NOTIFICATION_COORDINATOR_FALLBACK",
                actor="strands_orchestrator",
                idempotency_key=coord_key,
                details={
                    "recipient_type": recipient_type,
                    "masked_destination": masked_destination,
                    "safe_error_detail": safe_error_detail,
                    "correlation_id": correlation_id,
                },
            )
            self._audit_repo.record_audit_event(coord_event)
        except Exception as fallback_exc:
            LOGGER.error(
                "Coordinator fallback notification failed for %s: %s",
                donation_id,
                fallback_exc.__class__.__name__,
                extra={"correlation_id": correlation_id},
            )

    def _handle_assigned_replay(
        self,
        donation: Donation,
        correlation_id: str,
        dry_run: bool,
    ) -> OrchestrationResult:
        """Replay recovery for already ASSIGNED donations ensuring all dispatches."""
        LOGGER.info(
            "Donation %s already ASSIGNED, executing replay recovery",
            donation.donation_id,
            extra={"correlation_id": correlation_id},
        )
        assignment = assign_volunteer(
            donation_id=donation.donation_id,
            service_region="metro-core",
            correlation_id=correlation_id,
            donations_repo=self._donations_repo,
            volunteers_repo=self._volunteers_repo,
            audit_repo=self._audit_repo,
            distance_calculator=self._distance_calculator,
            sns_client=self._sns_client,
        )

        assigned_vol_id = (
            assignment.volunteer_id if assignment else donation.assigned_volunteer_id
        )
        vol_name = "Assigned Volunteer"
        if assigned_vol_id:
            vol_entity = self._volunteers_repo.get_volunteer(assigned_vol_id)
            if vol_entity:
                vol_name = vol_entity.volunteer_name

        rec_name = "Community Partner"
        contact_name = "Coordinator"
        if donation.matched_recipient_id:
            rec_entity = self._recipients_repo.get_recipient(
                donation.matched_recipient_id
            )
            if rec_entity:
                rec_name = rec_entity.organization_name
                contact_name = rec_entity.contact_name

        if not dry_run:
            audit_trail = self._audit_repo.query_audit_trail_by_donation(
                donation.donation_id
            )
            dispatched_keys = {
                evt.idempotency_key
                for evt in audit_trail
                if evt.action == "NOTIFICATION_DISPATCHED" and evt.idempotency_key
            }

            self._dispatch_donor_notification(
                donation=donation,
                recipient_name=rec_name,
                volunteer_name=vol_name,
                correlation_id=correlation_id,
                dispatched_keys=dispatched_keys,
            )
            self._dispatch_recipient_notification(
                donation=donation,
                recipient_id=donation.matched_recipient_id,
                contact_name=contact_name,
                volunteer_name=vol_name,
                correlation_id=correlation_id,
                dispatched_keys=dispatched_keys,
            )

        return OrchestrationResult(
            donation_id=donation.donation_id,
            status=DonationStatus.ASSIGNED,
            matched_recipient_id=donation.matched_recipient_id,
            assigned_volunteer_id=assigned_vol_id,
            steps_completed=[
                PipelineStep.ASSIGN_VOLUNTEER,
                PipelineStep.DISPATCH_NOTIFICATIONS,
            ],
            is_dry_run=dry_run,
            correlation_id=correlation_id,
        )

    def coordinate_donation(
        self,
        donation_id: str,
        dry_run: bool = False,
        correlation_id: str | None = None,
    ) -> OrchestrationResult:
        """Execute autonomous coordination pipeline for a donation record.

        Args:
            donation_id: Target donation unique identifier.
            dry_run: If True, simulates pipeline without persisting mutations.
            correlation_id: Optional trace correlation identifier.

        Returns:
            OrchestrationResult containing outcome and execution history.

        Raises:
            InfrastructureConsistencyError: If automated compensation fails.
        """
        corr_id: str = correlation_id or f"corr-{uuid.uuid4().hex[:12]}"
        guardrail = DecisionGuardrail()

        # ------------------------------------------------------------------
        # Step 0: Status-Driven Resume Inspection (Strongly Consistent Read)
        # ------------------------------------------------------------------
        donation = self._donations_repo.get_donation(donation_id, consistent_read=True)
        if donation is None:
            ticket = flag_for_human(
                donation_id=donation_id,
                reason=EscalationReason.INPUT_VALIDATION_FAILURE,
                summary=f"Donation {donation_id} not found in database",
                details={"error": f"Donation {donation_id} not found"},
                correlation_id=corr_id,
                donations_repo=self._donations_repo,
                audit_repo=self._audit_repo,
                sns_client=self._sns_client,
            )
            return OrchestrationResult(
                donation_id=donation_id,
                status=DonationStatus.ESCALATED,
                escalation_ticket=ticket,
                steps_completed=[],
                is_dry_run=dry_run,
                correlation_id=corr_id,
            )

        # Terminal state check
        if donation.status in (DonationStatus.ESCALATED, DonationStatus.CLOSED):
            LOGGER.info(
                "Donation %s is in terminal state %s, returning clean no-op",
                donation_id,
                donation.status.value,
                extra={"correlation_id": corr_id},
            )
            return OrchestrationResult(
                donation_id=donation_id,
                status=donation.status,
                steps_completed=[],
                is_dry_run=dry_run,
                correlation_id=corr_id,
            )

        # Replay recovery check for already assigned donations
        if donation.status == DonationStatus.ASSIGNED:
            return self._handle_assigned_replay(
                donation=donation,
                correlation_id=corr_id,
                dry_run=dry_run,
            )

        # Resume point check: Crash occurred after atomic claim + deduct
        resuming_from_matched = donation.status == DonationStatus.MATCHED

        classification: DonationClassification | None = None
        service_region: str = "metro-core"
        target_recipient_id: str | None = donation.matched_recipient_id
        target_recipient_name: str = "Community Partner"
        target_contact_name: str = "Coordinator"

        if not resuming_from_matched:
            # ------------------------------------------------------------------
            # Step 1: Intake Validation
            # ------------------------------------------------------------------
            guardrail.record_step(PipelineStep.INTAKE)

            # ------------------------------------------------------------------
            # Step 2: Classify Donation
            # ------------------------------------------------------------------
            guardrail.record_step(PipelineStep.CLASSIFY)
            classification = classify_donation(donation)

            # ------------------------------------------------------------------
            # Step 3: Food Safety Guardrail Check (<60m shelf life)
            # ------------------------------------------------------------------
            safety_reason = guardrail.check_food_safety(
                classification.is_safety_threshold_breached
            )
            if safety_reason is not None:
                LOGGER.warning(
                    "Food safety threshold breached for donation %s (%.1fh left)",
                    donation_id,
                    classification.shelf_life_remaining_hours,
                    extra={"correlation_id": corr_id},
                )
                ticket = flag_for_human(
                    donation_id=donation_id,
                    reason=safety_reason,
                    summary=(
                        f"Food safety threshold breached: "
                        f"{classification.shelf_life_remaining_hours:.2f}h remaining"
                    ),
                    details={
                        "urgency_level": classification.urgency_level.value,
                        "shelf_life_hours": (classification.shelf_life_remaining_hours),
                        "perishability_hours": donation.perishability_hours,
                    },
                    correlation_id=corr_id,
                    donations_repo=self._donations_repo,
                    audit_repo=self._audit_repo,
                    sns_client=self._sns_client,
                )
                return OrchestrationResult(
                    donation_id=donation_id,
                    status=DonationStatus.ESCALATED,
                    classification=classification,
                    escalation_ticket=ticket,
                    steps_completed=guardrail.completed_steps,
                    is_dry_run=dry_run,
                    correlation_id=corr_id,
                )

            # ------------------------------------------------------------------
            # Step 4: Fetch Active Recipient Capacities
            # ------------------------------------------------------------------
            guardrail.record_step(PipelineStep.FETCH_CAPACITY)
            active_recipients = get_recipient_capacity(
                service_region=service_region,
                recipients_repo=self._recipients_repo,
            )

            # ------------------------------------------------------------------
            # Step 5: Multi-Factor Matching Algorithm
            # ------------------------------------------------------------------
            guardrail.record_step(PipelineStep.MATCH)
            match_result = find_best_match(
                donation=donation,
                candidates=active_recipients,
                classification=classification,
                distance_calculator=self._distance_calculator,
            )

            # ------------------------------------------------------------------
            # Step 6: Match Guardrail Check
            # ------------------------------------------------------------------
            match_escalation = guardrail.check_matching_result(match_result)
            if match_escalation is not None:
                LOGGER.warning(
                    "No match found for donation %s in region %s",
                    donation_id,
                    service_region,
                    extra={"correlation_id": corr_id},
                )
                ticket = flag_for_human(
                    donation_id=donation_id,
                    reason=match_escalation,
                    summary=(
                        match_result.rejection_reason
                        if match_result and match_result.rejection_reason
                        else "No eligible recipient organizations available"
                    ),
                    details={
                        "service_region": service_region,
                        "rejection_reason": (
                            match_result.rejection_reason
                            if match_result
                            else "No eligible recipient organizations available"
                        ),
                    },
                    correlation_id=corr_id,
                    donations_repo=self._donations_repo,
                    audit_repo=self._audit_repo,
                    sns_client=self._sns_client,
                )
                return OrchestrationResult(
                    donation_id=donation_id,
                    status=DonationStatus.ESCALATED,
                    classification=classification,
                    escalation_ticket=ticket,
                    steps_completed=guardrail.completed_steps,
                    is_dry_run=dry_run,
                    correlation_id=corr_id,
                )

            # ------------------------------------------------------------------
            # Step 7: Recipient Candidate Fallback Loop via Atomic Transaction
            # ------------------------------------------------------------------
            guardrail.record_step(PipelineStep.CLAIM_RECIPIENT)
            claimed_candidate: MatchCandidate | None = None

            if dry_run:
                # Read-only simulation: verify claimability and capacity
                if (
                    donation.status != DonationStatus.REPORTED
                    or donation.matched_recipient_id is not None
                ):
                    LOGGER.warning(
                        "Dry-run simulation detected state conflict for %s",
                        donation_id,
                        extra={"correlation_id": corr_id},
                    )
                    ticket = flag_for_human(
                        donation_id=donation_id,
                        reason=EscalationReason.RECIPIENT_CLAIM_CONFLICT,
                        summary=f"Donation {donation_id} already claimed",
                        details={"simulated_conflict": True},
                        correlation_id=corr_id,
                        donations_repo=self._donations_repo,
                        audit_repo=self._audit_repo,
                        sns_client=self._sns_client,
                    )
                    return OrchestrationResult(
                        donation_id=donation_id,
                        status=DonationStatus.ESCALATED,
                        classification=classification,
                        escalation_ticket=ticket,
                        steps_completed=guardrail.completed_steps,
                        is_dry_run=dry_run,
                        correlation_id=corr_id,
                    )

                for candidate in match_result.ranked_candidates:
                    rec_entity = self._recipients_repo.get_recipient(
                        candidate.recipient_id, consistent_read=True
                    )
                    if (
                        rec_entity is not None
                        and rec_entity.capacity_kg_remaining >= donation.quantity_kg
                    ):
                        claimed_candidate = candidate
                        target_recipient_id = candidate.recipient_id
                        target_recipient_name = candidate.recipient_name
                        break
                    LOGGER.info(
                        "Dry-run: candidate %s lacks capacity, trying next",
                        candidate.recipient_id,
                        extra={"correlation_id": corr_id},
                    )

                if claimed_candidate is None:
                    LOGGER.warning(
                        "Dry-run: All %d candidates lack capacity for %s",
                        len(match_result.ranked_candidates),
                        donation_id,
                        extra={"correlation_id": corr_id},
                    )
                    ticket = flag_for_human(
                        donation_id=donation_id,
                        reason=EscalationReason.NO_MATCH_WITHIN_WINDOW,
                        summary="All matched recipient candidates lack capacity",
                        details={
                            "exhausted_candidates": len(match_result.ranked_candidates)
                        },
                        correlation_id=corr_id,
                        donations_repo=self._donations_repo,
                        audit_repo=self._audit_repo,
                        sns_client=self._sns_client,
                    )
                    return OrchestrationResult(
                        donation_id=donation_id,
                        status=DonationStatus.ESCALATED,
                        classification=classification,
                        escalation_ticket=ticket,
                        steps_completed=guardrail.completed_steps,
                        is_dry_run=dry_run,
                        correlation_id=corr_id,
                    )
            else:
                for candidate in match_result.ranked_candidates:
                    try:
                        LOGGER.info(
                            "Attempting atomic claim on recipient: %s "
                            "(name: %s, score: %.2f) for %.2f kg",
                            candidate.recipient_id,
                            candidate.recipient_name,
                            candidate.score,
                            donation.quantity_kg,
                            extra={"correlation_id": corr_id},
                        )
                        self._donations_repo.claim_and_deduct_recipient(
                            donation_id=donation.donation_id,
                            recipient_id=candidate.recipient_id,
                            quantity_kg=donation.quantity_kg,
                        )
                        claimed_candidate = candidate
                        target_recipient_id = candidate.recipient_id
                        target_recipient_name = candidate.recipient_name
                        break
                    except DonationClaimConflictError:
                        LOGGER.warning(
                            "Donation %s claim race conflict on recipient %s",
                            donation_id,
                            candidate.recipient_id,
                            extra={"correlation_id": corr_id},
                        )
                        ticket = flag_for_human(
                            donation_id=donation_id,
                            reason=EscalationReason.RECIPIENT_CLAIM_CONFLICT,
                            summary=(
                                f"Donation {donation_id} already claimed concurrently"
                            ),
                            details={"candidate_id": candidate.recipient_id},
                            correlation_id=corr_id,
                            donations_repo=self._donations_repo,
                            audit_repo=self._audit_repo,
                            sns_client=self._sns_client,
                        )
                        return OrchestrationResult(
                            donation_id=donation_id,
                            status=DonationStatus.ESCALATED,
                            classification=classification,
                            escalation_ticket=ticket,
                            steps_completed=guardrail.completed_steps,
                            is_dry_run=dry_run,
                            correlation_id=corr_id,
                        )
                    except InsufficientCapacityError:
                        LOGGER.info(
                            "Candidate %s has insufficient capacity concurrently, "
                            "advancing to next ranked candidate",
                            candidate.recipient_id,
                            extra={"correlation_id": corr_id},
                        )
                        continue

                if claimed_candidate is None:
                    LOGGER.warning(
                        "All %d candidates exhausted concurrently for donation %s",
                        len(match_result.ranked_candidates),
                        donation_id,
                        extra={"correlation_id": corr_id},
                    )
                    ticket = flag_for_human(
                        donation_id=donation_id,
                        reason=EscalationReason.NO_MATCH_WITHIN_WINDOW,
                        summary="All matched recipient candidates exhausted",
                        details={
                            "exhausted_candidates": len(match_result.ranked_candidates)
                        },
                        correlation_id=corr_id,
                        donations_repo=self._donations_repo,
                        audit_repo=self._audit_repo,
                        sns_client=self._sns_client,
                    )
                    return OrchestrationResult(
                        donation_id=donation_id,
                        status=DonationStatus.ESCALATED,
                        classification=classification,
                        escalation_ticket=ticket,
                        steps_completed=guardrail.completed_steps,
                        is_dry_run=dry_run,
                        correlation_id=corr_id,
                    )

        # ----------------------------------------------------------------------
        # Step 8: Assign Volunteer & Atomic Resource Unwind on Exhaustion
        # ----------------------------------------------------------------------
        guardrail.record_step(PipelineStep.ASSIGN_VOLUNTEER)
        assigned_vol_id: str | None = None
        volunteer_name: str = "Assigned Volunteer"

        if dry_run:
            assigned_vol_id = "vol-dry-run-001"
        else:
            assignment = assign_volunteer(
                donation_id=donation.donation_id,
                service_region=service_region,
                correlation_id=corr_id,
                donations_repo=self._donations_repo,
                volunteers_repo=self._volunteers_repo,
                audit_repo=self._audit_repo,
                distance_calculator=self._distance_calculator,
                sns_client=self._sns_client,
            )

            if assignment is None:
                LOGGER.warning(
                    "Volunteers exhausted for donation %s in region %s. "
                    "Unwinding claimed recipient resources atomically.",
                    donation_id,
                    service_region,
                    extra={"correlation_id": corr_id},
                )
                # Atomic unwind rollback
                if target_recipient_id:
                    self._donations_repo.unclaim_and_restore_recipient(
                        donation_id=donation.donation_id,
                        recipient_id=target_recipient_id,
                        quantity_kg=donation.quantity_kg,
                    )

                ticket = flag_for_human(
                    donation_id=donation_id,
                    reason=EscalationReason.NO_MATCH_WITHIN_WINDOW,
                    summary="No available volunteers in service region",
                    details={
                        "unwound_recipient_id": target_recipient_id,
                        "error": "No available volunteers in region",
                    },
                    correlation_id=corr_id,
                    donations_repo=self._donations_repo,
                    audit_repo=self._audit_repo,
                    sns_client=self._sns_client,
                )
                return OrchestrationResult(
                    donation_id=donation_id,
                    status=DonationStatus.ESCALATED,
                    classification=classification,
                    matched_recipient_id=None,
                    escalation_ticket=ticket,
                    steps_completed=guardrail.completed_steps,
                    is_dry_run=dry_run,
                    correlation_id=corr_id,
                )

            assigned_vol_id = assignment.volunteer_id
            vol_entity = self._volunteers_repo.get_volunteer(assigned_vol_id)
            if vol_entity:
                volunteer_name = vol_entity.volunteer_name

        # ----------------------------------------------------------------------
        # Step 9: Dispatch Notifications with Idempotency Tracking
        # ----------------------------------------------------------------------
        guardrail.record_step(PipelineStep.DISPATCH_NOTIFICATIONS)
        if not dry_run:
            audit_trail = self._audit_repo.query_audit_trail_by_donation(
                donation.donation_id
            )
            dispatched_keys = {
                evt.idempotency_key
                for evt in audit_trail
                if evt.action == "NOTIFICATION_DISPATCHED" and evt.idempotency_key
            }

            self._dispatch_donor_notification(
                donation=donation,
                recipient_name=target_recipient_name,
                volunteer_name=volunteer_name,
                correlation_id=corr_id,
                dispatched_keys=dispatched_keys,
            )
            self._dispatch_recipient_notification(
                donation=donation,
                recipient_id=target_recipient_id,
                contact_name=target_contact_name,
                volunteer_name=volunteer_name,
                correlation_id=corr_id,
                dispatched_keys=dispatched_keys,
            )

        # ----------------------------------------------------------------------
        # Step 10: Complete Audit Logging & Produce Result
        # ----------------------------------------------------------------------
        guardrail.record_step(PipelineStep.COMPLETE)
        if not dry_run:
            self._audit_repo.record_audit_event(
                AuditEvent(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    donation_id=donation.donation_id,
                    action="COORDINATION_COMPLETED",
                    actor="strands_orchestrator",
                    idempotency_key=f"{donation.donation_id}:coordination_complete",
                    details={
                        "matched_recipient_id": target_recipient_id,
                        "assigned_volunteer_id": assigned_vol_id,
                        "correlation_id": corr_id,
                    },
                )
            )

        return OrchestrationResult(
            donation_id=donation.donation_id,
            status=DonationStatus.ASSIGNED,
            classification=classification,
            matched_recipient_id=target_recipient_id,
            assigned_volunteer_id=assigned_vol_id,
            steps_completed=guardrail.completed_steps,
            is_dry_run=dry_run,
            correlation_id=corr_id,
        )

    def _dispatch_coordinator_escalation_alert(
        self,
        donation: Donation,
        correlation_id: str,
        reason: str = EscalationReason.NO_MATCH_WITHIN_WINDOW.value,
        summary: str | None = None,
    ) -> bool:
        """Atomically claim notification lease and dispatch coordinator alert.

        Enforces pre-dispatch mutual exclusion:
        1. If already DELIVERED, returns True as a safe NO-OP.
        2. Atomically acquires DynamoDB claim lease (PENDING -> CLAIMED).
           If another worker holds active claim or state is terminal, returns
           False cleanly without calling external SNS publish (zero duplicate
           concurrent dispatch).
        3. Invokes SNS publish.
        4. On success: transitions CLAIMED -> DELIVERED and records audit event.
        5. On permanent failure (4xx non-retryable): transitions CLAIMED -> FAILED
           and records audit event.
        6. On ambiguous transport failure (socket timeout / connection failure):
           Preserves CLAIMED state and allows the lease to expire naturally
           for subsequent at-least-once reconciliation recovery.

        Args:
            donation: Target escalated Donation model.
            correlation_id: Unique trace identifier.
            reason: Escalation reason string.
            summary: Optional alert description.

        Returns:
            True if alert dispatched or already delivered; False if claim not
            acquired or delivery failed.
        """
        if (
            donation.coordinator_notification_status
            == CoordinatorNotificationStatus.DELIVERED
        ):
            LOGGER.info(
                "Coordinator alert already DELIVERED for %s; safe NO-OP",
                donation.donation_id,
                extra={"correlation_id": correlation_id},
            )
            return True

        claim_id = f"clm-{uuid.uuid4().hex[:12]}"
        acquired = self._donations_repo.claim_coordinator_notification(
            donation_id=donation.donation_id,
            claim_id=claim_id,
        )
        if not acquired:
            LOGGER.info(
                "Could not acquire coordinator notification claim for %s; "
                "active lease held or already terminal (safe NO-OP)",
                donation.donation_id,
                extra={"correlation_id": correlation_id},
            )
            return False

        alert_summary = summary or (
            f"Donation {donation.donation_id} exceeded ready_by window "
            f"({donation.ready_by.isoformat()}) without matching"
        )

        try:
            send_notification(
                recipient_type=NotificationRecipientType.COORDINATOR,
                destination="coordinator-alert",
                template_id="COORDINATOR_ESCALATION_V1",
                parameters={
                    "donation_id": donation.donation_id,
                    "escalation_reason": reason,
                    "summary": alert_summary,
                },
                correlation_id=correlation_id,
                sns_client=self._sns_client,
            )
            self._donations_repo.mark_coordinator_notification_delivered(
                donation_id=donation.donation_id,
                claim_id=claim_id,
            )
            audit_event = AuditEvent(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                donation_id=donation.donation_id,
                action="COORDINATOR_ALERT_DELIVERED",
                actor="eventbridge_time_window_monitor",
                idempotency_key=f"{donation.donation_id}:coordinator_alert_delivered",
                details={
                    "correlation_id": correlation_id,
                    "destination": "coordinator-alert",
                    "claim_id": claim_id,
                },
            )
            self._audit_repo.record_audit_event(audit_event)
            return True
        except NotificationDeliveryError as exc:
            if exc.is_ambiguous:
                LOGGER.warning(
                    "Ambiguous transport outcome for %s alert: %s; "
                    "preserving CLAIMED state until lease expires",
                    donation.donation_id,
                    exc.safe_error_detail,
                    extra={"correlation_id": correlation_id},
                )
                return False

            LOGGER.error(
                "Permanent failure delivering coordinator alert for %s: %s",
                donation.donation_id,
                exc.safe_error_detail,
                extra={"correlation_id": correlation_id},
            )
            self._donations_repo.mark_coordinator_notification_failed(
                donation_id=donation.donation_id,
                claim_id=claim_id,
                error_detail=exc.safe_error_detail,
            )
            fail_audit = AuditEvent(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                donation_id=donation.donation_id,
                action="COORDINATOR_ALERT_FAILED",
                actor="eventbridge_time_window_monitor",
                idempotency_key=f"{donation.donation_id}:coordinator_alert_failed",
                details={
                    "correlation_id": correlation_id,
                    "safe_error_detail": exc.safe_error_detail,
                    "claim_id": claim_id,
                },
            )
            self._audit_repo.record_audit_event(fail_audit)
            return False

    def reconcile_time_window_donations(
        self,
        service_region: str = "metro-core",
        current_time: Any | None = None,
    ) -> dict[str, Any]:
        """Reconcile unmatched donations against operational time windows.

        Triggered periodically (e.g. every 5 minutes by EventBridge). Queries all
        unmatched (REPORTED) donations in the region and applies strict time precedence:
        1. Past or at ready_by (ready_by <= now): Missed window. Atomically escalates
           via TransactWriteItems (status update + audit event). The winning execution
           dispatches a coordinator escalation notification.
        2. Approaching ready_by (0 < (ready_by - now) <= approaching_window_hours):
           Triggers autonomous coordination pipeline for matching and dispatch.
        3. Outside window (> approaching_window_hours): Left untouched.

        Args:
            service_region: Operational region to inspect.
            current_time: Optional datetime for deterministic evaluation.

        Returns:
            Execution summary metrics dictionary.
        """
        from datetime import datetime, timezone

        now = current_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # Step 0: Outbox Recovery - discover and recover any un-dispatched alerts
        recovered_count = 0
        pending_donations = (
            self._donations_repo.query_pending_coordinator_notifications(
                service_region=service_region
            )
        )
        for p_don in pending_donations:
            p_corr_id = f"sched-recover-{p_don.donation_id}"
            dispatched = self._dispatch_coordinator_escalation_alert(
                p_don, correlation_id=p_corr_id
            )
            if dispatched:
                recovered_count += 1

        unmatched = self._donations_repo.query_unmatched_donations_by_region(
            service_region=service_region
        )

        LOGGER.info(
            "Reconciling %d unmatched donations in region %s (now: %s)",
            len(unmatched),
            service_region,
            now.isoformat(),
        )

        escalated_count = 0
        already_escalated_count = 0
        coordinated_count = 0
        untouched_count = 0
        approaching_seconds = self._config.approaching_window_hours * 3600

        for donation in unmatched:
            ready_by = donation.ready_by
            if ready_by.tzinfo is None:
                ready_by = ready_by.replace(tzinfo=timezone.utc)

            time_diff_seconds = (ready_by - now).total_seconds()

            # Precedence 1: Past or at ready_by window -> escalate
            if time_diff_seconds <= 0:
                won = self._donations_repo.escalate_missed_window_transaction(
                    donation_id=donation.donation_id,
                    reason=EscalationReason.NO_MATCH_WITHIN_WINDOW,
                    transition_time=now,
                )
                if won:
                    escalated_count += 1
                    corr_id = f"sched-missed-{donation.donation_id}"
                    LOGGER.warning(
                        "Donation %s missed ready_by window (%s <= %s); escalated",
                        donation.donation_id,
                        ready_by.isoformat(),
                        now.isoformat(),
                        extra={"correlation_id": corr_id},
                    )
                    self._dispatch_coordinator_escalation_alert(
                        donation,
                        correlation_id=corr_id,
                        reason=EscalationReason.NO_MATCH_WITHIN_WINDOW.value,
                    )
                else:
                    already_escalated_count += 1

            # Precedence 2: Approaching ready_by window -> coordinate
            elif time_diff_seconds <= approaching_seconds:
                corr_id = f"sched-approach-{donation.donation_id}"
                LOGGER.info(
                    "Donation %s in approaching window (%.1fm left); coordinating",
                    donation.donation_id,
                    time_diff_seconds / 60.0,
                    extra={"correlation_id": corr_id},
                )
                self.coordinate_donation(
                    donation_id=donation.donation_id,
                    service_region=service_region,
                    correlation_id=corr_id,
                )
                coordinated_count += 1

            # Precedence 3: Outside window -> untouched
            else:
                untouched_count += 1

        return {
            "status": "SUCCESS",
            "service_region": service_region,
            "evaluated_count": len(unmatched),
            "escalated_count": escalated_count,
            "already_escalated_count": already_escalated_count,
            "recovered_notifications_count": recovered_count,
            "coordinated_count": coordinated_count,
            "untouched_count": untouched_count,
        }

