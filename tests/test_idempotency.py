"""Unit tests for deterministic idempotency key builder and parser."""

import pytest

from idempotency import build_idempotency_key, parse_idempotency_key


def test_build_idempotency_key_valid() -> None:
    """Verify standard valid inputs produce formatted key string."""
    key = build_idempotency_key("don-1234", "assign_volunteer", 1)
    assert key == "don-1234:ASSIGN_VOLUNTEER:1"

    parsed_donation, parsed_action, parsed_attempt = parse_idempotency_key(key)
    assert parsed_donation == "don-1234"
    assert parsed_action == "ASSIGN_VOLUNTEER"
    assert parsed_attempt == 1


def test_build_idempotency_key_rejects_empty_inputs() -> None:
    """Verify empty donation ID or empty action raises ValueError."""
    with pytest.raises(ValueError, match="donation_id must be a non-empty string"):
        build_idempotency_key("", "ASSIGN_VOLUNTEER", 1)

    with pytest.raises(ValueError, match="action must be a non-empty string"):
        build_idempotency_key("don-1234", "   ", 1)


    err_msg = "attempt_number must be greater than or equal to 1"
    with pytest.raises(ValueError, match=err_msg):
        build_idempotency_key("don-1234", "ASSIGN_VOLUNTEER", 0)

    with pytest.raises(ValueError, match=err_msg):
        build_idempotency_key("don-1234", "ASSIGN_VOLUNTEER", -2)


def test_parse_idempotency_key_rejects_malformed_formats() -> None:
    """Verify invalid format strings fail parsing with ValueError."""
    malformed_keys = [
        "random-uuid-without-colons",
        "don-1234:ASSIGN",
        "don-1234:ASSIGN:0",
        "don-1234:ASSIGN:abc",
        ":ASSIGN:1",
    ]
    for bad_key in malformed_keys:
        with pytest.raises(ValueError, match="Invalid idempotency key format"):
            parse_idempotency_key(bad_key)
