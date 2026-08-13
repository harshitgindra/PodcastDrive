"""Run-completion notifications via Herald.

Formats a per-podcast summary and sends it through Herald (if installed).
When Herald is not installed or not configured, notifications are silently
skipped — this is by design so PodcastDrive works standalone.

Install Herald: pipx install herald (or pip install -e ~/Projects/Herald)
Configure: ~/.config/herald/config.yaml
Requires: Herald >= 0.2.0 (for --message flag support)
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess

logger = logging.getLogger(__name__)

_MIN_HERALD_VERSION = (0, 2, 0)


def send_run_notification(
    results: list[dict],
    *,
    elapsed_secs: int = 0,
    status: str = "success",
) -> bool:
    """Format and send a run summary via Herald.

    Args:
        results: List of per-podcast dicts with keys:
            name, new_episodes, failed, bot_detected (all optional except name)
        elapsed_secs: Total run duration in seconds.
        status: Overall run status (success/partial_failure/failure).

    Returns:
        True if sent successfully, False otherwise (including Herald not installed).
    """
    if not _herald_available():
        logger.debug("Herald not installed — skipping notification")
        return False

    if not _herald_supports_message_flag():
        return False

    message = _format_message(results, elapsed_secs=elapsed_secs, status=status)
    return _send_via_herald(message)


def _format_message(
    results: list[dict],
    *,
    elapsed_secs: int = 0,
    status: str = "success",
) -> str:
    """Build the notification message text."""
    runner = os.environ.get("RUNNER", platform.node() or "unknown")
    mins, secs = divmod(elapsed_secs, 60)

    lines = [f"📡 *PodcastDrive* — {runner} ({mins}m {secs}s)\n"]

    total_new = 0
    total_failed = 0

    for r in results:
        name = r.get("name", "?")
        new = r.get("new_episodes", 0)
        failed = r.get("failed", 0)
        bot = r.get("bot_detected", False)
        error_msg = r.get("error")

        total_new += new
        total_failed += failed

        if error_msg:
            lines.append(f"❌ {name} — {error_msg}")
        elif bot:
            lines.append(f"⚠️ {name} — bot detected")
        elif failed > 0:
            lines.append(f"❌ {name} — {failed} failed, {new} new")
        elif new > 0:
            lines.append(f"✅ {name} — {new} new")
        else:
            lines.append(f"— {name} — up to date")

    # Footer
    status_emoji = "✅" if status == "success" else "⚠️"
    lines.append(f"\n{status_emoji} {total_new} downloaded, {total_failed} failed")

    return "\n".join(lines)


def _herald_available() -> bool:
    """Check if Herald CLI is installed and on PATH."""
    return shutil.which("herald") is not None


def _herald_supports_message_flag() -> bool:
    """Check that installed Herald is >= 0.2.0 (supports --message flag).

    Runs ``herald version``, parses the X.Y.Z output, and compares against
    the minimum required version. On any failure, returns False with a
    warning log — never raises.
    """
    try:
        result = subprocess.run(
            ["herald", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(
                "Herald version check failed (exit %d) — upgrade with `pipx upgrade herald`",
                result.returncode,
            )
            return False

        version_str = result.stdout.strip()
        parts = version_str.split(".")
        if len(parts) < 3:
            logger.warning(
                "Herald <0.2.0 detected (%r) — upgrade with `pipx upgrade herald`",
                version_str,
            )
            return False

        version_tuple = tuple(int(p) for p in parts[:3])
        if version_tuple < _MIN_HERALD_VERSION:
            logger.warning(
                "Herald %s is too old (need >= 0.2.0) — upgrade with `pipx upgrade herald`",
                version_str,
            )
            return False

        return True

    except FileNotFoundError:
        logger.debug("Herald binary not found during version check")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("Herald version check timed out")
        return False
    except (OSError, ValueError) as exc:
        logger.warning("Herald version check failed: %s", exc)
        return False


def _send_via_herald(message: str) -> bool:
    """Call Herald CLI to send the message using the --message flag."""
    try:
        result = subprocess.run(
            ["herald", "notify", "--message", message],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("Notification sent via Herald")
            return True
        else:
            logger.warning("Herald exited %d: %s", result.returncode, result.stderr.strip())
            return False
    except FileNotFoundError:
        logger.debug("Herald binary not found")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("Herald timed out after 30s")
        return False
    except OSError as exc:
        logger.warning("Failed to call Herald: %s", exc)
        return False
