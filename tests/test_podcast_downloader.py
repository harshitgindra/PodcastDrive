"""Tests for src/podcast_downloader.py."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from podcast_downloader import (
    EpisodeMeta,
    _parse_duration,
    download_episode,
    episode_id_from_guid,
    fetch_feed_xml,
    is_apple_podcasts_url,
    parse_episodes,
    resolve_feed_url,
)


# ---------------------------------------------------------------------------
# is_apple_podcasts_url
# ---------------------------------------------------------------------------

class TestIsApplePodcastsUrl:
    def test_apple_podcasts_url(self):
        assert is_apple_podcasts_url(
            "https://podcasts.apple.com/us/podcast/my-show/id123456789"
        )

    def test_itunes_url(self):
        assert is_apple_podcasts_url(
            "https://itunes.apple.com/podcast/id987654321"
        )

    def test_direct_rss_url(self):
        assert not is_apple_podcasts_url("https://feeds.example.com/podcast.rss")

    def test_empty_string(self):
        assert not is_apple_podcasts_url("")

    def test_random_url(self):
        assert not is_apple_podcasts_url("https://example.com/feed.xml")


# ---------------------------------------------------------------------------
# resolve_feed_url
# ---------------------------------------------------------------------------

class TestResolveFeedUrl:
    def test_non_apple_url_returned_as_is(self):
        url = "https://feeds.example.com/podcast.rss"
        assert resolve_feed_url(url) == url

    def test_apple_url_resolved(self):
        apple_url = "https://podcasts.apple.com/us/podcast/test/id111222333"
        itunes_response = json.dumps({
            "results": [{"feedUrl": "https://feeds.example.com/real.rss"}]
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = itunes_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("podcast_downloader.urllib.request.urlopen", return_value=mock_resp):
            result = resolve_feed_url(apple_url)

        assert result == "https://feeds.example.com/real.rss"

    def test_apple_url_no_results_returns_original(self):
        apple_url = "https://podcasts.apple.com/us/podcast/test/id111222333"
        itunes_response = json.dumps({"results": []}).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = itunes_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("podcast_downloader.urllib.request.urlopen", return_value=mock_resp):
            result = resolve_feed_url(apple_url)

        assert result == apple_url

    def test_apple_url_network_error_returns_original(self):
        apple_url = "https://podcasts.apple.com/us/podcast/test/id111222333"
        with patch(
            "podcast_downloader.urllib.request.urlopen",
            side_effect=OSError("network error"),
        ):
            result = resolve_feed_url(apple_url)

        assert result == apple_url

    def test_apple_url_no_feed_url_in_result(self):
        apple_url = "https://podcasts.apple.com/us/podcast/test/id111222333"
        itunes_response = json.dumps({"results": [{"name": "no feedUrl"}]}).encode(
            "utf-8"
        )

        mock_resp = MagicMock()
        mock_resp.read.return_value = itunes_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("podcast_downloader.urllib.request.urlopen", return_value=mock_resp):
            result = resolve_feed_url(apple_url)

        assert result == apple_url


# ---------------------------------------------------------------------------
# _parse_duration
# ---------------------------------------------------------------------------

class TestParseDuration:
    def test_hms_format(self):
        assert _parse_duration("1:02:03") == 3723

    def test_ms_format(self):
        assert _parse_duration("5:30") == 330

    def test_seconds_only(self):
        assert _parse_duration("120") == 120

    def test_empty_string(self):
        assert _parse_duration("") == 0

    def test_invalid_string(self):
        assert _parse_duration("not-a-duration") == 0


# ---------------------------------------------------------------------------
# fetch_feed_xml
# ---------------------------------------------------------------------------

class TestFetchFeedXml:
    def test_successful_fetch(self):
        fake_xml = b"<rss><channel></channel></rss>"
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_xml
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("podcast_downloader.urllib.request.urlopen", return_value=mock_resp):
            result = fetch_feed_xml("https://feeds.example.com/rss")

        assert result == fake_xml

    def test_network_failure_raises_runtime_error(self):
        with patch(
            "podcast_downloader.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            with pytest.raises(RuntimeError, match="Failed to fetch RSS feed"):
                fetch_feed_xml("https://feeds.example.com/rss")


# ---------------------------------------------------------------------------
# parse_episodes
# ---------------------------------------------------------------------------

_SAMPLE_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Test Podcast</title>
    <item>
      <title>Episode 1</title>
      <guid>https://example.com/ep1</guid>
      <enclosure url="https://example.com/ep1.mp3" length="1000" type="audio/mpeg"/>
      <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
      <itunes:duration>30:00</itunes:duration>
    </item>
    <item>
      <title>Episode 2</title>
      <guid>https://example.com/ep2</guid>
      <enclosure url="https://example.com/ep2.mp3" length="2000" type="audio/mpeg"/>
      <pubDate>Tue, 02 Jan 2024 00:00:00 +0000</pubDate>
      <itunes:duration>45:00</itunes:duration>
    </item>
  </channel>
</rss>"""

