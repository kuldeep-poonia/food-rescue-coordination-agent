"""Hardcore test suite for Phase 6 Matching Intelligence.

Covers:
1. Exact food-safety threshold boundaries (3599s, 3600s, 3601s) with fixed reference.
2. Scoring weight normalization (sum == 1.0) and named factor constants.
3. Deterministic tie-breaking:
   - Equal score, different distance -> lower distance wins
   - Equal score, equal distance -> recipient_id wins
   - Equal volunteer distance -> volunteer_id wins
4. 100-run scoring stability with randomly shuffled candidate sequences.
5. LocationServiceUnavailableError propagation from find_best_match and dispatch.
6. Degraded dependency fail-safe escalation in orchestrator.
7. Strict allow_fallback=False production default vs explicitly controlled fallback.
8. Location data privacy verification across logs, exceptions, and audit trails.
"""

import logging
import random
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from botocore.exceptions import ClientError

from agent.orchestrator import StrandsOrchestrator
from audit_repo import AuditRepository
from config import (
    AppConfig,
)
from donations_repo import DonationsRepository
from models import (
    Coordinates,
    Donation,
    DonationStatus,
    EscalationReason,
    FoodCategory,
    Recipient,
    Volunteer,
)
from recipients_repo import RecipientsRepository
from tools.assign_volunteer import assign_volunteer
from tools.classify_donation import classify_donation
from tools.distance_calculator import (
    AmazonLocationDistanceCalculator,
    LocationServiceUnavailableError,
)
from tools.find_best_match import (
    CAPACITY_MAX_SCORE,
    CAPACITY_MIN_SCORE,
    CAPACITY_UTILIZATION_MULTIPLIER,
    CRITICAL_URGENCY_DISTANCE_FACTOR,
    DIETARY_COMPATIBLE_SCORE,
    DIETARY_PRIORITY_SCORE,
    HIGH_URGENCY_DISTANCE_FACTOR,
    STANDARD_URGENCY_SCORE,
    WEIGHT_CAPACITY,
    WEIGHT_DIETARY,
    WEIGHT_DISTANCE,
    WEIGHT_URGENCY,
    find_best_match,
)
from volunteers_repo import VolunteersRepository


# ------------------------------------------------------------------------------
# Test Fixtures & Shared Helpers
# ------------------------------------------------------------------------------
def create_test_config() -> AppConfig:
    """Create test configuration with mock resource identifiers."""
    return AppConfig(
        aws_region="us-east-1",
        donations_table_name="frca-donations-test",
        recipients_table_name="frca-recipients-test",
        volunteers_table_name="frca-volunteers-test",
        matches_audit_table_name="frca-matches-audit-test",
        sessions_memory_table_name="frca-sessions-test",
        notification_topic_arn="arn:aws:sns:us-east-1:123456789012:notif",
        coordinator_escalation_topic_arn="arn:aws:sns:us-east-1:123456789012:escl",
        coordinator_dlq_url="https://sqs.us-east-1.amazonaws.com/123456789012/dlq",
        location_place_index_name="index",
        route_calculator_name="calc",
        bedrock_agent_id="agent",
        bedrock_agent_alias_id="alias",
    )


def build_sample_donation(
    donation_id: str = "don-match-001",
    quantity_kg: float = 20.0,
    ready_by: datetime | None = None,
    perishability_hours: float = 4.0,
    food_category: FoodCategory = FoodCategory.PREPARED_MEALS,
    status: DonationStatus = DonationStatus.REPORTED,
) -> Donation:
    """Construct a clean valid Donation model for testing."""
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    return Donation(
        donation_id=donation_id,
        donor_id="donor-kitchen-01",
        donor_name="Community Kitchen",
        donor_phone="+12125550199",
        donor_address="100 Main St, New York, NY",
        donor_coordinates=Coordinates(latitude=40.7128, longitude=-74.0060),
        food_category=food_category,
        quantity_kg=quantity_kg,
        ready_by=ready_by or now,
        perishability_hours=perishability_hours,
        service_region="metro-core",
        status=status,
    )


