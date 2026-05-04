"""Unit tests for the sync orchestration module."""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

# A date that is always recent (2 days ago) for tests that expect downloads
_RECENT_DATE = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y%m%d")

import pytest

from models import EpisodeMeta, PlaylistMeta, VideoEntry
from sync import _rebuild_feed, _reconcile, process_playlist


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_playlist_meta(title="Test Playlist"):
    return PlaylistMeta(
        title=title,
        description="A test playlist",
        uploader="Test Channel",
        channel_url="https://youtube.com/@TestChannel",
        webpage_url="https://youtube.com/playlist?list=PLtest",
        playlist_id="PLtest",
    )


def _make_video(video_id="vid001", duration=300, upload_date=None):
    if upload_date is None:
        upload_date = _RECENT_DATE
    return VideoEntry(
        video_id=video_id,
        title=f"Video {video_id}",
        description="",
        duration=duration,
        upload_date=upload_date,
        thumbnail="https://img.youtube.com/vi/vid001/0.jpg",
        webpage_url=f"https://www.youtube.com/watch?v={video_id}",
        playlist_index=1,
    )


def _make_s3_manager(existing=None):
    s3 = MagicMock()
    s3.list_existing_episodes.return_value = set(existing or [])
    s3.upload_episode.return_value = "PLtest/episodes/vid001.mp3"
    s3.upload_feed.return_value = "PLtest/feed.xml"
    s3.get_object_size.return_value = 1024
    return s3


BASE_ENV = {
    "S3_BUCKET": "test-bucket",
    "CLOUDFRONT_BASE": "https://cdn.example.com",
    "MAX_DOWNLOADS_PER_RUN": "10",
    "MAX_AGE_DAYS": "30",
    "SLEEP_BETWEEN_DOWNLOADS": "0",
}


# ---------------------------------------------------------------------------
# process_playlist — configuration / validation
# ---------------------------------------------------------------------------

class TestProcessPlaylistConfig:
    def test_raises_if_s3_bucket_not_set(self):
        with patch.dict(os.environ, {"CLOUDFRONT_BASE": "https://cdn.example.com"}, clear=True):
            with pytest.raises(ValueError, match="S3_BUCKET"):
                process_playlist("https://youtube.com/playlist?list=PLtest")

    def test_raises_if_cloudfront_base_not_set(self):
        with patch.dict(os.environ, {"S3_BUCKET": "bucket"}, clear=True):
            with pytest.raises(ValueError, match="CLOUDFRONT_BASE"):
                process_playlist("https://youtube.com/playlist?list=PLtest")

    def test_reads_max_downloads_from_env(self):
        env = {**BASE_ENV, "MAX_DOWNLOADS_PER_RUN": "2"}
        playlist_meta = _make_playlist_meta()
        video_entries = [_make_video(f"vid{i:03d}") for i in range(5)]

        with patch.dict(os.environ, env, clear=True):
            with patch("sync.S3Manager") as mock_s3_cls, \
                 patch("sync.extract_playlist", return_value=(playlist_meta, video_entries)), \
                 patch("sync.extract_video_metadata", return_value={"upload_date": "20240101", "description": "", "thumbnail": "", "duration": 300, "title": ""}), \
                 patch("sync.download_and_convert") as mock_dl, \
                 patch("sync.build_episode_metadata", return_value=[]), \
                 patch("sync.generate_rss", return_value="<rss/>"), \
                 patch("sync.shutil.rmtree"):
                s3 = _make_s3_manager()
                mock_s3_cls.return_value = s3

                def fake_dl(url, vid, tmp):
                    path = f"/tmp/PLtest/{vid}.mp3"
                    return path

                mock_dl.side_effect = fake_dl

                with patch("os.makedirs"), patch("os.remove"):
                    result = process_playlist("https://youtube.com/playlist?list=PLtest")

                # Should download at most 2
                assert mock_dl.call_count <= 2


# ---------------------------------------------------------------------------
# process_playlist — happy path
# ---------------------------------------------------------------------------

