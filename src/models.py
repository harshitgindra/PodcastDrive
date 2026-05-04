"""Data models for YouTube Playlist to Podcast Lambda."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class PlaylistMeta:
    """Playlist-level metadata extracted from YouTube."""

    title: str
    description: str
    uploader: str
    channel_url: str
    webpage_url: str
    playlist_id: str


@dataclass
class VideoEntry:
    """Per-video metadata extracted from a YouTube playlist."""

    video_id: str
    title: str
    description: str
    duration: Optional[int]  # seconds
    upload_date: str  # YYYYMMDD format from yt_dlp
    thumbnail: str
    webpage_url: str
    playlist_index: Optional[int]


@dataclass
class EpisodeMeta:
    """Episode metadata combining video info with S3/CloudFront details."""

    video_id: str
    title: str
    description: str
    duration: Optional[int]
    upload_date: str
    thumbnail: str
    webpage_url: str
    playlist_index: Optional[int]
    s3_key: str  # e.g. "PLxyz/episodes/abc123.mp3"
    file_size: int  # bytes
    cloudfront_url: str  # full URL for enclosure