def build_sample_recipient(
    recipient_id: str,
    org_name: str,
    coords: Coordinates,
    capacity_kg: float = 100.0,
    dietary_requirements: list[str] | None = None,
    dietary_exclusions: list[str] | None = None,
) -> Recipient:
    """Construct a clean valid Recipient model for testing."""
    return Recipient(
        recipient_id=recipient_id,
        organization_name=org_name,
        contact_name="Coordinator",
        contact_phone="+12125550188",
        contact_email="intake@shelter.org",
        address="200 Shelter Way, New York, NY",
        coordinates=coords,
        capacity_kg_total=200.0,
        capacity_kg_remaining=capacity_kg,
        service_region="metro-core",
        dietary_requirements=dietary_requirements or ["prepared meals"],
        dietary_exclusions=dietary_exclusions or [],
        is_active=True,
    )


# ------------------------------------------------------------------------------
# 1. Food-Safety Threshold Boundary Test (3599s, 3600s, 3601s)
# ------------------------------------------------------------------------------
def test_food_safety_threshold_exact_boundaries() -> None:
    """Assert food-safety boundary behaves exactly at 3599s, 3600s, and 3601s.

    Threshold: FOOD_SAFETY_MIN_SHELF_LIFE_MINUTES = 60 minutes = 3600 seconds.
    - 3599s (59m 59s): Breached -> Matcher returns rejection; orchestrator escalates.
    - 3600s (60m 00s): Safe (boundary) -> Allowed; match succeeds.
    - 3601s (60m 01s): Safe -> Allowed; match succeeds.
    """
    fixed_now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    candidates = [
        build_sample_recipient(
            "rec-01",
            "Downtown Shelter",
            Coordinates(latitude=40.7150, longitude=-74.0040),
        )
    ]

    # --- Scenario A: 3599 seconds remaining (Boundary - 1s -> MUST REJECT) ---
    ready_by_a = fixed_now
    perishability_a = 3599.0 / 3600.0  # exactly 3599s from fixed_now
    donation_3599 = build_sample_donation(
        donation_id="don-3599",
        ready_by=ready_by_a,
        perishability_hours=perishability_a,
    )

    clf_3599 = classify_donation(donation_3599, current_time=fixed_now)
    assert clf_3599.is_safety_threshold_breached is True

    match_3599 = find_best_match(
        donation=donation_3599,
        candidates=candidates,
        classification=clf_3599,
        current_time=fixed_now,
    )
    assert match_3599.best_match is None
    assert match_3599.rejection_reason is not None
    assert "Food safety threshold breached" in match_3599.rejection_reason

    # Also test find_best_match without passing clf (calls classify_donation internally)
    match_3599_auto = find_best_match(
        donation=donation_3599,
        candidates=candidates,
        current_time=fixed_now,
    )
    assert match_3599_auto.best_match is None
    assert match_3599_auto.rejection_reason is not None
    assert "Food safety threshold breached" in match_3599_auto.rejection_reason

    # --- Scenario B: 3600 seconds remaining (Exact Threshold -> MUST ALLOW) ---
    perishability_b = 3600.0 / 3600.0  # exactly 1.0 hour (3600s)
    donation_3600 = build_sample_donation(
        donation_id="don-3600",
        ready_by=fixed_now,
        perishability_hours=perishability_b,
    )

    clf_3600 = classify_donation(donation_3600, current_time=fixed_now)
    assert clf_3600.is_safety_threshold_breached is False

    match_3600 = find_best_match(
        donation=donation_3600,
        candidates=candidates,
        classification=clf_3600,
        current_time=fixed_now,
    )
    assert match_3600.best_match is not None
    assert match_3600.best_match.recipient_id == "rec-01"
    assert match_3600.rejection_reason is None

    # --- Scenario C: 3601 seconds remaining (Threshold + 1s -> MUST ALLOW) ---
    perishability_c = 3601.0 / 3600.0
    donation_3601 = build_sample_donation(
        donation_id="don-3601",
        ready_by=fixed_now,
        perishability_hours=perishability_c,
    )

    clf_3601 = classify_donation(donation_3601, current_time=fixed_now)
    assert clf_3601.is_safety_threshold_breached is False

    match_3601 = find_best_match(
        donation=donation_3601,
        candidates=candidates,
        classification=clf_3601,
        current_time=fixed_now,
    )
    assert match_3601.best_match is not None
    assert match_3601.best_match.recipient_id == "rec-01"
    assert match_3601.rejection_reason is None


