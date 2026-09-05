"""Secure notification tool with fixed templates and anti-injection sanitization."""

import html
import re
from typing import Any

from config import load_app_configuration
from models import NotificationMessage, NotificationRecipientType
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

    LOGGER.info(
        "Dispatching %s notification using %s (Correlation: %s)",
        recipient_type.value,
        template_id,
        correlation_id,
        extra={
            "details": {
                "recipient_type": recipient_type.value,
                "template_id": template_id,
                "destination": destination,
                "correlation_id": correlation_id,
            }
        },
    )

    if sns_client is not None and target_arn:
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

    return NotificationMessage(
        recipient_type=recipient_type,
        destination=destination,
        template_id=template_id,
        rendered_body=rendered_body,
        correlation_id=correlation_id,
    )
