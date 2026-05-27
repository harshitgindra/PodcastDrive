"""Unit tests for src/ad_remover.py.

All external I/O is mocked:
  - boto3 S3 / Transcribe / Bedrock clients  → patched with MagicMock via monkeypatch
  - urllib.request.urlopen                   → patched to return fake transcript JSON
  - subprocess.run                           → patched for ffprobe / ffmpeg calls
  - time.sleep                               → patched to avoid real waits
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from io import BytesIO
from unittest.mock import MagicMock, call, patch

import pytest

# Ensure src/ is on the path (conftest.py usually handles this, but be explicit)
sys.path.insert(0, "src")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_transcript_json(items: list[dict] | None = None) -> bytes:
    """Return minimal AWS Transcribe JSON bytes."""
    if items is None:
        items = [
            {"type": "pronunciation", "start_time": "0.5", "end_time": "1.0",
             "alternatives": [{"content": "Hello", "confidence": "0.99"}]},
            {"type": "pronunciation", "start_time": "1.1", "end_time": "1.8",
             "alternatives": [{"content": "world", "confidence": "0.99"}]},
        ]
    return json.dumps({"results": {"items": items}}).encode()


def _make_transcribe_client(status_sequence=("COMPLETED",), failure_reason=None):
    """Return a mock boto3 Transcribe client."""
    client = MagicMock()

    # get_transcription_job cycles through status_sequence
    responses = []
    for status in status_sequence:
        job = {
            "TranscriptionJobStatus": status,
            "Transcript": {"TranscriptFileUri": "https://fake-s3/transcript.json"},
        }
        if status == "FAILED" and failure_reason:
            job["FailureReason"] = failure_reason
        responses.append({"TranscriptionJob": job})

    client.get_transcription_job.side_effect = responses
    return client


def _make_bedrock_client(content: str = "[]"):
    """Return a mock boto3 bedrock-runtime client."""
    client = MagicMock()
    client.converse.return_value = {
        "output": {
            "message": {
                "content": [{"text": content}]
            }
        }
    }
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reload_ad_remover():
    """Ensure a fresh import of ad_remover for every test."""
    sys.modules.pop("ad_remover", None)
    yield
    sys.modules.pop("ad_remover", None)


@pytest.fixture()
def mock_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", MagicMock())


@pytest.fixture()
def mock_urlopen(monkeypatch):
    """Patch urllib.request.urlopen to return fake transcript bytes."""
    fake_resp = MagicMock()
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = MagicMock(return_value=False)
    fake_resp.read.return_value = _fake_transcript_json()

    import urllib.request as ur
    monkeypatch.setattr(ur, "urlopen", MagicMock(return_value=fake_resp))
    return fake_resp


# ---------------------------------------------------------------------------
# _items_to_segments
# ---------------------------------------------------------------------------

class TestItemsToSegments:
    def test_basic_grouping(self):
        """Words close together are grouped into one segment."""
        import ad_remover
        items = [
            {"type": "pronunciation", "start_time": "0.0", "end_time": "0.5",
             "alternatives": [{"content": "Hello"}]},
            {"type": "pronunciation", "start_time": "0.6", "end_time": "1.0",
             "alternatives": [{"content": "world"}]},
        ]
        segs = ad_remover._items_to_segments(items)
        assert len(segs) == 1
        assert segs[0]["text"] == "Hello world"
        assert segs[0]["start"] == 0.0
        assert segs[0]["end"] == 1.0

    def test_gap_splits_segments(self):
        """A gap larger than gap_threshold creates two separate segments."""
        import ad_remover
        items = [
            {"type": "pronunciation", "start_time": "0.0", "end_time": "1.0",
             "alternatives": [{"content": "Intro"}]},
            {"type": "pronunciation", "start_time": "5.0", "end_time": "6.0",
             "alternatives": [{"content": "Ad"}]},
        ]
        segs = ad_remover._items_to_segments(items, gap_threshold=1.5)
        assert len(segs) == 2
        assert segs[0]["text"] == "Intro"
        assert segs[1]["text"] == "Ad"

    def test_punctuation_items_appended(self):
        """Punctuation items (no timing) are appended to the current segment text."""
        import ad_remover
        items = [
            {"type": "pronunciation", "start_time": "0.0", "end_time": "0.5",
             "alternatives": [{"content": "Hello"}]},
            {"type": "punctuation", "alternatives": [{"content": ","}]},
            {"type": "pronunciation", "start_time": "0.6", "end_time": "1.0",
             "alternatives": [{"content": "world"}]},
        ]
        segs = ad_remover._items_to_segments(items)
        assert len(segs) == 1
        assert "," in segs[0]["text"]

    def test_empty_items_returns_empty(self):
        import ad_remover
        assert ad_remover._items_to_segments([]) == []

    def test_items_without_alternatives_skipped(self):
        """Items with no alternatives list are silently ignored."""
        import ad_remover
        items = [
            {"type": "pronunciation", "start_time": "0.0", "end_time": "0.5",
             "alternatives": []},
        ]
        assert ad_remover._items_to_segments(items) == []


# ---------------------------------------------------------------------------
# transcribe_audio
# ---------------------------------------------------------------------------

class TestTranscribeAudio:
    def _patch_boto3(self, monkeypatch, transcribe_client, s3_client=None):
        """Patch boto3.client to return the given mocks."""
        if s3_client is None:
            s3_client = MagicMock()

        def fake_boto3_client(service, **kwargs):
            if service == "s3":
                return s3_client
            if service == "transcribe":
                return transcribe_client
            return MagicMock()

        import boto3 as _boto3
        monkeypatch.setattr(_boto3, "client", fake_boto3_client)
        return s3_client

    def test_happy_path_returns_segments(self, monkeypatch, mock_sleep, mock_urlopen):
        """transcribe_audio returns parsed segments on a COMPLETED job."""
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        tc = _make_transcribe_client(status_sequence=["COMPLETED"])
        self._patch_boto3(monkeypatch, tc)
        import ad_remover

        result = ad_remover.transcribe_audio("/tmp/ep.mp3", "vid123")

        assert isinstance(result, list)
        assert len(result) > 0
        assert "start" in result[0]
        assert "end" in result[0]
        assert "text" in result[0]

    def test_raises_when_no_s3_bucket(self, monkeypatch):
        """transcribe_audio raises RuntimeError when S3_BUCKET is not set."""
        monkeypatch.delenv("S3_BUCKET", raising=False)
        import ad_remover

        with pytest.raises(RuntimeError, match="S3_BUCKET must be set"):
            ad_remover.transcribe_audio("/tmp/ep.mp3", "vid123")

    def test_raises_on_failed_job(self, monkeypatch, mock_sleep):
        """transcribe_audio raises RuntimeError when the job status is FAILED."""
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        tc = _make_transcribe_client(status_sequence=["FAILED"], failure_reason="Bad audio")
        self._patch_boto3(monkeypatch, tc)
        import ad_remover

        with pytest.raises(RuntimeError, match="failed: Bad audio"):
            ad_remover.transcribe_audio("/tmp/ep.mp3", "vid123")

    def test_raises_on_timeout(self, monkeypatch, mock_sleep):
        """transcribe_audio raises RuntimeError when max_wait is exceeded."""
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_POLL_INTERVAL", "1")
        monkeypatch.setenv("TRANSCRIBE_MAX_WAIT", "2")

        # Job always stays IN_PROGRESS → will time out after 2 polls
        tc = MagicMock()
        tc.get_transcription_job.return_value = {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "IN_PROGRESS",
                "Transcript": {},
            }
        }
        self._patch_boto3(monkeypatch, tc)
        import ad_remover

        with pytest.raises(RuntimeError, match="timed out"):
            ad_remover.transcribe_audio("/tmp/ep.mp3", "vid123")

    def test_cleans_up_s3_and_job_on_success(self, monkeypatch, mock_sleep, mock_urlopen):
        """S3 object and Transcribe job are deleted even after a successful run."""
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        s3 = MagicMock()
        tc = _make_transcribe_client(status_sequence=["COMPLETED"])
        self._patch_boto3(monkeypatch, tc, s3_client=s3)
        import ad_remover

        ad_remover.transcribe_audio("/tmp/ep.mp3", "vid123")

        s3.delete_object.assert_called_once()
        tc.delete_transcription_job.assert_called_once()

    def test_cleans_up_on_failure(self, monkeypatch, mock_sleep):
        """S3 object and Transcribe job are deleted even when the job fails."""
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        s3 = MagicMock()
        tc = _make_transcribe_client(status_sequence=["FAILED"])
        self._patch_boto3(monkeypatch, tc, s3_client=s3)
        import ad_remover

        with pytest.raises(RuntimeError):
            ad_remover.transcribe_audio("/tmp/ep.mp3", "vid123")

        s3.delete_object.assert_called_once()
        tc.delete_transcription_job.assert_called_once()

    def test_polls_multiple_times_before_complete(self, monkeypatch, mock_sleep, mock_urlopen):
        """transcribe_audio polls correctly through IN_PROGRESS → COMPLETED."""
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        tc = _make_transcribe_client(status_sequence=["IN_PROGRESS", "IN_PROGRESS", "COMPLETED"])
        self._patch_boto3(monkeypatch, tc)
        import ad_remover

        result = ad_remover.transcribe_audio("/tmp/ep.mp3", "vid123")

        assert tc.get_transcription_job.call_count == 3
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# detect_ads
# ---------------------------------------------------------------------------

class TestDetectAds:
    def _patch_bedrock(self, monkeypatch, content: str):
        bc = _make_bedrock_client(content=content)

        import boto3 as _boto3
        monkeypatch.setattr(_boto3, "client", lambda svc, **kw: bc)
        return bc

    def test_returns_empty_for_no_segments(self, monkeypatch):
        """detect_ads returns [] immediately when given no segments."""
        import ad_remover
        assert ad_remover.detect_ads([]) == []

    def test_parses_clean_json_response(self, monkeypatch):
        """detect_ads correctly parses a clean JSON array from Bedrock."""
        content = '[{"start": 60.0, "end": 120.5}, {"start": 300.0, "end": 360.0}]'
        self._patch_bedrock(monkeypatch, content)
        import ad_remover

        result = ad_remover.detect_ads([{"start": 0.0, "end": 5.0, "text": "hi"}])

        assert len(result) == 2
        assert result[0] == {"start": 60.0, "end": 120.5}
        assert result[1] == {"start": 300.0, "end": 360.0}

    def test_extracts_json_from_prose_response(self, monkeypatch):
        """detect_ads handles Bedrock responses that wrap JSON in prose."""
        content = 'Sure, here are the ads:\n[{"start": 90.0, "end": 150.0}]\nDone!'
        self._patch_bedrock(monkeypatch, content)
        import ad_remover

        result = ad_remover.detect_ads([{"start": 0.0, "end": 5.0, "text": "hi"}])
        assert result == [{"start": 90.0, "end": 150.0}]

    def test_returns_empty_when_no_json_array(self, monkeypatch):
        """detect_ads returns [] when Bedrock response contains no JSON array."""
        self._patch_bedrock(monkeypatch, "No ads found.")
        import ad_remover

        result = ad_remover.detect_ads([{"start": 0.0, "end": 5.0, "text": "clean"}])
        assert result == []

    def test_returns_empty_on_malformed_json(self, monkeypatch):
        """detect_ads returns [] and does not raise on malformed JSON."""
        self._patch_bedrock(monkeypatch, "[{bad json}]")
        import ad_remover

        result = ad_remover.detect_ads([{"start": 0.0, "end": 5.0, "text": "test"}])
        assert result == []

    def test_ignores_malformed_segment_entries(self, monkeypatch):
        """detect_ads filters out ad entries missing start or end."""
        content = '[{"start": 10.0, "end": 20.0}, {"only_start": 30.0}, "bad"]'
        self._patch_bedrock(monkeypatch, content)
        import ad_remover

        result = ad_remover.detect_ads([{"start": 0.0, "end": 5.0, "text": "ok"}])
        assert result == [{"start": 10.0, "end": 20.0}]

    def test_filters_out_short_segments_below_minimum(self, monkeypatch):
        """detect_ads drops segments shorter than the 5-second minimum."""
        # 3-second segment should be dropped; 60-second should be kept
        content = '[{"start": 10.0, "end": 13.0}, {"start": 60.0, "end": 120.0}]'
        self._patch_bedrock(monkeypatch, content)
        import ad_remover

        result = ad_remover.detect_ads([{"start": 0.0, "end": 5.0, "text": "test"}])
        assert len(result) == 1
        assert result[0] == {"start": 60.0, "end": 120.0}

    def test_keeps_segments_exactly_at_minimum_length(self, monkeypatch):
        """detect_ads keeps segments that are exactly 5 seconds long."""
        content = '[{"start": 10.0, "end": 15.0}]'
        self._patch_bedrock(monkeypatch, content)
        import ad_remover

        result = ad_remover.detect_ads([{"start": 0.0, "end": 5.0, "text": "test"}])
        assert result == [{"start": 10.0, "end": 15.0}]

    def test_uses_bedrock_model_id_env_var(self, monkeypatch):
        """BEDROCK_MODEL_ID env var is forwarded to bedrock.converse."""
        monkeypatch.setenv("BEDROCK_MODEL_ID", "amazon.titan-v2:0")
        bc = _make_bedrock_client(content="[]")
        import boto3 as _boto3
        monkeypatch.setattr(_boto3, "client", lambda svc, **kw: bc)
        import ad_remover

        ad_remover.detect_ads([{"start": 0.0, "end": 5.0, "text": "x"}])

        call_kwargs = bc.converse.call_args
        assert call_kwargs.kwargs["modelId"] == "amazon.titan-v2:0"


# ---------------------------------------------------------------------------
# splice_audio
# ---------------------------------------------------------------------------

class TestSpliceAudio:
    def test_raises_value_error_on_empty_segments(self):
        import ad_remover
        with pytest.raises(ValueError, match="empty ad_segments"):
            ad_remover.splice_audio("/in.mp3", [], "/out.mp3")

    def test_builds_correct_ffmpeg_command(self, monkeypatch):
        """splice_audio invokes ffmpeg with the correct filter_complex."""
        import ad_remover

        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)

        probe_result = MagicMock(stdout="600.0\n", returncode=0, stderr="")
        ffmpeg_result = MagicMock(returncode=0)
        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return probe_result if cmd[0] == "ffprobe" else ffmpeg_result

        monkeypatch.setattr(subprocess, "run", fake_run)

        ad_remover.splice_audio("/in.mp3", [{"start": 60.0, "end": 120.0}], "/out.mp3")

        assert len(run_calls) == 2
        ffmpeg_cmd = run_calls[1]
        assert ffmpeg_cmd[0] == "ffmpeg"
        assert "/in.mp3" in ffmpeg_cmd
        assert "/out.mp3" in ffmpeg_cmd
        fc = ffmpeg_cmd[ffmpeg_cmd.index("-filter_complex") + 1]
        assert "atrim" in fc
        assert "concat" in fc

    def test_merges_overlapping_ad_segments(self, monkeypatch):
        """Overlapping ads are merged → correct number of keep intervals."""
        import ad_remover

        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)

        probe_result = MagicMock(stdout="300.0\n", returncode=0, stderr="")
        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            return probe_result

        monkeypatch.setattr(subprocess, "run", fake_run)

        # [60-120] + [100-150] → merged [60-150] → keep: [0-60], [150-300] → n=2
        ad_remover.splice_audio(
            "/in.mp3",
            [{"start": 60.0, "end": 120.0}, {"start": 100.0, "end": 150.0}],
            "/out.mp3",
        )

        fc = run_calls[1][run_calls[1].index("-filter_complex") + 1]
        assert "concat=n=2" in fc

    def test_raises_on_ffprobe_failure(self, monkeypatch):
        import ad_remover
        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)
        monkeypatch.setattr(
            subprocess, "run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, "ffprobe")),
        )
        with pytest.raises(RuntimeError, match="ffprobe failed"):
            ad_remover.splice_audio("/in.mp3", [{"start": 10.0, "end": 20.0}], "/out.mp3")

    def test_raises_on_ffmpeg_failure(self, monkeypatch):
        import ad_remover

        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)

        probe_result = MagicMock(stdout="300.0\n", returncode=0, stderr="")

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return probe_result
            raise subprocess.CalledProcessError(1, cmd, stderr="ffmpeg error")

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="ffmpeg splice failed"):
            ad_remover.splice_audio("/in.mp3", [{"start": 10.0, "end": 20.0}], "/out.mp3")

    def test_raises_when_ads_cover_entire_file(self, monkeypatch):
        import ad_remover

        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return MagicMock(stdout="60.0\n", stderr="")
            return MagicMock()

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="cover the entire file"):
            ad_remover.splice_audio("/in.mp3", [{"start": 0.0, "end": 60.0}], "/out.mp3")


# ---------------------------------------------------------------------------
# remove_ads  (orchestrator)
# ---------------------------------------------------------------------------

class TestRemoveAds:
    def test_returns_original_when_disabled(self, monkeypatch):
        monkeypatch.setenv("REMOVE_ADS", "false")
        import ad_remover
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp")[0] == "/ep.mp3"

    def test_returns_original_when_disabled_zero(self, monkeypatch):
        monkeypatch.setenv("REMOVE_ADS", "0")
        import ad_remover
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp")[0] == "/ep.mp3"

    def test_returns_original_on_transcription_failure(self, monkeypatch):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(side_effect=RuntimeError("boom")))
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp")[0] == "/ep.mp3"

    def test_returns_original_on_ad_detection_failure(self, monkeypatch):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "x"}]))
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(side_effect=ConnectionError("no bedrock")))
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp")[0] == "/ep.mp3"

    def test_returns_original_when_no_ads(self, monkeypatch):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "clean"}]))
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[]))
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp")[0] == "/ep.mp3"

    def test_returns_original_on_splice_failure(self, monkeypatch):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "ad"}]))
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[{"start": 0.2, "end": 0.8}]))
        monkeypatch.setattr(ad_remover, "splice_audio", MagicMock(side_effect=RuntimeError("ffmpeg gone")))
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp")[0] == "/ep.mp3"

    def test_returns_cleaned_path_on_success(self, monkeypatch, tmp_path):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        import os
        tmp_dir = str(tmp_path)
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "ad"}]))
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[{"start": 0.2, "end": 0.8}]))
        monkeypatch.setattr(ad_remover, "splice_audio", MagicMock(return_value=None))

        result, _segs = ad_remover.remove_ads("/ep.mp3", "vid123", tmp_dir)
        assert result == os.path.join(tmp_dir, "vid123_clean.mp3")

    def test_calls_splice_with_correct_args(self, monkeypatch, tmp_path):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        import os
        tmp_dir = str(tmp_path)
        ad_segs = [{"start": 10.0, "end": 30.0}]
        mock_splice = MagicMock(return_value=None)

        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "x"}]))
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=ad_segs))
        monkeypatch.setattr(ad_remover, "splice_audio", mock_splice)

        ad_remover.remove_ads("/ep.mp3", "vid123", tmp_dir)

        expected_out = os.path.join(tmp_dir, "vid123_clean.mp3")
        mock_splice.assert_called_once_with("/ep.mp3", ad_segs, expected_out)

    def test_transcribe_audio_called_with_video_id(self, monkeypatch, tmp_path):
        """remove_ads passes video_id to transcribe_audio."""
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        mock_transcribe = MagicMock(return_value=[])
        monkeypatch.setattr(ad_remover, "transcribe_audio", mock_transcribe)

        ad_remover.remove_ads("/ep.mp3", "my_video_id", str(tmp_path))

        mock_transcribe.assert_called_once_with("/ep.mp3", "my_video_id")

    def test_dry_run_returns_original_without_splicing(self, monkeypatch, tmp_path):
        """With REMOVE_ADS_DRY_RUN=true, ads are detected but splice is never called."""
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        monkeypatch.setenv("REMOVE_ADS_DRY_RUN", "true")
        import ad_remover

        mock_splice = MagicMock()
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "ad copy"}]))
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[{"start": 10.0, "end": 70.0}]))
        monkeypatch.setattr(ad_remover, "splice_audio", mock_splice)

        result, _segs = ad_remover.remove_ads("/ep.mp3", "vid_dry", str(tmp_path))

        assert result == "/ep.mp3"
        mock_splice.assert_not_called()

    def test_dry_run_with_no_ads_returns_original(self, monkeypatch, tmp_path):
        """With REMOVE_ADS_DRY_RUN=true and no ads, returns original (same as non-dry-run)."""
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        monkeypatch.setenv("REMOVE_ADS_DRY_RUN", "true")
        import ad_remover

        mock_splice = MagicMock()
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "clean"}]))
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[]))
        monkeypatch.setattr(ad_remover, "splice_audio", mock_splice)

        result, _segs = ad_remover.remove_ads("/ep.mp3", "vid_dry_clean", str(tmp_path))

        assert result == "/ep.mp3"
        mock_splice.assert_not_called()

    def test_dry_run_env_var_variants(self, monkeypatch, tmp_path):
        """REMOVE_ADS_DRY_RUN accepts '1' and 'yes' in addition to 'true'."""
        for val in ("1", "yes", "YES", "True"):
            monkeypatch.delenv("REMOVE_ADS", raising=False)
            monkeypatch.setenv("REMOVE_ADS_DRY_RUN", val)
            # Reload module to pick up new env
            import sys
            sys.modules.pop("ad_remover", None)
            import ad_remover

            mock_splice = MagicMock()
            monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "ad"}]))
            monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[{"start": 10.0, "end": 70.0}]))
            monkeypatch.setattr(ad_remover, "splice_audio", mock_splice)

            result, _segs = ad_remover.remove_ads("/ep.mp3", f"vid_{val}", str(tmp_path))
            assert result == "/ep.mp3", f"Expected original path for DRY_RUN={val!r}"
            mock_splice.assert_not_called()


# ---------------------------------------------------------------------------
# _split_segments_into_chunks
# ---------------------------------------------------------------------------

class TestSplitSegmentsIntoChunks:
    def test_single_chunk_when_under_limit(self):
        """Returns one chunk when total transcript fits within max_chars."""
        import ad_remover
        segments = [
            {"start": 0.0, "end": 10.0, "text": "Hello world"},
            {"start": 11.0, "end": 20.0, "text": "Second segment"},
        ]
        chunks = ad_remover._split_segments_into_chunks(segments, max_chars=5000, overlap_secs=60)
        assert len(chunks) == 1
        assert chunks[0] == segments

    def test_splits_into_multiple_chunks(self):
        """Segments exceeding max_chars are split into multiple chunks."""
        import ad_remover
        segments = [
            {"start": i * 10.0, "end": (i + 1) * 10.0, "text": "word " * 50}
            for i in range(20)
        ]
        chunks = ad_remover._split_segments_into_chunks(segments, max_chars=2000, overlap_secs=30)
        assert len(chunks) > 1
        # All segments should be covered
        all_starts = {s["start"] for chunk in chunks for s in chunk}
        expected_starts = {s["start"] for s in segments}
        assert expected_starts.issubset(all_starts)

    def test_overlap_between_chunks(self):
        """Adjacent chunks overlap by at least overlap_secs."""
        import ad_remover
        segments = [
            {"start": i * 10.0, "end": (i + 1) * 10.0 - 1, "text": "x " * 30}
            for i in range(50)
        ]
        chunks = ad_remover._split_segments_into_chunks(segments, max_chars=2000, overlap_secs=30)
        assert len(chunks) > 1
        for i in range(len(chunks) - 1):
            end_of_current = chunks[i][-1]["end"]
            start_of_next = chunks[i + 1][0]["start"]
            # Next chunk should start before or near where current ends
            assert start_of_next < end_of_current + 5, (
                f"Chunk {i+1} ends at {end_of_current}, chunk {i+2} starts at {start_of_next}"
            )

    def test_empty_segments(self):
        """Empty input returns a single empty chunk."""
        import ad_remover
        chunks = ad_remover._split_segments_into_chunks([], max_chars=5000, overlap_secs=60)
        assert chunks == [[]]

    def test_single_large_segment(self):
        """A single segment larger than max_chars still produces one chunk."""
        import ad_remover
        segments = [{"start": 0.0, "end": 600.0, "text": "x" * 10000}]
        chunks = ad_remover._split_segments_into_chunks(segments, max_chars=5000, overlap_secs=60)
        assert len(chunks) == 1
        assert chunks[0] == segments

    def test_forward_progress_guaranteed(self):
        """Chunker doesn't loop infinitely even with adversarial inputs."""
        import ad_remover
        # All segments are large and close together in time
        segments = [
            {"start": i * 2.0, "end": (i + 1) * 2.0, "text": "y" * 500}
            for i in range(20)
        ]
        chunks = ad_remover._split_segments_into_chunks(segments, max_chars=1000, overlap_secs=100)
        assert len(chunks) >= 1
        # Should terminate (this test mainly checks no infinite loop)


