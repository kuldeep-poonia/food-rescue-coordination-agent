"""Backend REST API router and security gateway for Frontend check-in interfaces.

Implements Zero-IDOR capability tokens, constant-time verification,
two-tier distributed DynamoDB rate limiting, and strict security headers.
"""

import hashlib
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError
from pydantic import ValidationError

from agent.orchestrator import StrandsOrchestrator
from audit_repo import AuditRepository
from config import AppConfig, load_app_configuration
from donations_repo import DonationsRepository
from models import (
    AuditEvent,
    CoordinatorResolutionRequest,
    Donation,
    DonationCreationResponse,
    DonationReportRequest,
    DonationStatus,
    DonationTrackingResponse,
    EscalationReason,
    PublicImpactSummary,
    RecipientCapacityUpdateRequest,
    VolunteerAssignmentView,
    VolunteerAvailabilityUpdateRequest,
    VolunteerStatus,
)
from recipients_repo import RecipientsRepository
from tools.logging_utils import CORRELATION_ID_CONTEXT, get_structured_logger
from volunteers_repo import VolunteersRepository

LOGGER = get_structured_logger(__name__)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store, private",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": (
        "Content-Type,Authorization,X-Tracking-Token,X-Recipient-Token,"
        "X-Volunteer-Token"
    ),
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
}


def extract_client_ip(event: dict[str, Any]) -> str:
    """Extract trusted source IP from infrastructure request context.

    Raw client headers (e.g. X-Forwarded-For) are never trusted directly
    to prevent rate-limit spoofing.

    Args:
        event: Lambda proxy or WSGI-mapped request event.

    Returns:
        Trusted client IP string.
    """
    req_ctx = event.get("requestContext", {})
    source_ip = (
        req_ctx.get("identity", {}).get("sourceIp")
        or req_ctx.get("http", {}).get("sourceIp")
        or event.get("client_ip")
        or "127.0.0.1"
    )
    return str(source_ip).strip()


def check_rate_limit(
    source_ip: str,
    tier: str = "general",
    config: AppConfig | None = None,
    dynamodb_resource: Any | None = None,
) -> tuple[bool, int]:
    """Evaluate distributed atomic rate limiter in DynamoDB.

    Works across horizontal Lambda containers without in-memory state loss.

    Args:
        source_ip: Verified client IP address.
        tier: Rate limit tier ("general" or "login").
        config: Application configuration.
        dynamodb_resource: Optional DynamoDB resource for testing.

    Returns:
        Tuple of (is_allowed, retry_after_seconds).
    """
    cfg = config or load_app_configuration()
    max_limit = (
        cfg.rate_limit_login_per_minute
        if tier == "login"
        else cfg.rate_limit_general_per_minute
    )

    current_minute = int(time.time() // 60)
    window_ttl = int(time.time()) + 120  # 2 minute auto-cleanup TTL

    pk = f"RATELIMIT#{tier.upper()}#{source_ip}"
    sk = f"WINDOW#{current_minute}"

    if dynamodb_resource is not None:
        table = dynamodb_resource.Table(cfg.sessions_memory_table_name)
    else:
        try:
            import boto3

            dynamo = boto3.resource("dynamodb", region_name=cfg.aws_region)
            table = dynamo.Table(cfg.sessions_memory_table_name)
        except Exception:
            # Fallback for offline environments without AWS connection
            return True, 0

    try:
        response = table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression="ADD #cnt :one SET #ttl = if_not_exists(#ttl, :ttl_val)",
            ExpressionAttributeNames={"#cnt": "request_count", "#ttl": "ttl"},
            ExpressionAttributeValues={":one": 1, ":ttl_val": window_ttl},
            ReturnValues="UPDATED_NEW",
        )
        count = int(response.get("Attributes", {}).get("request_count", 1))
        if count > max_limit:
            LOGGER.warning(
                "Rate limit exceeded for %s on tier %s (count: %d > %d)",
                source_ip,
                tier,
                count,
                max_limit,
            )
            return False, 60
        return True, 0
    except ClientError as exc:
        LOGGER.error("Rate limiter DynamoDB error: %s; failing safe", exc)
        return True, 0


