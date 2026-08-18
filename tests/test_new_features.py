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

import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

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
            title="T",
            description="D",
            uploader="U",
            channel_url="http://c",
            webpage_url="http://w",
            playlist_id="PL1",
        )
        ep = EpisodeMeta(
            video_id="v1",
            title="Ep1",
            description="desc",
            duration=60,
            upload_date="20250601",
            thumbnail="",
            webpage_url="http://v",
            playlist_index=1,
            s3_key="k",
            file_size=100,
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
            title="T",
            description="D",
            uploader="U",
            channel_url="http://c",
            webpage_url="http://w",
            playlist_id="PL1",
        )
        ep = EpisodeMeta(
            video_id="v1",
            title="Ep1",
            description="desc",
            duration=60,
            upload_date="20250601",
            thumbnail="",
            webpage_url="http://v",
            playlist_index=1,
            s3_key="k",
            file_size=100,
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
        xml_str = _build_podcast_feed_xml(podcast, [], [], "https://cdn.example.com", "slug", language="ja")
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

            result = generate_episode_summary([{"start": 0, "end": 10, "text": "x"}], "Ep")
        assert result == ""

    def test_generate_episode_summary_truncates_long_transcript(self, monkeypatch):
        """Transcript is truncated to 40,000 chars."""
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")

        mock_client = MagicMock()
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "Summary."}]}}}
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
            title="T",
            description="D",
            uploader="U",
            channel_url="http://c",
            webpage_url="http://w",
            playlist_id="PL1",
        )
        ep = EpisodeMeta(
            video_id="v1",
            title="Ep1",
            description="Episode text.",
            duration=3600,
            upload_date="20250601",
            thumbnail="",
            webpage_url="http://v",
            playlist_index=1,
            s3_key="k",
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
            title="T",
            description="D",
            uploader="U",
            channel_url="http://c",
            webpage_url="http://w",
            playlist_id="PL1",
        )
        ep = EpisodeMeta(
            video_id="v1",
            title="Ep1",
            description="Episode text.",
            duration=60,
            upload_date="20250601",
            thumbnail="",
            webpage_url="http://v",
            playlist_index=1,
            s3_key="k",
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


# ---------------------------------------------------------------------------
# Feed Title Differentiation
# ---------------------------------------------------------------------------


class TestFeedDifferentiation:
    """Tests for FEED_TITLE_SUFFIX and FEED_SUBTITLE env vars."""

    def _make_playlist_meta(self):
        from models import PlaylistMeta

        return PlaylistMeta(
            title="My Podcast",
            description="Desc",
            uploader="Host",
            channel_url="http://c",
            webpage_url="http://w",
            playlist_id="PL1",
        )

    def _make_episode(self):
        from models import EpisodeMeta

        return EpisodeMeta(
            video_id="v1",
            title="Ep1",
            description="desc",
            duration=60,
            upload_date="20250601",
            thumbnail="",
            webpage_url="http://v",
            playlist_index=1,
            s3_key="k",
            file_size=100,
            cloudfront_url="https://cdn.example.com/PL1/episodes/v1.mp3",
        )

    def _make_podcast_config(self):
        from config_provider import PodcastConfig

        return PodcastConfig(name="My Podcast", url="http://x")

    def test_default_suffix_youtube(self, monkeypatch):
        """Default suffix ' ✂️' applied to YouTube feed title."""
        monkeypatch.delenv("FEED_TITLE_SUFFIX", raising=False)
        monkeypatch.delenv("FEED_SUBTITLE", raising=False)
        from rss_generator import generate_rss

        meta = self._make_playlist_meta()
        xml_str = generate_rss(meta, [self._make_episode()], "https://cdn.example.com", "PL1")
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/title").text == "My Podcast ✂️"

    def test_default_suffix_podcast(self, monkeypatch):
        """Default suffix ' ✂️' applied to podcast feed title."""
        monkeypatch.delenv("FEED_TITLE_SUFFIX", raising=False)
        monkeypatch.delenv("FEED_SUBTITLE", raising=False)
        from podcast_sync import _build_podcast_feed_xml

        podcast = self._make_podcast_config()
        xml_str = _build_podcast_feed_xml(podcast, [], [], "https://cdn.example.com", "slug")
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/title").text == "My Podcast ✂️"

    def test_custom_suffix(self, monkeypatch):
        """Custom suffix applied when FEED_TITLE_SUFFIX is set."""
        monkeypatch.setenv("FEED_TITLE_SUFFIX", " [Clean]")
        from rss_generator import generate_rss

        meta = self._make_playlist_meta()
        xml_str = generate_rss(meta, [self._make_episode()], "https://cdn.example.com", "PL1")
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/title").text == "My Podcast [Clean]"

    def test_suffix_disabled(self, monkeypatch):
        """Empty FEED_TITLE_SUFFIX means no suffix."""
        monkeypatch.setenv("FEED_TITLE_SUFFIX", "")
        from rss_generator import generate_rss

        meta = self._make_playlist_meta()
        xml_str = generate_rss(meta, [self._make_episode()], "https://cdn.example.com", "PL1")
        root = ET.fromstring(xml_str)
        assert root.find(".//channel/title").text == "My Podcast"

    def test_subtitle_present(self, monkeypatch):
        """Default FEED_SUBTITLE produces itunes:subtitle element."""
        monkeypatch.delenv("FEED_SUBTITLE", raising=False)
        monkeypatch.setenv("FEED_TITLE_SUFFIX", "")
        from rss_generator import generate_rss

        meta = self._make_playlist_meta()
        xml_str = generate_rss(meta, [self._make_episode()], "https://cdn.example.com", "PL1")
        # Parse with namespace
        ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
        root = ET.fromstring(xml_str)
        subtitle_el = root.find(".//channel/itunes:subtitle", ns)
        assert subtitle_el is not None
        assert subtitle_el.text == "Ad-free · PodcastDrive"

    def test_subtitle_disabled(self, monkeypatch):
        """Empty FEED_SUBTITLE means no itunes:subtitle element."""
        monkeypatch.setenv("FEED_SUBTITLE", "")
        monkeypatch.setenv("FEED_TITLE_SUFFIX", "")
        from rss_generator import generate_rss

        meta = self._make_playlist_meta()
        xml_str = generate_rss(meta, [self._make_episode()], "https://cdn.example.com", "PL1")
        ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
        root = ET.fromstring(xml_str)
        subtitle_el = root.find(".//channel/itunes:subtitle", ns)
        assert subtitle_el is None

    def test_original_name_not_mutated_youtube(self, monkeypatch):
        """PlaylistMeta.title is unchanged after generate_rss."""
        monkeypatch.delenv("FEED_TITLE_SUFFIX", raising=False)
        from rss_generator import generate_rss

        meta = self._make_playlist_meta()
        generate_rss(meta, [self._make_episode()], "https://cdn.example.com", "PL1")
        assert meta.title == "My Podcast"

    def test_original_name_not_mutated_podcast(self, monkeypatch):
        """PodcastConfig.name is unchanged after _build_podcast_feed_xml."""
        monkeypatch.delenv("FEED_TITLE_SUFFIX", raising=False)
        from podcast_sync import _build_podcast_feed_xml

        podcast = self._make_podcast_config()
        _build_podcast_feed_xml(podcast, [], [], "https://cdn.example.com", "slug")
        assert podcast.name == "My Podcast"


# ---------------------------------------------------------------------------
# Ad Removal Parity tests
# ---------------------------------------------------------------------------


class TestAdRemovalParity:
    """Regression guards ensuring ad_cleaner_harness uses the canonical remove_ads() path."""

    def test_remove_ads_is_the_entrypoint(self):
        """ad_cleaner_harness.py must not import internal ad_remover functions directly."""
        import ast

        source_path = os.path.join(os.path.dirname(__file__), "..", "ad_cleaner_harness.py")
        with open(source_path) as f:
            tree = ast.parse(f.read())

        # Collect all names imported from ad_remover
        forbidden = {
            "transcribe_audio",
            "detect_ads",
            "splice_audio",
            "_merge_overlapping_ads",
            "snap_ad_boundaries",
            "detect_silence",
        }
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "ad_remover":
                for alias in node.names:
                    imported_names.add(alias.name)

        violations = imported_names & forbidden
        assert not violations, (
            f"ad_cleaner_harness.py imports internal functions from ad_remover: {violations}. "
            "It should only import remove_ads()."
        )

    def test_rss_episode_id_matches_production(self):
        """episode_id_from_guid produces the same ID for test and production paths."""
        from podcast_downloader import episode_id_from_guid

        # URL-style GUID
        guid1 = "https://rss.art19.com/episodes/abc123-def456"
        id1 = episode_id_from_guid(guid1)
        assert id1 == "abc123-def456"  # last path segment

        # Plain UUID GUID
        guid2 = "abc123-def456-789"
        id2 = episode_id_from_guid(guid2)
        # Should be consistent (no full-URL encoding like the old code)
        assert "/" not in id2
        assert len(id2) <= 64

        # Short string
        guid3 = "episode-42"
        id3 = episode_id_from_guid(guid3)
        assert id3  # non-empty
        assert "/" not in id3

    def test_sync_podcast_py_exits_nonzero(self):
        """sync_podcast.py is retired and must exit non-zero with deprecation message."""
        import subprocess

        result = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "..", "sync_podcast.py")],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "DEPRECATED" in result.stderr


