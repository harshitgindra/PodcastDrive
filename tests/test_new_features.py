"""Tests for new features: Fixes 1–8.

Covers:
  Fix 1 — bestaudio format and MP3_QUALITY env var
  Fix 2 — Per-podcast language in RSS feeds
  Fix 3 — RSS podcast channel description
  Fix 5 — YamlPodcastConfigProvider for RSS sources
  Fix 6 — _save_transcript_text
  Fix 7 — generate_episode_summary + remove_ads 3-tuple
  Fix 8 — Chapter markers in episode description
"""

import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Fix 1: Audio format selector + MP3_QUALITY
# ---------------------------------------------------------------------------


class TestFix1AudioFormat:
    @patch("downloader.yt_dlp.YoutubeDL")
    def test_uses_bestaudio_format(self, mock_ydl_cls):
        """download_and_convert uses bestaudio format string."""
        from downloader import download_and_convert

        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "fmt_test"
            mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")

            def fake_download(urls):
                with open(mp3_path, "wb") as f:
                    f.write(b"\xff\xfb\x90\x00" * 100)

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = fake_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            download_and_convert("https://youtube.com/watch?v=x", video_id, tmp_dir)

            opts = mock_ydl_cls.call_args[0][0]
            assert opts["format"] == "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_mp3_quality_env_override(self, mock_ydl_cls, monkeypatch):
        """MP3_QUALITY env var overrides the preferredquality."""
        monkeypatch.setenv("MP3_QUALITY", "128")
        from downloader import download_and_convert

        with tempfile.TemporaryDirectory() as tmp_dir:
            video_id = "qual_test"
            mp3_path = os.path.join(tmp_dir, f"{video_id}.mp3")

            def fake_download(urls):
                with open(mp3_path, "wb") as f:
                    f.write(b"\xff\xfb\x90\x00" * 100)

            mock_ydl = MagicMock()
            mock_ydl.download.side_effect = fake_download
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl_cls.return_value = mock_ydl

            download_and_convert("https://youtube.com/watch?v=x", video_id, tmp_dir)

            opts = mock_ydl_cls.call_args[0][0]
            assert opts["postprocessors"][0]["preferredquality"] == "128"


# ---------------------------------------------------------------------------
# Fix 2: Per-podcast language in RSS feeds
# ---------------------------------------------------------------------------


class TestFix2Language:
    def test_generate_rss_uses_language_param(self):
        """generate_rss sets <language> from the language parameter."""
        from models import EpisodeMeta, PlaylistMeta
        from rss_generator import generate_rss

        meta = PlaylistMeta(
            title="T", description="D", uploader="U",
            channel_url="http://c", webpage_url="http://w", playlist_id="PL1",
        )
        ep = EpisodeMeta(
            video_id="v1", title="Ep1", description="desc", duration=60,
            upload_date="20250601", thumbnail="", webpage_url="http://v",
            playlist_index=1, s3_key="k", file_size=100,
            cloudfront_url="https://cdn.example.com/PL1/episodes/v1.mp3",
        )
        xml_str = generate_rss(meta, [ep], "https://cdn.example.com", "PL1", language="hi")
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/language").text == "hi"

    def test_generate_rss_defaults_to_en(self):
        """generate_rss defaults to 'en' when language not specified."""
        from models import EpisodeMeta, PlaylistMeta
        from rss_generator import generate_rss

        meta = PlaylistMeta(
            title="T", description="D", uploader="U",
            channel_url="http://c", webpage_url="http://w", playlist_id="PL1",
        )
        ep = EpisodeMeta(
            video_id="v1", title="Ep1", description="desc", duration=60,
            upload_date="20250601", thumbnail="", webpage_url="http://v",
            playlist_index=1, s3_key="k", file_size=100,
            cloudfront_url="https://cdn.example.com/PL1/episodes/v1.mp3",
        )
        xml_str = generate_rss(meta, [ep], "https://cdn.example.com", "PL1")
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/language").text == "en"

    def test_build_podcast_feed_xml_uses_language(self):
        """_build_podcast_feed_xml uses the language parameter."""
        from config_provider import PodcastConfig
        from podcast_sync import _build_podcast_feed_xml

        podcast = PodcastConfig(name="P", url="http://x", language="ja")
        xml_str = _build_podcast_feed_xml(
            podcast, [], [], "https://cdn.example.com", "slug", language="ja"
        )
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/language").text == "ja"


