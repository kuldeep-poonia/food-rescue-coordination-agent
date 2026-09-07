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

# Approaching window in hours: donations with ready_by within 2.0h are routed
DEFAULT_APPROACHING_WINDOW_HOURS: float = 2.0

# Query limits for unmatched donations retrieval
DEFAULT_MAX_UNMATCHED_QUERY_LIMIT: int = 50
DEFAULT_MAX_FALLBACK_SCAN_EVALUATED_ITEMS: int = 250

# Notification claim lease duration in seconds for coordinator outbox recovery
DEFAULT_NOTIFICATION_CLAIM_LEASE_SECONDS: int = 120

# Rate limiting defaults (per minute per trusted source IP)
RATE_LIMIT_GENERAL_PER_MINUTE: int = 30
RATE_LIMIT_LOGIN_PER_MINUTE: int = 5

# Coordinator authentication constants
AUTH_TOKEN_MIN_LENGTH: int = 32
DEV_LOCAL_COORDINATOR_KEY: str = (
    "dev-insecure-coordinator-key-for-local-testing-only-32chars"
)


class ConfigurationError(Exception):
    """Raised when runtime configuration fails validation or security invariants."""



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
    approaching_window_hours: float = DEFAULT_APPROACHING_WINDOW_HOURS
    max_unmatched_query_limit: int = DEFAULT_MAX_UNMATCHED_QUERY_LIMIT
    max_fallback_scan_evaluated_items: int = DEFAULT_MAX_FALLBACK_SCAN_EVALUATED_ITEMS
    notification_claim_lease_seconds: int = DEFAULT_NOTIFICATION_CLAIM_LEASE_SECONDS
    coordinator_api_key: str = DEV_LOCAL_COORDINATOR_KEY
    rate_limit_general_per_minute: int = RATE_LIMIT_GENERAL_PER_MINUTE
    rate_limit_login_per_minute: int = RATE_LIMIT_LOGIN_PER_MINUTE



def load_app_configuration() -> AppConfig:
    """Load configuration values from environment variables with documented fallbacks.

    Returns:
        AppConfig: Immutable configuration instance with all required placeholders.

    Raises:
        ConfigurationError: If production coordinator credentials are missing
            or default.
    """
    env_name = os.environ.get("ENVIRONMENT")
    if not env_name:
        fn_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
        if "-dev" in fn_name:
            env_name = "dev"
        elif "-prod" in fn_name:
            env_name = "prod"
        elif "-staging" in fn_name:
            env_name = "staging"

    suffix = f"-{env_name}" if env_name else "-table"

    # Production coordinator credential enforcement
    coord_key = os.environ.get("COORDINATOR_API_KEY", "")
    if env_name in ("prod", "production", "staging"):
        if not coord_key or coord_key == DEV_LOCAL_COORDINATOR_KEY:
            raise ConfigurationError(
                f"COORDINATOR_API_KEY is mandatory in '{env_name}' "
                "environment and cannot use the development default."
            )
        if len(coord_key) < AUTH_TOKEN_MIN_LENGTH:
            raise ConfigurationError(
                f"COORDINATOR_API_KEY in '{env_name}' must be at least "
                f"{AUTH_TOKEN_MIN_LENGTH} characters for production security."
            )
    elif not coord_key:
        coord_key = DEV_LOCAL_COORDINATOR_KEY

    return AppConfig(
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        donations_table_name=os.environ.get(
            "DONATIONS_TABLE_NAME", f"frca-donations{suffix}"
        ),
        recipients_table_name=os.environ.get(
            "RECIPIENTS_TABLE_NAME", f"frca-recipients{suffix}"
        ),
        volunteers_table_name=os.environ.get(
            "VOLUNTEERS_TABLE_NAME", f"frca-volunteers{suffix}"
        ),
        matches_audit_table_name=os.environ.get(
            "MATCHES_AUDIT_TABLE_NAME", f"frca-matches-audit{suffix}"
        ),
        sessions_memory_table_name=os.environ.get(
            "SESSIONS_MEMORY_TABLE_NAME", f"frca-sessions-memory{suffix}"
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
        bedrock_agent_id=os.environ.get("AGENT_ID", "BEDROCK_AGENT_ID_PLACEHOLDER"),
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
        approaching_window_hours=float(
            os.environ.get(
                "APPROACHING_WINDOW_HOURS", str(DEFAULT_APPROACHING_WINDOW_HOURS)
            )
        ),
        max_unmatched_query_limit=int(
            os.environ.get(
                "MAX_UNMATCHED_QUERY_LIMIT", str(DEFAULT_MAX_UNMATCHED_QUERY_LIMIT)
            )
        ),
        max_fallback_scan_evaluated_items=int(
            os.environ.get(
                "MAX_FALLBACK_SCAN_EVALUATED_ITEMS",
                str(DEFAULT_MAX_FALLBACK_SCAN_EVALUATED_ITEMS),
            )
        ),
        notification_claim_lease_seconds=int(
            os.environ.get(
                "NOTIFICATION_CLAIM_LEASE_SECONDS",
                str(DEFAULT_NOTIFICATION_CLAIM_LEASE_SECONDS),
            )
        ),
        coordinator_api_key=coord_key,
        rate_limit_general_per_minute=int(
            os.environ.get(
                "RATE_LIMIT_GENERAL_PER_MINUTE", str(RATE_LIMIT_GENERAL_PER_MINUTE)
            )
        ),
        rate_limit_login_per_minute=int(
            os.environ.get(
                "RATE_LIMIT_LOGIN_PER_MINUTE", str(RATE_LIMIT_LOGIN_PER_MINUTE)
            )
        ),
    )

