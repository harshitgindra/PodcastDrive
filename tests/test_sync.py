"""Unit tests for the sync orchestration module."""

import logging
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# Imported at module scope on purpose: an installed ``ytdlp_plugins.extractor``
# package shadows this project's ``extractor`` module once yt-dlp has loaded its
# plugins, so a late in-function import can resolve to the wrong module.
from extractor import BotDetectedError, ExtractionError
from models import PlaylistMeta, VideoEntry
from sync import _rebuild_feed, _reconcile, process_playlist

# A date that is always recent (2 days ago) for tests that expect downloads
_RECENT_DATE = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y%m%d")


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
            with (
                patch("sync.S3Manager") as mock_s3_cls,
                patch("sync.extract_playlist", return_value=(playlist_meta, video_entries)),
                patch(
                    "sync.extract_video_metadata",
                    return_value={
                        "upload_date": "20240101",
                        "description": "",
                        "thumbnail": "",
                        "duration": 300,
                        "title": "",
                    },
                ),
                patch("sync.download_and_convert") as mock_dl,
                patch("sync.build_episode_metadata", return_value=[]),
                patch("sync.generate_rss", return_value="<rss/>"),
                patch("sync.shutil.rmtree"),
            ):
                s3 = _make_s3_manager()
                mock_s3_cls.return_value = s3

                def fake_dl(url, vid, tmp):
                    path = f"/tmp/PLtest/{vid}.mp3"
                    return path

                mock_dl.side_effect = fake_dl

                with patch("os.makedirs"), patch("os.remove"):
                    process_playlist("https://youtube.com/playlist?list=PLtest")

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

        with (
            patch.dict(os.environ, env, clear=True),
            patch("sync.S3Manager") as mock_s3_cls,
            patch("sync.extract_playlist", return_value=(playlist_meta, video_entries)),
            patch("sync.extract_video_metadata", return_value=meta),
            patch("sync.download_and_convert") as mock_dl,
            patch("sync.build_episode_metadata", return_value=[]),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.shutil.rmtree"),
            patch("os.makedirs"),
            patch("os.remove"),
        ):
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
            "playlist_id",
            "new_episodes",
            "skipped_old",
            "unavailable",
            "failed",
            "bot_detected",
            "total_episodes",
            "elapsed_seconds",
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
            with (
                patch("sync.S3Manager") as mock_s3_cls,
                patch("sync.extract_playlist", return_value=(playlist_meta, videos)),
                patch(
                    "sync.extract_video_metadata",
                    return_value={
                        "upload_date": _RECENT_DATE,
                        "description": "",
                        "thumbnail": "",
                        "duration": 300,
                        "title": "",
                    },
                ),
                patch("sync.download_and_convert", side_effect=Exception("Network error")),
                patch("sync.build_episode_metadata", return_value=[]),
                patch("sync.generate_rss", return_value="<rss/>"),
                patch("sync.shutil.rmtree"),
                patch("os.makedirs"),
                patch("os.remove"),
            ):
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
        old_date = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y%m%d")
        videos = [_make_video("vid001", upload_date=old_date)]
        playlist_meta = _make_playlist_meta()
        env = {**BASE_ENV, "MAX_AGE_DAYS": "30"}

        with (
            patch.dict(os.environ, env, clear=True),
            patch("sync.S3Manager") as mock_s3_cls,
            patch("sync.extract_playlist", return_value=(playlist_meta, videos)),
            patch(
                "sync.extract_video_metadata",
                return_value={
                    "upload_date": old_date,
                    "description": "",
                    "thumbnail": "",
                    "duration": 300,
                    "title": "Old Video",
                },
            ),
            patch("sync.download_and_convert") as mock_dl,
            patch("sync.build_episode_metadata", return_value=[]),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.shutil.rmtree"),
            patch("os.makedirs"),
            patch("os.remove"),
        ):
            s3 = _make_s3_manager()
            mock_s3_cls.return_value = s3
            result = process_playlist("https://youtube.com/playlist?list=PLtest")
            assert mock_dl.call_count == 0
            assert result["skipped_old"] == 1

    def test_downloads_recent_episode(self):
        """Episodes within max_age_days should be downloaded."""
        recent_date = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y%m%d")
        videos = [_make_video("vid001", upload_date=recent_date)]
        playlist_meta = _make_playlist_meta()
        env = {**BASE_ENV, "MAX_AGE_DAYS": "30"}

        with (
            patch.dict(os.environ, env, clear=True),
            patch("sync.S3Manager") as mock_s3_cls,
            patch("sync.extract_playlist", return_value=(playlist_meta, videos)),
            patch(
                "sync.extract_video_metadata",
                return_value={
                    "upload_date": recent_date,
                    "description": "",
                    "thumbnail": "",
                    "duration": 300,
                    "title": "Recent Video",
                },
            ),
            patch("sync.download_and_convert") as mock_dl,
            patch("sync.build_episode_metadata", return_value=[]),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.shutil.rmtree"),
            patch("os.makedirs"),
            patch("os.remove"),
        ):
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
        old_date = (datetime.now(UTC) - timedelta(days=15)).strftime("%Y%m%d")
        videos = [_make_video("vid001", upload_date=old_date)]
        playlist_meta = _make_playlist_meta()
        env = {**BASE_ENV, "MAX_AGE_DAYS": "30"}

        with (
            patch.dict(os.environ, env, clear=True),
            patch("sync.S3Manager") as mock_s3_cls,
            patch("sync.extract_playlist", return_value=(playlist_meta, videos)),
            patch(
                "sync.extract_video_metadata",
                return_value={
                    "upload_date": old_date,
                    "description": "",
                    "thumbnail": "",
                    "duration": 300,
                    "title": "Mid-age video",
                },
            ),
            patch("sync.download_and_convert") as mock_dl,
            patch("sync.build_episode_metadata", return_value=[]),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.shutil.rmtree"),
            patch("os.makedirs"),
            patch("os.remove"),
        ):
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
    def test_raises_on_negative_sleep_between(self):
        with patch.dict(os.environ, BASE_ENV, clear=True):
            with pytest.raises(ValueError, match="sleep_between must be >= 0"):
                process_playlist(
                    "https://youtube.com/playlist?list=PLtest",
                    sleep_between=-1,
                )

    def test_sleeps_between_downloads(self):
        videos = [_make_video(f"vid{i:03d}") for i in range(2)]
        playlist_meta = _make_playlist_meta()
        env = {**BASE_ENV, "SLEEP_BETWEEN_DOWNLOADS": "1"}

        with patch.dict(os.environ, env, clear=True):
            with (
                patch("sync.S3Manager") as mock_s3_cls,
                patch("sync.extract_playlist", return_value=(playlist_meta, videos)),
                patch(
                    "sync.extract_video_metadata",
                    return_value={
                        "upload_date": _RECENT_DATE,
                        "description": "",
                        "thumbnail": "",
                        "duration": 300,
                        "title": "",
                    },
                ),
                patch("sync.download_and_convert") as mock_dl,
                patch("sync.remove_ads", side_effect=lambda p, *a, **kw: (p, [], "")),
                patch("sync.build_episode_metadata", return_value=[]),
                patch("sync.generate_rss", return_value="<rss/>"),
                patch("sync.time.sleep") as mock_sleep,
                patch("sync.shutil.rmtree"),
                patch("os.makedirs"),
                patch("os.remove"),
            ):
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
            with (
                patch("sync.S3Manager") as mock_s3_cls,
                patch("sync.extract_playlist", return_value=(playlist_meta, videos)),
                patch(
                    "sync.extract_video_metadata",
                    return_value={
                        "upload_date": _RECENT_DATE,
                        "description": "",
                        "thumbnail": "",
                        "duration": 300,
                        "title": "",
                    },
                ),
                patch("sync.download_and_convert") as mock_dl,
                patch("sync.remove_ads", side_effect=lambda p, *a, **kw: (p, [], "")),
                patch("sync.build_episode_metadata", return_value=[]),
                patch("sync.generate_rss", return_value="<rss/>"),
                patch("sync.time.sleep") as mock_sleep,
                patch("sync.shutil.rmtree"),
                patch("os.makedirs"),
                patch("os.remove"),
            ):
                s3 = _make_s3_manager()
                mock_s3_cls.return_value = s3

                def fake_dl(url, vid, tmp):
                    return f"/tmp/PLtest/{vid}.mp3"

                mock_dl.side_effect = fake_dl
                process_playlist("https://youtube.com/playlist?list=PLtest")
                mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# process_playlist — dry-run mode
