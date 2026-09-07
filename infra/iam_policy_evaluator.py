"""IAM Least-Privilege Policy Evaluator for FRCA Infrastructure.

Evaluates IAM execution policies against both positive (allowed) and
negative (denied) authorization boundaries. Official AWS IAM
SimulateCustomPolicy API serves as the authoritative verification path,
with transparent fallback to a deterministic evaluator for the project's
supported IAM policy subset when live AWS credentials or simulation
permissions are unavailable.
"""

import fnmatch
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from botocore.exceptions import BotoCoreError, ClientError

from tools.logging_utils import get_structured_logger

LOGGER: logging.Logger = get_structured_logger(__name__)

# Evaluation metrics tracking real AWS API calls vs deterministic fallback engine
EVALUATION_METRICS: dict[str, int] = {
    "AWS_SIMULATE": 0,
    "FALLBACK_ENGINE": 0,
    "TOTAL": 0,
}


@dataclass
class EvaluationResult:
    """Encapsulates the outcome of an IAM policy simulation evaluation.

    Attributes:
        role_name: Identifier of the IAM execution role evaluated.
        action: IAM action string evaluated (e.g. dynamodb:UpdateItem).
        resource_arn: Target AWS Resource ARN evaluated.
        decision: Decision outcome: 'allowed', 'implicitDeny', or 'explicitDeny'.
        eval_source: Evaluation path: 'AWS_SIMULATE' or 'FALLBACK_ENGINE'.
        matched_policy_name: Name of the inline policy that determined outcome.
        reason: Diagnostic explanation of evaluation path and matched rules.
    """

    role_name: str
    action: str
    resource_arn: str
    decision: str
    eval_source: str
    matched_policy_name: str | None = None
    reason: str = ""


class CloudFormationYamlLoader(yaml.SafeLoader):
    """Custom YAML loader handling standard CloudFormation intrinsic function tags."""

    pass


def _cfn_tag_constructor(loader: yaml.Loader, node: yaml.Node) -> Any:
    """Construct CloudFormation intrinsic tag representations safely.

    Args:
        loader: Current YAML loader instance.
        node: AST Node for scalar, sequence, or mapping.

    Returns:
        Constructed Python representation for the intrinsic tag.
    """
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


for _tag in (
    "!Sub",
    "!Ref",
    "!GetAtt",
    "!Join",
    "!Select",
    "!Split",
    "!FindInMap",
    "!Base64",
    "!GetAZs",
    "!ImportValue",
    "!Condition",
    "!And",
    "!Or",
    "!Not",
    "!Equals",
    "!If",
):
    CloudFormationYamlLoader.add_constructor(_tag, _cfn_tag_constructor)


def load_cloudformation_template(template_path: Path | str) -> dict[str, Any]:
    """Parse and load a CloudFormation template with intrinsic functions.

    Args:
        template_path: Absolute or relative file path to the YAML template.

    Returns:
        Parsed dictionary structure of the template.

    Raises:
        FileNotFoundError: If the template path does not exist.
        yaml.YAMLError: If parsing fails.
    """
    path = Path(template_path)
    if not path.is_file():
        raise FileNotFoundError(f"CloudFormation template not found at {path}")

    with path.open("r", encoding="utf-8") as file_stream:
        data: Any = yaml.load(file_stream, Loader=CloudFormationYamlLoader)
        if not isinstance(data, dict):
            return {}
        return data


