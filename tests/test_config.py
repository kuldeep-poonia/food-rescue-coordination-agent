"""Unit tests for centralized application configuration and environment resolution."""

import os
from dataclasses import FrozenInstanceError
from unittest import mock

import pytest

from config import (
    CAPACITY_WARNING_THRESHOLD_KG,
    DEFAULT_CLIENT_TIMEOUT_SECONDS,
    DEFAULT_MEMORY_TTL_DAYS,
    DEFAULT_SESSION_TTL_HOURS,
    FOOD_SAFETY_MIN_SHELF_LIFE_MINUTES,
    KG_TO_MEALS_CONVERSION_FACTOR,
    MAX_MATCH_DISTANCE_KM,
    MAX_TRANSIENT_RETRY_ATTEMPTS,
    load_app_configuration,
)


def test_configuration_defaults_match_placeholders() -> None:
    """Verify default placeholder values are assigned when environment is empty."""
    with mock.patch.dict(os.environ, {}, clear=True):
        cfg = load_app_configuration()

        assert cfg.aws_region == "us-east-1"
        assert cfg.donations_table_name == "frca-donations-table"
        assert cfg.recipients_table_name == "frca-recipients-table"
        assert cfg.volunteers_table_name == "frca-volunteers-table"
        assert cfg.matches_audit_table_name == "frca-matches-audit-table"
        assert cfg.sessions_memory_table_name == "frca-sessions-memory-table"
        assert "notifications" in cfg.notification_topic_arn
        assert "escalations" in cfg.coordinator_escalation_topic_arn
        assert "coordinator-dlq" in cfg.coordinator_dlq_url
        assert cfg.location_place_index_name == "frca-place-index-placeholder"
        assert cfg.route_calculator_name == "frca-route-calculator-placeholder"
        assert cfg.bedrock_agent_id == "BEDROCK_AGENT_ID_PLACEHOLDER"
        assert cfg.bedrock_agent_alias_id == "BEDROCK_AGENT_ALIAS_ID_PLACEHOLDER"
        assert cfg.capacity_warning_threshold_kg == 30.0
        assert cfg.session_ttl_hours == 24
        assert cfg.memory_ttl_days == 30



def test_configuration_environment_overrides() -> None:
    """Verify environment variables override default placeholder values."""
    custom_notif = "arn:aws:sns:us-west-2:999999999999:custom-notif"
    custom_esc = "arn:aws:sns:us-west-2:999999999999:custom-esc"
    custom_env = {
        "AWS_REGION": "us-west-2",
        "DONATIONS_TABLE_NAME": "custom-donations",
        "RECIPIENTS_TABLE_NAME": "custom-recipients",
        "VOLUNTEERS_TABLE_NAME": "custom-volunteers",
        "MATCHES_AUDIT_TABLE_NAME": "custom-audit",
        "SESSIONS_MEMORY_TABLE_NAME": "custom-sessions",
        "NOTIFICATION_TOPIC_ARN": custom_notif,
        "COORDINATOR_ESCALATION_TOPIC_ARN": custom_esc,
        "COORDINATOR_DLQ_URL": "https://sqs.us-west-2.amazonaws.com/999999999999/custom-dlq",
        "LOCATION_INDEX_NAME": "custom-index",
        "ROUTE_CALCULATOR_NAME": "custom-calc",
        "AGENT_ID": "CUSTOM_AGENT_ID",
        "AGENT_ALIAS_ID": "CUSTOM_ALIAS_ID",
        "CAPACITY_WARNING_THRESHOLD_KG": "45.0",
        "SESSION_TTL_HOURS": "48",
        "MEMORY_TTL_DAYS": "60",
    }

    with mock.patch.dict(os.environ, custom_env, clear=True):
        cfg = load_app_configuration()

        assert cfg.aws_region == "us-west-2"
        assert cfg.donations_table_name == "custom-donations"
        assert cfg.recipients_table_name == "custom-recipients"
        assert cfg.volunteers_table_name == "custom-volunteers"
        assert cfg.matches_audit_table_name == "custom-audit"
        assert cfg.sessions_memory_table_name == "custom-sessions"
        assert cfg.notification_topic_arn == custom_notif
        assert cfg.coordinator_escalation_topic_arn == custom_esc
        assert "custom-dlq" in cfg.coordinator_dlq_url
        assert cfg.location_place_index_name == "custom-index"
        assert cfg.route_calculator_name == "custom-calc"
        assert cfg.bedrock_agent_id == "CUSTOM_AGENT_ID"
        assert cfg.bedrock_agent_alias_id == "CUSTOM_ALIAS_ID"
        assert cfg.capacity_warning_threshold_kg == 45.0
        assert cfg.session_ttl_hours == 48
        assert cfg.memory_ttl_days == 60


def test_configuration_immutability() -> None:
    """Verify AppConfig instance is frozen against runtime mutations."""
    cfg = load_app_configuration()
    target_field: str = "aws_region"
    with pytest.raises(FrozenInstanceError):
        setattr(cfg, target_field, "invalid-mutation")


def test_operational_threshold_constants() -> None:
    """Verify operational constants preserve expected safety and retry bounds."""
    assert FOOD_SAFETY_MIN_SHELF_LIFE_MINUTES == 60
    assert MAX_MATCH_DISTANCE_KM == 25.0
    assert DEFAULT_CLIENT_TIMEOUT_SECONDS == 30
    assert MAX_TRANSIENT_RETRY_ATTEMPTS == 3
    assert KG_TO_MEALS_CONVERSION_FACTOR == 2.0
    assert CAPACITY_WARNING_THRESHOLD_KG == 30.0
    assert DEFAULT_SESSION_TTL_HOURS == 24
    assert DEFAULT_MEMORY_TTL_DAYS == 30

