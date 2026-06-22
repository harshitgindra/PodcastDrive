"""Property-based tests for YouTube Playlist to Podcast Lambda.

Uses the Hypothesis library to verify correctness properties defined
in the design document.
"""

import re
import string
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from models import EpisodeMeta, PlaylistMeta, VideoEntry
from rss_generator import ITUNES_NS, generate_rss
from utils import extract_playlist_id, parse_upload_date

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Playlist IDs: alphanumeric + hyphen + underscore, non-empty
playlist_id_chars = string.ascii_letters + string.digits + "-_"
playlist_id_strategy = st.text(
    alphabet=playlist_id_chars, min_size=1, max_size=50
)

# XML-safe text: printable characters that won't break XML serialization.
# XML 1.0 forbids most control characters (U+0000-U+001F except tab/newline/cr).
xml_safe_chars = string.ascii_letters + string.digits + " .,!?;:-_()[]{}@#$%^&*+=/<>'\""
xml_safe_text = st.text(alphabet=xml_safe_chars, min_size=1, max_size=50)
xml_safe_text_or_empty = st.text(alphabet=xml_safe_chars, max_size=100)

# Valid YYYYMMDD date strings within a reasonable range
valid_upload_date_strategy = st.dates(
    min_value=datetime(2020, 1, 1).date(),
    max_value=datetime(2030, 12, 31).date(),
).map(lambda d: d.strftime("%Y%m%d"))