# ---------------------------------------------------------------------------


class TestProcessPlaylistDryRun:
    def _run_dry(self, video_entries, existing=None, meta_override=None):
        playlist_meta = _make_playlist_meta()
        meta = meta_override or {
            "upload_date": _RECENT_DATE,
            "description": "desc",
            "thumbnail": "",
            "duration": 300,
            "title": "Video Title",
        }

        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch("sync.S3Manager") as mock_s3_cls,
            patch("sync.extract_playlist", return_value=(playlist_meta, video_entries)),
            patch("sync.extract_video_metadata", return_value=meta),
            patch("sync.download_and_convert") as mock_dl,
            patch("sync.build_episode_metadata", return_value=[]),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.shutil.rmtree"),
            patch("os.makedirs"),
            patch("os.remove"),
        ):
            s3 = _make_s3_manager(existing=existing)
            mock_s3_cls.return_value = s3
            result = process_playlist(
                "https://youtube.com/playlist?list=PLtest",
                dry_run=True,
            )
            return result, mock_dl, s3

    def test_dry_run_does_not_call_download(self):
        videos = [_make_video("vid001")]
        result, mock_dl, s3 = self._run_dry(videos)
        mock_dl.assert_not_called()

    def test_dry_run_does_not_upload_to_s3(self):
        videos = [_make_video("vid001")]
        result, mock_dl, s3 = self._run_dry(videos)
        s3.upload_episode.assert_not_called()

    def test_dry_run_does_not_call_reconcile_methods(self):
        videos = [_make_video("vid001")]
        result, mock_dl, s3 = self._run_dry(videos)
        s3.delete_episode.assert_not_called()
        s3.upload_feed.assert_not_called()

    def test_dry_run_counts_would_be_new(self):
        """new_episodes should reflect planned downloads, not actual ones."""
        videos = [_make_video("vid001"), _make_video("vid002")]
        result, _, _ = self._run_dry(videos)
        assert result["new_episodes"] == 2

    def test_dry_run_empty_playlist(self):
        result, mock_dl, _ = self._run_dry([])
        assert result["new_episodes"] == 0
        mock_dl.assert_not_called()


