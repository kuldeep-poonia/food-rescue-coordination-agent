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

# Industry-standard estimate: ~0.5kg per meal (1.2 lbs), per USDA
# and Feeding America guidelines
KG_TO_MEALS_CONVERSION_FACTOR: float = 2.0

# Operational threshold: capacity warning threshold in kg for advisory
# near-capacity flagging
CAPACITY_WARNING_THRESHOLD_KG: float = 30.0

# Session TTL in hours for day-scoped cache records (24 hours)
DEFAULT_SESSION_TTL_HOURS: int = 24

# Long-term memory TTL in days for entity operational patterns (30 days)
DEFAULT_MEMORY_TTL_DAYS: int = 30


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
    sessions_memory_table_name: str
    notification_topic_arn: str
    coordinator_escalation_topic_arn: str
    coordinator_dlq_url: str
    location_place_index_name: str
    route_calculator_name: str
    bedrock_agent_id: str
    bedrock_agent_alias_id: str
    capacity_warning_threshold_kg: float = CAPACITY_WARNING_THRESHOLD_KG
    session_ttl_hours: int = DEFAULT_SESSION_TTL_HOURS
    memory_ttl_days: int = DEFAULT_MEMORY_TTL_DAYS


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
        sessions_memory_table_name=os.environ.get(
            "SESSIONS_MEMORY_TABLE_NAME", "frca-sessions-memory-table"
        ),
        notification_topic_arn=os.environ.get(
            "NOTIFICATION_TOPIC_ARN",
            "arn:aws:sns:us-east-1:123456789012:frca-notifications-placeholder",
        ),
        coordinator_escalation_topic_arn=os.environ.get(
            "COORDINATOR_ESCALATION_TOPIC_ARN",
            "arn:aws:sns:us-east-1:123456789012:frca-escalations-placeholder",
        ),
        coordinator_dlq_url=os.environ.get(
            "COORDINATOR_DLQ_URL",
            "https://sqs.us-east-1.amazonaws.com/123456789012/frca-coordinator-dlq-placeholder",
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
        capacity_warning_threshold_kg=float(
            os.environ.get(
                "CAPACITY_WARNING_THRESHOLD_KG", str(CAPACITY_WARNING_THRESHOLD_KG)
            )
        ),
        session_ttl_hours=int(
            os.environ.get("SESSION_TTL_HOURS", str(DEFAULT_SESSION_TTL_HOURS))
        ),
        memory_ttl_days=int(
            os.environ.get("MEMORY_TTL_DAYS", str(DEFAULT_MEMORY_TTL_DAYS))
        ),
    )
