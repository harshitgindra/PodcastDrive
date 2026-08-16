"""Unit tests for yt-dlp runtime capability wiring.

The JS challenge solver is what makes any YouTube audio format reachable, so the
defaults here are load-bearing: a regression means every download fails with
"Requested format is not available".
"""

import logging

import pytest

from ytdlp_runtime import (
    DEFAULT_REMOTE_COMPONENTS,
    REMOTE_COMPONENTS_ENV,
    get_remote_components,
    inject_remote_components,
)


class TestGetRemoteComponents:
    def test_defaults_to_ejs_github_when_unset(self, monkeypatch):
        monkeypatch.delenv(REMOTE_COMPONENTS_ENV, raising=False)
        assert get_remote_components() == ["ejs:github"]

    def test_default_constant_matches_documented_value(self):
        assert DEFAULT_REMOTE_COMPONENTS == "ejs:github"

    def test_single_override(self, monkeypatch):
        monkeypatch.setenv(REMOTE_COMPONENTS_ENV, "ejs:npm")
        assert get_remote_components() == ["ejs:npm"]

    def test_comma_separated_list(self, monkeypatch):
        monkeypatch.setenv(REMOTE_COMPONENTS_ENV, "ejs:github,ejs:npm")
        assert get_remote_components() == ["ejs:github", "ejs:npm"]

    def test_whitespace_and_empty_entries_are_stripped(self, monkeypatch):
        monkeypatch.setenv(REMOTE_COMPONENTS_ENV, " ejs:github , , ejs:npm ")
        assert get_remote_components() == ["ejs:github", "ejs:npm"]

    @pytest.mark.parametrize("value", ["", "   ", ",", " , "])
    def test_empty_value_disables_fetching(self, monkeypatch, value):
        monkeypatch.setenv(REMOTE_COMPONENTS_ENV, value)
        assert get_remote_components() == []


class TestInjectRemoteComponents:
    def test_adds_default_components(self, monkeypatch):
        monkeypatch.delenv(REMOTE_COMPONENTS_ENV, raising=False)
        opts = {"quiet": True}
        result = inject_remote_components(opts)
        assert result is opts, "must mutate in place and return the same dict"
        assert opts["remote_components"] == ["ejs:github"]

    def test_respects_explicit_caller_override(self, monkeypatch):
        monkeypatch.delenv(REMOTE_COMPONENTS_ENV, raising=False)
        opts = {"remote_components": []}
        inject_remote_components(opts)
        assert opts["remote_components"] == [], "an explicit opt-out must be preserved"

    def test_respects_explicit_non_empty_override(self, monkeypatch):
        monkeypatch.setenv(REMOTE_COMPONENTS_ENV, "ejs:github")
        opts = {"remote_components": ["ejs:npm"]}
        inject_remote_components(opts)
        assert opts["remote_components"] == ["ejs:npm"]

    def test_disabled_leaves_key_absent_and_warns(self, monkeypatch, caplog):
        monkeypatch.setenv(REMOTE_COMPONENTS_ENV, "")
        opts = {"quiet": True}
        with caplog.at_level(logging.WARNING, logger="ytdlp_runtime"):
            inject_remote_components(opts)
        assert "remote_components" not in opts
        assert "remote components disabled" in caplog.text

    def test_does_not_disturb_other_options(self, monkeypatch):
        monkeypatch.delenv(REMOTE_COMPONENTS_ENV, raising=False)
        opts = {"quiet": True, "cookiefile": "/tmp/cookies.txt"}
        inject_remote_components(opts)
        assert opts["quiet"] is True
        assert opts["cookiefile"] == "/tmp/cookies.txt"

    def test_values_are_supported_by_installed_ytdlp(self, monkeypatch):
        """Guard against yt-dlp renaming or dropping the component we depend on."""
        from yt_dlp.globals import supported_remote_components

        monkeypatch.delenv(REMOTE_COMPONENTS_ENV, raising=False)
        assert set(get_remote_components()) <= set(supported_remote_components.value)

    def test_option_name_accepted_by_installed_ytdlp(self, monkeypatch):
        """The injected key must be a real YoutubeDL parameter, not silently ignored."""
        import yt_dlp

        monkeypatch.delenv(REMOTE_COMPONENTS_ENV, raising=False)
        opts = {"quiet": True, "no_warnings": True}
        inject_remote_components(opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            assert ydl.params["remote_components"] == {"ejs:github"}
