"""Single declarative registry for every environment-variable knob.

Before this module the pipeline read configuration with 100+ scattered
``os.environ.get`` calls, which produced three concrete classes of defect:

1.  **Divergent defaults for one name.**  ``MAX_AGE_DAYS`` defaulted to 7 in
    :mod:`sync`, 0 in :mod:`podcast_sync`, and was documented as 5 in
    ``config.env.example``.  ``AWS_DEFAULT_REGION`` defaulted to ``us-east-1``
    in :mod:`ad_remover` but ``us-west-2`` in :mod:`preflight`, so preflight
    validated a different region than the sync then used.
2.  **Three incompatible boolean dialects.**  Default-true knobs tested
    ``value not in ("false", "0", "no")`` while default-false knobs tested
    ``value in ("true", "1", "yes")``.  ``REMOVE_ADS=off`` therefore left ad
    removal *enabled* -- a typo'd disable that silently did nothing.
3.  **Undocumented knobs.**  32 of the ~69 live settings appeared nowhere in
    ``config.env.example``, including operationally important ones such as
    ``MAX_AD_SEGMENT_SECS`` and ``SPLICE_MAX_ATTEMPTS_PER_RUN``.

Every setting is declared once in :data:`REGISTRY` with its type, default and
documentation, and read through :func:`get`.  Reads stay **lazy** -- the value
is resolved from ``os.environ`` on every call rather than snapshotted at import
-- because ``run.sh`` exports configuration progressively and the test suite
uses ``monkeypatch.setenv`` mid-test.

Coercion is forgiving by design: a malformed numeric or boolean value logs a
warning and yields the declared default instead of raising.  A configuration
typo should degrade to documented behaviour, never abort a run that has already
paid for downloads and transcription.

String settings are deliberately *not* subject to blank-means-default: an
explicitly empty ``FEED_TITLE_SUFFIX=`` means "no suffix", and an empty
``YTDLP_REMOTE_COMPONENTS=`` means "fetch nothing".  Values are returned
verbatim, without stripping, because ``FEED_TITLE_SUFFIX`` has a significant
leading space.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Final, Literal

_logger = logging.getLogger("settings")

Kind = Literal["str", "int", "float", "bool"]

#: Accepted spellings of true/false for :data:`Kind` ``"bool"`` settings.
#: One dialect for every boolean knob, unlike the three the call sites used.
TRUE_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "on", "t"})
FALSE_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "no", "n", "off", "f"})


@dataclass(frozen=True)
class Setting:
    """Declaration of one environment-variable knob.

    Attributes:
        name:      Environment variable name.
        kind:      How the raw string is coerced.
        default:   Value used when the variable is unset (or unparseable).
        doc:       Operator-facing description, rendered into config.env.example.
        secret:    True for credentials, which are never rendered with a value.
        internal:  True for variables supplied by the runtime or wrapper script
                   rather than configured by a human (``RUNNER``, ``TRIGGER``,
                   ``AWS_LAMBDA_FUNCTION_NAME``, ...).  Excluded from the
                   generated example file.
        required:  True for settings an operator must supply (the default is not
                   usable), which are rendered uncommented in config.env.example
                   so a fresh copy has a blank to fill in.  Everything else is
                   rendered commented out, documenting the default in place.
    """

    name: str
    kind: Kind
    default: Any
    doc: str
    secret: bool = False
    internal: bool = False
    required: bool = False


def _s(
    name: str,
    default: str,
    doc: str,
    *,
    secret: bool = False,
    internal: bool = False,
    required: bool = False,
) -> Setting:
    return Setting(name, "str", default, doc, secret=secret, internal=internal, required=required)


def _i(name: str, default: int, doc: str, *, internal: bool = False) -> Setting:
    return Setting(name, "int", default, doc, internal=internal)


def _f(name: str, default: float, doc: str, *, internal: bool = False) -> Setting:
    return Setting(name, "float", default, doc, internal=internal)


def _b(name: str, default: bool, doc: str, *, internal: bool = False) -> Setting:
    return Setting(name, "bool", default, doc, internal=internal)


SECTIONS: Final[tuple[tuple[str, tuple[Setting, ...]], ...]] = (
    (
        "AWS",
        (
            _s(
                "AWS_DEFAULT_REGION",
                "us-west-2",
                "AWS region for S3, Transcribe and Bedrock. Must match the bucket's region.",
                required=True,
            ),
            _f(
                "AWS_CONNECT_TIMEOUT",
                10.0,
                "Seconds to wait for a TCP connection to an AWS endpoint before retrying. "
                "botocore's own default is 60s, long enough for one blackholed SYN to stall a run.",
            ),
            _f(
                "AWS_READ_TIMEOUT",
                60.0,
                "Seconds to wait for an AWS response body. Do not lower: Bedrock ad detection "
                "and Transcribe polling legitimately run close to this ceiling.",
            ),
            _s("AWS_ACCESS_KEY_ID", "", "Static AWS key. Prefer SSO or ~/.aws profiles.", secret=True),
            _s("AWS_SECRET_ACCESS_KEY", "", "Static AWS secret. Prefer SSO or ~/.aws profiles.", secret=True),
            _s(
                "AWS_LAMBDA_FUNCTION_NAME",
                "",
                "Set by the Lambda runtime; its presence switches logging to stdout-only.",
                internal=True,
            ),
        ),
    ),
    (
        "Storage / CDN",
        (
            _s("S3_BUCKET", "", "S3 bucket holding audio, manifests, feeds and run history.", required=True),
            _s("CLOUDFRONT_BASE", "", "Public CloudFront base URL, e.g. https://d123.cloudfront.net.", required=True),
            _s(
                "CLOUDFRONT_DISTRIBUTION_ID",
                "",
                "CloudFront distribution to invalidate after a feed changes.",
                required=True,
            ),
        ),
    ),
    (
        "Config provider",
        (
            _s("CONFIG_PROVIDER", "yaml", 'Where podcast definitions come from: "yaml" or "notion".', required=True),
            _s("PODCASTS_YAML", "podcasts.yaml", "Path to the YAML config when CONFIG_PROVIDER=yaml."),
            _s("NOTION_API_KEY", "", "Notion integration token when CONFIG_PROVIDER=notion.", secret=True),
            _s("NOTION_DATABASE_ID", "", "Notion database id when CONFIG_PROVIDER=notion."),
        ),
    ),
    (
        "YouTube pipeline",
        (
            _i("MAX_DOWNLOADS_PER_RUN", 10, "Cap on new YouTube episodes downloaded per playlist per run."),
            _i(
                "MAX_AGE_DAYS",
                7,
                "Ignore episodes older than this many days. 0 means no age limit. "
                "The RSS pipeline defaults to 0 (no limit) when this is unset.",
            ),
            _i("SLEEP_BETWEEN_DOWNLOADS", 5, "Seconds to pause between YouTube downloads, to look less like a bot."),
            _i("MP3_QUALITY", 192, "MP3 encoding bitrate in kbps."),
            _i("DOWNLOAD_MAX_RETRIES", 3, "Attempts per download before giving up (transient failures only)."),
            _i("DOWNLOAD_RETRY_WAIT_MIN", 5, "Minimum back-off seconds between download attempts."),
            _i("DOWNLOAD_RETRY_WAIT_MAX", 60, "Maximum back-off seconds between download attempts."),
            _s("COOKIES_FILE", "cookies.txt", "Netscape cookies file giving yt-dlp a logged-in YouTube session."),
            _s(
                "YTDLP_COOKIES",
                "",
                "Explicit cookies-file path, overriding the search of COOKIES_FILE and the "
                "repo/home default locations.",
            ),
            _s(
                "YTDLP_REMOTE_COMPONENTS",
                "ejs:github",
                "Comma-separated yt-dlp remote components. The JavaScript n-challenge solver "
                'ships as an opt-in remote component; without it yt-dlp returns storyboard '
                'images only and every download fails with "Requested format is not available". '
                "Set to an empty string only if the solver cache is pre-seeded.",
            ),
            _i("RECONCILE_MIN_ORPHANS", 3, "Orphan count below which S3 reconciliation deletes nothing."),
            _f("RECONCILE_MAX_ORPHAN_RATIO", 0.5, "Refuse to reconcile when orphans exceed this fraction of objects."),
        ),
    ),
    (
        "RSS pipeline",
        (
            _i("PODCAST_MAX_EPISODES", 5, "Cap on new RSS episodes downloaded per feed per run."),
            _i(
                "PODCAST_EPISODE_WORKERS",
                1,
                "Episodes processed in parallel per feed (1 = sequential). 3 balances speed "
                "against AWS API rate limits; the work is IO-bound.",
            ),
            _i("MAX_SPLICE_RETRIES", 3, "Lifetime splice attempts per episode before it is abandoned."),
            _i("SPLICE_MAX_ATTEMPTS_PER_RUN", 2, "Splice retries attempted in any single run, across all episodes."),
            _i("MAX_FEED_BYTES", 32 * 1024 * 1024, "Refuse RSS feeds larger than this. The largest real feeds are ~5 MiB."),
            _i("MAX_ITUNES_BYTES", 8 * 1024 * 1024, "Refuse iTunes lookup/search responses larger than this."),
        ),
    ),
    (
        "Ad removal",
        (
            _b("REMOVE_ADS", True, "Master switch for Transcribe + Bedrock ad removal."),
            _b("REMOVE_ADS_DRY_RUN", False, "Detect and report ad segments without re-encoding the audio."),
            _b("TRIM_MUSIC_INTRO", False, "Also trim a music-only intro from the start of each episode."),
            _b("TRIM_MUSIC_OUTRO", False, "Also trim a music-only outro from the end of each episode."),
            _f("MUSIC_INTRO_MIN_SECS", 8.0, "Shortest music intro worth trimming, in seconds."),
            _f("MUSIC_OUTRO_MIN_SECS", 5.0, "Shortest music outro worth trimming, in seconds."),
            _b("AD_SNAP_TO_SILENCE", True, "Move cut points to the nearest detected silence so splices are inaudible."),
            _f("AD_MERGE_GAP_SECS", 2.0, "Merge two ad segments separated by less than this gap."),
            _f("MIN_AD_SEGMENT_SECS", 5.0, "Discard detected ad segments shorter than this."),
            _f("MAX_AD_SEGMENT_SECS", 300.0, "Discard detected ad segments longer than this (likely a detection error)."),
            _f("AD_VERIFY_THRESHOLD_SECS", 90.0, "Send segments longer than this to the second-pass verification model."),
            _i("AD_DETECT_MAX_CHARS", 60000, "Transcript characters per Bedrock detection request."),
            _f("AD_DETECT_OVERLAP_SECS", 60.0, "Overlap between consecutive detection chunks, so ads at a seam are seen."),
            _s(
                "AD_TRANSCRIBE_WINDOWS",
                "",
                'Comma-separated "start:end" second ranges to transcribe instead of the whole '
                'episode; "end" means total duration and "end-N" means total minus N. '
                'Example: 0:300,end-600:end.',
            ),
            _b(
                "SPLICE_LOUDNORM",
                True,
                "Apply EBU R128 loudness normalisation after splicing. Equalises loudness across "
                "kept intervals so cut points are inaudible; costs 10-20% more ffmpeg time.",
            ),
            _s("TRANSCRIBE_LANGUAGE_CODE", "en-US", "BCP-47 language code for AWS Transcribe."),
            _i("TRANSCRIBE_POLL_INTERVAL", 10, "Seconds between Transcribe job status polls."),
            _i("TRANSCRIBE_MAX_WAIT", 3600, "Seconds to wait for a Transcribe job before giving up."),
            _b("TRANSCRIBE_CACHE_ENABLED", True, "Reuse transcripts cached in S3 instead of paying to transcribe again."),
            _s("TRANSCRIBE_CACHE_PREFIX", "transcribe-cache", "S3 key prefix for cached transcripts."),
            _s(
                "BEDROCK_MODEL_ID",
                "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "Bedrock model for ad verification and episode summaries.",
            ),
            _s(
                "BEDROCK_DETECT_MODEL_ID",
                "",
                "Bedrock model for first-pass detection over every transcript chunk. Falls back to "
                "BEDROCK_MODEL_ID.",
            ),
            _b("EVALUATE_AD_REMOVAL", False, "Re-transcribe cleaned audio and report residual ads. Costs extra AWS spend."),
            _s("EVAL_REPORTS_DIR", "reports", "Directory for ad-removal evaluation reports."),
            _b("GENERATE_SUMMARIES", False, "Generate AI episode summaries via Bedrock (one extra call per episode)."),
            _i(
                "SUMMARY_MAX_DURATION_SECS",
                1800,
                "Skip summaries for episodes longer than this many seconds. 0 disables the guard.",
            ),
        ),
    ),
    (
        "ffmpeg / ffprobe timeouts",
        (
            # A hung child holds the S3 distributed lock (TTL 3600s), which silently blocks
            # every later cron run, so every invocation must be bounded.
            _f("FFMPEG_SILENCEDETECT_TIMEOUT_SECS", 1800.0, "Timeout for the ffmpeg silencedetect pass."),
            _f("FFMPEG_SPLICE_TIMEOUT_SECS", 3600.0, "Timeout for the ffmpeg splice/concat pass."),
            _f("FFMPEG_SEGMENT_TIMEOUT_SECS", 600.0, "Timeout for extracting a single audio segment."),
            _f("FFPROBE_TIMEOUT_SECS", 60.0, "Timeout for an ffprobe metadata probe."),
        ),
    ),
    (
        "Feed presentation",
        (
            _s(
                "FEED_TITLE_SUFFIX",
                " \u2702\ufe0f",
                "Suffix appended to the channel title in generated feeds, so managed feeds are "
                'visually distinct from the originals. Set to "" to disable.',
            ),
            _s(
                "FEED_SUBTITLE",
                "Ad-free \u00b7 PodcastDrive",
                'Secondary label shown under the title in Overcast and Apple Podcasts. Set to "" to disable.',
            ),
            _s(
                "EPISODE_AD_REMOVED_SUFFIX",
                " \u2702\ufe0f",
                'Suffix added to episode titles whose ads were removed. Set to "" to disable.',
            ),
        ),
    ),
    (
        "Logging",
        (
            _s("LOG_DIR", "./logs", "Directory for rotating log files (local runs only; ignored on Lambda)."),
            _s("LOG_LEVEL", "INFO", "Logging level: DEBUG, INFO, WARNING or ERROR."),
            _s("LOG_FORMAT", "", 'Set to "json" for structured logs; anything else gives human-readable lines.'),
            _i("LOG_RETENTION_DAYS", 30, "Daily log files to keep before the oldest is deleted."),
        ),
    ),
    (
        "Health alerting",
        (
            _s(
                "HEALTH_ALERT_URL",
                "",
                "Webhook POSTed when the health report finds HIGH-priority issues (Slack, ntfy, "
                "PagerDuty, ...). Empty disables alerting.",
            ),
        ),
    ),
    (
        "Run metadata, set by run.sh / Herald",
        (
            _s("RUNNER", "", "Identifier of the host or CI job executing the run.", internal=True),
            _s("TRIGGER", "manual", 'What started the run: "manual", "cron", "herald", ...', internal=True),
            _s("HERALD_JOB_ID", "", "Herald job id, used to route the completion notification.", internal=True),
            _s("NOTIFY_RESULTS", "", "Path to the JSON file each source appends its result summary to.", internal=True),
            _b(
                "PODCAST_DRY_RUN",
                False,
                "Set by run.sh --dry-run: report what would be downloaded without touching S3 "
                "or spending on AWS.",
                internal=True,
            ),
        ),
    ),
)

_DECLARATIONS: Final[tuple[Setting, ...]] = tuple(s for _, group in SECTIONS for s in group)

REGISTRY: Final[dict[str, Setting]] = {s.name: s for s in _DECLARATIONS}

assert len(REGISTRY) == len(_DECLARATIONS), "duplicate Setting name in _DECLARATIONS"


class _Unset:
    """Sentinel distinguishing "no override given" from an override of ``None``."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET: Final = _Unset()


