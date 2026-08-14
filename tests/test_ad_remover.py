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
from unittest.mock import MagicMock, patch

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
            {
                "type": "pronunciation",
                "start_time": "0.5",
                "end_time": "1.0",
                "alternatives": [{"content": "Hello", "confidence": "0.99"}],
            },
            {
                "type": "pronunciation",
                "start_time": "1.1",
                "end_time": "1.8",
                "alternatives": [{"content": "world", "confidence": "0.99"}],
            },
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


def _make_bedrock_client(content: str = "[]", prefill: str = "["):
    """Return a mock boto3 bedrock-runtime client.

    The actual code prepends *prefill* to the model output (assistant-turn prefill).
    This helper strips the prefill from *content* so the caller can pass the full
    expected JSON and the mock simulates the model returning everything after the prefill.
    """
    # Strip leading prefill character from mock content (code will re-add it)
    mock_text = content[len(prefill) :] if content.startswith(prefill) else content
    client = MagicMock()
    client.converse.return_value = {"output": {"message": {"content": [{"text": mock_text}]}}}
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
            {"type": "pronunciation", "start_time": "0.0", "end_time": "0.5", "alternatives": [{"content": "Hello"}]},
            {"type": "pronunciation", "start_time": "0.6", "end_time": "1.0", "alternatives": [{"content": "world"}]},
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
            {"type": "pronunciation", "start_time": "0.0", "end_time": "1.0", "alternatives": [{"content": "Intro"}]},
            {"type": "pronunciation", "start_time": "5.0", "end_time": "6.0", "alternatives": [{"content": "Ad"}]},
        ]
        segs = ad_remover._items_to_segments(items, gap_threshold=1.5)
        assert len(segs) == 2
        assert segs[0]["text"] == "Intro"
        assert segs[1]["text"] == "Ad"

    def test_punctuation_items_appended(self):
        """Punctuation items (no timing) are appended to the current segment text."""
        import ad_remover

        items = [
            {"type": "pronunciation", "start_time": "0.0", "end_time": "0.5", "alternatives": [{"content": "Hello"}]},
            {"type": "punctuation", "alternatives": [{"content": ","}]},
            {"type": "pronunciation", "start_time": "0.6", "end_time": "1.0", "alternatives": [{"content": "world"}]},
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
            {"type": "pronunciation", "start_time": "0.0", "end_time": "0.5", "alternatives": []},
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
        import mutagen.mp3 as _mut

        import ad_remover

        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)
        # Make every subprocess.run call raise CalledProcessError (covers both ffprobe attempts)
        monkeypatch.setattr(
            subprocess,
            "run",
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

        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "x"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(side_effect=ConnectionError("no bedrock")))
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp")[0] == "/ep.mp3"

    def test_returns_original_when_no_ads(self, monkeypatch):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover

        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "clean"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[]))
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp")[0] == "/ep.mp3"

    def test_returns_original_on_splice_failure(self, monkeypatch):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import ad_remover

        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "ad"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[{"start": 0.2, "end": 0.8}]))
        monkeypatch.setattr(ad_remover, "splice_audio", MagicMock(side_effect=RuntimeError("ffmpeg gone")))
        assert ad_remover.remove_ads("/ep.mp3", "v1", "/tmp")[0] == "/ep.mp3"

    def test_returns_cleaned_path_on_success(self, monkeypatch, tmp_path):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import os

        import ad_remover

        tmp_dir = str(tmp_path)
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "ad"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[{"start": 0.2, "end": 0.8}]))
        monkeypatch.setattr(ad_remover, "splice_audio", MagicMock(return_value=None))

        result, _segs, _summary = ad_remover.remove_ads("/ep.mp3", "vid123", tmp_dir)
        assert result == os.path.join(tmp_dir, "vid123_clean.mp3")

    def test_calls_splice_with_correct_args(self, monkeypatch, tmp_path):
        monkeypatch.delenv("REMOVE_ADS", raising=False)
        import os

        import ad_remover

        tmp_dir = str(tmp_path)
        ad_segs = [{"start": 10.0, "end": 30.0}]
        mock_splice = MagicMock(return_value=None)

        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "x"}])
        )
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
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "ad copy"}])
        )
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
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "clean"}])
        )
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
            monkeypatch.setattr(
                ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "ad"}])
            )
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

        segments = [{"start": i * 10.0, "end": (i + 1) * 10.0, "text": "word " * 50} for i in range(20)]
        chunks = ad_remover._split_segments_into_chunks(segments, max_chars=2000, overlap_secs=30)
        assert len(chunks) > 1
        # All segments should be covered
        all_starts = {s["start"] for chunk in chunks for s in chunk}
        expected_starts = {s["start"] for s in segments}
        assert expected_starts.issubset(all_starts)

    def test_overlap_between_chunks(self):
        """Adjacent chunks overlap by at least overlap_secs."""
        import ad_remover

        segments = [{"start": i * 10.0, "end": (i + 1) * 10.0 - 1, "text": "x " * 30} for i in range(50)]
        chunks = ad_remover._split_segments_into_chunks(segments, max_chars=2000, overlap_secs=30)
        assert len(chunks) > 1
        for i in range(len(chunks) - 1):
            end_of_current = chunks[i][-1]["end"]
            start_of_next = chunks[i + 1][0]["start"]
            # Next chunk should start before or near where current ends
            assert start_of_next < end_of_current + 5, (
                f"Chunk {i + 1} ends at {end_of_current}, chunk {i + 2} starts at {start_of_next}"
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
        segments = [{"start": i * 2.0, "end": (i + 1) * 2.0, "text": "y" * 500} for i in range(20)]
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
            "```json\n"
            '[{"start": 175.8, "end": 310.5}, {"start": 784.8, "end": 845.4}]\n'
            "```"
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
        """Patch bedrock to return different responses for each chunk call.

        Strips the leading '[' from each response to simulate assistant prefill
        (the code prepends '[' to whatever the model returns).
        """
        bc = MagicMock()
        bc.converse.side_effect = [
            {"output": {"message": {"content": [{"text": r[1:] if r.startswith("[") else r}]}}} for r in responses
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
        responses = ["[]"] * len(chunks)
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
        import importlib

        import ad_remover

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
        stderr = "[silencedetect] silence_start: 10.0\n[silencedetect] silence_end: BAD | silence_duration: also_bad\n"
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
        import importlib

        import ad_remover

        importlib.reload(ad_remover)
        with patch("ad_remover.detect_silence") as mock_silence:
            result = ad_remover.snap_ad_boundaries([], "/fake.mp3")
        assert result == []
        mock_silence.assert_not_called()

    def test_detect_silence_exception_returns_original_segments(self):
        """If detect_silence raises, original segments are returned unchanged (lines 171-173)."""
        import importlib

        import ad_remover

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
        import importlib

        import ad_remover

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
        import mutagen.mp3 as _mut

        import ad_remover

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
        import importlib

        import ad_remover

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
        assert called_model_ids[0] == "us.anthropic.claude-haiku-4-5-20251015-v1:0", (
            "Detection call must use BEDROCK_DETECT_MODEL_ID (haiku)"
        )
        assert called_model_ids[1] == "us.anthropic.claude-sonnet-4-6", (
            "Verification call must use BEDROCK_MODEL_ID (sonnet-4-6), not the detect model"
        )
        assert len(result) == 1, "Confirmed ad should be in results"

    def test_verify_threshold_zero_verifies_all_segments(self, monkeypatch):
        """AD_VERIFY_THRESHOLD_SECS=0 should verify every segment (as documented).

        Previously the outer guard verify_threshold > 0 incorrectly skipped all
        verification when threshold=0. Now >= 0 allows the inner loop to run and
        every segment (duration >= 0) gets a second-pass call.
        """
        import importlib

        import ad_remover

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
        from botocore.exceptions import ClientError

        import ad_remover

        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey", "Message": ""}}, "GetObject")
        result = ad_remover._load_transcript_cache(mock_s3, "my-bucket", "ep123")
        assert result is None

    def test_load_returns_segments_on_cache_hit(self, monkeypatch):
        """Cache HIT returns the stored segment list."""
        import json

        import ad_remover

        segments = [{"start": 0.0, "end": 5.0, "text": "hello"}]
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps(segments).encode())}
        result = ad_remover._load_transcript_cache(mock_s3, "my-bucket", "ep123")
        assert result == segments

    def test_load_returns_none_on_corrupt_cache(self, monkeypatch):
        """Non-list cache content returns None gracefully."""
        import json

        import ad_remover

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps({"not": "a list"}).encode())}
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
        import json

        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")

        cached = [{"start": 5.0, "end": 10.0, "text": "sponsored by"}]

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps(cached).encode())}
        mock_transcribe = MagicMock()

        def fake_client(service, **kw):
            return mock_s3 if service == "s3" else mock_transcribe

        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=fake_client))

        result = ad_remover.transcribe_audio("/fake.mp3", "ep_cached")
        assert result == cached
        mock_transcribe.start_transcription_job.assert_not_called()

    def test_eval_jobs_skip_cache(self, monkeypatch):
        """eval- prefixed video_ids bypass cache (evaluator re-transcribes cleaned file)."""
        import json

        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")

        # S3 would return a hit if consulted — but it should NOT be consulted
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps([]).encode())}

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
        import json

        import ad_remover

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
            {"start": 94.0, "end": 97.0},  # end at 97.0 — 3s before target
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
            {"start": 94.0, "end": 97.0},  # end=97.0 — 3s before start at 100
            {"start": 103.0, "end": 106.0},  # start=103.0 — 3s after start at 100
        ]
        monkeypatch.setattr(ad_remover, "detect_silence", lambda *a, **kw: silences)

        result = ad_remover.snap_ad_boundaries([{"start": 100.0, "end": 200.0}], "/fake.mp3")
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

        result = ad_remover.snap_ad_boundaries([{"start": 10.0, "end": 200.0}], "/fake.mp3")
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
        from botocore.exceptions import ClientError

        import ad_remover

        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey", "Message": ""}}, "GetObject")
        result = ad_remover._load_ad_segments_cache(mock_s3, "bucket", "ep1")
        assert result is None

    def test_load_returns_cached_segments_on_hit(self):
        """Cache HIT returns the stored ad_segments list."""
        import json

        import ad_remover

        expected = [{"start": 10.0, "end": 50.0}]
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps(expected).encode())}
        result = ad_remover._load_ad_segments_cache(mock_s3, "bucket", "ep1")
        assert result == expected

    def test_save_writes_correct_key(self):
        """_save_ad_segments_cache stores data at the expected S3 key."""
        import json

        import ad_remover

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
        import json

        import ad_remover

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
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", lambda *a: (_ for _ in ()).throw(AssertionError("should not transcribe"))
        )

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
        resp.__getitem__ = lambda s, k: {"output": {"message": {"content": [{"text": payload}]}}}[k]
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
                    {
                        "type": "pronunciation",
                        "start_time": "1.0",
                        "end_time": "2.0",
                        "alternatives": [{"content": "Hello"}],
                    },
                ]
            }
        }
        import json

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
        import urllib.request as ur

        import ad_remover

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
        import json
        import urllib.request as ur

        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("AD_TRANSCRIBE_WINDOWS", "0:60,3540:3600")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "false")

        fake_mp3 = tmp_path / "ep.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" * 100)

        # First window returns word at 5s; second at 1s (offset 3540 → 3541)
        def make_transcript(word, start, end):
            return json.dumps(
                {
                    "results": {
                        "items": [
                            {
                                "type": "pronunciation",
                                "start_time": str(start),
                                "end_time": str(end),
                                "alternatives": [{"content": word}],
                            },
                        ]
                    }
                }
            ).encode()

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

        import ad_remover

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
        import json
        import urllib.request as ur

        import ad_remover

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