# ---------------------------------------------------------------------------
# _rebuild_feed
# ---------------------------------------------------------------------------


class TestRebuildFeed:
    def test_lists_s3_builds_and_uploads_feed(self):
        s3 = _make_s3_manager(existing=["vid001", "vid002"])
        videos = [_make_video("vid001"), _make_video("vid002")]
        playlist_meta = _make_playlist_meta()

        with (
            patch("sync.build_episode_metadata", return_value=[]) as mock_build,
            patch("sync.generate_rss", return_value="<rss/>") as mock_gen,
        ):
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

        with (
            patch("sync.build_episode_metadata", return_value=[fake_episode, fake_episode]),
            patch("sync.generate_rss", return_value="<rss/>"),
        ):
            count = _rebuild_feed(s3, videos, "https://cdn.example.com", "PLtest", playlist_meta)
            assert count == 2


# ---------------------------------------------------------------------------
# _reconcile
# ---------------------------------------------------------------------------


class TestReconcile:
    def test_does_not_delete_old_episodes_by_age(self):
        """Age-based deletion is now handled by S3 lifecycle — reconcile must not delete by age."""
        old_date = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y%m%d")
        video = _make_video("vid001", upload_date=old_date)
        s3 = _make_s3_manager(existing=["vid001"])
        s3.list_existing_episodes.return_value = {"vid001"}

        with patch("sync.build_episode_metadata", return_value=[]), patch("sync.generate_rss", return_value="<rss/>"):
            _reconcile(s3, [video], "https://cdn.example.com", "PLtest", _make_playlist_meta())
            # vid001 is still in the playlist — must NOT be deleted even if old
            s3.delete_episode.assert_not_called()

    def test_deletes_orphaned_s3_files(self):
        """S3 files not in the playlist anymore should be removed."""
        # S3 has vid_orphan, playlist only has vid001
        s3 = _make_s3_manager(existing=["vid_orphan"])
        s3.list_existing_episodes.side_effect = [
            {"vid_orphan"},  # initial list
            {"vid_orphan"},  # orphan check
            set(),  # after orphan deletion
        ]
        video = _make_video("vid001")

        with patch("sync.build_episode_metadata", return_value=[]), patch("sync.generate_rss", return_value="<rss/>"):
            _reconcile(s3, [video], "https://cdn.example.com", "PLtest", _make_playlist_meta())
            s3.delete_episode.assert_called_with("vid_orphan")

    def test_rebuilds_feed_after_reconcile(self):
        s3 = _make_s3_manager(existing=[])
        s3.list_existing_episodes.return_value = set()

        with (
            patch("sync.build_episode_metadata", return_value=[]) as mock_build,
            patch("sync.generate_rss", return_value="<rss/>"),
        ):
            _reconcile(s3, [], "https://cdn.example.com", "PLtest", _make_playlist_meta())
            mock_build.assert_called_once()
            s3.upload_feed.assert_called_once()

    def test_handles_delete_exception_gracefully(self):
        """Deletion failures (orphan) should not crash reconcile."""
        # vid_orphan is in S3 but not in playlist — orphan delete fails
        s3 = _make_s3_manager(existing=["vid_orphan"])
        s3.delete_episode.side_effect = Exception("S3 error")
        s3.list_existing_episodes.side_effect = [
            {"vid_orphan"},  # initial list
            {"vid_orphan"},  # orphan check
            {"vid_orphan"},  # feed rebuild
        ]
        video = _make_video("vid001")  # different video — vid_orphan is orphan

        with patch("sync.build_episode_metadata", return_value=[]), patch("sync.generate_rss", return_value="<rss/>"):
            # Should not raise
            _reconcile(s3, [video], "https://cdn.example.com", "PLtest", _make_playlist_meta())

    def test_no_deletions_when_no_orphans(self):
        """No deletions should happen when all S3 files are still in the playlist."""
        recent_date = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y%m%d")
        video = _make_video("vid001", upload_date=recent_date)
        s3 = _make_s3_manager(existing=["vid001"])
        s3.list_existing_episodes.return_value = {"vid001"}

        with patch("sync.build_episode_metadata", return_value=[]), patch("sync.generate_rss", return_value="<rss/>"):
            _reconcile(s3, [video], "https://cdn.example.com", "PLtest", _make_playlist_meta())
            s3.delete_episode.assert_not_called()


