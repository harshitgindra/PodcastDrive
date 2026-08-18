"""Tests for the environment-variable registry in :mod:`settings`.

Two of these classes are guards rather than unit tests:

* :class:`TestRegistryCoversEverySettingReadInSrc` scans ``src/`` for env-var
  reads and fails when one is not declared.  Without it the registry silently
  goes stale, which is exactly how 32 knobs came to be undocumented.
* :class:`TestGeneratedExampleIsCommitted` fails when ``config.env.example``
  does not match what the registry renders, so the checked-in file cannot drift
  from the code again.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


class TestRegistryShape:
    def test_every_declaration_is_reachable_by_name(self):
        for setting in settings._DECLARATIONS:
            assert settings.REGISTRY[setting.name] is setting

    def test_names_matches_registry_keys(self):
        assert settings.names() == frozenset(settings.REGISTRY)

    def test_no_duplicate_names(self):
        declared = [s.name for s in settings._DECLARATIONS]
        assert len(declared) == len(set(declared))

    def test_sections_partition_the_declarations(self):
        from_sections = [s for _, group in settings.SECTIONS for s in group]
        assert from_sections == list(settings._DECLARATIONS)

    def test_every_setting_has_a_sentence_of_documentation(self):
        for setting in settings._DECLARATIONS:
            assert len(setting.doc) > 20, setting.name
            assert setting.doc[0].isupper() or setting.doc[0] in "\"'", setting.name

    def test_default_type_matches_declared_kind(self):
        expected = {"str": str, "int": int, "float": float, "bool": bool}
        for setting in settings._DECLARATIONS:
            # bool is a subclass of int, so check bool first via exact type.
            assert type(setting.default) is expected[setting.kind], setting.name

    def test_secrets_default_to_empty(self):
        for setting in settings._DECLARATIONS:
            if setting.secret:
                assert setting.default == "", setting.name

    def test_settings_are_immutable(self):
        setting = settings.REGISTRY["S3_BUCKET"]
        with pytest.raises(AttributeError):
            setting.default = "other"  # frozen dataclass


# ---------------------------------------------------------------------------
# get(): lookup and coercion
# ---------------------------------------------------------------------------


class TestUnregisteredNamesFailLoudly:
    """A typo'd knob must not silently read as unset."""

    def test_unknown_name_raises_keyerror(self):
        with pytest.raises(KeyError) as exc:
            settings.get("AD_SNAP_TO_SILENC")
        assert "not a declared setting" in str(exc.value)

    def test_unknown_name_raises_even_when_the_variable_is_set(self, monkeypatch):
        monkeypatch.setenv("TOTALLY_MADE_UP_KNOB", "1")
        with pytest.raises(KeyError):
            settings.get("TOTALLY_MADE_UP_KNOB")


class TestReadsAreLazy:
    """Values must be resolved per call, not snapshotted at import.

    ``run.sh`` exports configuration progressively and the test suite mutates
    the environment mid-test, so a cached snapshot would be wrong in both.
    """

    def test_value_reflects_a_later_setenv(self, monkeypatch):
        monkeypatch.delenv("MP3_QUALITY", raising=False)
        assert settings.get("MP3_QUALITY") == 192
        monkeypatch.setenv("MP3_QUALITY", "320")
        assert settings.get("MP3_QUALITY") == 320
        monkeypatch.setenv("MP3_QUALITY", "128")
        assert settings.get("MP3_QUALITY") == 128


