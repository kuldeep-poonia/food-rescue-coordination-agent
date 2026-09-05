"""Centralized configuration module for Food Rescue Coordination Agent.

All environment-dependent values (AWS region, table names, ARNs, endpoints)
are managed here with explicit placeholders loaded from environment variables.
No configuration values, credentials, or resource names should be hardcoded
outside this module.
"""

import os
from dataclasses import dataclass

# Operational threshold constants with explicit rationales
# Food safety threshold: shelf-life under 60m requires coordinator review
FOOD_SAFETY_MIN_SHELF_LIFE_MINUTES: int = 60

# Operational boundary: standard max driving distance for volunteer routing
MAX_MATCH_DISTANCE_KM: float = 25.0

# Standard AWS SDK API request timeout (in seconds) to avoid hung connections
DEFAULT_CLIENT_TIMEOUT_SECONDS: int = 30

# Maximum retry attempts for transient DynamoDB and AWS service exceptions
MAX_TRANSIENT_RETRY_ATTEMPTS: int = 3


@dataclass(frozen=True)
class AppConfig:
    """Application runtime configuration holding environment-dependent parameters.

    All properties fall back to safe, explicit placeholder values suitable for
    development, local testing, and staging environments.
    """

    aws_region: str
    donations_table_name: str
    recipients_table_name: str
    volunteers_table_name: str
    matches_audit_table_name: str
    notification_topic_arn: str
    coordinator_escalation_topic_arn: str
    location_place_index_name: str
    route_calculator_name: str
    bedrock_agent_id: str
    bedrock_agent_alias_id: str


def load_app_configuration() -> AppConfig:
    """Load configuration values from environment variables with documented fallbacks.

    Returns:
        AppConfig: Immutable configuration instance with all required placeholders.

    Raises:
        None: Safe fallbacks are provided for all configurations.
    """
    return AppConfig(
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        donations_table_name=os.environ.get(
            "DONATIONS_TABLE_NAME", "frca-donations-table"
        ),
        recipients_table_name=os.environ.get(
            "RECIPIENTS_TABLE_NAME", "frca-recipients-table"
        ),
        volunteers_table_name=os.environ.get(
            "VOLUNTEERS_TABLE_NAME", "frca-volunteers-table"
        ),
        matches_audit_table_name=os.environ.get(
            "MATCHES_AUDIT_TABLE_NAME", "frca-matches-audit-table"
        ),
        notification_topic_arn=os.environ.get(
            "NOTIFICATION_TOPIC_ARN",
            "arn:aws:sns:us-east-1:123456789012:frca-notifications-placeholder",
        ),
        coordinator_escalation_topic_arn=os.environ.get(
            "COORDINATOR_ESCALATION_TOPIC_ARN",
            "arn:aws:sns:us-east-1:123456789012:frca-escalations-placeholder",
        ),
        location_place_index_name=os.environ.get(
            "LOCATION_INDEX_NAME", "frca-place-index-placeholder"
        ),
        route_calculator_name=os.environ.get(
            "ROUTE_CALCULATOR_NAME", "frca-route-calculator-placeholder"
        ),
        bedrock_agent_id=os.environ.get(
            "AGENT_ID", "BEDROCK_AGENT_ID_PLACEHOLDER"
        ),
        bedrock_agent_alias_id=os.environ.get(
            "AGENT_ALIAS_ID", "BEDROCK_AGENT_ALIAS_ID_PLACEHOLDER"
        ),
    )