def extract_role_policies(role_resource: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract inline policies list from a CloudFormation IAM Role resource dictionary.

    Args:
        role_resource: The CloudFormation resource mapping for AWS::IAM::Role.

    Returns:
        List of policy dictionaries containing PolicyName and PolicyDocument.
    """
    properties = role_resource.get("Properties", {})
    policies = properties.get("Policies", [])
    if isinstance(policies, list):
        return [p for p in policies if isinstance(p, dict)]
    return []


def _normalize_string_list(val: Any) -> list[str]:
    """Normalize single string or list of strings to list of strings."""
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return [str(v) for v in val]
    return []


RESOURCE_MAPPINGS: dict[str, str] = {
    "SessionsMemoryTable.Arn": "arn:aws:dynamodb:*:*:table/frca-sessions-memory-*",
    "SessionsMemoryTable": "arn:aws:dynamodb:*:*:table/frca-sessions-memory-*",
    "NotificationTopic": "arn:aws:sns:*:*:frca-notifications-*",
    "CoordinatorEscalationTopic": "arn:aws:sns:*:*:frca-escalations-*",
    "CoordinatorDLQ.Arn": "arn:aws:sqs:*:*:frca-coordinator-dlq-*",
    "CoordinatorDLQ": "arn:aws:sqs:*:*:frca-coordinator-dlq-*",
}


def _normalize_resource_pattern(pattern: str) -> str:
    """Normalize CloudFormation resource references and variable substitutions."""
    import re

    if pattern in RESOURCE_MAPPINGS:
        return RESOURCE_MAPPINGS[pattern]
    return re.sub(r"\$\{[^}]+\}", "*", pattern)


def _normalize_policy_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Clone and normalize statement resource strings for AWS IAM API compatibility."""
    import copy

    normalized = copy.deepcopy(doc)
    statements = normalized.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
        normalized["Statement"] = statements
    for stmt in statements:
        if isinstance(stmt, dict) and "Resource" in stmt:
            res = stmt["Resource"]
            if isinstance(res, str):
                stmt["Resource"] = _normalize_resource_pattern(res)
            elif isinstance(res, list):
                stmt["Resource"] = [_normalize_resource_pattern(r) for r in res]
    return normalized


def _matches_action(pattern: str, target_action: str) -> bool:
    """Check if target IAM action matches a policy action pattern (case-insensitive)."""
    return fnmatch.fnmatchcase(target_action.lower(), pattern.lower())


def _matches_resource(pattern: str, target_arn: str) -> bool:
    """Check if target Resource ARN matches a policy resource pattern."""
    normalized_pattern = _normalize_resource_pattern(pattern)
    if normalized_pattern == "*":
        return True
    if fnmatch.fnmatchcase(target_arn, normalized_pattern):
        return True

    # Account for wildcard substitutions or trailing sub-resources
    if normalized_pattern.endswith("/*"):
        prefix = normalized_pattern[:-2]
        if target_arn.startswith(prefix):
            return True

    return False


def evaluate_policy_locally(
    policies: list[dict[str, Any]],
    action: str,
    resource_arn: str,
) -> tuple[str, str | None, str]:
    """Deterministic evaluator for the project's supported IAM policy subset.

    NOTE: Official AWS iam:SimulateCustomPolicy remains the authoritative
    verification path for deployed AWS IAM behavior. This local evaluator
    provides deterministic offline evaluation for the project's specific policy
    statements (Allow/Deny effects, action pattern matching, and resource ARN
    prefix/wildcard matching) when AWS credentials or simulate permissions are
    unavailable, without claiming universal AWS specification compliance.

    Evaluation logic for supported policy subset:
    1. Default is implicitDeny.
    2. Any matching statement with Effect=Deny results in immediate explicitDeny.
    3. If no explicit Deny, any matching statement with Effect=Allow results in allowed.
    4. If no statement matches, outcome remains implicitDeny.

    Args:
        policies: List of inline policy dictionaries with PolicyDocument.
        action: The evaluated IAM action string.
        resource_arn: The target resource ARN string.

    Returns:
        Tuple of (decision, matched_policy_name, explanation_reason).
    """
    allow_matches: list[tuple[str, dict[str, Any]]] = []

    for policy in policies:
        policy_name = policy.get("PolicyName", "InlinePolicy")
        doc = policy.get("PolicyDocument", {})
        statements = doc.get("Statement", [])
        if isinstance(statements, dict):
            statements = [statements]

        for stmt in statements:
            if not isinstance(stmt, dict):
                continue
            effect = stmt.get("Effect", "Allow")
            actions = _normalize_string_list(stmt.get("Action", []))
            resources = _normalize_string_list(stmt.get("Resource", []))

            action_matched = any(_matches_action(act, action) for act in actions)
            resource_matched = any(
                _matches_resource(res, resource_arn) for res in resources
            )

            if action_matched and resource_matched:
                if effect == "Deny":
                    return (
                        "explicitDeny",
                        policy_name,
                        f"Explicit Deny matched in policy '{policy_name}'",
                    )
                if effect == "Allow":
                    allow_matches.append((policy_name, stmt))

    if allow_matches:
        matched_name = allow_matches[0][0]
        return (
            "allowed",
            matched_name,
            f"Allowed by matching statement in policy '{matched_name}'",
        )

    return (
        "implicitDeny",
        None,
        "No matching Allow statement found across attached policies (implicit deny)",
    )


_AWS_SIMULATE_AVAILABLE: bool | None = None
_AWS_UNAVAILABLE_REASON: str = ""


def simulate_role_action(
    role_name: str,
    policies: list[dict[str, Any]],
    action: str,
    resource_arn: str,
    iam_client: Any | None = None,
) -> EvaluationResult:
    """Evaluate whether an IAM role allows or denies an action on a resource.

    Attempts live evaluation via AWS IAM simulate_custom_policy API. If AWS returns
    AccessDenied (permissions missing) or network/credential errors, transparently
    falls back to the deterministic local policy evaluator and explicitly records
    eval_source as 'FALLBACK_ENGINE'.

    Args:
        role_name: Identifier for the role under evaluation.
        policies: List of inline policies attached to the role.
        action: Evaluated IAM action (e.g. 'dynamodb:UpdateItem').
        resource_arn: Target resource ARN.
        iam_client: Optional injected boto3 IAM client.

    Returns:
        EvaluationResult specifying decision and evaluation source.
    """
    global _AWS_SIMULATE_AVAILABLE, _AWS_UNAVAILABLE_REASON

    # 1. Attempt AWS IAM SimulateCustomPolicy API
    aws_attempt_error: str | None = None
    should_attempt_aws = (
        iam_client is not None
        or (_AWS_SIMULATE_AVAILABLE is not False and _can_attempt_aws_simulate())
    )

    if should_attempt_aws:
        try:
            client = iam_client or _get_default_iam_client()
            policy_documents = [
                json.dumps(_normalize_policy_document(p.get("PolicyDocument", {})))
                for p in policies
                if "PolicyDocument" in p and isinstance(p.get("PolicyDocument"), dict)
            ]

            if policy_documents:
                response = client.simulate_custom_policy(
                    PolicyInputList=policy_documents,
                    ActionNames=[action],
                    ResourceArns=[resource_arn],
                )
                results = response.get("EvaluationResults", [])
                if results:
                    if iam_client is None:
                        _AWS_SIMULATE_AVAILABLE = True
                    decision = results[0].get("EvalDecision", "implicitDeny")
                    EVALUATION_METRICS["AWS_SIMULATE"] += 1
                    EVALUATION_METRICS["TOTAL"] += 1
                    LOGGER.info(
                        "IAM Policy Evaluation [AWS_SIMULATE]: "
                        "role=%s action=%s decision=%s",
                        role_name,
                        action,
                        decision,
                    )
                    return EvaluationResult(
                        role_name=role_name,
                        action=action,
                        resource_arn=resource_arn,
                        decision=decision,
                        eval_source="AWS_SIMULATE",
                        matched_policy_name=None,
                        reason="Verified via official AWS IAM SimulateCustomPolicy API",
                    )
        except (ClientError, BotoCoreError, Exception) as exc:
            aws_attempt_error = str(exc)
            if iam_client is None:
                _AWS_SIMULATE_AVAILABLE = False
                _AWS_UNAVAILABLE_REASON = aws_attempt_error
            LOGGER.warning(
                "AWS IAM simulate_custom_policy call failed (%s). "
                "Falling back to deterministic policy engine.",
                aws_attempt_error,
            )
    elif _AWS_SIMULATE_AVAILABLE is False and iam_client is None:
        aws_attempt_error = _AWS_UNAVAILABLE_REASON

    # 2. Resilient Deterministic Fallback Engine
    decision, matched_policy, reason = evaluate_policy_locally(
        policies, action, resource_arn
    )
    EVALUATION_METRICS["FALLBACK_ENGINE"] += 1
    EVALUATION_METRICS["TOTAL"] += 1

    advisory = (
        f"Evaluated via deterministic fallback engine for supported policy subset "
        f"(authoritative AWS attempt reason: {aws_attempt_error})"
        if aws_attempt_error
        else "Evaluated via deterministic fallback engine for supported policy subset"
    )

    LOGGER.info(
        "IAM Policy Evaluation [FALLBACK_ENGINE]: "
        "role=%s action=%s decision=%s (matched: %s)",
        role_name,
        action,
        decision,
        matched_policy,
    )

    return EvaluationResult(
        role_name=role_name,
        action=action,
        resource_arn=resource_arn,
        decision=decision,
        eval_source="FALLBACK_ENGINE",
        matched_policy_name=matched_policy,
        reason=f"{reason}. {advisory}",
    )


def _can_attempt_aws_simulate() -> bool:
    """Check if boto3 is present and environment has AWS credentials configured."""
    try:
        import boto3

        session = boto3.Session()
        return session.get_credentials() is not None
    except Exception:
        return False


def _get_default_iam_client() -> Any:
    """Instantiate default boto3 IAM client."""
    import boto3

    return boto3.client("iam")


def get_evaluation_metrics() -> dict[str, int]:
    """Retrieve counts of assertions evaluated via AWS vs fallback engine."""
    return dict(EVALUATION_METRICS)


def reset_evaluation_metrics() -> None:
    """Reset evaluation metrics counters."""
    EVALUATION_METRICS["AWS_SIMULATE"] = 0
    EVALUATION_METRICS["FALLBACK_ENGINE"] = 0
    EVALUATION_METRICS["TOTAL"] = 0


def format_evaluation_summary() -> str:
    """Generate human-readable summary table of IAM evaluation verification paths."""
    total = EVALUATION_METRICS["TOTAL"]
    aws = EVALUATION_METRICS["AWS_SIMULATE"]
    fallback = EVALUATION_METRICS["FALLBACK_ENGINE"]
    pct_aws = (aws / total * 100.0) if total > 0 else 0.0
    pct_fallback = (fallback / total * 100.0) if total > 0 else 0.0

    return (
        "\n=======================================================\n"
        "           IAM LEAST-PRIVILEGE EVALUATION SUMMARY      \n"
        "=======================================================\n"
        f"Total Assertions Evaluated              : {total}\n"
        f"Live AWS Authoritative Verifications   : {aws} ({pct_aws:.1f}%)\n"
        f"Supported-Subset Fallback Executions    : {fallback} ({pct_fallback:.1f}%)\n"
        "=======================================================\n"
    )
