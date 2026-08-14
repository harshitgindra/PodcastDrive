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

if not TOKEN:
    print("ERROR: WEBHOOK_TOKEN environment variable must be set.", file=sys.stderr)
    sys.exit(1)


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
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/health":
            self._respond(200, {"status": "ok", "running": is_running()})
            return

        if not self._check_auth():
            self._respond(401, {"error": "unauthorized"})
            return

        if path == "/status":
            self._respond(
                200,
                {
                    "running": is_running(),
                    "logs": tail_log(20),
                },
            )

        elif path == "/run":
            if is_running():
                self._respond(
                    409,
                    {
                        "error": "already running",
                        "message": "A run is already in progress. Check /status for details.",
                    },
                )
            else:
                log_file = LOG_FILE.open("a")
                subprocess.Popen(
                    ["./run.sh"],
                    cwd=str(PROJECT_DIR),
                    env={**os.environ, "TRIGGER": "webhook"},
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self._respond(
                    200,
                    {
                        "status": "started",
                        "message": "run.sh launched in background. Check /status for progress.",
                    },
                )

        elif path == "/logs":
            self._respond(200, {"logs": tail_log(50)})

        else:
            self._respond(404, {"error": "not found", "endpoints": ["/run", "/status", "/logs", "/health"]})

    do_POST = do_GET


def main():
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
    main()
