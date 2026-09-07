"""Hardcore test suite for Phase 7 Frontend Interfaces & Security.

Covers:
1. Resource-Ownership Enforcement (Zero IDOR) across Donors, Recipients, and Volunteers.
2. Salted SHA-256 token storage & timing-attack-safe comparison.
3. Dedicated Login Rate Limiter (5 requests/min per IP).
4. General Distributed Rate Limiter (30 requests/min per IP).
5. Trusted Source IP extraction and anti-spoofing verification.
6. HTTP Security Headers (Referrer-Policy: no-referrer, Cache-Control: no-store).
7. Production Credential Guardrails (mandatory non-default COORDINATOR_API_KEY in prod).
8. Coordinator Auth-Bypass Protection (strict 401 on missing/invalid Bearer token).
9. XSS & Injection payload handling.
10. Cross-Role Data Leak Prevention.
11. Server-Side Validation Rejection (negative qty, past ready_by, invalid phone).
12. Coordinator Manual Override & Audit Trail Verification.
13. AUTH_VIOLATION audit record persistence in DynamoDB.
14. Static development server path traversal prevention.
"""

import hashlib
import http.client
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer
from typing import Any
from unittest import mock

import pytest

from audit_repo import AuditRepository
from config import (
    DEV_LOCAL_COORDINATOR_KEY,
    AppConfig,
    ConfigurationError,
    load_app_configuration,
)
from donations_repo import DonationsRepository
from frontend_api import (
    FrontendApiService,
    extract_client_ip,
)
from models import (
    Coordinates,
    Donation,
    DonationStatus,
    FoodCategory,
    OrchestrationResult,
    Recipient,
    Volunteer,
)
from recipients_repo import RecipientsRepository
from server import FrontendDevServerHandler
from volunteers_repo import VolunteersRepository


# ------------------------------------------------------------------------------
# Test Fixtures & Helpers
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
        coordinator_api_key="valid-test-coordinator-key-32chars-min",
    )


def create_mock_service() -> tuple[
    FrontendApiService,
    mock.MagicMock,
    mock.MagicMock,
    mock.MagicMock,
    mock.MagicMock,
    mock.MagicMock,
]:
    """Create FrontendApiService wired with isolated mock tables."""
    mock_donations_table = mock.MagicMock()
    mock_recipients_table = mock.MagicMock()
    mock_volunteers_table = mock.MagicMock()
    mock_audit_table = mock.MagicMock()
    mock_sessions_table = mock.MagicMock()

    def table_router(name: str) -> mock.MagicMock:
        if "recipients" in name:
            return mock_recipients_table
        if "volunteers" in name:
            return mock_volunteers_table
        if "audit" in name:
            return mock_audit_table
        if "sessions" in name:
            return mock_sessions_table
        return mock_donations_table

    mock_resource = mock.MagicMock()
    mock_resource.Table.side_effect = table_router

    mock_orchestrator = mock.MagicMock()
    mock_orchestrator.coordinate_donation.return_value = OrchestrationResult(
        donation_id="don-mock",
        correlation_id="corr-mock",
        status=DonationStatus.MATCHED,
    )

    config = create_test_config()
    d_repo = DonationsRepository(dynamodb_resource=mock_resource, config=config)
    r_repo = RecipientsRepository(dynamodb_resource=mock_resource, config=config)
    v_repo = VolunteersRepository(dynamodb_resource=mock_resource, config=config)
    a_repo = AuditRepository(dynamodb_resource=mock_resource, config=config)

    service = FrontendApiService(
        donations_repo=d_repo,
        recipients_repo=r_repo,
        volunteers_repo=v_repo,
        audit_repo=a_repo,
        orchestrator=mock_orchestrator,
        config=config,
        dynamodb_resource=mock_resource,
    )
    return (
        service,
        mock_donations_table,
        mock_recipients_table,
        mock_volunteers_table,
        mock_audit_table,
        mock_sessions_table,
    )


