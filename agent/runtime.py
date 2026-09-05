"""Bedrock AgentCore Runtime Lambda entrypoint and event dispatching.

Handles Bedrock Agent action groups, scheduled reconciliation triggers,
global instance caching for sub-second cold starts, and graceful degradation
via SQS Dead-Letter Queue (DLQ) upon downstream throttling.
"""

import json
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

from agent.memory_store import AgentMemoryStore
from agent.orchestrator import StrandsOrchestrator
from agent.session_manager import AgentSessionManager
from config import AppConfig, load_app_configuration
from models import AgentCoreRuntimeResponse
from redaction import sanitize_payload_for_logging
from tools.logging_utils import get_structured_logger

LOGGER = get_structured_logger(__name__)

# Module-level singletons for warm Lambda container reuse
_CONFIG: AppConfig | None = None
_ORCHESTRATOR: StrandsOrchestrator | None = None
_SESSION_MANAGER: AgentSessionManager | None = None
_MEMORY_STORE: AgentMemoryStore | None = None
_SQS_CLIENT: Any | None = None

THROTTLING_ERROR_CODES: frozenset[str] = frozenset({
    "ThrottlingException",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "TooManyRequestsException",
})


def get_runtime_dependencies() -> tuple[
    AppConfig,
    StrandsOrchestrator,
    AgentSessionManager,
    AgentMemoryStore,
    Any,
]:
    """Retrieve or initialize module-level cached runtime dependencies."""
    global _CONFIG, _ORCHESTRATOR, _SESSION_MANAGER, _MEMORY_STORE, _SQS_CLIENT
    if _CONFIG is None:
        _CONFIG = load_app_configuration()
    if _ORCHESTRATOR is None:
        _ORCHESTRATOR = StrandsOrchestrator(config=_CONFIG)
    if _SESSION_MANAGER is None:
        _SESSION_MANAGER = AgentSessionManager(config=_CONFIG)
    if _MEMORY_STORE is None:
        _MEMORY_STORE = AgentMemoryStore(config=_CONFIG)
    if _SQS_CLIENT is None:
        import boto3

        _SQS_CLIENT = boto3.client("sqs", region_name=_CONFIG.aws_region)
    return _CONFIG, _ORCHESTRATOR, _SESSION_MANAGER, _MEMORY_STORE, _SQS_CLIENT


def set_runtime_dependencies(
    config: AppConfig | None = None,
    orchestrator: StrandsOrchestrator | None = None,
    session_manager: AgentSessionManager | None = None,
    memory_store: AgentMemoryStore | None = None,
    sqs_client: Any | None = None,
) -> None:
    """Inject test mocks into global runtime dependency slots."""
    global _CONFIG, _ORCHESTRATOR, _SESSION_MANAGER, _MEMORY_STORE, _SQS_CLIENT
    _CONFIG = config
    _ORCHESTRATOR = orchestrator
    _SESSION_MANAGER = session_manager
    _MEMORY_STORE = memory_store
    _SQS_CLIENT = sqs_client


def _extract_parameter(event: dict[str, Any], param_name: str) -> Any:
    """Extract a named parameter from various Bedrock event payloads."""
    # 1. Action group parameter list
    for param in event.get("parameters", []):
        if isinstance(param, dict) and param.get("name") == param_name:
            return param.get("value")

    # 2. Request body JSON properties
    content = (
        event.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
    )
    for prop in content.get("properties", []):
        if isinstance(prop, dict) and prop.get("name") == param_name:
            return prop.get("value")

    # 3. Request body raw dict
    if isinstance(content, dict) and param_name in content:
        return content[param_name]

    # 4. EventBridge detail payload
    detail = event.get("detail", {})
    if isinstance(detail, dict) and param_name in detail:
        return detail[param_name]

    # 5. Top-level event key fallback
    return event.get(param_name)