# ---------------------------------------------------------------------------
# _parse_ad_response
# ---------------------------------------------------------------------------

class TestParseAdResponse:
    def test_parses_clean_json(self):
        """Parses a response that is just a JSON array."""
        import ad_remover
        raw = '[{"start": 10.0, "end": 60.0}]'
        result = ad_remover._parse_ad_response(raw)
        assert result == [{"start": 10.0, "end": 60.0}]

    def test_parses_json_with_reasoning(self):
        """Parses response with reasoning text containing brackets before JSON."""
        import ad_remover
        raw = (
            "# Reasoning\n"
            "1. **[177.8 - 308.5]**: NetSuite ad\n"
            "2. **[786.8 - 843.4]**: Indeed ad\n\n"
            '```json\n'
            '[{"start": 175.8, "end": 310.5}, {"start": 784.8, "end": 845.4}]\n'
            '```'
        )
        result = ad_remover._parse_ad_response(raw)
        assert len(result) == 2
        assert result[0] == {"start": 175.8, "end": 310.5}
        assert result[1] == {"start": 784.8, "end": 845.4}

    def test_returns_empty_on_no_brackets(self):
        """Returns empty list when response has no brackets."""
        import ad_remover
        result = ad_remover._parse_ad_response("No ads found in this episode.")
        assert result == []

    def test_returns_empty_on_all_invalid_json(self):
        """Returns empty list when all bracket pairs contain invalid JSON."""
        import ad_remover
        result = ad_remover._parse_ad_response("[not valid json] and [more bad]")
        assert result == []

    def test_filters_malformed_entries(self):
        """Entries missing start/end are filtered out."""
        import ad_remover
        raw = '[{"start": 10.0, "end": 20.0}, {"bad": true}, "string"]'
        result = ad_remover._parse_ad_response(raw)
        assert result == [{"start": 10.0, "end": 20.0}]

    def test_empty_array_response(self):
        """Model returning [] means no ads."""
        import ad_remover
        result = ad_remover._parse_ad_response("[]")
        assert result == []