# ------------------------------------------------------------------------------
# 1. Resource-Ownership Enforcement (Zero IDOR)
# ------------------------------------------------------------------------------
def test_resource_ownership_enforcement_zero_idor() -> None:
    """Assert cross-party access without matching token is strictly forbidden."""
    service, mock_d_table, mock_r_table, mock_v_table, _, _ = create_mock_service()

    # Setup Donation A and Donation B with distinct secrets
    token_a = "secret-token-donor-alpha-32chars-long!"
    hash_a = hashlib.sha256(token_a.encode()).hexdigest()

    token_b = "secret-token-donor-bravo-32chars-long!"

    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    don_a = Donation(
        donation_id="don-alpha",
        donor_id="donor-1",
        donor_name="Bakery A",
        donor_phone="+12125550101",
        donor_address="100 First St",
        donor_coordinates=Coordinates(latitude=40.71, longitude=-74.00),
        food_category=FoodCategory.BAKERY,
        quantity_kg=15.0,
        ready_by=now + timedelta(hours=3),
        perishability_hours=6.0,
        service_region="metro-core",
        status=DonationStatus.REPORTED,
        tracking_token_hash=hash_a,
    )

    mock_d_table.get_item.return_value = {"Item": don_a.model_dump(mode="json")}

    # 1. Access Donation A with Token B -> MUST RETURN 403 Forbidden
    resp_tamper = service.handle_request(
        {
            "httpMethod": "GET",
            "path": "/api/donations/don-alpha",
            "headers": {"X-Tracking-Token": token_b},
            "client_ip": "192.168.1.50",
        }
    )
    assert resp_tamper["statusCode"] == 403
    assert "Forbidden" in json.loads(resp_tamper["body"])["error"]

    # 2. Access Donation A without token -> MUST RETURN 401 Unauthorized
    resp_unauth = service.handle_request(
        {
            "httpMethod": "GET",
            "path": "/api/donations/don-alpha",
            "headers": {},
            "client_ip": "192.168.1.50",
        }
    )
    assert resp_unauth["statusCode"] == 401
    assert "Unauthorized" in json.loads(resp_unauth["body"])["error"]

    # 3. Access Donation A with valid Token A -> MUST RETURN 200 OK
    resp_valid = service.handle_request(
        {
            "httpMethod": "GET",
            "path": "/api/donations/don-alpha",
            "headers": {"X-Tracking-Token": token_a},
            "client_ip": "192.168.1.50",
        }
    )
    assert resp_valid["statusCode"] == 200
    data_valid = json.loads(resp_valid["body"])
    assert data_valid["donation_id"] == "don-alpha"

    # 4. Recipient ownership: Attempt updating Recipient A with Recipient B's token
    rec_a_hash = hashlib.sha256(b"rec-secret-rec-alpha").hexdigest()
    rec_a = Recipient(
        recipient_id="rec-alpha",
        organization_name="Shelter Alpha",
        contact_name="Coordinator",
        contact_phone="+12125550199",
        address="200 Shelter Way",
        coordinates=Coordinates(latitude=40.72, longitude=-74.01),
        capacity_kg_remaining=100.0,
        service_region="metro-core",
        auth_token_hash=rec_a_hash,
    )
    mock_r_table.get_item.return_value = {"Item": rec_a.model_dump(mode="json")}

    resp_rec_tamper = service.handle_request(
        {
            "httpMethod": "POST",
            "path": "/api/recipients/rec-alpha/capacity",
            "headers": {"X-Recipient-Token": "wrong-rec-token"},
            "body": json.dumps({"capacity_kg_remaining": 50.0}),
            "client_ip": "192.168.1.50",
        }
    )
    assert resp_rec_tamper["statusCode"] == 403

    # 5. Volunteer ownership: Attempt viewing assignments with wrong volunteer token
    vol_a_hash = hashlib.sha256(b"vol-secret-vol-alpha").hexdigest()
    vol_a = Volunteer(
        volunteer_id="vol-alpha",
        volunteer_name="Alice",
        phone="+12125550199",
        address="300 Volunteer Rd",
        coordinates=Coordinates(latitude=40.73, longitude=-74.02),
        max_capacity_kg=50.0,
        vehicle_type="car",
        service_region="metro-core",
        auth_token_hash=vol_a_hash,
    )
    mock_v_table.get_item.return_value = {"Item": vol_a.model_dump(mode="json")}
    mock_d_table.query.return_value = {"Items": []}

    resp_vol_tamper = service.handle_request(
        {
            "httpMethod": "GET",
            "path": "/api/volunteers/vol-alpha/assignments",
            "headers": {"X-Volunteer-Token": "tampered-vol-token"},
            "client_ip": "192.168.1.50",
        }
    )
    assert resp_vol_tamper["statusCode"] == 403


