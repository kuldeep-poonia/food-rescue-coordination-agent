"""Tests for AgentCore runtime singleton reuse and cold start minimization."""

import time
from collections.abc import Generator
from unittest import mock

import pytest

from agent.memory_store import AgentMemoryStore
from agent.orchestrator import StrandsOrchestrator
from agent.runtime import (
    get_runtime_dependencies,
    lambda_handler,
    set_runtime_dependencies,
)
from agent.session_manager import AgentSessionManager
from config import AppConfig


@pytest.fixture(autouse=True)
def reset_runtime_globals() -> Generator[None, None, None]:
    """Ensure clean runtime global dependency state for each test."""
    set_runtime_dependencies(None, None, None, None, None)
    yield
    set_runtime_dependencies(None, None, None, None, None)


def test_runtime_dependencies_singleton_caching() -> None:
    """Verify runtime dependencies are allocated once and reused across invocations."""
    cfg1, orch1, sess1, mem1, sqs1 = get_runtime_dependencies()
    cfg2, orch2, sess2, mem2, sqs2 = get_runtime_dependencies()

    # Must be identical cached instances (pointer equality)
    assert cfg1 is cfg2
    assert orch1 is orch2
    assert sess1 is sess2
    assert mem1 is mem2
    assert sqs1 is sqs2


def test_runtime_cold_start_timing_sub_second() -> None:
    """Benchmark cold vs warm retrieval demonstrating sub-second warm execution."""
    # Warm invocation
    get_runtime_dependencies()

    t0 = time.perf_counter()
    for _ in range(100):
        get_runtime_dependencies()
    elapsed_ms = (time.perf_counter() - t0) * 1000

    # 100 warm retrievals should complete in mere milliseconds (< 50ms)
    assert elapsed_ms < 50.0, f"100 warm retrievals took {elapsed_ms:.2f}ms"


def test_lambda_handler_eventbridge_scheduled_reconciliation() -> None:
    """Verify EventBridge scheduled reconciliation triggers session update."""
    mock_config = mock.create_autospec(AppConfig, instance=True)
    mock_orchestrator = mock.create_autospec(StrandsOrchestrator, instance=True)
    mock_session_mgr = mock.create_autospec(AgentSessionManager, instance=True)
    mock_memory = mock.create_autospec(AgentMemoryStore, instance=True)
    mock_sqs = mock.MagicMock()

    mock_reconciled = mock.MagicMock()
    mock_reconciled.session_date = "2026-09-05"
    mock_reconciled.total_kg_routed = 150.0
    mock_reconciled.total_donations_processed = 4
    mock_session_mgr.reconcile_session_metrics.return_value = mock_reconciled

    set_runtime_dependencies(
        config=mock_config,
        orchestrator=mock_orchestrator,
        session_manager=mock_session_mgr,
        memory_store=mock_memory,
        sqs_client=mock_sqs,
    )

    event = {
        "detail-type": "RECONCILE_SESSION_METRICS",
        "source": "aws.events",
        "detail": {
            "service_region": "metro-core",
            "session_date": "2026-09-05",
        },
    }

    response = lambda_handler(event)

    assert response["status"] == "SUCCESS"
    assert response["action"] == "RECONCILE_SESSION_METRICS"
    assert response["service_region"] == "metro-core"
    assert response["session_date"] == "2026-09-05"
    assert response["total_kg_routed"] == 150.0
    assert response["total_donations_processed"] == 4

    mock_session_mgr.reconcile_session_metrics.assert_called_once_with(
        service_region="metro-core",
        date_str="2026-09-05",
    )