# ------------------------------------------------------------------------------
# 2. Scoring Weights Normalization & Sub-Factor Constants
# ------------------------------------------------------------------------------
def test_scoring_weights_and_constants_integrity() -> None:
    """Verify scoring weights sum exactly to 1.0 and all sub-factors are documented."""
    # Strict sum verification
    weight_sum = WEIGHT_DISTANCE + WEIGHT_URGENCY + WEIGHT_DIETARY + WEIGHT_CAPACITY
    assert round(weight_sum, 6) == 1.0
    assert WEIGHT_DISTANCE == 0.35
    assert WEIGHT_URGENCY == 0.25
    assert WEIGHT_DIETARY == 0.25
    assert WEIGHT_CAPACITY == 0.15

    # Sub-factor constant verification
    assert CRITICAL_URGENCY_DISTANCE_FACTOR == 0.6
    assert HIGH_URGENCY_DISTANCE_FACTOR == 0.8
    assert STANDARD_URGENCY_SCORE == 1.0
    assert DIETARY_PRIORITY_SCORE == 1.0
    assert DIETARY_COMPATIBLE_SCORE == 0.6
    assert CAPACITY_MIN_SCORE == 0.2
    assert CAPACITY_MAX_SCORE == 1.0
    assert CAPACITY_UTILIZATION_MULTIPLIER == 1.5


