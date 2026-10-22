"""Export YouTube/Google cookies from Firefox to Netscape cookies.txt format.

Reads directly from Firefox's SQLite database (copies first to avoid locks).
No network access needed — instant execution.

``--filter PATH`` instead rewrites an existing cookies.txt (e.g. the full jar
yt-dlp exports from Chrome) down to the YouTube/Google domains yt-dlp needs.
Every file this module writes is created 0600 and replaced atomically.
"""

import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Firefox profile path (macOS)
FIREFOX_PROFILES_DIR = Path.home() / "Library/Application Support/Firefox/Profiles"
COOKIE_DOMAINS = (".youtube.com", ".google.com", ".googlevideo.com")
_HTTPONLY_PREFIX = "#HttpOnly_"
_NETSCAPE_HEADER = "# Netscape HTTP Cookie File\n"


def is_allowed_host(host: str) -> bool:
    """True when *host* is one of COOKIE_DOMAINS or a subdomain of one."""
    bare = "." + host.lower().removeprefix(_HTTPONLY_PREFIX.lower()).lstrip(".")
    return any(bare == domain or bare.endswith(domain) for domain in COOKIE_DOMAINS)


def _write_private(path: str, text: str) -> None:
    """Atomically replace *path* with *text*, never readable by other users."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cookies-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def filter_cookie_file(path: str) -> int:
    """Rewrite the Netscape cookie file at *path* to allowed domains only.

    Returns the number of cookies kept.
    """
    with open(path) as fh:
        lines = fh.read().splitlines()
    kept = [
        line
        for line in lines
        if line.strip()
        and (not line.startswith("#") or line.startswith(_HTTPONLY_PREFIX))
        and is_allowed_host(line.split("\t", 1)[0])
    ]
    _write_private(
        path,
        _NETSCAPE_HEADER + "# Filtered by PodcastDrive/export_cookies.py\n\n" + "".join(f"{line}\n" for line in kept),
    )
    return len(kept)


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

    # SQL LIKE '%google.com%' also matches e.g. notgoogle.com; keep exact domains only.
    rows = [row for row in rows if is_allowed_host(row[0])]
    if not rows:
        print("WARNING: No YouTube/Google cookies found in Firefox", file=sys.stderr)
        return 0

    lines = [
        _NETSCAPE_HEADER,
        "# Exported from Firefox by PodcastDrive/export_cookies.py\n",
        "# https://curl.haxx.se/rfc/cookie_spec.html\n\n",
    ]
    for host, name, value, path, expiry, is_secure, _is_http_only in rows:
        # Netscape format: domain, flag, path, secure, expiry, name, value
        include_subdomains = "TRUE" if host.startswith(".") else "FALSE"
        secure = "TRUE" if is_secure else "FALSE"
        lines.append(f"{host}\t{include_subdomains}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n")
    _write_private(output_path, "".join(lines))

    return len(rows)


def main(argv: list[str]) -> int:
    if argv[:1] == ["--filter"]:
        if len(argv) != 2:
            print("usage: export_cookies.py --filter PATH", file=sys.stderr)
            return 2
        kept = filter_cookie_file(argv[1])
        print(f"Kept {kept} YouTube/Google cookies in {argv[1]}")
        return 0 if kept else 1
    output = argv[0] if argv else "cookies.txt"
    count = export_cookies(output)
    if count < 0:
        return 1
    print(f"Exported {count} cookies to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
