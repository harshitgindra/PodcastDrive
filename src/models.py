"""Data models for YouTube Playlist to Podcast Lambda."""

from dataclasses import dataclass


@dataclass
class PlaylistMeta:
    """Playlist-level metadata extracted from YouTube."""

    title: str
    description: str
    uploader: str
    channel_url: str
    webpage_url: str
    playlist_id: str
    thumbnail: str = ""  # Playlist-level artwork URL


@dataclass
class VideoEntry:
    """Per-video metadata extracted from a YouTube playlist."""

    video_id: str
    title: str
    description: str
    duration: int | None  # seconds
    upload_date: str  # YYYYMMDD format from yt_dlp
    thumbnail: str
    webpage_url: str
    playlist_index: int | None
    live_status: str | None = None  # e.g. "is_upcoming", "is_live", "was_live", "not_live"


@dataclass
class EpisodeMeta:
    """Episode metadata combining video info with S3/CloudFront details."""

    video_id: str
    title: str
    description: str
    duration: int | None
    upload_date: str
    thumbnail: str
    webpage_url: str
    playlist_index: int | None
    s3_key: str  # e.g. "PLxyz/episodes/abc123.mp3"
    file_size: int  # bytes
    cloudfront_url: str  # full URL for enclosure