# ---------------------------------------------------------------------------
# _merge_overlapping_ads
# ---------------------------------------------------------------------------

class TestMergeOverlappingAds:
    def test_non_overlapping_preserved(self):
        """Non-overlapping segments remain separate."""
        import ad_remover
        ads = [{"start": 10.0, "end": 20.0}, {"start": 50.0, "end": 60.0}]
        result = ad_remover._merge_overlapping_ads(ads)
        assert result == [{"start": 10.0, "end": 20.0}, {"start": 50.0, "end": 60.0}]

    def test_overlapping_merged(self):
        """Overlapping segments are merged into one."""
        import ad_remover
        ads = [{"start": 10.0, "end": 30.0}, {"start": 25.0, "end": 50.0}]
        result = ad_remover._merge_overlapping_ads(ads)
        assert result == [{"start": 10.0, "end": 50.0}]

    def test_adjacent_within_2s_merged(self):
        """Segments within 2 seconds gap are merged (Fix #3: threshold reduced from 5s→2s)."""
        import ad_remover
        ads = [{"start": 10.0, "end": 20.0}, {"start": 22.0, "end": 40.0}]  # 2s gap
        result = ad_remover._merge_overlapping_ads(ads)
        assert result == [{"start": 10.0, "end": 40.0}]

    def test_adjacent_within_5s_no_longer_merged(self):
        """4s gap was merged under old 5s rule but stays separate under new 2s rule (Fix #3)."""
        import ad_remover
        ads = [{"start": 10.0, "end": 20.0}, {"start": 24.0, "end": 40.0}]  # 4s gap
        result = ad_remover._merge_overlapping_ads(ads)
        assert len(result) == 2, "4s gap should NOT merge under the new 2s threshold"

    def test_not_merged_when_gap_exceeds_2s(self):
        """Segments with > 2s gap remain separate."""
        import ad_remover
        ads = [{"start": 10.0, "end": 20.0}, {"start": 23.0, "end": 40.0}]  # 3s gap
        result = ad_remover._merge_overlapping_ads(ads)
        assert len(result) == 2

    def test_unsorted_input(self):
        """Handles unsorted input correctly."""
        import ad_remover
        ads = [{"start": 50.0, "end": 60.0}, {"start": 10.0, "end": 20.0}]
        result = ad_remover._merge_overlapping_ads(ads)
        assert result[0]["start"] == 10.0
        assert result[1]["start"] == 50.0

    def test_empty_input(self):
        """Empty list returns empty list."""
        import ad_remover
        assert ad_remover._merge_overlapping_ads([]) == []

    def test_multiple_overlaps_chain(self):
        """Chain of overlapping segments merge into one."""
        import ad_remover
        ads = [
            {"start": 10.0, "end": 20.0},
            {"start": 18.0, "end": 30.0},
            {"start": 28.0, "end": 45.0},
        ]
        result = ad_remover._merge_overlapping_ads(ads)
        assert result == [{"start": 10.0, "end": 45.0}]

    def test_duplicate_segments_from_chunk_overlap(self):
        """Duplicate segments (same ad detected by two chunks) are merged."""
        import ad_remover
        ads = [
            {"start": 175.8, "end": 310.5},
            {"start": 176.0, "end": 309.0},  # duplicate from overlap
            {"start": 784.8, "end": 912.0},
        ]
        result = ad_remover._merge_overlapping_ads(ads)
        assert len(result) == 2
        assert result[0] == {"start": 175.8, "end": 310.5}
        assert result[1] == {"start": 784.8, "end": 912.0}


