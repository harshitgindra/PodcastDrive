"""S3 storage and CloudFront invalidation for YouTube Playlist to Podcast."""

import logging
import os
import time

import boto3

logger = logging.getLogger(__name__)


class S3Manager:
    """Handles S3 interactions and CloudFront cache invalidation.

    All operations are scoped to a specific playlist_id prefix within the
    configured S3 bucket, ensuring multi-playlist isolation.

    Args:
        bucket: Name of the S3 bucket (e.g. ``"your-bucket-name"``).
        playlist_id: YouTube playlist ID used as the S3 key prefix.
    """

    def __init__(self, bucket: str, playlist_id: str) -> None:
        self.bucket = bucket
        self.playlist_id = playlist_id
        self.s3_client = boto3.client("s3")
        self._cf_client = None
        self._distribution_id = os.environ.get("CLOUDFRONT_DISTRIBUTION_ID", "")
        # Note: bucket name should come from the S3_BUCKET environment variable,
        # not be hardcoded. See config.env.example for configuration.

    def list_existing_episodes(self) -> set[str]:
        """List all video IDs that have MP3s in S3 under this playlist's prefix.

        Uses paginated ``list_objects_v2`` to handle playlists with many
        episodes.

        Returns:
            A set of video_id strings extracted from S3 keys matching
            ``{playlist_id}/episodes/{video_id}.mp3``.
        """
        prefix = f"{self.playlist_id}/episodes/"
        video_ids: set[str] = set()

        paginator = self.s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                filename = key.rsplit("/", 1)[-1]
                if filename.endswith(".mp3"):
                    video_id = filename[:-4]
                    video_ids.add(video_id)

        return video_ids

    def upload_episode(self, local_path: str, video_id: str) -> str:
        """Upload an MP3 file to S3.

        Args:
            local_path: Path to the local MP3 file.
            video_id: Video ID used to construct the S3 key.

        Returns:
            The S3 key where the file was uploaded
            (``{playlist_id}/episodes/{video_id}.mp3``).
        """
        key = f"{self.playlist_id}/episodes/{video_id}.mp3"
        logger.info("Uploading episode %s to s3://%s/%s", video_id, self.bucket, key)
        self.s3_client.upload_file(
            local_path,
            self.bucket,
            key,
            ExtraArgs={"ContentType": "audio/mpeg"},
        )
        return key

    def delete_episode(self, video_id: str) -> None:
        """Delete an MP3 episode from S3.

        Args:
            video_id: Video ID of the episode to delete.
        """
        key = f"{self.playlist_id}/episodes/{video_id}.mp3"
        logger.info("Deleting episode %s from s3://%s/%s", video_id, self.bucket, key)
        self.s3_client.delete_object(Bucket=self.bucket, Key=key)

    def upload_feed(self, xml_content: str) -> str:
        """Upload the RSS feed XML to S3 and invalidate CloudFront cache.

        Args:
            xml_content: The RSS XML string to upload.

        Returns:
            The S3 key where the feed was uploaded.
        """
        key = f"{self.playlist_id}/feed.xml"
        logger.info("Uploading feed to s3://%s/%s", self.bucket, key)
        self.s3_client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=xml_content.encode("utf-8"),
            ContentType="application/rss+xml",
            CacheControl="max-age=300, s-maxage=60",
        )

        # Invalidate CloudFront cache for the feed
        self._invalidate_cloudfront(f"/{key}")

        # Ping Overcast to trigger immediate feed crawl
        self._ping_overcast()

        return key

    def get_object_size(self, key: str) -> int:
        """Return the content length (in bytes) of an S3 object.

        Args:
            key: Full S3 key of the object.

        Returns:
            The size of the object in bytes.
        """
        response = self.s3_client.head_object(Bucket=self.bucket, Key=key)
        return response["ContentLength"]

    def _invalidate_cloudfront(self, path: str) -> None:
        """Create a CloudFront invalidation for the given path.

        Requires CLOUDFRONT_DISTRIBUTION_ID environment variable to be set.
        Silently skips if not configured.

        Args:
            path: The CloudFront path to invalidate (e.g. ``/PLxyz/feed.xml``).
        """
        if not self._distribution_id:
            logger.debug("No CLOUDFRONT_DISTRIBUTION_ID set, skipping invalidation")
            return

        try:
            if self._cf_client is None:
                self._cf_client = boto3.client("cloudfront")

            caller_ref = f"feed-{self.playlist_id}-{int(time.time())}"
            self._cf_client.create_invalidation(
                DistributionId=self._distribution_id,
                InvalidationBatch={
                    "Paths": {
                        "Quantity": 1,
                        "Items": [path],
                    },
                    "CallerReference": caller_ref,
                },
            )
            logger.info("CloudFront invalidation created for %s", path)
        except Exception as exc:
            logger.warning("CloudFront invalidation failed for %s: %s", path, exc)

    def _ping_overcast(self) -> None:
        """Ping Overcast to trigger an immediate feed crawl.

        Uses Overcast's ping API: https://overcast.fm/podcasterinfo
        Silently skips on failure.
        """
        cloudfront_base = os.environ.get("CLOUDFRONT_BASE", "")
        if not cloudfront_base:
            logger.debug("No CLOUDFRONT_BASE set, skipping Overcast ping")
            return
        feed_url = f"{cloudfront_base}/{self.playlist_id}/feed.xml"

        try:
            import ssl
            import urllib.request
            import urllib.parse
            import certifi

            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            params = urllib.parse.urlencode({"urlprefix": feed_url})
            url = f"https://overcast.fm/ping?{params}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
                logger.info("Overcast ping sent for %s (status=%d)", feed_url, resp.status)
        except Exception as exc:
            logger.warning("Overcast ping failed: %s", exc)
