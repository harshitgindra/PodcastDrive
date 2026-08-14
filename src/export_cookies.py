"""Export YouTube/Google cookies from Firefox to Netscape cookies.txt format.

Reads directly from Firefox's SQLite database (copies first to avoid locks).
No network access needed — instant execution.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Firefox profile path (macOS)
FIREFOX_PROFILES_DIR = Path.home() / "Library/Application Support/Firefox/Profiles"
COOKIE_DOMAINS = (".youtube.com", ".google.com", ".googlevideo.com")


def find_firefox_profile() -> Path:
    """Find the default-release Firefox profile."""
    if not FIREFOX_PROFILES_DIR.exists():
        return None
    for p in FIREFOX_PROFILES_DIR.iterdir():
        if p.name.endswith(".default-release"):
            return p
    # Fallback to any profile
    for p in FIREFOX_PROFILES_DIR.iterdir():
        if (p / "cookies.sqlite").exists():
            return p
    return None


def export_cookies(output_path: str) -> int:
    """Export Firefox cookies to Netscape format. Returns cookie count."""
    profile = find_firefox_profile()
    if not profile:
        print("ERROR: No Firefox profile found", file=sys.stderr)
        return -1

    cookies_db = profile / "cookies.sqlite"
    if not cookies_db.exists():
        print(f"ERROR: No cookies.sqlite in {profile}", file=sys.stderr)
        return -1

    # Copy DB to temp file to avoid locking issues
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(tmp_fd)
    try:
        shutil.copy2(cookies_db, tmp_path)
        # Also copy WAL/SHM if they exist (for latest data)
        for ext in ("-wal", "-shm"):
            wal = str(cookies_db) + ext
            if os.path.exists(wal):
                shutil.copy2(wal, tmp_path + ext)

        conn = sqlite3.connect(tmp_path)
        conn.execute("PRAGMA journal_mode=wal")

        rows = conn.execute(
            """
            SELECT host, name, value, path, expiry, isSecure, isHttpOnly
            FROM moz_cookies
            WHERE host LIKE '%youtube.com%'
               OR host LIKE '%google.com%'
               OR host LIKE '%googlevideo.com%'
            ORDER BY host, name
            """
        ).fetchall()
        conn.close()
    finally:
        os.unlink(tmp_path)
        for ext in ("-wal", "-shm"):
            if os.path.exists(tmp_path + ext):
                os.unlink(tmp_path + ext)

    if not rows:
        print("WARNING: No YouTube/Google cookies found in Firefox", file=sys.stderr)
        return 0

    # Write Netscape cookies.txt format
    with open(output_path, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write("# Exported from Firefox by PodcastDrive/export_cookies.py\n")
        f.write("# https://curl.haxx.se/rfc/cookie_spec.html\n\n")

        for host, name, value, path, expiry, is_secure, _is_http_only in rows:
            # Netscape format: domain, flag, path, secure, expiry, name, value
            include_subdomains = "TRUE" if host.startswith(".") else "FALSE"
            secure = "TRUE" if is_secure else "FALSE"
            f.write(f"{host}\t{include_subdomains}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n")

    return len(rows)


if __name__ == "__main__":
    output = sys.argv[1] if len(sys.argv) > 1 else "cookies.txt"
    count = export_cookies(output)
    if count < 0:
        sys.exit(1)
    print(f"Exported {count} cookies to {output}")
