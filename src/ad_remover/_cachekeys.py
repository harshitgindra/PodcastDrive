"""Pure helpers that build the S3 cache keys for transcripts and ad segments.

Key construction is deterministic and namespace-aware: episode identifiers are
only unique *within* a feed (RSS ``<guid>`` values are frequently bare integers
that collide across podcasts), so every key is scoped by a per-podcast
namespace. These functions have no AWS dependency — they only read the
``TRANSCRIBE_CACHE_PREFIX`` setting — and are imported back into the
:mod:`ad_remover` namespace by ``__init__``.
"""

from __future__ import annotations

import re

import settings


def _safe_namespace(namespace: str) -> str:
    """Return *namespace* reduced to characters that are safe in an S3 key."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", namespace).strip("-.")
    return cleaned[:80]


def _cache_key(video_id: str, suffix: str, namespace: str = "") -> str:
    """Return the S3 cache key for *video_id* under *namespace*.

    Episode identifiers are only unique *within* a feed.  RSS ``<guid>`` values
    are frequently bare integers, so ``episode_id_from_guid`` produces ids like
    ``"1"`` or ``"12345"`` that collide across podcasts.  Without a namespace,
    two different episodes shared one transcript and one ad-segment cache entry,
    and the second podcast had another show's ad timestamps spliced out of it.

    Args:
        video_id:  Episode identifier (unique within the namespace).
        suffix:    Key suffix, e.g. ``".json"``, ``"_ads.json"``.
        namespace: Per-podcast namespace, usually the S3 slug.  Empty means the
            legacy flat layout.

    Returns:
        The S3 object key.
    """
    prefix = settings.get("TRANSCRIBE_CACHE_PREFIX")
    ns = _safe_namespace(namespace)
    if ns:
        return f"{prefix}/{ns}/{video_id}{suffix}"
    return f"{prefix}/{video_id}{suffix}"


def _cache_read_keys(video_id: str, suffix: str, namespace: str = "") -> list[str]:
    """Return the keys to try when *reading* a cache entry, best match first.

    The flat (un-namespaced) key is included as a fallback so entries written
    before namespacing are still honoured instead of forcing a re-transcription
    of the whole back catalogue.
    """
    keys = [_cache_key(video_id, suffix, namespace)]
    flat = _cache_key(video_id, suffix, "")
    if flat != keys[0]:
        keys.append(flat)
    return keys


def _transcript_cache_key(video_id: str, namespace: str = "") -> str:
    """Return the S3 key used for caching a transcript."""
    return _cache_key(video_id, ".json", namespace)