# ---------------------------------------------------------------------------
# detect_ads chunking integration
# ---------------------------------------------------------------------------

class TestDetectAdsChunking:
    def _patch_bedrock(self, monkeypatch, responses: list[str]):
        """Patch bedrock to return different responses for each chunk call."""
        bc = MagicMock()
        bc.converse.side_effect = [
            {"output": {"message": {"content": [{"text": r}]}}}
            for r in responses
        ]
        import boto3 as _boto3
        monkeypatch.setattr(_boto3, "client", lambda svc, **kw: bc)
        return bc

    def test_single_chunk_no_splitting(self, monkeypatch):
        """Short transcripts use a single API call."""
        self._patch_bedrock(monkeypatch, ['[{"start": 10.0, "end": 20.0}]'])
        monkeypatch.setenv("AD_DETECT_MAX_CHARS", "50000")
        import ad_remover
        segments = [{"start": 0.0, "end": 5.0, "text": "short transcript"}]
        result = ad_remover.detect_ads(segments)
        assert result == [{"start": 10.0, "end": 20.0}]

    def test_multiple_chunks_results_merged(self, monkeypatch):
        """Results from multiple chunks are merged and deduplicated."""
        # Each segment ~100 chars in formatted line. With max_chars=250, ~2 per chunk.
        segments = [
            {"start": 0.0, "end": 10.0, "text": "intro " * 15},
            {"start": 10.0, "end": 20.0, "text": "ad one " * 15},
            {"start": 100.0, "end": 110.0, "text": "content " * 15},
            {"start": 110.0, "end": 120.0, "text": "ad two " * 15},
        ]
        import ad_remover
        chunks = ad_remover._split_segments_into_chunks(segments, max_chars=250, overlap_secs=5)
        assert len(chunks) >= 2, f"Expected multiple chunks, got {len(chunks)}"
        responses = ['[]'] * len(chunks)
        responses[0] = '[{"start": 10.0, "end": 20.0}]'
        responses[-1] = '[{"start": 110.0, "end": 120.0}]'
        self._patch_bedrock(monkeypatch, responses)
        monkeypatch.setenv("AD_DETECT_MAX_CHARS", "250")
        monkeypatch.setenv("AD_DETECT_OVERLAP_SECS", "5")
        sys.modules.pop("ad_remover", None)
        import ad_remover as ad_remover2

        result = ad_remover2.detect_ads(segments)
        assert len(result) == 2
        assert result[0] == {"start": 10.0, "end": 20.0}
        assert result[1] == {"start": 110.0, "end": 120.0}

    def test_overlapping_chunk_results_deduplicated(self, monkeypatch):
        """Same ad detected in two overlapping chunks is deduplicated."""
        segments = [
            {"start": 0.0, "end": 10.0, "text": "intro " * 15},
            {"start": 40.0, "end": 60.0, "text": "this is an ad " * 10},
            {"start": 70.0, "end": 80.0, "text": "back to content " * 10},
            {"start": 80.0, "end": 90.0, "text": "outro content " * 10},
        ]
        import ad_remover
        chunks = ad_remover._split_segments_into_chunks(segments, max_chars=250, overlap_secs=50)
        assert len(chunks) >= 2, f"Expected multiple chunks, got {len(chunks)}"
        # Both chunks detect the same ad
        responses = ['[{"start": 40.0, "end": 60.0}]'] * len(chunks)
        self._patch_bedrock(monkeypatch, responses)
        monkeypatch.setenv("AD_DETECT_MAX_CHARS", "250")
        monkeypatch.setenv("AD_DETECT_OVERLAP_SECS", "50")
        sys.modules.pop("ad_remover", None)
        import ad_remover as ad_remover2

        result = ad_remover2.detect_ads(segments)
        assert len(result) == 1
        assert result[0] == {"start": 40.0, "end": 60.0}