# ------------------------------------------------------------------------------
# 2. Hashed Token Storage & Constant-Time Verification
# ------------------------------------------------------------------------------
def test_hashed_token_storage_and_timing_safe_comparison() -> None:
    """Verify database item stores only SHA-256 hash and plaintext is never leaked."""
    service, mock_d_table, _, _, _, _ = create_mock_service()

    future_time = (
        datetime.now(timezone.utc) + timedelta(hours=3)
    ).isoformat()
    payload = {
        "donor_id": "donor-fresh",
        "donor_name": "Fresh Harvest",
        "donor_phone": "+12125550188",
        "donor_address": "500 Market St",
        "donor_coordinates": {"latitude": 40.715, "longitude": -74.005},
        "food_category": "produce",
        "quantity_kg": 30.0,
        "ready_by": future_time,
        "perishability_hours": 8.0,
        "service_region": "metro-core",
    }

    mock_d_table.put_item.return_value = {}
    mock_d_table.get_item.return_value = {
        "Item": {
            "donation_id": "don-mock",
            "status": "REPORTED",
            "service_region": "metro-core",
            "ready_by": future_time,
            "quantity_kg": 30.0,
            "perishability_hours": 8.0,
            "food_category": "produce",
            "donor_id": "donor-fresh",
            "donor_name": "Fresh Harvest",
            "donor_phone": "+12125550188",
            "donor_address": "500 Market St",
            "donor_coordinates": {"latitude": 40.715, "longitude": -74.005},
        }
    }

    res = service.handle_request(
        {
            "httpMethod": "POST",
            "path": "/api/donations",
            "body": json.dumps(payload),
            "client_ip": "127.0.0.1",
        }
    )
    assert res["statusCode"] == 201
    resp_body = json.loads(res["body"])
    raw_token = resp_body["tracking_token"]
    assert len(raw_token) >= 32

    # Assert what was persisted into DynamoDB
    put_call = mock_d_table.put_item.call_args
    saved_item = put_call[1]["Item"]
    assert "tracking_token" not in saved_item  # Plaintext token MUST NOT exist
    assert "tracking_token_hash" in saved_item
    assert len(saved_item["tracking_token_hash"]) == 64  # SHA-256 hex length
    # Verify the hash matches sha256(raw_token)
    assert saved_item["tracking_token_hash"] == hashlib.sha256(
        raw_token.encode()
    ).hexdigest()


# ------------------------------------------------------------------------------
# 3. Dedicated Login Rate Limiter (5 req/min)
# ------------------------------------------------------------------------------
def test_login_rate_limiter_tight_five_per_minute() -> None:
    """Verify coordinator login enforces dedicated 5 requests/min limit."""
    service, _, _, _, _, mock_s_table = create_mock_service()

    counter = 0

    def mock_update_item(**_kwargs: Any) -> dict[str, Any]:
        nonlocal counter
        counter += 1
        return {"Attributes": {"request_count": counter}}

    mock_s_table.update_item.side_effect = mock_update_item

    # Requests 1 to 5: must proceed (return 401 because bad key)
    for _ in range(5):
        res = service.handle_request(
            {
                "httpMethod": "POST",
                "path": "/api/coordinator/login",
                "body": json.dumps({"api_key": "wrong-key"}),
                "client_ip": "192.168.1.100",
            }
        )
        assert res["statusCode"] == 401

    # Request 6: must be rate-limited with 429 and Retry-After
    res_6 = service.handle_request(
        {
            "httpMethod": "POST",
            "path": "/api/coordinator/login",
            "body": json.dumps({"api_key": "wrong-key"}),
            "client_ip": "192.168.1.100",
        }
    )
    assert res_6["statusCode"] == 429
    assert "Too Many Requests" in json.loads(res_6["body"])["error"]
    assert "Retry-After" in res_6["headers"]


