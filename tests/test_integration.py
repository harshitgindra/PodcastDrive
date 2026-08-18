"""Integration tests for PodcastDrive.

These tests exercise the real AWS infrastructure (S3, Transcribe, Bedrock)
and make actual network calls to YouTube.  They are **skipped by default**
and must be opted-in explicitly:

    RUN_INTEGRATION_TESTS=true pytest tests/test_integration.py -v

Pre-requisites
--------------
* Valid AWS credentials in the environment (or ~/.aws/credentials).
* The following environment variables set (same as production):
    - S3_BUCKET            – target bucket (use a *test* bucket, not prod)
    - CLOUDFRONT_BASE      – https://... URL for the test bucket
    - AWS_REGION           – e.g. us-east-1
* ``yt-dlp`` and ``ffmpeg`` available on PATH.
* Optional: INTEGRATION_PLAYLIST_URL to override the default test playlist.

These tests may incur small AWS charges and take several minutes to run.
"""

import os
import xml.etree.ElementTree as ET

import pytest

# ---------------------------------------------------------------------------
# Guard — skip entire module unless explicitly opted-in
# ---------------------------------------------------------------------------

RUN_INTEGRATION = os.environ.get("RUN_INTEGRATION_TESTS", "").lower() == "true"

pytestmark = pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="Set RUN_INTEGRATION_TESTS=true to run integration tests",
)

# ---------------------------------------------------------------------------
# Shared constants / helpers
# ---------------------------------------------------------------------------

# A short public YouTube playlist safe to use as a test fixture.
# Override via env var to avoid brittle hard-coded IDs.
_DEFAULT_TEST_PLAYLIST = "https://www.youtube.com/playlist?list=PLbpi6ZahtOH6Ar_3GPy3workd3uG4"
TEST_PLAYLIST_URL = os.environ.get("INTEGRATION_PLAYLIST_URL", _DEFAULT_TEST_PLAYLIST)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def s3_bucket():
    """Return the S3 bucket name, failing early with a clear message if unset."""
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        pytest.fail("S3_BUCKET env var must be set for integration tests")
    return bucket


@pytest.fixture(scope="module")
def cloudfront_base():
    """Return the CloudFront base URL, failing early if unset."""
    base = os.environ.get("CLOUDFRONT_BASE", "")
    if not base:
        pytest.fail("CLOUDFRONT_BASE env var must be set for integration tests")
    return base


@pytest.fixture(scope="module")
def s3_manager(s3_bucket):
    """Return an S3Manager wired to the test bucket + a fixed test playlist ID."""
    from s3_manager import S3Manager

    return S3Manager(bucket=s3_bucket, playlist_id="integration-test-playlist")


# ---------------------------------------------------------------------------
# S3Manager — round-trip smoke tests
# ---------------------------------------------------------------------------


class TestS3ManagerIntegration:
    """Verify real S3 operations work end-to-end."""

    def test_list_existing_episodes_returns_set(self, s3_manager):
        """list_existing_episodes should return a set (may be empty for a fresh bucket)."""
        keys = s3_manager.list_existing_episodes()
        assert isinstance(keys, set)

    def test_upload_and_delete_episode(self, s3_manager, tmp_path):
        """Upload a tiny MP3-like file then delete it; S3 state should round-trip."""
        # Write a minimal placeholder file
        fake_mp3 = tmp_path / "integration_test.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" + b"\x00" * 128)  # minimal MP3 header bytes

        video_id = "integration-smoke-001"

        # Upload
        s3_manager.upload_episode(str(fake_mp3), video_id, max_age_days=1)
        keys_after_upload = s3_manager.list_existing_episodes()
        assert video_id in keys_after_upload, "Episode should appear in S3 after upload"

        # Delete
        s3_manager.delete_episode(video_id)
        keys_after_delete = s3_manager.list_existing_episodes()
        assert video_id not in keys_after_delete, "Episode should be gone after delete"

    def test_upload_feed_and_verify_contents(self, s3_manager):
        """Upload a minimal RSS feed and verify the object exists in S3."""
        sample_rss = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel>'
            "<title>Integration Test</title>"
            "</channel></rss>"
        )
        s3_manager.upload_feed(sample_rss)
        # Verify feed is readable by fetching its size
        size = s3_manager.get_object_size("integration-test-playlist/feed.xml")
        assert size is not None and size > 0

    def test_manifest_round_trip(self, s3_manager):
        """load_manifest / save_manifest should persist and restore data."""
        test_data = {"vid001": {"title": "Hello", "duration": 300}}
        s3_manager.save_manifest(test_data)
        loaded = s3_manager.load_manifest()
        assert loaded == test_data


