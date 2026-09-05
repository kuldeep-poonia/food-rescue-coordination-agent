"""Resilience, throttling fail-safe, and DLQ dispatch tests for AgentCore runtime."""

import json
from collections.abc import Generator
from unittest import mock

import pytest
from botocore.exceptions import ClientError

from agent.memory_store import AgentMemoryStore
from agent.orchestrator import StrandsOrchestrator
from agent.runtime import lambda_handler, set_runtime_dependencies
from agent.session_manager import AgentSessionManager
from config import AppConfig
from models import (
    DonationClassification,
    DonationStatus,
    FoodCategory,
    MemoryEntry,
    OrchestrationResult,
    PipelineStep,
    UrgencyLevel,
)


@pytest.fixture(autouse=True)
def reset_runtime_globals() -> Generator[None, None, None]:
    """Ensure clean runtime global dependency state for each test."""
    set_runtime_dependencies(None, None, None, None, None)
    yield
    set_runtime_dependencies(None, None, None, None, None)


def test_lambda_handler_coordinate_donation_happy_path() -> None:
    """Verify Bedrock action group coordinate-donation execution and formatting."""
    mock_config = mock.create_autospec(AppConfig, instance=True)
    mock_orchestrator = mock.create_autospec(StrandsOrchestrator, instance=True)
    mock_session_mgr = mock.create_autospec(AgentSessionManager, instance=True)
    mock_memory = mock.create_autospec(AgentMemoryStore, instance=True)
    mock_sqs = mock.MagicMock()

    mock_orchestrator.coordinate_donation.return_value = OrchestrationResult(
        donation_id="don-run-01",
        status=DonationStatus.ASSIGNED,
        classification=DonationClassification(
            food_category=FoodCategory.PREPARED_MEALS,
            urgency_level=UrgencyLevel.HIGH,
            shelf_life_remaining_hours=3.5,
            is_safety_threshold_breached=False,
        ),
        steps_completed=[
            PipelineStep.CLASSIFY,
            PipelineStep.MATCH,
            PipelineStep.ASSIGN_VOLUNTEER,
        ],
        is_dry_run=False,
        correlation_id="sess-run-123",
    )

    set_runtime_dependencies(
        config=mock_config,
        orchestrator=mock_orchestrator,
        session_manager=mock_session_mgr,
        memory_store=mock_memory,
        sqs_client=mock_sqs,
    )

    event = {
        "messageVersion": "1.0",
        "actionGroup": "food-rescue-actions",
        "apiPath": "/coordinate-donation",
        "httpMethod": "POST",
        "parameters": [
            {"name": "donation_id", "type": "string", "value": "don-run-01"},
            {"name": "service_region", "type": "string", "value": "metro-core"},
        ],
        "sessionId": "sess-run-123",
    }

    response = lambda_handler(event)

    assert response["messageVersion"] == "1.0"
    resp_inner = response["response"]
    assert resp_inner["httpStatusCode"] == 200
    assert resp_inner["apiPath"] == "/coordinate-donation"

    body = json.loads(resp_inner["responseBody"]["application/json"]["body"])
    assert body["donation_id"] == "don-run-01"
    assert body["status"] == "assigned"

    mock_orchestrator.coordinate_donation.assert_called_once_with(
        donation_id="don-run-01",
        dry_run=False,
        correlation_id="sess-run-123",
    )
    mock_session_mgr.record_donation_outcome.assert_called_once()


def test_lambda_handler_throttling_dispatches_to_dlq_and_returns_429() -> None:
    """Verify ThrottlingException triggers SQS DLQ dispatch and clean 429 response."""
    mock_config = mock.MagicMock(spec=AppConfig)
    mock_config.coordinator_dlq_url = "https://sqs.us-east-1.amazonaws.com/123/frca-dlq"
    mock_orchestrator = mock.create_autospec(StrandsOrchestrator, instance=True)
    mock_session_mgr = mock.create_autospec(AgentSessionManager, instance=True)
    mock_memory = mock.create_autospec(AgentMemoryStore, instance=True)
    mock_sqs = mock.MagicMock()

    # Simulate downstream throttling during coordinate_donation
    mock_orchestrator.coordinate_donation.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "InvokeModel",
    )

    set_runtime_dependencies(
        config=mock_config,
        orchestrator=mock_orchestrator,
        session_manager=mock_session_mgr,
        memory_store=mock_memory,
        sqs_client=mock_sqs,
    )

    event = {
        "messageVersion": "1.0",
        "actionGroup": "food-rescue-actions",
        "apiPath": "/coordinate-donation",
        "httpMethod": "POST",
        "parameters": [
            {"name": "donation_id", "type": "string", "value": "don-throttle-01"},
        ],
        "sessionId": "sess-throttle-456",
    }

    response = lambda_handler(event)

    # 1. Assert DLQ message was sent with error attribute
    assert mock_sqs.send_message.call_count == 1
    _, kwargs = mock_sqs.send_message.call_args
    assert kwargs["QueueUrl"] == "https://sqs.us-east-1.amazonaws.com/123/frca-dlq"
    msg_body = json.loads(kwargs["MessageBody"])
    assert msg_body["error"] == "ThrottlingException"
    assert "don-throttle-01" in str(msg_body["event"])

    # 2. Assert Bedrock received graceful HTTP 429 without unhandled Lambda crash
    resp_inner = response["response"]
    assert resp_inner["httpStatusCode"] == 429
    body = json.loads(resp_inner["responseBody"]["application/json"]["body"])
    assert body["status"] == "QUEUED_FOR_COORDINATOR"
    assert body["reason"] == "THROTTLING_DEGRADATION"


def test_lambda_handler_query_memory_action_group() -> None:
    """Verify Bedrock action group query-memory returns entity patterns."""
    mock_config = mock.create_autospec(AppConfig, instance=True)
    mock_orchestrator = mock.create_autospec(StrandsOrchestrator, instance=True)
    mock_session_mgr = mock.create_autospec(AgentSessionManager, instance=True)
    mock_memory = mock.create_autospec(AgentMemoryStore, instance=True)
    mock_sqs = mock.MagicMock()

    mock_memory.query_entity_patterns.return_value = [
        MemoryEntry(
            memory_id="mem-001",
            entity_type="donor",
            entity_id="donor-10",
            pattern_type="schedule_preference",
            insights={"preferred_time": "morning"},
            ttl_epoch=1789000000,
        )
    ]

    set_runtime_dependencies(
        config=mock_config,
        orchestrator=mock_orchestrator,
        session_manager=mock_session_mgr,
        memory_store=mock_memory,
        sqs_client=mock_sqs,
    )

    event = {
        "messageVersion": "1.0",
        "actionGroup": "food-rescue-actions",
        "apiPath": "/query-memory",
        "httpMethod": "POST",
        "parameters": [
            {"name": "entity_type", "type": "string", "value": "donor"},
            {"name": "entity_id", "type": "string", "value": "donor-10"},
        ],
    }

    response = lambda_handler(event)

    resp_inner = response["response"]
    assert resp_inner["httpStatusCode"] == 200
    body = json.loads(resp_inner["responseBody"]["application/json"]["body"])
    assert len(body["patterns"]) == 1
    assert body["patterns"][0]["memory_id"] == "mem-001"
    assert body["patterns"][0]["entity_id"] == "donor-10"
