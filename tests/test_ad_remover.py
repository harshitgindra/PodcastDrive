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
        """All three duration-detection stages fail → RuntimeError with descriptive message."""
        import ad_remover
        import mutagen.mp3 as _mut
        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)
        # Make every subprocess.run call raise CalledProcessError (covers both ffprobe attempts)
        monkeypatch.setattr(
            subprocess, "run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, "ffprobe")),
        )
        # Make mutagen also fail so stage 3 is exhausted
        monkeypatch.setattr(_mut, "MP3", MagicMock(side_effect=RuntimeError("mutagen unavailable")))
        with pytest.raises(RuntimeError, match="All duration-detection methods failed"):
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

        result, _segs, _summary = ad_remover.remove_ads("/ep.mp3", "vid123", tmp_dir)
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

        result, _segs, _summary = ad_remover.remove_ads("/ep.mp3", "vid_dry", str(tmp_path))

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

        result, _segs, _summary = ad_remover.remove_ads("/ep.mp3", "vid_dry_clean", str(tmp_path))

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

            result, _segs, _summary = ad_remover.remove_ads("/ep.mp3", f"vid_{val}", str(tmp_path))
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
        """Non-CalledProcessError from ffprobe (e.g. FileNotFoundError) exhausts all
        three duration-detection stages and raises a descriptive RuntimeError."""
        import ad_remover
        import mutagen.mp3 as _mut
        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                raise FileNotFoundError("ffprobe not found")
            return MagicMock()

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Exhaust stage 3 (mutagen) as well so the chain raises
        monkeypatch.setattr(_mut, "MP3", MagicMock(side_effect=RuntimeError("mutagen unavailable")))

        with pytest.raises(RuntimeError, match="All duration-detection methods failed"):
            ad_remover.splice_audio("/in.mp3", [{"start": 10.0, "end": 50.0}], "/out.mp3")