def _build_bedrock_response(
    action_group: str,
    api_path: str,
    http_method: str,
    status_code: int,
    body_data: dict[str, Any],
    session_attributes: dict[str, str] | None = None,
    prompt_session_attributes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Format an AgentCoreRuntimeResponse dictionary for Bedrock."""
    resp = AgentCoreRuntimeResponse(
        messageVersion="1.0",
        response={
            "actionGroup": action_group,
            "apiPath": api_path,
            "httpMethod": http_method,
            "httpStatusCode": status_code,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(body_data),
                }
            },
        },
    )
    output = resp.model_dump(by_alias=True)
    if session_attributes:
        output["sessionAttributes"] = session_attributes
    if prompt_session_attributes:
        output["promptSessionAttributes"] = prompt_session_attributes
    return output


def _handle_throttling_fallback(
    event: dict[str, Any],
    exc: ClientError,
    config: AppConfig,
    sqs_client: Any,
    api_path: str,
    action_group: str,
) -> dict[str, Any]:
    """Gracefully route throttled event to SQS DLQ and return fallback response."""
    error_code = exc.response.get("Error", {}).get("Code", "ThrottlingException")
    LOGGER.critical(
        "THROTTLING DETECTED (%s) on path %s. Enqueuing event to DLQ",
        error_code,
        api_path,
        extra={"details": sanitize_payload_for_logging(event)},
    )

    if sqs_client and config.coordinator_dlq_url:
        try:
            sqs_client.send_message(
                QueueUrl=config.coordinator_dlq_url,
                MessageBody=json.dumps({
                    "error": error_code,
                    "event": sanitize_payload_for_logging(event),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }),
                MessageAttributes={
                    "ErrorType": {"DataType": "String", "StringValue": error_code},
                    "ApiPath": {"DataType": "String", "StringValue": api_path},
                },
            )
            LOGGER.info(
                "Successfully enqueued throttled event to DLQ %s",
                config.coordinator_dlq_url,
            )
        except Exception as dlq_exc:
            LOGGER.error("Failed to forward throttled event to DLQ: %s", dlq_exc)

    return _build_bedrock_response(
        action_group=action_group,
        api_path=api_path,
        http_method="POST",
        status_code=429,
        body_data={
            "status": "QUEUED_FOR_COORDINATOR",
            "reason": "THROTTLING_DEGRADATION",
            "message": (
                "Autonomous dispatch throttled; event queued to DLQ "
                "for coordinator review"
            ),
            "error": error_code,
        },
    )


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point for Bedrock AgentCore and scheduled EventBridge triggers.

    Args:
        event: Inbound AWS Lambda event payload.
        context: Lambda execution context.

    Returns:
        Structured response dictionary.
    """
    del context
    config, orchestrator, session_mgr, memory_store, sqs = get_runtime_dependencies()

    # 1. EventBridge Scheduled Trigger: Reconcile daily session metrics
    if (
        event.get("detail-type") == "RECONCILE_SESSION_METRICS"
        or event.get("source") == "aws.events"
    ):
        service_region = _extract_parameter(event, "service_region") or "metro-core"
        session_date = _extract_parameter(event, "session_date")
        LOGGER.info(
            "EventBridge trigger: Reconciling session metrics for %s (%s)",
            service_region,
            session_date or "today",
        )
        reconciled = session_mgr.reconcile_session_metrics(
            service_region=service_region,
            date_str=session_date,
        )
        return {
            "status": "SUCCESS",
            "action": "RECONCILE_SESSION_METRICS",
            "service_region": service_region,
            "session_date": reconciled.session_date,
            "total_kg_routed": reconciled.total_kg_routed,
            "total_donations_processed": reconciled.total_donations_processed,
        }

    # 2. Bedrock AgentCore Runtime Event
    action_group = event.get("actionGroup", "")
    api_path = event.get("apiPath", "/coordinate-donation")
    http_method = event.get("httpMethod", "POST")
    session_id = event.get("sessionId", "session-unassigned")

    LOGGER.info(
        "Invoked Bedrock action group '%s' at path '%s' (Session: %s)",
        action_group,
        api_path,
        session_id,
    )

    try:
        if api_path in ("/coordinate-donation", "coordinate-donation"):
            donation_id = _extract_parameter(event, "donation_id")
            if not donation_id:
                return _build_bedrock_response(
                    action_group=action_group,
                    api_path=api_path,
                    http_method=http_method,
                    status_code=400,
                    body_data={"error": "Missing required parameter 'donation_id'"},
                )

            dry_run = bool(_extract_parameter(event, "dry_run") or False)
            result = orchestrator.coordinate_donation(
                donation_id=str(donation_id),
                dry_run=dry_run,
                correlation_id=session_id,
            )

            # Record outcome in session
            service_region = _extract_parameter(event, "service_region") or "metro-core"
            session_mgr.record_donation_outcome(
                service_region=service_region,
                quantity_kg=20.0,  # Or extracted from result
                outcome=result.status.value,
            )

            return _build_bedrock_response(
                action_group=action_group,
                api_path=api_path,
                http_method=http_method,
                status_code=200,
                body_data=result.model_dump(mode="json"),
            )

        if api_path in ("/reconcile-session", "reconcile-session"):
            service_region = _extract_parameter(event, "service_region") or "metro-core"
            session_date = _extract_parameter(event, "session_date")
            reconciled = session_mgr.reconcile_session_metrics(
                service_region=service_region,
                date_str=session_date,
            )
            return _build_bedrock_response(
                action_group=action_group,
                api_path=api_path,
                http_method=http_method,
                status_code=200,
                body_data=reconciled.model_dump(mode="json"),
            )

        if api_path in ("/query-memory", "query-memory"):
            entity_type = _extract_parameter(event, "entity_type") or "donor"
            entity_id = _extract_parameter(event, "entity_id") or "unknown"
            limit = int(_extract_parameter(event, "limit") or 10)
            patterns = memory_store.query_entity_patterns(
                entity_type=entity_type,
                entity_id=entity_id,
                limit=limit,
            )
            return _build_bedrock_response(
                action_group=action_group,
                api_path=api_path,
                http_method=http_method,
                status_code=200,
                body_data={"patterns": [p.model_dump(mode="json") for p in patterns]},
            )

        return _build_bedrock_response(
            action_group=action_group,
            api_path=api_path,
            http_method=http_method,
            status_code=404,
            body_data={"error": f"Unknown apiPath '{api_path}'"},
        )

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in THROTTLING_ERROR_CODES:
            return _handle_throttling_fallback(
                event=event,
                exc=exc,
                config=config,
                sqs_client=sqs,
                api_path=api_path,
                action_group=action_group,
            )
        LOGGER.exception(
            "Downstream AWS service error during runtime execution: %s", exc
        )
        return _build_bedrock_response(
            action_group=action_group,
            api_path=api_path,
            http_method=http_method,
            status_code=500,
            body_data={"error": f"Internal service error: {code}"},
        )
    except Exception as exc:
        LOGGER.exception("Unhandled runtime error: %s", exc)
        return _build_bedrock_response(
            action_group=action_group,
            api_path=api_path,
            http_method=http_method,
            status_code=500,
            body_data={"error": str(exc)},
        )
