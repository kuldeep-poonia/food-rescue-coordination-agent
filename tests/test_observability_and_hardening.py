"""Hardcore test suite for Observability and Security Hardening.

Verifies:
1. Bidirectional IAM least-privilege evaluation (testing both ALLOW and DENY)
   with explicit reporting of evaluation path (AWS_SIMULATE vs FALLBACK_ENGINE).
2. Static IAM policy audit (zero wildcard Action/Resource, zero DeleteItem).
3. Structured JSON logging with correlation ID propagation.
4. CloudWatch PII sanitization and zero-leakage log integrity.
5. CloudWatch monitoring dashboard and alarms specification.
6. Multi-endpoint DoS abuse simulation under high-burst traffic.
"""

import io
import json
import logging
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from config import AppConfig
from frontend_api import FrontendApiService
from infra.iam_policy_evaluator import (
    EVALUATION_METRICS,
    EvaluationResult,
    extract_role_policies,
    format_evaluation_summary,
    load_cloudformation_template,
    reset_evaluation_metrics,
    simulate_role_action,
)
from redaction import sanitize_text_for_logging
from tools.logging_utils import (
    CORRELATION_ID_CONTEXT,
    StructuredJsonFormatter,
    get_structured_logger,
)

INFRA_TEMPLATE_PATH: Path = (
    Path(__file__).resolve().parent.parent / "infra" / "agent_core_infra.yaml"
)


@pytest.fixture(autouse=True)
def _reset_eval_metrics() -> Generator[None, None, None]:
    """Ensure evaluation metrics are clean before every test run."""
    reset_evaluation_metrics()
    yield
    reset_evaluation_metrics()