# ---------------------------------------------------------------------------
# Extractor — real YouTube calls
# ---------------------------------------------------------------------------


class TestExtractorIntegration:
    """Verify yt-dlp-backed extraction against a real YouTube playlist."""

    def test_extract_playlist_returns_entries(self):
        """extract_playlist should return a PlaylistMeta and at least one VideoEntry."""
        from extractor import extract_playlist

        playlist_meta, entries = extract_playlist(TEST_PLAYLIST_URL)
        assert playlist_meta is not None
        assert playlist_meta.title, "PlaylistMeta.title must be non-empty"
        assert len(entries) > 0, "Playlist must have at least one video entry"

    def test_extract_video_metadata_returns_dict(self):
        """extract_video_metadata should return a dict with expected keys for a valid video."""
        from extractor import extract_playlist, extract_video_metadata

        _, entries = extract_playlist(TEST_PLAYLIST_URL)
        first = entries[0]

        meta = extract_video_metadata(first.webpage_url)
        assert meta is not None
        for key in ("upload_date", "description", "title", "duration"):
            assert key in meta, f"Expected key {key!r} in video metadata"


# ---------------------------------------------------------------------------
# RSS Generator — end-to-end with real S3 data
# ---------------------------------------------------------------------------


class TestRssGeneratorIntegration:
    """Verify RSS generation against real S3-listed episodes."""

    def test_generate_rss_from_s3_episodes(self, s3_manager, cloudfront_base):
        """generate_rss should produce parseable XML from real S3 episode keys."""
        from extractor import extract_playlist
        from rss_generator import build_episode_metadata, generate_rss

        playlist_meta, entries = extract_playlist(TEST_PLAYLIST_URL)
        final_keys = s3_manager.list_existing_episodes()

        episodes = build_episode_metadata(entries, final_keys, cloudfront_base, "integration-test-playlist", s3_manager)
        xml_str = generate_rss(playlist_meta, episodes, cloudfront_base, "integration-test-playlist")

        # Must be parseable XML
        root = ET.fromstring(xml_str)
        assert root.tag == "rss"
        channel = root.find("channel")
        assert channel is not None
        assert channel.find("title") is not None


# ---------------------------------------------------------------------------
# Full pipeline smoke test
# ---------------------------------------------------------------------------


class TestProcessPlaylistIntegration:
    """Smoke-test the full process_playlist pipeline in dry-run mode.

    Using dry_run=True avoids any downloads or uploads while still
    exercising the playlist extraction, S3 listing, and candidate
    selection logic against real infrastructure.
    """

    def test_dry_run_returns_expected_keys(self, s3_bucket, cloudfront_base):
        """process_playlist(dry_run=True) should return a dict with all expected keys."""
        from sync import process_playlist

        os.environ["S3_BUCKET"] = s3_bucket
        os.environ["CLOUDFRONT_BASE"] = cloudfront_base

        result = process_playlist(
            TEST_PLAYLIST_URL,
            max_downloads=1,
            max_age_days=365,
            sleep_between=0,
            dry_run=True,
        )

        expected_keys = {
            "playlist_id",
            "new_episodes",
            "skipped_old",
            "failed",
            "total_episodes",
            "elapsed_seconds",
        }
        assert expected_keys.issubset(result.keys()), f"Missing keys in result: {expected_keys - result.keys()}"
        assert result["failed"] == 0, "No episodes should fail in a dry-run"
        assert result["elapsed_seconds"] >= 0

    def test_dry_run_does_not_modify_s3(self, s3_manager, s3_bucket, cloudfront_base):
        """process_playlist(dry_run=True) must leave S3 state unchanged."""
        from sync import process_playlist

        os.environ["S3_BUCKET"] = s3_bucket
        os.environ["CLOUDFRONT_BASE"] = cloudfront_base

        before = s3_manager.list_existing_episodes()

        process_playlist(
            TEST_PLAYLIST_URL,
            max_downloads=3,
            max_age_days=365,
            sleep_between=0,
            dry_run=True,
        )

        after = s3_manager.list_existing_episodes()
        assert before == after, "Dry-run must not modify S3 episode set"
