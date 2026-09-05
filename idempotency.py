"""Deterministic idempotency key generation and validation for coordination actions.

Ensures that retried Lambda invocations, audit records, and volunteer assignments
produce identical, collision-resistant keys scoped strictly to the entity and action,
guaranteeing seamless deduplication across retry chains.
"""

import re

# Regex pattern validating the standardized deterministic idempotency key format
IDEMPOTENCY_KEY_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+$"
)


def build_idempotency_key(donation_id: str, action: str) -> str:
    """Build a deterministic idempotency key strictly bound to an operation.

    Formula:
        {donation_id}:{action}

    Because attempt numbers are intentionally excluded, any subsequent retry
    (e.g., Lambda timeout replay, network retry, service re-invocation)
    generates the exact same key, enabling robust deduplication.

    Args:
        donation_id: The target donation identifier (non-empty alphanumeric/hyphen).
        action: Specific action or state transition name (e.g. ASSIGN_VOLUNTEER).

    Returns:
        Formatted deterministic idempotency key string.

    Raises:
        ValueError: If donation_id or action is empty/invalid.
    """
    cleaned_donation = donation_id.strip()
    cleaned_action = action.strip().upper()

    if not cleaned_donation:
        raise ValueError("donation_id must be a non-empty string")
    if not cleaned_action:
        raise ValueError("action must be a non-empty string")

    key: str = f"{cleaned_donation}:{cleaned_action}"
    if not IDEMPOTENCY_KEY_PATTERN.match(key):
        raise ValueError(
            f"Generated key contains illegal characters: {key}. "
            "Must only contain alphanumeric characters, hyphens, and colons."
        )

    return key


def parse_idempotency_key(key: str) -> tuple[str, str]:
    """Parse and validate a deterministic idempotency key into its components.

    Args:
        key: The serialized idempotency key string.

    Returns:
        Tuple of (donation_id, action).

    Raises:
        ValueError: If the key does not conform to the required format.
    """
    if not IDEMPOTENCY_KEY_PATTERN.match(key):
        raise ValueError(
            f"Invalid idempotency key format: '{key}'. "
            "Expected format: '{donation_id}:{action}'"
        )

    parts = key.split(":")
    return parts[0], parts[1]
