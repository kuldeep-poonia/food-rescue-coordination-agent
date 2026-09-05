"""Domain entity models and validation rules for Food Rescue Coordination Agent.

Implements strict Pydantic schemas for Donations, Recipients, Volunteers,
and AuditEvents, enforcing boundary constraints and input sanitization.
"""

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Regular expression for sanitizing and validating standardized phone numbers
E164_PHONE_REGEX: re.Pattern[str] = re.compile(r"^\+?[1-9]\d{6,14}$")

# Maximum string length for text inputs to prevent resource exhaustion attacks
MAX_TEXT_FIELD_LENGTH: int = 500


class FoodCategory(str, Enum):
    """Supported perishable and shelf-stable food categories."""

    PRODUCE = "produce"
    BAKERY = "bakery"
    PREPARED_MEALS = "prepared_meals"
    DAIRY = "dairy"
    MEAT = "meat"
    PACKAGED = "packaged"


class DonationStatus(str, Enum):
    """Lifecycle states of a surplus food donation."""

    REPORTED = "reported"
    MATCHED = "matched"
    ASSIGNED = "assigned"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    CLOSED = "closed"
    ESCALATED = "escalated"


class EscalationReason(str, Enum):
    """Exhaustive list of escalation triggers defined in product overview."""

    NO_MATCH_WITHIN_WINDOW = "no_match_within_window"
    RECIPIENT_CLAIM_CONFLICT = "recipient_claim_conflict"
    FOOD_SAFETY_THRESHOLD_BREACH = "food_safety_threshold_breach"
    INPUT_VALIDATION_FAILURE = "input_validation_failure"


class Coordinates(BaseModel):
    """Geographic coordinates for distance and routing calculations."""

    model_config = ConfigDict(frozen=True)

    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)


class Donation(BaseModel):
    """Surplus food donation submitted by a participating donor."""

    model_config = ConfigDict(frozen=True)

    donation_id: str = Field(..., min_length=1, max_length=128)
    donor_id: str = Field(..., min_length=1, max_length=128)
    donor_name: str = Field(..., min_length=1, max_length=MAX_TEXT_FIELD_LENGTH)
    donor_phone: str = Field(..., min_length=7, max_length=32)
    donor_address: str = Field(..., min_length=1, max_length=MAX_TEXT_FIELD_LENGTH)
    donor_coordinates: Coordinates
    food_category: FoodCategory
    quantity_kg: float = Field(..., gt=0.0, le=5000.0)
    ready_by: datetime
    perishability_hours: float = Field(..., gt=0.0, le=168.0)
    status: DonationStatus = DonationStatus.REPORTED
    service_region: str = Field(..., min_length=1, max_length=64)
    matched_recipient_id: str | None = None
    assigned_volunteer_id: str | None = None
    escalation_reason: EscalationReason | None = None
    date_status: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("donor_phone")
    @classmethod
    def validate_phone_number(cls, value: str) -> str:
        """Validate phone string has appropriate length and character structure."""
        cleaned = re.sub(r"[\s\-\(\)\.]", "", value)
        if not E164_PHONE_REGEX.match(cleaned):
            raise ValueError(f"Invalid phone number format: {value}")
        return cleaned

    @field_validator("ready_by")
    @classmethod
    def validate_ready_by_future(cls, value: datetime) -> datetime:
        """Verify ready_by timestamp is in the future at model creation."""
        now = datetime.now(timezone.utc)
        target = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if target <= now:
            raise ValueError("ready_by timestamp must be in the future")
        return target


class RecipientStatus(str, Enum):
    """Operational status of a recipient organization."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class VolunteerStatus(str, Enum):
    """Availability status of a transit volunteer."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class Recipient(BaseModel):
    """Non-profit or community recipient receiving surplus food."""

    model_config = ConfigDict(frozen=True)

    recipient_id: str = Field(..., min_length=1, max_length=128)
    organization_name: str = Field(
        ..., min_length=1, max_length=MAX_TEXT_FIELD_LENGTH
    )
    contact_name: str = Field(..., min_length=1, max_length=MAX_TEXT_FIELD_LENGTH)
    contact_phone: str = Field(..., min_length=7, max_length=32)
    address: str = Field(..., min_length=1, max_length=MAX_TEXT_FIELD_LENGTH)
    coordinates: Coordinates
    capacity_kg_remaining: float = Field(..., ge=0.0, le=10000.0)
    dietary_requirements: list[str] = Field(default_factory=list)
    dietary_exclusions: list[str] = Field(default_factory=list)
    status: RecipientStatus = RecipientStatus.ACTIVE
    service_region: str = Field(..., min_length=1, max_length=64)
    last_checkin_time: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def is_active(self) -> bool:
        """Boolean indicator of recipient active availability."""
        return self.status == RecipientStatus.ACTIVE

    @field_validator("contact_phone")
    @classmethod
    def validate_contact_phone(cls, value: str) -> str:
        """Validate contact phone format."""
        cleaned = re.sub(r"[\s\-\(\)\.]", "", value)
        if not E164_PHONE_REGEX.match(cleaned):
            raise ValueError(f"Invalid contact phone format: {value}")
        return cleaned