# ------------------------------------------------------------------------------
# 3. Deterministic Tie-Breaking
# ------------------------------------------------------------------------------
def test_deterministic_tie_breaking_recipient_and_volunteer() -> None:
    """Verify deterministic tie-breaking across matching and volunteer dispatch.

    - Equal score + different distance -> lower distance wins
    - Equal score + equal distance -> recipient_id wins (lexicographical)
    - Equal volunteer distance -> volunteer_id wins (lexicographical)
    """
    fixed_now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    donation = build_sample_donation(
        donation_id="don-tie-01",
        quantity_kg=20.0,
        ready_by=fixed_now,
        perishability_hours=10.0,
    )
    donor_coords = donation.donor_coordinates

    # Case A: Equal score + different distance -> lower distance wins
    # We craft two candidates whose total scores are identical (0.7670) but
    # distances differ (2.0 km vs 9.14 km).
    # To test that lower distance takes precedence over recipient_id, we name the
    # closer candidate "rec-z-close" and the farther candidate "rec-a-far".
    # Even though "rec-a" < "rec-z", "rec-z-close" must win because distance
    # takes precedence.
    rec_close = build_sample_recipient(
        "rec-z-close",
        "Close Shelter",
        Coordinates(
            latitude=donor_coords.latitude + 0.01, longitude=donor_coords.longitude
        ),
        capacity_kg=100.0,
        dietary_requirements=["groceries"],  # incompatible with prepared meals -> 0.6
    )
    rec_far = build_sample_recipient(
        "rec-a-far",
        "Far Shelter",
        Coordinates(
            latitude=donor_coords.latitude + 0.05, longitude=donor_coords.longitude
        ),
        capacity_kg=100.0,
        dietary_requirements=["prepared_meals"],  # matches prepared_meals -> 1.0
    )

    class CustomEqualScoreDistanceCalculator:
        def calculate_distance_km(
            self, _origin: Coordinates, dest: Coordinates
        ) -> float:
            if abs(dest.latitude - (donor_coords.latitude + 0.01)) < 1e-4:
                return 2.0
            return 9.14

    mock_equal_score_calc = CustomEqualScoreDistanceCalculator()

    # Test with [rec_far, rec_close]
    match_res_a1 = find_best_match(
        donation,
        [rec_far, rec_close],
        distance_calculator=mock_equal_score_calc,
        current_time=fixed_now,
    )
    assert match_res_a1.best_match is not None
    assert (
        match_res_a1.ranked_candidates[0].score
        == match_res_a1.ranked_candidates[1].score
    )
    assert match_res_a1.ranked_candidates[0].score == 0.7670
    assert (
        match_res_a1.ranked_candidates[0].distance_km
        < match_res_a1.ranked_candidates[1].distance_km
    )
    assert match_res_a1.best_match.recipient_id == "rec-z-close"

    # Test reverse input order [rec_close, rec_far]
    match_res_a2 = find_best_match(
        donation,
        [rec_close, rec_far],
        distance_calculator=mock_equal_score_calc,
        current_time=fixed_now,
    )
    assert match_res_a2.best_match is not None
    assert match_res_a2.best_match.recipient_id == "rec-z-close"

    # Case B: Equal score AND equal distance -> recipient_id wins (lexicographical)
    rec_alpha = build_sample_recipient(
        "rec-alpha",
        "Alpha Shelter",
        Coordinates(
            latitude=donor_coords.latitude + 0.02, longitude=donor_coords.longitude
        ),
        capacity_kg=100.0,
        dietary_requirements=["prepared_meals"],
    )
    rec_beta = build_sample_recipient(
        "rec-beta",
        "Beta Shelter",
        Coordinates(
            latitude=donor_coords.latitude + 0.02, longitude=donor_coords.longitude
        ),
        capacity_kg=100.0,
        dietary_requirements=["prepared_meals"],
    )

    # Regardless of candidate input order [beta, alpha], alpha must win
    match_1 = find_best_match(donation, [rec_beta, rec_alpha], current_time=fixed_now)
    assert match_1.best_match is not None
    assert match_1.ranked_candidates[0].score == match_1.ranked_candidates[1].score
    assert (
        match_1.ranked_candidates[0].distance_km
        == match_1.ranked_candidates[1].distance_km
    )
    assert match_1.best_match.recipient_id == "rec-alpha"

    match_2 = find_best_match(donation, [rec_alpha, rec_beta], current_time=fixed_now)
    assert match_2.best_match is not None
    assert match_2.ranked_candidates[0].score == match_2.ranked_candidates[1].score
    assert (
        match_2.ranked_candidates[0].distance_km
        == match_2.ranked_candidates[1].distance_km
    )
    assert match_2.best_match.recipient_id == "rec-alpha"

    # Case C: Equal volunteer distance -> volunteer_id wins
    mock_d_repo = mock.MagicMock()
    mock_v_repo = mock.MagicMock()
    mock_a_repo = mock.MagicMock()

    mock_d_repo.get_donation.return_value = Donation(
        donation_id="don-vol-tie",
        donor_id="donor-1",
        donor_name="Kitchen",
        donor_phone="+12125550199",
        donor_address="100 Main St",
        donor_coordinates=donor_coords,
        food_category=FoodCategory.BAKERY,
        quantity_kg=20.0,
        ready_by=fixed_now + timedelta(hours=2),
        perishability_hours=4.0,
        service_region="metro-core",
        status=DonationStatus.MATCHED,
        matched_recipient_id="rec-01",
    )
    mock_d_repo.assign_volunteer.return_value = True

    vol_b = Volunteer(
        volunteer_id="vol-beta",
        volunteer_name="Beta Volunteer",
        phone="+12125550102",
        address="200 Volunteer Way",
        coordinates=Coordinates(
            latitude=donor_coords.latitude + 0.01, longitude=donor_coords.longitude
        ),
        max_capacity_kg=50.0,
        vehicle_type="car",
        is_available=True,
        service_region="metro-core",
    )
    vol_a = Volunteer(
        volunteer_id="vol-alpha",
        volunteer_name="Alpha Volunteer",
        phone="+12125550101",
        address="100 Volunteer Way",
        coordinates=Coordinates(
            latitude=donor_coords.latitude + 0.01, longitude=donor_coords.longitude
        ),
        max_capacity_kg=50.0,
        vehicle_type="car",
        is_available=True,
        service_region="metro-core",
    )

    # Input returned in reverse order [vol_b, vol_a]
    mock_v_repo.query_available_volunteers_by_region.return_value = [vol_b, vol_a]

    asgn = assign_volunteer(
        donation_id="don-vol-tie",
        service_region="metro-core",
        donations_repo=mock_d_repo,
        volunteers_repo=mock_v_repo,
        audit_repo=mock_a_repo,
    )
    assert asgn is not None
    # vol-alpha must be chosen first because "vol-alpha" < "vol-beta"
    assert asgn.volunteer_id == "vol-alpha"