class TestBedrockModelTiering:
    """BEDROCK_DETECT_MODEL_ID allows cheaper model for detection vs verification."""

    def test_detect_uses_detect_model_id_when_set(self, monkeypatch):
        """BEDROCK_DETECT_MODEL_ID is used by detect_ads when set."""
        import ad_remover
        monkeypatch.setenv("BEDROCK_DETECT_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251015-v1:0")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

        called_model_ids = []

        def fake_converse(**kwargs):
            called_model_ids.append(kwargs["modelId"])
            return {"output": {"message": {"content": [{"text": "[]"}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=lambda *a, **kw: mock_client))

        segments = [{"start": 0.0, "end": 30.0, "text": "hello world"}]
        ad_remover.detect_ads(segments)

        assert called_model_ids, "Bedrock should have been called"
        assert called_model_ids[0] == "us.anthropic.claude-haiku-4-5-20251015-v1:0"

    def test_detect_falls_back_to_bedrock_model_id_when_detect_not_set(self, monkeypatch):
        """detect_ads uses BEDROCK_MODEL_ID when BEDROCK_DETECT_MODEL_ID is absent."""
        import ad_remover
        monkeypatch.delenv("BEDROCK_DETECT_MODEL_ID", raising=False)
        monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

        called_model_ids = []

        def fake_converse(**kwargs):
            called_model_ids.append(kwargs["modelId"])
            return {"output": {"message": {"content": [{"text": "[]"}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=lambda *a, **kw: mock_client))

        segments = [{"start": 0.0, "end": 30.0, "text": "hello world"}]
        ad_remover.detect_ads(segments)

        assert called_model_ids[0] == "us.anthropic.claude-sonnet-4-6"


    def test_verify_uses_bedrock_model_id_not_detect_model_id(self, monkeypatch):
        """Bug fix: verification (second-pass) must use BEDROCK_MODEL_ID, not BEDROCK_DETECT_MODEL_ID.

        When BEDROCK_DETECT_MODEL_ID=haiku (cheap) and BEDROCK_MODEL_ID=sonnet-4-6 (accurate),
        the first Bedrock call (detection) should use haiku and the second (verification) sonnet-4-6.
        Previously both calls incorrectly used haiku.
        """
        import importlib, ad_remover
        importlib.reload(ad_remover)
        monkeypatch.setenv("BEDROCK_DETECT_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251015-v1:0")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        monkeypatch.setenv("AD_VERIFY_THRESHOLD_SECS", "10")
        monkeypatch.setenv("MAX_AD_SEGMENT_SECS", "9999")

        called_model_ids = []
        call_count = [0]

        def fake_converse(**kwargs):
            called_model_ids.append(kwargs["modelId"])
            call_count[0] += 1
            if call_count[0] == 1:
                return {"output": {"message": {"content": [{"text": '[{"start": 0.0, "end": 30.0}]'}]}}}
            return {"output": {"message": {"content": [{"text": '{"is_ad": true, "reason": "confirmed ad"}'}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=lambda *a, **kw: mock_client))

        segments = [{"start": 0.0, "end": 30.0, "text": "use code PODCAST20 for 20 percent off"}]
        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            result = ad_remover.detect_ads(segments)

        assert call_count[0] == 2, "Should have made detection + verification calls"
        assert called_model_ids[0] == "us.anthropic.claude-haiku-4-5-20251015-v1:0", \
            "Detection call must use BEDROCK_DETECT_MODEL_ID (haiku)"
        assert called_model_ids[1] == "us.anthropic.claude-sonnet-4-6", \
            "Verification call must use BEDROCK_MODEL_ID (sonnet-4-6), not the detect model"
        assert len(result) == 1, "Confirmed ad should be in results"

    def test_verify_threshold_zero_verifies_all_segments(self, monkeypatch):
        """AD_VERIFY_THRESHOLD_SECS=0 should verify every segment (as documented).

        Previously the outer guard verify_threshold > 0 incorrectly skipped all
        verification when threshold=0. Now >= 0 allows the inner loop to run and
        every segment (duration >= 0) gets a second-pass call.
        """
        import importlib, ad_remover
        importlib.reload(ad_remover)
        monkeypatch.setenv("AD_VERIFY_THRESHOLD_SECS", "0")
        monkeypatch.setenv("MAX_AD_SEGMENT_SECS", "9999")
        monkeypatch.delenv("BEDROCK_DETECT_MODEL_ID", raising=False)

        call_count = [0]

        def fake_converse(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"output": {"message": {"content": [{"text": '[{"start": 5.0, "end": 15.0}]'}]}}}
            return {"output": {"message": {"content": [{"text": '{"is_ad": true, "reason": "confirmed"}'}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=lambda *a, **kw: mock_client))

        segments = [{"start": 5.0, "end": 15.0, "text": "ad content"}]
        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            result = ad_remover.detect_ads(segments)

        assert call_count[0] == 2, (
            f"threshold=0 should trigger verification for every segment; got {call_count[0]} calls"
        )
        assert len(result) == 1


class TestTranscriptCache:
    """_load_transcript_cache, _save_transcript_cache, and transcribe_audio cache integration."""

    def test_load_returns_none_on_cache_miss(self, monkeypatch):
        """Cache MISS (NoSuchKey) returns None without raising."""
        import ad_remover
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": ""}}, "GetObject"
        )
        result = ad_remover._load_transcript_cache(mock_s3, "my-bucket", "ep123")
        assert result is None

    def test_load_returns_segments_on_cache_hit(self, monkeypatch):
        """Cache HIT returns the stored segment list."""
        import ad_remover
        import json

        segments = [{"start": 0.0, "end": 5.0, "text": "hello"}]
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(segments).encode())
        }
        result = ad_remover._load_transcript_cache(mock_s3, "my-bucket", "ep123")
        assert result == segments

    def test_load_returns_none_on_corrupt_cache(self, monkeypatch):
        """Non-list cache content returns None gracefully."""
        import ad_remover
        import json

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps({"not": "a list"}).encode())
        }
        result = ad_remover._load_transcript_cache(mock_s3, "my-bucket", "ep123")
        assert result is None

    def test_save_puts_object_to_s3(self, monkeypatch):
        """_save_transcript_cache writes JSON to correct S3 key."""
        import ad_remover

        mock_s3 = MagicMock()
        segments = [{"start": 1.0, "end": 10.0, "text": "ad content"}]
        ad_remover._save_transcript_cache(mock_s3, "my-bucket", "ep456", segments)

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "my-bucket"
        assert "ep456" in call_kwargs["Key"]
        import json
        assert json.loads(call_kwargs["Body"].decode()) == segments

    def test_save_swallows_exceptions(self, monkeypatch):
        """_save_transcript_cache never raises even if S3 write fails."""
        import ad_remover

        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = RuntimeError("network error")
        # Should not raise
        ad_remover._save_transcript_cache(mock_s3, "my-bucket", "ep789", [])

    def test_transcribe_returns_cache_when_hit(self, monkeypatch):
        """transcribe_audio returns cached segments without calling Transcribe."""
        import ad_remover
        import json

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")

        cached = [{"start": 5.0, "end": 10.0, "text": "sponsored by"}]

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(cached).encode())
        }
        mock_transcribe = MagicMock()

        def fake_client(service, **kw):
            return mock_s3 if service == "s3" else mock_transcribe

        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=fake_client))

        result = ad_remover.transcribe_audio("/fake.mp3", "ep_cached")
        assert result == cached
        mock_transcribe.start_transcription_job.assert_not_called()

    def test_eval_jobs_skip_cache(self, monkeypatch):
        """eval- prefixed video_ids bypass cache (evaluator re-transcribes cleaned file)."""
        import ad_remover
        import json

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")

        # S3 would return a hit if consulted — but it should NOT be consulted
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps([]).encode())
        }

        # Transcribe returns COMPLETED immediately with a valid transcript
        transcript = {"results": {"items": []}}
        import urllib.request as urllib_req

        def fake_urlopen(url, context=None):
            return MagicMock(
                __enter__=lambda s: s,
                __exit__=lambda s, *a: None,
                read=lambda: json.dumps(transcript).encode(),
            )

        mock_transcribe = MagicMock()
        mock_transcribe.get_transcription_job.return_value = {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "COMPLETED",
                "Transcript": {"TranscriptFileUri": "https://fake/transcript.json"},
            }
        }

        def fake_client(service, **kw):
            return mock_s3 if service == "s3" else mock_transcribe

        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=fake_client))
        monkeypatch.setattr(urllib_req, "urlopen", fake_urlopen)

        ad_remover.transcribe_audio("/fake.mp3", "eval-ep123")
        # Cache should NOT have been consulted (get_object not called)
        mock_s3.get_object.assert_not_called()
        # And cache should NOT have been saved either
        mock_s3.put_object.assert_not_called()

    def test_cache_disabled_skips_lookup(self, monkeypatch):
        """TRANSCRIBE_CACHE_ENABLED=false skips cache lookup entirely."""
        import ad_remover
        import json

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "false")

        mock_s3 = MagicMock()
        transcript = {"results": {"items": []}}

        import urllib.request as urllib_req

        def fake_urlopen(url, context=None):
            return MagicMock(
                __enter__=lambda s: s,
                __exit__=lambda s, *a: None,
                read=lambda: json.dumps(transcript).encode(),
            )

        mock_transcribe = MagicMock()
        mock_transcribe.get_transcription_job.return_value = {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "COMPLETED",
                "Transcript": {"TranscriptFileUri": "https://fake/transcript.json"},
            }
        }

        def fake_client(service, **kw):
            return mock_s3 if service == "s3" else mock_transcribe

        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=fake_client))
        monkeypatch.setattr(urllib_req, "urlopen", fake_urlopen)

        ad_remover.transcribe_audio("/fake.mp3", "ep_no_cache")
        # Cache get_object must not have been called
        mock_s3.get_object.assert_not_called()


