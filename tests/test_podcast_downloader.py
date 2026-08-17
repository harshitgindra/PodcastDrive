"""Tests for src/podcast_downloader.py."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from podcast_downloader import (
    _parse_duration,
    download_episode,
    episode_id_from_guid,
    fetch_feed_xml,
    is_apple_podcasts_url,
    parse_channel_thumbnail,
    parse_episodes,
    read_capped,
    require_http_url,
    resolve_feed_url,
    search_feed_url_by_name,
)

# ---------------------------------------------------------------------------
# is_apple_podcasts_url
# ---------------------------------------------------------------------------


class TestIsApplePodcastsUrl:
    def test_apple_podcasts_url(self):
        assert is_apple_podcasts_url("https://podcasts.apple.com/us/podcast/my-show/id123456789")

    def test_itunes_url(self):
        assert is_apple_podcasts_url("https://itunes.apple.com/podcast/id987654321")

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
        itunes_response = json.dumps({"results": [{"feedUrl": "https://feeds.example.com/real.rss"}]}).encode("utf-8")

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
        itunes_response = json.dumps({"results": [{"name": "no feedUrl"}]}).encode("utf-8")

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
        with (
            patch(
                "podcast_downloader.urllib.request.urlopen",
                side_effect=OSError("connection refused"),
            ),
            pytest.raises(RuntimeError, match="Failed to fetch RSS feed"),
        ):
            fetch_feed_xml("https://feeds.example.com/rss")

    def test_non_network_exception_raises_runtime_error(self):
        """Covers the generic `except Exception` branch (line 227-228)."""
        with (
            patch(
                "podcast_downloader.urllib.request.urlopen",
                side_effect=ValueError("unexpected error"),
            ),
            pytest.raises(RuntimeError, match="Failed to fetch RSS feed"),
        ):
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


_FEED_NO_CHANNEL = b"""<?xml version="1.0"?>
<rss version="2.0"></rss>"""

_FEED_ENCLOSURE_EMPTY_URL = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Bad Enclosure</title>
      <guid>guid-bad-enc</guid>
      <enclosure url="" length="0" type="audio/mpeg"/>
      <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""

_FEED_BAD_PUBDATE = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Bad Date Episode</title>
      <guid>guid-bad-date</guid>
      <enclosure url="https://example.com/ep.mp3" length="1000" type="audio/mpeg"/>
      <pubDate>not a valid date</pubDate>
    </item>
  </channel>
</rss>"""

