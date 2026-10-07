#!/usr/bin/env python3
"""PodcastDrive webhook server — triggers run.sh via HTTP.

Security:
  - Authentication via Authorization: Bearer <token> header ONLY.
  - Binds to 127.0.0.1 by default (use WEBHOOK_BIND for override).
  - No query-string token support (tokens in URLs leak to logs/history).
  - subprocess.Popen uses cwd= instead of shell interpolation.
"""

import hmac
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = int(os.environ.get("WEBHOOK_PORT", "9090"))
BIND = os.environ.get("WEBHOOK_BIND", "127.0.0.1")
TOKEN = os.environ.get("WEBHOOK_TOKEN", "")
PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", "/home/ec2-user/PodcastDrive"))
LOG_FILE = PROJECT_DIR / "logs" / "cron.log"
LOCK_FILE = PROJECT_DIR / ".podcastdrive.lock"


def validate_config() -> None:
    """Reject unsafe or invalid network configuration before starting."""
    if not TOKEN:
        raise ValueError("WEBHOOK_TOKEN environment variable must be set")
    # This service has no TLS support. Refuse non-loopback binding so a bearer
    # token can never be exposed over a direct, unencrypted network connection.
    if BIND not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(
            "webhook must bind to loopback; use an authenticated TLS reverse proxy or SSM tunnel"
        )
    if not 1 <= PORT <= 65535:
        raise ValueError("WEBHOOK_PORT must be between 1 and 65535")


def is_running() -> bool:
    """Check if run.sh is currently executing (lock file exists with live PID)."""
    if not LOCK_FILE.exists():
        return False
    try:
        pid = int(LOCK_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def tail_log(lines: int = 20) -> str:
    """Return last N lines of the log file."""
    if not LOG_FILE.exists():
        return "(no logs yet)"
    try:
        result = subprocess.run(
            ["tail", f"-{lines}", str(LOG_FILE)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout
    except Exception as e:
        return f"(error reading logs: {e})"


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        """Suppress default request logging to stderr."""
        pass

    def _check_auth(self) -> bool:
        """Validate Bearer token from Authorization header only.

        Query-string tokens are intentionally NOT supported — they leak
        into HTTP access logs, proxy logs, and browser history.
        """
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[7:]
            return hmac.compare_digest(provided, TOKEN)
        return False

    def _respond(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authenticated_path(self) -> str | None:
        if not self._check_auth():
            self._respond(401, {"error": "unauthorized"})
            return None
        # Query parameters are intentionally ignored; credentials are never accepted in URLs.
        return self.path.split("?", 1)[0]

    def _handle_status(self):
        self._respond(
            200,
            {"running": is_running(), "logs": tail_log(20)},
        )

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            # Liveness only, no job state or other operational information.
            self._respond(200, {"status": "ok"})
            return

        if self._authenticated_path() is None:
            return
        self._respond(405, {"error": "method not allowed"})

    def do_POST(self):
        path = self._authenticated_path()
        if path is None:
            return

        if path == "/run":
            if is_running():
                self._respond(409, {"error": "already running"})
                return
            try:
                LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                with LOG_FILE.open("a") as log_file:
                    subprocess.Popen(
                        ["./run.sh"],
                        cwd=str(PROJECT_DIR),
                        env={**os.environ, "TRIGGER": "webhook"},
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except OSError as exc:
                print(f"ERROR: could not launch run.sh: {exc}", file=sys.stderr)
                self._respond(500, {"error": "could not start run"})
                return
            self._respond(202, {"status": "started"})
        elif path == "/status":
            self._handle_status()
        elif path == "/logs":
            self._respond(200, {"logs": tail_log(50)})
        else:
            self._respond(404, {"error": "not found"})


def main():
    try:
        validate_config()
    except ValueError as exc:
        print(f"ERROR: {exc}.", file=sys.stderr)
        return 1
    server = HTTPServer((BIND, PORT), WebhookHandler)
    print(f"PodcastDrive webhook listening on {BIND}:{PORT}")
    print("Endpoints: /run, /status, /logs, /health")
    print("Auth: Authorization: Bearer <WEBHOOK_TOKEN>")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