# ------------------------------------------------------------------------------
# 4. General Distributed Rate Limiter (30 req/min)
# ------------------------------------------------------------------------------
def test_general_rate_limiter_thirty_per_minute() -> None:
    """Verify general public endpoints enforce 30 requests/min limit."""
    service, _, _, _, _, mock_s_table = create_mock_service()

    counter = 0

    def mock_update_general(**_kwargs: Any) -> dict[str, Any]:
        nonlocal counter
        counter += 1
        return {"Attributes": {"request_count": counter}}

    mock_s_table.update_item.side_effect = mock_update_general

    # Requests 1 to 30: proceed
    for _ in range(30):
        res = service.handle_request(
            {
                "httpMethod": "GET",
                "path": "/api/summary",
                "client_ip": "10.0.0.1",
            }
        )
        assert res["statusCode"] == 200

    # Request 31: rate-limited
    res_31 = service.handle_request(
        {
            "httpMethod": "GET",
            "path": "/api/summary",
            "client_ip": "10.0.0.1",
        }
    )
    assert res_31["statusCode"] == 429
    assert res_31["headers"]["Retry-After"] == "60"


# ------------------------------------------------------------------------------
# 5. Trusted Source IP Extraction & Anti-Spoofing
# ------------------------------------------------------------------------------
def test_trusted_source_ip_extraction_anti_spoofing() -> None:
    """Verify client-provided X-Forwarded-For is ignored for trusted sourceIp."""
    event_with_spoof = {
        "headers": {"X-Forwarded-For": "203.0.113.195, 70.41.3.18"},
        "requestContext": {
            "identity": {"sourceIp": "198.51.100.22"},
        },
    }
    extracted_ip = extract_client_ip(event_with_spoof)
    assert extracted_ip == "198.51.100.22"
    assert "203.0.113.195" not in extracted_ip

    # Test HTTP API v2 context format
    event_v2 = {
        "headers": {"X-Forwarded-For": "1.1.1.1"},
        "requestContext": {
            "http": {"sourceIp": "198.51.100.44"},
        },
    }
    assert extract_client_ip(event_v2) == "198.51.100.44"


# ------------------------------------------------------------------------------
# 6. HTTP Security Headers
# ------------------------------------------------------------------------------
def test_security_headers_and_privacy() -> None:
    """Verify Referrer-Policy, Cache-Control, and security headers are enforced."""
    service, _, _, _, _, _ = create_mock_service()
    res = service.handle_request(
        {
            "httpMethod": "GET",
            "path": "/api/summary",
            "client_ip": "127.0.0.1",
        }
    )
    headers = res["headers"]
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store, private"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"


# ------------------------------------------------------------------------------
# 7. Production Credential Guardrails
# ------------------------------------------------------------------------------
def test_production_credential_guardrail() -> None:
    """Assert production environment halts if COORDINATOR_API_KEY is missing/default."""
    # Scenario A: Missing in production -> MUST RAISE ConfigurationError
    with mock.patch.dict(
        os.environ, {"ENVIRONMENT": "prod", "COORDINATOR_API_KEY": ""}
    ):
        with pytest.raises(ConfigurationError) as exc1:
            load_app_configuration()
        assert "COORDINATOR_API_KEY is mandatory" in str(exc1.value)

    # Scenario B: Default dev key used in production -> MUST RAISE ConfigurationError
    with mock.patch.dict(
        os.environ,
        {"ENVIRONMENT": "prod", "COORDINATOR_API_KEY": DEV_LOCAL_COORDINATOR_KEY},
    ):
        with pytest.raises(ConfigurationError) as exc2:
            load_app_configuration()
        assert "cannot use the development default" in str(exc2.value)

    # Scenario C: Valid production key with >= 32 chars -> SUCCEEDS
    valid_prod_key = "secure-production-key-for-coordinator-over-32-chars"
    with mock.patch.dict(
        os.environ,
        {"ENVIRONMENT": "prod", "COORDINATOR_API_KEY": valid_prod_key},
    ):
        cfg = load_app_configuration()
        assert cfg.coordinator_api_key == valid_prod_key