class TestDetectMusicBookends:
    """Tests for detect_music_bookends()."""

    def test_no_segments_returns_empty(self):
        from ad_remover import detect_music_bookends

        assert detect_music_bookends([], "/fake.mp3") == []

    def test_intro_detected_when_gap_exceeds_min_and_has_audio(self, monkeypatch):
        import ad_remover
        from ad_remover import detect_music_bookends

        segments = [{"start": 15.0, "end": 60.0, "text": "hello"}]
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 120.0)
        monkeypatch.setattr(ad_remover, "detect_silence", lambda p: [])

        result = detect_music_bookends(segments, "/fake.mp3", min_intro_secs=8.0, min_outro_secs=9999.0)
        assert len(result) == 1
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 15.0
        assert result[0]["label"] == "music_intro"

    def test_intro_skipped_when_gap_below_min(self, monkeypatch):
        import ad_remover
        from ad_remover import detect_music_bookends

        segments = [{"start": 3.0, "end": 60.0, "text": "hello"}]
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 120.0)
        monkeypatch.setattr(ad_remover, "detect_silence", lambda p: [])

        result = detect_music_bookends(segments, "/fake.mp3", min_intro_secs=8.0)
        # No intro (3s < 8s), no outro (120-60=60s but min_outro default 5s — would detect)
        # Actually outro would be detected. Let's just check no intro via high min_outro
        result = detect_music_bookends(segments, "/fake.mp3", min_intro_secs=8.0, min_outro_secs=9999.0)
        assert result == []

    def test_intro_skipped_when_region_is_silent(self, monkeypatch):
        import ad_remover
        from ad_remover import detect_music_bookends

        segments = [{"start": 12.0, "end": 60.0, "text": "hello"}]
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 120.0)
        # Entire intro region is silence
        monkeypatch.setattr(ad_remover, "detect_silence", lambda p: [{"start": 0.0, "end": 12.0, "duration": 12.0}])

        result = detect_music_bookends(segments, "/fake.mp3", min_intro_secs=8.0, min_outro_secs=9999.0)
        assert result == []

    def test_outro_detected_when_gap_exceeds_min_and_has_audio(self, monkeypatch):
        import ad_remover
        from ad_remover import detect_music_bookends

        segments = [{"start": 5.0, "end": 50.0, "text": "hello"}]
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 65.0)
        monkeypatch.setattr(ad_remover, "detect_silence", lambda p: [])

        result = detect_music_bookends(segments, "/fake.mp3", min_intro_secs=9999.0, min_outro_secs=5.0)
        assert len(result) == 1
        assert result[0]["start"] == 50.0
        assert result[0]["end"] == 65.0
        assert result[0]["label"] == "music_outro"

    def test_outro_skipped_when_gap_below_min(self, monkeypatch):
        import ad_remover
        from ad_remover import detect_music_bookends

        segments = [{"start": 5.0, "end": 62.0, "text": "hello"}]
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 64.0)
        monkeypatch.setattr(ad_remover, "detect_silence", lambda p: [])

        result = detect_music_bookends(segments, "/fake.mp3", min_intro_secs=9999.0, min_outro_secs=5.0)
        assert result == []  # outro is only 2s < 5s

    def test_both_intro_and_outro_detected(self, monkeypatch):
        import ad_remover
        from ad_remover import detect_music_bookends

        segments = [{"start": 10.0, "end": 55.0, "text": "hello"}]
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 70.0)
        monkeypatch.setattr(ad_remover, "detect_silence", lambda p: [])

        result = detect_music_bookends(segments, "/fake.mp3", min_intro_secs=8.0, min_outro_secs=5.0)
        assert len(result) == 2
        assert result[0]["label"] == "music_intro"
        assert result[0]["end"] == 10.0
        assert result[1]["label"] == "music_outro"
        assert result[1]["start"] == 55.0
        assert result[1]["end"] == 70.0

    def test_zero_duration_returns_empty(self, monkeypatch):
        import ad_remover
        from ad_remover import detect_music_bookends

        segments = [{"start": 10.0, "end": 55.0, "text": "hello"}]
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 0.0)
        monkeypatch.setattr(ad_remover, "detect_silence", lambda p: [])

        result = detect_music_bookends(segments, "/fake.mp3")
        assert result == []