# ---------------------------------------------------------------------------
# Fix 5: YamlPodcastConfigProvider
# ---------------------------------------------------------------------------


class TestFix5YamlPodcastProvider:
    def test_returns_only_podcast_source_entries(self, tmp_path):
        """YamlPodcastConfigProvider returns only source=Podcast entries."""
        yaml_content = """
defaults:
  max_downloads: 5

podcasts:
  - name: YouTube Show
    url: PLxxxx
    source: YouTube
  - name: RSS Pod
    url: https://feeds.example.com/rss
    source: Podcast
  - name: Another YT
    url: "@channel"
"""
        yaml_file = tmp_path / "podcasts.yaml"
        yaml_file.write_text(yaml_content)

        from config_provider import YamlPodcastConfigProvider
        provider = YamlPodcastConfigProvider(path=str(yaml_file))
        podcasts = provider.get_podcasts()
        assert len(podcasts) == 1
        assert podcasts[0].name == "RSS Pod"
        assert podcasts[0].source == "Podcast"

    def test_update_url_noop_with_warning(self, tmp_path, caplog):
        """update_url logs a warning and does nothing."""
        yaml_file = tmp_path / "podcasts.yaml"
        yaml_file.write_text("podcasts: []")

        from config_provider import PodcastConfig, YamlPodcastConfigProvider
        provider = YamlPodcastConfigProvider(path=str(yaml_file))
        podcast = PodcastConfig(name="Test", url="http://old")
        provider.update_url(podcast, "http://new")
        assert "write-back not supported" in caplog.text


# ---------------------------------------------------------------------------
# Fix 6: _save_transcript_text
# ---------------------------------------------------------------------------


class TestFix6TranscriptText:
    def test_saves_formatted_text_to_s3(self):
        """_save_transcript_text writes formatted text with correct key."""
        import ad_remover

        s3 = MagicMock()
        segments = [
            {"start": 0.5, "end": 1.0, "text": "Hello"},
            {"start": 1.1, "end": 2.0, "text": "world"},
        ]
        ad_remover._save_transcript_text(s3, "my-bucket", "ep001", segments)

        s3.put_object.assert_called_once()
        call_kwargs = s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "my-bucket"
        assert call_kwargs["Key"] == "transcribe-cache/ep001.txt"
        assert call_kwargs["ContentType"] == "text/plain"
        body = call_kwargs["Body"].decode("utf-8")
        assert "[0.5s]  Hello" in body
        assert "[1.1s]  world" in body

    def test_custom_prefix(self, monkeypatch):
        """Respects TRANSCRIBE_CACHE_PREFIX env var."""
        import ad_remover
        monkeypatch.setenv("TRANSCRIBE_CACHE_PREFIX", "custom-prefix")

        s3 = MagicMock()
        ad_remover._save_transcript_text(s3, "b", "vid", [{"start": 0, "end": 1, "text": "x"}])
        key = s3.put_object.call_args[1]["Key"]
        assert key == "custom-prefix/vid.txt"


# ---------------------------------------------------------------------------
# Fix 7: Episode summaries
# ---------------------------------------------------------------------------


