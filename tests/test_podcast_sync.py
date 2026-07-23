"""Tests for src/podcast_sync.py."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

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
    def _make_episode(self, title="Ep 1", guid="guid-1", duration=300, thumbnail=""):
        return EpisodeMeta(
            title=title,
            url="https://example.com/ep.mp3",
            pub_date=datetime(2024, 1, 1, tzinfo=UTC),
            guid=guid,
            duration=duration,
            thumbnail=thumbnail,
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

    def test_channel_thumbnail_in_itunes_image(self):
        podcast = PodcastConfig(name="Art Pod", url="https://feeds.example.com/rss", source="Podcast")
        eps = [self._make_episode()]
        ids = ["ep-001"]
        xml = _build_podcast_feed_xml(
            podcast, eps, ids, "https://cdn.example.com", "art-pod",
            channel_thumbnail="https://example.com/channel-art.jpg",
        )
        assert "https://example.com/channel-art.jpg" in xml
        # Should appear in both RSS <image> block and itunes:image
        assert xml.count("https://example.com/channel-art.jpg") >= 2

    def test_channel_thumbnail_in_rss_image_block(self):
        podcast = PodcastConfig(name="Art Pod", url="https://feeds.example.com/rss", source="Podcast")
        eps = [self._make_episode()]
        ids = ["ep-001"]
        xml = _build_podcast_feed_xml(
            podcast, eps, ids, "https://cdn.example.com", "art-pod",
            channel_thumbnail="https://example.com/channel-art.jpg",
        )
        assert "<image>" in xml
        assert "<url>https://example.com/channel-art.jpg</url>" in xml

    def test_no_image_when_no_thumbnail(self):
        podcast = PodcastConfig(name="No Art Pod", url="https://feeds.example.com/rss", source="Podcast")
        xml = _build_podcast_feed_xml(podcast, [], [], "https://cdn.example.com", "no-art-pod")
        assert "<image>" not in xml
        assert "itunes:image" not in xml

    def test_episode_thumbnail_in_item(self):
        podcast = PodcastConfig(name="Art Pod", url="https://feeds.example.com/rss", source="Podcast")
        eps = [self._make_episode(thumbnail="https://example.com/ep-art.jpg")]
        ids = ["ep-001"]
        xml = _build_podcast_feed_xml(podcast, eps, ids, "https://cdn.example.com", "art-pod")
        assert "https://example.com/ep-art.jpg" in xml

    def test_episode_falls_back_to_channel_thumbnail(self):
        """Episode with no thumbnail should use the channel thumbnail in its item."""
        podcast = PodcastConfig(name="Art Pod", url="https://feeds.example.com/rss", source="Podcast")
        # Episode has no own thumbnail
        eps = [self._make_episode(thumbnail="")]
        ids = ["ep-001"]
        xml = _build_podcast_feed_xml(
            podcast, eps, ids, "https://cdn.example.com", "art-pod",
            channel_thumbnail="https://example.com/channel-art.jpg",
        )
        # channel thumbnail should appear in the item (episode fallback)
        assert "https://example.com/channel-art.jpg" in xml

    def test_episode_own_thumbnail_not_overridden_by_channel(self):
        """Episode with its own thumbnail should use it, not the channel art."""
        podcast = PodcastConfig(name="Art Pod", url="https://feeds.example.com/rss", source="Podcast")
        eps = [self._make_episode(thumbnail="https://example.com/ep-specific.jpg")]
        ids = ["ep-001"]
        xml = _build_podcast_feed_xml(
            podcast, eps, ids, "https://cdn.example.com", "art-pod",
            channel_thumbnail="https://example.com/channel-art.jpg",
        )
        assert "https://example.com/ep-specific.jpg" in xml

    def test_ads_removed_suffix_appended_to_title(self, monkeypatch):
        """Episode title gets ad-removed suffix when manifest marks ads_removed=True."""
        monkeypatch.setenv("EPISODE_AD_REMOVED_SUFFIX", " [Ad-Free]")
        podcast = PodcastConfig(name="Test Pod", url="https://feeds.example.com/rss", source="Podcast")
        eps = [self._make_episode(title="My Episode", guid="guid-1")]
        ids = ["ep-001"]
        xml = _build_podcast_feed_xml(
            podcast, eps, ids, "https://cdn.example.com", "test-pod",
            manifest={"ep-001": {"ads_removed": True}},
        )
        assert "My Episode [Ad-Free]" in xml

    def test_no_suffix_when_ads_not_removed(self):
        """Episode title is unchanged when manifest does not mark ads_removed."""
        podcast = PodcastConfig(name="Test Pod", url="https://feeds.example.com/rss", source="Podcast")
        eps = [self._make_episode(title="My Episode", guid="guid-1")]
        ids = ["ep-001"]
        xml = _build_podcast_feed_xml(
            podcast, eps, ids, "https://cdn.example.com", "test-pod",
            manifest={"ep-001": {"ads_removed": False}},
        )
        assert "My Episode ✂️" not in xml
        assert "My Episode" in xml


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
        pub_date=datetime.now(UTC),
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
            patch("podcast_sync.episode_id_from_guid", side_effect=lambda g: g),
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
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
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
            patch("podcast_sync.episode_id_from_guid", side_effect=lambda g: g),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", side_effect=fake_download),
            patch("podcast_sync.remove_ads", side_effect=lambda p, eid, td, **kw: (p, [], "")),
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
            process_podcast_feed(podcast, provider=mock_provider, dry_run=False)

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
            process_podcast_feed(podcast, provider=mock_provider, dry_run=True)

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


class TestProcessPodcastFeedEmptyUrl:
    """Tests for the iTunes-by-name discovery path when podcast.url is empty."""

    def test_empty_url_triggers_name_search(self):
        podcast = _make_podcast(url="")
        discovered_url = "https://feeds.example.com/discovered.rss"
        feed_xml = b"<rss/>"

        with (
            patch("podcast_sync.search_feed_url_by_name", return_value=discovered_url) as mock_search,
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[]),
        ):
            result = process_podcast_feed(podcast, provider=None, dry_run=False)

        mock_search.assert_called_once_with(podcast.name)
        assert result["new_episodes"] == 0

    def test_empty_url_writes_back_to_notion(self):
        podcast = _make_podcast(url="")
        discovered_url = "https://feeds.example.com/discovered.rss"
        feed_xml = b"<rss/>"
        mock_provider = MagicMock()

        with (
            patch("podcast_sync.search_feed_url_by_name", return_value=discovered_url),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[]),
        ):
            process_podcast_feed(podcast, provider=mock_provider, dry_run=False)

        mock_provider.update_url.assert_called_once_with(podcast, discovered_url)

    def test_empty_url_no_dry_run_write_back(self):
        """In dry_run mode, url write-back is skipped even when URL is discovered."""
        podcast = _make_podcast(url="")
        discovered_url = "https://feeds.example.com/discovered.rss"
        feed_xml = b"<rss/>"
        mock_provider = MagicMock()

        with (
            patch("podcast_sync.search_feed_url_by_name", return_value=discovered_url),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[]),
            patch("podcast_sync.S3Manager") as MockS3,
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            process_podcast_feed(podcast, provider=mock_provider, dry_run=True)

        mock_provider.update_url.assert_not_called()

    def test_empty_url_search_fails_returns_empty_result(self):
        podcast = _make_podcast(url="")

        with patch("podcast_sync.search_feed_url_by_name", return_value=""):
            result = process_podcast_feed(podcast, provider=None, dry_run=False)

        assert result["new_episodes"] == 0
        assert result["failed"] == 0


class TestProcessPodcastFeedEnvDefaults:
    """Covers env-var fallback paths for max_age_days and max_episodes."""

    def test_max_age_days_read_from_env_when_podcast_has_none(self, monkeypatch, tmp_path):
        """Covers lines 221-222: podcast.max_age_days is None → read MAX_AGE_DAYS env."""
        monkeypatch.setenv("MAX_AGE_DAYS", "30")
        podcast = _make_podcast(max_age_days=None, max_downloads=1)
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
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            result = process_podcast_feed(podcast, dry_run=False)

        assert result["new_episodes"] == 1

    def test_max_episodes_read_from_env_when_podcast_has_none(self, monkeypatch, tmp_path):
        """Covers lines 226-227: podcast.max_downloads is None → read PODCAST_MAX_EPISODES env."""
        monkeypatch.setenv("PODCAST_MAX_EPISODES", "2")
        podcast = _make_podcast(max_downloads=None)
        episodes = [_make_episode_meta(f"guid-{i}", f"Ep {i}") for i in range(5)]
        feed_xml = b"<rss/>"

        def fake_download(url, ep_id, tmp):
            path = os.path.join(tmp, f"{ep_id}.mp3")
            open(path, "wb").write(b"ID3")
            return path

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=episodes),
            patch("podcast_sync.episode_id_from_guid", side_effect=lambda g: g),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", side_effect=fake_download),
            patch("podcast_sync.remove_ads", side_effect=lambda p, eid, td, **kw: (p, [], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            result = process_podcast_feed(podcast, dry_run=False)

        assert result["new_episodes"] == 2

    def test_max_age_days_none_uses_all_feed_episodes(self):
        """Covers line 288: max_age_days=None → episodes = all_feed_episodes (no filter)."""
        podcast = _make_podcast(max_age_days=None, max_downloads=10)
        episodes = [_make_episode_meta(f"guid-{i}", f"Ep {i}") for i in range(3)]
        feed_xml = b"<rss/>"

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=episodes),
            patch("podcast_sync.S3Manager") as MockS3,
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            result = process_podcast_feed(podcast, dry_run=True)

        assert result["new_episodes"] == 3


class TestProcessPodcastFeedEdgeCases:
    """Covers edge-case execution paths inside process_podcast_feed."""

    def test_ad_removal_produces_different_file_original_deleted(self, tmp_path):
        """Covers line 354: cleaned_path != original_path → original removed."""
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "Ep 1")
        feed_xml = b"<rss/>"

        original = tmp_path / "guid-1.mp3"
        cleaned = tmp_path / "guid-1_clean.mp3"
        original.write_bytes(b"ID3_original")
        cleaned.write_bytes(b"ID3_clean")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(original)),
            patch("podcast_sync.remove_ads", return_value=(str(cleaned), [], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            result = process_podcast_feed(podcast, dry_run=False)

        # Original should have been deleted when ad removal produced a separate file
        assert not original.exists()
        assert result["new_episodes"] == 1

    def test_getsize_oserror_falls_back_to_zero(self, tmp_path):
        """Covers lines 363-364: OSError on getsize after upload → file_size=0."""
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
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
            patch("podcast_sync.os.path.getsize", side_effect=OSError("no file")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            result = process_podcast_feed(podcast, dry_run=False)

        assert result["new_episodes"] == 1
        # Manifest should record size=0 when getsize fails
        saved = mock_s3.save_manifest.call_args[0][0]
        assert saved["guid-1"]["size"] == 0

    def test_partial_file_oserror_during_cleanup_is_swallowed(self, tmp_path):
        """Covers lines 386-389: OSError when removing partial file is silently ignored."""
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "Ep 1")
        feed_xml = b"<rss/>"

        # Make the partial file exist so the cleanup branch is reached
        partial = tmp_path / "guid-1.mp3"
        partial.write_bytes(b"partial")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(partial)),
            patch("podcast_sync.remove_ads", side_effect=RuntimeError("ad removal failed")),
            patch("podcast_sync.os.remove", side_effect=OSError("permission denied")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            # Should not raise despite OSError in cleanup
            result = process_podcast_feed(podcast, dry_run=False)

        assert result["failed"] == 1

    def test_backfill_get_object_size_raises_uses_zero(self, tmp_path):
        """Covers lines 452-453: get_object_size raises during backfill → ep_sizes[eid]=0."""
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "Ep 1")
        feed_xml = b"<rss/>"

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = {"guid-1"}
            mock_s3.load_manifest.return_value = {}
            # Make get_object_size raise to exercise the except branch
            mock_s3.get_object_size.side_effect = RuntimeError("S3 error")

            result = process_podcast_feed(podcast, dry_run=False)

        # feed should still be generated with size=0 for the episode
        mock_s3.upload_feed.assert_called_once()
        assert result["skipped"] == 1


class TestProcessPodcastFeedManifest:
    """Verify manifest is loaded, updated, and saved during processing."""

    def test_manifest_loaded_and_saved_on_new_episode(self, tmp_path):
        """load_manifest is called once; save_manifest is called after upload."""
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
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}

            process_podcast_feed(podcast, provider=None, dry_run=False)

        mock_s3.load_manifest.assert_called_once()
        mock_s3.save_manifest.assert_called()

    def test_manifest_not_saved_when_no_new_episodes(self):
        """save_manifest is NOT called when all episodes already exist in S3."""
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "Ep 1")
        feed_xml = b"<rss/>"

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
        ):
            mock_s3 = MockS3.return_value
            # Episode already in S3 → skipped, no download
            mock_s3.list_existing_episodes.return_value = {"guid-1"}
            mock_s3.load_manifest.return_value = {"guid-1": {"size": 100}}

            process_podcast_feed(podcast, provider=None, dry_run=False)

        mock_s3.save_manifest.assert_not_called()

    def test_manifest_size_used_instead_of_head_object(self, tmp_path):
        """When manifest has size for an episode, head_object is not called for it."""
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "Ep 1")
        feed_xml = b"<rss/>"
        fake_mp3 = tmp_path / "guid-1.mp3"
        fake_mp3.write_bytes(b"ID3" * 1000)

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            # Manifest already has size for the episode after upload
            mock_s3.load_manifest.return_value = {}
            mock_s3.save_manifest.return_value = None

            process_podcast_feed(podcast, provider=None, dry_run=False)

        # get_object_size should NOT be called for the newly uploaded episode
        # (size was captured locally after upload)
        mock_s3.get_object_size.assert_not_called()

    def test_backfill_stores_title_and_metadata_for_existing_episodes(self, tmp_path):
        """Episodes in S3 but missing from manifest get title/guid/pub_date/duration backfilled."""
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "My Great Episode")
        feed_xml = b"<rss/>"

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
        ):
            mock_s3 = MockS3.return_value
            # Episode already in S3, so it's skipped (not downloaded again)
            mock_s3.list_existing_episodes.return_value = {"guid-1"}
            # Manifest is empty — backfill should populate it
            mock_s3.load_manifest.return_value = {}
            mock_s3.get_object_size.return_value = 5_000_000

            process_podcast_feed(podcast, provider=None, dry_run=False)

        # save_manifest called with the backfilled entry
        mock_s3.save_manifest.assert_called()
        saved = mock_s3.save_manifest.call_args[0][0]
        assert "guid-1" in saved
        assert saved["guid-1"]["title"] == "My Great Episode"
        assert saved["guid-1"]["guid"] == "guid-1"
        assert "pub_date" in saved["guid-1"]
        assert saved["guid-1"]["duration"] == 300


# ---------------------------------------------------------------------------
# process_podcast_feed — splice retry cap
# ---------------------------------------------------------------------------

class TestSpliceRetryCount:
    """Verify the MAX_SPLICE_RETRIES cap prevents infinite reprocessing."""

    def _make_manifest_with_splice_failure(self, ep_id: str, count: int) -> dict:
        return {ep_id: {"splice_failed": True, "splice_failed_count": count, "size": 1000}}

    def test_episode_retried_when_below_cap(self, tmp_path, monkeypatch):
        """Episode with splice_failed_count < MAX_SPLICE_RETRIES is re-queued."""
        monkeypatch.setenv("MAX_SPLICE_RETRIES", "3")
        podcast = _make_podcast(max_downloads=5)
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
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = {"guid-1"}
            mock_s3.load_manifest.return_value = self._make_manifest_with_splice_failure("guid-1", count=2)
            result = process_podcast_feed(podcast, dry_run=False)

        # Episode was re-queued (not skipped) — one new episode processed
        assert result["new_episodes"] == 1

    def test_episode_not_retried_when_at_cap(self, monkeypatch):
        """Episode with splice_failed_count >= MAX_SPLICE_RETRIES is skipped."""
        monkeypatch.setenv("MAX_SPLICE_RETRIES", "3")
        podcast = _make_podcast(max_downloads=5)
        ep = _make_episode_meta("guid-1", "Ep 1")
        feed_xml = b"<rss/>"

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode") as mock_dl,
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = {"guid-1"}
            mock_s3.load_manifest.return_value = self._make_manifest_with_splice_failure("guid-1", count=3)
            result = process_podcast_feed(podcast, dry_run=True)

        # Episode was NOT re-queued — download never called
        mock_dl.assert_not_called()
        assert result["new_episodes"] == 0

    def test_splice_failed_count_incremented_in_manifest(self, tmp_path, monkeypatch):
        """Each splice failure increments splice_failed_count in the manifest."""
        monkeypatch.setenv("MAX_SPLICE_RETRIES", "3")
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "Ep 1")
        feed_xml = b"<rss/>"
        original = tmp_path / "guid-1.mp3"
        original.write_bytes(b"ID3")

        # Simulate a splice failure: remove_ads returns the original file despite finding ads
        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(original)),
            # Both attempts return ads found but original path (splice failed)
            patch("podcast_sync.remove_ads", return_value=(str(original), [{"start": 0, "end": 5}], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {"guid-1": {"splice_failed_count": 1}}

            process_podcast_feed(podcast, dry_run=False)

        saved = mock_s3.save_manifest.call_args[0][0]
        assert saved["guid-1"]["splice_failed"] is True
        assert saved["guid-1"]["splice_failed_count"] == 2  # 1 previous + 1 this run

    def test_splice_failed_count_not_incremented_on_success(self, tmp_path, monkeypatch):
        """A successful episode resets splice_failed to False and does not increment count."""
        monkeypatch.setenv("MAX_SPLICE_RETRIES", "3")
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
            # Successful ad removal — no ads found, original returned
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {"guid-1": {"splice_failed_count": 2}}

            process_podcast_feed(podcast, dry_run=False)

        saved = mock_s3.save_manifest.call_args[0][0]
        assert saved["guid-1"]["splice_failed"] is False
        assert saved["guid-1"]["splice_failed_count"] == 2  # unchanged — no new failure


# ---------------------------------------------------------------------------
# process_podcast_feed — episode_title/duration_secs forwarded to remove_ads
# ---------------------------------------------------------------------------

class TestProcessPodcastFeedSummaryIntegration:
    """Verify remove_ads is called with episode metadata and summary is persisted."""

    def test_remove_ads_called_with_episode_title_and_duration(self, tmp_path):
        """remove_ads receives episode_title and duration_secs from EpisodeMeta."""
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "My Test Episode")
        feed_xml = b"<rss/>"
        fake_mp3 = tmp_path / "guid-1.mp3"
        fake_mp3.write_bytes(b"ID3")

        captured_kwargs: dict = {}

        def fake_remove_ads(path, ep_id, tmp, **kwargs):
            captured_kwargs.update(kwargs)
            return (path, [], "")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.remove_ads", side_effect=fake_remove_ads),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            mock_s3.save_manifest.return_value = None

            process_podcast_feed(podcast, provider=None, dry_run=False)

        assert captured_kwargs.get("episode_title") == "My Test Episode"
        assert captured_kwargs.get("duration_secs") == 300  # from _make_episode_meta

    def test_summary_saved_to_manifest_when_returned(self, tmp_path):
        """When remove_ads returns a non-empty summary it is stored in the manifest."""
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-2", "Summarised Episode")
        feed_xml = b"<rss/>"
        fake_mp3 = tmp_path / "guid-2.mp3"
        fake_mp3.write_bytes(b"ID3")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-2"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "AI-generated summary")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            mock_s3.save_manifest.return_value = None

            process_podcast_feed(podcast, provider=None, dry_run=False)

        # save_manifest called and manifest contains summary
        mock_s3.save_manifest.assert_called()
        saved = mock_s3.save_manifest.call_args[0][0]
        assert saved["guid-2"]["summary"] == "AI-generated summary"

    def test_empty_summary_not_saved_to_manifest(self, tmp_path):
        """When remove_ads returns empty summary, no 'summary' key is added to manifest."""
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-3", "Unsummarised Episode")
        feed_xml = b"<rss/>"
        fake_mp3 = tmp_path / "guid-3.mp3"
        fake_mp3.write_bytes(b"ID3")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-3"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            mock_s3.save_manifest.return_value = None

            process_podcast_feed(podcast, provider=None, dry_run=False)

        mock_s3.save_manifest.assert_called()
        saved = mock_s3.save_manifest.call_args[0][0]
        assert "summary" not in saved.get("guid-3", {})

    @pytest.mark.parametrize("error_code", ["TRANSCRIBE_FAILED", "DETECT_FAILED", "SPLICE_FAILED"])
    def test_error_code_not_saved_as_summary(self, error_code, tmp_path):
        """remove_ads error codes must never be stored as episode summaries in the manifest."""
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-err", "Failed Episode")
        feed_xml = b"<rss/>"
        fake_mp3 = tmp_path / "guid-err.mp3"
        fake_mp3.write_bytes(b"ID3")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-err"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], error_code)),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            mock_s3.save_manifest.return_value = None

            process_podcast_feed(podcast, provider=None, dry_run=False)

        saved = mock_s3.save_manifest.call_args[0][0]
        assert "summary" not in saved.get("guid-err", {}), (
            f"Error code {error_code!r} should not be stored as a summary"
        )