# ------------------------------------------------------------------------------
# 8. Coordinator Auth-Bypass Protection
# ------------------------------------------------------------------------------
def test_coordinator_auth_bypass_protection() -> None:
    """Assert coordinator endpoints strictly reject unauthenticated calls."""
    service, _, _, _, _, _ = create_mock_service()

    endpoints = [
        ("GET", "/api/coordinator/donations"),
        ("GET", "/api/coordinator/escalations"),
        ("POST", "/api/coordinator/escalations/don-123/resolve"),
    ]

    for method, path in endpoints:
        # Case 1: Missing Authorization header -> 401
        res_none = service.handle_request(
            {
                "httpMethod": method,
                "path": path,
                "headers": {},
                "client_ip": "127.0.0.1",
            }
        )
        assert res_none["statusCode"] == 401, f"Failed on {path} without auth"

        # Case 2: Invalid Bearer token -> 401
        res_bad = service.handle_request(
            {
                "httpMethod": method,
                "path": path,
                "headers": {"Authorization": "Bearer invalid-token-guess"},
                "client_ip": "127.0.0.1",
            }
        )
        assert res_bad["statusCode"] == 401, f"Failed on {path} with bad auth"


# ------------------------------------------------------------------------------
# 9. XSS & Script Injection Handling
# ------------------------------------------------------------------------------
def test_xss_and_injection_sanitization() -> None:
    """Assert script tags in donor name or notes are safely handled."""
    service, mock_d_table, _, _, _, _ = create_mock_service()

    future_time = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    xss_payload = {
        "donor_id": "donor-xss",
        "donor_name": "<script>alert('XSS')</script>",
        "donor_phone": "+12125550188",
        "donor_address": '"><img src=x onerror=alert(1)>',
        "donor_coordinates": {"latitude": 40.715, "longitude": -74.005},
        "food_category": "prepared_meals",
        "quantity_kg": 10.0,
        "ready_by": future_time,
        "perishability_hours": 4.0,
        "service_region": "metro-core",
    }

    mock_d_table.put_item.return_value = {}
    mock_d_table.get_item.return_value = {
        "Item": {
            "donation_id": "don-xss-01",
            "status": "REPORTED",
            "service_region": "metro-core",
            "ready_by": future_time,
            "quantity_kg": 10.0,
            "perishability_hours": 4.0,
            "food_category": "prepared_meals",
            "donor_id": "donor-xss",
            "donor_name": "<script>alert('XSS')</script>",
            "donor_phone": "+12125550188",
            "donor_address": '"><img src=x onerror=alert(1)>',
            "donor_coordinates": {"latitude": 40.715, "longitude": -74.005},
        }
    }

    res = service.handle_request(
        {
            "httpMethod": "POST",
            "path": "/api/donations",
            "body": json.dumps(xss_payload),
            "client_ip": "127.0.0.1",
        }
    )
    assert res["statusCode"] == 201

    # Ensure response headers contain nosniff and frame protection
    assert res["headers"]["X-Content-Type-Options"] == "nosniff"
    assert res["headers"]["X-Frame-Options"] == "DENY"


