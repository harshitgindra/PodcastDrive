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
    BEDROCK_MODEL_ID        – Bedrock model ID for ad-segment verification (second-pass
                              confirmation of long segments).  Defaults to Claude Sonnet for
                              accuracy (default: "us.anthropic.claude-sonnet-4-20250514-v1:0").
    BEDROCK_DETECT_MODEL_ID – Bedrock model ID for first-pass ad detection across all chunks.
                              Defaults to BEDROCK_MODEL_ID when not set.  Set to a cheaper
                              model (e.g. Claude Haiku) to reduce per-episode detection cost
                              while keeping Sonnet for the more critical verification step.
    TRANSCRIBE_POLL_INTERVAL – Seconds between Transcribe job status polls (default: 10).
    TRANSCRIBE_MAX_WAIT     – Maximum seconds to wait for a Transcribe job (default: 3600).
    REMOVE_ADS_DRY_RUN      – Set to "true" to detect ads and log them without actually
                              splicing the audio file (default: "false").  Useful for
                              evaluating detection quality before enabling full removal.
    MAX_AD_SEGMENT_SECS     – Ad segments longer than this are treated as false positives and
                              skipped (default: "180").  Legitimate podcast ads rarely exceed
                              3 minutes; very long segments usually indicate content misclassified
                              as an ad.
    AD_VERIFY_THRESHOLD_SECS – Segments longer than this value trigger a second Bedrock call to
                              confirm they are genuinely ads before removal (default: "90").
                              Set to "0" to verify every segment, or a very large number to
                              disable verification.
    AD_SNAP_TO_SILENCE      – Set to "true" to snap ad-segment boundaries to the nearest silence
                              gap (±3 s window), producing cleaner audio cuts (default: "true").
    TRANSCRIBE_CACHE_ENABLED – Set to "false" to disable S3 transcript caching (default: "true").
                               When enabled, the segment JSON from each successful Transcribe job is
                               saved to S3 at ``transcribe-cache/{video_id}.json`` and reused on
                               subsequent runs, eliminating repeated transcription costs for
                               reprocessed episodes.
    TRANSCRIBE_CACHE_PREFIX  – S3 key prefix for cached transcripts (default: "transcribe-cache").
    SPLICE_LOUDNORM         – Set to "false" to disable EBU R128 loudness normalisation after
                              splicing (default: "true").  Loudnorm equalises loudness across
                              all kept intervals so volume discontinuities at cut points are
                              inaudible.  Adds ~10-20% to ffmpeg processing time.
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

from utils import retry_aws_call

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Internal type alias
# ---------------------------------------------------------------------------
AdSegment = dict  # {"start": float, "end": float}


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
        "ffmpeg", "-i", mp3_path,
        "-af", f"silencedetect=noise={noise_threshold}:d={min_duration}",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        logger.warning("[AdRemover] ffmpeg not found — silence detection skipped")
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
                    float(parts[1].split("silence_duration:")[1].strip())
                    if len(parts) > 1
                    else end - current_start
                )
                silences.append({
                    "start": round(current_start, 2),
                    "end": round(end, 2),
                    "duration": round(duration, 2),
                })
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
                # Tie-break: prefer earlier for starts, later for ends
                if prefer_earlier and candidate < best_time:
                    best_time = candidate
                elif not prefer_earlier and candidate > best_time:
                    best_time = candidate
    return best_time


def snap_ad_boundaries(
    ad_segments: list[AdSegment],
    mp3_path: str,
    snap_window: float = 3.0,
) -> list[AdSegment]:
    """Snap each ad-segment boundary to the nearest silence gap.

    Cuts landing on silence rather than mid-word produce much cleaner audio.
    Falls back to the original boundaries if silence detection fails or finds
    nothing within *snap_window* seconds.

    Args:
        ad_segments:  Candidate ad segments to adjust.
        mp3_path:     Source audio file (used to detect silences).
        snap_window:  Maximum seconds to move a boundary (default: 3.0).

    Returns:
        Adjusted segment list.  Any segment shrunk below ``_MIN_AD_SECONDS``
        after snapping is kept at its original boundaries.
    """
    if not ad_segments:
        return ad_segments

    try:
        silences = detect_silence(mp3_path)
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
                seg["start"], seg["end"], new_end - new_start,
            )
            snapped.append(seg)
        else:
            if new_start != seg["start"] or new_end != seg["end"]:
                logger.info(
                    "[AdRemover] Snapped [%.1f–%.1f] → [%.1f–%.1f]",
                    seg["start"], seg["end"], new_start, new_end,
                )
            snapped.append({"start": new_start, "end": new_end})

    return snapped