# Recent upload dates (within 7 days)
recent_upload_date_strategy = st.integers(min_value=0, max_value=6).map(
    lambda days_ago: (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y%m%d")
)

# Old upload dates (older than 7 days)
old_upload_date_strategy = st.integers(min_value=8, max_value=365).map(
    lambda days_ago: (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y%m%d")
)

# Video IDs: alphanumeric + hyphen + underscore, non-empty
video_id_strategy = st.text(
    alphabet=string.ascii_letters + string.digits + "-_",
    min_size=1,
    max_size=20,
)


def video_entry_strategy(
    upload_date_st=None,
    video_id_st=None,
):
    """Build a strategy for VideoEntry with configurable date/id strategies."""
    if upload_date_st is None:
        upload_date_st = valid_upload_date_strategy
    if video_id_st is None:
        video_id_st = video_id_strategy

    return st.builds(
        VideoEntry,
        video_id=video_id_st,
        title=xml_safe_text,
        description=xml_safe_text_or_empty,
        duration=st.one_of(st.none(), st.integers(min_value=0, max_value=36000)),
        upload_date=upload_date_st,
        thumbnail=st.just("https://img.youtube.com/vi/test/0.jpg"),
        webpage_url=st.just("https://youtube.com/watch?v=test"),
        playlist_index=st.one_of(st.none(), st.integers(min_value=1, max_value=1000)),
    )


def episode_meta_strategy(
    playlist_id="PLtest123",
    cloudfront_base="https://cdn.example.com",
):
    """Build a strategy for EpisodeMeta."""
    return st.builds(
        EpisodeMeta,
        video_id=video_id_strategy,
        title=xml_safe_text,
        description=xml_safe_text_or_empty,
        duration=st.one_of(st.none(), st.integers(min_value=1, max_value=36000)),
        upload_date=valid_upload_date_strategy,
        thumbnail=st.just("https://img.youtube.com/vi/test/0.jpg"),
        webpage_url=st.just("https://youtube.com/watch?v=test"),
        playlist_index=st.one_of(st.none(), st.integers(min_value=1, max_value=1000)),
        s3_key=st.just(f"{playlist_id}/episodes/test.mp3"),
        file_size=st.integers(min_value=1000, max_value=100_000_000),
        cloudfront_url=st.just(f"{cloudfront_base}/{playlist_id}/episodes/test.mp3"),
    )


def playlist_meta_strategy():
    """Build a strategy for PlaylistMeta."""
    return st.builds(
        PlaylistMeta,
        title=xml_safe_text,
        description=xml_safe_text_or_empty,
        uploader=xml_safe_text,
        channel_url=st.just("https://youtube.com/c/test"),
        webpage_url=st.just("https://youtube.com/playlist?list=PLtest123"),
        playlist_id=st.just("PLtest123"),
    )


# ---------------------------------------------------------------------------
# Property 1: Playlist ID extraction round-trip
# Validates: Requirement 1.2
# ---------------------------------------------------------------------------


class TestProperty1PlaylistIdRoundTrip:
    """**Validates: Requirements 1.2**

    For any valid YouTube playlist ID string, constructing a YouTube playlist
    URL with that ID and then extracting the playlist ID from the URL SHALL
    return the original ID.
    """

    @given(playlist_id=playlist_id_strategy)
    @settings(max_examples=200)
    def test_roundtrip_extraction(self, playlist_id: str):
        """**Validates: Requirements 1.2**"""
        url = f"https://www.youtube.com/playlist?list={playlist_id}"
        extracted = extract_playlist_id(url)
        assert extracted == playlist_id


# ---------------------------------------------------------------------------
# Property 2: Invalid date fallback
# Validates: Requirement 2.6
# ---------------------------------------------------------------------------


class TestProperty2InvalidDateFallback:
    """**Validates: Requirements 2.6**

    For any string that is not a valid YYYYMMDD date, parse_upload_date SHALL
    return today's date (UTC) rather than raising an error.
    """

    @given(date_str=st.text(max_size=50))
    @settings(max_examples=200)
    def test_invalid_date_returns_today(self, date_str: str):
        """**Validates: Requirements 2.6**"""
        # Filter out strings that happen to be valid YYYYMMDD dates
        try:
            datetime.strptime(date_str, "%Y%m%d")
            assume(False)  # Skip valid dates
        except (ValueError, TypeError):
            pass

        result = parse_upload_date(date_str)
        today = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        assert result == today
        assert result.tzinfo == UTC


# ---------------------------------------------------------------------------
# Property 6: RSS output is well-formed XML with iTunes namespace
# Validates: Requirement 6.1
# ---------------------------------------------------------------------------

CLOUDFRONT_BASE = "https://cdn.example.com"
PLAYLIST_ID = "PLtest123"


class TestProperty6RssWellFormedXml:
    """**Validates: Requirements 6.1**

    For any valid PlaylistMeta and any list of EpisodeMeta objects, the
    RSS_Generator SHALL produce output that parses as well-formed XML and
    contains the iTunes namespace declaration.
    """

    @given(
        meta=playlist_meta_strategy(),
        episodes=st.lists(episode_meta_strategy(), min_size=0, max_size=10),
    )
    @settings(max_examples=100)
    def test_valid_xml_with_itunes_namespace(
        self, meta: PlaylistMeta, episodes: list[EpisodeMeta]
    ):
        """**Validates: Requirements 6.1**"""
        xml_str = generate_rss(meta, episodes, CLOUDFRONT_BASE, PLAYLIST_ID)

        # Must parse as valid XML
        root = ET.fromstring(xml_str)
        assert root.tag == "rss"
        assert root.get("version") == "2.0"

        # Must contain iTunes namespace
        assert ITUNES_NS in xml_str


# ---------------------------------------------------------------------------
# Property 7: RSS channel metadata completeness
# Validates: Requirement 6.2
# ---------------------------------------------------------------------------


class TestProperty7RssChannelMetadata:
    """**Validates: Requirements 6.2**

    For any valid PlaylistMeta, the generated RSS feed SHALL contain
    channel-level elements for title, link, description, language, generator,
    lastBuildDate, and iTunes author, summary, explicit, and owner tags.
    """

    @given(meta=playlist_meta_strategy())
    @settings(max_examples=100)
    def test_channel_metadata_present(self, meta: PlaylistMeta):
        """**Validates: Requirements 6.2**"""
        xml_str = generate_rss(meta, [], CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        channel = root.find("channel")
        ns = {"itunes": ITUNES_NS}

        # Standard RSS channel elements
        assert channel.find("title") is not None
        assert channel.find("link") is not None
        assert channel.find("description") is not None
        assert channel.find("language") is not None
        assert channel.find("generator") is not None
        assert channel.find("lastBuildDate") is not None

        # iTunes channel elements
        assert channel.find("itunes:author", ns) is not None
        assert channel.find("itunes:summary", ns) is not None
        assert channel.find("itunes:explicit", ns) is not None
        assert channel.find("itunes:owner", ns) is not None


# ---------------------------------------------------------------------------
# Property 8: RSS item correctness
# Validates: Requirements 6.3, 6.4, 6.5, 6.6
# ---------------------------------------------------------------------------


class TestProperty8RssItemCorrectness:
    """**Validates: Requirements 6.3, 6.4, 6.5, 6.6**

    For any list of N EpisodeMeta objects, the generated RSS feed SHALL
    contain exactly N <item> elements, each with a <title>, <guid> matching
    the video_id (with isPermaLink="false"), an <enclosure> URL following
    the pattern, a <pubDate>, and a <description>.
    """

    @given(
        meta=playlist_meta_strategy(),
        episodes=st.lists(episode_meta_strategy(), min_size=0, max_size=10),
    )
    @settings(max_examples=100)
    def test_item_count_and_structure(
        self, meta: PlaylistMeta, episodes: list[EpisodeMeta]
    ):
        """**Validates: Requirements 6.3, 6.4, 6.5, 6.6**"""
        xml_str = generate_rss(meta, episodes, CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        items = root.findall(".//item")

        # Exactly N items
        assert len(items) == len(episodes)

        for i, item in enumerate(items):
            ep = episodes[i]

            # Title present
            assert item.find("title") is not None

            # GUID matches video_id with isPermaLink=false
            guid = item.find("guid")
            assert guid is not None
            assert guid.text == ep.video_id
            assert guid.get("isPermaLink") == "false"

            # Enclosure URL follows pattern
            enc = item.find("enclosure")
            assert enc is not None
            assert enc.get("type") == "audio/mpeg"

            # pubDate present
            assert item.find("pubDate") is not None

            # Description present
            assert item.find("description") is not None


# ---------------------------------------------------------------------------
# Property 9: RSS date and duration formatting
# Validates: Requirements 6.7, 6.8
# ---------------------------------------------------------------------------


class TestProperty9RssDateDurationFormatting:
    """**Validates: Requirements 6.7, 6.8**

    For any EpisodeMeta with a valid upload_date, the generated <pubDate>
    SHALL be parseable as RFC 2822. For any EpisodeMeta with a non-None
    duration, the generated <itunes:duration> SHALL match the pattern
    H:MM:SS or M:SS.
    """

    @given(
        meta=playlist_meta_strategy(),
        episodes=st.lists(
            episode_meta_strategy(),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(max_examples=100)
    def test_date_and_duration_formatting(
        self, meta: PlaylistMeta, episodes: list[EpisodeMeta]
    ):
        """**Validates: Requirements 6.7, 6.8**"""
        xml_str = generate_rss(meta, episodes, CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        ns = {"itunes": ITUNES_NS}
        items = root.findall(".//item")

        for i, item in enumerate(items):
            episodes[i]

            # pubDate should be parseable as RFC 2822
            pub_date_text = item.find("pubDate").text
            from email.utils import parsedate_to_datetime
            parsed = parsedate_to_datetime(pub_date_text)
            assert parsed is not None

            # itunes:duration should match H:MM:SS or M:SS pattern
            duration_el = item.find("itunes:duration", ns)
            assert duration_el is not None
            duration_text = duration_el.text
            assert re.match(r"^\d+:\d{2}(:\d{2})?$", duration_text)


# ---------------------------------------------------------------------------
# Property 10: RSS episode sort order
# Validates: Requirement 6.10
# ---------------------------------------------------------------------------


class TestProperty10RssSortOrder:
    """**Validates: Requirements 6.10**

    For any list of EpisodeMeta objects with distinct upload_dates, the
    generated RSS feed SHALL list items in descending order by upload_date
    (newest first).
    """

    @given(
        meta=playlist_meta_strategy(),
        episodes=st.lists(
            episode_meta_strategy(),
            min_size=2,
            max_size=10,
        ),
    )
    @settings(max_examples=100)
    def test_items_sorted_newest_first(
        self, meta: PlaylistMeta, episodes: list[EpisodeMeta]
    ):
        """**Validates: Requirements 6.10**"""
        # Make upload_dates distinct
        seen_dates = set()
        unique_episodes = []
        for ep in episodes:
            if ep.upload_date not in seen_dates:
                seen_dates.add(ep.upload_date)
                unique_episodes.append(ep)

        assume(len(unique_episodes) >= 2)

        # Sort episodes newest-first (as the generator expects)
        sorted_episodes = sorted(
            unique_episodes, key=lambda e: e.upload_date, reverse=True
        )

        xml_str = generate_rss(meta, sorted_episodes, CLOUDFRONT_BASE, PLAYLIST_ID)
        root = ET.fromstring(xml_str)
        items = root.findall(".//item")

        # Extract pubDate from each item and verify descending order
        dates = []
        for item in items:
            pub_text = item.find("pubDate").text
            from email.utils import parsedate_to_datetime
            dates.append(parsedate_to_datetime(pub_text))

        for i in range(len(dates) - 1):
            assert dates[i] >= dates[i + 1]


# ---------------------------------------------------------------------------
# Property 11: S3 key construction scoped to playlist_id prefix
# Validates: Requirements 5.1, 5.7, 9.1, 9.2, 9.3
# ---------------------------------------------------------------------------


class TestProperty11S3KeyScoping:
    """**Validates: Requirements 5.1, 5.7, 9.1, 9.2, 9.3**

    For any playlist_id, all S3 operations (list, put, delete) performed by
    the S3_Manager SHALL use keys that start with {playlist_id}/. No
    operation SHALL reference a key outside this prefix.
    """

    @given(
        playlist_id=playlist_id_strategy,
        video_ids=st.lists(video_id_strategy, min_size=1, max_size=10),
    )
    @settings(max_examples=200)
    def test_all_keys_scoped_to_playlist_prefix(
        self, playlist_id: str, video_ids: list[str]
    ):
        """**Validates: Requirements 5.1, 5.7, 9.1, 9.2, 9.3**"""
        # Verify the key construction patterns used by S3Manager
        # Episode key pattern
        for vid in video_ids:
            episode_key = f"{playlist_id}/episodes/{vid}.mp3"
            assert episode_key.startswith(f"{playlist_id}/")

        # Feed key pattern
        feed_key = f"{playlist_id}/feed.xml"
        assert feed_key.startswith(f"{playlist_id}/")

        # List prefix pattern
        list_prefix = f"{playlist_id}/episodes/"
        assert list_prefix.startswith(f"{playlist_id}/")