# ------------------------------------------------------------------------------
# 4. Scoring Stability Test (100 Runs With Randomly Shuffled Candidates)
# ------------------------------------------------------------------------------
def test_scoring_stability_across_one_hundred_shuffled_runs() -> None:
    """Run matching 100 times with shuffled candidates and assert determinism."""
    fixed_now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    donation = build_sample_donation(
        donation_id="don-stability-100",
        quantity_kg=25.0,
        ready_by=fixed_now,
        perishability_hours=8.0,
    )

    # Generate 10 diverse candidates with subtle scoring variations
    base_coords = donation.donor_coordinates
    candidates: list[Recipient] = []
    for i in range(10):
        c_id = f"rec-{i:02d}"
        cand = build_sample_recipient(
            recipient_id=c_id,
            org_name=f"Shelter {i}",
            coords=Coordinates(
                latitude=base_coords.latitude + (0.01 * (i + 1)),
                longitude=base_coords.longitude + (0.01 * (i % 3)),
            ),
            capacity_kg=30.0 + (i * 10.0),
            dietary_requirements=(
                ["prepared meals"] if i % 2 == 0 else ["groceries"]
            ),
        )
        candidates.append(cand)

    # Baseline run
    baseline_result = find_best_match(
        donation=donation,
        candidates=candidates,
        current_time=fixed_now,
    )
    assert baseline_result.best_match is not None
    expected_top_id = baseline_result.best_match.recipient_id
    expected_ranked_ids = [c.recipient_id for c in baseline_result.ranked_candidates]

    # Run 100 iterations with shuffled candidate inputs
    rng = random.Random(42)  # seeded PRNG for reproducible test runs
    for iteration in range(100):
        shuffled = list(candidates)
        rng.shuffle(shuffled)

        res = find_best_match(
            donation=donation,
            candidates=shuffled,
            current_time=fixed_now,
        )
        assert res.best_match is not None, f"Failed on iteration {iteration}"
        assert res.best_match.recipient_id == expected_top_id, (
            f"Top match diverged on iteration {iteration}: "
            f"{res.best_match.recipient_id} != {expected_top_id}"
        )

        ranked_ids = [c.recipient_id for c in res.ranked_candidates]
        assert ranked_ids == expected_ranked_ids, (
            f"Ranking sequence diverged on iteration {iteration}"
        )


# ------------------------------------------------------------------------------
# 5. LocationServiceUnavailableError Propagation From Matching
# ------------------------------------------------------------------------------
def test_location_service_unavailable_error_propagates_from_find_best_match() -> None:
    """Verify find_best_match propagates LocationServiceUnavailableError directly."""
    fixed_now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    donation = build_sample_donation(
        donation_id="don-propagate-01",
        ready_by=fixed_now,
        perishability_hours=5.0,
    )
    candidates = [
        build_sample_recipient(
            "rec-01",
            "Downtown Shelter",
            Coordinates(latitude=40.72, longitude=-74.01),
        )
    ]

    # Mock calculator that raises LocationServiceUnavailableError
    mock_calc = mock.create_autospec(
        AmazonLocationDistanceCalculator, instance=True
    )
    mock_calc.calculate_distance_km.side_effect = LocationServiceUnavailableError(
        "Amazon Location Service route calculation failed: ClientError"
    )

    # Must raise directly, NOT catch and return synthetic MatchResult
    with pytest.raises(LocationServiceUnavailableError) as exc_info:
        find_best_match(
            donation=donation,
            candidates=candidates,
            distance_calculator=mock_calc,
            current_time=fixed_now,
        )

    assert "Amazon Location Service route calculation failed" in str(exc_info.value)