# ---------------------------------------------------------------------------
# process_playlist — live_status filtering (Step 3 & Step 4)
# ---------------------------------------------------------------------------


class TestLiveStatusFiltering:
    """Tests for upcoming/live video filtering and unavailable video handling."""

    def _base_env(self):
        return {
            "S3_BUCKET": "test-bucket",
            "CLOUDFRONT_BASE": "https://cdn.example.com",
            "MAX_DOWNLOADS_PER_RUN": "10",
            "MAX_AGE_DAYS": "30",
            "SLEEP_BETWEEN_DOWNLOADS": "0",
        }

    def _make_video_with_live_status(self, video_id="vid001", live_status=None):
        return VideoEntry(
            video_id=video_id,
            title=f"Video {video_id}",
            description="",
            duration=300,
            upload_date=_RECENT_DATE,
            thumbnail="https://img.youtube.com/vi/vid001/0.jpg",
            webpage_url=f"https://www.youtube.com/watch?v={video_id}",
            playlist_index=1,
            live_status=live_status,
        )

    def test_skips_is_upcoming_in_step3(self):
        """Video with live_status='is_upcoming' should be filtered in Step 3 (no metadata call)."""
        upcoming = self._make_video_with_live_status("vid001", live_status="is_upcoming")

        with (
            patch.dict("os.environ", self._base_env()),
            patch("sync.extract_playlist", return_value=(_make_playlist_meta(), [upcoming])),
            patch("sync.extract_video_metadata") as mock_meta,
            patch("sync.S3Manager", return_value=_make_s3_manager()),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.build_episode_metadata", return_value=[]),
        ):
            process_playlist("https://youtube.com/playlist?list=PLtest")
            # If filtered in Step 3, extract_video_metadata should NOT be called for this video
            mock_meta.assert_not_called()

    def test_skips_is_live_in_step3(self):
        """Video with live_status='is_live' should be filtered in Step 3 (no metadata call)."""
        live = self._make_video_with_live_status("vid002", live_status="is_live")

        with (
            patch.dict("os.environ", self._base_env()),
            patch("sync.extract_playlist", return_value=(_make_playlist_meta(), [live])),
            patch("sync.extract_video_metadata") as mock_meta,
            patch("sync.S3Manager", return_value=_make_s3_manager()),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.build_episode_metadata", return_value=[]),
        ):
            process_playlist("https://youtube.com/playlist?list=PLtest")
            mock_meta.assert_not_called()

    def test_skips_is_upcoming_in_step4_metadata(self):
        """Video passes Step 3 (no live_status) but metadata returns is_upcoming → skipped."""
        video = _make_video("vid003")  # no live_status set — passes Step 3

        meta = {
            "id": "vid003",
            "title": "Video vid003",
            "description": "",
            "duration": 300,
            "upload_date": _RECENT_DATE,
            "thumbnail": "https://img.youtube.com/vi/vid003/0.jpg",
            "webpage_url": "https://www.youtube.com/watch?v=vid003",
            "live_status": "is_upcoming",
        }

        s3 = _make_s3_manager()

        with (
            patch.dict("os.environ", self._base_env()),
            patch("sync.extract_playlist", return_value=(_make_playlist_meta(), [video])),
            patch("sync.extract_video_metadata", return_value=meta),
            patch("sync.S3Manager", return_value=s3),
            patch("sync.download_and_convert") as mock_dl,
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.build_episode_metadata", return_value=[]),
        ):
            process_playlist("https://youtube.com/playlist?list=PLtest")
            # Download should NOT be called since video is still upcoming
            mock_dl.assert_not_called()

    def test_skips_none_metadata_counts_unavailable(self):
        """extract_video_metadata returning None → video silently skipped, no download attempted."""
        video = _make_video("vid004")  # passes Step 3

        s3 = _make_s3_manager()

        with (
            patch.dict("os.environ", self._base_env()),
            patch("sync.extract_playlist", return_value=(_make_playlist_meta(), [video])),
            patch("sync.extract_video_metadata", return_value=None),
            patch("sync.S3Manager", return_value=s3),
            patch("sync.download_and_convert") as mock_dl,
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.build_episode_metadata", return_value=[]),
        ):
            # Should not raise
            process_playlist("https://youtube.com/playlist?list=PLtest")
            # No download attempted for unavailable video
            mock_dl.assert_not_called()