# ---------------------------------------------------------------------------
# Coverage gap tests — reach the remaining 20 uncovered lines
# ---------------------------------------------------------------------------

class TestDetectSilenceEdgeCases:
    """Cover malformed ffmpeg output branches in detect_silence (lines 102-103, 119-120)."""

    def _run(self, stderr_text):
        import importlib, ad_remover
        importlib.reload(ad_remover)
        fake = MagicMock(stderr=stderr_text)
        with patch("subprocess.run", return_value=fake):
            return ad_remover.detect_silence("/fake.mp3")

    def test_malformed_silence_start_does_not_crash(self):
        """ValueError on bad silence_start line is silently swallowed (line 102-103)."""
        # 'not_a_number' triggers ValueError in float()
        result = self._run("[silencedetect] silence_start: not_a_number\n")
        assert result == []

    def test_malformed_silence_end_does_not_crash(self):
        """ValueError on bad silence_end line resets current_start (lines 119-120)."""
        stderr = (
            "[silencedetect] silence_start: 10.0\n"
            "[silencedetect] silence_end: BAD | silence_duration: also_bad\n"
        )
        result = self._run(stderr)
        assert result == []

    def test_silence_end_without_duration_field_uses_calculation(self):
        """silence_end line without pipe-separated duration falls back to end-start."""
        stderr = (
            "[silencedetect] silence_start: 5.0\n"
            "[silencedetect] silence_end: 6.5\n"  # no | silence_duration part
        )
        result = self._run(stderr)
        assert len(result) == 1
        assert abs(result[0]["duration"] - 1.5) < 0.01