# ---------------------------------------------------------------------------
# Repository layout / coverage configuration guards (Fix #18)
# ---------------------------------------------------------------------------


class TestProjectLayoutGuards:
    """Keep the test/coverage configuration honest as the tree grows."""

    @staticmethod
    def _repo_root():
        return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    @staticmethod
    def _pyproject():
        import tomllib

        with open(os.path.join(TestProjectLayoutGuards._repo_root(), "pyproject.toml"), "rb") as fh:
            return tomllib.load(fh)

    def test_no_test_named_scripts_outside_tests_dir(self):
        """Manual harnesses must not be named test_*.py — `pytest .` would import them."""
        root = self._repo_root()
        stray = [
            name
            for name in os.listdir(root)
            if name.startswith("test_") and name.endswith(".py") and os.path.isfile(os.path.join(root, name))
        ]
        assert stray == [], f"rename these manual scripts so pytest cannot collect them: {stray}"

    def test_ad_cleaner_harness_exists_and_is_wired_to_the_wrapper(self):
        root = self._repo_root()
        harness = os.path.join(root, "ad_cleaner_harness.py")
        assert os.path.isfile(harness)
        with open(os.path.join(root, "test_ad.sh")) as fh:
            assert "ad_cleaner_harness.py" in fh.read()

    def test_coverage_threshold_is_configured_in_pyproject(self):
        """CI must not carry its own threshold — it would drift from local runs."""
        config = self._pyproject()
        assert config["tool"]["coverage"]["report"]["fail_under"] == 95
        assert config["tool"]["coverage"]["run"]["source"] == ["src"]
        assert "--cov=src" in config["tool"]["pytest"]["ini_options"]["addopts"]

    def test_ci_workflow_does_not_duplicate_coverage_flags(self):
        with open(os.path.join(self._repo_root(), ".github", "workflows", "test.yml")) as fh:
            workflow = fh.read()
        assert "--cov-fail-under" not in workflow, "threshold is configured in pyproject.toml"