# ---------------------------------------------------------------------------
# Step 1 – Transcription (AWS Transcribe)
# ---------------------------------------------------------------------------

def _transcript_cache_key(video_id: str) -> str:
    """Return the S3 key used for caching a transcript."""
    prefix = os.environ.get("TRANSCRIBE_CACHE_PREFIX", "transcribe-cache")
    return f"{prefix}/{video_id}.json"


def _load_transcript_cache(s3_client, bucket: str, video_id: str) -> list[dict] | None:
    """Try to load a cached transcript from S3.

    Args:
        s3_client: Boto3 S3 client.
        bucket:    S3 bucket name.
        video_id:  Episode identifier used as the cache key.

    Returns:
        Cached segment list, or ``None`` if not found or loading fails.
    """
    from botocore.exceptions import ClientError

    key = _transcript_cache_key(video_id)
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        data = json.loads(resp["Body"].read().decode("utf-8"))
        if isinstance(data, list):
            logger.info(
                "[AdRemover] Transcript cache HIT for %s (%d segments) — skipping Transcribe job",
                video_id, len(data),
            )
            return data
        logger.warning("[AdRemover] Cached transcript for %s is not a list — ignoring", video_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            logger.debug("[AdRemover] Transcript cache MISS for %s", video_id)
        else:
            logger.debug("[AdRemover] Transcript cache load error for %s (%s): %s", video_id, code, exc)
    except Exception as exc:
        logger.debug("[AdRemover] Transcript cache load failed for %s: %s", video_id, exc)
    return None


def _save_transcript_cache(s3_client, bucket: str, video_id: str, segments: list[dict]) -> None:
    """Persist a transcript segment list to S3 for future reuse.

    Args:
        s3_client: Boto3 S3 client.
        bucket:    S3 bucket name.
        video_id:  Episode identifier used as the cache key.
        segments:  Segment list returned by ``_items_to_segments``.
    """
    key = _transcript_cache_key(video_id)
    try:
        body = json.dumps(segments).encode("utf-8")
        s3_client.put_object(
            Bucket=bucket, Key=key, Body=body, ContentType="application/json"
        )
        logger.debug("[AdRemover] Transcript cache saved for %s → s3://%s/%s", video_id, bucket, key)
    except Exception as exc:
        logger.warning("[AdRemover] Could not save transcript cache for %s: %s", video_id, exc)


def _load_ad_segments_cache(s3_client, bucket: str, video_id: str) -> list[AdSegment] | None:
    """Try to load cached detected ad-segments from S3.

    Stored alongside the transcript cache at ``transcribe-cache/{video_id}_ads.json``.
    Returns ``None`` on miss or error so the caller falls back to a real Bedrock call.
    """
    from botocore.exceptions import ClientError

    key = _transcript_cache_key(video_id).replace(".json", "_ads.json")
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        data = json.loads(resp["Body"].read().decode("utf-8"))
        if isinstance(data, list):
            logger.info(
                "[AdRemover] Ad-segments cache HIT for %s (%d segments) — skipping Bedrock detection",
                video_id, len(data),
            )
            return data
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("NoSuchKey", "404"):
            logger.debug("[AdRemover] Ad-segments cache error for %s (%s): %s", video_id, code, exc)
    except Exception as exc:
        logger.debug("[AdRemover] Ad-segments cache load failed for %s: %s", video_id, exc)
    return None


def _save_ad_segments_cache(s3_client, bucket: str, video_id: str, ad_segments: list[AdSegment]) -> None:
    """Persist detected ad-segments to S3 for future reuse."""
    key = _transcript_cache_key(video_id).replace(".json", "_ads.json")
    try:
        body = json.dumps(ad_segments).encode("utf-8")
        s3_client.put_object(
            Bucket=bucket, Key=key, Body=body, ContentType="application/json"
        )
        logger.debug("[AdRemover] Ad-segments cache saved for %s", video_id)
    except Exception as exc:
        logger.warning("[AdRemover] Could not save ad-segments cache for %s: %s", video_id, exc)


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
    cache_enabled = os.environ.get("TRANSCRIBE_CACHE_ENABLED", "true").lower() not in ("false", "0", "no")
    # Skip caching for evaluator re-transcriptions (eval- prefix) — those target the cleaned
    # file and should not overwrite the original episode's cache entry.
    use_cache = cache_enabled and not video_id.startswith("eval-")

    s3_client = boto3.client("s3", region_name=region)
    transcribe_client = boto3.client("transcribe", region_name=region)

    # 0. Check transcript cache (skip expensive Transcribe job if already done)
    if use_cache:
        cached = _load_transcript_cache(s3_client, bucket, video_id)
        if cached is not None:
            return cached

    # 1. Upload audio to a temporary S3 key
    tmp_key = f"transcribe-tmp/{video_id}.mp3"
    logger.info("[AdRemover] Uploading %s to s3://%s/%s for transcription", mp3_path, bucket, tmp_key)
    retry_aws_call(
        lambda: s3_client.upload_file(mp3_path, bucket, tmp_key),
        label="s3.upload_file[transcribe-tmp]",
    )

    media_uri = f"s3://{bucket}/{tmp_key}"
    # Transcribe job names only allow [A-Za-z0-9_-] — sanitize video_id
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", video_id)[:64].strip("-")
    job_name = f"pad-{safe_id}-{uuid.uuid4().hex[:8]}"

    try:
        # 2. Start the transcription job
        logger.info("[AdRemover] Starting Transcribe job %s", job_name)
        retry_aws_call(
            lambda: transcribe_client.start_transcription_job(
                TranscriptionJobName=job_name,
                Media={"MediaFileUri": media_uri},
                MediaFormat="mp3",
                LanguageCode=language_code,
                Settings={"ShowSpeakerLabels": False, "ChannelIdentification": False},
            ),
            label="transcribe.start_transcription_job",
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

        # Save to cache so subsequent runs skip the Transcribe job
        if use_cache:
            _save_transcript_cache(s3_client, bucket, video_id, segments)

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
You are an expert podcast audio editor specialising in ad removal.

Below is the word-level transcript of a podcast episode. Each line has the
format:  [start_seconds - end_seconds]  text

## Your task
Identify EVERY advertisement and sponsored segment, including host-read ads
where the host personally delivers the ad copy in their own voice.

## Common ad signals to look for
- Sponsor introductions: "brought to you by", "this episode is sponsored by",
  "our sponsor", "a word from our sponsor", "thanks to X for supporting us",
  "partnered with", "presented by"
- Discount / promo language: "use code", "promo code", "get X% off",
  "first month free", "free trial", "limited time offer", "sign up today",
  "visit X dot com slash"
- URL / website mentions in a promotional context: "dot com slash podcast",
  website names followed by offers or calls to action
- Price mentions: "for just $X", "starting at", "plans from"
- Ad outros / transitions back to content: "now back to", "and we're back",
  "alright let's get into it", "back to the show", "let's continue"

## Rules
1. Only flag segments where you have clear evidence of advertising. Do NOT flag
   editorial discussion, interview content, or product mentions unless they are
   clearly promotional with a call-to-action. If a segment is ambiguous, leave it out.
2. Extend each segment's start time back by 2 seconds and end time forward
   by 2 seconds to avoid clipped transitions (but never below 0).
3. Return each ad break as a separate segment. Do NOT merge adjacent ad breaks —
   the code will handle merging. Keeping them separate lets each be verified independently.
4. Host-read ads blend naturally into the show's tone — look for the signals
   above even when the voice and style match the rest of the episode.
5. A single ad segment should rarely exceed 3 minutes (180 seconds). If you find
   yourself marking a very long stretch as an ad, double-check it is not content.

## Reasoning step (do NOT include in output)
Before writing the JSON, briefly note each candidate segment and why you
think it is an ad. Then output ONLY the final JSON array.

## Output format
Return ONLY a valid JSON array. Each element must have "start" and "end"
keys as floating-point seconds. If there are no ads return [].

Example:
[{{"start": 118.5, "end": 197.0}}, {{"start": 2308.0, "end": 2407.5}}]

Transcript:
{transcript}
"""


_AD_VERIFICATION_PROMPT = """You are reviewing a candidate ad segment extracted from a podcast transcript.
The segment below was flagged as a possible advertisement.

Segment [{start:.1f}s – {end:.1f}s]:
{text}

Is this segment an advertisement or sponsored content?
Answer with ONLY a JSON object on a single line:
{{"is_ad": true, "reason": "one-sentence explanation"}}

Criteria for YES (is_ad=true):
- Clear sponsor language, discount codes, promo URLs, or calls-to-action
- Explicit mention of a product/service being sold, with pricing or sign-up incentive

Criteria for NO (is_ad=false):
- Editorial discussion, interview, news analysis, or opinion content
- Casual product mention without promotion (e.g. "I used Notion for this")
- Content about the podcast itself (subscribe, leave a review)
"""


def _verify_ad_segment(
    segment: AdSegment,
    transcript_segments: list[dict],
    bedrock_client,
    model_id: str,
) -> bool:
    """Second-pass Bedrock call to confirm a candidate ad segment.

    Used for segments above ``AD_VERIFY_THRESHOLD_SECS`` where a false positive
    would remove a significant chunk of content.

    Args:
        segment:             Candidate ad segment with ``start`` and ``end``.
        transcript_segments: Full transcript (to extract the relevant text).
        bedrock_client:      Initialised ``bedrock-runtime`` boto3 client.
        model_id:            Bedrock model ID to use for verification.

    Returns:
        ``True`` if confirmed as an ad, ``False`` if the model rejects it.
        Defaults to ``True`` on any error so we never silently discard a real ad.
    """
    text = " ".join(
        s["text"] for s in transcript_segments
        if s["start"] >= segment["start"] - 5 and s["end"] <= segment["end"] + 5
    )
    if not text.strip():
        # No transcript coverage — keep the detection rather than risk missing an ad
        logger.warning(
            "[AdRemover] No transcript text for verification of [%.1f–%.1f] — keeping segment",
            segment["start"], segment["end"],
        )
        return True

    prompt = _AD_VERIFICATION_PROMPT.format(
        start=segment["start"],
        end=segment["end"],
        text=text[:2000],
    )

    try:
        response = retry_aws_call(
            lambda p=prompt: bedrock_client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": p}]}],
                inferenceConfig={"temperature": 0.0},
            ),
            label="bedrock.converse[verify]",
        )
        raw = response["output"]["message"]["content"][0]["text"].strip()

        # Parse the JSON response (model may wrap it in markdown)
        start_idx = raw.find("{")
        end_idx = raw.rfind("}") + 1
        if start_idx == -1 or end_idx == 0:
            logger.warning("[AdRemover] Verification response had no JSON — keeping segment: %s", raw[:200])
            return True

        result = json.loads(raw[start_idx:end_idx])
        is_ad = bool(result.get("is_ad", True))
        reason = result.get("reason", "")
        if is_ad:
            logger.info(
                "[AdRemover] Verification CONFIRMED ad [%.1f–%.1f]: %s",
                segment["start"], segment["end"], reason,
            )
        else:
            logger.info(
                "[AdRemover] Verification REJECTED [%.1f–%.1f] (not an ad): %s",
                segment["start"], segment["end"], reason,
            )
        return is_ad

    except Exception as exc:
        logger.warning(
            "[AdRemover] Verification call failed for [%.1f–%.1f]: %s — keeping segment",
            segment["start"], segment["end"], exc,
        )
        return True


def detect_ads(segments: list[dict]) -> list[AdSegment]:
    """Ask AWS Bedrock to identify ad segments in *segments*.

    Uses the Bedrock Converse API with the model specified by
    ``BEDROCK_MODEL_ID`` (default: ``us.anthropic.claude-sonnet-4-20250514-v1:0``).

    For transcripts that exceed ``AD_DETECT_MAX_CHARS``, the transcript is split
    into overlapping chunks (overlap controlled by ``AD_DETECT_OVERLAP_SECS``,
    default 60s). Each chunk is sent independently and results are merged with
    deduplication.

    Environment variables:
        AD_DETECT_MAX_CHARS     – Max transcript chars per chunk (default: 60000).
        AD_DETECT_OVERLAP_SECS  – Seconds of overlap between chunks (default: 60).

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
    # BEDROCK_DETECT_MODEL_ID overrides BEDROCK_MODEL_ID for detection (first-pass).
    # Use a cheaper model here (e.g. Haiku) and keep Sonnet for verification.
    _default_model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
    model_id = os.environ.get("BEDROCK_DETECT_MODEL_ID", _default_model)
    max_chars = int(os.environ.get("AD_DETECT_MAX_CHARS", "60000"))
    overlap_secs = float(os.environ.get("AD_DETECT_OVERLAP_SECS", "60"))

    chunks = _split_segments_into_chunks(segments, max_chars, overlap_secs)
    logger.info(
        "[AdRemover] Transcript split into %d chunk(s) (max_chars=%d, overlap=%.0fs)",
        len(chunks), max_chars, overlap_secs,
    )

    bedrock = boto3.client("bedrock-runtime", region_name=region)
    all_ads: list[AdSegment] = []

    for i, chunk in enumerate(chunks):
        transcript_lines = "\n".join(
            f"[{s['start']:.1f} - {s['end']:.1f}]  {s['text']}" for s in chunk
        )
        prompt = _AD_DETECTION_PROMPT.format(transcript=transcript_lines)

        logger.info(
            "[AdRemover] Sending chunk %d/%d to Bedrock (model=%s, segments=%d, chars=%d)",
            i + 1, len(chunks), model_id, len(chunk), len(transcript_lines),
        )

        response = retry_aws_call(
            lambda p=prompt: bedrock.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": p}]}],
                inferenceConfig={"temperature": 0.0},
            ),
            label=f"bedrock.converse[chunk-{i+1}]",
        )

        raw = response["output"]["message"]["content"][0]["text"]
        logger.debug("[AdRemover] Bedrock raw response (chunk %d): %s", i + 1, raw)

        chunk_ads = _parse_ad_response(raw)
        all_ads.extend(chunk_ads)

    merged = _merge_overlapping_ads(all_ads)

    # Fix #2: guard rails on duration — ads almost never exceed 3 min
    _MIN_AD_SECONDS = 5.0
    max_ad_secs = float(os.environ.get("MAX_AD_SEGMENT_SECS", "180"))
    verify_threshold = float(os.environ.get("AD_VERIFY_THRESHOLD_SECS", "90"))

    valid = []
    for seg in merged:
        duration = seg["end"] - seg["start"]
        if duration < _MIN_AD_SECONDS:
            logger.warning(
                "[AdRemover] Skipping suspiciously short ad segment "
                "(%.1fs < %.1fs minimum): start=%.1f end=%.1f",
                duration, _MIN_AD_SECONDS, seg["start"], seg["end"],
            )
            continue
        if duration > max_ad_secs:
            logger.warning(
                "[AdRemover] Skipping suspiciously long ad segment "
                "(%.1fs > %.1fs maximum): start=%.1f end=%.1f — likely a false positive",
                duration, max_ad_secs, seg["start"], seg["end"],
            )
            continue
        valid.append(seg)

    # Fix #4: second-pass verification for large segments
    if valid and verify_threshold > 0:
        confirmed = []
        for seg in valid:
            duration = seg["end"] - seg["start"]
            if duration >= verify_threshold:
                logger.info(
                    "[AdRemover] Segment [%.1f–%.1f] (%.0fs) exceeds verify threshold "
                    "(%.0fs) — running second-pass verification",
                    seg["start"], seg["end"], duration, verify_threshold,
                )
                if _verify_ad_segment(seg, segments, bedrock, model_id):
                    confirmed.append(seg)
                # If rejected, it is simply dropped (logged inside _verify_ad_segment)
            else:
                confirmed.append(seg)
        valid = confirmed

    if valid and segments:
        for ad in valid:
            covered = [
                s["text"] for s in segments
                if s["start"] >= ad["start"] - 5 and s["end"] <= ad["end"] + 5
            ]
            snippet = " ".join(covered)[:300]
            logger.info(
                "[AdRemover] Ad segment [%.1f–%.1f]: %s…",
                ad["start"], ad["end"], snippet,
            )

    logger.info("[AdRemover] Detected %d ad segment(s): %s", len(valid), valid)
    return valid


def _split_segments_into_chunks(
    segments: list[dict], max_chars: int, overlap_secs: float
) -> list[list[dict]]:
    """Split transcript segments into chunks that fit within max_chars.

    Each chunk overlaps with the next by at least *overlap_secs* worth of
    segments so ads at chunk boundaries are seen by both chunks.
    """
    all_lines = [
        (s, f"[{s['start']:.1f} - {s['end']:.1f}]  {s['text']}") for s in segments
    ]

    total_chars = sum(len(line) + 1 for _, line in all_lines)
    if total_chars <= max_chars:
        return [segments]

    chunks: list[list[dict]] = []
    i = 0
    while i < len(all_lines):
        chunk_segments: list[dict] = []
        char_count = 0

        j = i
        while j < len(all_lines):
            line_len = len(all_lines[j][1]) + 1
            if char_count + line_len > max_chars and chunk_segments:
                break
            chunk_segments.append(all_lines[j][0])
            char_count += line_len
            j += 1

        chunks.append(chunk_segments)

        if j >= len(all_lines):
            break

        # Walk back from j to find the overlap start point
        overlap_start_time = all_lines[j][0]["start"] - overlap_secs
        next_i = j
        while next_i > i and all_lines[next_i - 1][0]["start"] >= overlap_start_time:
            next_i -= 1

        # Ensure forward progress
        i = max(next_i, i + 1) if next_i <= i else next_i

    return chunks


def _parse_ad_response(raw: str) -> list[AdSegment]:
    """Extract a JSON array of ad segments from a model response string."""
    end_idx = raw.rfind("]")
    if end_idx == -1:
        logger.warning("[AdRemover] Bedrock response contained no JSON array — assuming no ads.")
        return []

    search_end = end_idx
    while search_end >= 0:
        start_idx = raw.rfind("[", 0, search_end)
        if start_idx == -1:
            break
        candidate = raw[start_idx : end_idx + 1]
        try:
            result = json.loads(candidate)
            if isinstance(result, list):
                valid = []
                for seg in result:
                    if isinstance(seg, dict) and "start" in seg and "end" in seg:
                        valid.append({"start": float(seg["start"]), "end": float(seg["end"])})
                    else:
                        logger.warning("[AdRemover] Ignoring malformed ad segment: %s", seg)
                return valid
        except json.JSONDecodeError:
            pass
        search_end = start_idx

    logger.warning("[AdRemover] Could not extract valid JSON array from Bedrock response — assuming no ads.")
    return []


def _merge_overlapping_ads(ads: list[AdSegment]) -> list[AdSegment]:
    """Merge overlapping or adjacent ad segments from multiple chunks."""
    if not ads:
        return []

    sorted_ads = sorted(ads, key=lambda s: s["start"])
    merged: list[AdSegment] = [{"start": sorted_ads[0]["start"], "end": sorted_ads[0]["end"]}]

    for seg in sorted_ads[1:]:
        if seg["start"] <= merged[-1]["end"] + 2:  # Fix #3: 5s→2s — was merging unrelated ad blocks
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
        else:
            merged.append({"start": seg["start"], "end": seg["end"]})

    return merged


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

    # Build ffmpeg atrim + concat + optional loudnorm filter_complex.
    # loudnorm (EBU R128) equalises loudness across all keep intervals so that
    # volume discontinuities at cut points are inaudible.
    loudnorm = os.environ.get("SPLICE_LOUDNORM", "true").lower() not in ("false", "0", "no")
    filter_parts = [
        f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]"
        for i, (start, end) in enumerate(keep)
    ]
    inputs = "".join(f"[a{i}]" for i in range(len(keep)))
    concat_out = "concat_out"
    filter_complex = (
        ";".join(filter_parts)
        + f";{inputs}concat=n={len(keep)}:v=0:a=1[{concat_out}]"
    )
    if loudnorm:
        filter_complex += f";[{concat_out}]loudnorm=I=-16:TP=-1.5:LRA=11[out]"
    else:
        filter_complex = filter_complex.replace(f"[{concat_out}]", "[out]", 1)

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

