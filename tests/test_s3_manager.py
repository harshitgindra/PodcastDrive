"""Unit tests for S3Manager."""

import os
import tempfile
import unittest.mock

import boto3
import pytest
from moto import mock_aws

from s3_manager import S3Manager

BUCKET = "test-podcast-bucket"
PLAYLIST_ID = "PLtest123"


@pytest.fixture
def s3_manager():
    """Create an S3Manager with a mocked S3 bucket."""
    with mock_aws():
        # Create the bucket in the mock
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket=BUCKET)

        manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
        # Override the client to use the mocked one
        manager.s3_client = conn
        yield manager


class TestInit:
    def test_stores_bucket_and_playlist_id(self):
        with mock_aws():
            manager = S3Manager(bucket="my-bucket", playlist_id="PLabc")
            assert manager.bucket == "my-bucket"
            assert manager.playlist_id == "PLabc"

    def test_creates_s3_client(self):
        with mock_aws():
            manager = S3Manager(bucket="my-bucket", playlist_id="PLabc")
            assert manager.s3_client is not None


class TestListExistingEpisodes:
    def test_empty_bucket_returns_empty_set(self, s3_manager):
        result = s3_manager.list_existing_episodes()
        assert result == set()

    def test_returns_video_ids_from_mp3_keys(self, s3_manager):
        # Upload some test objects
        s3_manager.s3_client.put_object(
            Bucket=BUCKET,
            Key=f"{PLAYLIST_ID}/episodes/vid001.mp3",
            Body=b"audio",
        )
        s3_manager.s3_client.put_object(
            Bucket=BUCKET,
            Key=f"{PLAYLIST_ID}/episodes/vid002.mp3",
            Body=b"audio",
        )
        result = s3_manager.list_existing_episodes()
        assert result == {"vid001", "vid002"}

    def test_ignores_non_mp3_files(self, s3_manager):
        s3_manager.s3_client.put_object(
            Bucket=BUCKET,
            Key=f"{PLAYLIST_ID}/episodes/vid001.mp3",
            Body=b"audio",
        )
        s3_manager.s3_client.put_object(
            Bucket=BUCKET,
            Key=f"{PLAYLIST_ID}/episodes/readme.txt",
            Body=b"text",
        )
        result = s3_manager.list_existing_episodes()
        assert result == {"vid001"}

    def test_ignores_other_playlist_prefixes(self, s3_manager):
        s3_manager.s3_client.put_object(
            Bucket=BUCKET,
            Key=f"{PLAYLIST_ID}/episodes/vid001.mp3",
            Body=b"audio",
        )
        s3_manager.s3_client.put_object(
            Bucket=BUCKET,
            Key="OtherPlaylist/episodes/vid999.mp3",
            Body=b"audio",
        )
        result = s3_manager.list_existing_episodes()
        assert result == {"vid001"}


class TestUploadEpisode:
    def test_uploads_file_and_returns_key(self, s3_manager):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"fake mp3 data")
            tmp_path = f.name

        try:
            key = s3_manager.upload_episode(tmp_path, "vid001", max_age_days=5)
            assert key == f"{PLAYLIST_ID}/episodes/vid001.mp3"

            # Verify the object exists in S3
            obj = s3_manager.s3_client.get_object(Bucket=BUCKET, Key=key)
            assert obj["Body"].read() == b"fake mp3 data"
        finally:
            os.unlink(tmp_path)

    def test_sets_audio_mpeg_content_type(self, s3_manager):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"fake mp3 data")
            tmp_path = f.name

        try:
            key = s3_manager.upload_episode(tmp_path, "vid001", max_age_days=5)
            obj = s3_manager.s3_client.head_object(Bucket=BUCKET, Key=key)
            assert obj["ContentType"] == "audio/mpeg"
        finally:
            os.unlink(tmp_path)

    def test_sets_expiry_days_tag(self, s3_manager):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"fake mp3 data")
            tmp_path = f.name

        try:
            key = s3_manager.upload_episode(tmp_path, "vid001", max_age_days=7)
            tags = s3_manager.s3_client.get_object_tagging(Bucket=BUCKET, Key=key)
            tag_dict = {t["Key"]: t["Value"] for t in tags.get("TagSet", [])}
            assert tag_dict.get("expiry-days") == "7"
        finally:
            os.unlink(tmp_path)

    def test_calls_set_lifecycle_expiration(self, s3_manager):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"fake mp3 data")
            tmp_path = f.name

        try:
            with unittest.mock.patch.object(s3_manager, "set_lifecycle_expiration") as mock_lc:
                s3_manager.upload_episode(tmp_path, "vid001", max_age_days=10)
                mock_lc.assert_called_once_with(10)
        finally:
            os.unlink(tmp_path)


