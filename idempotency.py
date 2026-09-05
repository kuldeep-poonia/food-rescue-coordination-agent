"""Deterministic idempotency key generation and validation for coordination actions.

Ensures that retried Lambda invocations, audit records, and volunteer assignments
produce identical, collision-resistant keys scoped strictly to the entity, action,
and retry attempt.
"""

import re

# Regex pattern validating the standardized deterministic idempotency key format
IDEMPOTENCY_KEY_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+:[1-9]\d*$"
)


def build_idempotency_key(
    donation_id: str, action: str, attempt_number: int = 1
) -> str:
    """Build a deterministic idempotency key with strict component validation.

    Formula:
        {donation_id}:{action}:{attempt_number}

    Args:
        donation_id: The target donation identifier (non-empty alphanumeric/hyphen).
        action: Specific action or state transition name (e.g. ASSIGN_VOLUNTEER).
        attempt_number: Positive integer representing the attempt index (default: 1).

    Returns:
        Formatted deterministic idempotency key string.

    Raises:
        ValueError: If donation_id or action is empty/invalid, or attempt_number < 1.
    """
    cleaned_donation = donation_id.strip()
    cleaned_action = action.strip().upper()

    if not cleaned_donation:
        raise ValueError("donation_id must be a non-empty string")
    if not cleaned_action:
        raise ValueError("action must be a non-empty string")
    if attempt_number < 1:
        raise ValueError("attempt_number must be greater than or equal to 1")

    key: str = f"{cleaned_donation}:{cleaned_action}:{attempt_number}"
    if not IDEMPOTENCY_KEY_PATTERN.match(key):
        raise ValueError(
            f"Generated key contains illegal characters: {key}. "
            "Must only contain alphanumeric characters, hyphens, and colons."
        )

    return key


def parse_idempotency_key(key: str) -> tuple[str, str, int]:
    """Parse and validate a deterministic idempotency key into its components.

    Args:
        key: The serialized idempotency key string.

    Returns:
        Tuple of (donation_id, action, attempt_number).

    Raises:
        ValueError: If the key does not conform to the required format.
    """
    if not IDEMPOTENCY_KEY_PATTERN.match(key):
        raise ValueError(
            f"Invalid idempotency key format: '{key}'. "
            "Expected format: '{donation_id}:{action}:{attempt_number}'"
        )

    parts = key.split(":")
    return parts[0], parts[1], int(parts[2])
