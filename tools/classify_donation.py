"""Donation classification tool calculating urgency windows and safety thresholds."""

from datetime import datetime, timedelta, timezone

from config import FOOD_SAFETY_MIN_SHELF_LIFE_MINUTES
from models import Donation, DonationClassification, UrgencyLevel
from tools.logging_utils import get_structured_logger

LOGGER = get_structured_logger(__name__)

# Urgency window threshold constants in seconds
CRITICAL_URGENCY_THRESHOLD_SECONDS: float = 2.0 * 3600.0  # < 2 hours
HIGH_URGENCY_THRESHOLD_SECONDS: float = 6.0 * 3600.0  # < 6 hours
FOOD_SAFETY_THRESHOLD_SECONDS: float = (
    float(FOOD_SAFETY_MIN_SHELF_LIFE_MINUTES) * 60.0
)  # < 60 minutes


def classify_donation(
    donation: Donation,
    current_time: datetime | None = None,
) -> DonationClassification:
    """Classify a surplus food donation and determine urgency and food safety status.

    Calculates the remaining consumable window from ready_by and perishability_hours,
    categorizes the urgency tier with second-precision boundaries, and identifies
    food-safety threshold breaches (<60 minutes remaining shelf life).

    Args:
        donation: Validated Donation model instance.
        current_time: Optional reference timestamp (defaults to UTC now).

    Returns:
        DonationClassification model with calculated urgency and safety indicators.
    """
    now = current_time or datetime.now(timezone.utc)
    if not now.tzinfo:
        now = now.replace(tzinfo=timezone.utc)

    ready_by_utc = (
        donation.ready_by
        if donation.ready_by.tzinfo
        else donation.ready_by.replace(tzinfo=timezone.utc)
    )

    expiry_time = ready_by_utc + timedelta(hours=donation.perishability_hours)
    remaining_seconds = (expiry_time - now).total_seconds()
    effective_seconds = max(0.0, remaining_seconds)
    shelf_life_hours = round(effective_seconds / 3600.0, 2)

    # Food safety threshold check (< 60 minutes)
    is_safety_breached = remaining_seconds < FOOD_SAFETY_THRESHOLD_SECONDS

    # Urgency level calculation with exact second boundaries
    if remaining_seconds < CRITICAL_URGENCY_THRESHOLD_SECONDS:
        urgency = UrgencyLevel.CRITICAL
    elif remaining_seconds < HIGH_URGENCY_THRESHOLD_SECONDS:
        urgency = UrgencyLevel.HIGH
    else:
        urgency = UrgencyLevel.STANDARD

    LOGGER.info(
        "Classified donation %s as %s urgency with %.2f hours remaining",
        donation.donation_id,
        urgency.value,
        shelf_life_hours,
        extra={
            "details": {
                "donation_id": donation.donation_id,
                "food_category": donation.food_category.value,
                "urgency_level": urgency.value,
                "shelf_life_hours": shelf_life_hours,
                "is_safety_breached": is_safety_breached,
            }
        },
    )

    return DonationClassification(
        food_category=donation.food_category,
        urgency_level=urgency,
        shelf_life_remaining_hours=shelf_life_hours,
        is_safety_threshold_breached=is_safety_breached,
    )