class TestProcessPlaylistHappyPath:
    def _run(self, video_entries, existing=None, meta_override=None, env_override=None):
        playlist_meta = _make_playlist_meta()
        env = {**(env_override or BASE_ENV)}
        meta = meta_override or {
            "upload_date": _RECENT_DATE,
            "description": "A description",
            "thumbnail": "https://img.youtube.com/vi/vid001/0.jpg",
            "duration": 300,
            "title": "Video Title",
        }

        with patch.dict(os.environ, env, clear=True):
            with patch("sync.S3Manager") as mock_s3_cls, \
                 patch("sync.extract_playlist", return_value=(playlist_meta, video_entries)), \
                 patch("sync.extract_video_metadata", return_value=meta), \
                 patch("sync.download_and_convert") as mock_dl, \
                 patch("sync.build_episode_metadata", return_value=[]), \
                 patch("sync.generate_rss", return_value="<rss/>"), \
                 patch("sync.shutil.rmtree"), \
                 patch("os.makedirs"), \
                 patch("os.remove"):

                s3 = _make_s3_manager(existing=existing)
                mock_s3_cls.return_value = s3

                def fake_dl(url, vid, tmp):
                    return f"/tmp/PLtest/{vid}.mp3"

                mock_dl.side_effect = fake_dl
                result = process_playlist("https://youtube.com/playlist?list=PLtest")
                return result, mock_dl, s3

    def test_returns_correct_keys(self):
        videos = [_make_video("vid001")]
        result, _, _ = self._run(videos)
        assert set(result.keys()) == {
            "playlist_id", "new_episodes", "skipped_old", "failed", "total_episodes"
        }

    def test_downloads_new_episode(self):
        videos = [_make_video("vid001")]
        result, mock_dl, _ = self._run(videos)
        assert mock_dl.call_count == 1
        assert result["new_episodes"] == 1

    def test_skips_already_existing_episode(self):
        videos = [_make_video("vid001")]
        result, mock_dl, _ = self._run(videos, existing=["vid001"])
        assert mock_dl.call_count == 0
        assert result["new_episodes"] == 0

    def test_skips_video_with_no_duration(self):
        videos = [_make_video("vid001", duration=None)]
        result, mock_dl, _ = self._run(videos)
        assert mock_dl.call_count == 0

    def test_skips_video_with_zero_duration(self):
        videos = [_make_video("vid001", duration=0)]
        result, mock_dl, _ = self._run(videos)
        assert mock_dl.call_count == 0

    def test_empty_playlist_returns_zero_new(self):
        result, mock_dl, _ = self._run([])
        assert result["new_episodes"] == 0
        assert mock_dl.call_count == 0

    def test_playlist_id_returned_correctly(self):
        videos = [_make_video("vid001")]
        result, _, _ = self._run(videos)
        assert result["playlist_id"] == "PLtest"

    def test_failed_download_counted(self):
        videos = [_make_video("vid001")]
        playlist_meta = _make_playlist_meta()

        with patch.dict(os.environ, BASE_ENV, clear=True):
            with patch("sync.S3Manager") as mock_s3_cls, \
                 patch("sync.extract_playlist", return_value=(playlist_meta, videos)), \
                 patch("sync.extract_video_metadata", return_value={"upload_date": _RECENT_DATE, "description": "", "thumbnail": "", "duration": 300, "title": ""}), \
                 patch("sync.download_and_convert", side_effect=Exception("Network error")), \
                 patch("sync.build_episode_metadata", return_value=[]), \
                 patch("sync.generate_rss", return_value="<rss/>"), \
                 patch("sync.shutil.rmtree"), \
                 patch("os.makedirs"), \
                 patch("os.remove"):
                s3 = _make_s3_manager()
                mock_s3_cls.return_value = s3
                result = process_playlist("https://youtube.com/playlist?list=PLtest")
                assert result["failed"] == 1
                assert result["new_episodes"] == 0

    def test_multiple_videos_all_downloaded(self):
        videos = [_make_video(f"vid{i:03d}") for i in range(3)]
        result, mock_dl, _ = self._run(videos)
        assert mock_dl.call_count == 3
        assert result["new_episodes"] == 3