# ------------------------------------------------------------------------------
# 6. Degraded Dependency Fail-Safe Escalation in Orchestrator
# ------------------------------------------------------------------------------
def test_orchestrator_matching_degraded_dependency_escalation() -> None:
    """Verify orchestrator catches routing service errors and safely escalates."""
    mock_donations_table = mock.MagicMock()
    mock_recipients_table = mock.MagicMock()
    mock_volunteers_table = mock.MagicMock()
    mock_audit_table = mock.MagicMock()

    def table_router(name: str) -> mock.MagicMock:
        if "recipients" in name:
            return mock_recipients_table
        if "volunteers" in name:
            return mock_volunteers_table
        if "audit" in name:
            return mock_audit_table
        return mock_donations_table

    mock_resource = mock.MagicMock()
    mock_resource.Table.side_effect = table_router
    mock_sns = mock.MagicMock()

    config = create_test_config()
    d_repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)
    r_repo = RecipientsRepository(dynamodb_resource=mock_resource, config=config)
    v_repo = VolunteersRepository(dynamodb_resource=mock_resource, config=config)
    a_repo = AuditRepository(dynamodb_resource=mock_resource, config=config)

    # AmazonLocationDistanceCalculator with mock client that fails
    mock_loc_client = mock.MagicMock()
    mock_loc_client.calculate_route.side_effect = ClientError(
        {"Error": {"Code": "InternalServerException", "Message": "AWS outage"}},
        "CalculateRoute",
    )
    degraded_calc = AmazonLocationDistanceCalculator(
        location_client=mock_loc_client,
        calculator_name=config.route_calculator_name,
        allow_fallback=False,  # Strict production default
    )

    fixed_now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    donation = build_sample_donation(
        donation_id="don-degraded-01",
        ready_by=fixed_now + timedelta(hours=2),
        perishability_hours=6.0,
    )

    mock_donations_table.get_item.return_value = {"Item": donation.model_dump()}
    mock_donations_table.update_item.return_value = {}
    mock_recipients_table.query.return_value = {
        "Items": [
            build_sample_recipient(
                "rec-01",
                "Downtown Shelter",
                Coordinates(latitude=40.72, longitude=-74.01),
            ).model_dump()
        ]
    }
    mock_audit_table.query.return_value = {"Items": []}
    mock_audit_table.put_item.return_value = {}

    orchestrator = StrandsOrchestrator(
        donations_repo=d_repo,
        recipients_repo=r_repo,
        volunteers_repo=v_repo,
        audit_repo=a_repo,
        distance_calculator=degraded_calc,
        sns_client=mock_sns,
        config=config,
    )

    result = orchestrator.coordinate_donation(
        donation_id="don-degraded-01",
        correlation_id="trace-degraded-match",
    )

    assert result.status == DonationStatus.ESCALATED
    assert result.escalation_ticket is not None
    assert result.escalation_ticket.reason == EscalationReason.NO_MATCH_WITHIN_WINDOW
    summary_text = result.escalation_ticket.details["summary"]
    assert "Amazon Location Service error" in summary_text


