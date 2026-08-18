"""Tests for the per-source run orchestration lifted out of run.sh heredocs.

These lock down the contract that run.sh and the Herald notifier depend on:
notify-entry schema, Notion status transitions, stdout format and exit codes.
"""

import json
import sys
import types

import pytest

import orchestrator


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakePodcast:
    def __init__(self, name, url, enabled=True, max_downloads=5, max_age_days=7, sleep_between=1):
        self.name = name
        self.url = url
        self.enabled = enabled
        self.max_downloads = max_downloads
        self.max_age_days = max_age_days
        self.sleep_between = sleep_between


class FakeProvider:
    """Records status/last-run calls so transitions can be asserted in order."""

    def __init__(self, podcasts=None, find_result=None, raise_on_status=False):
        self._podcasts = podcasts or []
        self._find_result = find_result
        self._raise_on_status = raise_on_status
        self.statuses = []
        self.last_runs = []
        self.lookups = []

    def get_podcasts(self):
        return self._podcasts

    def find_page_by_url(self, playlist_id):
        self.lookups.append(playlist_id)
        return self._find_result

    def update_status(self, podcast, status):
        if self._raise_on_status:
            raise RuntimeError("notion down")
        self.statuses.append((podcast.name, status))

    def update_last_run(self, podcast, feed_url=None):
        self.last_runs.append((podcast.name, feed_url))


@pytest.fixture
def notify_file(tmp_path, monkeypatch):
    path = tmp_path / ".notify_results.json"
    path.write_text("[]")
    monkeypatch.setenv("NOTIFY_RESULTS", str(path))
    return path


def read_notify(path):
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# append_notify_entry
# ---------------------------------------------------------------------------
class TestAppendNotifyEntry:
    def test_appends_to_existing_array(self, notify_file):
        orchestrator.append_notify_entry({"name": "a"})
        orchestrator.append_notify_entry({"name": "b"})
        assert [e["name"] for e in read_notify(notify_file)] == ["a", "b"]

    def test_noop_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_RESULTS", raising=False)
        orchestrator.append_notify_entry({"name": "a"})  # must not raise

    def test_noop_when_env_empty(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_RESULTS", "")
        orchestrator.append_notify_entry({"name": "a"})

    def test_recovers_from_corrupt_file(self, tmp_path, monkeypatch):
        path = tmp_path / "n.json"
        path.write_text("{not json")
        monkeypatch.setenv("NOTIFY_RESULTS", str(path))
        orchestrator.append_notify_entry({"name": "a"})
        assert read_notify(path) == [{"name": "a"}]

    def test_recovers_from_missing_file(self, tmp_path, monkeypatch):
        path = tmp_path / "missing.json"
        monkeypatch.setenv("NOTIFY_RESULTS", str(path))
        orchestrator.append_notify_entry({"name": "a"})
        assert read_notify(path) == [{"name": "a"}]

    def test_unwritable_path_is_swallowed(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("NOTIFY_RESULTS", str(tmp_path / "nodir" / "n.json"))
        orchestrator.append_notify_entry({"name": "a"})
        assert "Could not write notify results" in caplog.text

    def test_explicit_path_beats_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTIFY_RESULTS", str(tmp_path / "env.json"))
        explicit = tmp_path / "explicit.json"
        orchestrator.append_notify_entry({"name": "a"}, notify_file=str(explicit))
        assert read_notify(explicit) == [{"name": "a"}]
        assert not (tmp_path / "env.json").exists()


# ---------------------------------------------------------------------------
# feed_url_for
# ---------------------------------------------------------------------------
class TestFeedUrlFor:
    def test_builds_url(self, monkeypatch):
        monkeypatch.setenv("CLOUDFRONT_BASE", "https://cdn.example.com")
        assert orchestrator.feed_url_for("PL123") == "https://cdn.example.com/PL123/feed.xml"

    def test_empty_without_identifier(self, monkeypatch):
        monkeypatch.setenv("CLOUDFRONT_BASE", "https://cdn.example.com")
        assert orchestrator.feed_url_for("") == ""

    def test_empty_without_base(self, monkeypatch):
        monkeypatch.delenv("CLOUDFRONT_BASE", raising=False)
        assert orchestrator.feed_url_for("PL123") == ""


# ---------------------------------------------------------------------------
# success_status — the drift that used to differ per mode
# ---------------------------------------------------------------------------
class TestSuccessStatus:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            ({}, "Done"),
            ({"new_episodes": 3}, "Done"),
            ({"bot_detected": True}, "Error: Bot Detection"),
            ({"bot_detected": False}, "Done"),
            ({"splice_failed": 2}, "Splice Failed"),
            ({"splice_failed": 0}, "Done"),
            # bot detection outranks splice failure
            ({"bot_detected": True, "splice_failed": 1}, "Error: Bot Detection"),
        ],
    )
    def test_mapping(self, result, expected):
        assert orchestrator.success_status(result) == expected


