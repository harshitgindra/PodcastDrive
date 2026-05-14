"""Tests for src/podcast_sync.py."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config_provider import PodcastConfig
from podcast_downloader import EpisodeMeta
from podcast_sync import (
    _build_podcast_feed_xml,
    _format_duration,
    _podcast_slug,
    process_podcast_feed,
)


# ---------------------------------------------------------------------------
# _podcast_slug
# ---------------------------------------------------------------------------

class TestPodcastSlug:
    def test_basic(self):
        assert _podcast_slug("My Podcast") == "my-podcast"

    def test_special_chars(self):
        assert _podcast_slug("Hello, World!") == "hello-world"

    def test_leading_trailing_hyphens_stripped(self):
        slug = _podcast_slug("  -test-  ")
        assert not slug.startswith("-")
        assert not slug.endswith("-")

    def test_max_length_60(self):
        long_name = "a " * 50
        assert len(_podcast_slug(long_name)) <= 60

    def test_empty_name_returns_podcast(self):
        assert _podcast_slug("") == "podcast"
        assert _podcast_slug("!!!") == "podcast"


# ---------------------------------------------------------------------------
# _format_duration
# ---------------------------------------------------------------------------

class TestFormatDuration:
    def test_zero(self):
        assert _format_duration(0) == "0:00"

    def test_minutes_seconds(self):
        assert _format_duration(330) == "5:30"

    def test_hours(self):
        assert _format_duration(3723) == "1:02:03"

    def test_negative(self):
        assert _format_duration(-5) == "0:00"


# ---------------------------------------------------------------------------
# _build_podcast_feed_xml
# ---------------------------------------------------------------------------

class TestBuildPodcastFeedXml:
    def _make_episode(self, title="Ep 1", guid="guid-1", duration=300):
        return EpisodeMeta(
            title=title,
            url="https://example.com/ep.mp3",
            pub_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            guid=guid,
            duration=duration,
        )

    def test_returns_xml_string(self):
        podcast = PodcastConfig(name="Test Pod", url="https://feeds.example.com/rss", source="Podcast")
        eps = [self._make_episode()]
        ids = ["ep-001"]
        xml = _build_podcast_feed_xml(podcast, eps, ids, "https://cdn.example.com", "test-pod")
        assert xml.startswith("<?xml")
        assert "<rss" in xml
        assert "Test Pod" in xml

    def test_episode_enclosure_url(self):
        podcast = PodcastConfig(name="Test Pod", url="https://feeds.example.com/rss", source="Podcast")
        eps = [self._make_episode()]
        ids = ["ep-001"]
        xml = _build_podcast_feed_xml(podcast, eps, ids, "https://cdn.example.com", "test-pod")
        assert "https://cdn.example.com/test-pod/episodes/ep-001.mp3" in xml

    def test_empty_episodes(self):
        podcast = PodcastConfig(name="Empty Pod", url="https://feeds.example.com/rss", source="Podcast")
        xml = _build_podcast_feed_xml(podcast, [], [], "https://cdn.example.com", "empty-pod")
        assert "<item>" not in xml
        assert "Empty Pod" in xml


# ---------------------------------------------------------------------------
# process_podcast_feed
# ---------------------------------------------------------------------------

def _make_podcast(
    name="Test Podcast",
    url="https://feeds.example.com/rss",
    max_age_days=7,
    max_downloads=3,
):
    return PodcastConfig(
        name=name,
        url=url,
        enabled=True,
        max_age_days=max_age_days,
        max_downloads=max_downloads,
        source="Podcast",
    )


def _make_episode_meta(guid="guid-1", title="Episode 1"):
    return EpisodeMeta(
        title=title,
        url="https://example.com/ep.mp3",
        pub_date=datetime.now(timezone.utc),
        guid=guid,
        duration=300,
    )


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("CLOUDFRONT_BASE", "https://cdn.example.com")


class TestProcessPodcastFeedDryRun:
    def test_dry_run_returns_candidate_count(self):
        podcast = _make_podcast()
        episodes = [_make_episode_meta(f"guid-{i}", f"Episode {i}") for i in range(3)]
        feed_xml = b"<rss><channel><item/></channel></rss>"

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=episodes),
            patch("podcast_sync.S3Manager") as MockS3,
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()

            result = process_podcast_feed(podcast, provider=None, dry_run=True)

        assert result["new_episodes"] == 3
        assert result["skipped"] == 0
        assert result["failed"] == 0
        mock_s3.upload_episode.assert_not_called()
        mock_s3.upload_feed.assert_not_called()

    def test_dry_run_skips_existing(self):
        podcast = _make_podcast()
        episodes = [_make_episode_meta("guid-1", "Ep 1"), _make_episode_meta("guid-2", "Ep 2")]
        feed_xml = b"<rss/>"

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=episodes),
            patch("podcast_sync.episode_id_from_guid", side_effect=lambda g, s: g),
            patch("podcast_sync.S3Manager") as MockS3,
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = {"guid-1"}

            result = process_podcast_feed(podcast, provider=None, dry_run=True)

        assert result["skipped"] == 1
        assert result["new_episodes"] == 1


class TestProcessPodcastFeedLive:
    def test_new_episode_downloaded_and_uploaded(self, tmp_path):
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "Ep 1")
        feed_xml = b"<rss/>"
        fake_mp3 = tmp_path / "guid-1.mp3"
        fake_mp3.write_bytes(b"ID3")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.remove_ads", return_value=str(fake_mp3)),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()

            result = process_podcast_feed(podcast, provider=None, dry_run=False)

        assert result["new_episodes"] == 1
        assert result["failed"] == 0
        mock_s3.upload_episode.assert_called_once()
        mock_s3.upload_feed.assert_called_once()

    def test_failed_download_increments_failed_count(self, tmp_path):
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "Ep 1")
        feed_xml = b"<rss/>"

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", side_effect=RuntimeError("download fail")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()

            result = process_podcast_feed(podcast, provider=None, dry_run=False)

        assert result["failed"] == 1
        assert result["new_episodes"] == 0

    def test_no_episodes_returns_empty_result(self):
        podcast = _make_podcast()
        feed_xml = b"<rss/>"

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[]),
        ):
            result = process_podcast_feed(podcast, provider=None, dry_run=False)

        assert result["new_episodes"] == 0
        assert result["skipped"] == 0

    def test_max_downloads_limits_candidates(self):
        podcast = _make_podcast(max_downloads=2)
        episodes = [_make_episode_meta(f"guid-{i}", f"Ep {i}") for i in range(5)]
        feed_xml = b"<rss/>"

        downloaded = []

        def fake_download(url, ep_id, tmp):
            path = os.path.join(tmp, f"{ep_id}.mp3")
            open(path, "wb").write(b"ID3")
            downloaded.append(ep_id)
            return path

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=episodes),
            patch("podcast_sync.episode_id_from_guid", side_effect=lambda g, s: g),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", side_effect=fake_download),
            patch("podcast_sync.remove_ads", side_effect=lambda p, eid, td: p),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()

            result = process_podcast_feed(podcast, provider=None, dry_run=False)

        assert result["new_episodes"] == 2
        assert len(downloaded) == 2


class TestProcessPodcastFeedAppleUrl:
    def test_apple_url_resolved_and_written_back(self):
        apple_url = "https://podcasts.apple.com/us/podcast/test/id111222"
        rss_url = "https://feeds.example.com/real.rss"
        podcast = _make_podcast(url=apple_url)
        feed_xml = b"<rss/>"

        mock_provider = MagicMock()
        mock_provider.update_url = MagicMock()

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=True),
            patch("podcast_sync.resolve_feed_url", return_value=rss_url),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[]),
        ):
            result = process_podcast_feed(podcast, provider=mock_provider, dry_run=False)

        mock_provider.update_url.assert_called_once_with(podcast, rss_url)

    def test_apple_url_not_written_back_in_dry_run(self):
        apple_url = "https://podcasts.apple.com/us/podcast/test/id111222"
        rss_url = "https://feeds.example.com/real.rss"
        podcast = _make_podcast(url=apple_url)
        feed_xml = b"<rss/>"

        mock_provider = MagicMock()

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=True),
            patch("podcast_sync.resolve_feed_url", return_value=rss_url),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[]),
            patch("podcast_sync.S3Manager") as MockS3,
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            result = process_podcast_feed(podcast, provider=mock_provider, dry_run=True)

        mock_provider.update_url.assert_not_called()


class TestProcessPodcastFeedMissingEnv:
    def test_missing_s3_bucket_raises(self, monkeypatch):
        monkeypatch.delenv("S3_BUCKET", raising=False)
        podcast = _make_podcast()
        with pytest.raises(ValueError, match="S3_BUCKET"):
            process_podcast_feed(podcast)

    def test_missing_cloudfront_raises(self, monkeypatch):
        monkeypatch.delenv("CLOUDFRONT_BASE", raising=False)
        podcast = _make_podcast()
        with pytest.raises(ValueError, match="CLOUDFRONT_BASE"):
            process_podcast_feed(podcast)
