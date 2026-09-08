"""Lightweight development web server serving frontend assets and API endpoints.

Routes static assets to the frontend/ directory and delegates /api/* calls
to the FrontendApiService handler with trusted socket client IP extraction.
"""

import argparse
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from frontend_api import FrontendApiService
from tools.logging_utils import get_structured_logger

LOGGER = get_structured_logger(__name__)


class FrontendDevServerHandler(BaseHTTPRequestHandler):
    """HTTP handler serving frontend UI assets and routing API requests."""

    api_service: FrontendApiService | None = None
    frontend_dir: Path = Path(__file__).resolve().parent / "frontend"

    def do_OPTIONS(self) -> None:
        """Handle CORS pre-flight requests with permissive local headers."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            (
                "Content-Type, Authorization, X-Tracking-Token, "
                "X-Recipient-Token, X-Volunteer-Token"
            ),
        )
        self.end_headers()

    def do_GET(self) -> None:
        """Route GET requests to API service or static asset delivery."""
        if self.path.startswith("/api/"):
            self._handle_api_call("GET")
        else:
            self._serve_static_asset()

    def do_POST(self) -> None:
        """Route POST requests to API service."""
        if self.path.startswith("/api/"):
            self._handle_api_call("POST")
        else:
            self.send_error(405, "Method Not Allowed for static assets")

    def _handle_api_call(self, method: str) -> None:
        """Dispatch HTTP call to FrontendApiService using socket client IP."""
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b""
        body_str = body_bytes.decode("utf-8") if body_bytes else ""

        # Extract trusted client IP from the actual TCP socket connection
        client_ip = self.client_address[0] if self.client_address else "127.0.0.1"

        headers_dict = dict(self.headers.items())
        event: dict[str, Any] = {
            "httpMethod": method,
            "path": self.path.split("?")[0],
            "queryStringParameters": {},
            "headers": headers_dict,
            "body": body_str,
            "client_ip": client_ip,
            "requestContext": {
                "identity": {"sourceIp": client_ip},
                "http": {"sourceIp": client_ip},
            },
        }

        if self.api_service is None:
            self.api_service = FrontendApiService()

        response = self.api_service.handle_request(event)
        status = response.get("statusCode", 500)
        headers = response.get("headers", {})
        body = response.get("body", "")

        self.send_response(status)
        for h_key, h_val in headers.items():
            self.send_header(h_key, h_val)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _serve_static_asset(self) -> None:
        """Serve HTML, CSS, or JS file from the frontend/ directory."""
        raw_path = self.path.split("?")[0]

        # Iteratively decode percent-encoding to neutralize nested/double encoding
        decoded_path = urllib.parse.unquote(raw_path)
        while "%" in decoded_path:
            new_decoded = urllib.parse.unquote(decoded_path)
            if new_decoded == decoded_path:
                break
            decoded_path = new_decoded

        # Reject null byte injection attacks
        if "\x00" in decoded_path:
            self.send_error(400, "Bad Request: Null byte detected")
            return

        clean_path = decoded_path.lstrip("/")
        if not clean_path or clean_path in ("/", ""):
            clean_path = "index.html"

        # Explicit check for directory traversal segments (e.g. .., ..., ...., /./)
        path_segments = [p for p in clean_path.replace("\\", "/").split("/") if p]
        has_dot_traversal = (
            any(set(p) == {"."} for p in path_segments)
            or any(".." in p for p in path_segments)
        )
        if has_dot_traversal:
            self.send_error(403, "Access Denied")
            return

        file_path = (self.frontend_dir / clean_path).resolve()
        # Directory containment verification
        try:
            file_path.relative_to(self.frontend_dir.resolve())
        except ValueError:
            self.send_error(403, "Access Denied")
            return

        if not file_path.exists():
            # For missing assets with extensions, return 404 (do not leak index.html)
            if file_path.suffix and file_path.suffix != ".html":
                self.send_error(404, "File Not Found")
                return
            # SPA fallback: route client-side paths to index.html
            file_path = self.frontend_dir / "index.html"
        elif file_path.is_dir():
            file_path = self.frontend_dir / "index.html"

        if not file_path.exists():
            self.send_error(404, "File Not Found")
            return

        content_type, _ = mimetypes.guess_type(str(file_path))
        content_type = content_type or "application/octet-stream"

        try:
            content = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except OSError as exc:
            LOGGER.error("Failed reading file %s: %s", file_path, exc)
            self.send_error(500, "Internal Server Error")


def create_local_dev_service() -> FrontendApiService:
    """Create in-memory stateful service with seeded recipients and volunteers."""
    from recipients_repo import RecipientsRepository
    from tests.test_e2e_multi_day_simulation import (
        create_simulation_environment,
        seed_entities,
    )
    from volunteers_repo import VolunteersRepository

    _, frontend_api, _, mock_dynamo, config = create_simulation_environment()
    object.__setattr__(
        frontend_api._config,
        "coordinator_api_key",
        "dev-insecure-coordinator-key-for-local-testing-only-32chars",
    )
    rec_repo = RecipientsRepository(dynamodb_resource=mock_dynamo, config=config)
    vol_repo = VolunteersRepository(dynamodb_resource=mock_dynamo, config=config)
    seed_entities(rec_repo, vol_repo)
    return frontend_api


def create_aws_production_service() -> FrontendApiService:
    """Create production service connected directly to live AWS DynamoDB tables."""
    import os

    import boto3

    from agent.orchestrator import StrandsOrchestrator
    from audit_repo import AuditRepository
    from config import load_app_configuration
    from donations_repo import DonationsRepository
    from recipients_repo import RecipientsRepository
    from volunteers_repo import VolunteersRepository

    if not os.environ.get("AWS_REGION"):
        os.environ["AWS_REGION"] = "ap-south-1"
    if not os.environ.get("ENVIRONMENT"):
        os.environ["ENVIRONMENT"] = "dev"

    config = load_app_configuration()
    ddb = boto3.resource("dynamodb", region_name=config.aws_region)
    donations_repo = DonationsRepository(dynamodb_resource=ddb, config=config)
    recipients_repo = RecipientsRepository(dynamodb_resource=ddb, config=config)
    volunteers_repo = VolunteersRepository(dynamodb_resource=ddb, config=config)
    audit_repo = AuditRepository(dynamodb_resource=ddb, config=config)

    orchestrator = StrandsOrchestrator(
        donations_repo=donations_repo,
        recipients_repo=recipients_repo,
        volunteers_repo=volunteers_repo,
        audit_repo=audit_repo,
        config=config,
    )
    return FrontendApiService(
        donations_repo=donations_repo,
        recipients_repo=recipients_repo,
        volunteers_repo=volunteers_repo,
        audit_repo=audit_repo,
        config=config,
        orchestrator=orchestrator,
    )


def run_dev_server(port: int = 8080, use_mock: bool = False) -> None:
    """Run local development server listening on specified port."""
    if use_mock:
        LOGGER.info("Starting local server with in-memory mock and seed entities.")
        FrontendDevServerHandler.api_service = create_local_dev_service()
    else:
        LOGGER.info("Starting server connected directly to live AWS DynamoDB tables.")
        FrontendDevServerHandler.api_service = create_aws_production_service()

    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, FrontendDevServerHandler)
    mode_str = "IN-MEMORY MOCK" if use_mock else "LIVE AWS DYNAMODB"
    LOGGER.info(
        "Surplus Router Frontend Server running at http://127.0.0.1:%d [%s mode]",
        port,
        mode_str,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Server terminated by user.")
        httpd.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Surplus Router Local Server")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use in-memory mock repositories and seed entities for local testing",
    )
    args = parser.parse_args()
    run_dev_server(port=args.port, use_mock=args.mock)
