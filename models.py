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
    matched_recipient_id: str | None = None
    assigned_volunteer_id: str | None = None
    escalation_reason: EscalationReason | None = None
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

