"""Naming for the episode audio objects in S3 and on the CDN.

An episode keeps one id for its whole life, but its audio can be rewritten: a
repair re-splices an already published episode. Overwriting the object in place
desynchronises the CDN (which still serves the old bytes under that exact path)
from the feed (which advertises the new byte length), and a player that gets a
body shorter or longer than the advertised length stalls, re-downloads or
mis-seeks.

So each rewrite gets its own object name instead. The first publication stays
``<id>.mp3`` so nothing already published moves; revision N is ``<id>.rN.mp3``.
The guid is the episode id and is independent of the enclosure URL, so a player
sees the same episode with new audio, not a duplicate, and the previous object
is left for the bucket lifecycle rule to expire.
"""

from __future__ import annotations

import re

#: Trailing revision marker on an episode filename. Deliberately anchored and
#: digits-only: episode ids themselves contain dots (``9to5mac.com-e4512626c``).
_REVISION_SUFFIX = re.compile(r"\.r([1-9][0-9]*)$")

#: Manifest field holding the revision currently published for an episode.
REVISION_FIELD = "audio_revision"


def episode_filename(video_id: str, revision: int = 0) -> str:
    """Return the object filename for *video_id* at *revision*."""
    if revision <= 0:
        return f"{video_id}.mp3"
    return f"{video_id}.r{revision}.mp3"


def episode_key(playlist_id: str, video_id: str, revision: int = 0) -> str:
    """Return the full S3 key for *video_id* at *revision*."""
    return f"{playlist_id}/episodes/{episode_filename(video_id, revision)}"


def split_episode_filename(filename: str) -> tuple[str, int] | None:
    """Split an episode filename into (video_id, revision).

    Returns None for anything that is not an episode audio object, so callers
    listing a prefix can ignore strays without guessing.
    """
    if not filename.endswith(".mp3"):
        return None
    stem = filename[: -len(".mp3")]
    if not stem:
        return None
    match = _REVISION_SUFFIX.search(stem)
    if match:
        return stem[: match.start()], int(match.group(1))
    return stem, 0


def published_revision(manifest: dict | None, video_id: str) -> int:
    """Revision recorded for *video_id*, or 0 when none is (the plain name)."""
    entry = (manifest or {}).get(video_id)
    if not isinstance(entry, dict):
        return 0
    revision = entry.get(REVISION_FIELD)
    if isinstance(revision, int) and revision > 0:
        return revision
    return 0


def published_key(playlist_id: str, video_id: str, manifest: dict | None) -> str:
    """S3 key of the audio currently published for *video_id*."""
    return episode_key(playlist_id, video_id, published_revision(manifest, video_id))


def published_filename(video_id: str, manifest: dict | None) -> str:
    """Filename of the audio currently published for *video_id*."""
    return episode_filename(video_id, published_revision(manifest, video_id))