class TestDirectionalSnap:
    """_snap_to_silence_boundary respects prefer_earlier for start vs end snapping."""

    def _snap(self, time, silences, window=3.0, prefer_earlier=False):
        import ad_remover
        return ad_remover._snap_to_silence_boundary(time, silences, window, prefer_earlier)

    def test_prefer_earlier_breaks_tie_towards_earlier_candidate(self):
        """Two equidistant silence boundaries: prefer_earlier=True picks the earlier one.
        silence A: end=97 (dist 3 from 100); silence B: start=103 (dist 3 from 100).
        """
        silences = [
            {"start": 94.0, "end": 97.0},   # end at 97.0 — 3s before target
            {"start": 103.0, "end": 106.0},  # start at 103.0 — 3s after target
        ]
        result = self._snap(100.0, silences, window=3.0, prefer_earlier=True)
        assert result == 97.0, "prefer_earlier should pick 97.0 over 103.0 on a tie"

    def test_prefer_later_breaks_tie_towards_later_candidate(self):
        """Two equidistant silence boundaries: prefer_earlier=False picks the later one."""
        silences = [
            {"start": 94.0, "end": 97.0},
            {"start": 103.0, "end": 106.0},
        ]
        result = self._snap(100.0, silences, window=3.0, prefer_earlier=False)
        assert result == 103.0, "prefer_later should pick 103.0 over 97.0 on a tie"

    def test_snap_ad_boundaries_uses_prefer_earlier_for_start(self, monkeypatch):
        """snap_ad_boundaries snaps starts with prefer_earlier=True.
        Two equidistant silence boundaries — start should pick the earlier one.
        """
        import ad_remover

        silences = [
            {"start": 94.0, "end": 97.0},   # end=97.0 — 3s before start at 100
            {"start": 103.0, "end": 106.0},  # start=103.0 — 3s after start at 100
        ]
        monkeypatch.setattr(ad_remover, "detect_silence", lambda *a, **kw: silences)

        result = ad_remover.snap_ad_boundaries(
            [{"start": 100.0, "end": 200.0}], "/fake.mp3"
        )
        # Start should snap to 97.0 (earlier) not 103.0 (later)
        assert result[0]["start"] == 97.0

    def test_snap_ad_boundaries_uses_prefer_later_for_end(self, monkeypatch):
        """snap_ad_boundaries snaps ends with prefer_earlier=False.
        Two equidistant silence boundaries — end should pick the later one.
        """
        import ad_remover

        silences = [
            {"start": 194.0, "end": 197.0},  # end=197.0 — 3s before end at 200
            {"start": 203.0, "end": 206.0},  # start=203.0 — 3s after end at 200
        ]
        monkeypatch.setattr(ad_remover, "detect_silence", lambda *a, **kw: silences)

        result = ad_remover.snap_ad_boundaries(
            [{"start": 10.0, "end": 200.0}], "/fake.mp3"
        )
        # End should snap to 203.0 (later) not 197.0 (earlier)
        assert result[0]["end"] == 203.0


