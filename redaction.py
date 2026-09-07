"""PII, credential, and coordinate redaction utilities for safe structured logging.

Ensures that contact phone numbers, street addresses, individual names,
geographic coordinates, and authentication tokens/secrets are masked before
being logged or included in diagnostic telemetry.
"""

import re
from typing import Any

# Compiled regex patterns for PII, coordinate, and secret detection and masking
PHONE_REPLACEMENT_PATTERN: re.Pattern[str] = re.compile(
    r"(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?(\d{4})"
)
STREET_ADDRESS_PATTERN: re.Pattern[str] = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9\s.,#-]+?\b(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Plaza|Plz|Suite|Ste|Apt)\b[^\n,]*",
    re.IGNORECASE,
)
COORDINATE_TEXT_PATTERN: re.Pattern[str] = re.compile(
    r"[-+]?\d{1,2}\.\d{3,},\s*[-+]?\d{1,3}\.\d{3,}"
)
COORDINATE_KV_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)\b(lat(?:itude)?|lon(?:gitude)?)\s*[:=]\s*[-+]?\d{1,3}\.\d{3,}"
)
BEARER_TOKEN_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}"
)
SECRET_KV_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)\b(api[_-]?key|auth[_-]?token|tracking[_-]?token|bearer[_-]?token|secret|password|authorization)\s*[:=]\s*['\"]?[A-Za-z0-9._~+/-]{6,}['\"]?"
)

SENSITIVE_KEY_PATTERNS: frozenset[str] = frozenset(
    {
        "phone",
        "contact_phone",
        "donor_phone",
        "address",
        "street_address",
        "donor_address",
        "contact_name",
        "donor_name",
        "volunteer_name",
    }
)

COORDINATE_KEY_PATTERNS: frozenset[str] = frozenset(
    {
        "coordinates",
        "coords",
        "latitude",
        "longitude",
        "lat",
        "lon",
        "pickup_coords",
        "delivery_coords",
        "location_coords",
        "start_coords",
        "end_coords",
    }
)

SECRET_KEY_PATTERNS: frozenset[str] = frozenset(
    {
        "token",
        "auth_token",
        "tracking_token",
        "bearer_token",
        "authorization",
        "api_key",
        "coordinator_api_key",
        "secret",
        "password",
        "access_token",
        "refresh_token",
        "credential",
        "credentials",
        "private_key",
        "secret_key",
    }
)


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


def mask_coordinates() -> str:
    """Return standard placeholder for redacted coordinates."""
    return "*** [REDACTED_COORDINATES]"


def mask_secret() -> str:
    """Return standard placeholder for redacted secrets or auth tokens."""
    return "*** [REDACTED_SECRET]"


def sanitize_payload_for_logging(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively scrub sensitive PII, secrets, and coordinates from a telemetry dict.

    Args:
        data: Arbitrary dictionary payload intended for logging.

    Returns:
        Deep-copied dictionary with sensitive values replaced by masks.

    Raises:
        None: Traverses and transforms all nested dicts, lists, and values safely.
    """
    sanitized: dict[str, Any] = {}
    for key, value in data.items():
        normalized_key: str = key.lower()

        # 1. Secrets & Auth Tokens
        if normalized_key in SECRET_KEY_PATTERNS or any(
            sec in normalized_key for sec in ("api_key", "secret", "password", "token")
        ):
            sanitized[key] = mask_secret()
            continue

        # 2. Coordinates
        if normalized_key in COORDINATE_KEY_PATTERNS:
            sanitized[key] = mask_coordinates()
            continue

        # 3. Known PII (Phone, Address, Names)
        if normalized_key in SENSITIVE_KEY_PATTERNS:
            if isinstance(value, str):
                if "phone" in normalized_key:
                    sanitized[key] = mask_phone_number(value)
                elif "address" in normalized_key:
                    sanitized[key] = mask_street_address(value)
                else:
                    sanitized[key] = f"{value[:1]}***" if value else "***"
            else:
                sanitized[key] = "*** [REDACTED_PII]"
            continue

        # 4. Nested structures
        if isinstance(value, dict):
            dict_val: dict[str, Any] = {str(k): v for k, v in value.items()}
            sanitized[key] = sanitize_payload_for_logging(dict_val)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_payload_for_logging({str(k): v for k, v in item.items()})
                if isinstance(item, dict)
                else (
                    sanitize_text_for_logging(item)
                    if isinstance(item, str)
                    else item
                )
                for item in value
            ]
        elif isinstance(value, str):
            sanitized[key] = sanitize_text_for_logging(value)
        else:
            sanitized[key] = value

    return sanitized


def sanitize_text_for_logging(text: str) -> str:
    """Sanitize raw text strings by masking phone numbers, addresses, and secrets.

    Args:
        text: Input log message, exception string, or arbitrary text.

    Returns:
        String with PII, credentials, coordinates, and tokens redacted.
    """
    if not text:
        return text

    def _phone_sub(match: re.Match[str]) -> str:
        raw: str = match.group(0)
        digits: str = re.sub(r"\D", "", raw)
        if len(digits) >= 7:
            return mask_phone_number(raw)
        return raw

    scrubbed: str = PHONE_REPLACEMENT_PATTERN.sub(_phone_sub, text)
    scrubbed = STREET_ADDRESS_PATTERN.sub("*** [REDACTED_ADDRESS]", scrubbed)
    scrubbed = BEARER_TOKEN_PATTERN.sub("Bearer *** [REDACTED_TOKEN]", scrubbed)
    scrubbed = SECRET_KV_PATTERN.sub(r"\1=*** [REDACTED_SECRET]", scrubbed)
    scrubbed = COORDINATE_TEXT_PATTERN.sub(mask_coordinates(), scrubbed)
    return COORDINATE_KV_PATTERN.sub(r"\1=*** [REDACTED_COORDINATES]", scrubbed)
