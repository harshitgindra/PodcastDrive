"""Unit tests for yt-dlp runtime capability wiring.

The JS challenge solver is what makes any YouTube audio format reachable, so the
defaults here are load-bearing: a regression means every download fails with
"Requested format is not available".
"""

import logging

import pytest

from ytdlp_runtime import (
    DEFAULT_REMOTE_COMPONENTS,
    FALLBACK_CLIENTS_ENV,
    PRIMARY_CLIENT_ENV,
    REMOTE_COMPONENTS_ENV,
    YtdlpLogAdapter,
    describe_client,
    describe_client_chain,
    fallback_player_clients,
    get_remote_components,
    inject_logger,
    inject_remote_components,
    player_client_args,
    player_client_chain,
    primary_player_client,
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


class TestFallbackPlayerClients:
    """Both download paths read the same knob, so it is tested once here."""

    def test_defaults_to_mweb_when_unset(self, monkeypatch):
        monkeypatch.delenv(FALLBACK_CLIENTS_ENV, raising=False)
        assert fallback_player_clients() == ["mweb"]

    def test_comma_separated_list_keeps_order(self, monkeypatch):
        monkeypatch.setenv(FALLBACK_CLIENTS_ENV, "tv,mweb")
        assert fallback_player_clients() == ["tv", "mweb"]

    def test_whitespace_and_empty_entries_are_stripped(self, monkeypatch):
        monkeypatch.setenv(FALLBACK_CLIENTS_ENV, " tv , , mweb ")
        assert fallback_player_clients() == ["tv", "mweb"]

    @pytest.mark.parametrize("value", ["", "   ", ",", " , "])
    def test_empty_value_disables_the_fallback(self, monkeypatch, value):
        monkeypatch.setenv(FALLBACK_CLIENTS_ENV, value)
        assert fallback_player_clients() == []


class TestPlayerClientArgs:
    def test_renders_extractor_args_for_subprocess_callers(self):
        assert player_client_args("mweb") == ["--extractor-args", "youtube:player_client=mweb"]


class TestPlayerClientChain:
    """The chain decides which client pays the first attempt.

    YouTube binds the client yt-dlp picks by itself to a per-video GVS PO token,
    so that attempt is now a guaranteed 403: it belongs at the end of the chain,
    not the front.
    """

    def test_defaults_to_mweb_first_then_the_yt_dlp_default(self, monkeypatch):
        monkeypatch.delenv(PRIMARY_CLIENT_ENV, raising=False)
        monkeypatch.delenv(FALLBACK_CLIENTS_ENV, raising=False)
        assert player_client_chain() == ["mweb", None]

    def test_empty_primary_restores_the_yt_dlp_default_first(self, monkeypatch):
        monkeypatch.setenv(PRIMARY_CLIENT_ENV, "")
        monkeypatch.setenv(FALLBACK_CLIENTS_ENV, "mweb")
        assert player_client_chain() == [None, "mweb"]

    def test_primary_is_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv(PRIMARY_CLIENT_ENV, "  mweb  ")
        monkeypatch.setenv(FALLBACK_CLIENTS_ENV, "")
        assert primary_player_client() == "mweb"
        assert player_client_chain() == ["mweb", None]

    def test_whitespace_only_primary_is_no_primary(self, monkeypatch):
        monkeypatch.setenv(PRIMARY_CLIENT_ENV, "   ")
        monkeypatch.setenv(FALLBACK_CLIENTS_ENV, "")
        assert primary_player_client() is None
        assert player_client_chain() == [None]

    def test_fallbacks_follow_the_primary_in_order(self, monkeypatch):
        monkeypatch.setenv(PRIMARY_CLIENT_ENV, "mweb")
        monkeypatch.setenv(FALLBACK_CLIENTS_ENV, "tv_simply,android")
        assert player_client_chain() == ["mweb", "tv_simply", "android", None]

    def test_primary_listed_again_as_a_fallback_is_not_retried(self, monkeypatch):
        monkeypatch.setenv(PRIMARY_CLIENT_ENV, "mweb")
        monkeypatch.setenv(FALLBACK_CLIENTS_ENV, "mweb,tv_simply")
        assert player_client_chain() == ["mweb", "tv_simply", None]

    def test_yt_dlp_default_always_stays_in_the_chain(self, monkeypatch):
        """A primary that becomes token-bound later must still have a way out."""
        monkeypatch.setenv(PRIMARY_CLIENT_ENV, "mweb")
        monkeypatch.setenv(FALLBACK_CLIENTS_ENV, "")
        assert None in player_client_chain()

    def test_chain_is_never_empty(self, monkeypatch):
        monkeypatch.setenv(PRIMARY_CLIENT_ENV, "")
        monkeypatch.setenv(FALLBACK_CLIENTS_ENV, "")
        assert player_client_chain() == [None]

    def test_clients_are_described_for_humans(self):
        assert describe_client("mweb") == "mweb"
        assert describe_client(None) == "yt-dlp default"
        assert describe_client_chain(["mweb", None]) == "mweb, yt-dlp default"


class TestYtdlpLogAdapter:
    """yt-dlp writes to stdout/stderr unless given a logger.

    Without one the run log carried bare ``ERROR:`` lines and Python tracebacks
    with no timestamp, module or video id, so a 403 the downloader recovered
    from read as an unhandled crash.
    """

    FORBIDDEN = "ERROR: unable to download video data: HTTP Error 403: Forbidden"

    @staticmethod
    def _adapter(context="vid-1"):
        return YtdlpLogAdapter(logging.getLogger("ytdlp_runtime"), context)

    def test_error_is_logged_at_error_with_context(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="ytdlp_runtime"):
            self._adapter().error("ERROR: something genuinely broke")

        record = caplog.records[-1]
        assert record.levelno == logging.ERROR
        assert record.getMessage() == "vid-1: yt-dlp: ERROR: something genuinely broke"

    def test_media_forbidden_error_is_demoted_to_debug(self, caplog):
        """The fallback logs its own warning and recovers; this is not an error."""
        with caplog.at_level(logging.DEBUG, logger="ytdlp_runtime"):
            self._adapter().error(self.FORBIDDEN)

        assert caplog.records[-1].levelno == logging.DEBUG
        assert "403" in caplog.text

    def test_warning_stays_a_warning(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="ytdlp_runtime"):
            self._adapter().warning("WARNING: nudging you")

        assert caplog.records[-1].levelno == logging.WARNING

    @pytest.mark.parametrize("method", ["debug", "info"])
    def test_progress_output_is_debug(self, caplog, method):
        with caplog.at_level(logging.DEBUG, logger="ytdlp_runtime"):
            getattr(self._adapter(), method)("[download] 42% of 10MiB")

        assert caplog.records[-1].levelno == logging.DEBUG

    def test_messages_are_stripped_and_contextless_without_a_context(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="ytdlp_runtime"):
            YtdlpLogAdapter(logging.getLogger("ytdlp_runtime")).debug("  padded\n")

        assert caplog.records[-1].getMessage() == "yt-dlp: padded"

    def test_writes_through_the_callers_logger(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="downloader"):
            YtdlpLogAdapter(logging.getLogger("downloader"), "vid-2").warning("hi")

        assert caplog.records[-1].name == "downloader"


class TestInjectLogger:
    def test_adds_an_adapter(self):
        opts = {"quiet": True}
        assert inject_logger(opts, "vid-1") is opts
        assert isinstance(opts["logger"], YtdlpLogAdapter)

    def test_respects_an_explicit_caller_logger(self):
        sentinel = object()
        opts = {"logger": sentinel}
        inject_logger(opts, "vid-1")
        assert opts["logger"] is sentinel