# ---------------------------------------------------------------------------
# _fallback_name / normalize_youtube_url
# ---------------------------------------------------------------------------
class TestNameAndUrlHelpers:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.youtube.com/@SomeChannel/videos", "SomeChannel"),
            ("https://www.youtube.com/@SomeChannel", "SomeChannel"),
            ("https://www.youtube.com/playlist?list=PL1", "https://www.youtube.com/playlist?list=PL1"),
        ],
    )
    def test_fallback_name(self, url, expected):
        assert orchestrator._fallback_name(url) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("@Chan", "https://www.youtube.com/@Chan/videos"),
            ("PL123", "https://www.youtube.com/playlist?list=PL123"),
            ("https://www.youtube.com/@Chan", "https://www.youtube.com/@Chan/videos"),
            ("https://www.youtube.com/@Chan/", "https://www.youtube.com/@Chan/videos"),
            ("https://www.youtube.com/@Chan/videos", "https://www.youtube.com/@Chan/videos"),
            ("https://www.youtube.com/playlist?list=PL1", "https://www.youtube.com/playlist?list=PL1"),
        ],
    )
    def test_normalize_youtube_url(self, raw, expected):
        assert orchestrator.normalize_youtube_url(raw) == expected


# ---------------------------------------------------------------------------
# run_one
# ---------------------------------------------------------------------------
class TestRunOne:
    def test_success_transitions_running_then_done(self, notify_file, monkeypatch):
        monkeypatch.setenv("CLOUDFRONT_BASE", "https://cdn.example.com")
        provider = FakeProvider()
        podcast = FakePodcast("Show", "PL1")

        result, ok = orchestrator.run_one(
            name="Show",
            identifier_key="playlist_id",
            pipeline=lambda: {"playlist_id": "PL1", "new_episodes": 2},
            provider=provider,
            podcast=podcast,
        )

        assert ok is True
        assert result == {"playlist_id": "PL1", "new_episodes": 2}
        assert provider.statuses == [("Show", "Running"), ("Show", "Done")]
        assert provider.last_runs == [
            ("Show", None),
            ("Show", "https://cdn.example.com/PL1/feed.xml"),
        ]

    def test_notify_entry_always_has_all_counters(self, notify_file):
        orchestrator.run_one(
            name="Show",
            identifier_key="playlist_id",
            pipeline=lambda: {"new_episodes": 2, "failed": 1},
        )
        assert read_notify(notify_file) == [
            {
                "name": "Show",
                "new_episodes": 2,
                "failed": 1,
                "unavailable": 0,
                "splice_failed": 0,
                "bot_detected": False,
            }
        ]

    def test_prints_result_json_indented(self, notify_file, capsys):
        orchestrator.run_one(
            name="Show",
            identifier_key="playlist_id",
            pipeline=lambda: {"new_episodes": 1},
        )
        assert capsys.readouterr().out == json.dumps({"new_episodes": 1}, indent=2) + "\n"

    def test_bot_detected_sets_bot_status(self, notify_file):
        provider = FakeProvider()
        podcast = FakePodcast("Show", "PL1")
        orchestrator.run_one(
            name="Show",
            identifier_key="playlist_id",
            pipeline=lambda: {"playlist_id": "PL1", "bot_detected": True},
            provider=provider,
            podcast=podcast,
        )
        assert provider.statuses[-1] == ("Show", "Error: Bot Detection")
        assert read_notify(notify_file)[0]["bot_detected"] is True

    def test_splice_failed_sets_splice_status(self, notify_file):
        provider = FakeProvider()
        podcast = FakePodcast("Show", "slug")
        orchestrator.run_one(
            name="Show",
            identifier_key="slug",
            pipeline=lambda: {"slug": "show", "splice_failed": 3},
            provider=provider,
            podcast=podcast,
        )
        assert provider.statuses[-1] == ("Show", "Splice Failed")

    def test_failure_marks_failed_and_records_error(self, notify_file, capsys):
        provider = FakeProvider()
        podcast = FakePodcast("Show", "PL1")

        def boom():
            raise RuntimeError("kaboom")

        result, ok = orchestrator.run_one(
            name="Show",
            identifier_key="playlist_id",
            pipeline=boom,
            provider=provider,
            podcast=podcast,
        )

        assert (result, ok) == (None, False)
        assert provider.statuses == [("Show", "Running"), ("Show", "Failed")]
        assert read_notify(notify_file) == [
            {"name": "Show", "new_episodes": 0, "failed": 0, "error": "kaboom"}
        ]
        assert "ERROR: kaboom" in capsys.readouterr().err

    def test_dry_run_skips_all_provider_writes(self, notify_file):
        provider = FakeProvider()
        podcast = FakePodcast("Show", "PL1")
        orchestrator.run_one(
            name="Show",
            identifier_key="playlist_id",
            pipeline=lambda: {"playlist_id": "PL1"},
            provider=provider,
            podcast=podcast,
            dry_run=True,
        )
        assert provider.statuses == []
        assert provider.last_runs == []
        assert len(read_notify(notify_file)) == 1

    def test_missing_podcast_skips_provider_writes(self, notify_file):
        provider = FakeProvider()
        orchestrator.run_one(
            name="Show",
            identifier_key="playlist_id",
            pipeline=lambda: {"playlist_id": "PL1"},
            provider=provider,
            podcast=None,
        )
        assert provider.statuses == []

    def test_provider_error_does_not_fail_the_run(self, notify_file, caplog):
        provider = FakeProvider(raise_on_status=True)
        podcast = FakePodcast("Show", "PL1")
        _, ok = orchestrator.run_one(
            name="Show",
            identifier_key="playlist_id",
            pipeline=lambda: {"playlist_id": "PL1"},
            provider=provider,
            podcast=podcast,
        )
        assert ok is True
        assert "Could not update provider status" in caplog.text

    def test_notify_name_overrides_display_name(self, notify_file):
        orchestrator.run_one(
            name="Display",
            identifier_key="playlist_id",
            pipeline=lambda: {},
            notify_name="Notify",
        )
        assert read_notify(notify_file)[0]["name"] == "Notify"

    def test_success_notify_name_wins_over_notify_name(self, notify_file):
        orchestrator.run_one(
            name="Display",
            identifier_key="playlist_id",
            pipeline=lambda: {"playlist_id": "PLX"},
            notify_name="Notify",
            success_notify_name=lambda r: r["playlist_id"],
        )
        assert read_notify(notify_file)[0]["name"] == "PLX"

    def test_success_notify_name_unused_on_failure(self, notify_file):
        def boom():
            raise RuntimeError("x")

        orchestrator.run_one(
            name="Display",
            identifier_key="playlist_id",
            pipeline=boom,
            notify_name="Notify",
            success_notify_name=lambda r: "never",
        )
        assert read_notify(notify_file)[0]["name"] == "Notify"