class TestFix7Summary:
    def test_generate_episode_summary_success(self, monkeypatch):
        """generate_episode_summary calls Bedrock and returns summary text."""
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "This episode covers X and Y."}]}}
        }
        with patch("summary_generator.boto3.client", return_value=mock_client):
            from summary_generator import generate_episode_summary
            result = generate_episode_summary(
                [{"start": 0, "end": 10, "text": "some content"}],
                "Episode Title",
            )
        assert result == "This episode covers X and Y."
        # Verify prompt contains episode title
        call_args = mock_client.converse.call_args
        prompt_text = call_args[1]["messages"][0]["content"][0]["text"]
        assert "Episode Title" in prompt_text

    def test_generate_episode_summary_returns_empty_on_error(self, monkeypatch):
        """Returns empty string on Bedrock failure."""
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")

        with patch("summary_generator.boto3.client", side_effect=Exception("boom")):
            from summary_generator import generate_episode_summary
            result = generate_episode_summary(
                [{"start": 0, "end": 10, "text": "x"}], "Ep"
            )
        assert result == ""

    def test_generate_episode_summary_truncates_long_transcript(self, monkeypatch):
        """Transcript is truncated to 40,000 chars."""
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "Summary."}]}}
        }
        long_segments = [{"start": 0, "end": 1, "text": "a" * 50000}]

        with patch("summary_generator.boto3.client", return_value=mock_client):
            from summary_generator import generate_episode_summary
            generate_episode_summary(long_segments, "Ep")

        prompt = mock_client.converse.call_args[1]["messages"][0]["content"][0]["text"]
        # The transcript part should be truncated
        assert len(prompt) < 41000  # prompt header + 40k text

    def test_remove_ads_returns_3_tuple(self, monkeypatch, tmp_path):
        """remove_ads returns (path, segments, summary) 3-tuple."""
        monkeypatch.setenv("S3_BUCKET", "b")
        monkeypatch.setenv("REMOVE_ADS", "false")
        import ad_remover

        result = ad_remover.remove_ads(str(tmp_path / "ep.mp3"), "v1", str(tmp_path))
        assert len(result) == 3
        assert result[2] == ""  # summary empty when REMOVE_ADS=false


# ---------------------------------------------------------------------------
# Fix 8: Chapter markers in episode description
# ---------------------------------------------------------------------------


class TestFix8Chapters:
    def test_chapters_appended_to_description(self):
        """Episode description includes formatted chapter markers."""
        from models import EpisodeMeta, PlaylistMeta
        from rss_generator import generate_rss

        meta = PlaylistMeta(
            title="T", description="D", uploader="U",
            channel_url="http://c", webpage_url="http://w", playlist_id="PL1",
        )
        ep = EpisodeMeta(
            video_id="v1", title="Ep1", description="Episode text.",
            duration=3600, upload_date="20250601", thumbnail="",
            webpage_url="http://v", playlist_index=1, s3_key="k",
            file_size=100,
            cloudfront_url="https://cdn.example.com/PL1/episodes/v1.mp3",
            chapters=[
                {"start_time": 0, "end_time": 300, "title": "Intro"},
                {"start_time": 300, "end_time": 900, "title": "Main Topic"},
                {"start_time": 3600, "end_time": 3660, "title": "Hour Mark"},
            ],
        )
        xml_str = generate_rss(meta, [ep], "https://cdn.example.com", "PL1")
        root = ET.fromstring(xml_str)
        desc = root.find(".//item/description").text
        assert "Chapters:" in desc
        assert "0:00  Intro" in desc
        assert "5:00  Main Topic" in desc
        assert "1:00:00  Hour Mark" in desc

    def test_no_chapters_no_section(self):
        """No chapters section when episode.chapters is empty."""
        from models import EpisodeMeta, PlaylistMeta
        from rss_generator import generate_rss

        meta = PlaylistMeta(
            title="T", description="D", uploader="U",
            channel_url="http://c", webpage_url="http://w", playlist_id="PL1",
        )
        ep = EpisodeMeta(
            video_id="v1", title="Ep1", description="Episode text.",
            duration=60, upload_date="20250601", thumbnail="",
            webpage_url="http://v", playlist_index=1, s3_key="k",
            file_size=100,
            cloudfront_url="https://cdn.example.com/PL1/episodes/v1.mp3",
            chapters=[],
        )
        xml_str = generate_rss(meta, [ep], "https://cdn.example.com", "PL1")
        root = ET.fromstring(xml_str)
        desc = root.find(".//item/description").text
        assert "Chapters:" not in desc

    def test_extract_video_metadata_includes_chapters(self):
        """extract_video_metadata returns chapters from yt-dlp info."""
        from extractor import extract_video_metadata

        fake_info = {
            "upload_date": "20250601",
            "description": "desc",
            "thumbnail": "http://t",
            "duration": 600,
            "title": "Title",
            "live_status": "not_live",
            "chapters": [
                {"start_time": 0, "end_time": 60, "title": "Ch1"},
            ],
        }
        with patch("extractor.yt_dlp.YoutubeDL") as mock_cls:
            mock_ydl = MagicMock()
            mock_ydl.extract_info.return_value = fake_info
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_cls.return_value = mock_ydl

            result = extract_video_metadata("http://example.com")

        assert result["chapters"] == [{"start_time": 0, "end_time": 60, "title": "Ch1"}]