# ---------------------------------------------------------------------------
# Coverage gap tests – uncovered branches in ad_remover.py
# ---------------------------------------------------------------------------


class TestRegionHasAudioZeroDuration:
    """Line 296: _region_has_audio returns False when region_dur <= 0."""

    def test_zero_region_skipped_by_intro_check(self, monkeypatch):
        """When first_speech == 0 no intro segment is emitted (region_dur == 0)."""
        import ad_remover

        segments = [{"start": 0.0, "end": 5.0, "text": "hi"}]
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 30.0)
        monkeypatch.setattr(ad_remover, "detect_silence", lambda p: [])

        # With first_speech=0 the intro region [0,0] has duration 0 → no intro
        result = ad_remover.detect_music_bookends(segments, "/fake.mp3", min_intro_secs=0.0, min_outro_secs=9999.0)
        # The check `first_speech >= min_intro_secs` is True (0 >= 0), but
        # _region_has_audio(0, 0) returns False → no intro appended
        assert not any(s.get("label") == "music_intro" for s in result)


class TestSummaryCacheExceptionPaths:
    """Lines 391–421: _load_summary_cache / _save_summary_cache exception branches."""

    def test_load_summary_cache_non_nosuchkey_error_is_ignored(self):
        """ClientError with code other than NoSuchKey/404 is caught and returns None."""
        from botocore.exceptions import ClientError

        import ad_remover

        s3 = MagicMock()
        err = ClientError({"Error": {"Code": "AccessDenied", "Message": "Forbidden"}}, "GetObject")
        s3.get_object.side_effect = err

        result = ad_remover._load_summary_cache(s3, "my-bucket", "vid1")
        assert result is None

    def test_load_summary_cache_generic_exception_returns_none(self):
        """Any non-ClientError exception is caught and returns None."""
        import ad_remover

        s3 = MagicMock()
        s3.get_object.side_effect = RuntimeError("network blip")

        result = ad_remover._load_summary_cache(s3, "my-bucket", "vid2")
        assert result is None

    def test_save_summary_cache_exception_is_swallowed(self):
        """Errors saving summary cache are logged but not re-raised."""
        import ad_remover

        s3 = MagicMock()
        s3.put_object.side_effect = RuntimeError("S3 write failed")

        # Should not raise
        ad_remover._save_summary_cache(s3, "my-bucket", "vid3", "Summary text")


class TestSaveTranscriptTextException:
    """Lines 437–438: _save_transcript_text exception path."""

    def test_exception_is_swallowed(self):
        """Errors writing transcript text to S3 are logged but not re-raised."""
        import ad_remover

        s3 = MagicMock()
        s3.put_object.side_effect = RuntimeError("write error")

        segments = [{"start": 0.0, "end": 5.0, "text": "hello"}]
        # Should not raise
        ad_remover._save_transcript_text(s3, "my-bucket", "vid4", segments)


class TestAdSegmentsCacheExceptionPaths:
    """Lines 463–478: _load_ad_segments_cache / _save_ad_segments_cache exception branches."""

    def test_load_ad_segments_non_nosuchkey_error_returns_none(self):
        """ClientError with unexpected code is logged and returns None."""
        from botocore.exceptions import ClientError

        import ad_remover

        s3 = MagicMock()
        err = ClientError({"Error": {"Code": "InternalError", "Message": "oops"}}, "GetObject")
        s3.get_object.side_effect = err

        result = ad_remover._load_ad_segments_cache(s3, "my-bucket", "vid5")
        assert result is None

    def test_load_ad_segments_generic_exception_returns_none(self):
        """Generic exception during load returns None gracefully."""
        import ad_remover

        s3 = MagicMock()
        s3.get_object.side_effect = RuntimeError("connection reset")

        result = ad_remover._load_ad_segments_cache(s3, "my-bucket", "vid6")
        assert result is None

    def test_save_ad_segments_exception_is_swallowed(self):
        """Errors saving ad segment cache to S3 are swallowed."""
        import ad_remover

        s3 = MagicMock()
        s3.put_object.side_effect = RuntimeError("throttled")

        # Should not raise
        ad_remover._save_ad_segments_cache(s3, "my-bucket", "vid7", [{"start": 10.0, "end": 30.0}])


class TestParseTranscribeWindowsEdgeCases:
    """Lines 532, 545, 557–558: additional _parse_transcribe_windows branches."""

    def test_trailing_comma_produces_empty_part_skipped(self):
        """A trailing comma produces an empty part that is silently skipped (line 532)."""
        from ad_remover import _parse_transcribe_windows

        windows = _parse_transcribe_windows("0:300,", 3600.0)
        assert windows == [(0.0, 300.0)]

    def test_end_plus_token(self):
        """end+N is clamped to total duration (line 545)."""
        from ad_remover import _parse_transcribe_windows

        windows = _parse_transcribe_windows("0:end+100", 600.0)
        # end+100 → min(600, 600+100) = 600
        assert windows == [(0.0, 600.0)]

    def test_unparseable_value_raises_valueerror_skipped(self):
        """Non-numeric value raises ValueError which is caught (lines 557–558)."""
        from ad_remover import _parse_transcribe_windows

        windows = _parse_transcribe_windows("abc:xyz,0:60", 600.0)
        # abc:xyz raises ValueError → skipped; 0:60 is valid
        assert windows == [(0.0, 60.0)]


class TestWindowedTranscriptionTimeout:
    """Line 686: windowed transcription job timeout raises RuntimeError."""

    def test_windowed_job_timeout_raises(self, monkeypatch, tmp_path, mock_sleep):
        """When the windowed Transcribe job never completes, a RuntimeError is raised."""
        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("AD_TRANSCRIBE_WINDOWS", "0:300")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "false")
        # Force very low max wait so the loop exits quickly
        monkeypatch.setenv("TRANSCRIBE_MAX_WAIT", "1")
        monkeypatch.setenv("TRANSCRIBE_POLL_INTERVAL", "1")

        fake_mp3 = tmp_path / "ep.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" * 100)

        tc = MagicMock()
        # Always returns IN_PROGRESS so the loop exhausts max_wait
        tc.get_transcription_job.return_value = {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "IN_PROGRESS",
                "Transcript": {},
            }
        }
        s3 = MagicMock()

        def fake_client(service, **kw):
            return s3 if service == "s3" else tc

        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=fake_client))
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 3600.0)
        monkeypatch.setattr(ad_remover, "_extract_audio_window", lambda *a: None)

        with pytest.raises(RuntimeError, match="timed out"):
            ad_remover.transcribe_audio(str(fake_mp3), "vid_timeout_window")


class TestWindowedTranscriptionCacheSave:
    """Lines 718–719: windowed transcription saves cache after completion."""

    def test_windowed_transcription_saves_cache(self, monkeypatch, tmp_path, mock_sleep):
        """After windowed transcription completes, transcript cache is saved to S3."""
        import urllib.request as ur

        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("AD_TRANSCRIBE_WINDOWS", "0:60")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")

        fake_mp3 = tmp_path / "ep.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" * 100)

        transcript_data = {
            "results": {
                "items": [
                    {
                        "type": "pronunciation",
                        "start_time": "1.0",
                        "end_time": "2.0",
                        "alternatives": [{"content": "Hello"}],
                    },
                ]
            }
        }
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
        # No cached transcript (cache miss)
        from botocore.exceptions import ClientError

        s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject")

        def fake_client(service, **kw):
            return s3 if service == "s3" else tc

        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=fake_client))
        monkeypatch.setattr(ad_remover, "_get_audio_duration", lambda p: 3600.0)
        monkeypatch.setattr(ad_remover, "_extract_audio_window", lambda *a: None)
        monkeypatch.setattr(ur, "urlopen", MagicMock(return_value=fake_resp))

        ad_remover.transcribe_audio(str(fake_mp3), "vid_cache_save")

        # put_object should have been called to save the transcript cache
        assert s3.put_object.called


