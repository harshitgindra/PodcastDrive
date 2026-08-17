"""OneDrive client for MediaSync via Microsoft Graph API.

Uses OAuth2 refresh token for authentication. Access tokens are refreshed
automatically (they expire hourly; refresh tokens last 90 days rolling).

Files >4MB use resumable upload sessions (chunked).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_API = "https://graph.microsoft.com/v1.0"
CHUNK_SIZE = 4 * 1024 * 1024  # 4MB chunks for resumable upload
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024  # Files <= 4MB use simple PUT


class OneDriveError(Exception):
    """Raised when a OneDrive API call fails."""


class OneDriveClient:
    """Client for uploading and managing files on OneDrive."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        """Initialize and obtain a fresh access token.

        Args:
            client_id: Azure app client ID.
            client_secret: Azure app client secret value.
            refresh_token: OAuth2 refresh token (from onedrive_oauth.py).
        """
        if not refresh_token:
            raise OneDriveError("OneDrive refresh token is required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token = self._refresh_access_token()

    @property
    def current_refresh_token(self) -> str:
        """Return the current refresh token (may have been rotated)."""
        return self._refresh_token

    def check_health(self) -> bool:
        """Verify the OneDrive connection is working.

        Makes a lightweight API call (get drive info) to confirm the access
        token is valid. Returns True if healthy, False otherwise.
        """
        url = f"{GRAPH_API}/me/drive"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self._access_token}")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                quota = data.get("quota", {})
                used = quota.get("used", 0)
                total = quota.get("total", 0)
                if total > 0:
                    pct = (used / total) * 100
                    logger.info(
                        "OneDrive healthy: %.1f%% used (%d/%d bytes)",
                        pct, used, total,
                    )
                else:
                    logger.info("OneDrive healthy: connected")
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                logger.error("OneDrive health check failed: token expired or invalid")
            else:
                logger.error("OneDrive health check failed: HTTP %d", exc.code)
            return False
        except Exception as exc:
            logger.error("OneDrive health check failed: %s", exc)
            return False

    def _refresh_access_token(self) -> str:
        """Exchange refresh token for a new access token."""
        data = urllib.parse.urlencode({
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
            "scope": "Files.ReadWrite offline_access",
        }).encode()

        req = urllib.request.Request(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
        except Exception as exc:
            raise OneDriveError(f"Token refresh failed: {exc}") from exc

        if "error" in result:
            raise OneDriveError(f"Token refresh failed: {result.get('error_description', result['error'])}")

        # Update refresh token if a new one was issued
        if result.get("refresh_token"):
            self._refresh_token = result["refresh_token"]
            self._persist_rotated_token(result["refresh_token"])

        logger.debug("Access token refreshed successfully")
        return result["access_token"]

    def _persist_rotated_token(self, new_token: str) -> None:
        """Persist a rotated refresh token to the env file if it exists.

        This prevents token expiry if Microsoft issues a new refresh token
        (rolling 90-day window). Updates MEDIASYNC_ONEDRIVE_REFRESH_TOKEN
        in mediasync.env.
        """
        env_file = Path.cwd() / "mediasync.env"
        if not env_file.is_file():
            logger.debug("No mediasync.env found, skipping token persistence")
            return

        try:
            content = env_file.read_text()
            key = "MEDIASYNC_ONEDRIVE_REFRESH_TOKEN="
            if key in content:
                lines = content.splitlines()
                new_lines = []
                for line in lines:
                    if line.startswith(key) or line.startswith(f"export {key}"):
                        prefix = "export " if line.startswith("export ") else ""
                        new_lines.append(f"{prefix}{key}{new_token}")
                    else:
                        new_lines.append(line)
                env_file.write_text("\n".join(new_lines) + "\n")
                logger.info("Persisted rotated OneDrive refresh token to mediasync.env")
        except Exception as exc:
            logger.warning("Failed to persist rotated token: %s", exc)

    def upload(self, local_path: Path, remote_folder: str, filename: str) -> str:
        """Upload a file to OneDrive.

        Uses simple upload for files <=4MB, resumable upload for larger files.

        Args:
            local_path: Path to the local file.
            remote_folder: Remote folder path (e.g., "MediaSync/Harshit/audio").
            filename: Filename on OneDrive.

        Returns:
            Full remote path of the uploaded file.
        """
        file_size = local_path.stat().st_size
        remote_path = f"{remote_folder}/{filename}"

        if file_size <= SIMPLE_UPLOAD_LIMIT:
            self._simple_upload(local_path, remote_path)
        else:
            self._resumable_upload(local_path, remote_path, file_size)

        logger.info("Uploaded to OneDrive: %s (%d bytes)", remote_path, file_size)
        return remote_path

    def delete_file(self, remote_path: str) -> None:
        """Delete a file from OneDrive. Idempotent (ignores 404)."""
        encoded_path = urllib.parse.quote(remote_path)
        url = f"{GRAPH_API}/me/drive/root:/{encoded_path}"

        req = urllib.request.Request(url, method="DELETE")
        req.add_header("Authorization", f"Bearer {self._access_token}")

        try:
            with urllib.request.urlopen(req, timeout=30):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                logger.debug("File not found (already deleted): %s", remote_path)
                return
            if exc.code == 401:
                self._access_token = self._refresh_access_token()
                self._delete_retry(remote_path)
                return
            raise OneDriveError(f"Delete failed for {remote_path}: HTTP {exc.code}") from exc
        except Exception as exc:
            raise OneDriveError(f"Delete failed for {remote_path}: {exc}") from exc

        logger.info("Deleted from OneDrive: %s", remote_path)

    def _delete_retry(self, remote_path: str) -> None:
        """Retry delete after token refresh."""
        encoded_path = urllib.parse.quote(remote_path)
        url = f"{GRAPH_API}/me/drive/root:/{encoded_path}"

        req = urllib.request.Request(url, method="DELETE")
        req.add_header("Authorization", f"Bearer {self._access_token}")

        try:
            with urllib.request.urlopen(req, timeout=30):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return
            raise OneDriveError(f"Delete failed for {remote_path}: HTTP {exc.code}") from exc

    def _simple_upload(self, local_path: Path, remote_path: str) -> None:
        """Upload a small file (<=4MB) with a single PUT."""
        encoded_path = urllib.parse.quote(remote_path)
        url = f"{GRAPH_API}/me/drive/root:/{encoded_path}:/content"

        file_data = local_path.read_bytes()
        req = urllib.request.Request(url, data=file_data, method="PUT")
        req.add_header("Authorization", f"Bearer {self._access_token}")
        req.add_header("Content-Type", "application/octet-stream")

        try:
            with urllib.request.urlopen(req, timeout=120):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._access_token = self._refresh_access_token()
                self._simple_upload(local_path, remote_path)
                return
            body = exc.read().decode()[:500] if exc.fp else ""
            raise OneDriveError(f"Upload failed for {remote_path}: HTTP {exc.code} — {body}") from exc
        except Exception as exc:
            raise OneDriveError(f"Upload failed for {remote_path}: {exc}") from exc

    def _resumable_upload(self, local_path: Path, remote_path: str, file_size: int) -> None:
        """Upload a large file using a resumable upload session."""
        encoded_path = urllib.parse.quote(remote_path)
        url = f"{GRAPH_API}/me/drive/root:/{encoded_path}:/createUploadSession"

        # Create upload session
        session_body = json.dumps({
            "item": {"@microsoft.graph.conflictBehavior": "replace"},
        }).encode()

        req = urllib.request.Request(url, data=session_body, method="POST")
        req.add_header("Authorization", f"Bearer {self._access_token}")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                session = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._access_token = self._refresh_access_token()
                self._resumable_upload(local_path, remote_path, file_size)
                return
            raise OneDriveError(f"Upload session creation failed: HTTP {exc.code}") from exc

        upload_url = session["uploadUrl"]

        # Upload in chunks
        with open(local_path, "rb") as f:
            offset = 0
            while offset < file_size:
                chunk = f.read(CHUNK_SIZE)
                chunk_size = len(chunk)
                end = offset + chunk_size - 1

                req = urllib.request.Request(upload_url, data=chunk, method="PUT")
                req.add_header("Content-Length", str(chunk_size))
                req.add_header("Content-Range", f"bytes {offset}-{end}/{file_size}")

                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        pass
                except urllib.error.HTTPError as exc:
                    if exc.code in (200, 201, 202):
                        pass  # Success or accepted
                    else:
                        raise OneDriveError(
                            f"Chunk upload failed at offset {offset}: HTTP {exc.code}"
                        ) from exc

                offset += chunk_size
