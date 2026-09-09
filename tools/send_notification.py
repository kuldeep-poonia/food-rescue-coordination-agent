"""Secure notification tool with fixed templates and anti-injection sanitization."""

import html
import re
import time
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from config import load_app_configuration
from models import (
    NotificationDeliveryError,
    NotificationMessage,
    NotificationRecipientType,
)
from tools.logging_utils import get_structured_logger

LOGGER = get_structured_logger(__name__)

# Fixed, versioned transactional notification templates
TEMPLATES: dict[str, str] = {
    "DONOR_CONFIRMATION_V1": (
        "FRCA: Hello {donor_name}, your donation of {quantity_kg}kg {food_category} "
        "is matched with {recipient_name}. Volunteer {volunteer_name} will pick it up "
        "by {ready_by}."
    ),
    "RECIPIENT_CONFIRMATION_V1": (
        "FRCA: Hello {contact_name}, a delivery of {quantity_kg}kg {food_category} "
        "is scheduled from {donor_name}. Volunteer {volunteer_name} is assigned."
    ),
    "VOLUNTEER_ASSIGNMENT_V1": (
        "FRCA: Hello {volunteer_name}, rescue run assigned: Pick up {quantity_kg}kg "
        "{food_category} at {donor_address} by {ready_by}. Deliver to "
        "{recipient_name} at {recipient_address}."
    ),
    "COORDINATOR_ESCALATION_V1": (
        "FRCA ALERT: Escalation on donation {donation_id}. "
        "Reason: {escalation_reason}. Summary: {summary}."
    ),
}

# Regex to detect and strip script tags, HTML tags, and template injection sequences
HTML_TAG_REGEX: re.Pattern[str] = re.compile(r"<[^>]*>", re.IGNORECASE)
TEMPLATE_EXPR_REGEX: re.Pattern[str] = re.compile(
    r"(\$\{.*?\}|\{\{.*?\}\}|<%.*?%>|\{.*?\}|\[\[.*?\]\])", re.DOTALL
)
DANGEROUS_CHARS_REGEX: re.Pattern[str] = re.compile(r"[\r\n\t\x00-\x1f\x7f-\x9f]")

# Known, safe AWS SNS error code allowlist mapping to sanitized descriptions
ALLOWLISTED_SNS_ERROR_CODES: dict[str, str] = {
    "ThrottlingException": "ThrottlingException: Request was throttled by AWS SNS",
    "Throttling": "Throttling: Request was throttled by AWS SNS",
    "ProvisionedThroughputExceededException": (
        "ProvisionedThroughputExceeded: SNS throughput exceeded"
    ),
    "InternalErrorException": (
        "InternalErrorException: Downstream AWS SNS service error"
    ),
    "InternalError": "InternalError: Downstream AWS SNS service error",
    "ServiceUnavailable": "ServiceUnavailable: AWS SNS service temporarily unavailable",
    "ServiceUnavailableException": (
        "ServiceUnavailableException: AWS SNS service temporarily unavailable"
    ),
    "RequestLimitExceeded": "RequestLimitExceeded: Request limit exceeded",
    "TooManyRequestsException": "TooManyRequests: Too many requests sent to AWS SNS",
    "InvalidParameterException": (
        "InvalidParameterException: Invalid notification parameter or endpoint"
    ),
    "InvalidParameter": (
        "InvalidParameter: Invalid notification parameter or endpoint"
    ),
    "ParameterValueInvalid": "ParameterValueInvalid: Parameter value is invalid",
    "AuthorizationErrorException": (
        "AuthorizationErrorException: Access denied publishing to SNS topic"
    ),
    "AuthorizationError": (
        "AuthorizationError: Access denied publishing to SNS topic"
    ),
    "AccessDeniedException": (
        "AccessDeniedException: Access denied publishing to SNS topic"
    ),
    "AccessDenied": "AccessDenied: Access denied publishing to SNS topic",
    "EndpointDisabledException": (
        "EndpointDisabledException: Target notification endpoint is disabled"
    ),
    "EndpointDisabled": (
        "EndpointDisabled: Target notification endpoint is disabled"
    ),
    "NotFoundException": (
        "NotFoundException: Target SNS topic or subscription not found"
    ),
    "NotFound": "NotFound: Target SNS topic or subscription not found",
    "TimeoutException": (
        "TimeoutException: Connection timeout publishing to AWS SNS"
    ),
    "ConnectTimeoutError": (
        "ConnectTimeoutError: Connection timeout publishing to AWS SNS"
    ),
    "ReadTimeoutError": "ReadTimeoutError: Read timeout publishing to AWS SNS",
    "EndpointConnectionError": (
        "EndpointConnectionError: Network error reaching AWS SNS endpoint"
    ),
}