class TestNarrowOversizedSegmentEmptyAndException:
    """Lines 1094, 1100–1105: _narrow_oversized_segment edge cases."""

    def _make_bedrock_empty(self):
        """Bedrock responds with empty array (no ads in oversized segment)."""
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": [{"text": "[]"}]}}}
        return client

    def test_empty_narrowed_result_logged(self):
        """When Bedrock returns [] for narrowing, the function returns [] (line 1094)."""
        import ad_remover

        bedrock = self._make_bedrock_empty()
        segments = [{"start": 0.0, "end": 400.0, "text": "lots of content here"}]
        segment = {"start": 0.0, "end": 400.0}
        result = ad_remover._narrow_oversized_segment(segment, segments, bedrock, "model-id")
        assert result == []

    def test_exception_during_narrowing_returns_empty(self):
        """When Bedrock raises an exception during narrowing, [] is returned (lines 1100–1105)."""
        import ad_remover

        client = MagicMock()
        client.converse.side_effect = RuntimeError("Bedrock unavailable")
        segments = [{"start": 0.0, "end": 400.0, "text": "lots of content"}]
        segment = {"start": 0.0, "end": 400.0}
        result = ad_remover._narrow_oversized_segment(segment, segments, client, "model-id")
        assert result == []


class TestDetectAdsNarrowedValidSubsegment:
    """Line 1214: detect_ads adds narrowed sub-segment when within bounds."""

    def test_valid_narrowed_subsegment_is_included(self, monkeypatch):
        """An oversized segment is narrowed; valid sub-segments are included in output."""
        import ad_remover

        # Create segments that will produce an oversized ad detection
        segments = [{"start": float(i * 5), "end": float(i * 5 + 4), "text": f"ad content {i}"} for i in range(10)]

        call_count = [0]

        def fake_converse(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: return an oversized ad segment (>300s default max)
                return {"output": {"message": {"content": [{"text": '[{"start": 0.0, "end": 350.0}]'}]}}}
            else:
                # Narrowing call: return a valid sub-segment
                return {"output": {"message": {"content": [{"text": '[{"start": 5.0, "end": 25.0}]'}]}}}

        bedrock = MagicMock()
        bedrock.converse.side_effect = fake_converse

        with patch("ad_remover.boto3.client", return_value=bedrock):
            result = ad_remover.detect_ads(segments)

        # The narrowed sub-segment [5.0, 25.0] (20s) is within [1, 300] → included
        assert any(s["start"] == pytest.approx(5.0) and s["end"] == pytest.approx(25.0) for s in result)


class TestSpliceAudioMutagenFallback:
    """Line 1458: splice_audio uses mutagen when both ffprobe attempts fail."""

    def test_mutagen_fallback_used_when_ffprobe_fails(self, monkeypatch, tmp_path):
        """When ffprobe CalledProcessError + generic exception, mutagen provides duration."""
        import ad_remover

        fake_mp3 = tmp_path / "ep.mp3"
        fake_mp3.write_bytes(b"\xff\xfb" * 10000)

        monkeypatch.setattr(os.path, "getsize", lambda p: 5_000_000)

        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if cmd[0] == "ffprobe":
                if call_count[0] == 1:
                    # First ffprobe attempt (normal) → CalledProcessError
                    raise subprocess.CalledProcessError(1, cmd)
                else:
                    # Second ffprobe attempt (-f mp3) → generic OSError
                    raise OSError("ffprobe crashed")
            # ffmpeg call succeeds
            return MagicMock(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Mock mutagen to return a duration
        import mutagen.mp3 as _mut

        mock_mp3 = MagicMock()
        mock_mp3.info.length = 300.0
        monkeypatch.setattr(_mut, "MP3", MagicMock(return_value=mock_mp3))

        # Should succeed via mutagen fallback (ffmpeg call is also mocked to succeed)
        ad_remover.splice_audio(str(fake_mp3), [{"start": 10.0, "end": 20.0}], str(tmp_path / "out.mp3"))


class TestRemoveAdsCachedPaths:
    """Lines 1604–1635: remove_ads cached ad-segment paths."""

    def _setup_cached_env(self, monkeypatch, tmp_path, cached_ads, trim_intro=False, trim_outro=False):
        """Helper: configure monkeypatches for cached-ads path."""
        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")
        if trim_intro:
            monkeypatch.setenv("TRIM_MUSIC_INTRO", "true")
        if trim_outro:
            monkeypatch.setenv("TRIM_MUSIC_OUTRO", "true")

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = MagicMock()
        from botocore.exceptions import ClientError

        def fake_get(Bucket, Key):
            if "_ads.json" in Key:
                return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(cached_ads).encode()))}
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": ""}}, "GetObject")

        s3.get_object.side_effect = fake_get
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        return src, s3

    def test_cached_empty_ads_returns_original(self, monkeypatch, tmp_path):
        """When cached ad-segments is [], original file is returned (lines 1616–1618)."""
        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = MagicMock()
        from botocore.exceptions import ClientError

        def fake_get(Bucket, Key):
            if "_ads.json" in Key:
                return {"Body": MagicMock(read=MagicMock(return_value=json.dumps([]).encode()))}
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        s3.get_object.side_effect = fake_get
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", lambda *a: (_ for _ in ()).throw(AssertionError("should not transcribe"))
        )

        path, segs, summary = ad_remover.remove_ads(str(src), "ep_empty_cached", str(tmp_path))
        assert path == str(src)
        assert segs == []

    def test_cached_ads_dry_run(self, monkeypatch, tmp_path):
        """Cached ads + DRY_RUN returns original path without splicing (lines 1621–1627)."""
        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")
        monkeypatch.setenv("REMOVE_ADS_DRY_RUN", "true")

        cached_ads = [{"start": 10.0, "end": 40.0}]
        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = MagicMock()
        from botocore.exceptions import ClientError

        def fake_get(Bucket, Key):
            if "_ads.json" in Key:
                return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(cached_ads).encode()))}
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        s3.get_object.side_effect = fake_get
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        monkeypatch.setattr(ad_remover, "snap_ad_boundaries", lambda segs, path, **kw: segs)

        path, segs, summary = ad_remover.remove_ads(str(src), "ep_dry_cached", str(tmp_path))
        assert path == str(src)
        assert segs == cached_ads

    def test_cached_ads_splice_failure_returns_original(self, monkeypatch, tmp_path):
        """Splice failure on cached path logs error and returns original (lines 1631–1633)."""
        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")
        monkeypatch.setenv("REMOVE_ADS_DRY_RUN", "false")

        cached_ads = [{"start": 10.0, "end": 40.0}]
        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = MagicMock()
        from botocore.exceptions import ClientError

        def fake_get(Bucket, Key):
            if "_ads.json" in Key:
                return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(cached_ads).encode()))}
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        s3.get_object.side_effect = fake_get
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        monkeypatch.setattr(ad_remover, "snap_ad_boundaries", lambda segs, path, **kw: segs)
        monkeypatch.setattr(ad_remover, "splice_audio", MagicMock(side_effect=RuntimeError("splice failed")))

        path, segs, summary = ad_remover.remove_ads(str(src), "ep_splice_fail_cached", str(tmp_path))
        assert path == str(src)
        assert segs == cached_ads

    def test_cached_ads_with_music_bookend_detection(self, monkeypatch, tmp_path):
        """Cached path with TRIM_MUSIC_INTRO reads transcript cache for music detection (lines 1603–1613)."""
        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")
        monkeypatch.setenv("TRIM_MUSIC_INTRO", "true")
        monkeypatch.setenv("REMOVE_ADS_DRY_RUN", "false")

        cached_ads = [{"start": 10.0, "end": 40.0}]
        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        transcript_segments = [{"start": 5.0, "end": 100.0, "text": "hello"}]

        s3 = MagicMock()
        from botocore.exceptions import ClientError

        def fake_get(Bucket, Key):
            if "_ads.json" in Key:
                return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(cached_ads).encode()))}
            if Key.endswith(".json"):
                return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(transcript_segments).encode()))}
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        s3.get_object.side_effect = fake_get

        spliced = []

        def fake_splice(mp3_path, segs, out_path):
            spliced.append(segs)
            import pathlib

            pathlib.Path(out_path).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        monkeypatch.setattr(ad_remover, "detect_music_bookends", MagicMock(return_value=[]))
        monkeypatch.setattr(ad_remover, "snap_ad_boundaries", lambda segs, path, **kw: segs)
        monkeypatch.setattr(ad_remover, "splice_audio", fake_splice)

        path, segs, summary = ad_remover.remove_ads(str(src), "ep_music_cached", str(tmp_path))
        assert spliced, "splice_audio should be called"

    def test_cached_ads_with_music_bookend_exception(self, monkeypatch, tmp_path):
        """Music detection exception on cached path is swallowed (line 1613)."""
        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")
        monkeypatch.setenv("TRIM_MUSIC_INTRO", "true")
        monkeypatch.setenv("REMOVE_ADS_DRY_RUN", "false")

        cached_ads = [{"start": 10.0, "end": 40.0}]
        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        transcript_segments = [{"start": 5.0, "end": 100.0, "text": "hello"}]

        s3 = MagicMock()
        from botocore.exceptions import ClientError

        def fake_get(Bucket, Key):
            if "_ads.json" in Key:
                return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(cached_ads).encode()))}
            if Key.endswith(".json"):
                return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(transcript_segments).encode()))}
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        s3.get_object.side_effect = fake_get

        def fake_splice(mp3_path, segs, out_path):
            import pathlib

            pathlib.Path(out_path).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        monkeypatch.setattr(
            ad_remover, "detect_music_bookends", MagicMock(side_effect=RuntimeError("detection failed"))
        )
        monkeypatch.setattr(ad_remover, "snap_ad_boundaries", lambda segs, path, **kw: segs)
        monkeypatch.setattr(ad_remover, "splice_audio", fake_splice)

        # Should not raise — music detection exception is swallowed
        path, segs, summary = ad_remover.remove_ads(str(src), "ep_music_exc_cached", str(tmp_path))
        assert segs == cached_ads