class TestLoudnorm:
    """SPLICE_LOUDNORM controls loudnorm filter in ffmpeg command."""

    def _splice(self, monkeypatch, loudnorm_env, capture_cmd):
        import ad_remover
        monkeypatch.setenv("SPLICE_LOUDNORM", loudnorm_env)
        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)

        def fake_run(cmd, **kwargs):
            capture_cmd.extend(cmd)
            if cmd[0] == "ffprobe":
                return MagicMock(stdout="300.0\n", stderr="", returncode=0)
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        ad_remover.splice_audio("/in.mp3", [{"start": 10.0, "end": 50.0}], "/out.mp3")

    def test_loudnorm_filter_included_by_default(self, monkeypatch):
        """loudnorm=true adds loudnorm filter to filter_complex."""
        cmd = []
        self._splice(monkeypatch, "true", cmd)
        fc = " ".join(cmd)
        assert "loudnorm" in fc, "loudnorm filter should appear in ffmpeg command"

    def test_loudnorm_filter_excluded_when_disabled(self, monkeypatch):
        """SPLICE_LOUDNORM=false omits the loudnorm filter."""
        cmd = []
        self._splice(monkeypatch, "false", cmd)
        fc = " ".join(cmd)
        assert "loudnorm" not in fc, "loudnorm filter should be absent when disabled"