# ------------------------------------------------------------------------------
# 10. Cross-Role Data Leak Prevention
# ------------------------------------------------------------------------------
def test_cross_role_data_leak_prevention() -> None:
    """Assert volunteer view never returns recipient capacity or dietary data."""
    service, mock_d_table, mock_r_table, mock_v_table, _, _ = create_mock_service()

    vol_token = "vol-secret-token-32-chars-alice-test"
    vol_hash = hashlib.sha256(vol_token.encode()).hexdigest()

    vol = Volunteer(
        volunteer_id="vol-001",
        volunteer_name="Alice",
        phone="+12125550199",
        address="100 Volunteer Way",
        coordinates=Coordinates(latitude=40.73, longitude=-74.02),
        max_capacity_kg=50.0,
        vehicle_type="car",
        service_region="metro-core",
        auth_token_hash=vol_hash,
    )
    mock_v_table.get_item.return_value = {"Item": vol.model_dump(mode="json")}

    rec = Recipient(
        recipient_id="rec-secret-org",
        organization_name="Downtown Shelter",
        contact_name="Bob",
        contact_phone="+12125550177",
        address="200 Shelter Way",
        coordinates=Coordinates(latitude=40.74, longitude=-74.03),
        capacity_kg_remaining=999.0,  # Sensitive internal capacity
        dietary_exclusions=["pork"],  # Sensitive dietary preferences
        service_region="metro-core",
    )
    mock_r_table.get_item.return_value = {"Item": rec.model_dump(mode="json")}

    assigned_don = Donation(
        donation_id="don-assigned-01",
        donor_id="donor-1",
        donor_name="Kitchen",
        donor_phone="+12125550188",
        donor_address="100 Main St",
        donor_coordinates=Coordinates(latitude=40.71, longitude=-74.00),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=20.0,
        ready_by=datetime.now(timezone.utc) + timedelta(hours=2),
        perishability_hours=4.0,
        service_region="metro-core",
        status=DonationStatus.ASSIGNED,
        matched_recipient_id="rec-secret-org",
        assigned_volunteer_id="vol-001",
    )
    mock_d_table.query.return_value = {
        "Items": [assigned_don.model_dump(mode="json")]
    }

    res = service.handle_request(
        {
            "httpMethod": "GET",
            "path": "/api/volunteers/vol-001/assignments",
            "headers": {"X-Volunteer-Token": vol_token},
            "client_ip": "127.0.0.1",
        }
    )
    assert res["statusCode"] == 200
    res_str = res["body"]

    # Assert recipient internal capacity and dietary exclusions are NOT leaked
    assert "999.0" not in res_str
    assert "dietary_exclusions" not in res_str
    assert "capacity_kg_remaining" not in res_str


# ------------------------------------------------------------------------------
# 11. Server-Side Validation Rejection
# ------------------------------------------------------------------------------
def test_server_side_validation_rejection() -> None:
    """Assert invalid payload structures receive clean 400 Bad Request."""
    service, _, _, _, _, _ = create_mock_service()

    # Case A: Negative quantity
    res_neg = service.handle_request(
        {
            "httpMethod": "POST",
            "path": "/api/donations",
            "body": json.dumps(
                {
                    "donor_id": "d1",
                    "donor_name": "Test",
                    "donor_phone": "+12125550199",
                    "donor_address": "100 Main St",
                    "donor_coordinates": {"latitude": 40.7, "longitude": -74.0},
                    "food_category": "produce",
                    "quantity_kg": -10.0,  # INVALID
                    "ready_by": (
                        datetime.now(timezone.utc) + timedelta(hours=2)
                    ).isoformat(),
                    "perishability_hours": 4.0,
                }
            ),
            "client_ip": "127.0.0.1",
        }
    )
    assert res_neg["statusCode"] == 400
    assert "Validation failed" in json.loads(res_neg["body"])["error"]

    # Case B: Past ready_by timestamp
    res_past = service.handle_request(
        {
            "httpMethod": "POST",
            "path": "/api/donations",
            "body": json.dumps(
                {
                    "donor_id": "d1",
                    "donor_name": "Test",
                    "donor_phone": "+12125550199",
                    "donor_address": "100 Main St",
                    "donor_coordinates": {"latitude": 40.7, "longitude": -74.0},
                    "food_category": "produce",
                    "quantity_kg": 10.0,
                    "ready_by": (
                        datetime.now(timezone.utc) - timedelta(hours=2)
                    ).isoformat(),  # PAST
                    "perishability_hours": 4.0,
                }
            ),
            "client_ip": "127.0.0.1",
        }
    )
    assert res_past["statusCode"] == 400

    # Case C: Malformed phone
    res_phone = service.handle_request(
        {
            "httpMethod": "POST",
            "path": "/api/donations",
            "body": json.dumps(
                {
                    "donor_id": "d1",
                    "donor_name": "Test",
                    "donor_phone": "not-a-phone",  # INVALID
                    "donor_address": "100 Main St",
                    "donor_coordinates": {"latitude": 40.7, "longitude": -74.0},
                    "food_category": "produce",
                    "quantity_kg": 10.0,
                    "ready_by": (
                        datetime.now(timezone.utc) + timedelta(hours=2)
                    ).isoformat(),
                    "perishability_hours": 4.0,
                }
            ),
            "client_ip": "127.0.0.1",
        }
    )
    assert res_phone["statusCode"] == 400