def remove_ads(mp3_path: str, video_id: str, tmp_dir: str) -> tuple[str, list[AdSegment]]:
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
        A tuple of ``(cleaned_path, ad_segments)`` where *cleaned_path* is the
        path to the cleaned audio file (or the original *mp3_path* if ad removal
        was skipped or failed), and *ad_segments* is the list of detected ad
        intervals (empty list when none were found or removal was skipped).
    """
    if os.environ.get("REMOVE_ADS", "true").lower() in ("false", "0", "no"):
        logger.info("[AdRemover] REMOVE_ADS=false — skipping ad removal for %s", video_id)
        return mp3_path, []

    dry_run = os.environ.get("REMOVE_ADS_DRY_RUN", "false").lower() in ("true", "1", "yes")
    if dry_run:
        logger.info("[AdRemover] REMOVE_ADS_DRY_RUN=true — will detect ads but skip splicing for %s", video_id)

    logger.info("[AdRemover] Starting ad removal for %s", video_id)

    # Check ad-segments cache first (requires transcript cache to be enabled too).
    # If both transcript and ad-segments are cached, skip Transcribe + Bedrock entirely.
    cache_enabled = os.environ.get("TRANSCRIBE_CACHE_ENABLED", "true").lower() not in ("false", "0", "no")
    use_cache = cache_enabled and not video_id.startswith("eval-")
    ad_segments: list[AdSegment] = []
    segments: list[dict] = []

    if use_cache:
        bucket = os.environ.get("S3_BUCKET", "")
        if bucket:
            region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
            _s3 = boto3.client("s3", region_name=region)
            cached_ads = _load_ad_segments_cache(_s3, bucket, video_id)
            if cached_ads is not None:
                ad_segments = cached_ads
                # Skip transcription + detection — jump straight to snap + splice
                logger.info("[AdRemover] Using cached ad-segments for %s — skipping Transcribe+Bedrock", video_id)
                if not ad_segments:
                    logger.info("[AdRemover] Cached ad-segments empty (no ads) for %s — using original file", video_id)
                    return mp3_path, []
                if os.environ.get("AD_SNAP_TO_SILENCE", "true").lower() not in ("false", "0", "no"):
                    ad_segments = snap_ad_boundaries(ad_segments, mp3_path)
                if dry_run:
                    total_ad_secs = sum(s["end"] - s["start"] for s in ad_segments)
                    logger.info(
                        "[AdRemover] DRY-RUN: would remove %d cached ad segment(s) totalling %.1fs from %s",
                        len(ad_segments), total_ad_secs, video_id,
                    )
                    return mp3_path, ad_segments
                cleaned_path = os.path.join(tmp_dir, f"{video_id}_clean.mp3")
                try:
                    splice_audio(mp3_path, ad_segments, cleaned_path)
                except Exception as exc:
                    logger.error("[AdRemover] Splicing failed for %s: %s — using original file", video_id, exc)
                    return mp3_path, ad_segments
                logger.info("[AdRemover] Ad removal complete (cached) for %s → %s", video_id, cleaned_path)
                return cleaned_path, ad_segments

    try:
        segments = transcribe_audio(mp3_path, video_id)
    except Exception as exc:
        logger.error("[AdRemover] Transcription failed for %s: %s — using original file", video_id, exc)
        return mp3_path, []

    try:
        ad_segments = detect_ads(segments)
    except Exception as exc:
        logger.error("[AdRemover] Ad detection failed for %s: %s — using original file", video_id, exc)
        return mp3_path, []

    # Save detection result to cache so retries (after splice failure) skip Bedrock
    if use_cache:
        bucket = os.environ.get("S3_BUCKET", "")
        if bucket:
            region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
            _s3 = boto3.client("s3", region_name=region)
            _save_ad_segments_cache(_s3, bucket, video_id, ad_segments)

    if not ad_segments:
        logger.info("[AdRemover] No ads detected for %s — using original file", video_id)
        return mp3_path, []

    # Fix #5: snap boundaries to silence gaps for cleaner cuts
    if os.environ.get("AD_SNAP_TO_SILENCE", "true").lower() not in ("false", "0", "no"):
        ad_segments = snap_ad_boundaries(ad_segments, mp3_path)

    if dry_run:
        total_ad_secs = sum(s["end"] - s["start"] for s in ad_segments)
        logger.info(
            "[AdRemover] DRY-RUN: would remove %d ad segment(s) totalling %.1fs from %s — skipping splice",
            len(ad_segments), total_ad_secs, video_id,
        )
        return mp3_path, ad_segments

    cleaned_path = os.path.join(tmp_dir, f"{video_id}_clean.mp3")
    try:
        splice_audio(mp3_path, ad_segments, cleaned_path)
    except Exception as exc:
        logger.error("[AdRemover] Splicing failed for %s: %s — using original file", video_id, exc)
        return mp3_path, ad_segments

    logger.info("[AdRemover] Ad removal complete for %s → %s", video_id, cleaned_path)
    return cleaned_path, ad_segments