class TestAdSegmentsCache:
    """_load_ad_segments_cache, _save_ad_segments_cache, and remove_ads cache integration."""

    def test_load_returns_none_on_miss(self):
        """Cache MISS returns None."""
        import ad_remover
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": ""}}, "GetObject"
        )
        result = ad_remover._load_ad_segments_cache(mock_s3, "bucket", "ep1")
        assert result is None

    def test_load_returns_cached_segments_on_hit(self):
        """Cache HIT returns the stored ad_segments list."""
        import ad_remover, json

        expected = [{"start": 10.0, "end": 50.0}]
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(expected).encode())
        }
        result = ad_remover._load_ad_segments_cache(mock_s3, "bucket", "ep1")
        assert result == expected

    def test_save_writes_correct_key(self):
        """_save_ad_segments_cache stores data at the expected S3 key."""
        import ad_remover, json

        mock_s3 = MagicMock()
        segs = [{"start": 5.0, "end": 30.0}]
        ad_remover._save_ad_segments_cache(mock_s3, "bucket", "ep1", segs)

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        # Key should end with _ads.json
        assert call_kwargs["Key"].endswith("_ads.json")
        assert json.loads(call_kwargs["Body"].decode()) == segs

    def test_remove_ads_uses_cached_segments_skipping_transcribe_and_detect(self, monkeypatch, tmp_path):
        """remove_ads returns from ad-segments cache without calling Transcribe or Bedrock."""
        import ad_remover, json

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")
        monkeypatch.setenv("AD_SNAP_TO_SILENCE", "false")

        src = tmp_path / "episode.mp3"
        src.write_bytes(b"X" * 5000)

        cached_ads = [{"start": 10.0, "end": 50.0}]

        # S3: get_object returns ad-segments cache; put_object (from transcript) is irrelevant
        def fake_get_object(Bucket, Key):
            if "_ads.json" in Key:
                return {"Body": MagicMock(read=lambda: json.dumps(cached_ads).encode())}
            raise Exception("not called for transcript in this path")

        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = fake_get_object

        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=lambda *a, **kw: mock_s3))

        # splice_audio is called with the cached segments
        spliced_calls = []
        def fake_splice(mp3_path, segs, out_path):
            spliced_calls.append(segs)
            # create the output file so the path exists
            import pathlib
            pathlib.Path(out_path).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "splice_audio", fake_splice)
        monkeypatch.setattr(ad_remover, "transcribe_audio", lambda *a: (_ for _ in ()).throw(AssertionError("should not transcribe")))

        result_path, result_segs, _summary = ad_remover.remove_ads(str(src), "ep_cached", str(tmp_path))

        assert spliced_calls, "splice_audio should have been called"
        assert spliced_calls[0] == cached_ads
        assert result_segs == cached_ads


# ---------------------------------------------------------------------------
# Fix #5 – Selective transcription windows (_parse_transcribe_windows,
#           _extract_audio_window, _get_audio_duration)
# ---------------------------------------------------------------------------

