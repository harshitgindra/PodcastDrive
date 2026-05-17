"""S3 storage and CloudFront invalidation for YouTube Playlist to Podcast."""

import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

from utils import retry_aws_call

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
        self._lifecycle_days_set: int | None = None  # cache to skip redundant PUTs
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
        pages = retry_aws_call(
            lambda: list(paginator.paginate(Bucket=self.bucket, Prefix=prefix)),
            label="s3.list_objects_v2",
        )
        for page in pages:
            for obj in page.get("Contents", []):
                key = obj["Key"]
                filename = key.rsplit("/", 1)[-1]
                if filename.endswith(".mp3"):
                    video_id = filename[:-4]
                    video_ids.add(video_id)

        return video_ids

    def upload_episode(self, local_path: str, video_id: str, max_age_days: int = 5) -> str:
        """Upload an MP3 file to S3 and configure automatic expiration.

        Sets an S3 lifecycle rule so the object is automatically deleted after
        *max_age_days* days, removing the need for explicit age-based deletion.

        Args:
            local_path: Path to the local MP3 file.
            video_id: Video ID used to construct the S3 key.
            max_age_days: Number of days after which S3 should auto-delete
                this object (default: 5).

        Returns:
            The S3 key where the file was uploaded
            (``{playlist_id}/episodes/{video_id}.mp3``).
        """
        key = f"{self.playlist_id}/episodes/{video_id}.mp3"
        logger.info("Uploading episode %s to s3://%s/%s", video_id, self.bucket, key)
        retry_aws_call(
            lambda: self.s3_client.upload_file(
                local_path,
                self.bucket,
                key,
                ExtraArgs={
                    "ContentType": "audio/mpeg",
                    "Tagging": f"expiry-days={max_age_days}",
                },
            ),
            label="s3.upload_file",
        )
        # Only PUT the lifecycle rule when max_age_days actually changes
        if self._lifecycle_days_set != max_age_days:
            self.set_lifecycle_expiration(max_age_days)
            self._lifecycle_days_set = max_age_days
        return key

    def set_lifecycle_expiration(self, max_age_days: int) -> None:
        """Upsert an S3 lifecycle rule to auto-delete episodes after *max_age_days* days.

        The rule is scoped to the ``{playlist_id}/episodes/`` prefix so each
        playlist can have an independent retention period.  Existing rules for
        other playlists are preserved.

        Args:
            max_age_days: Number of days after upload before S3 expires the
                object automatically.
        """
        rule_id = f"expire-{self.playlist_id}-episodes"
        prefix = f"{self.playlist_id}/episodes/"

        try:
            # Fetch existing rules so we don't overwrite other playlists' rules
            try:
                existing = self.s3_client.get_bucket_lifecycle_configuration(
                    Bucket=self.bucket
                )
                rules = [
                    r for r in existing.get("Rules", [])
                    if r.get("ID") != rule_id
                ]
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "NoSuchLifecycleConfiguration":
                    rules = []
                else:
                    raise

            rules.append({
                "ID": rule_id,
                "Status": "Enabled",
                "Filter": {"Prefix": prefix},
                "Expiration": {"Days": max_age_days},
            })

            self.s3_client.put_bucket_lifecycle_configuration(
                Bucket=self.bucket,
                LifecycleConfiguration={"Rules": rules},
            )
            logger.info(
                "Lifecycle rule '%s' set: expire after %d days (prefix=%s)",
                rule_id, max_age_days, prefix,
            )
        except Exception as exc:
            logger.warning(
                "Failed to set lifecycle rule for %s: %s", self.playlist_id, exc
            )

    def delete_episode(self, video_id: str) -> None:
        """Delete an MP3 episode from S3.

        Args:
            video_id: Video ID of the episode to delete.
        """
        key = f"{self.playlist_id}/episodes/{video_id}.mp3"
        logger.info("Deleting episode %s from s3://%s/%s", video_id, self.bucket, key)
        retry_aws_call(
            lambda: self.s3_client.delete_object(Bucket=self.bucket, Key=key),
            label="s3.delete_object",
        )

    def upload_feed(self, xml_content: str) -> str:
        """Upload the RSS feed XML to S3 and invalidate CloudFront cache.

        Args:
            xml_content: The RSS XML string to upload.

        Returns:
            The S3 key where the feed was uploaded.
        """
        key = f"{self.playlist_id}/feed.xml"
        logger.info("Uploading feed to s3://%s/%s", self.bucket, key)
        retry_aws_call(
            lambda: self.s3_client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=xml_content.encode("utf-8"),
                ContentType="application/rss+xml",
                CacheControl="max-age=300, s-maxage=60",
            ),
            label="s3.put_object",
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
        response = retry_aws_call(
            lambda: self.s3_client.head_object(Bucket=self.bucket, Key=key),
            label="s3.head_object",
        )
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