def _coerce(setting: Setting, raw: str, default: Any) -> Any:
    """Convert *raw* per ``setting.kind``, falling back to *default* on garbage."""
    if setting.kind == "str":
        return raw

    stripped = raw.strip()
    if not stripped:
        return default

    if setting.kind == "bool":
        lowered = stripped.lower()
        if lowered in TRUE_VALUES:
            return True
        if lowered in FALSE_VALUES:
            return False
        _logger.warning(
            "Invalid %s=%r (expected a boolean such as true/false) - falling back to %s",
            setting.name,
            raw,
            default,
        )
        return default

    converter = int if setting.kind == "int" else float
    expected = "an integer" if setting.kind == "int" else "a number"
    try:
        return converter(stripped)
    except ValueError:
        _logger.warning(
            "Invalid %s=%r (expected %s) - falling back to %s",
            setting.name,
            raw,
            expected,
            default,
        )
        return default


def get(name: str, default: Any = _UNSET) -> Any:
    """Return the current value of registered setting *name*.

    Args:
        name:    A key of :data:`REGISTRY`.
        default: Overrides the registered default.  Reserved for the handful of
                 knobs whose sensible default is caller-specific -- ``MAX_AGE_DAYS``
                 differs between the YouTube and RSS pipelines, and
                 ``LOG_RETENTION_DAYS`` may be passed in by a caller.

    Returns:
        The coerced value, or the default when unset or unparseable.

    Raises:
        KeyError: If *name* is not declared in :data:`REGISTRY`.  This turns a
            typo'd knob into an immediate, loud failure instead of a silently
            ignored setting.
    """
    try:
        setting = REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"{name!r} is not a declared setting - add it to settings._DECLARATIONS "
            "so it appears in config.env.example and gets a typed default"
        ) from None

    effective_default = setting.default if isinstance(default, _Unset) else default
    raw = os.environ.get(name)
    if raw is None:
        return effective_default
    return _coerce(setting, raw, effective_default)


