"""ffmpeg/ffprobe process helpers and silence/music detection.

This module holds the audio-analysis primitives that shell out to ffmpeg:
binary resolution, silence detection, cut-boundary snapping, and music
intro/outro detection.

Patched-symbol routing: ``detect_silence`` and ``_get_audio_duration`` are
rebound on the ``ad_remover`` package by the test suite, so callers here reach
them through the package namespace (``ad_remover.detect_silence``) rather than
as bare names — otherwise ``patch("ad_remover.detect_silence")`` would not be
seen by ``snap_ad_boundaries``/``detect_music_bookends``. ``subprocess`` is
imported normally: tests patch ``ad_remover.subprocess.run`` on the shared
module object, which every importer sees.
"""

from __future__ import annotations

import logging
import subprocess
import sys

import settings
from ad_remover._segments import AdSegment

logger = logging.getLogger("ad_remover")


def _pkg():
    """Return the live ``ad_remover`` package module.

    Resolved from ``sys.modules`` at call time rather than cached via
    ``import ad_remover`` because the test suite's autouse fixture pops
    ``ad_remover`` from ``sys.modules`` between tests, so a cached reference
    would point at a stale, pre-reload object and miss ``patch("ad_remover.X")``.
    """
    return sys.modules["ad_remover"]


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe timeouts
# ---------------------------------------------------------------------------
#
# Every ffmpeg and ffprobe call must be bounded.  A hung child process holds the
# S3 distributed lock (TTL 3600s, see distributed_lock.py) for as long as it
# lives, which silently blocks every subsequent cron run -- the pipeline just
# stops producing episodes with no error reported anywhere.  A timeout raises
# TimeoutExpired, which the splice paths convert into a splice failure so the
# existing retry logic picks the episode up on a later run.


def _ffmpeg_timeout(name: str) -> float:
    """Return the configured timeout in seconds for an ffmpeg/ffprobe stage."""
    return settings.get(name)


def _ffmpeg_bin() -> str:
    """Return the configured ffmpeg executable.

    Defaults to ``"ffmpeg"`` (resolved via PATH) but can be pinned to an absolute
    path via ``FFMPEG_BIN`` when the PATH build is unstable — the Homebrew ARM
    ffmpeg 8.x, for example, SIGSEGVs (exit -11) on the atrim/concat splice graph
    and even on plain stream-copy segment extraction, which silently abandons
    every episode.  A blank value falls back to ``"ffmpeg"``.
    """
    return settings.get("FFMPEG_BIN") or "ffmpeg"


def _ffprobe_bin() -> str:
    """Return the configured ffprobe executable (see :func:`_ffmpeg_bin`)."""
    return settings.get("FFPROBE_BIN") or "ffprobe"


# ---------------------------------------------------------------------------
# Fix #5 – Silence detection + boundary snapping
# ---------------------------------------------------------------------------