def test_runtime_iam_bidirectional_least_privilege_evaluation() -> None:
    """Verify bidirectional IAM least-privilege boundaries across all execution roles.

    Explicitly tests both positive authorization (ALLOW) and negative boundary
    enforcement (DENY), asserting the exact evaluation source (AWS_SIMULATE vs
    FALLBACK_ENGINE) and outputting an execution summary table.
    """
    template = load_cloudformation_template(INFRA_TEMPLATE_PATH)
    resources = template.get("Resources", {})

    expected_roles = [
        "FrontendApiRole",
        "NotificationToolRole",
        "DistanceCalculatorToolRole",
        "CapacityQueryToolRole",
        "VolunteerDispatchToolRole",
        "AgentExecutionRole",
    ]
    for role in expected_roles:
        assert role in resources, f"Role {role} missing from CloudFormation resources"

    # Bidirectional test assertions: (role, action, resource, decision, description)
    test_cases: list[tuple[str, str, str, str, str]] = [
        # --- 1. FrontendApiRole ---
        (
            "FrontendApiRole",
            "dynamodb:UpdateItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-recipients-dev",
            "allowed",
            "FrontendApiRole must be allowed to update recipient capacity",
        ),
        (
            "FrontendApiRole",
            "dynamodb:UpdateItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-volunteers-dev",
            "allowed",
            "FrontendApiRole must be allowed to toggle volunteer availability",
        ),
        (
            "FrontendApiRole",
            "dynamodb:PutItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-donations-dev",
            "allowed",
            "FrontendApiRole must be allowed to intake donations",
        ),
        (
            "FrontendApiRole",
            "dynamodb:UpdateItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-sessions-memory-dev",
            "allowed",
            "FrontendApiRole must be allowed to update rate limiting counters",
        ),
        (
            "FrontendApiRole",
            "dynamodb:DeleteItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-donations-dev",
            "implicitDeny",
            "FrontendApiRole must be denied DeleteItem on DonationsTable",
        ),
        (
            "FrontendApiRole",
            "dynamodb:DeleteItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-recipients-dev",
            "implicitDeny",
            "FrontendApiRole must be denied DeleteItem on RecipientsTable",
        ),
        (
            "FrontendApiRole",
            "sns:Publish",
            "arn:aws:sns:us-east-1:123456789012:frca-notifications-dev",
            "implicitDeny",
            "FrontendApiRole must be denied direct SNS publish",
        ),
        # --- 2. NotificationToolRole ---
        (
            "NotificationToolRole",
            "sns:Publish",
            "arn:aws:sns:us-east-1:123456789012:frca-notifications-dev",
            "allowed",
            "NotificationToolRole must be allowed to publish notifications",
        ),
        (
            "NotificationToolRole",
            "dynamodb:Query",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-donations-dev",
            "implicitDeny",
            "NotificationToolRole must be denied DynamoDB Query",
        ),
        (
            "NotificationToolRole",
            "dynamodb:DeleteItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-donations-dev",
            "implicitDeny",
            "NotificationToolRole must be denied DynamoDB DeleteItem",
        ),
        (
            "NotificationToolRole",
            "geo:CalculateRoute",
            "arn:aws:geo:us-east-1:123456789012:route-calculator/default",
            "implicitDeny",
            "NotificationToolRole must be denied Location calculate route",
        ),
        # --- 3. DistanceCalculatorToolRole ---
        (
            "DistanceCalculatorToolRole",
            "geo:CalculateRoute",
            "arn:aws:geo:us-east-1:123456789012:route-calculator/frca-calculator",
            "allowed",
            "DistanceCalculatorToolRole must be allowed to calculate routes",
        ),
        (
            "DistanceCalculatorToolRole",
            "sns:Publish",
            "arn:aws:sns:us-east-1:123456789012:frca-notifications-dev",
            "implicitDeny",
            "DistanceCalculatorToolRole must be denied SNS publish",
        ),
        (
            "DistanceCalculatorToolRole",
            "dynamodb:GetItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-recipients-dev",
            "implicitDeny",
            "DistanceCalculatorToolRole must be denied DynamoDB read",
        ),
        # --- 4. CapacityQueryToolRole ---
        (
            "CapacityQueryToolRole",
            "dynamodb:Query",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-recipients-dev",
            "allowed",
            "CapacityQueryToolRole must be allowed to query Recipients",
        ),
        (
            "CapacityQueryToolRole",
            "dynamodb:PutItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-donations-dev",
            "implicitDeny",
            "CapacityQueryToolRole must be denied writes to DonationsTable",
        ),
        (
            "CapacityQueryToolRole",
            "sns:Publish",
            "arn:aws:sns:us-east-1:123456789012:frca-notifications-dev",
            "implicitDeny",
            "CapacityQueryToolRole must be denied SNS publish",
        ),
        # --- 5. VolunteerDispatchToolRole ---
        (
            "VolunteerDispatchToolRole",
            "dynamodb:UpdateItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-volunteers-dev",
            "allowed",
            "VolunteerDispatchToolRole must be allowed to update volunteers",
        ),
        (
            "VolunteerDispatchToolRole",
            "sns:Publish",
            "arn:aws:sns:us-east-1:123456789012:frca-notifications-dev",
            "implicitDeny",
            "VolunteerDispatchToolRole must be denied direct SNS publish",
        ),
        (
            "VolunteerDispatchToolRole",
            "dynamodb:DeleteItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-volunteers-dev",
            "implicitDeny",
            "VolunteerDispatchToolRole must be denied DeleteItem",
        ),
        # --- 6. AgentExecutionRole (Runtime Core) ---
        (
            "AgentExecutionRole",
            "bedrock:InvokeModel",
            "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0",
            "allowed",
            "AgentExecutionRole must be allowed to invoke Bedrock foundation model",
        ),
        (
            "AgentExecutionRole",
            "dynamodb:DeleteItem",
            "arn:aws:dynamodb:us-east-1:123456789012:table/frca-sessions-memory-dev",
            "implicitDeny",
            "AgentExecutionRole must be denied DeleteItem on memory table",
        ),
    ]

    results: list[EvaluationResult] = []

    for role_name, action, resource, expected_decision, description in test_cases:
        role_res = resources[role_name]
        policies = extract_role_policies(role_res)
        result = simulate_role_action(
            role_name=role_name,
            policies=policies,
            action=action,
            resource_arn=resource,
        )
        results.append(result)

        # Explicitly verify decision matches expected least-privilege boundary
        assert result.decision == expected_decision, (
            f"Failed boundary for {role_name} on {action}: "
            f"expected {expected_decision}, got {result.decision} "
            f"(eval_source={result.eval_source}, reason={result.reason})"
        )

        # Assert evaluation source was explicitly captured as one of the valid paths
        assert result.eval_source in ("AWS_SIMULATE", "FALLBACK_ENGINE"), (
            f"Invalid evaluation source: {result.eval_source}"
        )

        # Print detailed execution diagnostic line
        print(
            f"[{result.eval_source}] Role: {role_name:26} Action: {action:22} "
            f"Decision: {result.decision:12} ({description})"
        )

    # Print human-readable summary of evaluation sources
    summary = format_evaluation_summary()
    print(summary)

    # Validate overall evaluation metrics
    metrics = EVALUATION_METRICS
    assert metrics["TOTAL"] == len(test_cases)
    assert metrics["TOTAL"] >= 20
    assert metrics["AWS_SIMULATE"] + metrics["FALLBACK_ENGINE"] == metrics["TOTAL"]