_FEED_NAIVE_PUBDATE = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Naive Date Episode</title>
      <guid>guid-naive-date</guid>
      <enclosure url="https://example.com/ep.mp3" length="1000" type="audio/mpeg"/>
      <pubDate>Mon, 01 Jan 2024 12:00:00</pubDate>
    </item>
  </channel>
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

    def test_no_channel_element_returns_empty(self):
        """Covers line 287: channel is None → return []."""
        episodes = parse_episodes(_FEED_NO_CHANNEL)
        assert len(episodes) == 0

    def test_skips_enclosure_with_empty_url(self):
        """Covers line 306: enclosure url is empty string → skip item."""
        episodes = parse_episodes(_FEED_ENCLOSURE_EMPTY_URL)
        assert len(episodes) == 0

    def test_bad_pubdate_falls_back_to_epoch(self):
        """An unparseable pubDate must not look like the newest episode in the feed."""
        episodes = parse_episodes(_FEED_BAD_PUBDATE)
        assert len(episodes) == 1
        assert episodes[0].pub_date == datetime(1970, 1, 1, tzinfo=UTC)

    def test_naive_pubdate_gets_utc_timezone(self):
        """Covers line 322: timezone-naive date gets UTC applied."""
        episodes = parse_episodes(_FEED_NAIVE_PUBDATE)
        assert len(episodes) == 1
        assert episodes[0].pub_date.tzinfo is not None

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
        eid = episode_id_from_guid("https://example.com/episodes/abc123")
        assert eid == "abc123"

    def test_plain_guid(self):
        eid = episode_id_from_guid("plain-guid-value")
        assert eid == "plain-guid-value"

    def test_guid_with_query_string_stripped(self):
        """The path segment leads the ID; a digest keeps query-string variants distinct."""
        eid = episode_id_from_guid("https://example.com/episodes/ep99?source=rss")
        assert eid.startswith("ep99-")

    def test_guid_special_chars_replaced(self):
        eid = episode_id_from_guid("https://example.com/ep/hello world!")
        # space and ! should be replaced with underscores
        assert " " not in eid
        assert "!" not in eid

    def test_max_length_80(self):
        long_guid = "a" * 200
        eid = episode_id_from_guid(long_guid)
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

        with patch("podcast_downloader.urllib.request.urlopen", return_value=mock_resp):
            path = download_episode("https://example.com/ep.mp3", "ep001", str(tmp_path))

        assert path == str(tmp_path / "ep001.mp3")
        assert os.path.exists(path)

    def test_network_failure_raises_runtime_error(self, tmp_path):
        with (
            patch(
                "podcast_downloader.urllib.request.urlopen",
                side_effect=OSError("timeout"),
            ),
            pytest.raises(RuntimeError, match="Failed to download episode"),
        ):
            download_episode("https://example.com/ep.mp3", "ep001", str(tmp_path))

    def test_partial_file_removed_on_download_failure(self, tmp_path):
        """Covers line 432: partial file is deleted when download raises."""
        partial = tmp_path / "ep001.mp3"

        def fake_urlopen(req, **kwargs):
            # Write a partial file then raise to simulate a mid-download failure
            partial.write_bytes(b"partial")
            raise OSError("connection reset")

        with patch("podcast_downloader.urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(RuntimeError, match="Failed to download episode"):
                download_episode("https://example.com/ep.mp3", "ep001", str(tmp_path))

        # Partial file must have been cleaned up
        assert not partial.exists()


# ---------------------------------------------------------------------------
# search_feed_url_by_name
# ---------------------------------------------------------------------------


class TestSearchFeedUrlByName:
    def _mock_response(self, data: dict) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(data).encode("utf-8")
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_returns_feed_url_on_match(self):
        mock_resp = self._mock_response({"results": [{"feedUrl": "https://feeds.example.com/my-podcast.rss"}]})
        with patch("podcast_downloader.urllib.request.urlopen", return_value=mock_resp):
            result = search_feed_url_by_name("My Podcast")
        assert result == "https://feeds.example.com/my-podcast.rss"

    def test_empty_name_returns_empty_string(self):
        result = search_feed_url_by_name("")
        assert result == ""

    def test_no_results_returns_empty_string(self):
        mock_resp = self._mock_response({"results": []})
        with patch("podcast_downloader.urllib.request.urlopen", return_value=mock_resp):
            result = search_feed_url_by_name("Unknown Podcast XYZ")
        assert result == ""

    def test_result_has_no_feed_url_returns_empty_string(self):
        mock_resp = self._mock_response({"results": [{"trackName": "No feed here"}]})
        with patch("podcast_downloader.urllib.request.urlopen", return_value=mock_resp):
            result = search_feed_url_by_name("Some Podcast")
        assert result == ""

    def test_network_error_returns_empty_string(self):
        with patch(
            "podcast_downloader.urllib.request.urlopen",
            side_effect=OSError("timeout"),
        ):
            result = search_feed_url_by_name("My Podcast")
        assert result == ""

    def test_name_is_url_encoded(self):
        captured_urls = []

        def fake_urlopen(req, **kwargs):
            captured_urls.append(req.full_url)
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"results": []}).encode("utf-8")
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch("podcast_downloader.urllib.request.urlopen", side_effect=fake_urlopen):
            search_feed_url_by_name("9to5Mac Daily")

        assert captured_urls
        # spaces and special chars should be URL-encoded
        assert " " not in captured_urls[0]


# ---------------------------------------------------------------------------
# parse_channel_thumbnail
# ---------------------------------------------------------------------------

_FEED_ITUNES_IMAGE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Test Podcast</title>
    <itunes:image href="https://example.com/artwork.jpg"/>
  </channel>
</rss>"""

_FEED_RSS_IMAGE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Podcast</title>
    <image>
      <url>https://example.com/cover.jpg</url>
      <title>Test Podcast</title>
      <link>https://example.com</link>
    </image>
  </channel>
</rss>"""

_FEED_BOTH_IMAGES = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <itunes:image href="https://example.com/itunes-art.jpg"/>
    <image>
      <url>https://example.com/rss-art.jpg</url>
    </image>
  </channel>
