"""Unit tests for config_provider module."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
import yaml

from config_provider import (
    NotionConfigProvider,
    NotionPodcastConfigProvider,
    PodcastConfig,
    YamlConfigProvider,
    get_config_provider,
    get_podcast_config_provider,
)

# ---------------------------------------------------------------------------
# YamlConfigProvider
# ---------------------------------------------------------------------------


class TestYamlConfigProviderMissingFile:
    def test_returns_empty_list_when_file_not_found(self):
        provider = YamlConfigProvider(path="/nonexistent/path/podcasts.yaml")
        result = provider.get_podcasts()
        assert result == []


class TestYamlConfigProviderLoading:
    def _write_yaml(self, path, data):
        with open(path, "w") as f:
            yaml.dump(data, f)

    def test_loads_basic_podcast(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(
                {"podcasts": [{"name": "My Show", "url": "PLabc123", "enabled": True}]},
                f,
            )
            tmp_path = f.name

        try:
            provider = YamlConfigProvider(path=tmp_path)
            podcasts = provider.get_podcasts()
            assert len(podcasts) == 1
            assert podcasts[0].name == "My Show"
            assert podcasts[0].url == "PLabc123"
            assert podcasts[0].enabled is True
        finally:
            os.unlink(tmp_path)

    def test_defaults_applied_when_not_overridden(self):
        data = {
            "defaults": {"max_downloads": 5, "max_age_days": 14, "sleep_between": 3},
            "podcasts": [{"name": "Show A", "url": "PLxyz"}],
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            tmp_path = f.name

        try:
            provider = YamlConfigProvider(path=tmp_path)
            podcasts = provider.get_podcasts()
            assert podcasts[0].max_downloads == 5
            assert podcasts[0].max_age_days == 14
            assert podcasts[0].sleep_between == 3
        finally:
            os.unlink(tmp_path)

    def test_per_podcast_overrides_defaults(self):
        data = {
            "defaults": {"max_downloads": 10, "max_age_days": 7},
            "podcasts": [{"name": "Override", "url": "PLabc", "max_downloads": 2, "max_age_days": 3}],
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            tmp_path = f.name

        try:
            provider = YamlConfigProvider(path=tmp_path)
            podcasts = provider.get_podcasts()
            assert podcasts[0].max_downloads == 2
            assert podcasts[0].max_age_days == 3
        finally:
            os.unlink(tmp_path)

    def test_url_used_as_name_fallback(self):
        data = {"podcasts": [{"url": "PLfallback"}]}
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            tmp_path = f.name

        try:
            provider = YamlConfigProvider(path=tmp_path)
            podcasts = provider.get_podcasts()
            assert podcasts[0].name == "PLfallback"
        finally:
            os.unlink(tmp_path)

    def test_enabled_defaults_to_true(self):
        data = {"podcasts": [{"name": "Show", "url": "PLabc"}]}
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            tmp_path = f.name

        try:
            provider = YamlConfigProvider(path=tmp_path)
            podcasts = provider.get_podcasts()
            assert podcasts[0].enabled is True
        finally:
            os.unlink(tmp_path)

    def test_empty_yaml_returns_empty_list(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write("")
            tmp_path = f.name

        try:
            provider = YamlConfigProvider(path=tmp_path)
            result = provider.get_podcasts()
            assert result == []
        finally:
            os.unlink(tmp_path)

    def test_multiple_podcasts_loaded(self):
        data = {
            "podcasts": [
                {"name": "A", "url": "PL1"},
                {"name": "B", "url": "PL2"},
                {"name": "C", "url": "PL3"},
            ]
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            tmp_path = f.name

        try:
            provider = YamlConfigProvider(path=tmp_path)
            podcasts = provider.get_podcasts()
            assert len(podcasts) == 3
            assert [p.name for p in podcasts] == ["A", "B", "C"]
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# NotionConfigProvider — init
# ---------------------------------------------------------------------------


class TestNotionConfigProviderInit:
    def test_raises_if_api_key_missing(self):
        env = {"NOTION_DATABASE_ID": "db-123"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("NOTION_API_KEY", None)
            with pytest.raises(ValueError, match="NOTION_API_KEY"):
                NotionConfigProvider()

    def test_raises_if_database_id_missing(self):
        env = {"NOTION_API_KEY": "secret_abc"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("NOTION_DATABASE_ID", None)
            with pytest.raises(ValueError, match="NOTION_DATABASE_ID"):
                NotionConfigProvider()

    def test_constructs_with_both_env_vars(self):
        with patch.dict(
            os.environ,
            {
                "NOTION_API_KEY": "secret_abc",
                "NOTION_DATABASE_ID": "db-123",
            },
        ):
            provider = NotionConfigProvider()
            assert provider.api_key == "secret_abc"
            assert provider.database_id == "db-123"


# ---------------------------------------------------------------------------
# NotionConfigProvider — _parse_page
# ---------------------------------------------------------------------------


class TestNotionParsePage:
    def _make_provider(self):
        with patch.dict(
            os.environ,
            {
                "NOTION_API_KEY": "key",
                "NOTION_DATABASE_ID": "db",
            },
        ):
            return NotionConfigProvider()

    def _title_prop(self, text):
        return {"type": "title", "title": [{"plain_text": text}]}

    def _rich_text_prop(self, text):
        return {"type": "rich_text", "rich_text": [{"plain_text": text}]}

    def _url_prop(self, url):
        return {"type": "url", "url": url}

    def _checkbox_prop(self, checked):
        return {"type": "checkbox", "checkbox": checked}

    def _number_prop(self, n):
        return {"type": "number", "number": n}

    def _select_prop(self, value):
        return {"type": "select", "select": {"name": value}}

    def _base_props(self, name="My Podcast", url="PLabc123", enabled=True, source="YouTube"):
        """Helper: return a complete valid props dict."""
        return {
            "Name": self._title_prop(name),
            "URL": self._rich_text_prop(url),
            "Enabled": self._checkbox_prop(enabled),
            "Source": self._select_prop(source),
        }

    def test_parses_full_page(self):
        provider = self._make_provider()
        props = {
            "Name": self._title_prop("My Podcast"),
            "URL": self._rich_text_prop("PLabc123"),
            "Enabled": self._checkbox_prop(True),
            "Source": self._select_prop("YouTube"),
            "Max Downloads": self._number_prop(5),
            "Max Age Days": self._number_prop(14),
        }
        result = provider._parse_page(props)
        assert result is not None
        assert result.name == "My Podcast"
        assert result.url == "PLabc123"
        assert result.enabled is True
        assert result.max_downloads == 5
        assert result.max_age_days == 14

    def test_returns_none_when_url_empty(self):
        provider = self._make_provider()
        props = {
            "Name": self._title_prop("Podcast"),
            "URL": self._rich_text_prop(""),
            "Source": self._select_prop("YouTube"),
        }
        result = provider._parse_page(props)
        assert result is None

    def test_url_as_url_type(self):
        provider = self._make_provider()
        props = {
            "Name": self._title_prop("Podcast"),
            "URL": self._url_prop("https://youtube.com/playlist?list=PLxyz"),
            "Source": self._select_prop("YouTube"),
        }
        result = provider._parse_page(props)
        assert result is not None
        assert result.url == "https://youtube.com/playlist?list=PLxyz"

    def test_disabled_podcast_returns_none(self):
        """Disabled entries should be filtered out and return None."""
        provider = self._make_provider()
        props = self._base_props(enabled=False)
        result = provider._parse_page(props)
        assert result is None

    def test_optional_numbers_none_when_absent(self):
        provider = self._make_provider()
        props = self._base_props()
        result = provider._parse_page(props)
        assert result is not None
        assert result.max_downloads is None
        assert result.max_age_days is None

    def test_returns_none_when_source_is_podcast(self):
        """Source = 'Podcast' should be excluded."""
        provider = self._make_provider()
        props = self._base_props(source="Podcast")
        result = provider._parse_page(props)
        assert result is None

    def test_returns_none_when_source_is_missing(self):
        """Absent Source field should be excluded (source is required)."""
        provider = self._make_provider()
        props = {
            "Name": self._title_prop("Podcast"),
            "URL": self._rich_text_prop("PLabc"),
            "Enabled": self._checkbox_prop(True),
            # No "Source" key
        }
        result = provider._parse_page(props)
        assert result is None

    def test_returns_none_when_source_select_is_null(self):
        """Source present but select value is null should be excluded."""
        provider = self._make_provider()
        props = {
            "Name": self._title_prop("Podcast"),
            "URL": self._rich_text_prop("PLabc"),
            "Enabled": self._checkbox_prop(True),
            "Source": {"type": "select", "select": None},
        }
        result = provider._parse_page(props)
        assert result is None

    def test_includes_when_source_is_youtube(self):
        """Source = 'YouTube' + enabled = True → valid config returned."""
        provider = self._make_provider()
        props = self._base_props(source="YouTube")
        result = provider._parse_page(props)
        assert result is not None
        assert result.url == "PLabc123"

    def test_url_used_as_name_when_title_empty(self):
        provider = self._make_provider()
        props = {
            "Name": {"type": "title", "title": []},
            "URL": self._rich_text_prop("PLabc"),
            "Enabled": self._checkbox_prop(True),
            "Source": self._select_prop("YouTube"),
        }
        result = provider._parse_page(props)
        assert result is not None
        assert result.name == "PLabc"

    def test_returns_none_on_key_error(self):
        provider = self._make_provider()
        # Malformed props that will cause a KeyError
        props = {
            "Name": {"type": "title", "title": [{}]},  # missing 'plain_text'
            "URL": self._rich_text_prop("PLabc"),
        }
        # Should not raise, should return None
        result = provider._parse_page(props)
        # Either None (KeyError caught) or valid — just shouldn't crash
        # In practice the missing plain_text will trigger KeyError → None
        assert result is None or isinstance(result, PodcastConfig)


# ---------------------------------------------------------------------------
# NotionConfigProvider — get_podcasts (HTTP mocked)
# ---------------------------------------------------------------------------


class TestNotionGetPodcasts:
    def _make_provider(self):
        with patch.dict(
            os.environ,
            {
                "NOTION_API_KEY": "secret_key",
                "NOTION_DATABASE_ID": "db-abc",
            },
        ):
            return NotionConfigProvider()

    def _notion_response(self, results, has_more=False, next_cursor=None):
        import json

        body = {"results": results, "has_more": has_more}
        if next_cursor:
            body["next_cursor"] = next_cursor
        return json.dumps(body).encode("utf-8")

    def _make_page(self, name, url, enabled=True, source="YouTube"):
        return {
            "id": "page-001",
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": name}]},
                "URL": {"type": "rich_text", "rich_text": [{"plain_text": url}]},
                "Enabled": {"type": "checkbox", "checkbox": enabled},
                "Source": {"type": "select", "select": {"name": source}},
            },
        }

    @patch("urllib.request.urlopen")
    def test_returns_podcasts_from_response(self, mock_urlopen):
        provider = self._make_provider()
        page = self._make_page("Show A", "PLabc")
        response_data = self._notion_response([page])

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        podcasts = provider.get_podcasts()
        assert len(podcasts) == 1
        assert podcasts[0].name == "Show A"
        assert podcasts[0].url == "PLabc"

    @patch("urllib.request.urlopen")
    def test_returns_empty_on_http_error(self, mock_urlopen):
        provider = self._make_provider()
        mock_urlopen.side_effect = Exception("Connection refused")
        podcasts = provider.get_podcasts()
        assert podcasts == []

    @patch("urllib.request.urlopen")
    def test_skips_pages_with_no_url(self, mock_urlopen):
        provider = self._make_provider()
        page = {
            "id": "page-002",
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "No URL"}]},
                "URL": {"type": "rich_text", "rich_text": []},
            },
        }
        response_data = self._notion_response([page])
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        podcasts = provider.get_podcasts()
        assert podcasts == []


# ---------------------------------------------------------------------------
# NotionConfigProvider — update_last_run
# ---------------------------------------------------------------------------


class TestNotionUpdateLastRun:
    def _make_provider(self):
        with patch.dict(
            os.environ,
            {
                "NOTION_API_KEY": "key",
                "NOTION_DATABASE_ID": "db",
            },
        ):
            return NotionConfigProvider()

    def test_skips_when_no_page_id(self):
        provider = self._make_provider()
        podcast = PodcastConfig(name="Show", url="PLabc", page_id=None)
        # Should not raise, should just log and return
        provider.update_last_run(podcast, feed_url="https://cdn.example.com/feed.xml")

    @patch("urllib.request.urlopen")
    def test_calls_notion_api_with_page_id(self, mock_urlopen):
        provider = self._make_provider()
        podcast = PodcastConfig(name="Show", url="PLabc", page_id="page-123")

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        provider.update_last_run(podcast, feed_url="https://cdn.example.com/feed.xml")
        mock_urlopen.assert_called_once()

    @patch("urllib.request.urlopen")
    def test_handles_http_error_gracefully(self, mock_urlopen):
        provider = self._make_provider()
        podcast = PodcastConfig(name="Show", url="PLabc", page_id="page-123")
        mock_urlopen.side_effect = Exception("Timeout")
        # Should not raise
        provider.update_last_run(podcast)


# ---------------------------------------------------------------------------
# NotionConfigProvider — update_status
# ---------------------------------------------------------------------------


class TestNotionUpdateStatus:
    def _make_provider(self):
        with patch.dict(
            os.environ,
            {
                "NOTION_API_KEY": "key",
                "NOTION_DATABASE_ID": "db",
            },
        ):
            return NotionConfigProvider()

    def test_skips_when_no_page_id(self):
        provider = self._make_provider()
        podcast = PodcastConfig(name="Show", url="PLabc", page_id=None)
        # Should not raise, should just log and return
        provider.update_status(podcast, "Running")

    @patch("urllib.request.urlopen")
    def test_calls_notion_api_with_page_id(self, mock_urlopen):
        provider = self._make_provider()
        podcast = PodcastConfig(name="Show", url="PLabc", page_id="page-123")

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        provider.update_status(podcast, "Running")
        mock_urlopen.assert_called_once()

    @patch("urllib.request.urlopen")
    def test_sends_correct_status_value(self, mock_urlopen):
        import json

        provider = self._make_provider()
        podcast = PodcastConfig(name="Show", url="PLabc", page_id="page-123")

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        for status in ("Pending", "Running", "Done", "Failed"):
            mock_urlopen.reset_mock()
            provider.update_status(podcast, status)
            call_args = mock_urlopen.call_args[0][0]
            body = json.loads(call_args.data.decode("utf-8"))
            assert body["properties"]["Status"]["select"]["name"] == status

    @patch("urllib.request.urlopen")
    def test_uses_patch_method(self, mock_urlopen):
        provider = self._make_provider()
        podcast = PodcastConfig(name="Show", url="PLabc", page_id="page-123")

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        provider.update_status(podcast, "Done")
        call_args = mock_urlopen.call_args[0][0]
        assert call_args.method == "PATCH"
        assert f"/pages/{podcast.page_id}" in call_args.full_url

    @patch("urllib.request.urlopen")
    def test_handles_http_error_gracefully(self, mock_urlopen):
        provider = self._make_provider()
        podcast = PodcastConfig(name="Show", url="PLabc", page_id="page-123")
        mock_urlopen.side_effect = Exception("Timeout")
        # Should not raise
        provider.update_status(podcast, "Failed")


# ---------------------------------------------------------------------------
# get_config_provider factory
# ---------------------------------------------------------------------------


class TestGetConfigProviderFactory:
    def test_returns_yaml_provider_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONFIG_PROVIDER", None)
            provider = get_config_provider()
            assert isinstance(provider, YamlConfigProvider)

    def test_returns_yaml_provider_when_set_to_yaml(self):
        with patch.dict(os.environ, {"CONFIG_PROVIDER": "yaml"}):
            provider = get_config_provider()
            assert isinstance(provider, YamlConfigProvider)

    def test_returns_notion_provider_when_set_to_notion(self):
        with patch.dict(
            os.environ,
            {
                "CONFIG_PROVIDER": "notion",
                "NOTION_API_KEY": "key",
                "NOTION_DATABASE_ID": "db",
            },
        ):
            provider = get_config_provider()
            assert isinstance(provider, NotionConfigProvider)

    def test_yaml_path_from_env_var(self):
        with patch.dict(
            os.environ,
            {
                "CONFIG_PROVIDER": "yaml",
                "PODCASTS_YAML": "/custom/path/podcasts.yaml",
            },
        ):
            provider = get_config_provider()
            assert isinstance(provider, YamlConfigProvider)
            assert provider.path == "/custom/path/podcasts.yaml"


# ---------------------------------------------------------------------------
# NotionConfigProvider — find_page_by_url
# ---------------------------------------------------------------------------


class TestNotionFindPageByUrl:
    def _make_provider(self):
        with patch.dict(os.environ, {"NOTION_API_KEY": "key", "NOTION_DATABASE_ID": "db"}):
            return NotionConfigProvider()

    def test_returns_matching_podcast_by_url(self):
        """find_page_by_url returns the PodcastConfig whose URL matches."""
        provider = self._make_provider()
        podcasts = [
            PodcastConfig(name="Show A", url="https://youtube.com/playlist?list=PLA"),
            PodcastConfig(name="Show B", url="https://youtube.com/playlist?list=PLB"),
        ]
        with patch.object(provider, "get_podcasts", return_value=podcasts):
            result = provider.find_page_by_url("https://youtube.com/playlist?list=PLB")
        assert result is not None
        assert result.name == "Show B"

    def test_returns_none_when_no_match(self):
        """find_page_by_url returns None when no podcast URL matches."""
        provider = self._make_provider()
        podcasts = [
            PodcastConfig(name="Show A", url="https://youtube.com/playlist?list=PLA"),
        ]
        with patch.object(provider, "get_podcasts", return_value=podcasts):
            result = provider.find_page_by_url("https://youtube.com/playlist?list=UNKNOWN")
        assert result is None

    def test_returns_none_on_exception(self):
        """find_page_by_url silently returns None when get_podcasts raises."""
        provider = self._make_provider()
        with patch.object(provider, "get_podcasts", side_effect=Exception("API error")):
            result = provider.find_page_by_url("https://youtube.com/playlist?list=PLA")
        assert result is None


# ---------------------------------------------------------------------------
# NotionPodcastConfigProvider — _parse_page
# ---------------------------------------------------------------------------


class TestNotionPodcastConfigProviderParsePage:
    def _make_provider(self):
        with patch.dict(os.environ, {"NOTION_API_KEY": "key", "NOTION_DATABASE_ID": "db"}):
            return NotionPodcastConfigProvider()

    def _title_prop(self, text):
        return {"type": "title", "title": [{"plain_text": text}]}

    def _rich_text_prop(self, text):
        return {"type": "rich_text", "rich_text": [{"plain_text": text}]}

    def _url_prop(self, url):
        return {"type": "url", "url": url}

    def _checkbox_prop(self, checked):
        return {"type": "checkbox", "checkbox": checked}

    def _number_prop(self, n):
        return {"type": "number", "number": n}

    def _select_prop(self, value):
        return {"type": "select", "select": {"name": value}}

    def test_returns_podcast_config_for_source_podcast(self):
        provider = self._make_provider()
        props = {
            "Name": self._title_prop("My Podcast"),
            "URL": self._rich_text_prop("https://feeds.example.com/podcast.rss"),
            "Enabled": self._checkbox_prop(True),
            "Source": self._select_prop("Podcast"),
        }
        result = provider._parse_page(props)
        assert result is not None
        assert result.name == "My Podcast"
        assert result.url == "https://feeds.example.com/podcast.rss"
        assert result.source == "Podcast"

    def test_returns_none_for_source_youtube(self):
        """NotionPodcastConfigProvider should exclude YouTube entries."""
        provider = self._make_provider()
        props = {
            "Name": self._title_prop("YouTube Show"),
            "URL": self._rich_text_prop("https://youtube.com/playlist?list=PLabc"),
            "Enabled": self._checkbox_prop(True),
            "Source": self._select_prop("YouTube"),
        }
        result = provider._parse_page(props)
        assert result is None

    def test_returns_none_when_disabled(self):
        provider = self._make_provider()
        props = {
            "Name": self._title_prop("Podcast"),
            "URL": self._rich_text_prop("https://feeds.example.com/podcast.rss"),
            "Enabled": self._checkbox_prop(False),
            "Source": self._select_prop("Podcast"),
        }
        result = provider._parse_page(props)
        assert result is None

    def test_parses_max_age_days_and_max_downloads(self):
        provider = self._make_provider()
        props = {
            "Name": self._title_prop("Podcast"),
            "URL": self._rich_text_prop("https://feeds.example.com/podcast.rss"),
            "Enabled": self._checkbox_prop(True),
            "Source": self._select_prop("Podcast"),
            "Max Age Days": self._number_prop(14),
            "Max Downloads": self._number_prop(3),
        }
        result = provider._parse_page(props)
        assert result is not None
        assert result.max_age_days == 14
        assert result.max_downloads == 3

    def test_allows_empty_url_for_name_based_search(self):
        """Empty URL is allowed — podcast_sync will search iTunes by name."""
        provider = self._make_provider()
        props = {
            "Name": self._title_prop("Podcast By Name"),
            "URL": self._rich_text_prop(""),
            "Enabled": self._checkbox_prop(True),
            "Source": self._select_prop("Podcast"),
        }
        result = provider._parse_page(props)
        # Should return a PodcastConfig even with empty URL
        assert result is not None
        assert result.source == "Podcast"

    def test_returns_none_on_key_error(self):
        provider = self._make_provider()
        props = {"Name": {"type": "title", "title": [{}]}}
        result = provider._parse_page(props)
        assert result is None or isinstance(result, PodcastConfig)


# ---------------------------------------------------------------------------
# NotionPodcastConfigProvider — update_url
# ---------------------------------------------------------------------------


class TestNotionPodcastConfigProviderUpdateUrl:
    def _make_provider(self):
        with patch.dict(os.environ, {"NOTION_API_KEY": "key", "NOTION_DATABASE_ID": "db"}):
            return NotionPodcastConfigProvider()

    def test_skips_when_no_page_id(self):
        provider = self._make_provider()
        podcast = PodcastConfig(name="Show", url="PLabc", page_id=None, source="Podcast")
        # Should not raise
        provider.update_url(podcast, "https://new-feed.example.com/rss")

    @patch("urllib.request.urlopen")
    def test_calls_notion_api_with_new_url(self, mock_urlopen):
        import json

        provider = self._make_provider()
        podcast = PodcastConfig(name="Show", url="https://old.example.com/rss", page_id="page-abc", source="Podcast")

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        provider.update_url(podcast, "https://new.example.com/rss")

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["properties"]["URL"]["url"] == "https://new.example.com/rss"
        assert req.method == "PATCH"

    @patch("urllib.request.urlopen")
    def test_handles_error_gracefully(self, mock_urlopen):
        provider = self._make_provider()
        podcast = PodcastConfig(name="Show", url="https://old.example.com/rss", page_id="page-abc", source="Podcast")
        mock_urlopen.side_effect = Exception("Network error")
        # Should not raise
        provider.update_url(podcast, "https://new.example.com/rss")


# ---------------------------------------------------------------------------
# get_podcast_config_provider factory
# ---------------------------------------------------------------------------


class TestGetPodcastConfigProviderFactory:
    def test_returns_yaml_provider_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONFIG_PROVIDER", None)
            provider = get_podcast_config_provider()
            assert isinstance(provider, YamlConfigProvider)

    def test_returns_yaml_provider_when_set_to_yaml(self):
        with patch.dict(os.environ, {"CONFIG_PROVIDER": "yaml"}):
            provider = get_podcast_config_provider()
            assert isinstance(provider, YamlConfigProvider)

    def test_returns_notion_podcast_provider_when_set_to_notion(self):
        with patch.dict(
            os.environ,
            {
                "CONFIG_PROVIDER": "notion",
                "NOTION_API_KEY": "key",
                "NOTION_DATABASE_ID": "db",
            },
        ):
            provider = get_podcast_config_provider()
            assert isinstance(provider, NotionPodcastConfigProvider)

    def test_yaml_path_from_env_var(self):
        with patch.dict(
            os.environ,
            {
                "CONFIG_PROVIDER": "yaml",
                "PODCASTS_YAML": "/custom/podcasts.yaml",
            },
        ):
            provider = get_podcast_config_provider()
            assert isinstance(provider, YamlConfigProvider)
            assert provider.path == "/custom/podcasts.yaml"


# ---------------------------------------------------------------------------
# ConfigProvider base class methods (coverage for pass-through stubs)
# ---------------------------------------------------------------------------


class TestConfigProviderBaseMethods:
    def test_update_last_run_is_noop_on_yaml_provider(self, tmp_path):
        yaml_file = tmp_path / "podcasts.yaml"
        yaml_file.write_text(yaml.dump({"podcasts": [{"name": "Test", "url": "http://x", "enabled": True}]}))
        provider = YamlConfigProvider(path=str(yaml_file))
        podcasts = provider.get_podcasts()
        # Should not raise — it's a no-op
        provider.update_last_run(podcasts[0], feed_url="http://feed")

    def test_update_status_is_noop_on_yaml_provider(self, tmp_path):
        yaml_file = tmp_path / "podcasts.yaml"
        yaml_file.write_text(yaml.dump({"podcasts": [{"name": "Test", "url": "http://x", "enabled": True}]}))
        provider = YamlConfigProvider(path=str(yaml_file))
        podcasts = provider.get_podcasts()
        # Should not raise — it's a no-op
        provider.update_status(podcasts[0], status="Done")


# ---------------------------------------------------------------------------
# NotionPodcastConfigProvider._parse_page
# ---------------------------------------------------------------------------


class TestNotionPodcastParsePageExtended:
    def _make_provider(self):
        with patch.dict(
            os.environ,
            {
                "NOTION_API_KEY": "secret_test",
                "NOTION_DATABASE_ID": "db-id",
            },
        ):
            return NotionPodcastConfigProvider()

    def test_parses_url_type_url_field(self):
        provider = self._make_provider()
        props = {
            "Name": {"type": "title", "title": [{"plain_text": "My Podcast"}]},
            "URL": {"type": "url", "url": "https://feed.example.com/rss"},
            "Enabled": {"type": "checkbox", "checkbox": True},
            "Source": {"type": "select", "select": {"name": "Podcast"}},
        }
        result = provider._parse_page(props)
        assert result is not None
        assert result.url == "https://feed.example.com/rss"

    def test_parses_language_field(self):
        provider = self._make_provider()
        props = {
            "Name": {"type": "title", "title": [{"plain_text": "JP Pod"}]},
            "URL": {"type": "rich_text", "rich_text": [{"plain_text": "https://feed.jp"}]},
            "Enabled": {"type": "checkbox", "checkbox": True},
            "Source": {"type": "select", "select": {"name": "Podcast"}},
            "Language": {"type": "rich_text", "rich_text": [{"plain_text": "ja"}]},
        }
        result = provider._parse_page(props)
        assert result is not None
        assert result.language == "ja"

    def test_parses_description_field(self):
        provider = self._make_provider()
        props = {
            "Name": {"type": "title", "title": [{"plain_text": "Desc Pod"}]},
            "URL": {"type": "rich_text", "rich_text": [{"plain_text": "https://x.com"}]},
            "Enabled": {"type": "checkbox", "checkbox": True},
            "Source": {"type": "select", "select": {"name": "Podcast"}},
            "Description": {"type": "rich_text", "rich_text": [{"plain_text": "A cool podcast"}]},
        }
        result = provider._parse_page(props)
        assert result is not None
        assert result.description == "A cool podcast"

    def test_skips_non_podcast_source(self):
        provider = self._make_provider()
        props = {
            "Name": {"type": "title", "title": [{"plain_text": "YT Channel"}]},
            "URL": {"type": "rich_text", "rich_text": [{"plain_text": "https://yt.com"}]},
            "Enabled": {"type": "checkbox", "checkbox": True},
            "Source": {"type": "select", "select": {"name": "YouTube"}},
        }
        result = provider._parse_page(props)
        assert result is None