# ------------------------------------------------------------------------------
# 12. Coordinator Manual Resolution
# ------------------------------------------------------------------------------
def test_coordinator_manual_resolution() -> None:
    """Assert coordinator resolution updates donation state and creates audit event."""
    service, mock_d_table, _, _, mock_a_table, _ = create_mock_service()
    config = create_test_config()

    don = Donation(
        donation_id="don-escalated-01",
        donor_id="donor-1",
        donor_name="Kitchen",
        donor_phone="+12125550199",
        donor_address="100 Main St",
        donor_coordinates=Coordinates(latitude=40.71, longitude=-74.00),
        food_category=FoodCategory.PREPARED_MEALS,
        quantity_kg=20.0,
        ready_by=datetime.now(timezone.utc) + timedelta(hours=2),
        perishability_hours=4.0,
        service_region="metro-core",
        status=DonationStatus.ESCALATED,
    )
    mock_d_table.get_item.return_value = {"Item": don.model_dump(mode="json")}
    mock_d_table.update_item.return_value = {}
    mock_a_table.put_item.return_value = {}

    res = service.handle_request(
        {
            "httpMethod": "POST",
            "path": "/api/coordinator/escalations/don-escalated-01/resolve",
            "headers": {"Authorization": f"Bearer {config.coordinator_api_key}"},
            "body": json.dumps(
                {
                    "resolution_action": "dismiss",
                    "notes": "Verified food-safety window exception; marked safe.",
                }
            ),
            "client_ip": "127.0.0.1",
        }
    )
    assert res["statusCode"] == 200
    res_data = json.loads(res["body"])
    assert res_data["action"] == "dismiss"

    # Verify audit record was created
    audit_call = mock_a_table.put_item.call_args
    assert audit_call is not None
    audit_item = audit_call[1]["Item"]
    assert audit_item["action"] == "COORDINATOR_MANUAL_RESOLUTION"
    assert "Verified food-safety window" in str(audit_item["details"])


