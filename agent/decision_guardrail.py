"""Application code-level decision guardrail for autonomous coordination.

Enforces execution lifecycle sequences, prevents illegal double-mutations, and
triggers deterministic escalation boundaries directly in code without relying
on model prompt interpretation.
"""

from typing import Any

from models import (
    EscalationReason,
    GuardrailViolationError,
    MatchResult,
    PipelineStep,
)


class DecisionGuardrail:
    """Enforces state transitions and hardcoded decision boundaries in code."""

    def __init__(self) -> None:
        """Initialize guardrail with empty completed step history."""
        self._completed_steps: list[PipelineStep] = []

    @property
    def completed_steps(self) -> list[PipelineStep]:
        """Return shallow copy of recorded pipeline steps."""
        return list(self._completed_steps)

    def record_step(self, step: PipelineStep) -> None:
        """Record transition to next pipeline step, checking anti-double mutation.

        Args:
            step: Pipeline step attempting execution.

        Raises:
            GuardrailViolationError: If attempting illegal repeat mutation or order.
        """
        # Anti-double mutation guard
        if (
            step in (PipelineStep.CLAIM_RECIPIENT, PipelineStep.ASSIGN_VOLUNTEER)
            and step in self._completed_steps
        ):
            raise GuardrailViolationError(
                f"Anti-double mutation breach: step {step.value} already executed"
            )

        self._completed_steps.append(step)

    def check_food_safety(
        self, is_safety_threshold_breached: bool
    ) -> EscalationReason | None:
        """Evaluate food safety shelf-life threshold boundary (<60m).

        Args:
            is_safety_threshold_breached: Computed boolean from classify_donation.

        Returns:
            FOOD_SAFETY_THRESHOLD_BREACH if breached, else None.
        """
        if is_safety_threshold_breached:
            return EscalationReason.FOOD_SAFETY_THRESHOLD_BREACH
        return None

    def check_matching_result(
        self, match_result: MatchResult | None
    ) -> EscalationReason | None:
        """Evaluate whether matching identified qualified recipient candidates.

        Args:
            match_result: Result payload from find_best_match.

        Returns:
            NO_MATCH_WITHIN_WINDOW if no match found or candidates empty, else None.
        """
        if match_result is None or not match_result.ranked_candidates:
            return EscalationReason.NO_MATCH_WITHIN_WINDOW
        return None

    def check_volunteer_result(
        self, volunteer_assignment: Any | None
    ) -> EscalationReason | None:
        """Evaluate volunteer assignment availability.

        Args:
            volunteer_assignment: Assignment model or None if pool exhausted.

        Returns:
            NO_MATCH_WITHIN_WINDOW if no volunteer available, else None.
        """
        if volunteer_assignment is None:
            return EscalationReason.NO_MATCH_WITHIN_WINDOW
        return None

    def check_claim_conflict(self, is_conflict: bool) -> EscalationReason | None:
        """Evaluate concurrent recipient claim race condition.

        Args:
            is_conflict: True if DonationClaimConflictError encountered.

        Returns:
            RECIPIENT_CLAIM_CONFLICT if conflict detected, else None.
        """
        if is_conflict:
            return EscalationReason.RECIPIENT_CLAIM_CONFLICT
        return None

    def check_intake_validation(self, is_valid: bool) -> EscalationReason | None:
        """Evaluate initial donation input schema validity.

        Args:
            is_valid: True if donation data passed validation.

        Returns:
            INPUT_VALIDATION_FAILURE if invalid, else None.
        """
        if not is_valid:
            return EscalationReason.INPUT_VALIDATION_FAILURE
        return None