def detect_silence(
    mp3_path: str,
    noise_threshold: str = "-35dB",
    min_duration: float = 0.5,
) -> list[dict]:
    """Detect silence intervals in *mp3_path* using ffmpeg silencedetect.

    Args:
        mp3_path:        Path to the audio file.
        noise_threshold: Noise floor passed to silencedetect (e.g. ``"-35dB"``).
        min_duration:    Minimum silence duration in seconds to report.

    Returns:
        List of ``{"start": float, "end": float, "duration": float}`` dicts,
        sorted by start time.  Returns an empty list if ffmpeg is unavailable
        or the file contains no qualifying silences.
    """
    cmd = [
        _ffmpeg_bin(),
        "-i",
        mp3_path,
        "-af",
        f"silencedetect=noise={noise_threshold}:d={min_duration}",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_ffmpeg_timeout("FFMPEG_SILENCEDETECT_TIMEOUT_SECS"),
        )
    except FileNotFoundError:
        logger.warning("[AdRemover] ffmpeg not found — silence detection skipped")
        return []
    except subprocess.TimeoutExpired:
        # Silence detection only refines cut boundaries, so a timeout degrades
        # to "no silences found" rather than failing the episode.
        logger.warning("[AdRemover] ffmpeg silencedetect timed out for %s — skipping silence snapping", mp3_path)
        return []

    silences: list[dict] = []
    current_start: float | None = None
    for line in result.stderr.split("\n"):
        if "silence_start:" in line:
            try:
                current_start = float(line.split("silence_start:")[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif "silence_end:" in line and current_start is not None:
            try:
                parts = line.split("silence_end:")[1].strip().split("|")
                end = float(parts[0].strip().split()[0])
                duration = (
                    float(parts[1].split("silence_duration:")[1].strip()) if len(parts) > 1 else end - current_start
                )
                silences.append(
                    {
                        "start": round(current_start, 2),
                        "end": round(end, 2),
                        "duration": round(duration, 2),
                    }
                )
                current_start = None
            except (ValueError, IndexError):
                current_start = None

    return silences


def _snap_to_silence_boundary(
    time: float,
    silences: list[dict],
    window: float = 3.0,
    prefer_earlier: bool = False,
) -> float:
    """Return the silence boundary nearest to *time* within ±*window* seconds.

    If no silence boundary falls within the window, *time* is returned unchanged.

    Args:
        time:           Timestamp to snap (seconds).
        silences:       Silence interval list from :func:`detect_silence`.
        window:         Max distance (seconds) to move the boundary.
        prefer_earlier: When ``True``, break ties in favour of earlier candidates.
                        Use for segment **starts** — snapping earlier avoids cutting
                        into the content just before the ad begins.
                        When ``False`` (default), ties favour later candidates,
                        which is correct for segment **ends** — snapping later avoids
                        clipping the ad outro.

    Returns:
        Nearest silence boundary within *window*, or *time* if none found.
    """
    best_time = time
    best_dist = float("inf")
    for silence in silences:
        for candidate in (silence["start"], silence["end"]):
            dist = abs(candidate - time)
            if dist > window:
                continue
            if dist < best_dist:
                best_dist = dist
                best_time = candidate
            elif dist == best_dist:
                if (prefer_earlier and candidate < best_time) or (not prefer_earlier and candidate > best_time):
                    best_time = candidate
    return best_time


def snap_ad_boundaries(
    ad_segments: list[AdSegment],
    mp3_path: str,
    snap_window: float = 3.0,
    silences: list[dict] | None = None,
) -> list[AdSegment]:
    """Snap each ad-segment boundary to the nearest silence gap.

    Cuts landing on silence rather than mid-word produce much cleaner audio.
    Falls back to the original boundaries if silence detection fails or finds
    nothing within *snap_window* seconds.

    Args:
        ad_segments:  Candidate ad segments to adjust.
        mp3_path:     Source audio file (used to detect silences).
        snap_window:  Maximum seconds to move a boundary (default: 3.0).
        silences:     Pre-computed silence intervals (avoids redundant ffmpeg call).

    Returns:
        Adjusted segment list.  Any segment shrunk below ``_MIN_AD_SECONDS``
        after snapping is kept at its original boundaries.
    """
    if not ad_segments:
        return ad_segments

    if silences is None:
        try:
            silences = _pkg().detect_silence(mp3_path)
        except Exception as exc:
            logger.warning("[AdRemover] Silence detection failed — keeping original boundaries: %s", exc)
            return ad_segments

    if not silences:
        logger.debug("[AdRemover] No silence intervals found — skipping boundary snapping")
        return ad_segments

    _MIN_AD = 5.0
    snapped: list[AdSegment] = []
    for seg in ad_segments:
        # Snap start earlier (prefer_earlier=True): avoids cutting into content before ad
        # Snap end later (prefer_earlier=False): avoids clipping the ad outro
        new_start = _snap_to_silence_boundary(seg["start"], silences, snap_window, prefer_earlier=True)
        new_end = _snap_to_silence_boundary(seg["end"], silences, snap_window, prefer_earlier=False)
        if new_end - new_start < _MIN_AD:
            logger.warning(
                "[AdRemover] Silence snap would shrink [%.1f–%.1f] to %.1fs — keeping original",
                seg["start"],
                seg["end"],
                new_end - new_start,
            )
            snapped.append(seg)
        else:
            if new_start != seg["start"] or new_end != seg["end"]:
                logger.info(
                    "[AdRemover] Snapped [%.1f–%.1f] → [%.1f–%.1f]",
                    seg["start"],
                    seg["end"],
                    new_start,
                    new_end,
                )
            snapped.append({"start": new_start, "end": new_end})

    return snapped


# ---------------------------------------------------------------------------
# Music intro/outro detection
# ---------------------------------------------------------------------------


def detect_music_bookends(
    segments: list[dict],
    mp3_path: str,
    min_intro_secs: float = 8.0,
    min_outro_secs: float = 5.0,
    silences: list[dict] | None = None,
) -> list[AdSegment]:
    """Detect music intro/outro by finding non-silent audio outside transcript boundaries.

    The region [0, first_word_start] is a music intro if its duration >= min_intro_secs
    and it is not entirely silent.  The region [last_word_end, audio_duration] is a music
    outro by the same criteria using min_outro_secs.

    Returns:
        List of AdSegment dicts with an extra "label" key ("music_intro"/"music_outro").
        Empty list if segments is empty or audio duration cannot be read.
    """
    if not segments:
        return []

    duration = _pkg()._get_audio_duration(mp3_path)
    if duration <= 0.0:
        logger.warning("[AdRemover] Could not read audio duration for music detection: %s", mp3_path)
        return []

    first_speech = segments[0]["start"]
    last_speech = segments[-1]["end"]
    results: list[AdSegment] = []

    if silences is None:
        silences = _pkg().detect_silence(mp3_path)

    def _region_has_audio(region_start: float, region_end: float) -> bool:
        """Return True if [region_start, region_end] contains non-silent audio."""
        region_dur = region_end - region_start
        if region_dur <= 0:
            return False
        silence_secs = 0.0
        for s in silences:
            overlap_start = max(s["start"], region_start)
            overlap_end = min(s["end"], region_end)
            if overlap_end > overlap_start:
                silence_secs += overlap_end - overlap_start
        return (silence_secs / region_dur) < 0.85

    # Intro
    if first_speech >= min_intro_secs and _region_has_audio(0.0, first_speech):
        logger.info("[AdRemover] Music intro detected: [0.0, %.2fs] (%.1fs)", first_speech, first_speech)
        results.append({"start": 0.0, "end": round(first_speech, 2), "label": "music_intro"})

    # Outro
    outro_dur = duration - last_speech
    if outro_dur >= min_outro_secs and _region_has_audio(last_speech, duration):
        logger.info(
            "[AdRemover] Music outro detected: [%.2fs, %.2fs] (%.1fs)",
            last_speech,
            duration,
            outro_dur,
        )
        results.append({"start": round(last_speech, 2), "end": round(duration, 2), "label": "music_outro"})

    return results