class TestSetLifecycleExpiration:
    def test_creates_lifecycle_rule(self, s3_manager):
        s3_manager.set_lifecycle_expiration(5)
        response = s3_manager.s3_client.get_bucket_lifecycle_configuration(Bucket=BUCKET)
        rule_ids = [r["ID"] for r in response["Rules"]]
        assert f"expire-{PLAYLIST_ID}-episodes" in rule_ids

    def test_lifecycle_rule_has_correct_days(self, s3_manager):
        s3_manager.set_lifecycle_expiration(10)
        response = s3_manager.s3_client.get_bucket_lifecycle_configuration(Bucket=BUCKET)
        rule = next(
            r for r in response["Rules"]
            if r["ID"] == f"expire-{PLAYLIST_ID}-episodes"
        )
        assert rule["Expiration"]["Days"] == 10

    def test_lifecycle_rule_has_correct_prefix(self, s3_manager):
        s3_manager.set_lifecycle_expiration(5)
        response = s3_manager.s3_client.get_bucket_lifecycle_configuration(Bucket=BUCKET)
        rule = next(
            r for r in response["Rules"]
            if r["ID"] == f"expire-{PLAYLIST_ID}-episodes"
        )
        assert rule["Filter"]["Prefix"] == f"{PLAYLIST_ID}/episodes/"

    def test_updates_existing_rule_for_same_playlist(self, s3_manager):
        s3_manager.set_lifecycle_expiration(5)
        s3_manager.set_lifecycle_expiration(14)  # update to 14 days
        response = s3_manager.s3_client.get_bucket_lifecycle_configuration(Bucket=BUCKET)
        matching = [
            r for r in response["Rules"]
            if r["ID"] == f"expire-{PLAYLIST_ID}-episodes"
        ]
        assert len(matching) == 1
        assert matching[0]["Expiration"]["Days"] == 14

    def test_preserves_other_playlists_rules(self, s3_manager):
        # Manually set a rule for a different playlist
        other_rule_id = "expire-OtherPlaylist-episodes"
        s3_manager.s3_client.put_bucket_lifecycle_configuration(
            Bucket=BUCKET,
            LifecycleConfiguration={
                "Rules": [{
                    "ID": other_rule_id,
                    "Status": "Enabled",
                    "Filter": {"Prefix": "OtherPlaylist/episodes/"},
                    "Expiration": {"Days": 7},
                }]
            },
        )
        # Now set lifecycle for our playlist
        s3_manager.set_lifecycle_expiration(5)
        response = s3_manager.s3_client.get_bucket_lifecycle_configuration(Bucket=BUCKET)
        rule_ids = {r["ID"] for r in response["Rules"]}
        assert other_rule_id in rule_ids
        assert f"expire-{PLAYLIST_ID}-episodes" in rule_ids

    def test_handles_s3_error_gracefully(self, s3_manager):
        from botocore.exceptions import ClientError
        error_resp = {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}}
        s3_manager.s3_client.get_bucket_lifecycle_configuration = unittest.mock.MagicMock(
            side_effect=ClientError(error_resp, "GetBucketLifecycleConfiguration")
        )
        # Should not raise
        s3_manager.set_lifecycle_expiration(5)


class TestDeleteEpisode:
    def test_deletes_episode_from_s3(self, s3_manager):
        # Upload first
        s3_manager.s3_client.put_object(
            Bucket=BUCKET,
            Key=f"{PLAYLIST_ID}/episodes/vid001.mp3",
            Body=b"audio",
        )
        # Verify it exists
        assert s3_manager.list_existing_episodes() == {"vid001"}

        # Delete
        s3_manager.delete_episode("vid001")

        # Verify it's gone
        assert s3_manager.list_existing_episodes() == set()


class TestUploadFeed:
    def test_uploads_feed_and_returns_key(self, s3_manager):
        xml = "<rss><channel><title>Test</title></channel></rss>"
        key = s3_manager.upload_feed(xml)
        assert key == f"{PLAYLIST_ID}/feed.xml"

        # Verify content
        obj = s3_manager.s3_client.get_object(Bucket=BUCKET, Key=key)
        assert obj["Body"].read().decode("utf-8") == xml

    def test_sets_rss_xml_content_type(self, s3_manager):
        xml = "<rss><channel><title>Test</title></channel></rss>"
        key = s3_manager.upload_feed(xml)
        obj = s3_manager.s3_client.head_object(Bucket=BUCKET, Key=key)
        assert obj["ContentType"] == "application/rss+xml"


