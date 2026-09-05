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
    DonationClassification,
    DonationStatus,
    EscalationReason,
    MatchCandidate,
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
from tools.send_notification import send_notification
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
        self._donations_repo = (
            donations_repo or DonationsRepository(config=self._config)
        )
        self._recipients_repo = recipients_repo or RecipientsRepository(
            config=self._config
        )
        self._volunteers_repo = volunteers_repo or VolunteersRepository(
            config=self._config
        )
        self._audit_repo = audit_repo or AuditRepository(config=self._config)
        self._distance_calculator = (
            distance_calculator or GeodesicDistanceCalculator()
        )
        self._sns_client = sns_client

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
        donation = self._donations_repo.get_donation(
            donation_id, consistent_read=True
        )
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
            LOGGER.info(
                "Donation %s already ASSIGNED, executing replay recovery",
                donation_id,
                extra={"correlation_id": corr_id},
            )
            assignment = assign_volunteer(
                donation_id=donation_id,
                service_region="metro-core",
                correlation_id=corr_id,
                donations_repo=self._donations_repo,
                volunteers_repo=self._volunteers_repo,
                audit_repo=self._audit_repo,
                distance_calculator=self._distance_calculator,
                sns_client=self._sns_client,
            )
            return OrchestrationResult(
                donation_id=donation_id,
                status=DonationStatus.ASSIGNED,
                matched_recipient_id=donation.matched_recipient_id,
                assigned_volunteer_id=(
                    assignment.volunteer_id if assignment else None
                ),
                steps_completed=[PipelineStep.ASSIGN_VOLUNTEER],
                is_dry_run=dry_run,
                correlation_id=corr_id,
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
                        "shelf_life_hours": (
                            classification.shelf_life_remaining_hours
                        ),
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
                claimed_candidate = match_result.ranked_candidates[0]
                target_recipient_id = claimed_candidate.recipient_id
                target_recipient_name = claimed_candidate.recipient_name
            else:
                for candidate in match_result.ranked_candidates:
                    try:
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
                            "exhausted_candidates": len(
                                match_result.ranked_candidates
                            )
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
        # Step 9: Dispatch Notifications
        # ----------------------------------------------------------------------
        guardrail.record_step(PipelineStep.DISPATCH_NOTIFICATIONS)
        if not dry_run:
            # Donor confirmation
            send_notification(
                recipient_type=NotificationRecipientType.DONOR,
                destination=donation.donor_phone,
                template_id="DONOR_CONFIRMATION_V1",
                parameters={
                    "donor_name": donation.donor_name,
                    "quantity_kg": donation.quantity_kg,
                    "food_category": donation.food_category.value,
                    "recipient_name": target_recipient_name,
                    "volunteer_name": volunteer_name,
                    "ready_by": donation.ready_by.isoformat(),
                },
                correlation_id=corr_id,
                sns_client=self._sns_client,
            )
            # Recipient confirmation
            rec_entity = (
                self._recipients_repo.get_recipient(target_recipient_id)
                if target_recipient_id
                else None
            )
            rec_phone = (
                rec_entity.contact_phone if rec_entity else donation.donor_phone
            )
            if rec_entity:
                target_contact_name = rec_entity.contact_name

            send_notification(
                recipient_type=NotificationRecipientType.RECIPIENT,
                destination=rec_phone,
                template_id="RECIPIENT_CONFIRMATION_V1",
                parameters={
                    "contact_name": target_contact_name,
                    "quantity_kg": donation.quantity_kg,
                    "food_category": donation.food_category.value,
                    "donor_name": donation.donor_name,
                    "volunteer_name": volunteer_name,
                },
                correlation_id=corr_id,
                sns_client=self._sns_client,
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