class Volunteer(BaseModel):
    """Pre-vetted volunteer assisting with local food transit."""

    model_config = ConfigDict(frozen=True)

    volunteer_id: str = Field(..., min_length=1, max_length=128)
    volunteer_name: str = Field(..., min_length=1, max_length=MAX_TEXT_FIELD_LENGTH)
    phone: str = Field(..., min_length=7, max_length=32)
    address: str = Field(..., min_length=1, max_length=MAX_TEXT_FIELD_LENGTH)
    coordinates: Coordinates
    status: VolunteerStatus = VolunteerStatus.AVAILABLE
    max_capacity_kg: float = Field(..., gt=0.0, le=2000.0)
    vehicle_type: str = Field(..., min_length=1, max_length=32)
    service_region: str = Field(..., min_length=1, max_length=64)

    @property
    def is_available(self) -> bool:
        """Boolean indicator of volunteer transit availability."""
        return self.status == VolunteerStatus.AVAILABLE

    @field_validator("phone")
    @classmethod
    def validate_volunteer_phone(cls, value: str) -> str:
        """Validate volunteer phone format."""
        cleaned = re.sub(r"[\s\-\(\)\.]", "", value)
        if not E164_PHONE_REGEX.match(cleaned):
            raise ValueError(f"Invalid volunteer phone format: {value}")
        return cleaned