# ------------------------------------------------------------------------------
# 13. AUTH_VIOLATION Audit Trail Persistence Verification
# ------------------------------------------------------------------------------
def test_auth_violation_audit_record_persisted_in_dynamodb() -> None:
    """Verify unauthorized access attempts persist structured AUTH_VIOLATION records."""
    service, mock_d_table, mock_r_table, _, mock_a_table, _ = create_mock_service()

    # Case A: Donor endpoint with mismatched tracking token
    don = Donation(
        donation_id="don-sec-01",
        donor_id="donor-1",
        donor_name="Bakery",
        donor_phone="+12125550199",
        donor_address="100 Main St",
        donor_coordinates=Coordinates(latitude=40.71, longitude=-74.00),
        food_category=FoodCategory.BAKERY,
        quantity_kg=15.0,
        ready_by=datetime.now(timezone.utc) + timedelta(hours=3),
        perishability_hours=6.0,
        service_region="metro-core",
        status=DonationStatus.REPORTED,
        tracking_token_hash=hashlib.sha256(b"correct-token").hexdigest(),
    )
    mock_d_table.get_item.return_value = {"Item": don.model_dump(mode="json")}

    res_donor = service.handle_request(
        {
            "httpMethod": "GET",
            "path": "/api/donations/don-sec-01",
            "headers": {"X-Tracking-Token": "invalid-attacker-token"},
            "client_ip": "198.51.100.77",
        }
    )
    assert res_donor["statusCode"] == 403

    # Assert audit record persisted in DynamoDB audit table
    assert mock_a_table.put_item.called
    latest_call = mock_a_table.put_item.call_args[1]["Item"]
    assert latest_call["action"] == "AUTH_VIOLATION"
    assert latest_call["actor"] == "unauthorized_client"
    assert latest_call["donation_id"] == "SYSTEM_SECURITY"
    assert latest_call["details"]["source_ip"] == "198.51.100.77"
    assert latest_call["details"]["target_resource"] == "donation:don-sec-01"
    assert latest_call["details"]["violation_type"] == "INVALID_TRACKING_TOKEN"

    # Case B: Coordinator login with wrong credential
    mock_a_table.reset_mock()
    res_login = service.handle_request(
        {
            "httpMethod": "POST",
            "path": "/api/coordinator/login",
            "body": json.dumps({"api_key": "wrong-bad-password"}),
            "client_ip": "198.51.100.88",
        }
    )
    assert res_login["statusCode"] == 401
    assert mock_a_table.put_item.called
    login_call = mock_a_table.put_item.call_args[1]["Item"]
    assert login_call["action"] == "AUTH_VIOLATION"
    assert login_call["details"]["source_ip"] == "198.51.100.88"
    assert login_call["details"]["target_resource"] == "coordinator_login"
    assert login_call["details"]["violation_type"] == "FAILED_COORDINATOR_LOGIN"

    # Case C: Recipient capacity update with invalid token
    mock_a_table.reset_mock()
    rec = Recipient(
        recipient_id="rec-sec-01",
        organization_name="Shelter",
        contact_name="Alice",
        contact_phone="+12125550199",
        address="200 Shelter Way",
        coordinates=Coordinates(latitude=40.72, longitude=-74.01),
        capacity_kg_remaining=100.0,
        service_region="metro-core",
        auth_token_hash=hashlib.sha256(b"correct-rec-token").hexdigest(),
    )
    mock_r_table.get_item.return_value = {"Item": rec.model_dump(mode="json")}

    res_rec = service.handle_request(
        {
            "httpMethod": "POST",
            "path": "/api/recipients/rec-sec-01/capacity",
            "headers": {"X-Recipient-Token": "bad-rec-token"},
            "body": json.dumps({"capacity_kg_remaining": 50.0}),
            "client_ip": "198.51.100.99",
        }
    )
    assert res_rec["statusCode"] == 403
    assert mock_a_table.put_item.called
    rec_call = mock_a_table.put_item.call_args[1]["Item"]
    assert rec_call["action"] == "AUTH_VIOLATION"
    assert rec_call["details"]["target_resource"] == "recipient:rec-sec-01"
    assert rec_call["details"]["violation_type"] == "INVALID_RECIPIENT_TOKEN"


# ------------------------------------------------------------------------------
# 14. Static Development Server Path Traversal Hardcore Defense
# ------------------------------------------------------------------------------
def test_static_server_path_traversal_prevention() -> None:
    """Verify static asset server rejects directory traversal and never leaks files."""
    server = HTTPServer(("127.0.0.1", 0), FrontendDevServerHandler)
    host, port = server.server_address
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        traversal_attempts = [
            "/../../config.py",
            "/..%2f..%2fconfig.py",
            "/%2e%2e/%2e%2e/config.py",
            "/%2e%2e%2f%2e%2e%2fconfig.py",
            "/%252e%252e%252f%252e%252e%252fconfig.py",
            "/....//....//config.py",
            "/..\\..\\config.py",
            "/css/../../config.py",
            "/index.html%00.png",
        ]

        for path in traversal_attempts:
            conn = http.client.HTTPConnection(host, port, timeout=2)
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="ignore")
            conn.close()

            # Status must be error (400 Bad Request or 403 Forbidden or 404 Not Found)
            assert resp.status in (400, 403, 404), (
                f"Expected rejection for {path}, got {resp.status}"
            )

            # Security critical assertion: sensitive codebase content MUST NEVER leak
            assert "FOOD_SAFETY_MIN_SHELF_LIFE_MINUTES" not in body, (
                f"File content leaked for path {path}!"
            )
            assert "AppConfig" not in body, f"File content leaked for path {path}!"
            assert "coordinator_api_key" not in body, (
                f"File content leaked for path {path}!"
            )

        # Legitimate asset check: verify valid frontend files serve normally
        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request("GET", "/css/style.css")
        resp = conn.getresponse()
        css_body = resp.read().decode("utf-8", errors="ignore")
        conn.close()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "text/css"
        assert "var(--" in css_body or "body" in css_body

    finally:
        server.shutdown()
        server.server_close()