# ---------------------------------------------------------------------------
# process_playlist — episode_title/duration_secs forwarded to remove_ads
# ---------------------------------------------------------------------------


class TestProcessPlaylistSummaryIntegration:
    """Verify remove_ads is called with episode metadata and summary is persisted."""

    def _base_env(self):
        return dict(BASE_ENV)

    def test_remove_ads_called_with_episode_title_and_duration(self):
        """remove_ads receives episode_title and duration_secs from the video entry."""
        video = _make_video("vid001", duration=900)
        playlist_meta = _make_playlist_meta()
        meta = {
            "upload_date": _RECENT_DATE,
            "description": "desc",
            "thumbnail": "",
            "duration": 900,
            "title": "My Episode Title",
        }

        captured_kwargs: dict = {}

        def fake_remove_ads(mp3, vid, tmp, **kwargs):
            captured_kwargs.update(kwargs)
            return (mp3, [], "")

        with (
            patch.dict(os.environ, self._base_env(), clear=True),
            patch("sync.S3Manager") as mock_s3_cls,
            patch("sync.extract_playlist", return_value=(playlist_meta, [video])),
            patch("sync.extract_video_metadata", return_value=meta),
            patch("sync.download_and_convert", return_value="/tmp/vid001.mp3"),
            patch("sync.remove_ads", side_effect=fake_remove_ads),
            patch("sync.build_episode_metadata", return_value=[]),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.shutil.rmtree"),
            patch("os.makedirs"),
            patch("os.remove"),
        ):
            s3 = _make_s3_manager()
            mock_s3_cls.return_value = s3
            process_playlist("https://youtube.com/playlist?list=PLtest")

        assert captured_kwargs.get("episode_title") == "My Episode Title"
        assert captured_kwargs.get("duration_secs") == 900

    def test_summary_saved_to_manifest_when_returned(self):
        """When remove_ads returns a non-empty summary, it is stored in the manifest."""
        video = _make_video("vid001", duration=600)
        playlist_meta = _make_playlist_meta()
        meta = {
            "upload_date": _RECENT_DATE,
            "description": "desc",
            "thumbnail": "",
            "duration": 600,
            "title": "Summarised Episode",
        }

        with (
            patch.dict(os.environ, self._base_env(), clear=True),
            patch("sync.S3Manager") as mock_s3_cls,
            patch("sync.extract_playlist", return_value=(playlist_meta, [video])),
            patch("sync.extract_video_metadata", return_value=meta),
            patch("sync.download_and_convert", return_value="/tmp/vid001.mp3"),
            patch("sync.remove_ads", return_value=("/tmp/vid001.mp3", [], "Great episode summary")),
            patch("sync.build_episode_metadata", return_value=[]),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.shutil.rmtree"),
            patch("os.makedirs"),
            patch("os.remove"),
        ):
            s3 = _make_s3_manager()
            s3.load_manifest.return_value = {}  # real dict so setdefault() works
            mock_s3_cls.return_value = s3
            process_playlist("https://youtube.com/playlist?list=PLtest")

        # Manifest saved with summary
        s3.save_manifest.assert_called()
        saved_manifest = s3.save_manifest.call_args[0][0]
        assert saved_manifest["vid001"]["summary"] == "Great episode summary"

    def test_empty_summary_not_saved_to_manifest(self):
        """When remove_ads returns empty summary, no 'summary' key is added to manifest."""
        video = _make_video("vid001", duration=600)
        playlist_meta = _make_playlist_meta()
        meta = {
            "upload_date": _RECENT_DATE,
            "description": "desc",
            "thumbnail": "",
            "duration": 600,
            "title": "Unsummarised Episode",
        }

        with (
            patch.dict(os.environ, self._base_env(), clear=True),
            patch("sync.S3Manager") as mock_s3_cls,
            patch("sync.extract_playlist", return_value=(playlist_meta, [video])),
            patch("sync.extract_video_metadata", return_value=meta),
            patch("sync.download_and_convert", return_value="/tmp/vid001.mp3"),
            patch("sync.remove_ads", return_value=("/tmp/vid001.mp3", [], "")),
            patch("sync.build_episode_metadata", return_value=[]),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.shutil.rmtree"),
            patch("os.makedirs"),
            patch("os.remove"),
        ):
            s3 = _make_s3_manager()
            s3.load_manifest.return_value = {}  # real dict so setdefault() works
            mock_s3_cls.return_value = s3
            process_playlist("https://youtube.com/playlist?list=PLtest")

        # Manifest saved but no summary key added
        s3.save_manifest.assert_called()
        saved_manifest = s3.save_manifest.call_args[0][0]
        assert "summary" not in saved_manifest.get("vid001", {})

    @pytest.mark.parametrize("error_code", ["TRANSCRIBE_FAILED", "DETECT_FAILED", "SPLICE_FAILED"])
    def test_error_code_not_saved_as_summary(self, error_code):
        """remove_ads error codes must never be stored as episode summaries in the manifest."""
        video = _make_video("vid001", duration=600)
        playlist_meta = _make_playlist_meta()
        meta = {
            "upload_date": _RECENT_DATE,
            "description": "desc",
            "thumbnail": "",
            "duration": 600,
            "title": "Failed Episode",
        }

        with (
            patch.dict(os.environ, self._base_env(), clear=True),
            patch("sync.S3Manager") as mock_s3_cls,
            patch("sync.extract_playlist", return_value=(playlist_meta, [video])),
            patch("sync.extract_video_metadata", return_value=meta),
            patch("sync.download_and_convert", return_value="/tmp/vid001.mp3"),
            patch("sync.remove_ads", return_value=("/tmp/vid001.mp3", [], error_code)),
            patch("sync.build_episode_metadata", return_value=[]),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.shutil.rmtree"),
            patch("os.makedirs"),
            patch("os.remove"),
        ):
            s3 = _make_s3_manager()
            s3.load_manifest.return_value = {}
            mock_s3_cls.return_value = s3
            process_playlist("https://youtube.com/playlist?list=PLtest")

        saved_manifest = s3.save_manifest.call_args[0][0]
        assert "summary" not in saved_manifest.get("vid001", {}), (
            f"Error code {error_code!r} should not be stored as a summary"
        )


