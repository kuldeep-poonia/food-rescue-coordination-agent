"""Pure multi-factor recipient matching algorithm with transparent scoring."""

from config import MAX_MATCH_DISTANCE_KM
from models import (
    Donation,
    DonationClassification,
    MatchCandidate,
    MatchResult,
    Recipient,
    UrgencyLevel,
)
from tools.classify_donation import classify_donation
from tools.distance_calculator import DistanceCalculator, GeodesicDistanceCalculator
from tools.logging_utils import get_structured_logger

LOGGER = get_structured_logger(__name__)

# Scoring weights normalized to 1.0
WEIGHT_DISTANCE: float = 0.35
WEIGHT_URGENCY: float = 0.25
WEIGHT_DIETARY: float = 0.25
WEIGHT_CAPACITY: float = 0.15


def find_best_match(
    donation: Donation,
    candidates: list[Recipient],
    classification: DonationClassification | None = None,
    distance_calculator: DistanceCalculator | None = None,
) -> MatchResult:
    """Evaluate and rank recipient organizations for a surplus food donation.

    Enforces hard constraints (capacity, dietary exclusions, maximum distance radius)
    and applies a multi-factor scoring function:
    - Proximity / Distance (35%)
    - Expiry Urgency Fit (25%)
    - Dietary Alignment (25%)
    - Capacity Utilization (15%)

    Args:
        donation: Validated surplus food Donation model.
        candidates: List of active Recipient models in the service region.
        classification: Optional pre-computed DonationClassification.
        distance_calculator: Optional distance provider (defaults to Geodesic).

    Returns:
        MatchResult with ranked candidates, top match, and diagnostic reasoning.
    """
    calc = distance_calculator or GeodesicDistanceCalculator()
    clf = classification or classify_donation(donation)

    LOGGER.info(
        "Evaluating %d recipient candidates for donation %s (%s, %.1fkg)",
        len(candidates),
        donation.donation_id,
        donation.food_category.value,
        donation.quantity_kg,
    )

    qualified_candidates: list[MatchCandidate] = []
    disqualification_counts: dict[str, int] = {
        "capacity": 0,
        "dietary_exclusion": 0,
        "distance": 0,
        "inactive": 0,
    }

    food_cat = donation.food_category.value.lower()

    for recipient in candidates:
        if not recipient.is_active:
            disqualification_counts["inactive"] += 1
            continue

        # Hard constraint 1: Remaining capacity
        if recipient.capacity_kg_remaining < donation.quantity_kg:
            disqualification_counts["capacity"] += 1
            continue

        # Hard constraint 2: Dietary exclusions
        exclusions = [e.strip().lower() for e in recipient.dietary_exclusions]
        if food_cat in exclusions:
            disqualification_counts["dietary_exclusion"] += 1
            continue

        # Hard constraint 3: Distance boundary
        dist_km = calc.calculate_distance_km(
            donation.donor_coordinates, recipient.coordinates
        )
        if dist_km > MAX_MATCH_DISTANCE_KM:
            disqualification_counts["distance"] += 1
            continue

        # Multi-factor scoring calculation
        # 1. Distance score (linear decay to MAX_MATCH_DISTANCE_KM)
        distance_score = max(0.0, 1.0 - (dist_km / MAX_MATCH_DISTANCE_KM))

        # 2. Urgency fit score
        if clf.urgency_level == UrgencyLevel.CRITICAL:
            # Critical donations heavily reward closest destinations
            urgency_score = max(0.0, 1.0 - (dist_km / (MAX_MATCH_DISTANCE_KM * 0.6)))
        elif clf.urgency_level == UrgencyLevel.HIGH:
            urgency_score = max(0.0, 1.0 - (dist_km / (MAX_MATCH_DISTANCE_KM * 0.8)))
        else:
            urgency_score = 1.0

        # 3. Dietary requirements score
        requirements = [r.strip().lower() for r in recipient.dietary_requirements]
        if food_cat in requirements:
            dietary_score = 1.0
            dietary_fit = True
        else:
            dietary_score = 0.6  # Neutral fit: not requested, but not excluded
            dietary_fit = False

        # 4. Capacity efficiency score
        utilization = donation.quantity_kg / max(
            recipient.capacity_kg_remaining, 1.0
        )
        capacity_score = max(0.2, min(1.0, utilization * 1.5))

        total_score = round(
            WEIGHT_DISTANCE * distance_score
            + WEIGHT_URGENCY * urgency_score
            + WEIGHT_DIETARY * dietary_score
            + WEIGHT_CAPACITY * capacity_score,
            4,
        )

        dietary_str = (
            "matches dietary priority" if dietary_fit else "compatible dietary profile"
        )
        reason = (
            f"{recipient.organization_name}: {dist_km:.1f}km away, "
            f"{recipient.capacity_kg_remaining:.1f}kg capacity, {dietary_str}"
        )

        qualified_candidates.append(
            MatchCandidate(
                recipient_id=recipient.recipient_id,
                recipient_name=recipient.organization_name,
                score=total_score,
                distance_km=dist_km,
                capacity_match_kg=recipient.capacity_kg_remaining,
                dietary_fit=dietary_fit,
                reason=reason,
            )
        )

    if not qualified_candidates:
        rejection_reason = (
            f"No eligible recipients found for donation {donation.donation_id}: "
            f"evaluated {len(candidates)} candidates "
            f"(disqualified: {disqualification_counts['capacity']} capacity, "
            f"{disqualification_counts['dietary_exclusion']} dietary, "
            f"{disqualification_counts['distance']} >{MAX_MATCH_DISTANCE_KM}km range)."
        )
        LOGGER.warning(rejection_reason)
        return MatchResult(
            donation_id=donation.donation_id,
            ranked_candidates=[],
            best_match=None,
            rejection_reason=rejection_reason,
        )

    # Sort primarily by score descending, secondarily by distance ascending
    qualified_candidates.sort(key=lambda c: (-c.score, c.distance_km))
    best_candidate = qualified_candidates[0]

    LOGGER.info(
        "Top match for donation %s: %s (Score: %.2f, %.1fkm)",
        donation.donation_id,
        best_candidate.recipient_name,
        best_candidate.score,
        best_candidate.distance_km,
    )

    return MatchResult(
        donation_id=donation.donation_id,
        ranked_candidates=qualified_candidates,
        best_match=best_candidate,
        rejection_reason=None,
    )
