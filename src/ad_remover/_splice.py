"""Step 2.5/3 of the pipeline: duration probing, file validation and splicing.

Covers the 3-tier duration fallback (ffprobe → ffprobe -f mp3 → mutagen),
downloaded-file pre-flight validation, the ffmpeg atrim/concat splice, and the
concat-demuxer fallback used when the filter graph crashes on ARM ffmpeg.

Patched-symbol routing: ``probe_duration_with_fallbacks`` and
``_splice_concat_demuxer`` are rebound on the ``ad_remover`` package by the test
suite, so their callers here reach them through the package namespace.
``NoUsableAdSegments`` is imported from the package (defined before this module
is imported). ``subprocess`` is imported normally — tests patch
``ad_remover.subprocess.run`` on the shared module.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import settings
from ad_remover._ffmpeg import _ffmpeg_bin, _ffmpeg_timeout, _ffprobe_bin
from ad_remover._segments import AdSegment, _clamp_ad_segments, _coerce_ad_segment

logger = logging.getLogger("ad_remover")


def _pkg():
    """Return the live ``ad_remover`` package module (see _ffmpeg._pkg)."""
    return sys.modules["ad_remover"]


def _ffprobe_duration(path: str, force_format: str | None = None) -> float:
    """Run ffprobe to get duration in seconds. Raises RuntimeError on failure."""
    cmd = [_ffprobe_bin(), "-v", "error"]
    if force_format:
        cmd += ["-f", force_format]
    cmd += [
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        timeout=_ffmpeg_timeout("FFPROBE_TIMEOUT_SECS"),
    )
    if result.stderr.strip():
        logger.debug("[AdRemover] ffprobe stderr (non-fatal): %s", result.stderr.strip())
    return float(result.stdout.strip())


def _mutagen_duration(path: str) -> float:
    """Use mutagen to read MP3 duration - pure Python, crash-proof fallback."""
    try:
        from mutagen.mp3 import MP3  # noqa: PLC0415

        return MP3(path).info.length
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"mutagen could not read duration: {exc}") from exc


def probe_duration_with_fallbacks(mp3_path: str) -> float:
    """Determine the duration (seconds) of *mp3_path* using a 3-tier fallback chain.

    Tier 1: plain ``ffprobe`` (format auto-detected).
    Tier 2: ``ffprobe -f mp3`` (handles non-standard/SSAI-stitched containers).
    Tier 3: ``mutagen`` (pure Python, immune to ffprobe crashes).

    Raises:
        RuntimeError: If all three methods fail - a strong signal the file is
                      corrupt/truncated rather than just unusually encoded.
    """
    total_duration: float | None = None
    try:
        total_duration = _ffprobe_duration(mp3_path)
    except subprocess.CalledProcessError as exc:
        stdout = (exc.stdout or "").strip()
        stderr = (exc.stderr or "").strip()
        logger.warning(
            "[AdRemover] ffprobe auto-detect failed (exit %s) for %s - retrying with -f mp3.\n  stdout: %r  stderr: %r",
            exc.returncode,
            mp3_path,
            stdout,
            stderr,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AdRemover] ffprobe error (%s): %s - retrying with -f mp3.", type(exc).__name__, exc)

    if total_duration is None:
        try:
            total_duration = _ffprobe_duration(mp3_path, force_format="mp3")
        except subprocess.CalledProcessError as exc:
            stdout = (exc.stdout or "").strip()
            stderr = (exc.stderr or "").strip()
            logger.warning(
                "[AdRemover] ffprobe -f mp3 also failed (exit %s) for %s - falling back to mutagen.\n"
                "  stdout: %r  stderr: %r",
                exc.returncode,
                mp3_path,
                stdout,
                stderr,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AdRemover] ffprobe -f mp3 error (%s): %s - falling back to mutagen.",
                type(exc).__name__,
                exc,
            )

    if total_duration is None:
        total_duration = _mutagen_duration(mp3_path)
        logger.info("[AdRemover] Used mutagen fallback for duration of %s: %.2fs", mp3_path, total_duration)

    return total_duration


def validate_audio_file(path: str, min_bytes: int = 1024) -> tuple[bool, str]:
    """Cheap pre-flight validation of a freshly downloaded audio file.

    Intended to run immediately after download, *before* the expensive
    Transcribe + Bedrock steps, so a corrupt/truncated SSAI-stitched fetch is
    caught and re-downloaded cheaply instead of discovered only when splicing
    crashes after paying for transcription and ad detection.

    Args:
        path:      Path to the downloaded audio file.
        min_bytes: Minimum acceptable file size in bytes (default 1024).

    Returns:
        ``(True, "")`` if the file looks structurally sound (exists, non-trivial
        size, and a duration can be determined via ffprobe/mutagen).
        ``(False, reason)`` otherwise, where *reason* is a short human-readable
        explanation suitable for logging.
    """
    try:
        file_size = os.path.getsize(path)
    except OSError as exc:
        return False, f"cannot stat file: {exc}"

    if file_size < min_bytes:
        return False, f"suspiciously small ({file_size} bytes < {min_bytes})"

    try:
        duration = _pkg().probe_duration_with_fallbacks(path)
    except Exception as exc:  # noqa: BLE001
        return False, f"duration probe failed: {exc}"

    if duration <= 0:
        return False, f"zero/negative duration ({duration})"

    return True, ""


# ---------------------------------------------------------------------------
# Step 3 - Audio splicing (ffmpeg)
# ---------------------------------------------------------------------------


def splice_audio(mp3_path: str, ad_segments: list[AdSegment], output_path: str) -> None:
    """Remove *ad_segments* from *mp3_path* and write result to *output_path*.

    Uses ffmpeg's atrim + concat filter graph to cut the non-ad portions and
    join them into a single file.

    Args:
        mp3_path:    Path to the source audio file.
        ad_segments: List of ``{"start": float, "end": float}`` dicts to cut.
        output_path: Destination path for the cleaned audio file.

    Raises:
        NoUsableAdSegments: If *ad_segments* is empty or every candidate is
                      value-invalid (malformed, non-numeric, non-finite,
                      inverted or zero-length).  Deterministic: there is nothing
                      to cut, so the caller should publish the original.
        RuntimeError: If ffmpeg/ffprobe fails, or if value-valid segments all
                      fall outside the probed duration — that means the media is
                      shorter than the audio the timestamps came from (a
                      truncated download), which a fresh download can fix.
    """
    if not ad_segments:
        raise _pkg().NoUsableAdSegments("splice_audio called with empty ad_segments list")

    # Pre-flight: verify the file exists and is non-trivially sized
    try:
        file_size = os.path.getsize(mp3_path)
    except OSError as exc:
        raise RuntimeError(f"ffprobe aborted: cannot stat input file '{mp3_path}': {exc}") from exc

    logger.debug("[AdRemover] ffprobe input '%s' - size %d bytes", mp3_path, file_size)
    if file_size < 1024:
        raise RuntimeError(
            f"ffprobe aborted: input file is suspiciously small ({file_size} bytes), "
            f"likely corrupt or incomplete: '{mp3_path}'"
        )

    # Probe total duration via the shared 3-tier fallback chain (ffprobe ->
    # ffprobe -f mp3 -> mutagen); see probe_duration_with_fallbacks().
    try:
        total_duration = _pkg().probe_duration_with_fallbacks(mp3_path)
    except RuntimeError as exc:
        raise RuntimeError(f"All duration-detection methods failed for '{mp3_path}': {exc}") from exc

    # Re-validate here as well as at parse time: segments also arrive from the
    # S3 ad-segment cache and from music-bookend detection, neither of which
    # goes through _parse_ad_response.
    coerced = [c for c in (_coerce_ad_segment(seg) for seg in ad_segments) if c is not None]
    if not coerced:
        raise _pkg().NoUsableAdSegments(
            f"No usable ad segments after validation (from {len(ad_segments)} candidate(s))"
        )

    validated = _clamp_ad_segments(coerced, total_duration)
    if not validated:
        # The segments are well-formed; they just do not overlap the audio we
        # probed.  That happens when the download is truncated -- the timestamps
        # were detected against the full episode, so a shorter file drops them
        # all.  Publishing here would ship a truncated episode with its ads
        # intact, so this stays an operational error and the caller retries with
        # a fresh download.
        raise RuntimeError(
            f"All {len(coerced)} ad segment(s) fall outside the probed duration "
            f"({total_duration:.1f}s) of '{mp3_path}' — the file is likely truncated"
        )

    # Sort ad segments and merge overlaps
    sorted_ads = sorted(validated, key=lambda s: s["start"])
    merged_ads: list[AdSegment] = []
    for seg in sorted_ads:
        if merged_ads and seg["start"] <= merged_ads[-1]["end"]:
            merged_ads[-1]["end"] = max(merged_ads[-1]["end"], seg["end"])
        else:
            merged_ads.append({"start": seg["start"], "end": seg["end"]})

    # Build keep intervals (inverse of ad segments)
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for ad in merged_ads:
        if ad["start"] > cursor:
            keep.append((cursor, ad["start"]))
        cursor = ad["end"]
    if cursor < total_duration:
        keep.append((cursor, total_duration))

    if not keep:
        raise RuntimeError("Ad segments cover the entire file — nothing left to splice.")

    logger.info("[AdRemover] Keep intervals (%d): %s", len(keep), keep)

    # Build ffmpeg atrim + concat + optional loudnorm filter_complex.
    # loudnorm (EBU R128) equalises loudness across all keep intervals so that
    # volume discontinuities at cut points are inaudible.
    #
    # loudnorm emits planar float (fltp) samples. Feeding those straight into
    # libmp3lame triggers an assertion crash in lame's psychoacoustic model
    # ("Assertion failed: (el >= 0), function calc_energy, psymodel.c:576") on
    # certain content — reproducible across ffmpeg 7/8/9 and lame 3.100/4.0, so
    # it is a libmp3lame bug, not an ffmpeg-version issue. Converting the samples
    # to signed 16-bit planar (s16p) before the encoder avoids the broken code
    # path entirely (verified 15/15 vs 0/15 unfixed). The explicit aformat is a
    # no-op for already-s16 audio, so it is safe to apply unconditionally.
    loudnorm = settings.get("SPLICE_LOUDNORM")
    filter_parts = [
        f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]" for i, (start, end) in enumerate(keep)
    ]
    inputs = "".join(f"[a{i}]" for i in range(len(keep)))
    concat_out = "concat_out"
    filter_complex = ";".join(filter_parts) + f";{inputs}concat=n={len(keep)}:v=0:a=1[{concat_out}]"
    if loudnorm:
        filter_complex += f";[{concat_out}]loudnorm=I=-16:TP=-1.5:LRA=11,aformat=sample_fmts=s16p[out]"
    else:
        filter_complex = filter_complex.replace(f"[{concat_out}]", "[out]", 1)

    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-i",
        mp3_path,
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "2",
        output_path,
    ]

    logger.info("[AdRemover] Running ffmpeg splice command")
    logger.debug("[AdRemover] ffmpeg cmd: %s", " ".join(cmd))

    # ffmpeg can crash with a signal (SIGSEGV -11 / SIGABRT -6) rather than a
    # clean error, and on the ARM Homebrew build these crashes are often
    # probabilistic — the identical command succeeds on a re-run. Retry in-place
    # a few times before paying for the slower concat-demuxer fallback, which
    # re-reads the same file with the same decoder and tends to crash again.
    crash_retries = settings.get("SPLICE_CRASH_RETRIES")
    last_crash: subprocess.CalledProcessError | None = None
    for crash_attempt in range(1, crash_retries + 2):  # 1 initial + N retries
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=_ffmpeg_timeout("FFMPEG_SPLICE_TIMEOUT_SECS"),
            )
            return  # success
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ffmpeg splice timed out after {exc.timeout}s for {mp3_path}") from exc
        except subprocess.CalledProcessError as exc:
            if exc.returncode not in _pkg().FFMPEG_CRASH_RETURNCODES:
                raise RuntimeError(f"ffmpeg splice failed (exit {exc.returncode}):\n{exc.stderr}") from exc
            last_crash = exc
            if crash_attempt <= crash_retries:
                logger.warning(
                    "[AdRemover] ffmpeg crashed (exit %d) on splice attempt %d/%d for %s — re-running",
                    exc.returncode,
                    crash_attempt,
                    crash_retries + 1,
                    mp3_path,
                )

    # In-place retries exhausted — fall back to the concat demuxer, which extracts
    # each keep interval by stream-copy and re-encodes without the loudnorm filter
    # graph, side-stepping both crash paths.
    logger.warning(
        "[AdRemover] ffmpeg crashed (exit %d) after %d splice attempt(s) — retrying with concat-demuxer fallback for %s",
        last_crash.returncode if last_crash else -1,
        crash_retries + 1,
        mp3_path,
    )
    _pkg()._splice_concat_demuxer(mp3_path, keep, output_path)


def _splice_concat_demuxer(
    mp3_path: str,
    keep: list[tuple[float, float]],
    output_path: str,
) -> None:
    """Concat-demuxer fallback for ``splice_audio``.

    Extracts each keep interval as a separate segment file using stream-copy
    (no re-encode), then joins all segments with the ``-f concat`` demuxer and
    re-encodes to MP3 with libmp3lame.  Slower than the filter_complex path but
    avoids the ARM ffmpeg SIGSEGV triggered by long filter graphs.

    Args:
        mp3_path:    Path to the source audio file.
        keep:        List of ``(start, end)`` float tuples to retain.
        output_path: Destination path for the cleaned audio file.

    Raises:
        RuntimeError: If any ffmpeg sub-command fails.
    """
    import tempfile
    import uuid

    work_dir = os.path.dirname(output_path) or tempfile.gettempdir()
    segment_paths: list[str] = []
    # os.getpid() alone is not unique: episodes are spliced concurrently by
    # threads (PODCAST_EPISODE_WORKERS) that share one PID and one work_dir, so
    # two episodes wrote to the same _seg_0_<pid>.mp3 and each other's concat
    # list, silently producing audio stitched from both episodes.
    token = f"{os.getpid()}_{uuid.uuid4().hex[:12]}"
    list_path = os.path.join(work_dir, f"_concat_{token}.txt")

    try:
        # Step 1: extract each keep interval via stream-copy
        for i, (start, end) in enumerate(keep):
            seg_path = os.path.join(work_dir, f"_seg_{i}_{token}.mp3")
            segment_paths.append(seg_path)
            cmd = [
                _ffmpeg_bin(),
                "-y",
                "-ss",
                str(start),
                "-to",
                str(end),
                "-i",
                mp3_path,
                "-c:a",
                "copy",
                seg_path,
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=_ffmpeg_timeout("FFMPEG_SEGMENT_TIMEOUT_SECS"),
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"ffmpeg segment {i} extraction timed out after {exc.timeout}s") from exc
            if result.returncode in _pkg().FFMPEG_CRASH_RETURNCODES:
                raise _pkg().FfmpegCrashed(f"ffmpeg segment {i} extraction crashed (exit {result.returncode})")
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg segment {i} extraction failed (exit {result.returncode}):\n{result.stderr}")

        # Step 2: write concat list file
        with open(list_path, "w") as fh:
            for seg_path in segment_paths:
                fh.write(f"file '{seg_path}'\n")

        # Step 3: join and re-encode
        cmd = [
            _ffmpeg_bin(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_path,
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            output_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_ffmpeg_timeout("FFMPEG_SPLICE_TIMEOUT_SECS"),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ffmpeg concat-demuxer join timed out after {exc.timeout}s") from exc
        if result.returncode in _pkg().FFMPEG_CRASH_RETURNCODES:
            raise _pkg().FfmpegCrashed(f"ffmpeg concat-demuxer join crashed (exit {result.returncode})")
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg concat-demuxer join failed (exit {result.returncode}):\n{result.stderr}")

        logger.info("[AdRemover] concat-demuxer fallback succeeded for %s", mp3_path)

    finally:
        # Clean up temp segment files
        for seg_path in segment_paths:
            try:
                os.remove(seg_path)
            except OSError:
                pass
        try:
            os.remove(list_path)
        except OSError:
            pass