class TestParseTranscribeWindows:
    """Unit tests for _parse_transcribe_windows."""

    from ad_remover import _parse_transcribe_windows

    def test_absolute_range(self):
        from ad_remover import _parse_transcribe_windows
        windows = _parse_transcribe_windows("0:300", 3600.0)
        assert windows == [(0.0, 300.0)]

    def test_end_keyword(self):
        from ad_remover import _parse_transcribe_windows
        windows = _parse_transcribe_windows("0:end", 600.0)
        assert windows == [(0.0, 600.0)]

    def test_end_minus_offset(self):
        from ad_remover import _parse_transcribe_windows
        windows = _parse_transcribe_windows("end-120:end", 600.0)
        assert windows == [(480.0, 600.0)]

    def test_multiple_windows(self):
        from ad_remover import _parse_transcribe_windows
        windows = _parse_transcribe_windows("0:300,end-600:end", 3600.0)
        assert windows == [(0.0, 300.0), (3000.0, 3600.0)]

    def test_empty_string_returns_empty(self):
        from ad_remover import _parse_transcribe_windows
        assert _parse_transcribe_windows("", 3600.0) == []

    def test_degenerate_window_skipped(self):
        from ad_remover import _parse_transcribe_windows
        # start >= end → skipped
        windows = _parse_transcribe_windows("300:100", 3600.0)
        assert windows == []

    def test_invalid_entry_skipped(self):
        from ad_remover import _parse_transcribe_windows
        # no colon → invalid; valid entry still parsed
        windows = _parse_transcribe_windows("bad,0:60", 600.0)
        assert windows == [(0.0, 60.0)]

    def test_clamped_to_duration(self):
        from ad_remover import _parse_transcribe_windows
        windows = _parse_transcribe_windows("0:9999", 600.0)
        assert windows == [(0.0, 600.0)]