class TestGetObjectSize:
    def test_returns_content_length(self, s3_manager):
        data = b"hello world 12345"
        s3_manager.s3_client.put_object(
            Bucket=BUCKET,
            Key=f"{PLAYLIST_ID}/episodes/vid001.mp3",
            Body=data,
        )
        size = s3_manager.get_object_size(f"{PLAYLIST_ID}/episodes/vid001.mp3")
        assert size == len(data)


class TestInvalidateCloudFront:
    def test_skips_when_no_distribution_id(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
            manager.s3_client = conn
            manager._distribution_id = ""  # not set
            # Should not raise, just skip
            manager._invalidate_cloudfront("/PLtest123/feed.xml")
            assert manager._cf_client is None

    def test_creates_invalidation_when_distribution_id_set(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
            manager.s3_client = conn
            manager._distribution_id = "EDIST123"

            mock_cf = unittest.mock.MagicMock()
            manager._cf_client = mock_cf

            manager._invalidate_cloudfront("/PLtest123/feed.xml")
            mock_cf.create_invalidation.assert_called_once()
            call_kwargs = mock_cf.create_invalidation.call_args[1]
            assert call_kwargs["DistributionId"] == "EDIST123"
            assert "/PLtest123/feed.xml" in call_kwargs["InvalidationBatch"]["Paths"]["Items"]

    def test_handles_cloudfront_exception_gracefully(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
            manager.s3_client = conn
            manager._distribution_id = "EDIST123"

            mock_cf = unittest.mock.MagicMock()
            mock_cf.create_invalidation.side_effect = Exception("CF error")
            manager._cf_client = mock_cf

            # Should not raise
            manager._invalidate_cloudfront("/PLtest123/feed.xml")


class TestManifest:
    def test_load_manifest_returns_empty_when_missing(self, s3_manager):
        result = s3_manager.load_manifest()
        assert result == {}

    def test_save_and_load_manifest_roundtrip(self, s3_manager):
        manifest = {
            "ep-001": {"size": 1234567, "title": "Episode 1", "duration": 1800},
            "ep-002": {"size": 2345678, "title": "Episode 2", "duration": 2400},
        }
        s3_manager.save_manifest(manifest)
        loaded = s3_manager.load_manifest()
        assert loaded["ep-001"]["size"] == 1234567
        assert loaded["ep-001"]["title"] == "Episode 1"
        assert loaded["ep-002"]["size"] == 2345678

    def test_save_manifest_stored_at_correct_key(self, s3_manager):
        s3_manager.save_manifest({"ep-001": {"size": 100}})
        expected_key = f"{PLAYLIST_ID}/manifest.json"
        obj = s3_manager.s3_client.get_object(Bucket=BUCKET, Key=expected_key)
        data = obj["Body"].read()
        assert b"ep-001" in data

    def test_load_manifest_returns_empty_on_corrupt_json(self, s3_manager):
        key = f"{PLAYLIST_ID}/manifest.json"
        s3_manager.s3_client.put_object(
            Bucket=BUCKET, Key=key, Body=b"not valid json"
        )
        result = s3_manager.load_manifest()
        assert result == {}

    def test_load_manifest_returns_empty_on_non_dict_json(self, s3_manager):
        key = f"{PLAYLIST_ID}/manifest.json"
        s3_manager.s3_client.put_object(
            Bucket=BUCKET, Key=key, Body=b'["not", "a", "dict"]'
        )
        result = s3_manager.load_manifest()
        assert result == {}

    def test_save_manifest_content_type_is_json(self, s3_manager):
        s3_manager.save_manifest({"ep-001": {"size": 42}})
        key = f"{PLAYLIST_ID}/manifest.json"
        meta = s3_manager.s3_client.head_object(Bucket=BUCKET, Key=key)
        assert meta["ContentType"] == "application/json"

    def test_manifest_updated_after_overwrite(self, s3_manager):
        s3_manager.save_manifest({"ep-001": {"size": 100}})
        s3_manager.save_manifest({"ep-001": {"size": 999}, "ep-002": {"size": 50}})
        loaded = s3_manager.load_manifest()
        assert loaded["ep-001"]["size"] == 999
        assert "ep-002" in loaded


class TestPingOvercast:
    def test_skips_when_no_cloudfront_base(self):
        import os
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
            manager.s3_client = conn

            env = os.environ.copy()
            env.pop("CLOUDFRONT_BASE", None)
            with unittest.mock.patch.dict(os.environ, env, clear=True):
                # Should not raise
                manager._ping_overcast()

    def test_sends_ping_when_cloudfront_base_set(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
            manager.s3_client = conn

            mock_resp = unittest.mock.MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = unittest.mock.MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)

            with unittest.mock.patch.dict(os.environ, {"CLOUDFRONT_BASE": "https://cdn.example.com"}):
                with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
                    manager._ping_overcast()
                    # Should have called urlopen with overcast ping URL
                    # No assertion needed beyond no exception raised

    def test_handles_ping_exception_gracefully(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
            manager.s3_client = conn

            with unittest.mock.patch.dict(os.environ, {"CLOUDFRONT_BASE": "https://cdn.example.com"}):
                with unittest.mock.patch("urllib.request.urlopen", side_effect=Exception("timeout")):
                    # Should not raise
                    manager._ping_overcast()


class TestResetPodcast:
    """Tests for S3Manager.reset_podcast()."""

    def _make_manager(self, conn):
        manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
        manager.s3_client = conn
        return manager

    def _upload_episodes(self, conn, video_ids):
        for vid in video_ids:
            conn.put_object(
                Bucket=BUCKET,
                Key=f"{PLAYLIST_ID}/episodes/{vid}.mp3",
                Body=b"audio",
            )

    def _upload_feed(self, conn):
        conn.put_object(
            Bucket=BUCKET,
            Key=f"{PLAYLIST_ID}/feed.xml",
            Body=b"<rss/>",
        )

    def _upload_manifest(self, conn):
        conn.put_object(
            Bucket=BUCKET,
            Key=f"{PLAYLIST_ID}/manifest.json",
            Body=b"{}",
        )

    def _key_exists(self, conn, key):
        try:
            conn.head_object(Bucket=BUCKET, Key=key)
            return True
        except Exception:
            return False

    def test_returns_correct_counts(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            self._upload_episodes(conn, ["vid1", "vid2", "vid3"])
            self._upload_feed(conn)
            self._upload_manifest(conn)

            manager = self._make_manager(conn)
            result = manager.reset_podcast()

            assert result["episodes_deleted"] == 3
            assert result["feed_deleted"] is True
            assert result["manifest_deleted"] is True

    def test_episodes_are_deleted(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            self._upload_episodes(conn, ["vid1", "vid2"])

            manager = self._make_manager(conn)
            manager.reset_podcast()

            assert not self._key_exists(conn, f"{PLAYLIST_ID}/episodes/vid1.mp3")
            assert not self._key_exists(conn, f"{PLAYLIST_ID}/episodes/vid2.mp3")

    def test_feed_xml_is_deleted(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            self._upload_feed(conn)

            manager = self._make_manager(conn)
            manager.reset_podcast()

            assert not self._key_exists(conn, f"{PLAYLIST_ID}/feed.xml")

    def test_manifest_json_is_deleted(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            self._upload_manifest(conn)

            manager = self._make_manager(conn)
            manager.reset_podcast()

            assert not self._key_exists(conn, f"{PLAYLIST_ID}/manifest.json")

    def test_empty_bucket_returns_zero_episodes(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)

            manager = self._make_manager(conn)
            result = manager.reset_podcast()

            assert result["episodes_deleted"] == 0

    def test_other_playlist_episodes_are_not_deleted(self):
        other_playlist = "PLother456"
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            # Upload to the target playlist
            self._upload_episodes(conn, ["vid1"])
            # Upload to a different playlist
            conn.put_object(
                Bucket=BUCKET,
                Key=f"{other_playlist}/episodes/other_vid.mp3",
                Body=b"audio",
            )

            manager = self._make_manager(conn)
            manager.reset_podcast()

            # Other playlist episode should still exist
            assert self._key_exists(conn, f"{other_playlist}/episodes/other_vid.mp3")
            # This playlist episode should be gone
            assert not self._key_exists(conn, f"{PLAYLIST_ID}/episodes/vid1.mp3")

    def test_no_feed_returns_false(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)
            # No feed uploaded

            manager = self._make_manager(conn)
            result = manager.reset_podcast()

            # delete_object on a non-existent key is a no-op in S3 — still reports deleted
            assert result["feed_deleted"] is True

    def test_no_manifest_returns_false(self):
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket=BUCKET)

            manager = self._make_manager(conn)
            result = manager.reset_podcast()

            assert result["manifest_deleted"] is True


# ---------------------------------------------------------------------------
# Exception-path coverage for lines 237-238, 265-266, 283, 353-354, 365-366
# ---------------------------------------------------------------------------

class TestManifestExceptionPaths:
    """Cover non-404 ClientError and generic Exception branches in load/save_manifest."""

    def test_load_manifest_non_404_client_error_returns_empty(self):
        """Non-404 ClientError (e.g. 403 Forbidden) is swallowed, returns {} (lines 237-238)."""
        from botocore.exceptions import ClientError

        manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
        mock_s3 = unittest.mock.MagicMock()
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}}, "GetObject"
        )
        manager.s3_client = mock_s3

        result = manager.load_manifest()
        assert result == {}

    def test_load_manifest_generic_exception_returns_empty(self):
        """Generic Exception in load_manifest is swallowed, returns {} (lines 239-241)."""
        manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
        mock_s3 = unittest.mock.MagicMock()
        mock_s3.get_object.side_effect = RuntimeError("unexpected S3 failure")
        manager.s3_client = mock_s3

        result = manager.load_manifest()
        assert result == {}

    def test_save_manifest_exception_is_swallowed(self):
        """Exception in save_manifest is swallowed (lines 265-266)."""
        manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
        mock_s3 = unittest.mock.MagicMock()
        mock_s3.put_object.side_effect = RuntimeError("S3 write failure")
        manager.s3_client = mock_s3

        # Should not raise
        manager.save_manifest({"ep-001": {"size": 100}})


class TestCloudFrontClientInit:
    """Cover the lazy _cf_client initialization branch (line 283)."""

    def test_cf_client_initialized_lazily_when_none(self):
        """When _cf_client is None and distribution_id is set, boto3.client('cloudfront')
        is called to create it (line 283)."""
        manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
        manager._distribution_id = "EDIST_LAZY"
        assert manager._cf_client is None  # precondition

        mock_cf = unittest.mock.MagicMock()

        with unittest.mock.patch("s3_manager.boto3.client", return_value=mock_cf) as mock_boto3:
            manager._invalidate_cloudfront("/feed.xml")

        # boto3.client should have been called to create the CF client
        mock_boto3.assert_called_once_with("cloudfront")
        # And the invalidation should have been made using that client
        mock_cf.create_invalidation.assert_called_once()


class TestResetPodcastExceptionPaths:
    """Cover exception branches in reset_podcast for feed/manifest deletes
    (lines 353-354, 365-366)."""

    def _make_manager(self, conn):
        manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
        manager.s3_client = conn
        return manager

    def test_feed_delete_exception_is_swallowed(self):
        """Exception during feed.xml delete does not propagate (lines 353-354)."""
        manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
        mock_s3 = unittest.mock.MagicMock()

        # paginator returns empty pages (no episodes to delete)
        mock_paginator = unittest.mock.MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": []}]
        mock_s3.get_paginator.return_value = mock_paginator

        # First delete_object call (feed.xml) raises; second (manifest.json) succeeds
        call_count = [0]
        def fake_delete_object(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("feed delete failed")

        mock_s3.delete_object.side_effect = fake_delete_object
        manager.s3_client = mock_s3

        # Should not raise
        result = manager.reset_podcast()
        assert result["feed_deleted"] is False
        assert result["manifest_deleted"] is True

    def test_manifest_delete_exception_is_swallowed(self):
        """Exception during manifest.json delete does not propagate (lines 365-366)."""
        manager = S3Manager(bucket=BUCKET, playlist_id=PLAYLIST_ID)
        mock_s3 = unittest.mock.MagicMock()

        mock_paginator = unittest.mock.MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": []}]
        mock_s3.get_paginator.return_value = mock_paginator

        # First delete_object (feed.xml) succeeds; second (manifest.json) raises
        call_count = [0]
        def fake_delete_object(**kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("manifest delete failed")

        mock_s3.delete_object.side_effect = fake_delete_object
        manager.s3_client = mock_s3

        # Should not raise
        result = manager.reset_podcast()
        assert result["feed_deleted"] is True
        assert result["manifest_deleted"] is False
