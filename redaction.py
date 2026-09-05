"""PII redaction and sanitization utilities for safe structured logging.

Ensures that contact phone numbers, street addresses, and individual names
are masked before being logged or included in diagnostic telemetry.
"""

import re
from typing import Any

# Compiled regex patterns for PII detection and masking
PHONE_REPLACEMENT_PATTERN: re.Pattern[str] = re.compile(
    r"(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?(\d{4})"
)
SENSITIVE_KEY_PATTERNS: frozenset[str] = frozenset({
    "phone",
    "contact_phone",
    "donor_phone",
    "address",
    "street_address",
    "donor_address",
    "contact_name",
    "donor_name",
    "volunteer_name",
})


def mask_phone_number(phone_raw: str) -> str:
    """Mask a raw phone number, preserving only the final four digits.

    Args:
        phone_raw: The plaintext phone string.

    Returns:
        The masked phone string with obscured area and exchange codes.

    Raises:
        None: Returns default mask if string length is insufficient.
    """
    cleaned: str = re.sub(r"[^\d+]", "", phone_raw)
    if len(cleaned) < 4:
        return "***-****"
    return f"***-***-{cleaned[-4:]}"


def mask_street_address(address_raw: str) -> str:
    """Mask specific street numbers and premises identifiers in an address.

    Args:
        address_raw: The plaintext address string.

    Returns:
        Masked address preserving general locality while obscuring premises.

    Raises:
        None: Safely returns placeholder if empty.
    """
    if not address_raw.strip():
        return "[EMPTY_ADDRESS]"

    parts: list[str] = [segment.strip() for segment in address_raw.split(",")]
    if not parts:
        return "*** [REDACTED_ADDRESS]"

    # Mask initial street number component
    first_part: str = re.sub(r"^\d+\s*", "*** ", parts[0])
    parts[0] = first_part
    return ", ".join(parts)


def sanitize_payload_for_logging(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively scrub known sensitive PII keys from a telemetry dictionary.

    Args:
        data: Arbitrary dictionary payload intended for logging.

    Returns:
        Deep-copied dictionary with sensitive values replaced by masks.

    Raises:
        None: Traverses and transforms all nested dicts and lists safely.
    """
    sanitized: dict[str, Any] = {}
    for key, value in data.items():
        normalized_key: str = key.lower()
        if normalized_key in SENSITIVE_KEY_PATTERNS and isinstance(value, str):
            if "phone" in normalized_key:
                sanitized[key] = mask_phone_number(value)
            elif "address" in normalized_key:
                sanitized[key] = mask_street_address(value)
            else:
                sanitized[key] = f"{value[:1]}***" if value else "***"
        elif isinstance(value, dict):
            # Recursively sanitize nested dictionaries
            dict_val: dict[str, Any] = {str(k): v for k, v in value.items()}
            sanitized[key] = sanitize_payload_for_logging(dict_val)
        elif isinstance(value, list):
            # Sanitize list items if they contain dicts
            sanitized[key] = [
                sanitize_payload_for_logging({str(k): v for k, v in item.items()})
                if isinstance(item, dict)
                else item
                for item in value
            ]
        else:
            sanitized[key] = value

    return sanitized