DEFAULT_SAFE_ERROR_DETAIL: str = "DownstreamDeliveryError: Notification delivery failed"

RETRYABLE_SNS_ERROR_CODES: frozenset[str] = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
        "InternalError",
        "InternalErrorException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
    }
)


def mask_destination(destination: str) -> str:
    """Mask destination address (phone or email) preserving only edge identifiers.

    Guarantees that raw recipient contact numbers or emails are never written
    to log streams or exception diagnostic traces.
    """
    raw = str(destination).strip()
    if not raw:
        return "***"
    if "@" in raw:
        parts = raw.split("@", 1)
        name = parts[0]
        domain = parts[1] if len(parts) > 1 else ""
        masked_name = f"{name[0]}***" if len(name) > 1 else "***"
        return f"{masked_name}@{domain}"
    if len(raw) <= 4:
        return "****"
    return f"{raw[:2]}*****{raw[-4:]}"


def sanitize_sns_error_detail(exc: Exception) -> str:
    """Extract allowlisted error code without raw exception message or payload dump.

    Strictly protects against leaking PII, payload fragments, or sensitive AWS
    diagnostics that may exist in raw exception strings or boto3 responses.
    """
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ALLOWLISTED_SNS_ERROR_CODES:
            return ALLOWLISTED_SNS_ERROR_CODES[code]
        if code and code.replace("_", "").isalnum():
            return f"{code}: Downstream AWS SNS service error"
        return DEFAULT_SAFE_ERROR_DETAIL

    exc_class_name = exc.__class__.__name__
    if exc_class_name in ALLOWLISTED_SNS_ERROR_CODES:
        return ALLOWLISTED_SNS_ERROR_CODES[exc_class_name]

    if isinstance(exc, TimeoutError | ConnectionError | BotoCoreError):
        return f"{exc_class_name}: Network or transport connection failure"

    return DEFAULT_SAFE_ERROR_DETAIL