# ---------------------------------------------------------------------------
# process_playlist — age filtering
# ---------------------------------------------------------------------------

class TestProcessPlaylistAgeFiltering:
    def test_skips_old_episode(self):
        """Episodes older than max_age_days should be skipped."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y%m%d")
        videos = [_make_video("vid001", upload_date=old_date)]
        playlist_meta = _make_playlist_meta()
        env = {**BASE_ENV, "MAX_AGE_DAYS": "30"}

        with patch.dict(os.environ, env, clear=True):
            with patch("sync.S3Manager") as mock_s3_cls, \
                 patch("sync.extract_playlist", return_value=(playlist_meta, videos)), \
                 patch("sync.extract_video_metadata", return_value={
                     "upload_date": old_date,
                     "description": "",
                     "thumbnail": "",
                     "duration": 300,
                     "title": "Old Video",
                 }), \
                 patch("sync.download_and_convert") as mock_dl, \
                 patch("sync.build_episode_metadata", return_value=[]), \
                 patch("sync.generate_rss", return_value="<rss/>"), \
                 patch("sync.shutil.rmtree"), \
                 patch("os.makedirs"), \
                 patch("os.remove"):
                s3 = _make_s3_manager()
                mock_s3_cls.return_value = s3
                result = process_playlist("https://youtube.com/playlist?list=PLtest")
                assert mock_dl.call_count == 0
                assert result["skipped_old"] == 1

    def test_downloads_recent_episode(self):
        """Episodes within max_age_days should be downloaded."""
        recent_date = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%d")
        videos = [_make_video("vid001", upload_date=recent_date)]
        playlist_meta = _make_playlist_meta()
        env = {**BASE_ENV, "MAX_AGE_DAYS": "30"}

        with patch.dict(os.environ, env, clear=True):
            with patch("sync.S3Manager") as mock_s3_cls, \
                 patch("sync.extract_playlist", return_value=(playlist_meta, videos)), \
                 patch("sync.extract_video_metadata", return_value={
                     "upload_date": recent_date,
                     "description": "",
                     "thumbnail": "",
                     "duration": 300,
                     "title": "Recent Video",
                 }), \
                 patch("sync.download_and_convert") as mock_dl, \
                 patch("sync.build_episode_metadata", return_value=[]), \
                 patch("sync.generate_rss", return_value="<rss/>"), \
                 patch("sync.shutil.rmtree"), \
                 patch("os.makedirs"), \
                 patch("os.remove"):
                s3 = _make_s3_manager()
                mock_s3_cls.return_value = s3

                def fake_dl(url, vid, tmp):
                    return f"/tmp/PLtest/{vid}.mp3"

                mock_dl.side_effect = fake_dl
                result = process_playlist("https://youtube.com/playlist?list=PLtest")
                assert mock_dl.call_count == 1
                assert result["new_episodes"] == 1

    def test_per_podcast_max_age_override(self):
        """max_age_days kwarg overrides the env var."""
        # Video is 15 days old — within env MAX_AGE_DAYS=30 but outside override=10
        old_date = (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y%m%d")
        videos = [_make_video("vid001", upload_date=old_date)]
        playlist_meta = _make_playlist_meta()
        env = {**BASE_ENV, "MAX_AGE_DAYS": "30"}

        with patch.dict(os.environ, env, clear=True):
            with patch("sync.S3Manager") as mock_s3_cls, \
                 patch("sync.extract_playlist", return_value=(playlist_meta, videos)), \
                 patch("sync.extract_video_metadata", return_value={
                     "upload_date": old_date,
                     "description": "",
                     "thumbnail": "",
                     "duration": 300,
                     "title": "Mid-age video",
                 }), \
                 patch("sync.download_and_convert") as mock_dl, \
                 patch("sync.build_episode_metadata", return_value=[]), \
                 patch("sync.generate_rss", return_value="<rss/>"), \
                 patch("sync.shutil.rmtree"), \
                 patch("os.makedirs"), \
                 patch("os.remove"):
                s3 = _make_s3_manager()
                mock_s3_cls.return_value = s3
                result = process_playlist(
                    "https://youtube.com/playlist?list=PLtest",
                    max_age_days=10,
                )
                assert mock_dl.call_count == 0
                assert result["skipped_old"] == 1


# ---------------------------------------------------------------------------
# process_playlist — sleep between downloads
# ---------------------------------------------------------------------------

class TestProcessPlaylistSleep:
    def test_sleeps_between_downloads(self):
        videos = [_make_video(f"vid{i:03d}") for i in range(2)]
        playlist_meta = _make_playlist_meta()
        env = {**BASE_ENV, "SLEEP_BETWEEN_DOWNLOADS": "1"}

        with patch.dict(os.environ, env, clear=True):
            with patch("sync.S3Manager") as mock_s3_cls, \
                 patch("sync.extract_playlist", return_value=(playlist_meta, videos)), \
                 patch("sync.extract_video_metadata", return_value={"upload_date": _RECENT_DATE, "description": "", "thumbnail": "", "duration": 300, "title": ""}), \
                 patch("sync.download_and_convert") as mock_dl, \
                 patch("sync.build_episode_metadata", return_value=[]), \
                 patch("sync.generate_rss", return_value="<rss/>"), \
                 patch("sync.time.sleep") as mock_sleep, \
                 patch("sync.shutil.rmtree"), \
                 patch("os.makedirs"), \
                 patch("os.remove"):
                s3 = _make_s3_manager()
                mock_s3_cls.return_value = s3

                def fake_dl(url, vid, tmp):
                    return f"/tmp/PLtest/{vid}.mp3"

                mock_dl.side_effect = fake_dl
                process_playlist("https://youtube.com/playlist?list=PLtest")
                # Sleep should be called once (between 2 downloads, not after last)
                mock_sleep.assert_called_once_with(1)

    def test_no_sleep_when_zero(self):
        videos = [_make_video(f"vid{i:03d}") for i in range(2)]
        playlist_meta = _make_playlist_meta()
        env = {**BASE_ENV, "SLEEP_BETWEEN_DOWNLOADS": "0"}

        with patch.dict(os.environ, env, clear=True):
            with patch("sync.S3Manager") as mock_s3_cls, \
                 patch("sync.extract_playlist", return_value=(playlist_meta, videos)), \
                 patch("sync.extract_video_metadata", return_value={"upload_date": _RECENT_DATE, "description": "", "thumbnail": "", "duration": 300, "title": ""}), \
                 patch("sync.download_and_convert") as mock_dl, \
                 patch("sync.build_episode_metadata", return_value=[]), \
                 patch("sync.generate_rss", return_value="<rss/>"), \
                 patch("sync.time.sleep") as mock_sleep, \
                 patch("sync.shutil.rmtree"), \
                 patch("os.makedirs"), \
                 patch("os.remove"):
                s3 = _make_s3_manager()
                mock_s3_cls.return_value = s3

                def fake_dl(url, vid, tmp):
                    return f"/tmp/PLtest/{vid}.mp3"

                mock_dl.side_effect = fake_dl
                process_playlist("https://youtube.com/playlist?list=PLtest")
                mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# _rebuild_feed
# ---------------------------------------------------------------------------

class TestRebuildFeed:
    def test_lists_s3_builds_and_uploads_feed(self):
        s3 = _make_s3_manager(existing=["vid001", "vid002"])
        videos = [_make_video("vid001"), _make_video("vid002")]
        playlist_meta = _make_playlist_meta()

        with patch("sync.build_episode_metadata", return_value=[]) as mock_build, \
             patch("sync.generate_rss", return_value="<rss/>") as mock_gen:
            count = _rebuild_feed(s3, videos, "https://cdn.example.com", "PLtest", playlist_meta)
            mock_build.assert_called_once()
            mock_gen.assert_called_once()
            s3.upload_feed.assert_called_once_with("<rss/>")
            assert count == 0  # no episodes in mocked return

    def test_returns_episode_count(self):
        s3 = _make_s3_manager(existing=["vid001"])
        videos = [_make_video("vid001")]
        playlist_meta = _make_playlist_meta()
        fake_episode = MagicMock()

        with patch("sync.build_episode_metadata", return_value=[fake_episode, fake_episode]), \
             patch("sync.generate_rss", return_value="<rss/>"):
            count = _rebuild_feed(s3, videos, "https://cdn.example.com", "PLtest", playlist_meta)
            assert count == 2


# ---------------------------------------------------------------------------
# _reconcile
# ---------------------------------------------------------------------------

class TestReconcile:
    def test_deletes_old_episodes_from_s3(self):
        """Episodes older than max_age_days should be deleted from S3."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y%m%d")
        video = _make_video("vid001", upload_date=old_date)
        s3 = _make_s3_manager(existing=["vid001"])
        # Second call (after deletion) returns empty
        s3.list_existing_episodes.side_effect = [{"vid001"}, set(), set()]

        with patch("sync.build_episode_metadata", return_value=[]), \
             patch("sync.generate_rss", return_value="<rss/>"):
            _reconcile(s3, [video], "https://cdn.example.com", "PLtest",
                       _make_playlist_meta(), max_age_days=30)
            s3.delete_episode.assert_called_with("vid001")

    def test_deletes_orphaned_s3_files(self):
        """S3 files not in the playlist anymore should be removed."""
        # S3 has vid_orphan, playlist only has vid001
        s3 = _make_s3_manager(existing=["vid_orphan"])
        s3.list_existing_episodes.side_effect = [
            {"vid_orphan"},   # initial list
            {"vid_orphan"},   # after age-deletion (nothing deleted)
            set(),            # after orphan deletion
        ]
        video = _make_video("vid001")

        with patch("sync.build_episode_metadata", return_value=[]), \
             patch("sync.generate_rss", return_value="<rss/>"):
            _reconcile(s3, [video], "https://cdn.example.com", "PLtest",
                       _make_playlist_meta(), max_age_days=30)
            s3.delete_episode.assert_called_with("vid_orphan")

    def test_rebuilds_feed_after_reconcile(self):
        s3 = _make_s3_manager(existing=[])
        s3.list_existing_episodes.return_value = set()

        with patch("sync.build_episode_metadata", return_value=[]) as mock_build, \
             patch("sync.generate_rss", return_value="<rss/>"):
            _reconcile(s3, [], "https://cdn.example.com", "PLtest",
                       _make_playlist_meta(), max_age_days=30)
            mock_build.assert_called_once()
            s3.upload_feed.assert_called_once()

    def test_handles_delete_exception_gracefully(self):
        """Deletion failures should not crash reconcile."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y%m%d")
        video = _make_video("vid001", upload_date=old_date)
        s3 = _make_s3_manager(existing=["vid001"])
        s3.delete_episode.side_effect = Exception("S3 error")
        s3.list_existing_episodes.side_effect = [{"vid001"}, {"vid001"}, {"vid001"}]

        with patch("sync.build_episode_metadata", return_value=[]), \
             patch("sync.generate_rss", return_value="<rss/>"):
            # Should not raise
            _reconcile(s3, [video], "https://cdn.example.com", "PLtest",
                       _make_playlist_meta(), max_age_days=30)

    def test_no_deletions_when_all_recent(self):
        """Recent episodes should not be deleted during reconcile."""
        recent_date = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%d")
        video = _make_video("vid001", upload_date=recent_date)
        s3 = _make_s3_manager(existing=["vid001"])
        s3.list_existing_episodes.return_value = {"vid001"}

        with patch("sync.build_episode_metadata", return_value=[]), \
             patch("sync.generate_rss", return_value="<rss/>"):
            _reconcile(s3, [video], "https://cdn.example.com", "PLtest",
                       _make_playlist_meta(), max_age_days=30)
            s3.delete_episode.assert_not_called()