class TestSnapAdBoundariesEdgeCases:
    """Cover snap_ad_boundaries empty-list and exception branches (lines 167, 171-173)."""

    def test_empty_ad_segments_returns_immediately(self):
        """snap_ad_boundaries returns [] without calling detect_silence (line 167)."""
        import importlib, ad_remover
        importlib.reload(ad_remover)
        with patch("ad_remover.detect_silence") as mock_silence:
            result = ad_remover.snap_ad_boundaries([], "/fake.mp3")
        assert result == []
        mock_silence.assert_not_called()

    def test_detect_silence_exception_returns_original_segments(self):
        """If detect_silence raises, original segments are returned unchanged (lines 171-173)."""
        import importlib, ad_remover
        importlib.reload(ad_remover)
        segments = [{"start": 10.0, "end": 50.0}]
        with patch("ad_remover.detect_silence", side_effect=RuntimeError("ffmpeg crashed")):
            result = ad_remover.snap_ad_boundaries(segments, "/fake.mp3")
        assert result == segments


class TestTranscribeCleanupErrors:
    """Cover silent-swallow branches when S3/Transcribe cleanup fails (lines 301-308)."""

    @staticmethod
    def _patch_boto3(monkeypatch, transcribe_client, s3_client=None):
        """Patch boto3.client to return the provided mock clients."""
        if s3_client is None:
            s3_client = MagicMock()

        def _client_factory(service, **kw):
            if service == "s3":
                return s3_client
            return transcribe_client

        import boto3
        monkeypatch.setattr(boto3, "client", _client_factory)

    def _make_transcribe(self):
        """Return a transcribe mock that completes successfully."""
        import json, urllib
        transcript_data = {"results": {"items": []}}
        tc = MagicMock()
        tc.get_transcription_job.return_value = {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "COMPLETED",
                "Transcript": {"TranscriptFileUri": "https://example.com/t.json"},
            }
        }
        return tc

    def test_s3_delete_error_is_swallowed(self, monkeypatch, mock_urlopen):
        """S3 delete_object failure does not propagate (line 301-302)."""
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        s3 = MagicMock()
        s3.delete_object.side_effect = RuntimeError("S3 unavailable")
        tc = self._make_transcribe()
        self._patch_boto3(monkeypatch, tc, s3_client=s3)
        import ad_remover
        # Should not raise despite S3 delete failure
        result = ad_remover.transcribe_audio("/tmp/ep.mp3", "vid123")
        assert isinstance(result, list)
        s3.delete_object.assert_called_once()

    def test_transcribe_job_delete_error_is_swallowed(self, monkeypatch, mock_urlopen):
        """Transcribe job delete failure does not propagate (lines 307-308)."""
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        tc = self._make_transcribe()
        tc.delete_transcription_job.side_effect = RuntimeError("delete failed")
        self._patch_boto3(monkeypatch, tc)
        import ad_remover
        # Should not raise despite job delete failure
        result = ad_remover.transcribe_audio("/tmp/ep.mp3", "vid123")
        assert isinstance(result, list)
        tc.delete_transcription_job.assert_called_once()