class TestStringCoercion:
    def test_unset_yields_the_declared_default(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIBE_LANGUAGE_CODE", raising=False)
        assert settings.get("TRANSCRIBE_LANGUAGE_CODE") == "en-US"

    def test_explicit_empty_string_is_honoured_not_replaced_by_the_default(self, monkeypatch):
        # FEED_TITLE_SUFFIX="" documents "no suffix"; falling back to the default
        # would re-add the scissors emoji the operator just removed.
        monkeypatch.setenv("FEED_TITLE_SUFFIX", "")
        assert settings.get("FEED_TITLE_SUFFIX") == ""

    def test_value_is_not_stripped(self, monkeypatch):
        # The default itself is " ✂️" — a significant leading space.
        monkeypatch.setenv("FEED_TITLE_SUFFIX", "  spaced  ")
        assert settings.get("FEED_TITLE_SUFFIX") == "  spaced  "

    def test_default_suffix_keeps_its_leading_space(self, monkeypatch):
        monkeypatch.delenv("FEED_TITLE_SUFFIX", raising=False)
        assert settings.get("FEED_TITLE_SUFFIX").startswith(" ")


class TestIntCoercion:
    def test_parses_a_valid_value(self, monkeypatch):
        monkeypatch.setenv("MAX_DOWNLOADS_PER_RUN", "25")
        assert settings.get("MAX_DOWNLOADS_PER_RUN") == 25

    def test_surrounding_whitespace_is_tolerated(self, monkeypatch):
        monkeypatch.setenv("MAX_DOWNLOADS_PER_RUN", "  25\n")
        assert settings.get("MAX_DOWNLOADS_PER_RUN") == 25

    def test_blank_value_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("MAX_DOWNLOADS_PER_RUN", "   ")
        assert settings.get("MAX_DOWNLOADS_PER_RUN") == 10

    def test_garbage_warns_and_falls_back_instead_of_raising(self, monkeypatch, caplog):
        monkeypatch.setenv("MAX_DOWNLOADS_PER_RUN", "ten")
        with caplog.at_level(logging.WARNING, logger="settings"):
            assert settings.get("MAX_DOWNLOADS_PER_RUN") == 10
        assert "MAX_DOWNLOADS_PER_RUN" in caplog.text
        assert "expected an integer" in caplog.text

    def test_float_string_for_an_int_setting_falls_back(self, monkeypatch):
        monkeypatch.setenv("MAX_DOWNLOADS_PER_RUN", "10.5")
        assert settings.get("MAX_DOWNLOADS_PER_RUN") == 10


class TestFloatCoercion:
    def test_parses_a_valid_value(self, monkeypatch):
        monkeypatch.setenv("MAX_AD_SEGMENT_SECS", "420.5")
        assert settings.get("MAX_AD_SEGMENT_SECS") == 420.5

    def test_integer_string_is_accepted(self, monkeypatch):
        monkeypatch.setenv("MAX_AD_SEGMENT_SECS", "420")
        assert settings.get("MAX_AD_SEGMENT_SECS") == 420.0

    def test_garbage_warns_and_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("MAX_AD_SEGMENT_SECS", "5 minutes")
        with caplog.at_level(logging.WARNING, logger="settings"):
            assert settings.get("MAX_AD_SEGMENT_SECS") == 300.0
        assert "expected a number" in caplog.text


class TestBoolCoercion:
    """One dialect for every boolean knob.

    The call sites previously used two incompatible dialects: default-true knobs
    tested ``not in ("false", "0", "no")`` and default-false knobs tested
    ``in ("true", "1", "yes")``.  ``REMOVE_ADS=off`` therefore left ad removal
    enabled, so a typo'd disable silently did nothing.
    """

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "True", "yes", "Y", "on", "t"])
    def test_truthy_spellings(self, monkeypatch, raw):
        monkeypatch.setenv("GENERATE_SUMMARIES", raw)
        assert settings.get("GENERATE_SUMMARIES") is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "False", "no", "N", "off", "f"])
    def test_falsey_spellings(self, monkeypatch, raw):
        monkeypatch.setenv("REMOVE_ADS", raw)
        assert settings.get("REMOVE_ADS") is False

    def test_off_now_disables_a_default_true_knob(self, monkeypatch):
        monkeypatch.setenv("REMOVE_ADS", "off")
        assert settings.get("REMOVE_ADS") is False

    def test_unrecognised_word_warns_and_keeps_the_default(self, monkeypatch, caplog):
        monkeypatch.setenv("REMOVE_ADS", "disabled")
        with caplog.at_level(logging.WARNING, logger="settings"):
            assert settings.get("REMOVE_ADS") is True
        assert "expected a boolean" in caplog.text

    def test_blank_keeps_the_default(self, monkeypatch):
        monkeypatch.setenv("REMOVE_ADS", "  ")
        assert settings.get("REMOVE_ADS") is True

    def test_true_and_false_word_sets_are_disjoint(self):
        assert not settings.TRUE_VALUES & settings.FALSE_VALUES


class TestCallerSuppliedDefault:
    """``MAX_AGE_DAYS`` means different things to the two pipelines.

    YouTube ignores episodes older than a week; the RSS pipeline has no age
    limit unless one is configured.  The name is shared because operators set it
    once in config.env, so the caller must be able to supply its own fallback.
    """

    def test_override_is_used_when_unset(self, monkeypatch):
        monkeypatch.delenv("MAX_AGE_DAYS", raising=False)
        assert settings.get("MAX_AGE_DAYS") == 7
        assert settings.get("MAX_AGE_DAYS", default=0) == 0

    def test_environment_wins_over_the_override(self, monkeypatch):
        monkeypatch.setenv("MAX_AGE_DAYS", "37")
        assert settings.get("MAX_AGE_DAYS", default=0) == 37

    def test_override_is_used_when_the_value_is_garbage(self, monkeypatch, caplog):
        monkeypatch.setenv("MAX_AGE_DAYS", "forever")
        with caplog.at_level(logging.WARNING, logger="settings"):
            assert settings.get("MAX_AGE_DAYS", default=0) == 0

    def test_a_none_override_is_distinguishable_from_no_override(self, monkeypatch):
        monkeypatch.delenv("HEALTH_ALERT_URL", raising=False)
        assert settings.get("HEALTH_ALERT_URL", default=None) is None
        assert settings.get("HEALTH_ALERT_URL") == ""


# ---------------------------------------------------------------------------
# Drift guards
# ---------------------------------------------------------------------------

#: Env vars read in ``src/`` that are not pipeline configuration.
_NOT_CONFIGURATION = frozenset(
    {
        "PATH",  # mutated to put the venv's ffmpeg first
        "XDG_CACHE_HOME",  # OS convention, honoured not configured
        "HOME",
    }
)

