"""Unit tests for src/reset.py.

All external I/O is mocked:
  - config providers  → MagicMock via monkeypatch
  - S3Manager         → MagicMock via monkeypatch
  - builtins.input    → patched for confirmation prompt tests
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, "src")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_podcast(name: str, url: str = "https://example.com/feed.xml", enabled: bool = True):
    p = MagicMock()
    p.name = name
    p.url = url
    p.enabled = enabled
    return p


# ---------------------------------------------------------------------------
# _podcast_slug
# ---------------------------------------------------------------------------

class TestPodcastSlug:
    def test_basic_slug(self):
        from reset import _podcast_slug
        assert _podcast_slug("My Great Podcast") == "my-great-podcast"

    def test_special_chars_replaced(self):
        from reset import _podcast_slug
        assert _podcast_slug("The Best One Yet!") == "the-best-one-yet"

    def test_slug_truncated_to_60(self):
        from reset import _podcast_slug
        long_name = "a" * 100
        assert len(_podcast_slug(long_name)) == 60

    def test_empty_name_returns_podcast(self):
        from reset import _podcast_slug
        assert _podcast_slug("") == "podcast"

    def test_only_special_chars_returns_podcast(self):
        from reset import _podcast_slug
        assert _podcast_slug("!!!") == "podcast"


# ---------------------------------------------------------------------------
# _collect_slugs
# ---------------------------------------------------------------------------

class TestCollectSlugs:
    """_collect_slugs patches config_provider and utils since reset.py uses lazy imports."""

    def _patch(self, monkeypatch, yt_provider, rss_provider, extract_fn=None):
        import config_provider as cp
        import utils as u
        monkeypatch.setattr(cp, "get_config_provider", lambda: yt_provider)
        monkeypatch.setattr(cp, "get_podcast_config_provider", lambda: rss_provider)
        if extract_fn:
            monkeypatch.setattr(u, "extract_playlist_id", extract_fn)

    def test_returns_yt_and_rss_slugs(self, monkeypatch):
        """Returns slugs from both YouTube and RSS providers."""
        from reset import _collect_slugs

        yt_podcast = _make_podcast("My YT Show", url="PLabc123")
        rss_podcast = _make_podcast("My RSS Podcast", url="https://rss.example.com/feed")

        mock_yt = MagicMock(); mock_yt.get_podcasts.return_value = [yt_podcast]
        mock_rss = MagicMock(); mock_rss.get_podcasts.return_value = [rss_podcast]
        self._patch(monkeypatch, mock_yt, mock_rss, extract_fn=lambda url: "PLabc123")

        result = _collect_slugs()
        assert len(result) == 2
        assert result[0] == ("My YT Show", "PLabc123")
        assert result[1][0] == "My RSS Podcast"

    def test_skips_disabled_podcasts(self, monkeypatch):
        """Disabled podcasts are excluded from the slug list."""
        from reset import _collect_slugs

        enabled = _make_podcast("Enabled Show", enabled=True)
        disabled = _make_podcast("Disabled Show", enabled=False)

        mock_yt = MagicMock(); mock_yt.get_podcasts.return_value = [enabled, disabled]
        mock_rss = MagicMock(); mock_rss.get_podcasts.return_value = []
        self._patch(monkeypatch, mock_yt, mock_rss, extract_fn=lambda url: "slug")

        result = _collect_slugs()
        assert len(result) == 1
        assert result[0][0] == "Enabled Show"

    def test_yt_provider_exception_is_swallowed(self, monkeypatch):
        """If the YT config provider raises, RSS slugs are still returned."""
        from reset import _collect_slugs

        rss_podcast = _make_podcast("RSS Only")
        bad_yt = MagicMock(); bad_yt.get_podcasts.side_effect = RuntimeError("no yt")
        mock_rss = MagicMock(); mock_rss.get_podcasts.return_value = [rss_podcast]
        self._patch(monkeypatch, bad_yt, mock_rss)

        result = _collect_slugs()
        assert len(result) == 1
        assert result[0][0] == "RSS Only"

    def test_rss_provider_exception_is_swallowed(self, monkeypatch):
        """If the RSS config provider raises, YT slugs are still returned."""
        from reset import _collect_slugs

        yt_podcast = _make_podcast("YT Only", url="PLxyz")
        mock_yt = MagicMock(); mock_yt.get_podcasts.return_value = [yt_podcast]
        bad_rss = MagicMock(); bad_rss.get_podcasts.side_effect = RuntimeError("no rss")
        self._patch(monkeypatch, mock_yt, bad_rss, extract_fn=lambda url: "PLxyz")

        result = _collect_slugs()
        assert len(result) == 1
        assert result[0][0] == "YT Only"

    def test_extract_playlist_id_exception_falls_back_to_url(self, monkeypatch):
        """If extract_playlist_id raises, the raw URL is used as the slug."""
        from reset import _collect_slugs

        yt_podcast = _make_podcast("Bad URL Show", url="https://bad-url")
        mock_yt = MagicMock(); mock_yt.get_podcasts.return_value = [yt_podcast]
        mock_rss = MagicMock(); mock_rss.get_podcasts.return_value = []
        self._patch(monkeypatch, mock_yt, mock_rss, extract_fn=lambda url: (_ for _ in ()).throw(ValueError("bad")))

        result = _collect_slugs()
        assert result[0][1] == "https://bad-url"


# ---------------------------------------------------------------------------
# _count_episodes
# ---------------------------------------------------------------------------

class TestCountEpisodes:
    def test_returns_count_on_success(self):
        from reset import _count_episodes
        mock_s3 = MagicMock()
        mock_s3.list_existing_episodes.return_value = ["ep1.mp3", "ep2.mp3", "ep3.mp3"]
        assert _count_episodes(mock_s3) == 3

    def test_returns_zero_on_exception(self):
        from reset import _count_episodes
        mock_s3 = MagicMock()
        mock_s3.list_existing_episodes.side_effect = RuntimeError("S3 down")
        assert _count_episodes(mock_s3) == 0


# ---------------------------------------------------------------------------
# run_reset
# ---------------------------------------------------------------------------

class TestRunReset:
    def _setup(self, monkeypatch, podcasts=None, reset_result=None):
        """Common patch setup for run_reset tests."""
        if podcasts is None:
            podcasts = [_make_podcast("Test Podcast")]
        if reset_result is None:
            reset_result = {"episodes_deleted": 3, "feed_deleted": True, "manifest_deleted": True}

        monkeypatch.setenv("S3_BUCKET", "my-bucket")

        mock_slugs = [("Test Podcast", "test-podcast")]
        monkeypatch.setattr("reset._collect_slugs", lambda: mock_slugs)

        mock_s3_instance = MagicMock()
        mock_s3_instance.list_existing_episodes.return_value = ["ep1.mp3", "ep2.mp3", "ep3.mp3"]
        mock_s3_instance.reset_podcast.return_value = reset_result

        mock_s3_cls = MagicMock(return_value=mock_s3_instance)
        # S3Manager is lazily imported inside run_reset() — patch the source module
        import s3_manager as _s3m
        monkeypatch.setattr(_s3m, "S3Manager", mock_s3_cls)

        return mock_s3_instance, mock_s3_cls

    def test_returns_1_when_no_s3_bucket(self, monkeypatch):
        """Returns exit code 1 when S3_BUCKET is not set."""
        monkeypatch.delenv("S3_BUCKET", raising=False)
        import reset
        assert reset.run_reset(force=True) == 1

    def test_returns_0_when_no_podcasts(self, monkeypatch):
        """Returns 0 cleanly when no enabled podcasts are found."""
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setattr("reset._collect_slugs", lambda: [])
        import reset
        assert reset.run_reset(force=True) == 0

    def test_force_skips_prompt_and_returns_0(self, monkeypatch):
        """force=True skips confirmation and resets successfully (exit 0)."""
        self._setup(monkeypatch)
        import reset
        assert reset.run_reset(force=True) == 0

    def test_confirms_yes_and_resets(self, monkeypatch):
        """User answers 'y' at prompt → reset proceeds and returns 0."""
        self._setup(monkeypatch)
        import reset
        with patch("builtins.input", return_value="y"):
            assert reset.run_reset(force=False) == 0

    def test_confirms_yes_full_word(self, monkeypatch):
        """User answers 'yes' at prompt → reset proceeds."""
        self._setup(monkeypatch)
        import reset
        with patch("builtins.input", return_value="yes"):
            assert reset.run_reset(force=False) == 0

    def test_aborts_on_n(self, monkeypatch):
        """User answers 'n' → aborted, returns 1."""
        self._setup(monkeypatch)
        import reset
        with patch("builtins.input", return_value="n"):
            assert reset.run_reset(force=False) == 1

    def test_aborts_on_empty_answer(self, monkeypatch):
        """User presses Enter (empty answer) → aborted, returns 1."""
        self._setup(monkeypatch)
        import reset
        with patch("builtins.input", return_value=""):
            assert reset.run_reset(force=False) == 1

    def test_aborts_on_eof_error(self, monkeypatch):
        """EOFError at input prompt → aborted, returns 1."""
        self._setup(monkeypatch)
        import reset
        with patch("builtins.input", side_effect=EOFError):
            assert reset.run_reset(force=False) == 1

    def test_aborts_on_keyboard_interrupt(self, monkeypatch):
        """KeyboardInterrupt at input prompt → aborted, returns 1."""
        self._setup(monkeypatch)
        import reset
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            assert reset.run_reset(force=False) == 1

    def test_s3_reset_exception_does_not_abort_loop(self, monkeypatch):
        """If S3 reset fails for one podcast, the function still returns 0."""
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setattr("reset._collect_slugs", lambda: [
            ("Pod A", "pod-a"),
            ("Pod B", "pod-b"),
        ])

        call_count = 0

        def make_s3(bucket, playlist_id):
            nonlocal call_count
            call_count += 1
            m = MagicMock()
            m.list_existing_episodes.return_value = []
            if playlist_id == "pod-a":
                m.reset_podcast.side_effect = RuntimeError("S3 error")
            else:
                m.reset_podcast.return_value = {
                    "episodes_deleted": 0, "feed_deleted": True, "manifest_deleted": True
                }
            return m

        import s3_manager as _s3m
        monkeypatch.setattr(_s3m, "S3Manager", make_s3)

        import reset
        # Should not raise and should still return 0 (best-effort)
        result = reset.run_reset(force=True)
        assert result == 0
        assert call_count >= 2  # S3Manager instantiated for both podcasts

    def test_reset_calls_s3_reset_podcast(self, monkeypatch):
        """run_reset calls s3.reset_podcast() for each podcast."""
        s3_instance, s3_cls = self._setup(monkeypatch)
        import reset
        reset.run_reset(force=True)
        s3_instance.reset_podcast.assert_called()