class TestVerifyAdSegmentNoJson:
    """Cover the no-JSON-in-verification-response branch (lines 503-504)."""

    def test_no_json_in_response_keeps_segment(self, monkeypatch):
        """If the verification response contains no JSON object, segment is kept."""
        import importlib, ad_remover
        importlib.reload(ad_remover)

        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "I cannot determine if this is an ad."}]}}
        }
        segs = [{"start": 100.0, "end": 200.0, "text": "some content"}]
        seg = {"start": 100.0, "end": 200.0}

        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            result = ad_remover._verify_ad_segment(seg, segs, mock_bedrock, "test-model")

        assert result is True, "Segment with no-JSON response should be kept (fail-safe)"


class TestSpliceAudioEdgeCases:
    """Cover the remaining splice_audio error branches (lines 782-783, 789, 804, 815-816)."""

    def test_raises_runtime_error_when_getsize_fails(self, monkeypatch):
        """OSError from os.path.getsize is wrapped in RuntimeError (lines 782-783)."""
        import ad_remover
        monkeypatch.setattr(os.path, "getsize", MagicMock(side_effect=OSError("no such file")))
        with pytest.raises(RuntimeError, match="cannot stat input file"):
            ad_remover.splice_audio("/missing.mp3", [{"start": 0.0, "end": 10.0}], "/out.mp3")

    def test_raises_runtime_error_for_tiny_file(self, monkeypatch):
        """Files smaller than 1 KB are rejected as corrupt (line 789)."""
        import ad_remover
        monkeypatch.setattr(os.path, "getsize", lambda p: 512)  # 512 bytes < 1 KB
        with pytest.raises(RuntimeError, match="suspiciously small"):
            ad_remover.splice_audio("/tiny.mp3", [{"start": 0.0, "end": 10.0}], "/out.mp3")

    def test_ffprobe_stderr_is_logged_but_does_not_fail(self, monkeypatch):
        """Non-empty ffprobe stderr is logged at DEBUG and does not block execution (line 804)."""
        import ad_remover
        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd[0])
            if cmd[0] == "ffprobe":
                return MagicMock(stdout="300.0\n", stderr="some non-fatal ffprobe warning", returncode=0)
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Should complete without raising despite ffprobe stderr output
        ad_remover.splice_audio("/in.mp3", [{"start": 10.0, "end": 50.0}], "/out.mp3")
        assert "ffprobe" in run_calls

    def test_raises_runtime_error_for_non_subprocess_ffprobe_error(self, monkeypatch):
        """Non-CalledProcessError from ffprobe is wrapped in RuntimeError (lines 815-816)."""
        import ad_remover
        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                raise FileNotFoundError("ffprobe not found")
            return MagicMock()

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="ffprobe error"):
            ad_remover.splice_audio("/in.mp3", [{"start": 10.0, "end": 50.0}], "/out.mp3")
