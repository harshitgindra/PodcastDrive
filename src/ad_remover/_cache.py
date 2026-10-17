"""S3 read/write helpers for the transcript, summary and ad-segment caches.

These functions receive an already-constructed S3 client as their first
argument — they never build one themselves — so they have no ``boto3``
dependency and no patched-symbol routing to worry about internally. Key
construction is delegated to :mod:`ad_remover._cachekeys`. ``__init__``
re-exports every name so callers and the test suite keep using
``ad_remover.<name>``.
"""

from __future__ import annotations

import json
import logging

from ad_remover._cachekeys import _cache_key, _cache_read_keys
from ad_remover._segments import AdSegment

logger = logging.getLogger("ad_remover")


def _get_cached_bytes(s3_client, bucket: str, keys: list[str], label: str, video_id: str) -> bytes | None:
    """Return the body of the first key in *keys* that exists, else ``None``.

    Args:
        s3_client: Boto3 S3 client.
        bucket:    S3 bucket name.
        keys:      Candidate keys, best match first.
        label:     Human-readable cache name, used only for log messages.
        video_id:  Episode identifier, used only for log messages.
    """
    from botocore.exceptions import ClientError

    for key in keys:
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key)
            return resp["Body"].read()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("NoSuchKey", "404"):
                logger.debug("[AdRemover] %s cache error for %s (%s): %s", label, video_id, code, exc)
        except Exception as exc:
            logger.debug("[AdRemover] %s cache load failed for %s: %s", label, video_id, exc)
    logger.debug("[AdRemover] %s cache MISS for %s", label, video_id)
    return None


def _load_transcript_cache(s3_client, bucket: str, video_id: str, namespace: str = "") -> list[dict] | None:
    """Try to load a cached transcript from S3.

    Args:
        s3_client: Boto3 S3 client.
        bucket:    S3 bucket name.
        video_id:  Episode identifier used as the cache key.
        namespace: Per-podcast cache namespace (see :func:`_cache_key`).

    Returns:
        Cached segment list, or ``None`` if not found or loading fails.
    """
    raw = _get_cached_bytes(
        s3_client, bucket, _cache_read_keys(video_id, ".json", namespace), "Transcript", video_id
    )
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.debug("[AdRemover] Transcript cache decode failed for %s: %s", video_id, exc)
        return None
    if isinstance(data, list):
        logger.info(
            "[AdRemover] Transcript cache HIT for %s (%d segments) — skipping Transcribe job",
            video_id,
            len(data),
        )
        return data
    logger.warning("[AdRemover] Cached transcript for %s is not a list — ignoring", video_id)
    return None


def _save_transcript_cache(
    s3_client, bucket: str, video_id: str, segments: list[dict], namespace: str = ""
) -> None:
    """Persist a transcript segment list to S3 for future reuse.

    Args:
        s3_client: Boto3 S3 client.
        bucket:    S3 bucket name.
        video_id:  Episode identifier used as the cache key.
        segments:  Segment list returned by ``_items_to_segments``.
        namespace: Per-podcast cache namespace (see :func:`_cache_key`).
    """
    key = _cache_key(video_id, ".json", namespace)
    try:
        body = json.dumps(segments).encode("utf-8")
        s3_client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
        logger.debug("[AdRemover] Transcript cache saved for %s → s3://%s/%s", video_id, bucket, key)
    except Exception as exc:
        logger.warning("[AdRemover] Could not save transcript cache for %s: %s", video_id, exc)


def _load_summary_cache(s3_client, bucket: str, video_id: str, namespace: str = "") -> str | None:
    """Try to load a cached episode summary from S3."""
    raw = _get_cached_bytes(
        s3_client, bucket, _cache_read_keys(video_id, "_summary.txt", namespace), "Summary", video_id
    )
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except Exception as exc:
        logger.debug("[AdRemover] Summary cache decode failed for %s: %s", video_id, exc)
        return None
    if text:
        logger.info("[AdRemover] Summary cache HIT for %s", video_id)
        return text
    return None


def _save_summary_cache(s3_client, bucket: str, video_id: str, summary: str, namespace: str = "") -> None:
    """Persist an episode summary to S3."""
    key = _cache_key(video_id, "_summary.txt", namespace)
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=summary.encode("utf-8"),
            ContentType="text/plain",
        )
        logger.debug("[AdRemover] Summary cache saved for %s", video_id)
    except Exception as exc:
        logger.warning("[AdRemover] Could not save summary cache for %s: %s", video_id, exc)


def _save_transcript_text(
    s3_client, bucket: str, video_id: str, segments: list[dict], namespace: str = ""
) -> None:
    """Persist the full transcript as plain text to S3 alongside the segment cache."""
    key = _cache_key(video_id, ".txt", namespace)
    text = "\n".join(f"[{s['start']:.1f}s]  {s['text']}" for s in segments)
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/plain",
        )
        logger.info("[AdRemover] Transcript text saved to s3://%s/%s", bucket, key)
    except Exception as exc:
        logger.warning("[AdRemover] Could not save transcript text for %s: %s", video_id, exc)


def _load_ad_segments_cache(s3_client, bucket: str, video_id: str, namespace: str = "") -> list[AdSegment] | None:
    """Try to load cached detected ad-segments from S3.

    Stored alongside the transcript cache at
    ``transcribe-cache/{namespace}/{video_id}_ads.json``.  Returns ``None`` on
    miss or error so the caller falls back to a real Bedrock call.
    """
    raw = _get_cached_bytes(
        s3_client, bucket, _cache_read_keys(video_id, "_ads.json", namespace), "Ad-segments", video_id
    )
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.debug("[AdRemover] Ad-segments cache decode failed for %s: %s", video_id, exc)
        return None
    if isinstance(data, list):
        logger.info(
            "[AdRemover] Ad-segments cache HIT for %s (%d segments) — skipping Bedrock detection",
            video_id,
            len(data),
        )
        return data
    return None


def _save_ad_segments_cache(
    s3_client, bucket: str, video_id: str, ad_segments: list[AdSegment], namespace: str = ""
) -> None:
    """Persist detected ad-segments to S3 for future reuse.

    Empty results are intentionally not cached: a cache miss is always safer
    than permanently treating a false-negative detection as confirmed "no ads".
    On the next run, Bedrock will be called again and given another chance to
    detect ads with the latest prompt and model.
    """
    if not ad_segments:
        logger.debug(
            "[AdRemover] Ad-segments cache skipped for %s (empty result — not persisting to avoid false-negative lock-in)",
            video_id,
        )
        return
    key = _cache_key(video_id, "_ads.json", namespace)
    try:
        body = json.dumps(ad_segments).encode("utf-8")
        s3_client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
        logger.debug("[AdRemover] Ad-segments cache saved for %s", video_id)
    except Exception as exc:
        logger.warning("[AdRemover] Could not save ad-segments cache for %s: %s", video_id, exc)