def is_retryable_sns_error(exc: Exception) -> bool:
    """Classify whether an SNS error is transient and eligible for a single retry."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in RETRYABLE_SNS_ERROR_CODES
    return isinstance(exc, TimeoutError | ConnectionError | BotoCoreError)


def is_ambiguous_transport_error(exc: Exception) -> bool:
    """Classify whether an exception indicates an ambiguous external transport outcome.

    Occurs when the request was transmitted or socket connection initiated,
    but response was not received or connection timed out, so external delivery
    status cannot be definitively known.
    """
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    exc_name = exc.__class__.__name__
    if exc_name in (
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "EndpointConnectionError",
        "SocketTimeout",
    ):
        return True
    msg = str(exc).lower()
    return "timeout" in msg or "timed out" in msg or "connection reset" in msg



def sanitize_template_variable(value: Any) -> str:
    """Strip HTML, script tags, template expressions, and control characters.

    Prevents SMS/WhatsApp injection, email header injections, and template engine
    expression evaluation attacks.

    Args:
        value: Any primitive value passed for template rendering.

    Returns:
        Escaped and sanitized plain-text string.
    """
    raw_str = str(value)

    # 1. Unescape first in case input was double-encoded, then strip HTML/script tags
    unescaped = html.unescape(raw_str)
    no_html = HTML_TAG_REGEX.sub("", unescaped)

    # 2. Strip template injection syntax (${...}, {{...}}, <%...%>, etc.)
    no_templates = TEMPLATE_EXPR_REGEX.sub("", no_html)

    # 3. Strip any stray curly braces so str.format() cannot be spoofed
    no_braces = no_templates.replace("{", "").replace("}", "")

    # 4. Strip control characters (CRLF header injection prevention)
    sanitized_text = DANGEROUS_CHARS_REGEX.sub(" ", no_braces)

    # 5. Collapse repeated whitespace and strip edges
    collapsed = " ".join(sanitized_text.split())

    # 6. Escape remaining HTML entities for defense in depth
    return html.escape(collapsed, quote=True)


def send_notification(
    recipient_type: NotificationRecipientType,
    destination: str,
    template_id: str,
    parameters: dict[str, Any],
    correlation_id: str = "unassigned",
    sns_client: Any | None = None,
    topic_arn: str | None = None,
) -> NotificationMessage:
    """Render a fixed template with sanitized variables and publish via Amazon SNS.

    Args:
        recipient_type: Category of notification recipient.
        destination: Phone number, email, or endpoint address.
        template_id: Pre-registered template identifier.
        parameters: Dynamic variables to populate in the template.
        correlation_id: Unique lifecycle trace identifier.
        sns_client: Optional pre-configured boto3 SNS client.
        topic_arn: Optional SNS Topic ARN (defaults from AppConfig).

    Returns:
        Rendered and validated NotificationMessage model.

    Raises:
        ValueError: If template_id is unknown or required variables are missing.
    """
    if template_id not in TEMPLATES:
        raise ValueError(
            f"Unknown notification template '{template_id}'. "
            f"Allowed templates: {list(TEMPLATES.keys())}"
        )

    template = TEMPLATES[template_id]

    # Sanitize every parameter value individually
    sanitized_params: dict[str, str] = {
        key: sanitize_template_variable(val) for key, val in parameters.items()
    }

    try:
        rendered_body = template.format(**sanitized_params)
    except KeyError as exc:
        raise ValueError(
            f"Missing required parameter {exc} for template '{template_id}'"
        ) from exc

    config = load_app_configuration()
    target_arn = (
        topic_arn
        if topic_arn is not None
        else (
            config.coordinator_escalation_topic_arn
            if recipient_type == NotificationRecipientType.COORDINATOR
            else config.notification_topic_arn
        )
    )

    masked_dest = mask_destination(destination)

    LOGGER.info(
        "Dispatching %s notification using %s (Correlation: %s)",
        recipient_type.value,
        template_id,
        correlation_id,
        extra={
            "details": {
                "recipient_type": recipient_type.value,
                "template_id": template_id,
                "destination": masked_dest,
                "correlation_id": correlation_id,
            }
        },
    )

    if sns_client is not None and target_arn:
        max_attempts = 2
        for attempt in range(max_attempts):
            try:
                sns_client.publish(
                    TopicArn=target_arn,
                    Message=rendered_body,
                    Subject=f"FRCA Notification: {template_id}",
                    MessageAttributes={
                        "RecipientType": {
                            "DataType": "String",
                            "StringValue": recipient_type.value,
                        },
                        "CorrelationId": {
                            "DataType": "String",
                            "StringValue": correlation_id,
                        },
                    },
                )
                break
            except Exception as exc:
                safe_detail = sanitize_sns_error_detail(exc)
                if is_retryable_sns_error(exc) and attempt == 0:
                    LOGGER.warning(
                        "Transient error publishing %s to %s (%s); retrying once",
                        recipient_type.value,
                        masked_dest,
                        safe_detail,
                        extra={"correlation_id": correlation_id},
                    )
                    time.sleep(0.1)
                    continue

                LOGGER.error(
                    "Failed to deliver %s notification to %s: %s",
                    recipient_type.value,
                    masked_dest,
                    safe_detail,
                    extra={"correlation_id": correlation_id},
                )
                raise NotificationDeliveryError(
                    message=(
                        f"Notification delivery failed for {recipient_type.value}: "
                        f"{safe_detail}"
                    ),
                    recipient_type=recipient_type.value,
                    masked_destination=masked_dest,
                    safe_error_detail=safe_detail,
                    is_ambiguous=is_ambiguous_transport_error(exc),
                ) from None


    return NotificationMessage(
        recipient_type=recipient_type,
        destination=destination,
        template_id=template_id,
        rendered_body=rendered_body,
        correlation_id=correlation_id,
    )