def test_iam_evaluator_aws_simulate_and_fallback_mock_paths() -> None:
    """Verify simulate_role_action correctly reports AWS_SIMULATE vs FALLBACK_ENGINE."""
    mock_policies = [
        {
            "PolicyName": "TestAllowPolicy",
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:UpdateItem"],
                        "Resource": ["arn:aws:dynamodb:*:*:table/frca-recipients-*"],
                    }
                ],
            },
        }
    ]

    target_arn = (
        "arn:aws:dynamodb:us-east-1:123456789012:table/frca-recipients-dev"
    )

    # 1. Path A: AWS IAM SimulateCustomPolicy succeeds
    mock_iam_client = MagicMock()
    mock_iam_client.simulate_custom_policy.return_value = {
        "EvaluationResults": [
            {
                "EvalActionName": "dynamodb:UpdateItem",
                "EvalResourceName": target_arn,
                "EvalDecision": "allowed",
            }
        ]
    }

    res_aws = simulate_role_action(
        role_name="MockRole",
        policies=mock_policies,
        action="dynamodb:UpdateItem",
        resource_arn=target_arn,
        iam_client=mock_iam_client,
    )
    assert res_aws.decision == "allowed"
    assert res_aws.eval_source == "AWS_SIMULATE"
    assert "SimulateCustomPolicy" in res_aws.reason

    # 2. Path B: AWS IAM returns AccessDenied -> Fallback Engine triggers
    mock_iam_denied = MagicMock()
    mock_iam_denied.simulate_custom_policy.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "User is not authorized"}},
        "SimulateCustomPolicy",
    )

    res_fallback = simulate_role_action(
        role_name="MockRole",
        policies=mock_policies,
        action="dynamodb:UpdateItem",
        resource_arn=target_arn,
        iam_client=mock_iam_denied,
    )
    assert res_fallback.decision == "allowed"
    assert res_fallback.eval_source == "FALLBACK_ENGINE"
    assert "deterministic fallback engine" in res_fallback.reason
    assert "AccessDenied" in res_fallback.reason


def test_static_iam_policy_audit_and_scoping() -> None:
    """Verify zero wildcards, zero DeleteItem, and scoped CloudWatch logs in YAML."""
    template = load_cloudformation_template(INFRA_TEMPLATE_PATH)
    resources = template.get("Resources", {})

    for res_name, res_def in resources.items():
        if res_def.get("Type") != "AWS::IAM::Role":
            continue

        policies = extract_role_policies(res_def)
        for policy in policies:
            policy_doc = policy.get("PolicyDocument", {})
            statements = policy_doc.get("Statement", [])
            if isinstance(statements, dict):
                statements = [statements]

            for stmt in statements:
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                resources_list = stmt.get("Resource", [])
                if isinstance(resources_list, str):
                    resources_list = [resources_list]

                # Assert NO naked wildcard actions
                assert "*" not in actions, (
                    f"Forbidden wildcard Action '*' in role {res_name}, "
                    f"policy {policy.get('PolicyName')}"
                )

                # Assert NO naked wildcard resources
                assert "*" not in resources_list, (
                    f"Forbidden wildcard Resource '*' in role {res_name}, "
                    f"policy {policy.get('PolicyName')}"
                )

                # Assert COMPLETE absence of dynamodb:DeleteItem
                assert "dynamodb:DeleteItem" not in actions, (
                    f"Violation: dynamodb:DeleteItem found in role {res_name}, "
                    f"policy {policy.get('PolicyName')}"
                )

                # Assert CloudWatch Logs statements are scoped to /aws/lambda/frca-*
                if "logs:PutLogEvents" in actions:
                    for res_arn in resources_list:
                        assert "log-group:/aws/lambda/frca-" in str(res_arn), (
                            f"CloudWatch Log resource {res_arn} in {res_name} "
                            f"is not scoped to /aws/lambda/frca-*"
                        )