# ---------------------------------------------------------------------------
# process_playlist — extraction faults vs genuinely unavailable videos
# ---------------------------------------------------------------------------


class TestExtractionFaultAccounting:
    """A broken extractor must be reported as a failure, never as a silent skip.

    Regression guard: an ``ExtractionError`` used to surface as
    "video unavailable" with ``failed=0``, so a total YouTube outage looked
    identical to "no new episodes".
    """

    def _run(self, metadata_side_effect, videos=None, dry_run=False):
        playlist_meta = _make_playlist_meta()
        video_entries = videos if videos is not None else [_make_video("vid001")]

        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch("sync.S3Manager") as mock_s3_cls,
            patch("sync.extract_playlist", return_value=(playlist_meta, video_entries)),
            patch("sync.extract_video_metadata", side_effect=metadata_side_effect),
            patch("sync.download_and_convert") as mock_dl,
            patch("sync.build_episode_metadata", return_value=[]),
            patch("sync.generate_rss", return_value="<rss/>"),
            patch("sync.shutil.rmtree"),
            patch("os.makedirs"),
            patch("os.remove"),
        ):
            s3 = _make_s3_manager()
            mock_s3_cls.return_value = s3
            mock_dl.side_effect = lambda url, vid, tmp: f"/tmp/PLtest/{vid}.mp3"
            result = process_playlist(
                "https://youtube.com/playlist?list=PLtest", dry_run=dry_run
            )
            return result, mock_dl

    def test_extraction_error_counts_as_failure_not_unavailable(self):
        result, mock_dl = self._run(
            ExtractionError("Requested format is not available")
        )

        assert result["failed"] == 1
        assert result["unavailable"] == 0
        assert result["new_episodes"] == 0
        assert mock_dl.call_count == 0, "must not attempt a download after extraction failed"

    def test_extraction_error_is_logged_as_retryable(self, caplog):
        with caplog.at_level(logging.ERROR, logger="sync"):
            self._run(ExtractionError("Requested format is not available"))

        assert "EXTRACTION FAILED" in caplog.text
        assert "retried next run" in caplog.text

    def test_extraction_error_does_not_abort_remaining_candidates(self):
        """Unlike bot detection, a per-video fault must not stop the run."""
        good = {
            "upload_date": _RECENT_DATE,
            "description": "",
            "thumbnail": "",
            "duration": 300,
            "title": "Video Title",
        }
        result, mock_dl = self._run(
            [ExtractionError("boom"), good],
            videos=[_make_video("vid001"), _make_video("vid002")],
        )

        assert result["failed"] == 1
        assert result["new_episodes"] == 1
        assert mock_dl.call_count == 1

    def test_unavailable_video_still_reported_separately(self):
        result, mock_dl = self._run([None])

        assert result["unavailable"] == 1
        assert result["failed"] == 0
        assert mock_dl.call_count == 0

    def test_unavailable_count_is_in_the_summary_line(self, caplog):
        with caplog.at_level(logging.WARNING, logger="sync"):
            self._run([None])

        summaries = [r for r in caplog.records if "SYNC SUMMARY" in r.getMessage()]
        assert len(summaries) == 1
        assert "unavailable=1" in summaries[0].getMessage()
        assert summaries[0].levelno == logging.WARNING, (
            "a silent skip must be escalated above INFO so it is visible"
        )

    def test_clean_run_summary_stays_at_info(self, caplog):
        good = {
            "upload_date": _RECENT_DATE,
            "description": "",
            "thumbnail": "",
            "duration": 300,
            "title": "Video Title",
        }
        with caplog.at_level(logging.INFO, logger="sync"):
            self._run([good])

        summaries = [r for r in caplog.records if "SYNC SUMMARY" in r.getMessage()]
        assert len(summaries) == 1
        assert "unavailable=0" in summaries[0].getMessage()
        assert summaries[0].levelno == logging.INFO

    def test_bot_detection_still_takes_precedence_over_unavailable(self, caplog):
        with caplog.at_level(logging.INFO, logger="sync"):
            self._run(
                [None, BotDetectedError("blocked")],
                videos=[_make_video("vid001"), _make_video("vid002")],
            )

        summaries = [r for r in caplog.records if "SYNC SUMMARY" in r.getMessage()]
        assert summaries[0].levelno == logging.ERROR
        assert "bot_detected=True" in summaries[0].getMessage()

    def test_extraction_error_reported_in_dry_run_too(self):
        result, mock_dl = self._run(ExtractionError("boom"), dry_run=True)

        assert result["failed"] == 1
        assert mock_dl.call_count == 0
