"""Auto-inject cookies.txt into yt-dlp calls.

Discovers cookies.txt from project root and provides it to yt-dlp
for both extraction and download phases. This avoids YouTube bot detection.
"""

import os
from pathlib import Path


def get_cookies_path() -> str | None:
    """Find cookies.txt in project root or home dir."""
    candidates = [
        Path(__file__).parent.parent / "cookies.txt",
        Path.home() / "PodcastDrive" / "cookies.txt",
    ]
    for p in candidates:
        if p.exists() and p.stat().st_size > 100:
            return str(p)
    return None


def inject_cookies(ydl_opts: dict) -> dict:
    """Add cookiefile to yt-dlp options if cookies.txt exists."""
    if "cookiefile" not in ydl_opts:
        cookies = get_cookies_path()
        if cookies:
            ydl_opts["cookiefile"] = cookies
    return ydl_opts