class TestGetAudioDuration:
    def test_returns_float_from_ffprobe(self, tmp_path):
        from ad_remover import _get_audio_duration
        fake_mp3 = tmp_path / "ep.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" * 100)

        with patch("ad_remover.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="3600.123\n", stderr="")
            duration = _get_audio_duration(str(fake_mp3))

        assert duration == pytest.approx(3600.123)

    def test_returns_zero_on_failure(self, tmp_path):
        from ad_remover import _get_audio_duration
        with patch("ad_remover.subprocess.run", side_effect=OSError("ffprobe not found")):
            assert _get_audio_duration("nonexistent.mp3") == 0.0


class TestExtractAudioWindow:
    def test_calls_ffmpeg_with_correct_args(self, tmp_path):
        from ad_remover import _extract_audio_window
        with patch("ad_remover.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _extract_audio_window("input.mp3", 30.0, 120.0, "output.mp3")

        cmd = mock_run.call_args[0][0]
        assert "-ss" in cmd and "30.0" in cmd
        assert "-to" in cmd and "120.0" in cmd
        assert "output.mp3" in cmd

    def test_raises_on_nonzero_returncode(self, tmp_path):
        from ad_remover import _extract_audio_window
        with patch("ad_remover.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error message")
            with pytest.raises(RuntimeError, match="ffmpeg window extract failed"):
                _extract_audio_window("input.mp3", 0.0, 60.0, "output.mp3")


# ---------------------------------------------------------------------------
# Fix #8 – Per-podcast ad_hints injected into detection prompt
# ---------------------------------------------------------------------------

class TestDetectAdsWithHints:
    """Tests for ad_hints parameter in detect_ads (fix #8)."""

    def _make_segments(self, n: int = 3) -> list[dict]:
        return [{"start": float(i * 10), "end": float(i * 10 + 9), "text": f"Word {i}"} for i in range(n)]

    def _make_bedrock_response(self, payload: str):
        resp = MagicMock()
        resp.__getitem__ = lambda s, k: {
            "output": {"message": {"content": [{"text": payload}]}}
        }[k]
        return resp

    def test_hints_section_included_in_prompt(self):
        """When ad_hints is non-empty, _AD_HINTS_SECTION content appears in prompt."""
        from ad_remover import detect_ads

        captured_prompts = []

        def fake_converse(**kwargs):
            captured_prompts.append(kwargs["messages"][0]["content"][0]["text"])
            return {"output": {"message": {"content": [{"text": "[]"}]}}}

        bedrock_mock = MagicMock()
        bedrock_mock.converse.side_effect = fake_converse

        with patch("ad_remover.boto3.client", return_value=bedrock_mock):
            detect_ads(self._make_segments(), ad_hints="Ads always start with 'Brought to you by'")

        assert captured_prompts, "No prompt captured"
        assert "Podcast-specific ad patterns" in captured_prompts[0]
        assert "Brought to you by" in captured_prompts[0]

    def test_no_hints_section_when_empty(self):
        """When ad_hints is empty, no hints section appears in prompt."""
        from ad_remover import detect_ads

        captured_prompts = []

        def fake_converse(**kwargs):
            captured_prompts.append(kwargs["messages"][0]["content"][0]["text"])
            return {"output": {"message": {"content": [{"text": "[]"}]}}}

        bedrock_mock = MagicMock()
        bedrock_mock.converse.side_effect = fake_converse

        with patch("ad_remover.boto3.client", return_value=bedrock_mock):
            detect_ads(self._make_segments(), ad_hints="")

        assert "Podcast-specific ad patterns" not in captured_prompts[0]


# ---------------------------------------------------------------------------
# Windowed transcription (AD_TRANSCRIBE_WINDOWS) — lines 511-593
# ---------------------------------------------------------------------------

class TestWindowedTranscription:
    """transcribe_audio with AD_TRANSCRIBE_WINDOWS set processes sub-clips."""

    @staticmethod
    def _patch_boto3(monkeypatch, s3_client, transcribe_client):
        import boto3 as _boto3
        def fake_client(service, **kw):
            if service == "s3":
                return s3_client
            return transcribe_client
        monkeypatch.setattr(_boto3, "client", fake_client)

    @staticmethod
    def _make_transcribe_client_completed():
        transcript_data = {
            "results": {
                "items": [
                    {"type": "pronunciation", "start_time": "1.0", "end_time": "2.0",
                     "alternatives": [{"content": "Hello"}]},
                ]
            }
        }
        import json, urllib.request as ur
        fake_resp = MagicMock()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)
        fake_resp.read.return_value = json.dumps(transcript_data).encode()

        tc = MagicMock()
        tc.get_transcription_job.return_value = {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "COMPLETED",
                "Transcript": {"TranscriptFileUri": "https://fake/t.json"},
            }
        }
        return tc, fake_resp

    def test_windowed_transcription_offsets_segments(self, monkeypatch, tmp_path, mock_sleep):
        """Segments from windowed transcription have start/end offset by window start."""
        import ad_remover, json, urllib.request as ur

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("AD_TRANSCRIBE_WINDOWS", "300:600")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "false")

        # Create a real temp file so _get_audio_duration doesn't fail stat
        fake_mp3 = tmp_path / "ep.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" * 100)

        tc, fake_resp = self._make_transcribe_client_completed()
        s3 = MagicMock()
        self._patch_boto3(monkeypatch, s3, tc)

        # Mock _get_audio_duration to return 3600s
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 3600.0)
        # Mock _extract_audio_window to not actually run ffmpeg
        monkeypatch.setattr(ad_remover, "_extract_audio_window", lambda *a: None)
        # Mock urlopen to return fake transcript
        monkeypatch.setattr(ur, "urlopen", MagicMock(return_value=fake_resp))

        result = ad_remover.transcribe_audio(str(fake_mp3), "vid_windowed")

        assert len(result) == 1
        # Original word at 1.0-2.0s should be offset by window start (300s)
        assert result[0]["start"] == pytest.approx(301.0)
        assert result[0]["end"] == pytest.approx(302.0)

    def test_windowed_transcription_multiple_windows(self, monkeypatch, tmp_path, mock_sleep):
        """Multiple windows produce merged and sorted segments."""
        import ad_remover, json, urllib.request as ur

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("AD_TRANSCRIBE_WINDOWS", "0:60,3540:3600")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "false")

        fake_mp3 = tmp_path / "ep.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" * 100)

        # First window returns word at 5s; second at 1s (offset 3540 → 3541)
        def make_transcript(word, start, end):
            return json.dumps({
                "results": {"items": [
                    {"type": "pronunciation", "start_time": str(start), "end_time": str(end),
                     "alternatives": [{"content": word}]},
                ]}
            }).encode()

        call_count = [0]
        def fake_urlopen(url, context=None):
            call_count[0] += 1
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            if call_count[0] == 1:
                resp.read.return_value = make_transcript("Intro", 5.0, 6.0)
            else:
                resp.read.return_value = make_transcript("Outro", 1.0, 2.0)
            return resp

        tc = MagicMock()
        tc.get_transcription_job.return_value = {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "COMPLETED",
                "Transcript": {"TranscriptFileUri": "https://fake/t.json"},
            }
        }
        s3 = MagicMock()
        self._patch_boto3(monkeypatch, s3, tc)

        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 3600.0)
        monkeypatch.setattr(ad_remover, "_extract_audio_window", lambda *a: None)
        monkeypatch.setattr(ur, "urlopen", fake_urlopen)

        result = ad_remover.transcribe_audio(str(fake_mp3), "vid_multi_window")

        assert len(result) == 2
        # Should be sorted: first window's word (5.0) before second (3541.0)
        assert result[0]["start"] == pytest.approx(5.0)
        assert result[1]["start"] == pytest.approx(3541.0)

    def test_windowed_transcription_cleans_up_on_failure(self, monkeypatch, tmp_path, mock_sleep):
        """Temp S3 object and Transcribe job are deleted even when window job fails."""
        import ad_remover, urllib.request as ur

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("AD_TRANSCRIBE_WINDOWS", "0:300")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "false")

        fake_mp3 = tmp_path / "ep.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" * 100)

        tc = MagicMock()
        tc.get_transcription_job.return_value = {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "FAILED",
                "FailureReason": "Bad audio",
                "Transcript": {},
            }
        }
        s3 = MagicMock()
        self._patch_boto3(monkeypatch, s3, tc)

        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 3600.0)
        monkeypatch.setattr(ad_remover, "_extract_audio_window", lambda *a: None)

        with pytest.raises(RuntimeError, match="failed"):
            ad_remover.transcribe_audio(str(fake_mp3), "vid_fail_window")

        # S3 delete and transcribe job delete should have been called (cleanup)
        s3.delete_object.assert_called()
        tc.delete_transcription_job.assert_called()

    def test_empty_windows_falls_through_to_normal_transcription(self, monkeypatch, tmp_path, mock_sleep):
        """When AD_TRANSCRIBE_WINDOWS parses to empty list, full-file transcription runs."""
        import ad_remover, json, urllib.request as ur

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        # Degenerate window (start >= end) → empty → fallthrough
        monkeypatch.setenv("AD_TRANSCRIBE_WINDOWS", "300:100")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "false")

        fake_mp3 = tmp_path / "ep.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" * 100)

        transcript_data = {"results": {"items": []}}
        fake_resp = MagicMock()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)
        fake_resp.read.return_value = json.dumps(transcript_data).encode()

        tc = MagicMock()
        tc.get_transcription_job.return_value = {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "COMPLETED",
                "Transcript": {"TranscriptFileUri": "https://fake/t.json"},
            }
        }
        s3 = MagicMock()
        self._patch_boto3(monkeypatch, s3, tc)
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 3600.0)
        monkeypatch.setattr(ur, "urlopen", MagicMock(return_value=fake_resp))

        # Should not raise — falls through to normal full-file transcription
        result = ad_remover.transcribe_audio(str(fake_mp3), "vid_empty_windows")
        assert isinstance(result, list)
        # Normal transcription should have uploaded the full file
        s3.upload_file.assert_called()