def test_structured_json_logging_and_correlation_id_propagation() -> None:
    """Verify unified JSON log format, ISO timestamps, and correlation ID threading."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    formatter = StructuredJsonFormatter()
    handler.setFormatter(formatter)

    test_logger = get_structured_logger("test.observability.logger")
    test_logger.handlers = [handler]

    test_cid = "corr-test-xyz-987"
    token = CORRELATION_ID_CONTEXT.set(test_cid)

    try:
        test_logger.info(
            "Processing donation event",
            extra={"details": {"donation_id": "DON-999", "quantity_kg": 15.0}},
        )

        output = stream.getvalue().strip()
        record = json.loads(output)

        assert record["level"] == "INFO"
        assert record["logger"] == "test.observability.logger"
        assert record["correlation_id"] == test_cid
        assert record["message"] == "Processing donation event"
        assert record["details"]["donation_id"] == "DON-999"
        assert record["details"]["quantity_kg"] == 15.0
        assert "timestamp" in record
    finally:
        CORRELATION_ID_CONTEXT.reset(token)


def test_cloudwatch_pii_sanitization_and_redaction_integrity() -> None:
    """Verify plaintext phone numbers and addresses are strictly masked in logs."""
    raw_phone = "+1 (212) 555-0199"
    raw_address = "789 Broadway Ave, Suite 300, New York, NY"

    # Test inline text sanitization
    raw_msg = f"Dispatched volunteer to donor at {raw_address} with phone {raw_phone}"
    sanitized_message = sanitize_text_for_logging(raw_msg)

    assert raw_phone not in sanitized_message
    assert "0199" in sanitized_message  # Masked retains last 4 digits
    assert raw_address not in sanitized_message
    assert "[REDACTED_ADDRESS]" in sanitized_message

    # Test full StructuredJsonFormatter end-to-end output
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredJsonFormatter())

    audit_logger = get_structured_logger("audit.pii.test")
    audit_logger.handlers = [handler]

    audit_logger.info(
        f"Contact donor at {raw_phone} located at {raw_address}",
        extra={
            "details": {
                "donor_phone": raw_phone,
                "donor_address": raw_address,
                "contact_name": "Jane Doe",
            }
        },
    )

    raw_log = stream.getvalue()
    log_json = json.loads(raw_log.strip())

    # Critical Assertion: Plaintext PII strings MUST NEVER appear anywhere in output
    assert raw_phone not in raw_log
    assert raw_address not in raw_log
    assert log_json["details"]["donor_phone"] == "***-***-0199"
    assert log_json["details"]["contact_name"] == "J***"
    assert log_json["details"]["donor_address"].startswith("*** ")


def test_cloudwatch_dashboard_and_alarms_specification() -> None:
    """Verify CloudWatch Monitoring Dashboard widgets and alarm specifications."""
    template = load_cloudformation_template(INFRA_TEMPLATE_PATH)
    resources = template.get("Resources", {})

    # 1. CloudWatch Dashboard
    assert "CloudWatchMonitoringDashboard" in resources
    dashboard = resources["CloudWatchMonitoringDashboard"]
    dashboard_body_raw = dashboard["Properties"]["DashboardBody"]
    dashboard_json = json.loads(dashboard_body_raw)
    widgets = dashboard_json.get("widgets", [])
    assert len(widgets) == 6

    widget_titles = [w.get("properties", {}).get("title", "") for w in widgets]
    assert any("Invocations" in t for t in widget_titles)
    assert any("Latency" in t for t in widget_titles)
    assert any("Dead-Letter Queue" in t for t in widget_titles)
    assert any("Consumed Capacity" in t for t in widget_titles)
    assert any("Throttle" in t for t in widget_titles)
    assert any("Auth Violations" in t for t in widget_titles)

    # 2. Metric Filter
    assert "AuthViolationMetricFilter" in resources
    mf = resources["AuthViolationMetricFilter"]["Properties"]
    assert mf["FilterPattern"] == '"AUTH_VIOLATION"'
    assert mf["MetricTransformations"][0]["MetricName"] == "AuthViolationCount"

    # 3. Alarms
    assert "DLQMessagesVisibleAlarm" in resources
    dlq_alarm = resources["DLQMessagesVisibleAlarm"]["Properties"]
    assert dlq_alarm["MetricName"] == "ApproximateNumberOfMessagesVisible"
    assert dlq_alarm["Threshold"] == 0

    assert "RuntimeErrorRateAlarm" in resources
    err_alarm = resources["RuntimeErrorRateAlarm"]["Properties"]
    assert err_alarm["MetricName"] == "Errors"
    assert err_alarm["Threshold"] == 1

    assert "AuthViolationSurgeAlarm" in resources
    surge_alarm = resources["AuthViolationSurgeAlarm"]["Properties"]
    assert surge_alarm["MetricName"] == "AuthViolationCount"
    assert surge_alarm["Threshold"] == 5


def test_abuse_dos_simulation_across_public_endpoints() -> None:
    """Verify 100-request bursts on POST /donations & GET /summary trigger 429."""
    config = AppConfig(
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
        coordinator_api_key="valid-test-coordinator-key-32chars-min",
    )
    mock_dynamo = MagicMock()
    table_mock = MagicMock()
    mock_dynamo.Table.return_value = table_mock

    mock_donations_repo = MagicMock()
    mock_donations_repo.get_authoritative_daily_summary.return_value = MagicMock(
        total_kg_routed=125.0,
        meals_equivalent=250.0,
        organizations_served=3,
    )

    router = FrontendApiService(
        config=config,
        dynamodb_resource=mock_dynamo,
        donations_repo=mock_donations_repo,
    )

    # 1. Burst on POST /api/donations (Limit: 30/min per IP)
    burst_ip_1 = "198.51.100.42"
    donation_statuses: list[int] = []

    for i in range(1, 101):
        table_mock.update_item.return_value = {
            "Attributes": {"request_count": i, "ttl": 1800000000}
        }

        resp = router.handle_request(
            {
                "httpMethod": "POST",
                "path": "/api/donations",
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"invalid": "payload"}),
                "requestContext": {"identity": {"sourceIp": burst_ip_1}},
            }
        )
        donation_statuses.append(resp["statusCode"])

    # First 30 requests passed rate limiter (returning 400 validation error on body)
    assert all(s == 400 for s in donation_statuses[:30])
    # Next 70 requests strictly rejected with 429
    assert all(s == 429 for s in donation_statuses[30:])
    assert len(donation_statuses) == 100

    # 2. Burst on GET /api/summary (Limit: 30/min per IP)
    burst_ip_2 = "198.51.100.88"
    summary_statuses: list[int] = []

    for i in range(1, 101):
        table_mock.update_item.return_value = {
            "Attributes": {"request_count": i, "ttl": 1800000000}
        }

        resp = router.handle_request(
            {
                "httpMethod": "GET",
                "path": "/api/summary",
                "requestContext": {"identity": {"sourceIp": burst_ip_2}},
            }
        )
        summary_statuses.append(resp["statusCode"])

    # First 30 requests returned 200 OK
    assert all(s == 200 for s in summary_statuses[:30])
    # Next 70 requests strictly returned 429 Too Many Requests
    assert all(s == 429 for s in summary_statuses[30:])
    assert len(summary_statuses) == 100

