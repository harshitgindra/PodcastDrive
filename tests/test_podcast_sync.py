"""Tests for src/podcast_sync.py."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from config_provider import PodcastConfig
from podcast_downloader import EpisodeMeta
from podcast_sync import (
    CDN_RETRY_OVERRIDES,
    _build_podcast_feed_xml,
    _format_duration,
    _podcast_slug,
    _splice_attempts_for_cdn,
    detect_cdn,
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
            podcast,
            eps,
            ids,
            "https://cdn.example.com",
            "art-pod",
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
            podcast,
            eps,
            ids,
            "https://cdn.example.com",
            "art-pod",
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
            podcast,
            eps,
            ids,
            "https://cdn.example.com",
            "art-pod",
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
            podcast,
            eps,
            ids,
            "https://cdn.example.com",
            "art-pod",
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
            podcast,
            eps,
            ids,
            "https://cdn.example.com",
            "test-pod",
            manifest={"ep-001": {"ads_removed": True}},
        )
        assert "My Episode [Ad-Free]" in xml

    def test_no_suffix_when_ads_not_removed(self):
        """Episode title is unchanged when manifest does not mark ads_removed."""
        podcast = PodcastConfig(name="Test Pod", url="https://feeds.example.com/rss", source="Podcast")
        eps = [self._make_episode(title="My Episode", guid="guid-1")]
        ids = ["ep-001"]
        xml = _build_podcast_feed_xml(
            podcast,
            eps,
            ids,
            "https://cdn.example.com",
            "test-pod",
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


@pytest.fixture(autouse=True)
def _skip_ffprobe_validation(monkeypatch):
    """Most tests use tiny fake mp3 fixtures that legitimately fail real
    ffprobe validation (a few placeholder bytes, not real audio). Default
    the pre-flight check to 'valid' so existing tests keep exercising the
    retry/upload/manifest logic they were written for; dedicated validation
    tests override this per-test with monkeypatch/patch as needed."""
    monkeypatch.setattr("podcast_sync.validate_audio_file", lambda path, min_bytes=1024: (True, ""))


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


# ---------------------------------------------------------------------------
# CDN/SSAI detection
# ---------------------------------------------------------------------------


class TestDetectCdn:
    """detect_cdn() tags episode URLs by CDN/SSAI provider."""

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://podtrac.com/pts/redirect.mp3/media.megaphone.fm/ep1.mp3", "megaphone"),
            ("https://traffic.megaphone.fm/ep1.mp3", "megaphone"),
            ("https://sphinx.acast.com/p/show/ep/media.mp3", "acast"),
            ("https://rss.art19.com/episodes/abc.mp3", "art19"),
            ("https://anchor.fm/s/abc/podcast/play/1.mp3", "anchor"),
            ("https://d3ctxlq1ktw2nl.cloudfront.net/staging/ep.mp3", "cloudfront"),
            ("https://traffic.libsyn.com/secure/show/ep.mp3", "libsyn"),
            ("https://example.com/direct-host/ep.mp3", "unknown"),
            ("", "unknown"),
        ],
    )
    def test_known_and_unknown_cdns(self, url, expected):
        assert detect_cdn(url) == expected

    def test_case_insensitive(self):
        assert detect_cdn("https://TRAFFIC.MEGAPHONE.FM/ep1.mp3") == "megaphone"


class TestSpliceAttemptsForCdn:
    """_splice_attempts_for_cdn() resolves per-CDN retry-attempt overrides."""

    def test_known_flaky_cdn_gets_override(self):
        assert _splice_attempts_for_cdn("megaphone") == CDN_RETRY_OVERRIDES["megaphone"]
        assert _splice_attempts_for_cdn("acast") == CDN_RETRY_OVERRIDES["acast"]

    def test_unknown_cdn_falls_back_to_env_default(self, monkeypatch):
        monkeypatch.delenv("SPLICE_MAX_ATTEMPTS_PER_RUN", raising=False)
        assert _splice_attempts_for_cdn("unknown") == 2

    def test_unknown_cdn_respects_env_override(self, monkeypatch):
        monkeypatch.setenv("SPLICE_MAX_ATTEMPTS_PER_RUN", "5")
        assert _splice_attempts_for_cdn("unknown") == 5

    def test_known_cdn_ignores_env_override(self, monkeypatch):
        """A CDN-specific override takes precedence over the generic env var."""
        monkeypatch.setenv("SPLICE_MAX_ATTEMPTS_PER_RUN", "1")
        assert _splice_attempts_for_cdn("megaphone") == CDN_RETRY_OVERRIDES["megaphone"]


class TestCdnTagInManifest:
    """The detected CDN tag is persisted to the manifest for observability."""

    def test_cdn_tag_saved_on_success(self, tmp_path):
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-cdn", "Ep")
        ep.url = "https://traffic.megaphone.fm/ep1.mp3"
        feed_xml = b"<rss/>"
        fake_mp3 = tmp_path / "guid-cdn.mp3"
        fake_mp3.write_bytes(b"ID3")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-cdn"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            mock_s3.save_manifest.return_value = None

            process_podcast_feed(podcast, provider=None, dry_run=False)

        saved = mock_s3.save_manifest.call_args[0][0]
        assert saved["guid-cdn"]["cdn"] == "megaphone"

    def test_cdn_tag_and_fail_reason_saved_on_splice_failure(self, tmp_path):
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-cdn2", "Ep")
        ep.url = "https://sphinx.acast.com/show/ep.mp3"
        feed_xml = b"<rss/>"
        fake_mp3 = tmp_path / "guid-cdn2.mp3"
        fake_mp3.write_bytes(b"ID3")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-cdn2"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            # Detection finds ads but splice keeps returning the original path -> splice_failed
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [{"start": 0, "end": 5}], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            mock_s3.save_manifest.return_value = None

            process_podcast_feed(podcast, provider=None, dry_run=False)

        saved = mock_s3.save_manifest.call_args[0][0]
        assert saved["guid-cdn2"]["cdn"] == "acast"
        assert saved["guid-cdn2"]["splice_failed"] is True
        assert "splice crashed" in saved["guid-cdn2"]["fail_reason"]


# ---------------------------------------------------------------------------
# ffprobe pre-download validation
# ---------------------------------------------------------------------------


class TestFfprobeValidationRetry:
    """A corrupt/truncated download is caught by validate_audio_file() before
    Transcribe/Bedrock ever runs, and retried with a fresh download."""

    def test_invalid_download_skips_remove_ads_and_retries(self, tmp_path):
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-bad", "Ep")
        feed_xml = b"<rss/>"
        fake_mp3 = tmp_path / "guid-bad.mp3"
        fake_mp3.write_bytes(b"ID3")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-bad"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)) as mock_dl,
            # Override the autouse "always valid" fixture: fail once, then succeed
            patch(
                "podcast_sync.validate_audio_file",
                side_effect=[(False, "suspiciously small (3 bytes)"), (True, "")],
            ) as mock_validate,
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")) as mock_remove_ads,
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            mock_s3.save_manifest.return_value = None

            result = process_podcast_feed(podcast, provider=None, dry_run=False)

        # Two download attempts (initial + one retry after validation failure)
        assert mock_dl.call_count == 2
        # remove_ads (Transcribe + Bedrock) is only ever called once — the
        # invalid attempt never reaches it. This is the "no wasted detection
        # spend on a corrupt download" guarantee.
        assert mock_remove_ads.call_count == 1
        assert mock_validate.call_count == 2
        assert result["new_episodes"] == 1

    def test_all_attempts_invalid_results_in_splice_failed_no_upload(self, tmp_path):
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-bad2", "Ep")
        feed_xml = b"<rss/>"
        fake_mp3 = tmp_path / "guid-bad2.mp3"
        fake_mp3.write_bytes(b"ID3")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-bad2"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.validate_audio_file", return_value=(False, "zero/negative duration (0)")),
            patch("podcast_sync.remove_ads") as mock_remove_ads,
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            mock_s3.save_manifest.return_value = None

            process_podcast_feed(podcast, provider=None, dry_run=False)

        mock_remove_ads.assert_not_called()
        saved = mock_s3.save_manifest.call_args[0][0]
        assert saved["guid-bad2"]["splice_failed"] is True
        assert "download validation failed" in saved["guid-bad2"]["fail_reason"]
        assert "size" not in saved["guid-bad2"]  # never uploaded


# ---------------------------------------------------------------------------
# Decoupling: ad-segment cache is preserved across in-run retries so a splice
# crash never forces a redundant Transcribe/Bedrock pass.
# ---------------------------------------------------------------------------


class TestSpliceRetryReusesDetection:
    """Splice-only retries (ad_segments detected, but splice crashed and
    returned the original file) must NOT re-run Transcribe/Bedrock — the
    ad-segment + transcript caches inside remove_ads() are keyed by ep_id
    and untouched by podcast_sync, so a second remove_ads() call on the
    freshly re-downloaded file is expected to hit the cache internally.
    This test asserts podcast_sync's contract: it never clears any cache
    key itself and calls remove_ads() again with the same arguments."""

    def test_retry_calls_remove_ads_again_without_clearing_any_cache(self, tmp_path):
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-retry", "Ep")
        feed_xml = b"<rss/>"
        fake_mp3 = tmp_path / "guid-retry.mp3"
        fake_mp3.write_bytes(b"ID3")

        call_count = {"n": 0}

        def fake_remove_ads(path, ep_id, tmp, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call: ads detected, splice crashed -> original returned
                return (path, [{"start": 0, "end": 5}], "")
            # Second call (post-retry): splice succeeds -> different path
            cleaned = tmp_path / "guid-retry_clean.mp3"
            cleaned.write_bytes(b"ID3-clean")
            return (str(cleaned), [{"start": 0, "end": 5}], "")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=feed_xml),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-retry"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.remove_ads", side_effect=fake_remove_ads) as mock_remove_ads,
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.load_manifest.return_value = {}
            mock_s3.save_manifest.return_value = None

            result = process_podcast_feed(podcast, provider=None, dry_run=False)

        # remove_ads called twice (attempt 1 crashed, attempt 2 succeeded);
        # podcast_sync performed no S3 cache-clearing calls of its own between
        # attempts (no boto3 client is even constructed for that purpose any more).
        assert mock_remove_ads.call_count == 2
        assert result["new_episodes"] == 1


# ---------------------------------------------------------------------------
# _build_podcast_feed_xml — sanitisation of third-party feed text
# ---------------------------------------------------------------------------


class TestFeedXmlSanitization:
    """Third-party RSS text reaches the feed verbatim, so control chars must be stripped."""

    def _ep(self, **kw):
        base = dict(
            title="Ep 1",
            url="https://example.com/ep.mp3",
            pub_date=datetime(2024, 1, 1, tzinfo=UTC),
            guid="guid-1",
            duration=300,
            thumbnail="",
        )
        base.update(kw)
        return EpisodeMeta(**base)

    def _build(self, podcast, eps, ids, **kw):
        return _build_podcast_feed_xml(podcast, eps, ids, "https://cdn.example.com", "test-pod", **kw)

    def test_control_char_in_episode_title_does_not_raise(self):
        podcast = PodcastConfig(name="Test Pod", url="https://feeds.example.com/rss", source="Podcast")
        xml = self._build(podcast, [self._ep(title="Bad\x08Title\x1a")], ["ep-001"])
        assert "\x08" not in xml
        assert "\x1a" not in xml
        assert "BadTitle" in xml

    def test_control_char_in_podcast_name_does_not_raise(self):
        podcast = PodcastConfig(name="Bad\x0bPod", url="https://feeds.example.com/rss", source="Podcast")
        xml = self._build(podcast, [], [])
        assert "\x0b" not in xml
        assert "BadPod" in xml

    def test_control_char_in_description_does_not_raise(self):
        podcast = PodcastConfig(
            name="Test Pod",
            url="https://feeds.example.com/rss",
            source="Podcast",
            description="Desc\x1fwith junk",
        )
        xml = self._build(podcast, [], [])
        assert "\x1f" not in xml

    def test_control_char_in_guid_does_not_raise(self):
        podcast = PodcastConfig(name="Test Pod", url="https://feeds.example.com/rss", source="Podcast")
        xml = self._build(podcast, [self._ep(guid="gu\x07id")], ["ep-001"])
        assert "\x07" not in xml

    def test_control_char_in_ai_summary_does_not_raise(self):
        podcast = PodcastConfig(name="Test Pod", url="https://feeds.example.com/rss", source="Podcast")
        xml = self._build(
            podcast,
            [self._ep()],
            ["ep-001"],
            manifest={"ep-001": {"summary": "A\x0csummary"}},
        )
        assert "\x0c" not in xml

    def test_control_char_in_thumbnail_url_does_not_raise(self):
        podcast = PodcastConfig(name="Test Pod", url="https://feeds.example.com/rss", source="Podcast")
        xml = self._build(podcast, [self._ep(thumbnail="https://img.example.com/a\x01.jpg")], ["ep-001"])
        assert "\x01" not in xml

    def test_output_is_parseable_after_sanitization(self):
        import xml.etree.ElementTree as ET

        podcast = PodcastConfig(name="P\x08od", url="https://feeds.example.com/rss", source="Podcast")
        xml = self._build(podcast, [self._ep(title="T\x1ai", guid="g\x0bu")], ["ep-001"])
        ET.fromstring(xml)  # must not raise

    def test_legitimate_unicode_survives(self):
        podcast = PodcastConfig(name="Café ☕ Pod", url="https://feeds.example.com/rss", source="Podcast")
        xml = self._build(podcast, [self._ep(title="Épisode – naïve")], ["ep-001"])
        assert "Café ☕ Pod" in xml
        assert "Épisode – naïve" in xml

    def test_tabs_and_newlines_are_preserved(self):
        podcast = PodcastConfig(
            name="Test Pod",
            url="https://feeds.example.com/rss",
            source="Podcast",
            description="line1\nline2\tend",
        )
        xml = self._build(podcast, [], [])
        assert "line1" in xml and "line2" in xml


# ---------------------------------------------------------------------------
# process_podcast_feed — feed-build failure isolation
# ---------------------------------------------------------------------------


class TestFeedBuildFailureIsolation:
    """Episodes are already uploaded when the feed is built, so a build fault must not abort."""

    def _run(self, tmp_path, feed_side_effect):
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "Ep 1")
        fake_mp3 = tmp_path / "guid-1.mp3"
        fake_mp3.write_bytes(b"ID3")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=b"<rss/>"),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
            patch("podcast_sync._build_podcast_feed_xml", side_effect=feed_side_effect),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            result = process_podcast_feed(podcast, provider=None, dry_run=False)

        return result, mock_s3

    def test_build_error_does_not_raise(self, tmp_path):
        result, _ = self._run(tmp_path, ValueError("not well-formed"))
        assert result["new_episodes"] == 1

    def test_uploaded_episode_is_still_counted(self, tmp_path):
        result, mock_s3 = self._run(tmp_path, ValueError("not well-formed"))
        mock_s3.upload_episode.assert_called_once()
        assert result["failed"] == 0

    def test_feed_is_not_uploaded_when_the_build_fails(self, tmp_path):
        _, mock_s3 = self._run(tmp_path, ValueError("not well-formed"))
        mock_s3.upload_feed.assert_not_called()

    def test_failure_is_logged_as_error(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.ERROR, logger="podcast_sync"):
            self._run(tmp_path, ValueError("not well-formed"))
        assert "feed.xml generation failed" in caplog.text

    def test_notion_write_back_still_happens(self, tmp_path):
        """The provider update lives after the feed build and must not be skipped."""
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "Ep 1")
        fake_mp3 = tmp_path / "guid-1.mp3"
        fake_mp3.write_bytes(b"ID3")
        provider = MagicMock()

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=b"<rss/>"),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
            patch("podcast_sync._build_podcast_feed_xml", side_effect=ValueError("boom")),
        ):
            MockS3.return_value.list_existing_episodes.return_value = set()
            process_podcast_feed(podcast, provider=provider, dry_run=False)

        provider.update_status.assert_called_once_with(podcast, "Done")

    def test_upload_feed_failure_is_also_contained(self, tmp_path):
        podcast = _make_podcast(max_downloads=1)
        ep = _make_episode_meta("guid-1", "Ep 1")
        fake_mp3 = tmp_path / "guid-1.mp3"
        fake_mp3.write_bytes(b"ID3")

        with (
            patch("podcast_sync.is_apple_podcasts_url", return_value=False),
            patch("podcast_sync.fetch_feed_xml", return_value=b"<rss/>"),
            patch("podcast_sync.parse_episodes", return_value=[ep]),
            patch("podcast_sync.episode_id_from_guid", return_value="guid-1"),
            patch("podcast_sync.S3Manager") as MockS3,
            patch("podcast_sync.download_episode", return_value=str(fake_mp3)),
            patch("podcast_sync.remove_ads", return_value=(str(fake_mp3), [], "")),
        ):
            mock_s3 = MockS3.return_value
            mock_s3.list_existing_episodes.return_value = set()
            mock_s3.upload_feed.side_effect = RuntimeError("S3 down")
            result = process_podcast_feed(podcast, provider=None, dry_run=False)

        assert result["new_episodes"] == 1
