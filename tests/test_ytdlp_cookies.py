"""Unit tests for src/ytdlp_cookies.py."""

from pathlib import Path
from unittest.mock import patch

import pytest

import ytdlp_cookies
from ytdlp_cookies import (
    MIN_COOKIE_BYTES,
    cookie_args,
    get_cookies_path,
    inject_cookies,
)


def write_cookies(path: Path, size: int = MIN_COOKIE_BYTES) -> Path:
    """Create a cookies.txt of exactly *size* bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("c" * size)
    return path


@pytest.fixture
def candidates(monkeypatch):
    """Take full control of the candidate list so discovery is deterministic."""
    holder: list[Path] = []
    monkeypatch.setattr(ytdlp_cookies, "_candidates", lambda: list(holder))
    monkeypatch.delenv("YTDLP_COOKIES", raising=False)
    return holder


class TestGetCookiesPath:
    def test_returns_none_when_no_candidate_exists(self, candidates, tmp_path):
        candidates.append(tmp_path / "nope.txt")
        assert get_cookies_path() is None

    def test_returns_none_when_candidate_list_is_empty(self, candidates):
        assert get_cookies_path() is None

    def test_finds_valid_file(self, candidates, tmp_path):
        target = write_cookies(tmp_path / "cookies.txt")
        candidates.append(target)
        assert get_cookies_path() == str(target)

    def test_rejects_file_below_size_floor(self, candidates, tmp_path):
        # A near-empty cookies.txt holds no usable session; accepting it means
        # silently downloading unauthenticated and hitting bot detection later.
        candidates.append(write_cookies(tmp_path / "cookies.txt", size=MIN_COOKIE_BYTES - 1))
        assert get_cookies_path() is None

    def test_accepts_file_exactly_at_size_floor(self, candidates, tmp_path):
        target = write_cookies(tmp_path / "cookies.txt", size=MIN_COOKIE_BYTES)
        candidates.append(target)
        assert get_cookies_path() == str(target)

    def test_first_valid_candidate_wins(self, candidates, tmp_path):
        first = write_cookies(tmp_path / "a" / "cookies.txt")
        second = write_cookies(tmp_path / "b" / "cookies.txt")
        candidates.extend([first, second])
        assert get_cookies_path() == str(first)

    def test_skips_invalid_candidate_and_continues(self, candidates, tmp_path):
        small = write_cookies(tmp_path / "a" / "cookies.txt", size=1)
        good = write_cookies(tmp_path / "b" / "cookies.txt")
        candidates.extend([tmp_path / "missing.txt", small, good])
        assert get_cookies_path() == str(good)

    def test_directory_is_not_mistaken_for_a_file(self, candidates, tmp_path):
        as_dir = tmp_path / "cookies.txt"
        as_dir.mkdir()
        candidates.append(as_dir)
        assert get_cookies_path() is None

    def test_duplicate_candidates_are_only_checked_once(self, candidates, tmp_path):
        # repo-root and cwd collapse to the same file when run from the repo.
        target = write_cookies(tmp_path / "cookies.txt")
        candidates.extend([target, target])
        assert get_cookies_path() == str(target)


class TestCookiesEnvOverride:
    def test_override_wins_over_discovery(self, candidates, tmp_path, monkeypatch):
        discovered = write_cookies(tmp_path / "discovered.txt")
        override = write_cookies(tmp_path / "override.txt")
        candidates.append(discovered)
        monkeypatch.setenv("YTDLP_COOKIES", str(override))
        assert get_cookies_path() == str(override)

    def test_override_bypasses_size_floor(self, candidates, tmp_path, monkeypatch):
        override = write_cookies(tmp_path / "override.txt", size=1)
        monkeypatch.setenv("YTDLP_COOKIES", str(override))
        assert get_cookies_path() == str(override)

    def test_missing_override_falls_back_to_discovery(
        self, candidates, tmp_path, monkeypatch, caplog
    ):
        discovered = write_cookies(tmp_path / "discovered.txt")
        candidates.append(discovered)
        monkeypatch.setenv("YTDLP_COOKIES", str(tmp_path / "absent.txt"))
        assert get_cookies_path() == str(discovered)
        assert "does not exist" in caplog.text

    def test_empty_override_is_ignored(self, candidates, tmp_path, monkeypatch):
        discovered = write_cookies(tmp_path / "discovered.txt")
        candidates.append(discovered)
        monkeypatch.setenv("YTDLP_COOKIES", "")
        assert get_cookies_path() == str(discovered)


class TestDefaultCandidates:
    def test_includes_repo_root_and_cwd_and_home(self):
        found = ytdlp_cookies._candidates()
        assert all(p.name == "cookies.txt" for p in found)
        repo_root = Path(ytdlp_cookies.__file__).resolve().parent.parent
        assert repo_root / "cookies.txt" in found
        assert Path.cwd() / "cookies.txt" in found
        # MediaSync's historical locations must stay reachable.
        assert Path.home() / "cookies.txt" in found
        assert Path.home() / ".config" / "yt-dlp" / "cookies.txt" in found
        # The podcast pipeline's historical location must stay reachable.
        assert Path.home() / "PodcastDrive" / "cookies.txt" in found


class TestInjectCookies:
    def test_does_not_overwrite_existing_cookiefile(self):
        opts = {"cookiefile": "/existing/path.txt"}
        assert inject_cookies(opts)["cookiefile"] == "/existing/path.txt"

    def test_adds_cookiefile_when_path_found(self):
        with patch("ytdlp_cookies.get_cookies_path", return_value="/found/cookies.txt"):
            assert inject_cookies({})["cookiefile"] == "/found/cookies.txt"

    def test_leaves_opts_untouched_when_no_cookies(self):
        with patch("ytdlp_cookies.get_cookies_path", return_value=None):
            assert inject_cookies({}) == {}

    def test_returns_same_dict_for_chaining(self):
        opts = {}
        with patch("ytdlp_cookies.get_cookies_path", return_value=None):
            assert inject_cookies(opts) is opts


class TestCookieArgs:
    def test_builds_flags_when_found(self):
        with patch("ytdlp_cookies.get_cookies_path", return_value="/found/cookies.txt"):
            assert cookie_args() == ["--cookies", "/found/cookies.txt"]

    def test_empty_when_not_found(self):
        with patch("ytdlp_cookies.get_cookies_path", return_value=None):
            assert cookie_args() == []