</rss>"""

_FEED_NO_IMAGE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>No Image Podcast</title>
  </channel>
</rss>"""


class TestParseChannelThumbnail:
    def test_itunes_image_href(self):
        url = parse_channel_thumbnail(_FEED_ITUNES_IMAGE)
        assert url == "https://example.com/artwork.jpg"

    def test_rss_image_url_element(self):
        url = parse_channel_thumbnail(_FEED_RSS_IMAGE)
        assert url == "https://example.com/cover.jpg"

    def test_itunes_image_preferred_over_rss_image(self):
        # When both are present, itunes:image should win
        url = parse_channel_thumbnail(_FEED_BOTH_IMAGES)
        assert url == "https://example.com/itunes-art.jpg"

    def test_no_image_returns_empty_string(self):
        url = parse_channel_thumbnail(_FEED_NO_IMAGE)
        assert url == ""

    def test_invalid_xml_returns_empty_string(self):
        url = parse_channel_thumbnail(b"<not valid xml")
        assert url == ""

    def test_no_channel_returns_empty_string(self):
        url = parse_channel_thumbnail(b"<rss version='2.0'></rss>")
        assert url == ""


# ---------------------------------------------------------------------------
# parse_episodes — thumbnail extraction
# ---------------------------------------------------------------------------

_FEED_WITH_ITEM_IMAGE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Art Podcast</title>
    <itunes:image href="https://example.com/channel-art.jpg"/>
    <item>
      <title>Episode with own art</title>
      <guid>ep-own-art</guid>
      <enclosure url="https://example.com/ep1.mp3" length="1000" type="audio/mpeg"/>
      <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
      <itunes:image href="https://example.com/ep-art.jpg"/>
    </item>
    <item>
      <title>Episode without art</title>
      <guid>ep-no-art</guid>
      <enclosure url="https://example.com/ep2.mp3" length="2000" type="audio/mpeg"/>
      <pubDate>Tue, 02 Jan 2024 00:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""


class TestParseEpisodesThumbnails:
    def test_episode_with_own_itunes_image(self):
        episodes = parse_episodes(_FEED_WITH_ITEM_IMAGE)
        ep = next(e for e in episodes if e.guid == "ep-own-art")
        assert ep.thumbnail == "https://example.com/ep-art.jpg"

    def test_episode_without_art_falls_back_to_channel_thumbnail(self):
        episodes = parse_episodes(_FEED_WITH_ITEM_IMAGE)
        ep = next(e for e in episodes if e.guid == "ep-no-art")
        assert ep.thumbnail == "https://example.com/channel-art.jpg"

    def test_no_thumbnail_field_is_empty_string_when_no_channel_art(self):
        episodes = parse_episodes(_SAMPLE_FEED)  # _SAMPLE_FEED has no images
        for ep in episodes:
            assert ep.thumbnail == ""


# ---------------------------------------------------------------------------
# Fix #10 – HTTP range-request download resume
# ---------------------------------------------------------------------------


