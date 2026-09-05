"""Explicit human escalation tool restricted strictly to defined failure scenarios."""

import uuid
from typing import Any

from audit_repo import AuditRepository
from donations_repo import DonationsRepository
from idempotency import build_idempotency_key
from models import (
    AuditEvent,
    EscalationReason,
    EscalationTicket,
    NotificationRecipientType,
)
from tools.logging_utils import get_structured_logger
from tools.send_notification import send_notification

LOGGER = get_structured_logger(__name__)


def flag_for_human(
    donation_id: str,
    reason: EscalationReason,
    summary: str,
    details: dict[str, Any] | None = None,
    correlation_id: str = "unassigned",
    audit_repo: AuditRepository | None = None,
    donations_repo: DonationsRepository | None = None,
    sns_client: Any | None = None,
    escalation_topic_arn: str | None = None,
) -> EscalationTicket:
    """Escalate a coordination exception to a human coordinator.

    Enforces the strict escalation boundary from the product specification.
    Only allows the 4 defined EscalationReason enums:
    - NO_MATCH_WITHIN_WINDOW
    - RECIPIENT_CLAIM_CONFLICT
    - FOOD_SAFETY_THRESHOLD_BREACH
    - INPUT_VALIDATION_FAILURE

    Args:
        donation_id: Target donation identifier.
        reason: Validated EscalationReason enum value.
        summary: Human-readable summary of why autonomous resolution was halted.
        details: Optional diagnostic dictionary.
        correlation_id: Unique trace identifier.
        audit_repo: Optional AuditRepository instance.
        donations_repo: Optional DonationsRepository instance.
        sns_client: Optional boto3 SNS client.
        escalation_topic_arn: Optional SNS topic ARN for coordinator alerts.

    Returns:
        Structured EscalationTicket model.

    Raises:
        ValueError: If reason is not one of the 4 defined EscalationReason values.
    """
    if not isinstance(reason, EscalationReason):
        raise ValueError(
            f"Invalid escalation reason '{reason}'. Every escalation must strictly map "
            f"to one of: {[r.value for r in EscalationReason]}"
        )

    ticket_id = f"tkt-{uuid.uuid4().hex[:12]}"
    a_repo = audit_repo or AuditRepository()
    d_repo = donations_repo or DonationsRepository()

    LOGGER.warning(
        "ESCALATION TRIGGERED: Donation %s flagged for human coordinator. Reason: %s",
        donation_id,
        reason.value,
        extra={
            "details": {
                "ticket_id": ticket_id,
                "donation_id": donation_id,
                "reason": reason.value,
                "summary": summary,
                "correlation_id": correlation_id,
            }
        },
    )

    # 1. Update donation status to escalated if donation exists
    donation = d_repo.get_donation(donation_id, consistent_read=True)
    if donation is not None:
        LOGGER.info(
            "Marking donation %s as escalated in repository", donation_id
        )
        d_repo.escalate_donation(donation_id, reason)

    # 2. Record immutable audit event
    audit_key = build_idempotency_key(donation_id, f"escalate_{reason.value}")
    audit_event = AuditEvent(
        event_id=f"evt-{uuid.uuid4().hex[:12]}",
        donation_id=donation_id,
        action="HUMAN_ESCALATION",
        actor="strands_orchestrator",
        idempotency_key=audit_key,
        details={
            "ticket_id": ticket_id,
            "reason": reason.value,
            "summary": summary,
            "correlation_id": correlation_id,
            **(details or {}),
        },
    )
    a_repo.record_audit_event(audit_event)

    # 3. Publish alert to coordinator SNS topic with idempotency check
    coord_key = f"{donation_id}:notify_coordinator_escalation"
    trail = a_repo.query_audit_trail_by_donation(donation_id)
    notif_already_sent = any(
        evt.action == "NOTIFICATION_DISPATCHED" and evt.idempotency_key == coord_key
        for evt in trail
    )
    if not notif_already_sent:
        send_notification(
            recipient_type=NotificationRecipientType.COORDINATOR,
            destination="coordinator-alert",
            template_id="COORDINATOR_ESCALATION_V1",
            parameters={
                "donation_id": donation_id,
                "escalation_reason": reason.value,
                "summary": summary,
            },
            correlation_id=correlation_id,
            sns_client=sns_client,
            topic_arn=escalation_topic_arn,
        )
        coord_audit = AuditEvent(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            donation_id=donation_id,
            action="NOTIFICATION_DISPATCHED",
            actor="strands_orchestrator",
            idempotency_key=coord_key,
            details={
                "recipient_type": NotificationRecipientType.COORDINATOR.value,
                "destination": "coordinator-alert",
                "ticket_id": ticket_id,
                "reason": reason.value,
                "correlation_id": correlation_id,
            },
        )
        a_repo.record_audit_event(coord_audit)
    else:
        LOGGER.info(
            "Coordinator escalation alert already sent for donation %s (key %s); "
            "skipping duplicate",
            donation_id,
            coord_key,
        )

    return EscalationTicket(
        ticket_id=ticket_id,
        donation_id=donation_id,
        reason=reason,
        details={"summary": summary, **(details or {})},
    )
