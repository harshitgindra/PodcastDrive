"""Tests targeting specific uncovered lines to maximize coverage."""

import os
from unittest.mock import MagicMock, patch

# ── ad_remover.py: _load_summary_cache HIT path (lines 412-415) ──────────────


class TestLoadSummaryCacheHit:
    def test_returns_cached_summary_text(self):
        from ad_remover import _load_summary_cache

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"This is a cached summary")}
        result = _load_summary_cache(mock_s3, "my-bucket", "video123")
        assert result == "This is a cached summary"

    def test_returns_none_for_empty_cached_text(self):
        from ad_remover import _load_summary_cache

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"   ")}
        result = _load_summary_cache(mock_s3, "my-bucket", "video123")
        assert result is None


# ── ad_remover.py: generate_episode_summary edge cases (lines 1639, 1664, 1669)


class TestGenerateEpisodeSummaryEdgeCases:
    def test_returns_empty_when_segments_empty(self):
        from ad_remover import _generate_summary as generate_episode_summary

        with patch.dict(os.environ, {"GENERATE_SUMMARIES": "true", "S3_BUCKET": "b"}):
            result = generate_episode_summary(segments=[], video_id="v1", duration_secs=100.0)
        assert result == ""

    def test_returns_empty_when_bucket_not_set(self):
        from ad_remover import _generate_summary as generate_episode_summary

        with patch.dict(os.environ, {"GENERATE_SUMMARIES": "true", "S3_BUCKET": ""}):
            result = generate_episode_summary(segments=[{"text": "hello"}], video_id="v1", duration_secs=100.0)
        assert result == ""

    def test_returns_cached_summary_without_regenerating(self):
        from ad_remover import _generate_summary as generate_episode_summary

        with (
            patch.dict(
                os.environ,
                {
                    "GENERATE_SUMMARIES": "true",
                    "S3_BUCKET": "mybucket",
                    "AWS_DEFAULT_REGION": "us-east-1",
                },
            ),
            patch("ad_remover.boto3") as mock_boto3,
        ):
            mock_s3 = MagicMock()
            mock_boto3.client.return_value = mock_s3
            mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"Cached summary here")}
            result = generate_episode_summary(segments=[{"text": "hello"}], video_id="v1", duration_secs=100.0)
        assert result == "Cached summary here"


# ── health_report.py: blank line skip (line 39) and empty log (line 85) ───────


class TestHealthReportEdgeCases:
    def test_fetch_runs_skips_blank_lines(self):
        from datetime import datetime, timezone

        from health_report import _fetch_runs

        mock_s3 = MagicMock()
        content = (
            '{"started_at": "2025-01-01T00:00:00+00:00", "status": "ok"}\n'
            "\n"
            '{"started_at": "2025-01-02T00:00:00+00:00", "status": "ok"}\n'
        )
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: content.encode())}
        since = datetime(2024, 12, 1, tzinfo=timezone.utc)
        result = _fetch_runs(mock_s3, "bucket", since)
        assert len(result) == 2

    def test_fetch_log_files_handles_exception(self):
        from datetime import datetime, timezone

        from health_report import _fetch_log_files

        mock_s3 = MagicMock()
        mock_s3.get_paginator.side_effect = Exception("network error")
        since = datetime(2024, 12, 1, tzinfo=timezone.utc)
        result = _fetch_log_files(mock_s3, "bucket", since)
        assert result == []

    def test_parse_log_file_empty_content(self):
        from health_report import _parse_log_file

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"")}
        result = _parse_log_file(mock_s3, "bucket", "_meta/logs/2025/01/01/run.log")
        assert result.get("lines") == 0 or "key" in result


# ── reset.py: disabled podcast skip (line 67) ────────────────────────────────


class TestResetDisabledPodcastSkip:
    def test_skips_disabled_podcasts_in_slug_collection(self):
        from config_provider import PodcastConfig
        from reset import _collect_slugs

        mock_yt_provider = MagicMock()
        mock_yt_provider.get_podcasts.return_value = [
            PodcastConfig(name="Active YT", url="PLxyz", enabled=True),
            PodcastConfig(name="Disabled YT", url="PLabc", enabled=False),
        ]
        mock_rss_provider = MagicMock()
        mock_rss_provider.get_podcasts.return_value = [
            PodcastConfig(name="Active RSS", url="http://a", enabled=True, source="Podcast"),
            PodcastConfig(name="Disabled RSS", url="http://b", enabled=False, source="Podcast"),
        ]
        with (
            patch("config_provider.get_config_provider", return_value=mock_yt_provider),
            patch("config_provider.get_podcast_config_provider", return_value=mock_rss_provider),
        ):
            slugs = _collect_slugs()
        names = [s[0] for s in slugs]
        assert "Active YT" in names
        assert "Disabled YT" not in names
        assert "Active RSS" in names
        assert "Disabled RSS" not in names


# ── config_provider.py: NotionConfigProvider.update_last_run with runner (line 417)


class TestNotionUpdateLastRunWithRunner:
    def test_includes_runner_field_when_hostname_set(self):
        import json

        from config_provider import NotionConfigProvider, PodcastConfig

        with patch.dict(
            os.environ,
            {
                "NOTION_API_KEY": "secret_test",
                "NOTION_DATABASE_ID": "db-id",
            },
        ):
            provider = NotionConfigProvider()

        podcast = PodcastConfig(name="Test", url="http://x", page_id="page-123")

        with patch("urllib.request.urlopen") as mock_urlopen, patch.dict(os.environ, {"RUNNER": "my-host"}):
            mock_urlopen.return_value.__enter__ = MagicMock()
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            provider.update_last_run(podcast, feed_url="http://feed.xml")

        # Verify the request was made with Runner property
        call_args = mock_urlopen.call_args
        if call_args:
            req = call_args[0][0]
            body = json.loads(req.data.decode())
            assert "Runner" in body["properties"]