# ---------------------------------------------------------------------------
# run_url_target
# ---------------------------------------------------------------------------
def _patch_url_mode(monkeypatch, *, provider, process_playlist, extract="PL1"):
    """Install fake modules for run_url_target's late imports."""
    monkeypatch.setitem(
        sys.modules,
        "config_provider",
        types.SimpleNamespace(
            get_config_provider=lambda: provider,
            get_podcast_config_provider=lambda: provider,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "sync",
        types.SimpleNamespace(process_playlist=process_playlist),
    )

    def _extract(url):
        if isinstance(extract, Exception):
            raise extract
        return extract

    monkeypatch.setitem(
        sys.modules,
        "utils",
        types.SimpleNamespace(extract_playlist_id=_extract),
    )


class TestRunUrlTarget:
    def test_matched_notion_entry_uses_its_name(self, notify_file, monkeypatch):
        podcast = FakePodcast("Matched Show", "PL1")
        provider = FakeProvider(find_result=podcast)
        _patch_url_mode(
            monkeypatch,
            provider=provider,
            process_playlist=lambda url, dry_run=False: {"playlist_id": "PL1"},
        )

        assert orchestrator.run_url_target("https://yt/playlist?list=PL1", dry_run=False) is True
        assert provider.lookups == ["PL1"]
        assert provider.statuses == [("Matched Show", "Running"), ("Matched Show", "Done")]
        assert read_notify(notify_file)[0]["name"] == "Matched Show"

    def test_unmatched_success_is_named_after_playlist_id(self, notify_file, monkeypatch):
        provider = FakeProvider(find_result=None)
        _patch_url_mode(
            monkeypatch,
            provider=provider,
            process_playlist=lambda url, dry_run=False: {"playlist_id": "PLRESOLVED"},
        )
        orchestrator.run_url_target("https://www.youtube.com/@Chan/videos", dry_run=False)
        assert read_notify(notify_file)[0]["name"] == "PLRESOLVED"

    def test_unmatched_failure_is_named_after_url(self, notify_file, monkeypatch):
        provider = FakeProvider(find_result=None)

        def boom(url, dry_run=False):
            raise RuntimeError("nope")

        _patch_url_mode(monkeypatch, provider=provider, process_playlist=boom)
        assert orchestrator.run_url_target("https://www.youtube.com/@Chan/videos", dry_run=False) is False
        entry = read_notify(notify_file)[0]
        assert entry["name"] == "Chan"
        assert entry["error"] == "nope"

    def test_name_is_truncated_to_40_chars(self, notify_file, monkeypatch):
        provider = FakeProvider(find_result=None)
        long_id = "P" * 60
        _patch_url_mode(
            monkeypatch,
            provider=provider,
            process_playlist=lambda url, dry_run=False: {"playlist_id": long_id},
        )
        orchestrator.run_url_target("https://yt/playlist?list=x", dry_run=False)
        assert read_notify(notify_file)[0]["name"] == "P" * 40

    def test_dry_run_skips_notion_lookup(self, notify_file, monkeypatch):
        provider = FakeProvider(find_result=FakePodcast("Matched", "PL1"))
        _patch_url_mode(
            monkeypatch,
            provider=provider,
            process_playlist=lambda url, dry_run=False: {"playlist_id": "PL1"},
        )
        orchestrator.run_url_target("https://www.youtube.com/@Chan", dry_run=True)
        assert provider.lookups == []
        assert provider.statuses == []
        assert read_notify(notify_file)[0]["name"] == "PL1"

    def test_unparseable_url_skips_lookup(self, notify_file, monkeypatch):
        provider = FakeProvider(find_result=FakePodcast("Matched", "PL1"))
        _patch_url_mode(
            monkeypatch,
            provider=provider,
            process_playlist=lambda url, dry_run=False: {"playlist_id": "PL1"},
            extract=ValueError("bad url"),
        )
        orchestrator.run_url_target("not-a-url", dry_run=False)
        assert provider.lookups == []

    def test_lookup_failure_is_swallowed(self, notify_file, monkeypatch, caplog):
        class ExplodingProvider(FakeProvider):
            def find_page_by_url(self, playlist_id):
                raise RuntimeError("notion 503")

        provider = ExplodingProvider()
        _patch_url_mode(
            monkeypatch,
            provider=provider,
            process_playlist=lambda url, dry_run=False: {"playlist_id": "PL1"},
        )
        assert orchestrator.run_url_target("https://yt/playlist?list=PL1", dry_run=False) is True
        assert "Notion lookup failed" in caplog.text

    def test_provider_without_find_page_by_url(self, notify_file, monkeypatch):
        provider = types.SimpleNamespace(
            update_status=lambda *a: None, update_last_run=lambda *a, **k: None
        )
        _patch_url_mode(
            monkeypatch,
            provider=provider,
            process_playlist=lambda url, dry_run=False: {"playlist_id": "PL1"},
        )
        assert orchestrator.run_url_target("https://yt/playlist?list=PL1", dry_run=False) is True


# ---------------------------------------------------------------------------
# run_youtube_sources
# ---------------------------------------------------------------------------
class TestRunYoutubeSources:
    def test_processes_only_enabled_and_forwards_per_podcast_options(
        self, notify_file, monkeypatch, capsys
    ):
        calls = []

        def fake_process(url, **kwargs):
            calls.append((url, kwargs))
            return {"playlist_id": "PL1", "new_episodes": 1}

        provider = FakeProvider(
            podcasts=[
                FakePodcast("On", "@Chan", max_downloads=9, max_age_days=3, sleep_between=2),
                FakePodcast("Off", "PL2", enabled=False),
            ]
        )
        _patch_url_mode(monkeypatch, provider=provider, process_playlist=fake_process)

        assert orchestrator.run_youtube_sources(dry_run=False) is True
        assert calls == [
            (
                "https://www.youtube.com/@Chan/videos",
                {"max_downloads": 9, "max_age_days": 3, "sleep_between": 2, "dry_run": False},
            )
        ]
        out = capsys.readouterr().out
        assert "Found 1 enabled podcasts (of 2 total)" in out
        assert "[1/1] On" in out
        assert "=" * 50 in out
        assert [e["name"] for e in read_notify(notify_file)] == ["On"]

    def test_one_failure_does_not_stop_the_loop(self, notify_file, monkeypatch):
        def fake_process(url, **kwargs):
            if "bad" in url:
                raise RuntimeError("bad source")
            return {"playlist_id": "PL", "new_episodes": 0}

        provider = FakeProvider(
            podcasts=[FakePodcast("Bad", "bad"), FakePodcast("Good", "good")]
        )
        _patch_url_mode(monkeypatch, provider=provider, process_playlist=fake_process)

        assert orchestrator.run_youtube_sources(dry_run=False) is False
        names = [e["name"] for e in read_notify(notify_file)]
        assert names == ["Bad", "Good"]
        assert provider.statuses[-1] == ("Good", "Done")

    def test_no_enabled_sources(self, notify_file, monkeypatch, capsys):
        provider = FakeProvider(podcasts=[FakePodcast("Off", "PL", enabled=False)])
        _patch_url_mode(monkeypatch, provider=provider, process_playlist=lambda *a, **k: {})
        assert orchestrator.run_youtube_sources(dry_run=False) is True
        assert "Found 0 enabled podcasts (of 1 total)" in capsys.readouterr().out

    def test_provider_failure_propagates(self, monkeypatch):
        class Broken(FakeProvider):
            def get_podcasts(self):
                raise RuntimeError("notion unreachable")

        _patch_url_mode(monkeypatch, provider=Broken(), process_playlist=lambda *a, **k: {})
        with pytest.raises(RuntimeError, match="notion unreachable"):
            orchestrator.run_youtube_sources(dry_run=False)


# ---------------------------------------------------------------------------
# run_rss_sources
# ---------------------------------------------------------------------------
def _patch_rss_mode(monkeypatch, *, provider, process_feed):
    monkeypatch.setitem(
        sys.modules,
        "config_provider",
        types.SimpleNamespace(get_podcast_config_provider=lambda: provider),
    )
    monkeypatch.setitem(
        sys.modules,
        "podcast_sync",
        types.SimpleNamespace(process_podcast_feed=process_feed),
    )


class TestRunRssSources:
    def test_uses_slug_for_feed_url_and_rss_prefix(self, notify_file, monkeypatch, capsys):
        monkeypatch.setenv("CLOUDFRONT_BASE", "https://cdn.example.com")
        seen = []

        def fake_feed(podcast, provider=None, dry_run=False):
            seen.append((podcast.name, provider is not None, dry_run))
            return {"slug": "my-show", "new_episodes": 4}

        provider = FakeProvider(podcasts=[FakePodcast("RSS Show", "https://feed")])
        _patch_rss_mode(monkeypatch, provider=provider, process_feed=fake_feed)

        assert orchestrator.run_rss_sources(dry_run=False) is True
        assert seen == [("RSS Show", True, False)]
        assert provider.last_runs[-1] == ("RSS Show", "https://cdn.example.com/my-show/feed.xml")
        out = capsys.readouterr().out
        assert "Found 1 enabled RSS podcast feeds (of 1 total)" in out
        assert "[RSS 1/1] RSS Show" in out

    def test_splice_failure_status(self, notify_file, monkeypatch):
        provider = FakeProvider(podcasts=[FakePodcast("RSS Show", "https://feed")])
        _patch_rss_mode(
            monkeypatch,
            provider=provider,
            process_feed=lambda p, provider=None, dry_run=False: {
                "slug": "s",
                "splice_failed": 1,
            },
        )
        orchestrator.run_rss_sources(dry_run=False)
        assert provider.statuses[-1] == ("RSS Show", "Splice Failed")
        assert read_notify(notify_file)[0]["splice_failed"] == 1

    def test_failure_recorded(self, notify_file, monkeypatch):
        def boom(p, provider=None, dry_run=False):
            raise RuntimeError("feed 404")

        provider = FakeProvider(podcasts=[FakePodcast("RSS Show", "https://feed")])
        _patch_rss_mode(monkeypatch, provider=provider, process_feed=boom)
        assert orchestrator.run_rss_sources(dry_run=False) is False
        assert read_notify(notify_file)[0]["error"] == "feed 404"
        assert provider.statuses[-1] == ("RSS Show", "Failed")


# ---------------------------------------------------------------------------
# main / CLI contract
# ---------------------------------------------------------------------------
class TestMain:
    @pytest.fixture(autouse=True)
    def _silence_logging(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules,
            "logger_config",
            types.SimpleNamespace(setup_logging=lambda *a, **k: None),
        )

    def test_no_args_is_usage_error(self, capsys):
        assert orchestrator.main([]) == 2
        assert "usage: orchestrator" in capsys.readouterr().err

    def test_unknown_mode_is_usage_error(self, capsys):
        assert orchestrator.main(["bogus"]) == 2
        assert "unknown mode: bogus" in capsys.readouterr().err

    def test_urls_without_url_is_usage_error(self, capsys):
        assert orchestrator.main(["urls"]) == 2
        assert "usage: orchestrator urls" in capsys.readouterr().err

    def test_urls_mode_processes_each_url(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            orchestrator, "run_url_target", lambda url, dry_run: seen.append((url, dry_run)) or True
        )
        assert orchestrator.main(["urls", "a", "b"]) == 0
        assert seen == [("a", False), ("b", False)]

    def test_per_source_failure_still_exits_zero(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_url_target", lambda url, dry_run: False)
        assert orchestrator.main(["urls", "a"]) == 0

    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on"])
    def test_dry_run_env_is_honoured(self, monkeypatch, value):
        # run.sh exports "true"; the other spellings come from settings.get()'s
        # single boolean dialect. Widening only ever suppresses writes, so the
        # error direction is safe.
        monkeypatch.setenv("PODCAST_DRY_RUN", value)
        seen = []
        monkeypatch.setattr(
            orchestrator, "run_url_target", lambda url, dry_run: seen.append(dry_run) or True
        )
        orchestrator.main(["urls", "a"])
        assert seen == [True]

    @pytest.mark.parametrize("value", ["false", "", "no", "off", "0"])
    def test_falsey_values_do_not_enable_dry_run(self, monkeypatch, value):
        monkeypatch.setenv("PODCAST_DRY_RUN", value)
        seen = []
        monkeypatch.setattr(
            orchestrator, "run_url_target", lambda url, dry_run: seen.append(dry_run) or True
        )
        orchestrator.main(["urls", "a"])
        assert seen == [False]

    def test_youtube_mode(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            orchestrator, "run_youtube_sources", lambda dry_run: seen.append(dry_run) or True
        )
        assert orchestrator.main(["youtube"]) == 0
        assert seen == [False]

    def test_rss_mode(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            orchestrator, "run_rss_sources", lambda dry_run: seen.append(dry_run) or True
        )
        assert orchestrator.main(["rss"]) == 0
        assert seen == [False]

    def test_reads_sys_argv_when_argv_is_none(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["orchestrator", "youtube"])
        monkeypatch.setattr(orchestrator, "run_youtube_sources", lambda dry_run: True)
        assert orchestrator.main() == 0
