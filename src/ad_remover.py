"""Ad removal pipeline for downloaded podcast audio.

Pipeline:
    1. Upload the audio file to a temporary S3 prefix and transcribe it with
       AWS Transcribe (word-level timestamps).
    2. Send the transcript to AWS Bedrock (us.anthropic.claude-sonnet-4-20250514-v1:0 via the
       Converse API) to identify ad-segment timestamps as JSON.
    3. Use ffmpeg to splice out the ad segments and stitch the remaining audio
       into a single output MP3.
    4. Fall back to the original file if any step fails.

Environment variables (all optional — sensible defaults provided):
    REMOVE_ADS              – Set to "false" to skip ad removal entirely (default: "true").
    S3_BUCKET               – Bucket used for the temporary Transcribe input file
                              (re-uses the existing podcast bucket).
    AWS_DEFAULT_REGION      – AWS region for Transcribe and Bedrock clients.
    TRANSCRIBE_LANGUAGE_CODE – BCP-47 language code passed to Transcribe (default: "en-US").
    BEDROCK_MODEL_ID        – Bedrock model ID for ad detection
                              (default: "us.anthropic.claude-sonnet-4-20250514-v1:0").
    TRANSCRIBE_POLL_INTERVAL – Seconds between Transcribe job status polls (default: 10).
    TRANSCRIBE_MAX_WAIT     – Maximum seconds to wait for a Transcribe job (default: 3600).
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import subprocess
import time
import uuid

import boto3
import certifi

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Internal type alias
# ---------------------------------------------------------------------------
AdSegment = dict  # {"start": float, "end": float}


# ---------------------------------------------------------------------------
# Step 1 – Transcription (AWS Transcribe)
# ---------------------------------------------------------------------------

def transcribe_audio(mp3_path: str, video_id: str) -> list[dict]:
    """Upload *mp3_path* to S3 and transcribe it with AWS Transcribe.

    Returns a list of segment dicts, each with keys ``start`` (float),
    ``end`` (float), and ``text`` (str) derived from the word-level items
    in the Transcribe output.

    Args:
        mp3_path: Local path to the audio file.
        video_id: Episode identifier, used to name the temporary S3 object and
                  the Transcribe job.

    Returns:
        List of segment dicts.  Empty list when Transcribe produces no output.

    Raises:
        RuntimeError: On any AWS API error or if the Transcribe job fails.
    """
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        raise RuntimeError("S3_BUCKET must be set to use AWS Transcribe")

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    language_code = os.environ.get("TRANSCRIBE_LANGUAGE_CODE", "en-US")
    poll_interval = int(os.environ.get("TRANSCRIBE_POLL_INTERVAL", "10"))
    max_wait = int(os.environ.get("TRANSCRIBE_MAX_WAIT", "3600"))

    s3_client = boto3.client("s3", region_name=region)
    transcribe_client = boto3.client("transcribe", region_name=region)

    # 1. Upload audio to a temporary S3 key
    tmp_key = f"transcribe-tmp/{video_id}.mp3"
    logger.info("[AdRemover] Uploading %s to s3://%s/%s for transcription", mp3_path, bucket, tmp_key)
    s3_client.upload_file(mp3_path, bucket, tmp_key)

    media_uri = f"s3://{bucket}/{tmp_key}"
    # Transcribe job names only allow [A-Za-z0-9_-] — sanitize video_id
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", video_id)[:64].strip("-")
    job_name = f"pad-{safe_id}-{uuid.uuid4().hex[:8]}"

    try:
        # 2. Start the transcription job
        logger.info("[AdRemover] Starting Transcribe job %s", job_name)
        transcribe_client.start_transcription_job(
            TranscriptionJobName=job_name,
            Media={"MediaFileUri": media_uri},
            MediaFormat="mp3",
            LanguageCode=language_code,
            Settings={"ShowSpeakerLabels": False, "ChannelIdentification": False},
        )

        # 3. Poll until complete
        elapsed = 0
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval
            status_resp = transcribe_client.get_transcription_job(
                TranscriptionJobName=job_name
            )
            status = status_resp["TranscriptionJob"]["TranscriptionJobStatus"]
            logger.info("[AdRemover] Transcribe job %s status: %s (elapsed %ds)", job_name, status, elapsed)

            if status == "COMPLETED":
                break
            if status == "FAILED":
                reason = status_resp["TranscriptionJob"].get("FailureReason", "unknown")
                raise RuntimeError(f"Transcribe job {job_name} failed: {reason}")
            # Ignore transient API errors on individual poll calls — keep looping

        else:
            raise RuntimeError(f"Transcribe job {job_name} timed out after {max_wait}s")

        # 4. Download and parse the transcript JSON
        transcript_uri = status_resp["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
        logger.info("[AdRemover] Downloading transcript from %s", transcript_uri)

        import urllib.request
        with urllib.request.urlopen(transcript_uri, context=_SSL_CTX) as resp:
            transcript_data = json.loads(resp.read())

        items = transcript_data.get("results", {}).get("items", [])
        segments = _items_to_segments(items)
        logger.info("[AdRemover] Transcription complete: %d segments from %d items", len(segments), len(items))
        return segments

    finally:
        # 5. Clean up: delete temp S3 object and Transcribe job
        try:
            s3_client.delete_object(Bucket=bucket, Key=tmp_key)
            logger.debug("[AdRemover] Deleted temp S3 object s3://%s/%s", bucket, tmp_key)
        except Exception as exc:
            logger.warning("[AdRemover] Could not delete temp S3 object: %s", exc)

        try:
            transcribe_client.delete_transcription_job(TranscriptionJobName=job_name)
            logger.debug("[AdRemover] Deleted Transcribe job %s", job_name)
        except Exception as exc:
            logger.warning("[AdRemover] Could not delete Transcribe job %s: %s", job_name, exc)


def _items_to_segments(items: list[dict], gap_threshold: float = 1.5) -> list[dict]:
    """Collapse Transcribe word-items into phrase segments.

    Consecutive words separated by less than *gap_threshold* seconds are
    grouped into a single segment.

    Args:
        items:         Raw ``items`` list from the Transcribe JSON response.
        gap_threshold: Maximum silence (seconds) allowed within a segment.

    Returns:
        List of ``{"start": float, "end": float, "text": str}`` dicts.
    """
    segments: list[dict] = []
    current_words: list[str] = []
    current_start: float | None = None
    current_end: float = 0.0

    for item in items:
        if item.get("type") != "pronunciation":
            # Punctuation items have no timing — append to current text
            if current_words and item.get("alternatives"):
                current_words.append(item["alternatives"][0].get("content", ""))
            continue

        alts = item.get("alternatives", [])
        if not alts:
            continue

        word = alts[0].get("content", "")
        start = float(item.get("start_time", 0))
        end = float(item.get("end_time", 0))

        if current_start is None:
            current_start = start
            current_end = end
            current_words = [word]
        elif start - current_end > gap_threshold:
            # Flush the current segment
            segments.append({
                "start": current_start,
                "end": current_end,
                "text": " ".join(current_words),
            })
            current_start = start
            current_end = end
            current_words = [word]
        else:
            current_words.append(word)
            current_end = end

    if current_words and current_start is not None:
        segments.append({
            "start": current_start,
            "end": current_end,
            "text": " ".join(current_words),
        })

    return segments


# ---------------------------------------------------------------------------
# Step 2 – Ad detection (AWS Bedrock)
# ---------------------------------------------------------------------------

_AD_DETECTION_PROMPT = """\
You are an audio editor specialising in podcast ad removal.