def test_orchestrator_volunteer_dispatch_degraded_dependency_escalation() -> None:
    """Verify volunteer routing failure unwinds claimed recipient and escalates."""
    mock_donations_table = mock.MagicMock()
    mock_recipients_table = mock.MagicMock()
    mock_volunteers_table = mock.MagicMock()
    mock_audit_table = mock.MagicMock()

    def table_router_vol(name: str) -> mock.MagicMock:
        if "recipients" in name:
            return mock_recipients_table
        if "volunteers" in name:
            return mock_volunteers_table
        if "audit" in name:
            return mock_audit_table
        return mock_donations_table

    mock_resource = mock.MagicMock()
    mock_resource.Table.side_effect = table_router_vol
    mock_sns = mock.MagicMock()

    config = create_test_config()
    d_repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)
    r_repo = RecipientsRepository(dynamodb_resource=mock_resource, config=config)
    v_repo = VolunteersRepository(dynamodb_resource=mock_resource, config=config)
    a_repo = AuditRepository(dynamodb_resource=mock_resource, config=config)

    fixed_now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    donation = build_sample_donation(
        donation_id="don-degraded-vol",
        ready_by=fixed_now + timedelta(hours=2),
        perishability_hours=6.0,
    )
    donation_matched = donation.model_dump()
    donation_matched["status"] = DonationStatus.MATCHED.value
    donation_matched["matched_recipient_id"] = "rec-01"

    rec = build_sample_recipient(
        "rec-01", "Downtown Shelter", Coordinates(latitude=40.72, longitude=-74.01)
    )
    vol = Volunteer(
        volunteer_id="vol-01",
        volunteer_name="Alice",
        phone="+12125550199",
        address="100 Volunteer Way",
        coordinates=Coordinates(latitude=40.73, longitude=-74.00),
        max_capacity_kg=50.0,
        vehicle_type="car",
        is_available=True,
        service_region="metro-core",
    )

    call_count = 0

    class HybridFailingDistanceCalculator:
        def calculate_distance_km(
            self, _origin: Coordinates, _destination: Coordinates
        ) -> float:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 2.5  # Recipient match distance succeeds
            raise LocationServiceUnavailableError(
                "Amazon Location Service route calculation failed: "
                "EndpointConnectionError"
            )

    hybrid_calc = HybridFailingDistanceCalculator()

    # Step 0 reads REPORTED; Step 8 assign_volunteer reads MATCHED;
    # Step 8 flag_for_human reads MATCHED
    mock_donations_table.get_item.side_effect = [
        {"Item": donation.model_dump()},
        {"Item": donation_matched},
        {"Item": donation_matched},
    ]
    mock_donations_table.update_item.return_value = {}
    mock_recipients_table.get_item.return_value = {"Item": rec.model_dump()}
    mock_recipients_table.query.return_value = {"Items": [rec.model_dump()]}
    mock_volunteers_table.query.return_value = {"Items": [vol.model_dump()]}
    mock_audit_table.query.return_value = {"Items": []}
    mock_audit_table.put_item.return_value = {}

    orchestrator = StrandsOrchestrator(
        donations_repo=d_repo,
        recipients_repo=r_repo,
        volunteers_repo=v_repo,
        audit_repo=a_repo,
        distance_calculator=hybrid_calc,
        sns_client=mock_sns,
        config=config,
    )

    result = orchestrator.coordinate_donation(
        donation_id="don-degraded-vol",
        correlation_id="trace-degraded-vol",
    )

    assert result.status == DonationStatus.ESCALATED
    assert result.escalation_ticket is not None
    assert result.escalation_ticket.reason == EscalationReason.NO_MATCH_WITHIN_WINDOW
    assert "volunteer assignment" in result.escalation_ticket.details["summary"]


# ------------------------------------------------------------------------------
# 7. Strict Production Default allow_fallback=False vs Explicit Fallback
# ------------------------------------------------------------------------------
def test_amazon_location_strict_allow_fallback_default() -> None:
    """Verify allow_fallback=False is default and never silently degrades."""
    # 1. Default constructor must have allow_fallback=False
    calc_default = AmazonLocationDistanceCalculator(location_client=None)
    assert calc_default._allow_fallback is False

    # 2. Unconfigured client with allow_fallback=False must raise
    c1 = Coordinates(latitude=40.71, longitude=-74.00)
    c2 = Coordinates(latitude=40.72, longitude=-74.01)
    with pytest.raises(LocationServiceUnavailableError) as exc1:
        calc_default.calculate_distance_km(c1, c2)
    assert "fallback is disabled" in str(exc1.value)

    # 3. Client error with allow_fallback=False must raise
    mock_client = mock.MagicMock()
    mock_client.calculate_route.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "Denied"}},
        "CalculateRoute",
    )
    calc_prod = AmazonLocationDistanceCalculator(
        location_client=mock_client,
        allow_fallback=False,
    )
    with pytest.raises(LocationServiceUnavailableError) as exc2:
        calc_prod.calculate_distance_km(c1, c2)
    assert "AccessDeniedException" in str(exc2.value)

    # 4. Explicit controlled local testing with allow_fallback=True succeeds
    calc_local = AmazonLocationDistanceCalculator(
        location_client=mock_client,
        allow_fallback=True,
    )
    dist = calc_local.calculate_distance_km(c1, c2)
    assert dist > 0.0  # Geodesic fallback succeeded


