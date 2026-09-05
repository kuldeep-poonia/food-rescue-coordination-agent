"""Unit tests for deterministic idempotency key builder and parser."""

import pytest

from idempotency import build_idempotency_key, parse_idempotency_key


def test_build_idempotency_key_valid() -> None:
    """Verify standard valid inputs produce formatted key string."""
    key = build_idempotency_key("don-1234", "assign_volunteer")
    assert key == "don-1234:ASSIGN_VOLUNTEER"

    parsed_donation, parsed_action = parse_idempotency_key(key)
    assert parsed_donation == "don-1234"
    assert parsed_action == "ASSIGN_VOLUNTEER"


def test_build_idempotency_key_identical_across_retries() -> None:
    """Verify multiple retry invocations produce the exact same key."""
    key_attempt_1 = build_idempotency_key("don-5678", "NOTIFY_DONOR")
    key_attempt_2 = build_idempotency_key("don-5678", "NOTIFY_DONOR")
    key_attempt_3 = build_idempotency_key("don-5678", "NOTIFY_DONOR")

    assert key_attempt_1 == key_attempt_2 == key_attempt_3
    assert key_attempt_1 == "don-5678:NOTIFY_DONOR"


def test_build_idempotency_key_rejects_empty_inputs() -> None:
    """Verify empty donation ID or empty action raises ValueError."""
    with pytest.raises(ValueError, match="donation_id must be a non-empty string"):
        build_idempotency_key("", "ASSIGN_VOLUNTEER")

    with pytest.raises(ValueError, match="action must be a non-empty string"):
        build_idempotency_key("don-1234", "   ")


def test_parse_idempotency_key_rejects_malformed_formats() -> None:
    """Verify invalid format strings fail parsing with ValueError."""
    malformed_keys = [
        "random-uuid-without-colons",
        "don-1234:ASSIGN:extra_part",
        ":ASSIGN",
        "don-1234:",
    ]
    for bad_key in malformed_keys:
        with pytest.raises(ValueError, match="Invalid idempotency key format"):
            parse_idempotency_key(bad_key)
