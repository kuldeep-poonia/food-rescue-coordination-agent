"""Unit tests for centralized application configuration and environment resolution."""

import os
from dataclasses import FrozenInstanceError
from unittest import mock

import pytest

from config import (
    DEFAULT_CLIENT_TIMEOUT_SECONDS,
    FOOD_SAFETY_MIN_SHELF_LIFE_MINUTES,
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
        assert "notifications" in cfg.notification_topic_arn
        assert "escalations" in cfg.coordinator_escalation_topic_arn
        assert cfg.location_place_index_name == "frca-place-index-placeholder"
        assert cfg.route_calculator_name == "frca-route-calculator-placeholder"
        assert cfg.bedrock_agent_id == "BEDROCK_AGENT_ID_PLACEHOLDER"
        assert cfg.bedrock_agent_alias_id == "BEDROCK_AGENT_ALIAS_ID_PLACEHOLDER"


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
        "NOTIFICATION_TOPIC_ARN": custom_notif,
        "COORDINATOR_ESCALATION_TOPIC_ARN": custom_esc,
        "LOCATION_INDEX_NAME": "custom-index",
        "ROUTE_CALCULATOR_NAME": "custom-calc",
        "AGENT_ID": "CUSTOM_AGENT_ID",
        "AGENT_ALIAS_ID": "CUSTOM_ALIAS_ID",
    }

    with mock.patch.dict(os.environ, custom_env, clear=True):
        cfg = load_app_configuration()

        assert cfg.aws_region == "us-west-2"
        assert cfg.donations_table_name == "custom-donations"
        assert cfg.recipients_table_name == "custom-recipients"
        assert cfg.volunteers_table_name == "custom-volunteers"
        assert cfg.matches_audit_table_name == "custom-audit"
        assert cfg.notification_topic_arn == custom_notif
        assert cfg.coordinator_escalation_topic_arn == custom_esc
        assert cfg.location_place_index_name == "custom-index"
        assert cfg.route_calculator_name == "custom-calc"
        assert cfg.bedrock_agent_id == "CUSTOM_AGENT_ID"
        assert cfg.bedrock_agent_alias_id == "CUSTOM_ALIAS_ID"


def test_configuration_immutability() -> None:
    """Verify AppConfig instance is frozen against runtime mutations."""
    cfg = load_app_configuration()
    with pytest.raises(FrozenInstanceError):
        cfg.aws_region = "invalid-mutation"


def test_operational_threshold_constants() -> None:
    """Verify operational constants preserve expected safety and retry bounds."""
    assert FOOD_SAFETY_MIN_SHELF_LIFE_MINUTES == 60
    assert MAX_MATCH_DISTANCE_KM == 25.0
    assert DEFAULT_CLIENT_TIMEOUT_SECONDS == 30
    assert MAX_TRANSIENT_RETRY_ATTEMPTS == 3