def names() -> frozenset[str]:
    """Return every declared setting name."""
    return frozenset(REGISTRY)


# ---------------------------------------------------------------------------
# config.env.example rendering
# ---------------------------------------------------------------------------

_EXAMPLE_HEADER = """\
# PodcastDrive — Configuration
#
# GENERATED FILE — do not edit by hand.
# Regenerate with:  make config-example   (or python -m settings --write)
# Every knob is declared in src/settings.py; that is the file to change.
#
# Copy this file to config.env and fill in your values.
# Do NOT commit config.env to version control.
#
# Lines are commented out where the built-in default is already the right
# answer. Uncomment and edit only what you need to change.
"""


_EXAMPLE_FOOTER = """\
# --- Run notifications (via Herald) ---
#
# Notifications are sent through Herald (https://github.com/harshitgindra/Herald).
# Install:   pipx install ~/Projects/Herald
# Configure: ~/.config/herald/config.yaml
# When Herald is not installed, notifications are silently skipped.
"""


def _format_default(setting: Setting) -> str:
    """Render *setting*'s default the way it would be written in config.env."""
    if setting.kind == "bool":
        return "true" if setting.default else "false"
    return str(setting.default)


def _wrap_doc(doc: str, width: int = 96) -> list[str]:
    """Wrap *doc* into ``# ``-prefixed comment lines."""
    words = doc.split()
    lines: list[str] = []
    current = "#"
    for word in words:
        candidate = f"{current} {word}"
        if len(candidate) > width and current != "#":
            lines.append(current)
            current = f"# {word}"
        else:
            current = candidate
    if current != "#":
        lines.append(current)
    return lines


