"""Telegram notification for run summaries.

Sends a concise per-podcast summary at the end of each run so you know
what happened without checking logs.

Env vars:
    TELEGRAM_BOT_TOKEN – Bot API token.
    TELEGRAM_CHAT_ID   – Chat/user ID to send to.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_run_notification(
    results: list[dict],
    *,
    elapsed_secs: int = 0,
    status: str = "success",
) -> bool:
    """Format and send a Telegram message summarizing the run.

    Args:
        results: List of per-podcast dicts with keys:
            name, new_episodes, failed, bot_detected (all optional except name)
        elapsed_secs: Total run duration in seconds.
        status: Overall run status (success/partial_failure/failure).

    Returns:
        True if sent successfully, False otherwise.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping notification")
        return False

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

    message = "\n".join(lines)
    return _send_telegram(token, chat_id, message)


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    """POST message to Telegram Bot API."""
    url = TELEGRAM_API.format(token=token)
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            logger.info("Telegram notification sent")
            return True
    except (urllib.error.URLError, OSError) as exc:
        logger.error("Failed to send Telegram notification: %s", exc)
        return False


# CLI entry point — called from run.sh
if __name__ == "__main__":
    import sys

    results_file = sys.argv[1] if len(sys.argv) > 1 else None
    if not results_file or not os.path.exists(results_file):
        print("Usage: python notifier.py <results.json>", file=sys.stderr)
        sys.exit(1)

    with open(results_file) as f:
        data = json.load(f)

    ok = send_run_notification(
        data.get("results", []),
        elapsed_secs=data.get("elapsed_secs", 0),
        status=data.get("status", "success"),
    )
    sys.exit(0 if ok else 1)