class TestRemoveAdsSaveAdSegmentsCache:
    """Lines 1653–1655: remove_ads saves ad segments cache after fresh detection."""

    def test_ad_segments_saved_to_cache_after_detection(self, monkeypatch, tmp_path):
        """After fresh Bedrock detection, ad segments are saved to S3 cache."""
        import ad_remover

        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = MagicMock()
        from botocore.exceptions import ClientError

        # No cached ads
        s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        detected_ads = [{"start": 10.0, "end": 40.0}]
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "hi"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=detected_ads))
        monkeypatch.setattr(ad_remover, "snap_ad_boundaries", lambda segs, path, **kw: segs)

        def fake_splice(mp3_path, segs, out_path):
            import pathlib

            pathlib.Path(out_path).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "splice_audio", fake_splice)

        ad_remover.remove_ads(str(src), "ep_cache_save", str(tmp_path))

        # put_object should have been called to save ad segments cache
        assert s3.put_object.called


class TestRemoveAdsMusicBookendOnFreshPath:
    """Lines 1660–1667: music bookend detection on fresh transcription path."""

    def test_music_bookend_detection_exception_is_swallowed(self, monkeypatch, tmp_path):
        """Music detection exception on fresh path is logged and doesn't abort (lines 1666–1667)."""
        import ad_remover

        monkeypatch.setenv("TRIM_MUSIC_INTRO", "true")
        monkeypatch.delenv("S3_BUCKET", raising=False)

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        detected_ads = [{"start": 10.0, "end": 40.0}]
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 5.0, "end": 10.0, "text": "hi"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=detected_ads))
        monkeypatch.setattr(
            ad_remover, "detect_music_bookends", MagicMock(side_effect=RuntimeError("music detect crash"))
        )
        monkeypatch.setattr(ad_remover, "snap_ad_boundaries", lambda segs, path, **kw: segs)

        def fake_splice(mp3_path, segs, out_path):
            import pathlib

            pathlib.Path(out_path).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "splice_audio", fake_splice)

        # Should not raise — music detection exception is swallowed
        path, segs, summary = ad_remover.remove_ads(str(src), "ep_music_exc_fresh", str(tmp_path))
        assert segs == detected_ads


class TestRemoveAdsGenerateSummaryPaths:
    """Lines 1677–1689, 1713–1726: GENERATE_SUMMARIES paths in remove_ads."""

    def _make_s3(self):
        from botocore.exceptions import ClientError

        s3 = MagicMock()
        # summary cache miss
        s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return s3

    def test_summary_generated_when_no_ads_found(self, monkeypatch, tmp_path):
        """When no ads detected and GENERATE_SUMMARIES=true, summary is generated (lines 1677–1690)."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.delenv("TRANSCRIBE_CACHE_ENABLED", raising=False)

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = self._make_s3()
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "hi"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[]))

        import sys

        fake_summary_mod = MagicMock()
        fake_summary_mod.generate_episode_summary.return_value = "Great episode!"
        monkeypatch.setitem(sys.modules, "summary_generator", fake_summary_mod)

        path, segs, summary = ad_remover.remove_ads(str(src), "ep_sum_no_ads", str(tmp_path))
        assert summary == "Great episode!"
        assert path == str(src)

    def test_summary_generation_exception_is_swallowed_no_ads(self, monkeypatch, tmp_path):
        """Summary generation exception when no ads is swallowed (line 1689)."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = self._make_s3()
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "hi"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[]))

        import sys

        fake_summary_mod = MagicMock()
        fake_summary_mod.generate_episode_summary.side_effect = RuntimeError("summary failed")
        monkeypatch.setitem(sys.modules, "summary_generator", fake_summary_mod)

        path, segs, summary = ad_remover.remove_ads(str(src), "ep_sum_exc_no_ads", str(tmp_path))
        assert summary == ""

    def test_summary_generated_after_splice(self, monkeypatch, tmp_path):
        """After successful splice with GENERATE_SUMMARIES=true, summary is returned (lines 1713–1726)."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.delenv("TRANSCRIBE_CACHE_ENABLED", raising=False)

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = self._make_s3()
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "hi"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[{"start": 10.0, "end": 40.0}]))
        monkeypatch.setattr(ad_remover, "snap_ad_boundaries", lambda segs, path, **kw: segs)

        def fake_splice(mp3_path, segs, out_path):
            import pathlib

            pathlib.Path(out_path).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "splice_audio", fake_splice)

        import sys

        fake_summary_mod = MagicMock()
        fake_summary_mod.generate_episode_summary.return_value = "Spliced episode summary!"
        monkeypatch.setitem(sys.modules, "summary_generator", fake_summary_mod)

        path, segs, summary = ad_remover.remove_ads(str(src), "ep_sum_after_splice", str(tmp_path))
        assert summary == "Spliced episode summary!"

    def test_summary_generation_exception_is_swallowed_after_splice(self, monkeypatch, tmp_path):
        """Summary generation exception after splice is swallowed (line 1726)."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = self._make_s3()
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "hi"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[{"start": 10.0, "end": 40.0}]))
        monkeypatch.setattr(ad_remover, "snap_ad_boundaries", lambda segs, path, **kw: segs)

        def fake_splice(mp3_path, segs, out_path):
            import pathlib

            pathlib.Path(out_path).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "splice_audio", fake_splice)

        import sys

        fake_summary_mod = MagicMock()
        fake_summary_mod.generate_episode_summary.side_effect = RuntimeError("summariser crashed")
        monkeypatch.setitem(sys.modules, "summary_generator", fake_summary_mod)

        path, segs, summary = ad_remover.remove_ads(str(src), "ep_sum_exc_splice", str(tmp_path))
        assert summary == ""


