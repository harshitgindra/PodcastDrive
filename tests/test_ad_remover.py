"""Unit tests for src/ad_remover.py.

All external I/O is mocked:
  - boto3 S3 / Transcribe / Bedrock clients  → patched with MagicMock via monkeypatch
  - urllib.request.urlopen                   → patched to return fake transcript JSON
  - subprocess.run                           → patched for ffprobe / ffmpeg calls
  - time.sleep                               → patched to avoid real waits
"""

from __future__ import annotations

import json
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

        probe_result = MagicMock(stdout="600.0\n", returncode=0)
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

        probe_result = MagicMock(stdout="300.0\n", returncode=0)
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
        monkeypatch.setattr(
            subprocess, "run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, "ffprobe")),
        )
        with pytest.raises(RuntimeError, match="ffprobe failed"):
            ad_remover.splice_audio("/in.mp3", [{"start": 10.0, "end": 20.0}], "/out.mp3")

    def test_raises_on_ffmpeg_failure(self, monkeypatch):
        import ad_remover

        probe_result = MagicMock(stdout="300.0\n", returncode=0)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return probe_result
            raise subprocess.CalledProcessError(1, cmd, stderr="ffmpeg error")

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="ffmpeg splice failed"):
            ad_remover.splice_audio("/in.mp3", [{"start": 10.0, "end": 20.0}], "/out.mp3")

    def test_raises_when_ads_cover_entire_file(self, monkeypatch):
        import ad_remover

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return MagicMock(stdout="60.0\n")
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
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp") == "/ep.mp3"

    def test_returns_original_when_disabled_zero(self, monkeypatch):
        monkeypatch.setenv("REMOVE_ADS", "0")
        import ad_remover
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp") == "/ep.mp3"

    def test_returns_original_on_transcription_failure(self, monkeypatch):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(side_effect=RuntimeError("boom")))
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp") == "/ep.mp3"

    def test_returns_original_on_ad_detection_failure(self, monkeypatch):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "x"}]))
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(side_effect=ConnectionError("no bedrock")))
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp") == "/ep.mp3"

    def test_returns_original_when_no_ads(self, monkeypatch):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "clean"}]))
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[]))
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp") == "/ep.mp3"

    def test_returns_original_on_splice_failure(self, monkeypatch):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "ad"}]))
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[{"start": 0.2, "end": 0.8}]))
        monkeypatch.setattr(ad_remover, "splice_audio", MagicMock(side_effect=RuntimeError("ffmpeg gone")))
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp") == "/ep.mp3"

    def test_returns_cleaned_path_on_success(self, monkeypatch, tmp_path):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover
        import os
        tmp_dir = str(tmp_path)
        monkeypatch.setattr(ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "ad"}]))
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[{"start": 0.2, "end": 0.8}]))
        monkeypatch.setattr(ad_remover, "splice_audio", MagicMock(return_value=None))

        result = ad_remover.remove_ads("/ep.mp3", "vid123", tmp_dir)
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