class TestDownloadEpisodeRangeResume:
    """Tests for range-request resumption in download_episode (fix #10)."""

    def _make_resp(self, data: bytes, status: int = 200):
        """Build a mock urllib response."""
        resp = MagicMock()
        resp.read.side_effect = [data, b""]
        resp.status = status
        resp.getcode.return_value = status
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_resume_sends_range_header(self, tmp_path):
        """When a partial file exists, the Range header is sent."""
        partial = tmp_path / "ep001.mp3"
        partial.write_bytes(b"partial-data")  # 12 bytes already downloaded

        remaining = b"rest-of-file"
        resp = self._make_resp(remaining, status=206)

        captured_reqs = []

        def fake_urlopen(req, **kwargs):
            captured_reqs.append(req)
            return resp

        with patch("podcast_downloader.urllib.request.urlopen", side_effect=fake_urlopen):
            download_episode("https://example.com/ep.mp3", "ep001", str(tmp_path))

        assert captured_reqs, "urlopen was not called"
        assert captured_reqs[0].get_header("Range") == f"bytes={len(b'partial-data')}-"
        # File should contain original partial + new data
        assert partial.read_bytes() == b"partial-data" + b"rest-of-file"

    def test_206_appends_to_partial_file(self, tmp_path):
        """A 206 response causes the download to append, not overwrite."""
        partial = tmp_path / "ep001.mp3"
        partial.write_bytes(b"AAA")

        resp = self._make_resp(b"BBB", status=206)

        with patch("podcast_downloader.urllib.request.urlopen", return_value=resp):
            download_episode("https://example.com/ep.mp3", "ep001", str(tmp_path))

        assert partial.read_bytes() == b"AAABBB"

    def test_200_overwrites_when_no_range_support(self, tmp_path):
        """A 200 response (server ignores Range) restarts the download from scratch."""
        partial = tmp_path / "ep001.mp3"
        partial.write_bytes(b"stale-partial")

        resp = self._make_resp(b"fresh-full", status=200)

        with patch("podcast_downloader.urllib.request.urlopen", return_value=resp):
            download_episode("https://example.com/ep.mp3", "ep001", str(tmp_path))

        assert partial.read_bytes() == b"fresh-full"

    def test_416_treated_as_already_complete(self, tmp_path):
        """HTTP 416 (Range Not Satisfiable) means the file is already fully downloaded."""
        import urllib.error

        partial = tmp_path / "ep001.mp3"
        partial.write_bytes(b"complete-data")

        def fake_urlopen(req, **kwargs):
            raise urllib.error.HTTPError("url", 416, "Range Not Satisfiable", {}, None)

        with patch("podcast_downloader.urllib.request.urlopen", side_effect=fake_urlopen):
            result = download_episode("https://example.com/ep.mp3", "ep001", str(tmp_path))

        assert result == str(partial)
        assert partial.read_bytes() == b"complete-data"  # Unchanged

    def test_fresh_download_no_range_header(self, tmp_path):
        """Without a partial file, no Range header is sent."""
        resp = self._make_resp(b"full-content", status=200)
        captured_reqs = []

        def fake_urlopen(req, **kwargs):
            captured_reqs.append(req)
            return resp

        with patch("podcast_downloader.urllib.request.urlopen", side_effect=fake_urlopen):
            download_episode("https://example.com/ep.mp3", "ep001", str(tmp_path))

        assert captured_reqs[0].get_header("Range") is None


# ---------------------------------------------------------------------------
# parse_episodes — malformed pubDate must not defeat the age filter
# ---------------------------------------------------------------------------

_FEED_NO_PUBDATE = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>No Date Episode</title>
      <guid>guid-no-date</guid>
      <enclosure url="https://example.com/ep.mp3" length="1000" type="audio/mpeg"/>
    </item>
  </channel>
</rss>"""

_FEED_MIXED_DATES = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Good</title>
      <guid>guid-good</guid>
      <enclosure url="https://example.com/good.mp3" length="1000" type="audio/mpeg"/>
      <pubDate>Wed, 02 Oct 2030 13:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Broken</title>
      <guid>guid-broken</guid>
      <enclosure url="https://example.com/broken.mp3" length="1000" type="audio/mpeg"/>
      <pubDate>garbage</pubDate>
    </item>
  </channel>
</rss>"""


