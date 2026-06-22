#!/usr/bin/env python3
"""PodcastDrive webhook server — triggers run.sh via HTTP."""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORT = int(os.environ.get("WEBHOOK_PORT", "9090"))
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
        os.kill(pid, 0)  # Check if process exists
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
            capture_output=True, text=True, timeout=5
        )
        return result.stdout
    except Exception as e:
        return f"(error reading logs: {e})"


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        """Suppress default request logging to stderr."""
        pass

    def _check_auth(self) -> bool:
        """Validate Bearer token."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[7:]
            return hmac.compare_digest(provided, TOKEN)
        # Also accept ?token= query param (for simplicity from Shortcuts)
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            if "token" in params:
                return hmac.compare_digest(params["token"], TOKEN)
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
            self._respond(200, {
                "running": is_running(),
                "logs": tail_log(20)
            })

        elif path == "/run":
            if is_running():
                self._respond(409, {
                    "error": "already running",
                    "message": "A run is already in progress. Check /status for details."
                })
            else:
                # Launch in background
                subprocess.Popen(
                    ["bash", "-c", f"cd {PROJECT_DIR} && TRIGGER=webhook ./run.sh >> logs/cron.log 2>&1"],
                    start_new_session=True
                )
                self._respond(200, {
                    "status": "started",
                    "message": "run.sh launched in background. Check /status for progress."
                })

        elif path == "/logs":
            lines = 50
            self._respond(200, {"logs": tail_log(lines)})

        else:
            self._respond(404, {"error": "not found", "endpoints": ["/run", "/status", "/logs", "/health"]})

    do_POST = do_GET  # Accept both GET and POST for /run


def main():
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    print(f"PodcastDrive webhook listening on port {PORT}")
    print(f"Endpoints: /run, /status, /logs, /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