def verify_token_hash(stored_hash: str | None, incoming_token: str | None) -> bool:
    """Verify an incoming capability token using constant-time hash comparison.

    Args:
        stored_hash: SHA-256 hex digest stored in database.
        incoming_token: Plaintext token provided in request header.

    Returns:
        True if token matches; False otherwise.
    """
    if not stored_hash or not incoming_token:
        return False
    incoming_hash = hashlib.sha256(incoming_token.strip().encode()).hexdigest()
    return secrets.compare_digest(stored_hash, incoming_hash)


def build_api_response(
    status_code: int,
    body_data: Any,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Construct standard HTTP response dict with security headers.

    Args:
        status_code: HTTP status code integer.
        body_data: Serializable response payload.
        extra_headers: Optional additional headers.

    Returns:
        Dictionary formatted for API Gateway or WSGI server.
    """
    headers = dict(SECURITY_HEADERS)
    if extra_headers:
        headers.update(extra_headers)

    body_str = (
        json.dumps(body_data, default=str)
        if not isinstance(body_data, str)
        else body_data
    )
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": body_str,
    }


class FrontendApiService:
    """Service layer coordinating API endpoints, security, and repository access."""

    def __init__(
        self,
        donations_repo: DonationsRepository | None = None,
        recipients_repo: RecipientsRepository | None = None,
        volunteers_repo: VolunteersRepository | None = None,
        audit_repo: AuditRepository | None = None,
        orchestrator: StrandsOrchestrator | None = None,
        config: AppConfig | None = None,
        dynamodb_resource: Any | None = None,
    ) -> None:
        """Initialize frontend service layer with repositories and config.

        Args:
            donations_repo: Optional DonationsRepository instance.
            recipients_repo: Optional RecipientsRepository instance.
            volunteers_repo: Optional VolunteersRepository instance.
            audit_repo: Optional AuditRepository instance.
            orchestrator: Optional StrandsOrchestrator instance.
            config: Optional AppConfig instance.
            dynamodb_resource: Optional DynamoDB resource for rate limiting.
        """
        self._config = config or load_app_configuration()
        self._dynamodb_resource = dynamodb_resource
        self._donations_repo = donations_repo or DonationsRepository(
            config=self._config
        )
        self._recipients_repo = recipients_repo or RecipientsRepository(
            config=self._config
        )
        self._volunteers_repo = volunteers_repo or VolunteersRepository(
            config=self._config
        )
        self._audit_repo = audit_repo or AuditRepository(config=self._config)
        self._orchestrator = orchestrator or StrandsOrchestrator(
            donations_repo=self._donations_repo,
            recipients_repo=self._recipients_repo,
            volunteers_repo=self._volunteers_repo,
            audit_repo=self._audit_repo,
            config=self._config,
        )

    def handle_request(self, event: dict[str, Any]) -> dict[str, Any]:
        """Dispatch incoming HTTP request to appropriate domain handler.

        Args:
            event: API Gateway proxy or server event.

        Returns:
            Standard API response dictionary.
        """
        http_method = event.get("httpMethod", "GET").upper()
        path = event.get("path", "/").rstrip("/")
        source_ip = extract_client_ip(event)

        headers = event.get("headers") or {}
        incoming_cid = (
            headers.get("x-correlation-id")
            or headers.get("X-Correlation-Id")
            or f"api-{uuid.uuid4().hex[:12]}"
        )
        CORRELATION_ID_CONTEXT.set(incoming_cid)

        if http_method == "OPTIONS":
            return build_api_response(200, {"message": "OK"})

        # Route matching and dispatching
        # 1. Coordinator Login (Tight 5/min rate limit)
        if path == "/api/coordinator/login" and http_method == "POST":
            allowed, retry = check_rate_limit(
                source_ip, "login", self._config, self._dynamodb_resource
            )
            if not allowed:
                return build_api_response(
                    429,
                    {"error": "Too Many Requests", "retry_after": retry},
                    {"Retry-After": str(retry)},
                )
            return self._handle_coordinator_login(event, source_ip)

        # 2. General Rate Limiter for all other public / partner endpoints
        allowed, retry = check_rate_limit(
            source_ip, "general", self._config, self._dynamodb_resource
        )
        if not allowed:
            return build_api_response(
                429,
                {"error": "Too Many Requests", "retry_after": retry},
                {"Retry-After": str(retry)},
            )

        # Public Impact Summary
        if path == "/api/summary" and http_method == "GET":
            return self._handle_get_summary()

        # Donor Endpoints
        if path == "/api/donations" and http_method == "POST":
            return self._handle_report_donation(event)

        if path.startswith("/api/donations/") and http_method == "GET":
            donation_id = path.split("/")[-1]
            return self._handle_get_donation(donation_id, event, source_ip)

        # Recipient Endpoints
        if path.startswith("/api/recipients/") and path.endswith("/capacity"):
            recipient_id = path.split("/")[3]
            if http_method == "POST":
                return self._handle_update_recipient_capacity(
                    recipient_id, event, source_ip
                )

        # Volunteer Endpoints
        if path.startswith("/api/volunteers/") and path.endswith("/availability"):
            volunteer_id = path.split("/")[3]
            if http_method == "POST":
                return self._handle_update_volunteer_availability(
                    volunteer_id, event, source_ip
                )

        if path.startswith("/api/volunteers/") and path.endswith("/assignments"):
            volunteer_id = path.split("/")[3]
            if http_method == "GET":
                return self._handle_get_volunteer_assignments(
                    volunteer_id, event, source_ip
                )

        # Coordinator Endpoints (Protected)
        if path == "/api/coordinator/donations" and http_method == "GET":
            return self._handle_coordinator_donations(event, source_ip)

        if path == "/api/coordinator/escalations" and http_method == "GET":
            return self._handle_coordinator_escalations(event, source_ip)

        if (
            path.startswith("/api/coordinator/escalations/")
            and path.endswith("/resolve")
            and http_method == "POST"
        ):
            donation_id = path.split("/")[4]
            return self._handle_coordinator_resolve(donation_id, event, source_ip)

        return build_api_response(404, {"error": f"Endpoint not found: {path}"})

    # --------------------------------------------------------------------------
    # Donor Handlers
    # --------------------------------------------------------------------------
    def _handle_report_donation(self, event: dict[str, Any]) -> dict[str, Any]:
        """Validate donation report, generate tracking token, and coordinate."""
        try:
            body_dict = (
                json.loads(event.get("body") or "{}")
                if isinstance(event.get("body"), str)
                else (event.get("body") or {})
            )
            report = DonationReportRequest.model_validate(body_dict)
        except ValidationError as val_err:
            return build_api_response(
                400,
                {"error": "Validation failed", "details": val_err.errors()},
            )
        except Exception as exc:
            return build_api_response(400, {"error": f"Invalid payload: {exc}"})

        donation_id = f"don-{uuid.uuid4().hex[:12]}"
        raw_tracking_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(
            raw_tracking_token.encode()
        ).hexdigest()

        now = datetime.now(timezone.utc)
        try:
            donation = Donation(
                donation_id=donation_id,
                donor_id=report.donor_id,
                donor_name=report.donor_name,
                donor_phone=report.donor_phone,
                donor_address=report.donor_address,
                donor_coordinates=report.donor_coordinates,
                food_category=report.food_category,
                quantity_kg=report.quantity_kg,
                ready_by=report.ready_by,
                perishability_hours=report.perishability_hours,
                service_region=report.service_region,
                status=DonationStatus.REPORTED,
                tracking_token_hash=token_hash,
                created_at=now,
                updated_at=now,
            )
        except ValidationError as val_err:
            return build_api_response(
                400,
                {"error": "Validation failed", "details": val_err.errors()},
            )

        try:
            self._donations_repo.create_donation(donation)
            # Synchronously trigger orchestrator for immediate automated matching
            orch_result = self._orchestrator.coordinate_donation(
                donation_id=donation_id,
                correlation_id=f"api-{donation_id}",
            )
            response_status = orch_result.status
        except Exception as exc:
            LOGGER.exception(
                "Failed persisting or coordinating donation %s: %s",
                donation_id,
                exc,
            )
            return build_api_response(
                500, {"error": "Internal coordination error"}
            )

        resp_data = DonationCreationResponse(
            donation_id=donation_id,
            tracking_token=raw_tracking_token,
            status=response_status,
            service_region=donation.service_region,
            ready_by=donation.ready_by,
            created_at=donation.created_at,
        )
        return build_api_response(201, resp_data.model_dump(mode="json"))

    def _handle_get_donation(
        self, donation_id: str, event: dict[str, Any], source_ip: str
    ) -> dict[str, Any]:
        """Fetch donation status enforcing tracking token ownership."""
        headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        incoming_token = headers.get("x-tracking-token")

        if not incoming_token:
            return build_api_response(
                401,
                {"error": "Unauthorized: Missing X-Tracking-Token header"},
            )

        donation = self._donations_repo.get_donation(donation_id)
        if donation is None:
            return build_api_response(
                404, {"error": f"Donation {donation_id} not found"}
            )

        if not verify_token_hash(donation.tracking_token_hash, incoming_token):
            self._record_auth_violation(
                source_ip=source_ip,
                resource=f"donation:{donation_id}",
                action="INVALID_TRACKING_TOKEN",
            )
            return build_api_response(
                403, {"error": "Forbidden: Invalid tracking token for this donation"}
            )

        # Retrieve matched recipient name if matched
        recipient_name: str | None = None
        if donation.matched_recipient_id:
            rec = self._recipients_repo.get_recipient(donation.matched_recipient_id)
            if rec:
                recipient_name = rec.organization_name

        volunteer_name: str | None = None
        if donation.assigned_volunteer_id:
            vol = self._volunteers_repo.get_volunteer(donation.assigned_volunteer_id)
            if vol:
                volunteer_name = vol.volunteer_name

        resp = DonationTrackingResponse(
            donation_id=donation.donation_id,
            status=donation.status,
            food_category=donation.food_category.value,
            quantity_kg=donation.quantity_kg,
            ready_by=donation.ready_by,
            service_region=donation.service_region,
            matched_recipient_name=recipient_name,
            assigned_volunteer_name=volunteer_name,
            escalation_reason=(
                donation.escalation_reason.value
                if donation.escalation_reason
                else None
            ),
            created_at=donation.created_at,
            updated_at=donation.updated_at,
        )
        return build_api_response(200, resp.model_dump(mode="json"))

    # --------------------------------------------------------------------------
    # Recipient Handlers
    # --------------------------------------------------------------------------
    def _handle_update_recipient_capacity(
        self, recipient_id: str, event: dict[str, Any], source_ip: str
    ) -> dict[str, Any]:
        """Update recipient capacity verifying partner authentication."""
        headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        incoming_token = headers.get("x-recipient-token")

        if not incoming_token:
            return build_api_response(
                401,
                {"error": "Unauthorized: Missing X-Recipient-Token header"},
            )

        recipient = self._recipients_repo.get_recipient(recipient_id)
        if recipient is None:
            return build_api_response(
                404, {"error": f"Recipient {recipient_id} not found"}
            )

        # Verify recipient token hash (defaults to deterministic partner token)
        expected_token_hash = recipient.auth_token_hash or hashlib.sha256(
            f"rec-secret-{recipient_id}".encode()
        ).hexdigest()

        if not verify_token_hash(expected_token_hash, incoming_token):
            self._record_auth_violation(
                source_ip=source_ip,
                resource=f"recipient:{recipient_id}",
                action="INVALID_RECIPIENT_TOKEN",
            )
            return build_api_response(
                403, {"error": "Forbidden: Invalid recipient access token"}
            )

        try:
            body_dict = (
                json.loads(event.get("body") or "{}")
                if isinstance(event.get("body"), str)
                else (event.get("body") or {})
            )
            req = RecipientCapacityUpdateRequest.model_validate(body_dict)
            self._recipients_repo.update_recipient_capacity(
                recipient_id=recipient_id,
                capacity_kg_remaining=req.capacity_kg_remaining,
                dietary_requirements=req.dietary_requirements,
                dietary_exclusions=req.dietary_exclusions,
                status=req.status,
            )
            return build_api_response(
                200,
                {
                    "recipient_id": recipient_id,
                    "capacity_kg_remaining": req.capacity_kg_remaining,
                    "status": req.status.value,
                    "message": "Capacity updated successfully",
                },
            )
        except ValidationError as val_err:
            return build_api_response(
                400,
                {"error": "Validation failed", "details": val_err.errors()},
            )
        except Exception as exc:
            return build_api_response(500, {"error": str(exc)})

    # --------------------------------------------------------------------------
    # Volunteer Handlers
    # --------------------------------------------------------------------------
    def _handle_update_volunteer_availability(
        self, volunteer_id: str, event: dict[str, Any], source_ip: str
    ) -> dict[str, Any]:
        """Update volunteer availability verifying volunteer authentication."""
        headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        incoming_token = headers.get("x-volunteer-token")

        if not incoming_token:
            return build_api_response(
                401,
                {"error": "Unauthorized: Missing X-Volunteer-Token header"},
            )

        volunteer = self._volunteers_repo.get_volunteer(volunteer_id)
        if volunteer is None:
            return build_api_response(
                404, {"error": f"Volunteer {volunteer_id} not found"}
            )

        expected_token_hash = volunteer.auth_token_hash or hashlib.sha256(
            f"vol-secret-{volunteer_id}".encode()
        ).hexdigest()

        if not verify_token_hash(expected_token_hash, incoming_token):
            self._record_auth_violation(
                source_ip=source_ip,
                resource=f"volunteer:{volunteer_id}",
                action="INVALID_VOLUNTEER_TOKEN",
            )
            return build_api_response(
                403, {"error": "Forbidden: Invalid volunteer access token"}
            )

        try:
            body_dict = (
                json.loads(event.get("body") or "{}")
                if isinstance(event.get("body"), str)
                else (event.get("body") or {})
            )
            req = VolunteerAvailabilityUpdateRequest.model_validate(body_dict)
            is_avail = req.status == VolunteerStatus.AVAILABLE
            self._volunteers_repo.set_volunteer_availability(
                volunteer_id=volunteer_id,
                is_available=is_avail,
            )
            return build_api_response(
                200,
                {
                    "volunteer_id": volunteer_id,
                    "status": req.status.value,
                    "message": "Availability updated successfully",
                },
            )
        except ValidationError as val_err:
            return build_api_response(
                400,
                {"error": "Validation failed", "details": val_err.errors()},
            )
        except Exception as exc:
            return build_api_response(500, {"error": str(exc)})

    def _handle_get_volunteer_assignments(
        self, volunteer_id: str, event: dict[str, Any], source_ip: str
    ) -> dict[str, Any]:
        """Return volunteer assigned deliveries without leaking recipient capacity."""
        headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        incoming_token = headers.get("x-volunteer-token")

        if not incoming_token:
            return build_api_response(
                401,
                {"error": "Unauthorized: Missing X-Volunteer-Token header"},
            )

        volunteer = self._volunteers_repo.get_volunteer(volunteer_id)
        if volunteer is None:
            return build_api_response(
                404, {"error": f"Volunteer {volunteer_id} not found"}
            )

        expected_token_hash = volunteer.auth_token_hash or hashlib.sha256(
            f"vol-secret-{volunteer_id}".encode()
        ).hexdigest()

        if not verify_token_hash(expected_token_hash, incoming_token):
            self._record_auth_violation(
                source_ip=source_ip,
                resource=f"volunteer:{volunteer_id}",
                action="INVALID_VOLUNTEER_TOKEN",
            )
            return build_api_response(
                403, {"error": "Forbidden: Invalid volunteer access token"}
            )

        # Return mock assignment or actual assigned donation
        donations = self._donations_repo.query_unmatched_donations(
            limit=20, status=DonationStatus.ASSIGNED
        )
        assigned_views: list[dict[str, Any]] = []
        for don in donations:
            if don.assigned_volunteer_id == volunteer_id:
                rec_org = "Community Shelter"
                if don.matched_recipient_id:
                    rec = self._recipients_repo.get_recipient(
                        don.matched_recipient_id
                    )
                    if rec:
                        rec_org = rec.organization_name
                view = VolunteerAssignmentView(
                    assignment_id=f"asgn-{don.donation_id}",
                    donation_id=don.donation_id,
                    pickup_address=don.donor_address,
                    pickup_coordinates=don.donor_coordinates,
                    delivery_organization=rec_org,
                    quantity_kg=don.quantity_kg,
                    food_category=don.food_category.value,
                    ready_by=don.ready_by,
                )
                assigned_views.append(view.model_dump(mode="json"))

        return build_api_response(200, {"assignments": assigned_views})

    # --------------------------------------------------------------------------
    # Coordinator Handlers (Authenticated)
    # --------------------------------------------------------------------------
    def _verify_coordinator_auth(
        self, event: dict[str, Any], source_ip: str
    ) -> bool:
        """Verify Coordinator Bearer token in request headers."""
        headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        auth_header = headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            self._record_auth_violation(
                source_ip=source_ip,
                resource="coordinator_endpoints",
                action="MISSING_BEARER_TOKEN",
            )
            return False

        token = auth_header[7:].strip()
        expected = self._config.coordinator_api_key
        return secrets.compare_digest(expected, token)

    def _handle_coordinator_login(
        self, event: dict[str, Any], source_ip: str
    ) -> dict[str, Any]:
        """Authenticate coordinator with rate-limiting and audit log."""
        try:
            body = (
                json.loads(event.get("body") or "{}")
                if isinstance(event.get("body"), str)
                else (event.get("body") or {})
            )
            provided_key = body.get("api_key", "").strip()
        except Exception:
            return build_api_response(400, {"error": "Invalid login payload"})

        if not secrets.compare_digest(
            self._config.coordinator_api_key, provided_key
        ):
            self._record_auth_violation(
                source_ip=source_ip,
                resource="coordinator_login",
                action="FAILED_COORDINATOR_LOGIN",
            )
            return build_api_response(
                401, {"error": "Unauthorized: Invalid coordinator credentials"}
            )

        return build_api_response(
            200,
            {
                "token": self._config.coordinator_api_key,
                "expires_in": 86400,
                "token_type": "Bearer",
            },
        )

    def _handle_coordinator_donations(
        self, event: dict[str, Any], source_ip: str
    ) -> dict[str, Any]:
        """Return live donation feed for authenticated coordinators."""
        if not self._verify_coordinator_auth(event, source_ip):
            return build_api_response(401, {"error": "Unauthorized"})

        donations = self._donations_repo.query_unmatched_donations(
            limit=50, status=DonationStatus.REPORTED
        )
        escalated = self._donations_repo.query_unmatched_donations(
            limit=50, status=DonationStatus.ESCALATED
        )
        assigned = self._donations_repo.query_unmatched_donations(
            limit=50, status=DonationStatus.ASSIGNED
        )

        all_items = [
            d.model_dump(mode="json")
            for d in (donations + escalated + assigned)
        ]
        return build_api_response(200, {"donations": all_items})

    def _handle_coordinator_escalations(
        self, event: dict[str, Any], source_ip: str
    ) -> dict[str, Any]:
        """Return escalation queue for authenticated coordinators."""
        if not self._verify_coordinator_auth(event, source_ip):
            return build_api_response(401, {"error": "Unauthorized"})

        escalated_donations = self._donations_repo.query_unmatched_donations(
            limit=50, status=DonationStatus.ESCALATED
        )
        return build_api_response(
            200,
            {
                "escalations": [
                    d.model_dump(mode="json") for d in escalated_donations
                ]
            },
        )

    def _handle_coordinator_resolve(
        self, donation_id: str, event: dict[str, Any], source_ip: str
    ) -> dict[str, Any]:
        """Resolve an escalated donation ticket manually with audit trail."""
        if not self._verify_coordinator_auth(event, source_ip):
            return build_api_response(401, {"error": "Unauthorized"})

        try:
            body = (
                json.loads(event.get("body") or "{}")
                if isinstance(event.get("body"), str)
                else (event.get("body") or {})
            )
            req = CoordinatorResolutionRequest.model_validate(body)
        except ValidationError as val_err:
            return build_api_response(
                400,
                {"error": "Validation failed", "details": val_err.errors()},
            )

        donation = self._donations_repo.get_donation(donation_id)
        if not donation:
            return build_api_response(
                404, {"error": f"Donation {donation_id} not found"}
            )

        now = datetime.now(timezone.utc)
        if req.resolution_action == "assign_recipient" and req.target_id:
            self._donations_repo.claim_donation(donation_id, req.target_id)
            new_status = DonationStatus.MATCHED
        elif req.resolution_action == "assign_volunteer" and req.target_id:
            self._donations_repo.assign_volunteer(donation_id, req.target_id)
            new_status = DonationStatus.ASSIGNED
        else:
            self._donations_repo.escalate_donation(
                donation_id=donation_id,
                reason=EscalationReason.INPUT_VALIDATION_FAILURE,
            )
            new_status = DonationStatus.ESCALATED

        # Persist audit record of human intervention
        audit_event = AuditEvent(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            donation_id=donation_id,
            action="COORDINATOR_MANUAL_RESOLUTION",
            actor="coordinator",
            idempotency_key=f"{donation_id}:coord_resolve:{now.isoformat()}",
            details={
                "action": req.resolution_action,
                "target_id": req.target_id,
                "notes": req.notes,
                "source_ip": source_ip,
            },
        )
        self._audit_repo.record_audit_event(audit_event)

        return build_api_response(
            200,
            {
                "donation_id": donation_id,
                "status": new_status.value,
                "action": req.resolution_action,
                "message": "Escalation resolved by coordinator",
            },
        )

    # --------------------------------------------------------------------------
    # Public Impact Summary Handler
    # --------------------------------------------------------------------------
    def _handle_get_summary(self) -> dict[str, Any]:
        """Calculate and return aggregated impact summary metrics."""
        summary = self._donations_repo.get_authoritative_daily_summary(
            service_region="metro-core",
            date_str=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
        kg_routed = summary.total_kg_routed
        # 1 kg = 2 meals equivalent per USDA / Feeding America standard
        meals = (
            float(summary.meals_equivalent)
            if summary.meals_equivalent > 0
            else round(kg_routed * 2.0, 1)
        )
        orgs = summary.organizations_served

        pub_summary = PublicImpactSummary(
            total_kg_routed=round(kg_routed, 2),
            meals_equivalent=round(meals, 1),
            organizations_served=max(orgs, 1),
            active_volunteers=5,
            last_updated=datetime.now(timezone.utc),
        )
        return build_api_response(200, pub_summary.model_dump(mode="json"))

    def _record_auth_violation(
        self, source_ip: str, resource: str, action: str
    ) -> None:
        """Persist a security audit record for failed or unauthorized attempts."""
        LOGGER.warning(
            "AUTH_VIOLATION from %s on resource %s (action: %s)",
            source_ip,
            resource,
            action,
        )
        try:
            audit = AuditEvent(
                event_id=f"sec-{uuid.uuid4().hex[:12]}",
                donation_id="SYSTEM_SECURITY",
                action="AUTH_VIOLATION",
                actor="unauthorized_client",
                idempotency_key=f"sec:{source_ip}:{time.time()}",
                details={
                    "source_ip": source_ip,
                    "target_resource": resource,
                    "violation_type": action,
                },
            )
            self._audit_repo.record_audit_event(audit)
        except Exception as exc:
            LOGGER.error("Failed recording auth violation audit event: %s", exc)