class TestMalformedPubDate:
    def test_missing_pubdate_element_falls_back_to_epoch(self):
        episodes = parse_episodes(_FEED_NO_PUBDATE)
        assert len(episodes) == 1
        assert episodes[0].pub_date == datetime(1970, 1, 1, tzinfo=UTC)

    def test_epoch_fallback_is_excluded_by_the_age_filter(self):
        """The point of the fix: a dateless episode must not download forever."""
        episodes = parse_episodes(_FEED_BAD_PUBDATE, max_age_days=30)
        assert episodes == []

    def test_missing_pubdate_is_excluded_by_the_age_filter(self):
        episodes = parse_episodes(_FEED_NO_PUBDATE, max_age_days=30)
        assert episodes == []

    def test_broken_date_does_not_outrank_a_valid_one(self):
        episodes = parse_episodes(_FEED_MIXED_DATES)
        by_title = {e.title: e.pub_date for e in episodes}
        assert by_title["Broken"] < by_title["Good"]

    def test_malformed_pubdate_is_logged_as_a_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="podcast_downloader"):
            parse_episodes(_FEED_BAD_PUBDATE)
        assert "unparseable pubDate" in caplog.text

    def test_valid_pubdate_is_unaffected(self):
        episodes = parse_episodes(_FEED_MIXED_DATES)
        by_title = {e.title: e.pub_date for e in episodes}
        assert by_title["Good"] == datetime(2030, 10, 2, 13, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# URL scheme allowlist (Fix #12)
# ---------------------------------------------------------------------------


class TestRequireHttpUrl:
    """Feed/enclosure URLs are untrusted, so only http(s) may be fetched."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://feeds.example.com/rss",
            "https://feeds.example.com/rss",
            "HTTPS://feeds.example.com/rss",
            "  https://feeds.example.com/rss  ",
        ],
    )
    def test_accepts_http_and_https(self, url):
        assert require_http_url(url, "RSS feed") == url

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "file://localhost/etc/shadow",
            "ftp://example.com/feed.xml",
            "data:text/xml,<rss/>",
            "gopher://example.com/",
            "/etc/passwd",
            "feeds.example.com/rss",
        ],
    )
    def test_rejects_other_schemes(self, url):
        with pytest.raises(ValueError, match="unsupported URL scheme"):
            require_http_url(url, "RSS feed")

    @pytest.mark.parametrize("url", ["", None])
    def test_rejects_empty_url(self, url):
        with pytest.raises(ValueError, match="empty URL"):
            require_http_url(url, "RSS feed")

    def test_error_message_names_the_resource(self):
        with pytest.raises(ValueError, match="episode ep-1"):
            require_http_url("file:///etc/passwd", "episode ep-1")


class TestFetchFeedXmlRejectsLocalFiles:
    def test_file_url_is_refused_without_opening_it(self, tmp_path):
        secret = tmp_path / "secret.xml"
        secret.write_text("<rss><channel><title>secret</title></channel></rss>")

        with patch("podcast_downloader.urllib.request.urlopen") as mock_open:
            with pytest.raises(ValueError, match="unsupported URL scheme"):
                fetch_feed_xml(f"file://{secret}")
        mock_open.assert_not_called()


class TestDownloadEpisodeRejectsLocalFiles:
    def test_file_url_is_refused_and_nothing_written(self, tmp_path):
        source = tmp_path / "local.mp3"
        source.write_bytes(b"\x00" * 2048)
        dest_dir = tmp_path / "dl"
        dest_dir.mkdir()

        with patch("podcast_downloader.urllib.request.urlopen") as mock_open:
            with pytest.raises(ValueError, match="unsupported URL scheme"):
                download_episode(f"file://{source}", "ep-1", str(dest_dir))
        mock_open.assert_not_called()
        assert list(dest_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# episode_id_from_guid — empty / colliding IDs (Fix #14)
# ---------------------------------------------------------------------------


class TestEpisodeIdNeverEmpty:
    @pytest.mark.parametrize("guid", ["", "/", "///", "?x=1", "#frag", ".", "..", "https://example.com/"])
    def test_degenerate_guids_still_produce_a_usable_id(self, guid):
        eid = episode_id_from_guid(guid)
        assert eid, f"empty id for {guid!r} would yield the S3 key 'slug/episodes/.mp3'"
        assert eid.strip(".")
        assert "/" not in eid
        assert re.fullmatch(r"[A-Za-z0-9._-]+", eid)

    def test_non_string_guid_is_tolerated(self):
        assert episode_id_from_guid(None) != ""


class TestEpisodeIdCollisions:
    def test_wordpress_style_guids_do_not_all_collide(self):
        """"https://example.com/?p=N" has its identity in the query string."""
        ids = {episode_id_from_guid(f"https://example.com/?p={n}") for n in range(5)}
        assert len(ids) == 5

    def test_query_string_variants_do_not_collide(self):
        a = episode_id_from_guid("https://example.com/ep?v=1")
        b = episode_id_from_guid("https://example.com/ep?v=2")
        assert a != b

    def test_ids_differing_only_by_unsafe_chars_do_not_collide(self):
        a = episode_id_from_guid("ep#1")
        b = episode_id_from_guid("ep 1")
        c = episode_id_from_guid("ep@1")
        assert len({a, b, c}) == 3

    def test_empty_guid_variants_do_not_collide(self):
        assert episode_id_from_guid("") != episode_id_from_guid("/")

    def test_long_guids_sharing_a_prefix_do_not_collide(self):
        a = episode_id_from_guid("x" * 200 + "1")
        b = episode_id_from_guid("x" * 200 + "2")
        assert a != b
        assert len(a) <= 80

    def test_clean_guids_are_unchanged(self):
        """Existing S3 keys must stay stable for GUIDs that need no sanitising."""
        assert episode_id_from_guid("abc123def456") == "abc123def456"
        assert episode_id_from_guid("plain-guid-value") == "plain-guid-value"
        assert episode_id_from_guid("https://example.com/episodes/abc123") == "abc123"
        assert episode_id_from_guid("https://example.com/episodes/abc123/") == "abc123"

    def test_id_is_deterministic(self):
        assert episode_id_from_guid("ep 1") == episode_id_from_guid("ep 1")


# ---------------------------------------------------------------------------
# Bounded response reads (Fix #16)
# ---------------------------------------------------------------------------


class _StreamingResponse:
    """A response whose body is generated on demand, like a real socket."""

    def __init__(self, total: int, chunk: bytes = b"x"):
        self.total = total
        self.chunk = chunk
        self.served = 0

    def read(self, amt=None):
        remaining = self.total - self.served
        n = remaining if amt is None else min(amt, remaining)
        self.served += n
        return self.chunk * n

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestReadCapped:
    def test_returns_body_under_the_limit(self):
        resp = _StreamingResponse(100)
        assert read_capped(resp, 1024, "RSS feed") == b"x" * 100

    def test_body_exactly_at_the_limit_is_accepted(self):
        resp = _StreamingResponse(1024)
        assert len(read_capped(resp, 1024, "RSS feed")) == 1024

    def test_oversized_body_raises(self):
        resp = _StreamingResponse(2048)
        with pytest.raises(RuntimeError, match="larger than the 1024 byte limit"):
            read_capped(resp, 1024, "RSS feed")

    def test_never_reads_more_than_limit_plus_one(self):
        resp = _StreamingResponse(10 * 1024 * 1024)
        with pytest.raises(RuntimeError):
            read_capped(resp, 1024, "RSS feed")
        assert resp.served == 1025, "the whole stream was buffered anyway"


class TestFetchFeedXmlSizeLimit:
    def test_oversized_feed_is_refused(self, monkeypatch):
        monkeypatch.setenv("MAX_FEED_BYTES", "1000")
        resp = _StreamingResponse(50_000)

        with patch("podcast_downloader.urllib.request.urlopen", return_value=resp):
            with pytest.raises(RuntimeError, match="Failed to fetch RSS feed"):
                fetch_feed_xml("https://feeds.example.com/rss")

        assert resp.served == 1001

    def test_normal_feed_still_fetched(self, monkeypatch):
        monkeypatch.setenv("MAX_FEED_BYTES", "100000")
        body = b"<rss><channel></channel></rss>"
        resp = _StreamingResponse(len(body), b"<rss><channel></channel></rss>"[:1])

        with patch("podcast_downloader.urllib.request.urlopen", return_value=resp):
            result = fetch_feed_xml("https://feeds.example.com/rss")

        assert len(result) == len(body)

    def test_malformed_limit_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("MAX_FEED_BYTES", "not-a-number")
        resp = _StreamingResponse(10)

        with patch("podcast_downloader.urllib.request.urlopen", return_value=resp):
            assert fetch_feed_xml("https://feeds.example.com/rss") == b"x" * 10


class TestItunesResponseSizeLimit:
    def test_oversized_lookup_response_is_not_buffered(self, monkeypatch):
        monkeypatch.setenv("MAX_ITUNES_BYTES", "500")
        resp = _StreamingResponse(1_000_000)

        with patch("podcast_downloader.urllib.request.urlopen", return_value=resp):
            # resolve_feed_url swallows failures and returns the original URL
            out = resolve_feed_url("https://podcasts.apple.com/us/podcast/x/id123")

        assert out == "https://podcasts.apple.com/us/podcast/x/id123"
        assert resp.served == 501

    def test_oversized_search_response_is_not_buffered(self, monkeypatch):
        monkeypatch.setenv("MAX_ITUNES_BYTES", "500")
        resp = _StreamingResponse(1_000_000)

        with patch("podcast_downloader.urllib.request.urlopen", return_value=resp):
            assert search_feed_url_by_name("Some Podcast") == ""

        assert resp.served == 501