_FEED_NO_ENCLOSURE = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>No Enclosure</title>
      <guid>guid-no-enc</guid>
      <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""

_FEED_EMPTY_CHANNEL = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel></channel>
</rss>"""


class TestParseEpisodes:
    def test_parses_all_episodes(self):
        episodes = parse_episodes(_SAMPLE_FEED)
        assert len(episodes) == 2

    def test_episode_fields(self):
        episodes = parse_episodes(_SAMPLE_FEED)
        ep = episodes[0]
        assert ep.title == "Episode 1"
        assert ep.url == "https://example.com/ep1.mp3"
        assert ep.guid == "https://example.com/ep1"
        assert ep.duration == 1800

    def test_skips_items_without_enclosure(self):
        episodes = parse_episodes(_FEED_NO_ENCLOSURE)
        assert len(episodes) == 0

    def test_empty_channel(self):
        episodes = parse_episodes(_FEED_EMPTY_CHANNEL)
        assert len(episodes) == 0

    def test_invalid_xml_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="Failed to parse RSS XML"):
            parse_episodes(b"<not valid xml")

    def test_max_age_days_filter(self):
        # Both episodes are from 2024 — with a 1-day filter (from now) they should be excluded
        episodes = parse_episodes(_SAMPLE_FEED, max_age_days=1)
        assert len(episodes) == 0

    def test_max_age_days_none_returns_all(self):
        episodes = parse_episodes(_SAMPLE_FEED, max_age_days=None)
        assert len(episodes) == 2


# ---------------------------------------------------------------------------
# episode_id_from_guid
# ---------------------------------------------------------------------------

class TestEpisodeIdFromGuid:
    def test_url_guid_uses_last_segment(self):
        eid = episode_id_from_guid("https://example.com/episodes/abc123", "my-pod")
        assert eid == "abc123"

    def test_plain_guid(self):
        eid = episode_id_from_guid("plain-guid-value", "my-pod")
        assert eid == "plain-guid-value"

    def test_guid_with_query_string_stripped(self):
        eid = episode_id_from_guid(
            "https://example.com/episodes/ep99?source=rss", "my-pod"
        )
        assert eid == "ep99"

    def test_guid_special_chars_replaced(self):
        eid = episode_id_from_guid("https://example.com/ep/hello world!", "my-pod")
        # space and ! should be replaced with underscores
        assert " " not in eid
        assert "!" not in eid

    def test_max_length_80(self):
        long_guid = "a" * 200
        eid = episode_id_from_guid(long_guid, "my-pod")
        assert len(eid) <= 80


# ---------------------------------------------------------------------------
# download_episode
# ---------------------------------------------------------------------------

class TestDownloadEpisode:
    def test_successful_download(self, tmp_path):
        fake_audio = b"ID3" + b"\x00" * 100
        mock_resp = MagicMock()
        mock_resp.read.side_effect = [fake_audio, b""]
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "podcast_downloader.urllib.request.urlopen", return_value=mock_resp
        ):
            path = download_episode(
                "https://example.com/ep.mp3", "ep001", str(tmp_path)
            )

        assert path == str(tmp_path / "ep001.mp3")
        assert os.path.exists(path)

    def test_network_failure_raises_runtime_error(self, tmp_path):
        with patch(
            "podcast_downloader.urllib.request.urlopen",
            side_effect=OSError("timeout"),
        ):
            with pytest.raises(RuntimeError, match="Failed to download episode"):
                download_episode(
                    "https://example.com/ep.mp3", "ep001", str(tmp_path)
                )