def render_example() -> str:
    """Return the full text of ``config.env.example``.

    Secrets are rendered without a value and commented out; settings supplied by
    the runtime rather than a human (:attr:`Setting.internal`) are omitted
    entirely, since documenting ``RUNNER`` as something to configure would be
    misleading.
    """
    out: list[str] = [_EXAMPLE_HEADER.rstrip()]
    for title, group in SECTIONS:
        visible = [s for s in group if not s.internal]
        if not visible:
            continue
        out.append("")
        out.append(f"# --- {title} ---")
        for setting in visible:
            out.append("")
            out.extend(_wrap_doc(setting.doc))
            if setting.secret:
                out.append(f"# {setting.name}=")
            elif setting.required:
                out.append(f"{setting.name}={_format_default(setting)}")
            else:
                out.append(f"# {setting.name}={_format_default(setting)}")
    out.append("")
    out.append(_EXAMPLE_FOOTER.rstrip())
    return "\n".join(out) + "\n"


def _main(argv: list[str] | None = None) -> int:
    """Print or write ``config.env.example``.  Entry point for ``-m settings``."""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Render config.env.example from the settings registry")
    parser.add_argument("--write", action="store_true", help="write config.env.example instead of printing")
    args = parser.parse_args(argv)

    text = render_example()
    if args.write:
        target = Path(__file__).resolve().parent.parent / "config.env.example"
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target} ({len(REGISTRY)} settings declared)")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(_main())
