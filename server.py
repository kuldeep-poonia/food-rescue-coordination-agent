"""Lightweight development web server serving frontend assets and API endpoints.

Routes static assets to the frontend/ directory and delegates /api/* calls
to the FrontendApiService handler with trusted socket client IP extraction.
"""

import argparse
import mimetypes
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
        clean_path = self.path.split("?")[0].lstrip("/")
        if not clean_path or clean_path in ("/", ""):
            clean_path = "index.html"

        file_path = (self.frontend_dir / clean_path).resolve()
        # Directory traversal prevention
        try:
            file_path.relative_to(self.frontend_dir.resolve())
        except ValueError:
            self.send_error(403, "Access Denied")
            return

        if not file_path.exists() or file_path.is_dir():
            # SPA fallback: route to index.html
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


def run_dev_server(port: int = 8080) -> None:
    """Run local development server listening on specified port."""
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, FrontendDevServerHandler)
    LOGGER.info("Surplus Router Frontend Server running at http://127.0.0.1:%d", port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Server terminated by user.")
        httpd.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Surplus Router Local Server")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    args = parser.parse_args()
    run_dev_server(port=args.port)