# ------------------------------------------------------------------------------
# 8. Location Data Privacy & Sanitization Across Logs, Exceptions & Audit
# ------------------------------------------------------------------------------
def test_location_privacy_and_sanitization(caplog: pytest.LogCaptureFixture) -> None:
    """Verify plaintext coordinates and raw street addresses never leak."""
    caplog.set_level(logging.DEBUG)

    mock_donations_table = mock.MagicMock()
    mock_recipients_table = mock.MagicMock()
    mock_volunteers_table = mock.MagicMock()
    mock_audit_table = mock.MagicMock()

    def table_router_priv(name: str) -> mock.MagicMock:
        if "recipients" in name:
            return mock_recipients_table
        if "volunteers" in name:
            return mock_volunteers_table
        if "audit" in name:
            return mock_audit_table
        return mock_donations_table

    mock_resource = mock.MagicMock()
    mock_resource.Table.side_effect = table_router_priv
    mock_sns = mock.MagicMock()

    config = create_test_config()
    d_repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)
    r_repo = RecipientsRepository(dynamodb_resource=mock_resource, config=config)
    v_repo = VolunteersRepository(dynamodb_resource=mock_resource, config=config)
    a_repo = AuditRepository(dynamodb_resource=mock_resource, config=config)

    secret_donor_address = "999 Confidential Way, Suite 400, Secret City"
    secret_lat = 40.789123
    secret_lon = -73.987654

    donation = Donation(
        donation_id="don-privacy-01",
        donor_id="donor-priv",
        donor_name="Private Donor",
        donor_phone="+12125550199",
        donor_address=secret_donor_address,
        donor_coordinates=Coordinates(latitude=secret_lat, longitude=secret_lon),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=15.0,
        ready_by=datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc),
        perishability_hours=5.0,
        service_region="metro-core",
        status=DonationStatus.REPORTED,
    )

    rec = build_sample_recipient(
        "rec-priv",
        "Safe Haven Shelter",
        Coordinates(latitude=40.7900, longitude=-73.9800),
    )

    mock_loc_client = mock.MagicMock()
    mock_loc_client.calculate_route.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "CalculateRoute",
    )
    dist_calc = AmazonLocationDistanceCalculator(
        location_client=mock_loc_client,
        allow_fallback=False,
    )

    mock_donations_table.get_item.return_value = {"Item": donation.model_dump()}
    mock_donations_table.update_item.return_value = {}
    mock_recipients_table.query.return_value = {"Items": [rec.model_dump()]}
    mock_audit_table.query.return_value = {"Items": []}
    mock_audit_table.put_item.return_value = {}

    orchestrator = StrandsOrchestrator(
        donations_repo=d_repo,
        recipients_repo=r_repo,
        volunteers_repo=v_repo,
        audit_repo=a_repo,
        distance_calculator=dist_calc,
        sns_client=mock_sns,
        config=config,
    )

    result = orchestrator.coordinate_donation(
        donation_id="don-privacy-01",
        correlation_id="trace-privacy",
    )

    assert result.status == DonationStatus.ESCALATED

    # Assert application logs do NOT contain secret coordinates or raw address
    all_log_text = caplog.text
    assert secret_donor_address not in all_log_text
    assert str(secret_lat) not in all_log_text
    assert str(secret_lon) not in all_log_text

    # Assert escalation ticket details do NOT contain secret coordinates or address
    ticket_details = result.escalation_ticket.details
    ticket_text = str(ticket_details)
    assert secret_donor_address not in ticket_text
    assert str(secret_lat) not in ticket_text
    assert str(secret_lon) not in ticket_text

    # Assert updates recorded in mock_donations_table do NOT contain secret coordinates
    for call in mock_donations_table.update_item.call_args_list:
        kwargs = call[1]
        vals = str(kwargs.get("ExpressionAttributeValues", {}))
        assert secret_donor_address not in vals
        assert str(secret_lat) not in vals
        assert str(secret_lon) not in vals

    # Assert audit table payloads do NOT contain secret coordinates or raw address
    for call in mock_audit_table.put_item.call_args_list:
        item = str(call[1].get("Item", {}))
        assert secret_donor_address not in item
        assert str(secret_lat) not in item
        assert str(secret_lon) not in item

    # Assert exception messages directly raised never leak coordinates or addresses
    with pytest.raises(LocationServiceUnavailableError) as exc_privacy:
        dist_calc.calculate_distance_km(donation.donor_coordinates, rec.coordinates)
    exc_str = str(exc_privacy.value)
    assert secret_donor_address not in exc_str
    assert str(secret_lat) not in exc_str
    assert str(secret_lon) not in exc_str
    assert "ThrottlingException" in exc_str