_READ_PATTERNS = (
    re.compile(r"""os\.environ(?:\.get)?\(\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
    re.compile(r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]{2,})["']\s*\]"""),
    re.compile(r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
    re.compile(r"""\benv_(?:int|float)\(\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
    re.compile(r"""\b_ffmpeg_timeout\(\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
    re.compile(r"""\bsettings\.get\(\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
)


def _env_names_read_in(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    found: set[str] = set()
    for pattern in _READ_PATTERNS:
        found.update(pattern.findall(text))
    return found - _NOT_CONFIGURATION


def _pipeline_modules() -> list[Path]:
    # src/mediasync/ has its own typed Config dataclass and its own
    # mediasync.env.example, so it is deliberately out of this registry.
    return [
        p
        for p in sorted(SRC.rglob("*.py"))
        if "mediasync" not in p.parts and p.name != "settings.py"
    ]


class TestRegistryCoversEverySettingReadInSrc:
    def test_scan_finds_a_plausible_number_of_reads(self):
        # Non-vacuity guard: if the patterns stop matching, the test below
        # would pass trivially.
        found: set[str] = set()
        for module in _pipeline_modules():
            found |= _env_names_read_in(module)
        assert len(found) > 40, f"scanner found only {sorted(found)}"

    def test_every_name_read_in_src_is_declared(self):
        undeclared: dict[str, set[str]] = {}
        for module in _pipeline_modules():
            missing = _env_names_read_in(module) - settings.names()
            if missing:
                undeclared[module.name] = missing
        assert not undeclared, (
            "these environment variables are read but not declared in "
            f"settings._DECLARATIONS: {undeclared}"
        )


class TestGeneratedExampleIsCommitted:
    def test_committed_file_matches_the_registry(self):
        committed = (REPO_ROOT / "config.env.example").read_text(encoding="utf-8")
        assert committed == settings.render_example(), (
            "config.env.example is stale — run `make config-example`"
        )

    def test_required_settings_are_uncommented_so_a_copy_has_blanks_to_fill(self):
        rendered = settings.render_example()
        for setting in settings._DECLARATIONS:
            if setting.required and not setting.secret:
                assert f"\n{setting.name}=" in rendered, setting.name

    def test_optional_settings_are_commented_out_with_their_default(self):
        rendered = settings.render_example()
        assert "# MP3_QUALITY=192" in rendered
        assert "\nMP3_QUALITY=" not in rendered

    def test_booleans_render_as_true_or_false_not_python_repr(self):
        rendered = settings.render_example()
        assert "# REMOVE_ADS=true" in rendered
        assert "# GENERATE_SUMMARIES=false" in rendered
        assert "=True" not in rendered and "=False" not in rendered

    def test_secrets_render_without_a_value(self):
        rendered = settings.render_example()
        assert "# NOTION_API_KEY=\n" in rendered
        assert "# AWS_SECRET_ACCESS_KEY=\n" in rendered

    def test_internal_variables_are_not_offered_as_configuration(self):
        rendered = settings.render_example()
        for name in ("RUNNER", "TRIGGER", "HERALD_JOB_ID", "NOTIFY_RESULTS", "AWS_LAMBDA_FUNCTION_NAME"):
            assert name not in rendered, name

    def test_every_documented_line_wraps_reasonably(self):
        for line in settings.render_example().splitlines():
            assert len(line) <= 120, line


class TestRenderExampleHelpers:
    def test_format_default_renders_bools_lowercase(self):
        assert settings._format_default(settings.REGISTRY["REMOVE_ADS"]) == "true"
        assert settings._format_default(settings.REGISTRY["GENERATE_SUMMARIES"]) == "false"

    def test_format_default_renders_numbers_and_strings(self):
        assert settings._format_default(settings.REGISTRY["MP3_QUALITY"]) == "192"
        assert settings._format_default(settings.REGISTRY["CONFIG_PROVIDER"]) == "yaml"

    def test_wrap_doc_emits_comment_lines(self):
        lines = settings._wrap_doc("a " * 80)
        assert len(lines) > 1
        assert all(line.startswith("#") for line in lines)

    def test_wrap_doc_of_empty_text_emits_nothing(self):
        assert settings._wrap_doc("") == []

    def test_wrap_doc_keeps_an_overlong_single_word_on_its_own_line(self):
        word = "x" * 200
        assert settings._wrap_doc(word) == [f"# {word}"]


class TestCli:
    def test_prints_the_example_by_default(self, capsys):
        assert settings._main([]) == 0
        assert "# --- AWS ---" in capsys.readouterr().out

    def test_write_updates_the_file(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "src" / "settings.py"
        target.parent.mkdir()
        monkeypatch.setattr(settings, "__file__", str(target))
        assert settings._main(["--write"]) == 0
        written = (tmp_path / "config.env.example").read_text(encoding="utf-8")
        assert written == settings.render_example()
        assert "wrote" in capsys.readouterr().out

    def test_unset_sentinel_has_a_readable_repr(self):
        assert repr(settings._UNSET) == "<unset>"