class AuditEvent(BaseModel):
    """Immutable audit trail record for state transitions and agent decisions."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(..., min_length=1, max_length=128)
    donation_id: str = Field(..., min_length=1, max_length=128)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    action: str = Field(..., min_length=1, max_length=64)
    actor: str = Field(..., min_length=1, max_length=128)
    idempotency_key: str = Field(..., min_length=1, max_length=256)
    details: dict[str, Any] = Field(default_factory=dict)


class UrgencyLevel(str, Enum):
    """Perishability and delivery urgency tiers."""

    STANDARD = "standard"
    HIGH = "high"
    CRITICAL = "critical"


class DonationClassification(BaseModel):
    """Calculated classification and urgency assessment for a donation."""

    model_config = ConfigDict(frozen=True)

    food_category: FoodCategory
    urgency_level: UrgencyLevel
    shelf_life_remaining_hours: float = Field(..., ge=0.0)
    is_safety_threshold_breached: bool


class MatchCandidate(BaseModel):
    """Ranked recipient candidate evaluated by the matching algorithm."""

    model_config = ConfigDict(frozen=True)

    recipient_id: str = Field(..., min_length=1, max_length=128)
    recipient_name: str = Field(..., min_length=1, max_length=MAX_TEXT_FIELD_LENGTH)
    score: float = Field(..., ge=0.0, le=1.0)
    distance_km: float = Field(..., ge=0.0)
    capacity_match_kg: float = Field(..., ge=0.0)
    dietary_fit: bool
    reason: str = Field(..., min_length=1, max_length=MAX_TEXT_FIELD_LENGTH)


class MatchResult(BaseModel):
    """Complete ranked output of the matching algorithm for a donation."""

    model_config = ConfigDict(frozen=True)

    donation_id: str = Field(..., min_length=1, max_length=128)
    ranked_candidates: list[MatchCandidate] = Field(default_factory=list)
    best_match: MatchCandidate | None = None
    rejection_reason: str | None = None


class VolunteerAssignment(BaseModel):
    """Transit volunteer assignment record for moving surplus food."""

    model_config = ConfigDict(frozen=True)

    assignment_id: str = Field(..., min_length=1, max_length=128)
    donation_id: str = Field(..., min_length=1, max_length=128)
    volunteer_id: str = Field(..., min_length=1, max_length=128)
    recipient_id: str = Field(..., min_length=1, max_length=128)
    status: str = Field(default="assigned", max_length=32)
    assigned_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class NotificationRecipientType(str, Enum):
    """Target entity category for transactional notifications."""

    DONOR = "donor"
    RECIPIENT = "recipient"
    VOLUNTEER = "volunteer"
    COORDINATOR = "coordinator"


class NotificationMessage(BaseModel):
    """Sanitized, template-rendered notification payload."""

    model_config = ConfigDict(frozen=True)

    recipient_type: NotificationRecipientType
    destination: str = Field(..., min_length=1, max_length=256)
    template_id: str = Field(..., min_length=1, max_length=64)
    rendered_body: str = Field(..., min_length=1, max_length=1000)
    correlation_id: str = Field(..., min_length=1, max_length=128)


class EscalationTicket(BaseModel):
    """Explicit human-coordinator escalation request record."""

    model_config = ConfigDict(frozen=True)

    ticket_id: str = Field(..., min_length=1, max_length=128)
    donation_id: str = Field(..., min_length=1, max_length=128)
    reason: EscalationReason
    details: dict[str, Any] = Field(default_factory=dict)
    escalated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class InfrastructureConsistencyError(RuntimeError):
    """Raised when atomic compensation fails, requiring ops investigation."""


class DonationStateConflictError(Exception):
    """Raised when an operation encounters an unexpected donation status or state."""


class GuardrailViolationError(RuntimeError):
    """Raised when an action violates an operational safety guardrail."""


class PipelineStep(str, Enum):
    """Execution stages within the autonomous donation coordination pipeline."""

    INTAKE = "intake"
    CLASSIFY = "classify"
    FETCH_CAPACITY = "fetch_capacity"
    MATCH = "match"
    CLAIM_RECIPIENT = "claim_recipient"
    ASSIGN_VOLUNTEER = "assign_volunteer"
    DISPATCH_NOTIFICATIONS = "dispatch_notifications"
    COMPLETE = "complete"
    ESCALATED = "escalated"


class OrchestrationResult(BaseModel):
    """Immutable result payload produced by the Strands Agent Orchestrator."""

    model_config = ConfigDict(frozen=True)

    donation_id: str = Field(..., min_length=1, max_length=128)
    status: DonationStatus
    classification: DonationClassification | None = None
    matched_recipient_id: str | None = None
    assigned_volunteer_id: str | None = None
    escalation_ticket: EscalationTicket | None = None
    steps_completed: list[PipelineStep] = Field(default_factory=list)
    is_dry_run: bool = False
    correlation_id: str = Field(..., min_length=1, max_length=128)


class SessionContext(BaseModel):
    """Day-scoped operational context and metrics for an active service region."""

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(..., min_length=1, max_length=128)
    service_region: str = Field(..., min_length=1, max_length=64)
    session_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    recipients_near_capacity: list[str] = Field(default_factory=list)
    recent_volunteer_assignments: dict[str, int] = Field(default_factory=dict)
    total_donations_processed: int = Field(default=0, ge=0)
    total_kg_routed: float = Field(default=0.0, ge=0.0)
    active_escalations_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_epoch: int = Field(..., gt=0)


class RunningSummary(BaseModel):
    """Authoritative summary of routed donations and community impact for reporting."""

    model_config = ConfigDict(frozen=True)

    service_region: str = Field(..., min_length=1, max_length=64)
    date_str: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    total_kg_routed: float = Field(default=0.0, ge=0.0)
    meals_equivalent: int = Field(default=0, ge=0)
    organizations_served: int = Field(default=0, ge=0)
    donations_count: int = Field(default=0, ge=0)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryEntry(BaseModel):
    """Long-term entity pattern or operational insight with strict PII scrubbing."""

    model_config = ConfigDict(frozen=True)

    memory_id: str = Field(..., min_length=1, max_length=128)
    entity_type: str = Field(..., pattern=r"^(donor|recipient)$")
    entity_id: str = Field(..., min_length=1, max_length=128)
    pattern_type: str = Field(..., min_length=1, max_length=64)
    insights: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_epoch: int = Field(..., gt=0)


class AgentCoreRuntimeEvent(BaseModel):
    """Standard AWS Bedrock AgentCore Runtime invocation event."""

    model_config = ConfigDict(extra="ignore")

    message_version: str = Field(default="1.0", alias="messageVersion")
    agent: dict[str, Any] = Field(default_factory=dict)
    action_group: str = Field(default="", alias="actionGroup")
    api_path: str = Field(default="", alias="apiPath")
    http_method: str = Field(default="POST", alias="httpMethod")
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    request_body: dict[str, Any] = Field(default_factory=dict, alias="requestBody")
    session_id: str = Field(default="", alias="sessionId")
    session_attributes: dict[str, str] = Field(
        default_factory=dict, alias="sessionAttributes"
    )
    prompt_session_attributes: dict[str, str] = Field(
        default_factory=dict, alias="promptSessionAttributes"
    )


class AgentCoreRuntimeResponse(BaseModel):
    """Standard AWS Bedrock AgentCore Runtime action group response."""

    model_config = ConfigDict(extra="ignore")

    message_version: str = Field(default="1.0", alias="messageVersion")
    response: dict[str, Any]