class TestPromptImprovements:
    """Tests for Bedrock prompt quality improvements (Changes 1-8)."""

    def test_no_boundary_padding_rule(self):
        """Change 2: Rule 2 about extending start/end times is removed."""
        import ad_remover

        assert "Extend each segment" not in ad_remover._AD_DETECTION_PROMPT

    def test_negative_examples_section_present(self):
        """Change 5: 'What is NOT an ad' section exists with subscribe example."""
        import ad_remover

        assert "What is NOT an ad" in ad_remover._AD_DETECTION_PROMPT
        assert "subscribe" in ad_remover._AD_DETECTION_PROMPT

    def test_hints_section_before_rules(self):
        """Change 8: {hints_section} appears before '## Rules' in the prompt."""
        import ad_remover

        hints_pos = ad_remover._AD_DETECTION_PROMPT.index("{hints_section}")
        rules_pos = ad_remover._AD_DETECTION_PROMPT.index("## Rules")
        assert hints_pos < rules_pos

    def test_detection_prefill_in_messages(self, monkeypatch):
        """Change 3: detect_ads sends assistant prefill '[' in messages."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        captured_kwargs = []

        def fake_converse(**kwargs):
            captured_kwargs.append(kwargs)
            return {"output": {"message": {"content": [{"text": "]"}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse
        monkeypatch.setattr("boto3.client", lambda svc, **kw: mock_client)

        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            ad_remover.detect_ads([{"start": 0.0, "end": 5.0, "text": "hi"}])

        msgs = captured_kwargs[0]["messages"]
        assert len(msgs) == 2
        assert msgs[1] == {"role": "assistant", "content": [{"text": "["}]}

    def test_detection_system_prompt_present(self, monkeypatch):
        """Change 4: detect_ads sends a system prompt."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        captured_kwargs = []

        def fake_converse(**kwargs):
            captured_kwargs.append(kwargs)
            return {"output": {"message": {"content": [{"text": "]"}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse
        monkeypatch.setattr("boto3.client", lambda svc, **kw: mock_client)

        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            ad_remover.detect_ads([{"start": 0.0, "end": 5.0, "text": "hi"}])

        assert "system" in captured_kwargs[0]
        assert len(captured_kwargs[0]["system"]) > 0
        assert "text" in captured_kwargs[0]["system"][0]

    def test_narrowing_prefill_in_messages(self, monkeypatch):
        """Change 3: _narrow_oversized_segment sends assistant prefill '[' in messages."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        captured_kwargs = []

        def fake_converse(**kwargs):
            captured_kwargs.append(kwargs)
            return {"output": {"message": {"content": [{"text": "]"}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse

        seg = {"start": 100.0, "end": 400.0}
        segs = [
            {"start": 100.0, "end": 200.0, "text": "some text"},
            {"start": 200.0, "end": 400.0, "text": "more text"},
        ]

        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            ad_remover._narrow_oversized_segment(seg, segs, mock_client, "test-model")

        msgs = captured_kwargs[0]["messages"]
        assert len(msgs) == 2
        assert msgs[1] == {"role": "assistant", "content": [{"text": "["}]}

    def test_narrowing_system_prompt_present(self, monkeypatch):
        """Change 4: _narrow_oversized_segment sends a system prompt."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        captured_kwargs = []

        def fake_converse(**kwargs):
            captured_kwargs.append(kwargs)
            return {"output": {"message": {"content": [{"text": "]"}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse

        seg = {"start": 100.0, "end": 400.0}
        segs = [{"start": 100.0, "end": 400.0, "text": "content"}]

        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            ad_remover._narrow_oversized_segment(seg, segs, mock_client, "test-model")

        assert "system" in captured_kwargs[0]
        assert len(captured_kwargs[0]["system"]) > 0

    def test_verification_prefill_in_messages(self, monkeypatch):
        """Change 3: _verify_ad_segment sends assistant prefill '{' in messages."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        captured_kwargs = []

        def fake_converse(**kwargs):
            captured_kwargs.append(kwargs)
            return {"output": {"message": {"content": [{"text": '"is_ad": true, "reason": "test"}'}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse

        seg = {"start": 100.0, "end": 250.0}
        segs = [{"start": 100.0, "end": 250.0, "text": "sponsor content"}]

        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            ad_remover._verify_ad_segment(seg, segs, mock_client, "test-model")

        msgs = captured_kwargs[0]["messages"]
        assert len(msgs) == 2
        assert msgs[1] == {"role": "assistant", "content": [{"text": "{"}]}

    def test_verification_system_prompt_present(self, monkeypatch):
        """Change 4: _verify_ad_segment sends a system prompt."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        captured_kwargs = []

        def fake_converse(**kwargs):
            captured_kwargs.append(kwargs)
            return {"output": {"message": {"content": [{"text": '"is_ad": true, "reason": "x"}'}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse

        seg = {"start": 100.0, "end": 250.0}
        segs = [{"start": 100.0, "end": 250.0, "text": "sponsor content"}]

        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            ad_remover._verify_ad_segment(seg, segs, mock_client, "test-model")

        assert "system" in captured_kwargs[0]
        assert len(captured_kwargs[0]["system"]) > 0

    def test_narrow_timestamp_format_in_prompt(self, monkeypatch):
        """Change 1: _narrow_oversized_segment sends timestamped transcript lines."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        captured_kwargs = []

        def fake_converse(**kwargs):
            captured_kwargs.append(kwargs)
            return {"output": {"message": {"content": [{"text": "]"}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse

        seg = {"start": 100.0, "end": 200.0}
        segs = [{"start": 105.5, "end": 150.3, "text": "hello world"}]

        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            ad_remover._narrow_oversized_segment(seg, segs, mock_client, "test-model")

        prompt_text = captured_kwargs[0]["messages"][0]["content"][0]["text"]
        assert "[105.5 - 150.3]" in prompt_text
        assert "hello world" in prompt_text

    def test_verify_timestamp_format_in_prompt(self, monkeypatch):
        """Change 6: _verify_ad_segment sends timestamped transcript lines."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        captured_kwargs = []

        def fake_converse(**kwargs):
            captured_kwargs.append(kwargs)
            return {"output": {"message": {"content": [{"text": '"is_ad": true, "reason": "x"}'}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse

        seg = {"start": 100.0, "end": 250.0}
        segs = [{"start": 102.5, "end": 180.7, "text": "use code SAVE20"}]

        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            ad_remover._verify_ad_segment(seg, segs, mock_client, "test-model")

        prompt_text = captured_kwargs[0]["messages"][0]["content"][0]["text"]
        assert "[102.5 - 180.7]" in prompt_text
        assert "use code SAVE20" in prompt_text

    def test_episode_context_in_detection_prompt(self, monkeypatch):
        """Change 7: detect_ads adds episode context header to each chunk."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        captured_kwargs = []

        def fake_converse(**kwargs):
            captured_kwargs.append(kwargs)
            return {"output": {"message": {"content": [{"text": "]"}]}}}

        mock_client = MagicMock()
        mock_client.converse.side_effect = fake_converse
        monkeypatch.setattr("boto3.client", lambda svc, **kw: mock_client)

        segs = [
            {"start": 0.0, "end": 1800.0, "text": "first half"},
            {"start": 1800.0, "end": 3600.0, "text": "second half"},
        ]

        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            ad_remover.detect_ads(segs)

        prompt_text = captured_kwargs[0]["messages"][0]["content"][0]["text"]
        assert "Total episode duration:" in prompt_text
        assert "chunk 1 of" in prompt_text


class TestAdSegmentsCacheSave:
    """Tests for _save_ad_segments_cache."""

    def test_save_ad_segments_cache_empty_list_not_saved(self):
        """_save_ad_segments_cache must NOT write to S3 when ad_segments is empty."""
        from ad_remover import _save_ad_segments_cache

        mock_s3 = MagicMock()
        _save_ad_segments_cache(mock_s3, "my-bucket", "vid123", [])
        mock_s3.put_object.assert_not_called()

    def test_save_ad_segments_cache_non_empty_list_saved(self):
        """_save_ad_segments_cache must write to S3 when ad_segments is non-empty."""
        from ad_remover import _save_ad_segments_cache

        mock_s3 = MagicMock()
        _save_ad_segments_cache(mock_s3, "my-bucket", "vid123", [{"start": 10.0, "end": 45.0}])
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "my-bucket"
        assert b'"start"' in call_kwargs["Body"]


# ---------------------------------------------------------------------------
# _generate_summary — duration guard (SUMMARY_MAX_DURATION_SECS)
# ---------------------------------------------------------------------------


class TestGenerateSummaryDurationGuard:
    """Tests for the SUMMARY_MAX_DURATION_SECS duration guard in _generate_summary."""

    def _make_s3_cache_miss(self):
        from botocore.exceptions import ClientError

        s3 = MagicMock()
        s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return s3

    def _inject_summary_module(self, monkeypatch, return_value: str = "a summary"):
        mod = MagicMock()
        mod.generate_episode_summary.return_value = return_value
        monkeypatch.setitem(sys.modules, "summary_generator", mod)
        return mod

    def test_skips_when_duration_exceeds_max(self, monkeypatch):
        """Summary is skipped when duration_secs > SUMMARY_MAX_DURATION_SECS."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("SUMMARY_MAX_DURATION_SECS", "1800")
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=self._make_s3_cache_miss())))
        mod = self._inject_summary_module(monkeypatch)
        result = ad_remover._generate_summary(
            [{"start": 0.0, "end": 5.0, "text": "hi"}],
            "ep-long",
            episode_title="Long Episode",
            duration_secs=1801.0,
        )
        assert result == ""
        mod.generate_episode_summary.assert_not_called()

    def test_runs_when_duration_at_max(self, monkeypatch):
        """Summary is generated when duration_secs == SUMMARY_MAX_DURATION_SECS."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("SUMMARY_MAX_DURATION_SECS", "1800")
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=self._make_s3_cache_miss())))
        self._inject_summary_module(monkeypatch, "short summary")
        result = ad_remover._generate_summary(
            [{"start": 0.0, "end": 5.0, "text": "hi"}],
            "ep-at-limit",
            duration_secs=1800.0,
        )
        assert result == "short summary"

    def test_runs_when_duration_is_none(self, monkeypatch):
        """Guard is bypassed when duration_secs is None."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("SUMMARY_MAX_DURATION_SECS", "1800")
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=self._make_s3_cache_miss())))
        self._inject_summary_module(monkeypatch, "no duration guard")
        result = ad_remover._generate_summary(
            [{"start": 0.0, "end": 5.0, "text": "hi"}],
            "ep-no-dur",
            duration_secs=None,
        )
        assert result == "no duration guard"

    def test_guard_disabled_when_max_is_zero(self, monkeypatch):
        """SUMMARY_MAX_DURATION_SECS=0 disables the guard for any episode length."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("SUMMARY_MAX_DURATION_SECS", "0")
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=self._make_s3_cache_miss())))
        self._inject_summary_module(monkeypatch, "very long episode summary")
        result = ad_remover._generate_summary(
            [{"start": 0.0, "end": 5.0, "text": "hi"}],
            "ep-huge",
            duration_secs=18_000.0,  # 5 hours
        )
        assert result == "very long episode summary"

    def test_invalid_max_duration_uses_default_1800(self, monkeypatch):
        """Non-numeric SUMMARY_MAX_DURATION_SECS falls back to 1800s instead of crashing."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("SUMMARY_MAX_DURATION_SECS", "30min")  # invalid — not a number
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=self._make_s3_cache_miss())))
        self._inject_summary_module(monkeypatch, "summary text")

        # 29-minute episode should be summarised (within default 1800 s)
        result = ad_remover._generate_summary(
            [{"start": 0.0, "end": 5.0, "text": "hi"}],
            "ep-id",
            duration_secs=1740.0,
        )
        assert result == "summary text"

    def test_invalid_max_duration_skips_long_episodes(self, monkeypatch):
        """With invalid SUMMARY_MAX_DURATION_SECS, the 1800 s default is enforced."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("SUMMARY_MAX_DURATION_SECS", "none")  # invalid
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=self._make_s3_cache_miss())))
        mod = self._inject_summary_module(monkeypatch, "summary text")

        # 31-minute episode should be skipped (exceeds default 1800 s)
        result = ad_remover._generate_summary(
            [{"start": 0.0, "end": 5.0, "text": "hi"}],
            "ep-id",
            duration_secs=1860.0,
        )
        assert result == ""
        mod.generate_episode_summary.assert_not_called()

    def test_episode_title_forwarded_to_generator(self, monkeypatch):
        """Human-readable title (not video_id) is passed to generate_episode_summary."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=self._make_s3_cache_miss())))
        mod = self._inject_summary_module(monkeypatch, "summary")
        ad_remover._generate_summary(
            [{"start": 0.0, "end": 5.0, "text": "hi"}],
            "abc123xyz",
            episode_title="My Great Episode Title",
        )
        _, call_kwargs = mod.generate_episode_summary.call_args
        positional = mod.generate_episode_summary.call_args[0]
        assert positional[1] == "My Great Episode Title"
        assert "abc123xyz" not in positional[1]

    def test_falls_back_to_video_id_when_title_empty(self, monkeypatch):
        """video_id is used as title fallback when episode_title is empty."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=self._make_s3_cache_miss())))
        mod = self._inject_summary_module(monkeypatch, "summary")
        ad_remover._generate_summary(
            [{"start": 0.0, "end": 5.0, "text": "hi"}],
            "the-real-video-id",
            episode_title="",
        )
        positional = mod.generate_episode_summary.call_args[0]
        assert positional[1] == "the-real-video-id"


# ---------------------------------------------------------------------------
# remove_ads — episode_title and duration_secs forwarded to _generate_summary
# ---------------------------------------------------------------------------


class TestRemoveAdsSummaryParams:
    """Verify remove_ads passes episode_title and duration_secs to _generate_summary."""

    def test_params_forwarded_no_ads(self, monkeypatch, tmp_path):
        """episode_title and duration_secs reach _generate_summary (no-ads path)."""
        import ad_remover

        monkeypatch.delenv("TRANSCRIBE_CACHE_ENABLED", raising=False)
        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        captured: dict = {}

        def fake_generate(segs, vid, episode_title="", duration_secs=None):
            captured["episode_title"] = episode_title
            captured["duration_secs"] = duration_secs
            return ""

        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=MagicMock())))
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "hi"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[]))
        monkeypatch.setattr(ad_remover, "_generate_summary", fake_generate)

        ad_remover.remove_ads(
            str(src),
            "ep-fwd",
            str(tmp_path),
            episode_title="Forwarded Title",
            duration_secs=900.0,
        )
        assert captured["episode_title"] == "Forwarded Title"
        assert captured["duration_secs"] == 900.0

    def test_params_forwarded_after_splice(self, monkeypatch, tmp_path):
        """episode_title and duration_secs reach _generate_summary (post-splice path)."""
        import ad_remover

        monkeypatch.delenv("TRANSCRIBE_CACHE_ENABLED", raising=False)
        monkeypatch.setenv("AD_SNAP_TO_SILENCE", "false")
        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        captured: dict = {}

        def fake_generate(segs, vid, episode_title="", duration_secs=None):
            captured["episode_title"] = episode_title
            captured["duration_secs"] = duration_secs
            return ""

        def fake_splice(mp3, segs, out):
            import pathlib

            pathlib.Path(out).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=MagicMock())))
        monkeypatch.setattr(
            ad_remover, "transcribe_audio", MagicMock(return_value=[{"start": 0.0, "end": 5.0, "text": "hi"}])
        )
        monkeypatch.setattr(ad_remover, "detect_ads", MagicMock(return_value=[{"start": 10.0, "end": 40.0}]))
        monkeypatch.setattr(ad_remover, "splice_audio", fake_splice)
        monkeypatch.setattr(ad_remover, "_generate_summary", fake_generate)

        ad_remover.remove_ads(
            str(src),
            "ep-fwd-splice",
            str(tmp_path),
            episode_title="Spliced Episode",
            duration_secs=1500.0,
        )
        assert captured["episode_title"] == "Spliced Episode"
        assert captured["duration_secs"] == 1500.0


# ---------------------------------------------------------------------------
# Cached path — summary generation
# ---------------------------------------------------------------------------


class TestCachedPathSummaryGeneration:
    """Tests that the cached ad-segment path generates summaries correctly."""

    def _s3_with_cached_data(self, ads: list, transcript: list | None = None):
        """S3 mock routing get_object by key suffix."""
        from botocore.exceptions import ClientError

        s3 = MagicMock()
        _transcript = transcript or []

        def fake_get(Bucket, Key):
            if "_ads.json" in Key:
                return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(ads).encode()))}
            if "_summary.txt" in Key:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            if Key.endswith(".json"):  # transcript cache
                return {"Body": MagicMock(read=MagicMock(return_value=json.dumps(_transcript).encode()))}
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        s3.get_object.side_effect = fake_get
        return s3

    def test_cached_splice_success_generates_summary(self, monkeypatch, tmp_path):
        """After a successful cached-path splice, summary is generated from transcript."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")
        monkeypatch.setenv("AD_SNAP_TO_SILENCE", "false")

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = self._s3_with_cached_data(
            ads=[{"start": 10.0, "end": 40.0}],
            transcript=[{"start": 0.0, "end": 5.0, "text": "great episode"}],
        )
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))

        def fake_splice(mp3, segs, out):
            import pathlib

            pathlib.Path(out).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "splice_audio", fake_splice)

        mod = MagicMock()
        mod.generate_episode_summary.return_value = "Cached splice summary"
        monkeypatch.setitem(sys.modules, "summary_generator", mod)
        _, _, summary = ad_remover.remove_ads(
            str(src),
            "ep-cached-splice-sum",
            str(tmp_path),
            episode_title="Cached Episode",
            duration_secs=900.0,
        )
        assert summary == "Cached splice summary"

    def test_cached_splice_failure_generates_summary(self, monkeypatch, tmp_path):
        """After a cached-path splice failure, summary is still generated."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")
        monkeypatch.setenv("AD_SNAP_TO_SILENCE", "false")

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = self._s3_with_cached_data(
            ads=[{"start": 10.0, "end": 40.0}],
            transcript=[{"start": 0.0, "end": 5.0, "text": "failed splice but summary ok"}],
        )
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))
        monkeypatch.setattr(ad_remover, "splice_audio", MagicMock(side_effect=RuntimeError("splice failed")))
        monkeypatch.setattr(ad_remover, "snap_ad_boundaries", lambda segs, path, **kw: segs)

        mod = MagicMock()
        mod.generate_episode_summary.return_value = "Summary despite failure"
        monkeypatch.setitem(sys.modules, "summary_generator", mod)
        path, _, summary = ad_remover.remove_ads(
            str(src),
            "ep-cached-fail-sum",
            str(tmp_path),
            episode_title="Failing Episode",
            duration_secs=900.0,
        )
        assert path == str(src)
        assert summary == "Summary despite failure"

    def test_cached_path_duration_guard_skips_summary(self, monkeypatch, tmp_path):
        """Duration guard prevents summary on cached path when episode is too long."""
        import ad_remover

        monkeypatch.setenv("GENERATE_SUMMARIES", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")
        monkeypatch.setenv("AD_SNAP_TO_SILENCE", "false")
        monkeypatch.setenv("SUMMARY_MAX_DURATION_SECS", "1800")

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = self._s3_with_cached_data(
            ads=[{"start": 10.0, "end": 40.0}],
            transcript=[{"start": 0.0, "end": 5.0, "text": "long episode content"}],
        )
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))

        def fake_splice(mp3, segs, out):
            import pathlib

            pathlib.Path(out).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "splice_audio", fake_splice)

        mod = MagicMock()
        mod.generate_episode_summary.return_value = "Should not appear"
        monkeypatch.setitem(sys.modules, "summary_generator", mod)
        _, _, summary = ad_remover.remove_ads(
            str(src),
            "ep-cached-long",
            str(tmp_path),
            episode_title="Long Episode",
            duration_secs=7200.0,  # 2 hours > 1800s guard
        )
        assert summary == ""
        mod.generate_episode_summary.assert_not_called()

    def test_cached_path_no_summary_when_disabled(self, monkeypatch, tmp_path):
        """When GENERATE_SUMMARIES=false (default), cached path returns '' for summary."""
        import ad_remover

        monkeypatch.delenv("GENERATE_SUMMARIES", raising=False)
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("TRANSCRIBE_CACHE_ENABLED", "true")
        monkeypatch.setenv("AD_SNAP_TO_SILENCE", "false")

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 100)

        s3 = self._s3_with_cached_data(ads=[{"start": 10.0, "end": 40.0}])
        monkeypatch.setattr(ad_remover, "boto3", MagicMock(client=MagicMock(return_value=s3)))

        def fake_splice(mp3, segs, out):
            import pathlib

            pathlib.Path(out).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "splice_audio", fake_splice)

        _, _, summary = ad_remover.remove_ads(str(src), "ep-cached-no-gen", str(tmp_path))
        assert summary == ""


# ---------------------------------------------------------------------------
# concat-demuxer fallback tests
# ---------------------------------------------------------------------------


class TestSpliceConcatDemuxer:
    """Unit tests for the _splice_concat_demuxer fallback function."""

    def test_fallback_called_on_sigsegv(self, monkeypatch, tmp_path):
        """splice_audio triggers _splice_concat_demuxer on ffmpeg exit -11."""
        import subprocess

        import ad_remover

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 5000)
        out = tmp_path / "out.mp3"

        sigsegv_exc = subprocess.CalledProcessError(-11, "ffmpeg", stderr="Segmentation fault")
        fallback_called = []

        def fake_run(cmd, **kwargs):
            if "-filter_complex" in cmd:
                raise sigsegv_exc
            return subprocess.CompletedProcess(cmd, 0, stdout="120.0", stderr="")

        monkeypatch.setattr(ad_remover.subprocess, "run", fake_run)

        def fake_fallback(mp3_path, keep, output_path):
            fallback_called.append((mp3_path, keep, output_path))
            import pathlib

            pathlib.Path(output_path).write_bytes(b"cleaned")

        monkeypatch.setattr(ad_remover, "_splice_concat_demuxer", fake_fallback)

        ad_remover.splice_audio(
            str(src),
            [{"start": 30.0, "end": 60.0}],
            str(out),
        )

        assert len(fallback_called) == 1
        mp3_arg, keep_arg, out_arg = fallback_called[0]
        assert mp3_arg == str(src)
        assert out_arg == str(out)
        assert keep_arg[0] == (0.0, 30.0)
        assert keep_arg[1][0] == 60.0

    def test_non_sigsegv_not_swallowed(self, monkeypatch, tmp_path):
        """splice_audio re-raises CalledProcessError for non-SIGSEGV exit codes."""
        import subprocess

        import pytest

        import ad_remover

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 5000)
        out = tmp_path / "out.mp3"

        exc = subprocess.CalledProcessError(1, "ffmpeg", stderr="error")

        def fake_run(cmd, **kwargs):
            if "-filter_complex" in cmd:
                raise exc
            return subprocess.CompletedProcess(cmd, 0, stdout="120.0", stderr="")

        monkeypatch.setattr(ad_remover.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="ffmpeg splice failed"):
            ad_remover.splice_audio(
                str(src),
                [{"start": 30.0, "end": 60.0}],
                str(out),
            )

    def test_concat_demuxer_cleans_up_on_error(self, monkeypatch, tmp_path):
        """_splice_concat_demuxer removes segment files even when join fails."""
        import pathlib
        import subprocess

        import pytest

        import ad_remover

        src = tmp_path / "ep.mp3"
        src.write_bytes(b"\xff\xfb" * 5000)
        out = tmp_path / "out.mp3"

        created_segs: list = []

        def fake_run(cmd, **kwargs):
            if "-ss" in cmd and "-to" in cmd:
                seg_path = cmd[-1]
                pathlib.Path(seg_path).write_bytes(b"seg")
                created_segs.append(seg_path)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if "-f" in cmd and "concat" in cmd:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="concat failed")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(ad_remover.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="concat-demuxer join failed"):
            ad_remover._splice_concat_demuxer(
                str(src),
                [(0.0, 30.0), (60.0, 120.0)],
                str(out),
            )

        for seg in created_segs:
            assert not pathlib.Path(seg).exists(), f"Segment not cleaned up: {seg}"
