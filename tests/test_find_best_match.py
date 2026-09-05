"""Adversarial and multi-factor ranking test suite for find_best_match."""

from datetime import datetime, timedelta, timezone

from models import Coordinates, Donation, FoodCategory, Recipient, RecipientStatus
from tools.find_best_match import find_best_match


def make_donation(
    quantity_kg: float = 30.0,
    category: FoodCategory = FoodCategory.PRODUCE,
) -> Donation:
    """Helper creating a standard test donation."""
    future = datetime.now(timezone.utc) + timedelta(hours=3)
    return Donation(
        donation_id="don-match-001",
        donor_id="donor-1",
        donor_name="Downtown Bakery",
        donor_phone="+12125550199",
        donor_address="100 Main St, New York, NY",
        donor_coordinates=Coordinates(latitude=40.7128, longitude=-74.0060),
        food_category=category,
        quantity_kg=quantity_kg,
        ready_by=future,
        perishability_hours=6.0,
        service_region="metro-core",
    )


def make_recipient(
    recipient_id: str,
    name: str,
    capacity_kg: float,
    latitude: float = 40.7150,
    longitude: float = -74.0080,
    dietary_reqs: list[str] | None = None,
    dietary_exclusions: list[str] | None = None,
    status: RecipientStatus = RecipientStatus.ACTIVE,
) -> Recipient:
    """Helper creating a test recipient organization."""
    return Recipient(
        recipient_id=recipient_id,
        organization_name=name,
        contact_name="Coordinator",
        contact_phone="+12125550188",
        address="200 Charity Blvd",
        coordinates=Coordinates(latitude=latitude, longitude=longitude),
        capacity_kg_remaining=capacity_kg,
        dietary_requirements=dietary_reqs or [],
        dietary_exclusions=dietary_exclusions or [],
        status=status,
        service_region="metro-core",
    )


def test_find_best_match_ranks_ideal_candidate_first() -> None:
    """Verify closest candidate with matching dietary priority scores highest."""
    don = make_donation(quantity_kg=20.0, category=FoodCategory.PRODUCE)

    # Candidate 1: 0.3km away, requested produce
    r1 = make_recipient(
        "rec-001",
        "Shelter Prime",
        capacity_kg=100.0,
        latitude=40.7140,
        longitude=-74.0070,
        dietary_reqs=["produce"],
    )

    # Candidate 2: 8km away, neutral dietary profile
    r2 = make_recipient(
        "rec-002",
        "Pantry Secondary",
        capacity_kg=150.0,
        latitude=40.7800,
        longitude=-73.9600,
        dietary_reqs=[],
    )

    result = find_best_match(don, [r1, r2])
    assert result.best_match is not None
    assert result.best_match.recipient_id == "rec-001"
    assert result.best_match.score > result.ranked_candidates[1].score
    assert "matches dietary priority" in result.best_match.reason


def test_find_best_match_disqualifies_exclusions_and_radius() -> None:
    """Verify hard disqualifications for dietary conflicts and distance > 25km."""
    don = make_donation(quantity_kg=25.0, category=FoodCategory.MEAT)

    # Excluded dietary candidate: very close, but excludes meat
    r_excluded = make_recipient(
        "rec-veg",
        "Vegetarian Kitchen",
        capacity_kg=200.0,
        latitude=40.7130,
        longitude=-74.0062,
        dietary_exclusions=["meat"],
    )

    # Far away candidate: > 25km (e.g. Philadelphia at ~130km)
    r_far = make_recipient(
        "rec-far",
        "Far Away Shelter",
        capacity_kg=200.0,
        latitude=39.9526,
        longitude=-75.1652,
    )

    # Eligible candidate: 3km away, accepts meat
    r_eligible = make_recipient(
        "rec-ok",
        "General Food Bank",
        capacity_kg=100.0,
        latitude=40.7300,
        longitude=-74.0000,
    )

    result = find_best_match(don, [r_excluded, r_far, r_eligible])
    assert result.best_match is not None
    assert result.best_match.recipient_id == "rec-ok"
    assert len(result.ranked_candidates) == 1


def test_adversarial_matching_zero_eligible_and_fifty_at_capacity() -> None:
    """Hardcore requirement: zero candidates & 50 full-capacity candidates.

    Must return clean 'no match' with diagnostic reason, never throw unhandled
    exceptions or construct invalid forced matches.
    """
    don = make_donation(quantity_kg=50.0, category=FoodCategory.PREPARED_MEALS)

    # Case 1: Zero eligible candidates provided
    res_empty = find_best_match(don, [])
    assert res_empty.best_match is None
    assert res_empty.ranked_candidates == []
    assert res_empty.rejection_reason is not None
    assert "evaluated 0 candidates" in res_empty.rejection_reason

    # Case 2: 50 active recipients, ALL at 0 remaining capacity
    recipients_full = [
        make_recipient(
            recipient_id=f"rec-full-{i:03d}",
            name=f"Full Shelter {i}",
            capacity_kg=0.0,  # 0 capacity remaining (needs 50.0)
            latitude=40.7128 + (i * 0.001),
            longitude=-74.0060,
        )
        for i in range(50)
    ]

    res_50_full = find_best_match(don, recipients_full)
    assert res_50_full.best_match is None
    assert res_50_full.ranked_candidates == []
    assert res_50_full.rejection_reason is not None
    assert "50 capacity" in res_50_full.rejection_reason
