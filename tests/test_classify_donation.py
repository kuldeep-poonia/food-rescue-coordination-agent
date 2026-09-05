"""Unit tests and exact second-precision boundary tests for classify_donation."""

from datetime import datetime, timedelta, timezone

from models import Coordinates, Donation, FoodCategory, UrgencyLevel
from tools.classify_donation import classify_donation


def make_test_donation(
    ready_by: datetime,
    perishability_hours: float,
    category: FoodCategory = FoodCategory.PRODUCE,
) -> Donation:
    """Helper to construct a valid donation with customized time parameters."""
    return Donation(
        donation_id="don-classify-test",
        donor_id="donor-001",
        donor_name="Green Grocer",
        donor_phone="+12125550199",
        donor_address="123 Market St",
        donor_coordinates=Coordinates(latitude=40.7128, longitude=-74.0060),
        food_category=category,
        quantity_kg=25.0,
        ready_by=ready_by,
        perishability_hours=perishability_hours,
        service_region="metro-core",
    )


def test_classify_donation_food_safety_threshold_boundary() -> None:
    """Verify exact second-precision boundary for food safety threshold breach.

    Threshold: < 60 minutes (3600 seconds).
    At 3599s -> is_safety_threshold_breached is True.
    At 3600s -> is_safety_threshold_breached is False.
    """
    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
    future_ready = now + timedelta(minutes=10)

    # Scenario 1: Exactly 3599 seconds remaining from 'now'
    # ready_by + perishability = now + 3599s
    # perishability_hours = (now + 3599s - future_ready) in hours
    diff_seconds_breach = 3599
    expiry_breach = now + timedelta(seconds=diff_seconds_breach)
    perish_hours_breach = (expiry_breach - future_ready).total_seconds() / 3600.0

    don_breach = make_test_donation(future_ready, perish_hours_breach)
    res_breach = classify_donation(don_breach, current_time=now)
    assert res_breach.is_safety_threshold_breached is True

    # Scenario 2: Exactly 3600 seconds remaining from 'now'
    diff_seconds_safe = 3600
    expiry_safe = now + timedelta(seconds=diff_seconds_safe)
    perish_hours_safe = (expiry_safe - future_ready).total_seconds() / 3600.0

    don_safe = make_test_donation(future_ready, perish_hours_safe)
    res_safe = classify_donation(don_safe, current_time=now)
    assert res_safe.is_safety_threshold_breached is False


def test_classify_donation_urgency_tier_boundaries() -> None:
    """Verify boundaries between CRITICAL (<2h), HIGH (2h-6h), and STANDARD (>=6h)."""
    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
    future_ready = now + timedelta(minutes=10)

    # 7199 seconds (1h 59m 59s) -> CRITICAL
    expiry_crit = now + timedelta(seconds=7199)
    perish_crit = (expiry_crit - future_ready).total_seconds() / 3600.0
    don_crit = make_test_donation(future_ready, perish_crit)
    res_crit = classify_donation(don_crit, current_time=now)
    assert res_crit.urgency_level == UrgencyLevel.CRITICAL

    # 7200 seconds (exactly 2h 00m 00s) -> HIGH
    expiry_high_min = now + timedelta(seconds=7200)
    perish_high_min = (expiry_high_min - future_ready).total_seconds() / 3600.0
    don_high_min = make_test_donation(future_ready, perish_high_min)
    res_high_min = classify_donation(don_high_min, current_time=now)
    assert res_high_min.urgency_level == UrgencyLevel.HIGH

    # 21599 seconds (5h 59m 59s) -> HIGH
    expiry_high_max = now + timedelta(seconds=21599)
    perish_high_max = (expiry_high_max - future_ready).total_seconds() / 3600.0
    don_high_max = make_test_donation(future_ready, perish_high_max)
    res_high_max = classify_donation(don_high_max, current_time=now)
    assert res_high_max.urgency_level == UrgencyLevel.HIGH

    # 21600 seconds (exactly 6h 00m 00s) -> STANDARD
    expiry_std = now + timedelta(seconds=21600)
    perish_std = (expiry_std - future_ready).total_seconds() / 3600.0
    don_std = make_test_donation(future_ready, perish_std)
    res_std = classify_donation(don_std, current_time=now)
    assert res_std.urgency_level == UrgencyLevel.STANDARD
