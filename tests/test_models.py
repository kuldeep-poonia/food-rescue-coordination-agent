"""Adversarial and boundary test suite for domain entity models and validation."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from models import (
    AgentCoreRuntimeEvent,
    AgentCoreRuntimeResponse,
    Coordinates,
    Donation,
    DonationClassification,
    DonationStateConflictError,
    DonationStatus,
    EscalationReason,
    EscalationTicket,
    FoodCategory,
    GuardrailViolationError,
    MatchCandidate,
    MatchResult,
    MemoryEntry,
    NotificationMessage,
    NotificationRecipientType,
    OrchestrationResult,
    PipelineStep,
    Recipient,
    RunningSummary,
    SessionContext,
    UrgencyLevel,
    Volunteer,
    VolunteerAssignment,
)
from redaction import (
    mask_phone_number,
    mask_street_address,
    sanitize_payload_for_logging,
)


def create_valid_coordinates() -> Coordinates:
    """Provide standard valid coordinates for model fixtures."""
    return Coordinates(latitude=40.7128, longitude=-74.0060)


def test_donation_rejects_negative_quantity() -> None:
    """Verify negative and zero quantity_kg inputs are rejected by validation."""
    future_time = datetime.now(timezone.utc) + timedelta(hours=2)

    with pytest.raises(ValidationError) as exc_info:
        Donation(
            donation_id="don-001",
            donor_id="donor-1",
            donor_name="Downtown Bakery",
            donor_phone="+12125550199",
            donor_address="123 Main St",
            donor_coordinates=create_valid_coordinates(),
            food_category=FoodCategory.BAKERY,
            quantity_kg=-5.0,  # Negative quantity
            ready_by=future_time,
            perishability_hours=12.0,
            service_region="metro-core",
        )
    assert "quantity_kg" in str(exc_info.value)


def test_donation_rejects_past_expiry() -> None:
    """Verify ready_by timestamp in the past is rejected."""
    past_time = datetime.now(timezone.utc) - timedelta(minutes=30)

    with pytest.raises(ValidationError) as exc_info:
        Donation(
            donation_id="don-002",
            donor_id="donor-1",
            donor_name="Downtown Bakery",
            donor_phone="+12125550199",
            donor_address="123 Main St",
            donor_coordinates=create_valid_coordinates(),
            food_category=FoodCategory.BAKERY,
            quantity_kg=15.0,
            ready_by=past_time,  # Past timestamp
            perishability_hours=12.0,
            service_region="metro-core",
        )
    assert "ready_by timestamp must be in the future" in str(exc_info.value)


def test_donation_rejects_malformed_phone() -> None:
    """Verify malformed phone numbers are rejected by regex validation."""
    future_time = datetime.now(timezone.utc) + timedelta(hours=2)

    invalid_phones = ["123", "phone-number", "++12345", "0000000"]
    for bad_phone in invalid_phones:
        with pytest.raises(ValidationError):
            Donation(
                donation_id="don-003",
                donor_id="donor-1",
                donor_name="Downtown Bakery",
                donor_phone=bad_phone,
                donor_address="123 Main St",
                donor_coordinates=create_valid_coordinates(),
                food_category=FoodCategory.BAKERY,
                quantity_kg=10.0,
                ready_by=future_time,
                perishability_hours=6.0,
                service_region="metro-core",
            )


def test_donation_rejects_oversized_string_input() -> None:
    """Verify 10,000+ character string attacks are rejected before DB access."""
    future_time = datetime.now(timezone.utc) + timedelta(hours=2)
    oversized_payload = "A" * 15000

    with pytest.raises(ValidationError):
        Donation(
            donation_id="don-004",
            donor_id="donor-1",
            donor_name=oversized_payload,  # 15,000 chars
            donor_phone="+12125550199",
            donor_address="123 Main St",
            donor_coordinates=create_valid_coordinates(),
            food_category=FoodCategory.BAKERY,
            quantity_kg=10.0,
            ready_by=future_time,
            perishability_hours=6.0,
            service_region="metro-core",
        )


def test_donation_rejects_missing_service_region() -> None:
    """Verify omitted service_region triggers validation error.

    Zero silent fallback is permitted.
    """
    future_time = datetime.now(timezone.utc) + timedelta(hours=2)

    with pytest.raises(ValidationError) as exc_info:
        Donation(  # type: ignore[call-arg]
            donation_id="don-005",
            donor_id="donor-1",
            donor_name="Downtown Bakery",
            donor_phone="+12125550199",
            donor_address="123 Main St",
            donor_coordinates=create_valid_coordinates(),
            food_category=FoodCategory.BAKERY,
            quantity_kg=10.0,
            ready_by=future_time,
            perishability_hours=6.0,
            # service_region deliberately omitted
        )
    assert "service_region" in str(exc_info.value)


def test_recipient_rejects_negative_capacity() -> None:
    """Verify recipient capacity cannot be set to a negative value."""
    with pytest.raises(ValidationError):
        Recipient(
            recipient_id="rec-001",
            organization_name="Community Shelter",
            contact_name="Jane Doe",
            contact_phone="+12125550188",
            address="456 Elm St",
            coordinates=create_valid_coordinates(),
            capacity_kg_remaining=-10.0,  # Invalid negative capacity
            service_region="metro-core",
        )


def test_volunteer_rejects_invalid_coordinates() -> None:
    """Verify latitude and longitude out of geographic bounds are rejected."""
    with pytest.raises(ValidationError):
        Volunteer(
            volunteer_id="vol-001",
            volunteer_name="Alex Smith",
            phone="+12125550177",
            address="789 Pine St",
            coordinates=Coordinates(latitude=95.0, longitude=-74.0),  # Latitude > 90
            max_capacity_kg=50.0,
            vehicle_type="car",
            service_region="metro-core",
        )


def test_pii_redaction_utilities() -> None:
    """Verify phone, address, and payload masking correctly obscure sensitive data."""
    raw_phone = "+1 (212) 555-0199"
    masked_phone = mask_phone_number(raw_phone)
    assert masked_phone == "***-***-0199"

    raw_address = "456 West 34th Street, Suite 500, New York, NY"
    masked_address = mask_street_address(raw_address)
    assert masked_address.startswith("*** ")
    assert "New York" in masked_address

    payload = {
        "donor_phone": "+12125550199",
        "donor_name": "Alice Wonderland",
        "donor_address": "123 Main St, Springfield",
        "metadata": {
            "contact_phone": "+12125550188",
            "quantity_kg": 25.5,
        },
    }
    sanitized = sanitize_payload_for_logging(payload)
    assert sanitized["donor_phone"] == "***-***-0199"
    assert sanitized["donor_name"] == "A***"
    assert sanitized["donor_address"].startswith("*** ")
    assert sanitized["metadata"]["contact_phone"] == "***-***-0188"
    assert sanitized["metadata"]["quantity_kg"] == 25.5


def test_coordination_models() -> None:
    """Verify instantiation and constraints of tool layer coordination models."""
    classification = DonationClassification(
        food_category=FoodCategory.PRODUCE,
        urgency_level=UrgencyLevel.CRITICAL,
        shelf_life_remaining_hours=1.5,
        is_safety_threshold_breached=False,
    )
    assert classification.urgency_level == UrgencyLevel.CRITICAL
    assert classification.food_category == FoodCategory.PRODUCE

    candidate = MatchCandidate(
        recipient_id="rec-001",
        recipient_name="Food Bank East",
        score=0.88,
        distance_km=4.2,
        capacity_match_kg=150.0,
        dietary_fit=True,
        reason="Close proximity with ample capacity",
    )
    assert candidate.score == 0.88

    match_result = MatchResult(
        donation_id="don-100",
        ranked_candidates=[candidate],
        best_match=candidate,
    )
    assert match_result.best_match is not None
    assert match_result.best_match.recipient_id == "rec-001"

    assignment = VolunteerAssignment(
        assignment_id="asgn-001",
        donation_id="don-100",
        volunteer_id="vol-001",
        recipient_id="rec-001",
    )
    assert assignment.status == "assigned"

    notification = NotificationMessage(
        recipient_type=NotificationRecipientType.VOLUNTEER,
        destination="+12125550177",
        template_id="VOLUNTEER_ASSIGNMENT_V1",
        rendered_body="Pickup at 123 Main St",
        correlation_id="corr-123",
    )
    assert notification.recipient_type == NotificationRecipientType.VOLUNTEER

    ticket = EscalationTicket(
        ticket_id="tkt-001",
        donation_id="don-100",
        reason=EscalationReason.NO_MATCH_WITHIN_WINDOW,
        details={"attempted_candidates": 0},
    )
    assert ticket.reason == EscalationReason.NO_MATCH_WITHIN_WINDOW


def test_orchestration_models_and_errors() -> None:
    """Verify PipelineStep, OrchestrationResult, and exception contracts."""
    # Pipeline steps
    assert PipelineStep.INTAKE.value == "intake"
    assert PipelineStep.CLAIM_RECIPIENT.value == "claim_recipient"
    assert PipelineStep.ASSIGN_VOLUNTEER.value == "assign_volunteer"

    # Exceptions
    state_err = DonationStateConflictError("Expected MATCHED state")
    assert isinstance(state_err, Exception)
    assert str(state_err) == "Expected MATCHED state"

    guard_err = GuardrailViolationError("Shelf life below threshold")
    assert isinstance(guard_err, RuntimeError)
    assert str(guard_err) == "Shelf life below threshold"

    # OrchestrationResult
    result = OrchestrationResult(
        donation_id="don-999",
        status=DonationStatus.ASSIGNED,
        matched_recipient_id="rec-001",
        assigned_volunteer_id="vol-001",
        steps_completed=[
            PipelineStep.INTAKE,
            PipelineStep.CLASSIFY,
            PipelineStep.FETCH_CAPACITY,
            PipelineStep.MATCH,
            PipelineStep.CLAIM_RECIPIENT,
            PipelineStep.ASSIGN_VOLUNTEER,
            PipelineStep.DISPATCH_NOTIFICATIONS,
            PipelineStep.COMPLETE,
        ],
        is_dry_run=False,
        correlation_id="corr-test-999",
    )
    assert result.donation_id == "don-999"
    assert result.status == DonationStatus.ASSIGNED
    assert len(result.steps_completed) == 8
    assert not result.is_dry_run


def test_session_context_model() -> None:
    """Verify initialization, invariants, and validation of SessionContext model."""
    session = SessionContext(
        session_id="session-metro-core-2026-09-05",
        service_region="metro-core",
        session_date="2026-09-05",
        recipients_near_capacity=["rec-001"],
        recent_volunteer_assignments={"vol-001": 2},
        total_donations_processed=5,
        total_kg_routed=125.5,
        active_escalations_count=1,
        ttl_epoch=1788566400,
    )
    assert session.session_id == "session-metro-core-2026-09-05"
    assert session.service_region == "metro-core"
    assert session.total_donations_processed == 5
    assert session.total_kg_routed == 125.5
    assert session.recipients_near_capacity == ["rec-001"]
    assert session.recent_volunteer_assignments["vol-001"] == 2

    # Verify invalid date pattern fails validation
    with pytest.raises(ValidationError):
        SessionContext(
            session_id="session-bad",
            service_region="metro-core",
            session_date="bad-date",
            ttl_epoch=1788566400,
        )


def test_running_summary_model() -> None:
    """Verify initialization and attributes of RunningSummary model."""
    summary = RunningSummary(
        service_region="metro-core",
        date_str="2026-09-05",
        total_kg_routed=250.0,
        meals_equivalent=500,
        organizations_served=4,
        donations_count=10,
    )
    assert summary.service_region == "metro-core"
    assert summary.date_str == "2026-09-05"
    assert summary.total_kg_routed == 250.0
    assert summary.meals_equivalent == 500
    assert summary.organizations_served == 4
    assert summary.donations_count == 10

    # Negative meals_equivalent rejected
    with pytest.raises(ValidationError):
        RunningSummary(
            service_region="metro-core",
            date_str="2026-09-05",
            total_kg_routed=250.0,
            meals_equivalent=-1,
            organizations_served=4,
            donations_count=10,
        )


def test_memory_entry_model() -> None:
    """Verify MemoryEntry model structure, entity_type restriction, and validation."""
    entry = MemoryEntry(
        memory_id="mem-001",
        entity_type="donor",
        entity_id="donor-100",
        pattern_type="recurring_surplus",
        insights={"typical_kg": 45.0, "preferred_pickup_hour": 16},
        ttl_epoch=1791158400,
    )
    assert entry.entity_type == "donor"
    assert entry.pattern_type == "recurring_surplus"
    assert entry.insights["typical_kg"] == 45.0

    # Invalid entity_type rejected
    with pytest.raises(ValidationError):
        MemoryEntry(
            memory_id="mem-002",
            entity_type="invalid_type",
            entity_id="id-100",
            pattern_type="pattern",
            ttl_epoch=1791158400,
        )


def test_agent_core_runtime_models() -> None:
    """Verify Bedrock AgentCore runtime event and response models."""
    raw_event = {
        "messageVersion": "1.0",
        "agent": {"name": "frca-agent", "id": "agt-123"},
        "actionGroup": "coordinate_donation",
        "apiPath": "/coordinate",
        "httpMethod": "POST",
        "parameters": [{"name": "donation_id", "value": "don-123"}],
        "requestBody": {"content": {"application/json": {"donation_id": "don-123"}}},
        "sessionId": "session-metro-core-2026-09-05",
    }
    event = AgentCoreRuntimeEvent.model_validate(raw_event)
    assert event.action_group == "coordinate_donation"
    assert event.session_id == "session-metro-core-2026-09-05"
    assert event.http_method == "POST"

    response = AgentCoreRuntimeResponse(
        messageVersion="1.0",
        response={
            "actionGroup": "coordinate_donation",
            "apiPath": "/coordinate",
            "httpStatusCode": 200,
            "responseBody": {"application/json": {"status": "ASSIGNED"}},
        },
    )
    assert response.response["httpStatusCode"] == 200