Below is the word-level transcript of a podcast episode. Each line has the
format:  [start_seconds - end_seconds]  text

Your task is to identify every advertisement / sponsored segment. Return ONLY a
valid JSON array where each element is an object with "start" and "end" keys
(floating-point seconds). If there are no ads return an empty array [].

Example output:
[{{"start": 120.5, "end": 195.0}}, {{"start": 2310.0, "end": 2405.5}}]

Transcript:
{transcript}
"""


def detect_ads(segments: list[dict]) -> list[AdSegment]:
    """Ask AWS Bedrock to identify ad segments in *segments*.

    Uses the Bedrock Converse API with the model specified by
    ``BEDROCK_MODEL_ID`` (default: ``us.anthropic.claude-sonnet-4-20250514-v1:0``).

    Args:
        segments: List of transcript segment dicts as returned by
            :func:`transcribe_audio`.

    Returns:
        List of ``{"start": float, "end": float}`` dicts for ad intervals.
        Returns an empty list if the model returns no ads or response cannot
        be parsed.

    Raises:
        Exception: Propagated if the Bedrock API call itself fails (caller
            catches and falls back to original file).
    """
    if not segments:
        logger.info("[AdRemover] No transcript segments — skipping ad detection.")
        return []

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")

    # Build transcript: keep first + last 30 min of segments to stay within
    # Bedrock context window for very long episodes (ads are almost always
    # near the start or end of the content).
    _MAX_TRANSCRIPT_CHARS = 60_000  # ~15k tokens, well within Claude Sonnet limits
    transcript_lines = "\n".join(
        f"[{s['start']:.1f} - {s['end']:.1f}]  {s['text']}" for s in segments
    )
    if len(transcript_lines) > _MAX_TRANSCRIPT_CHARS:
        half = _MAX_TRANSCRIPT_CHARS // 2
        transcript_lines = (
            transcript_lines[:half]
            + "\n\n[... middle of episode truncated for brevity ...]\n\n"
            + transcript_lines[-half:]
        )
        logger.info(
            "[AdRemover] Transcript truncated to ~%d chars for Bedrock prompt",
            _MAX_TRANSCRIPT_CHARS,
        )
    prompt = _AD_DETECTION_PROMPT.format(transcript=transcript_lines)

    logger.info("[AdRemover] Sending transcript to Bedrock (model=%s, region=%s)", model_id, region)

    bedrock = boto3.client("bedrock-runtime", region_name=region)
    response = bedrock.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"temperature": 0.0},
    )

    raw = response["output"]["message"]["content"][0]["text"]
    logger.debug("[AdRemover] Bedrock raw response: %s", raw)

    # Extract JSON array from the response (models sometimes add extra prose)
    start_idx = raw.find("[")
    end_idx = raw.rfind("]")
    if start_idx == -1 or end_idx == -1:
        logger.warning("[AdRemover] Bedrock response contained no JSON array — assuming no ads.")
        return []

    try:
        ad_segments: list[AdSegment] = json.loads(raw[start_idx : end_idx + 1])
    except json.JSONDecodeError as exc:
        logger.warning("[AdRemover] Failed to parse Bedrock JSON response: %s", exc)
        return []

    # Validate structure
    valid = []
    for seg in ad_segments:
        if isinstance(seg, dict) and "start" in seg and "end" in seg:
            valid.append({"start": float(seg["start"]), "end": float(seg["end"])})
        else:
            logger.warning("[AdRemover] Ignoring malformed ad segment: %s", seg)

    logger.info("[AdRemover] Detected %d ad segment(s): %s", len(valid), valid)
    return valid


# ---------------------------------------------------------------------------
# Step 3 – Audio splicing (ffmpeg)
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
        RuntimeError: If ffmpeg/ffprobe is not available or returns non-zero.
        ValueError:   If *ad_segments* is empty.
    """
    if not ad_segments:
        raise ValueError("splice_audio called with empty ad_segments list")

    # Pre-flight: verify the file exists and is non-trivially sized
    try:
        file_size = os.path.getsize(mp3_path)
    except OSError as exc:
        raise RuntimeError(
            f"ffprobe aborted: cannot stat input file '{mp3_path}': {exc}"
        ) from exc

    logger.debug("[AdRemover] ffprobe input '%s' — size %d bytes", mp3_path, file_size)
    if file_size < 1024:
        raise RuntimeError(
            f"ffprobe aborted: input file is suspiciously small ({file_size} bytes), "
            f"likely corrupt or incomplete: '{mp3_path}'"
        )

    # Probe total duration via ffprobe
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        mp3_path,
    ]
    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        if result.stderr.strip():
            logger.debug("[AdRemover] ffprobe stderr (non-fatal): %s", result.stderr.strip())
        total_duration = float(result.stdout.strip())
    except subprocess.CalledProcessError as exc:
        stdout = (exc.stdout or "").strip()
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"ffprobe failed (exit {exc.returncode}):\n"
            f"  stdout: {stdout!r}\n"
            f"  stderr: {stderr!r}\n"
            f"  cmd:    {' '.join(exc.cmd)}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"ffprobe error ({type(exc).__name__}): {exc} — "
            f"file: '{mp3_path}'"
        ) from exc

    # Sort ad segments and merge overlaps
    sorted_ads = sorted(ad_segments, key=lambda s: s["start"])
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

    # Build ffmpeg atrim + concat filter_complex
    filter_parts = [
        f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]"
        for i, (start, end) in enumerate(keep)
    ]
    inputs = "".join(f"[a{i}]" for i in range(len(keep)))
    filter_complex = ";".join(filter_parts) + f";{inputs}concat=n={len(keep)}:v=0:a=1[out]"

    cmd = [
        "ffmpeg", "-y",
        "-i", mp3_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-codec:a", "libmp3lame",
        "-q:a", "2",
        output_path,
    ]

    logger.info("[AdRemover] Running ffmpeg splice command")
    logger.debug("[AdRemover] ffmpeg cmd: %s", " ".join(cmd))

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"ffmpeg splice failed (exit {exc.returncode}):\n{exc.stderr}"
        ) from exc


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def remove_ads(mp3_path: str, video_id: str, tmp_dir: str) -> str:
    """Run the full ad-removal pipeline on *mp3_path*.

    Steps:
        1. Transcribe with AWS Transcribe.
        2. Detect ads with AWS Bedrock.
        3. Splice out detected ad segments with ffmpeg.

    On *any* failure the function logs the error and returns the original
    *mp3_path* unchanged so the caller can still upload the unmodified file.

    Args:
        mp3_path: Path to the downloaded audio file.
        video_id: Episode identifier (used to name the Transcribe job and output file).
        tmp_dir:  Temporary directory to write the cleaned file into.

    Returns:
        Path to the cleaned audio file, or *mp3_path* if ad removal was skipped
        or failed.
    """
    if os.environ.get("REMOVE_ADS", "true").lower() in ("false", "0", "no"):
        logger.info("[AdRemover] REMOVE_ADS=false — skipping ad removal for %s", video_id)
        return mp3_path

    logger.info("[AdRemover] Starting ad removal for %s", video_id)

    try:
        segments = transcribe_audio(mp3_path, video_id)
    except Exception as exc:
        logger.error("[AdRemover] Transcription failed for %s: %s — using original file", video_id, exc)
        return mp3_path

    try:
        ad_segments = detect_ads(segments)
    except Exception as exc:
        logger.error("[AdRemover] Ad detection failed for %s: %s — using original file", video_id, exc)
        return mp3_path

    if not ad_segments:
        logger.info("[AdRemover] No ads detected for %s — using original file", video_id)
        return mp3_path

    cleaned_path = os.path.join(tmp_dir, f"{video_id}_clean.mp3")
    try:
        splice_audio(mp3_path, ad_segments, cleaned_path)
    except Exception as exc:
        logger.error("[AdRemover] Splicing failed for %s: %s — using original file", video_id, exc)
        return mp3_path

    logger.info("[AdRemover] Ad removal complete for %s → %s", video_id, cleaned_path)
    return cleaned_path
