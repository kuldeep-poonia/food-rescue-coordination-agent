"""Adversarial and security injection test suite for send_notification."""

from unittest import mock

import pytest

from models import NotificationRecipientType
from tools.send_notification import (
    sanitize_template_variable,
    send_notification,
)


def test_sanitize_template_variable_strips_injection_attacks() -> None:
    """Verify script tags, template syntax, and CRLF injections are neutralized."""
    # 1. Cross-Site Scripting (XSS) / script tags
    xss_payload = "<script>alert('XSS')</script>John Doe"
    sanitized_xss = sanitize_template_variable(xss_payload)
    assert "<script>" not in sanitized_xss
    assert "</script>" not in sanitized_xss
    assert "alert('XSS')" not in sanitized_xss
    assert "John Doe" in sanitized_xss

    # 2. Template injection expressions (${...}, {{...}}, <%...%>)
    tpl_payload = "Donor ${{7*7}} {{evil.payload}} <%rm -rf%> Bakery"
    sanitized_tpl = sanitize_template_variable(tpl_payload)
    assert "${{7*7}}" not in sanitized_tpl
    assert "{{evil.payload}}" not in sanitized_tpl
    assert "<%rm -rf%>" not in sanitized_tpl
    assert "Donor Bakery" in sanitized_tpl

    # 3. CRLF header injection payload
    crlf_payload = "Alice\r\nBcc: attacker@example.com\r\nSubject: Injected"
    sanitized_crlf = sanitize_template_variable(crlf_payload)
    assert "\r" not in sanitized_crlf
    assert "\n" not in sanitized_crlf
    assert "Alice Bcc: attacker@example.com Subject: Injected" in sanitized_crlf


def test_send_notification_renders_safely_with_malicious_inputs() -> None:
    """Hardcore requirement: submit malicious payloads and verify outgoing message."""
    malicious_donor_name = "<script>alert('pwn')</script>Malicious Donor"
    malicious_category = "{{food.override}}produce"

    notification = send_notification(
        recipient_type=NotificationRecipientType.RECIPIENT,
        destination="+12125550188",
        template_id="RECIPIENT_CONFIRMATION_V1",
        parameters={
            "contact_name": "Shelter Admin",
            "quantity_kg": 50.0,
            "food_category": malicious_category,
            "donor_name": malicious_donor_name,
            "volunteer_name": "Sam Driver",
        },
        correlation_id="corr-sec-test-01",
    )

    # Verification: Raw script tag must NOT exist in the rendered message
    assert "<script>" not in notification.rendered_body
    assert "</script>" not in notification.rendered_body
    assert "{{food.override}}" not in notification.rendered_body
    assert "Malicious Donor" in notification.rendered_body
    assert "produce" in notification.rendered_body
    assert notification.correlation_id == "corr-sec-test-01"


def test_send_notification_publishes_to_sns_mock() -> None:
    """Verify SNS client receives properly formatted message and attributes."""
    mock_sns = mock.MagicMock()

    send_notification(
        recipient_type=NotificationRecipientType.DONOR,
        destination="+12125550199",
        template_id="DONOR_CONFIRMATION_V1",
        parameters={
            "donor_name": "Downtown Bakery",
            "quantity_kg": 25.0,
            "food_category": "bakery",
            "recipient_name": "Community Table",
            "volunteer_name": "Taylor Transit",
            "ready_by": "2:00 PM UTC",
        },
        correlation_id="corr-donor-01",
        sns_client=mock_sns,
        topic_arn="arn:aws:sns:us-east-1:123456789012:test-topic",
    )

    mock_sns.publish.assert_called_once()
    kwargs = mock_sns.publish.call_args[1]
    assert kwargs["TopicArn"] == "arn:aws:sns:us-east-1:123456789012:test-topic"
    assert "Downtown Bakery" in kwargs["Message"]
    attrs = kwargs["MessageAttributes"]
    assert attrs["CorrelationId"]["StringValue"] == "corr-donor-01"


def test_send_notification_rejects_unknown_template_and_missing_params() -> None:
    """Verify invalid templates and incomplete parameters raise clear ValueErrors."""
    with pytest.raises(ValueError, match="Unknown notification template"):
        send_notification(
            recipient_type=NotificationRecipientType.VOLUNTEER,
            destination="+12125550177",
            template_id="NON_EXISTENT_TEMPLATE_V99",
            parameters={},
        )

    with pytest.raises(ValueError, match="Missing required parameter"):
        send_notification(
            recipient_type=NotificationRecipientType.DONOR,
            destination="+12125550199",
            template_id="DONOR_CONFIRMATION_V1",
            parameters={"donor_name": "Bakery"},  # Missing quantity_kg, etc.
        )
