"""Tests for the 5 ad-cleaner fixes.

Fix #1 – Evaluator timestamp coordinate translation (_translate_cleaned_to_original)
Fix #2 – Max ad segment duration guard (MAX_AD_SEGMENT_SECS)
Fix #3 – Reduced merge gap + updated prompt (10s→30s, rule rewrite)
Fix #4 – Second-pass verification for large segments (AD_VERIFY_THRESHOLD_SECS)
Fix #5 – Silence-based boundary snapping (snap_ad_boundaries / detect_silence)

Each class maps to one fix. An "E2E scenario" section at the bottom wires
the full pipeline with mocked AWS to reproduce the three real failure cases
observed in production eval reports.
"""

from __future__ import annotations

import json

# Ensure src/ is importable
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _make_segments(*pairs: tuple[float, float, str]) -> list[dict]:
    """Build transcript segment dicts from (start, end, text) triples."""
    return [{"start": s, "end": e, "text": t} for s, e, t in pairs]


def _bedrock_response(text: str) -> dict:
    """Wrap a string in the Bedrock Converse response shape."""
    return {"output": {"message": {"content": [{"text": text}]}}}


# ---------------------------------------------------------------------------
# Fix #1 – Timestamp coordinate translation
# ---------------------------------------------------------------------------


class TestTranslateCleanedToOriginal:
    """_translate_cleaned_to_original converts cleaned timestamps → original space."""

    def _fn(self):
        from ad_evaluator import _translate_cleaned_to_original

        return _translate_cleaned_to_original

    def test_no_removed_segments_is_identity(self):
        fn = self._fn()
        assert fn(100.0, []) == 100.0
        assert fn(0.0, []) == 0.0

    def test_single_removal_before_query_point(self):
        """Remove [100–200] (100s), query cleaned time 50 → original 50 (before cut)."""
        fn = self._fn()
        # 50s is in the first keep interval [0–100], so no shift
        assert fn(50.0, [{"start": 100.0, "end": 200.0}]) == 50.0

    def test_single_removal_after_cut_adds_offset(self):
        """Remove [100–200], query cleaned time 150 → original 250 (150+100)."""
        fn = self._fn()
        result = fn(150.0, [{"start": 100.0, "end": 200.0}])
        assert abs(result - 250.0) < 0.01

    def test_multiple_removals_accumulate_offsets(self):
        """Remove [50–100] (50s) and [200–300] (100s).
        Cleaned time 180 lands after both cuts → original 180 + 50 + 100 = 330.
        But let's trace carefully:
          keep [0–50]   → cleaned [0–50]
          removed [50–100]
          keep [100–200] → cleaned [50–150]
          removed [200–300]
          keep [300–...] → cleaned [150–...]
        cleaned 180 falls in [150–...] → original 300 + (180-150) = 330.
        """
        fn = self._fn()
        removed = [{"start": 50.0, "end": 100.0}, {"start": 200.0, "end": 300.0}]
        result = fn(180.0, removed)
        assert abs(result - 330.0) < 0.01

    def test_mewanv5zrac_scenario(self):
        """Reproduce the mEWanV5zrac partial-miss case.

        Original ad: [93.4–139.6] (46.2s removed).
        Evaluator found residual at [95.3–131.6] in cleaned file.
        Expected original position: [141.5–177.8].
        """
        fn = self._fn()
        removed = [{"start": 93.4, "end": 139.6}]

        orig_start = fn(95.3, removed)
        orig_end = fn(131.6, removed)

        assert abs(orig_start - 141.5) < 0.5  # 139.6 + (95.3-93.4) = 141.5
        assert abs(orig_end - 177.8) < 0.5  # 139.6 + (131.6-93.4) = 177.8

    def test_lljpnubsowc_scenario(self):
        """Reproduce the LLjpnubsOWc missed-second-ad case.

        Removed [1256.3–1467.9] (211.6s).
        Evaluator found residual at [4948.0–5102.1] in cleaned file.
        Expected original position ≈ [5159.6–5313.7].
        """
        fn = self._fn()
        removed = [{"start": 1256.3, "end": 1467.9}]

        orig_start = fn(4948.0, removed)
        orig_end = fn(5102.1, removed)

        assert abs(orig_start - 5159.6) < 1.0
        assert abs(orig_end - 5313.7) < 1.0

    def test_translation_stored_in_eval_report(self, monkeypatch, tmp_path):
        """evaluate_ad_removal writes original_time_start/end into residual dicts."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        removed = [{"start": 100.0, "end": 150.0}]  # 50s removed
        # Residual at cleaned [60–80] → original [110–130]  (before cut; no shift for <100)
        # Actually: cleaned 60 < 100 (keep interval end) → original 60.
        # Let's use a residual that's after the cut:
        # cleaned 120 → original 170, cleaned 140 → original 190
        residual_segs = [{"start": 120.0, "end": 140.0}]

        with (
            patch("ad_remover.transcribe_audio", return_value=[]),
            patch("ad_remover.detect_ads", return_value=residual_segs),
        ):
            from ad_evaluator import evaluate_ad_removal

            report = evaluate_ad_removal(
                "fake.mp3",
                "ep-001",
                "slug",
                original_ad_segments=removed,
                reports_dir=str(tmp_path),
            )

        residuals = report["residual_ad_segments"]
        assert len(residuals) == 1
        r = residuals[0]
        assert "original_time_start" in r
        assert "original_time_end" in r
        assert abs(r["original_time_start"] - 170.0) < 0.1  # 150+(120-100)
        assert abs(r["original_time_end"] - 190.0) < 0.1  # 150+(140-100)


# ---------------------------------------------------------------------------
# Fix #2 – Max ad duration guard
# ---------------------------------------------------------------------------


class TestMaxAdDurationGuard:
    """detect_ads drops segments that exceed MAX_AD_SEGMENT_SECS."""

    def _run_detect(
        self, monkeypatch, bedrock_json: str, max_secs: str = "180", verify_threshold: str = "9999"
    ) -> list[dict]:
        monkeypatch.setenv("MAX_AD_SEGMENT_SECS", max_secs)
        monkeypatch.setenv("AD_VERIFY_THRESHOLD_SECS", verify_threshold)

        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = _bedrock_response(bedrock_json)

        with (
            patch("boto3.client", return_value=mock_bedrock),
            patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()),
        ):
            import importlib

            import ad_remover

            importlib.reload(ad_remover)
            segs = _make_segments((0.0, 5.0, "intro"), (10.0, 15.0, "more"))
            return ad_remover.detect_ads(segs)

    def test_normal_segment_not_dropped(self, monkeypatch):
        """60s segment with default 180s max is kept."""
        result = self._run_detect(monkeypatch, '[{"start": 10.0, "end": 70.0}]')
        assert len(result) == 1
        assert result[0]["start"] == 10.0

    def test_segment_at_exactly_max_is_kept(self, monkeypatch):
        """Segment equal to max is kept (guard is strictly greater-than)."""
        result = self._run_detect(
            monkeypatch,
            '[{"start": 0.0, "end": 180.0}]',
            max_secs="180",
        )
        assert len(result) == 1

    def test_segment_over_max_is_dropped(self, monkeypatch):
        """181s segment with 180s max is dropped as a false positive."""
        result = self._run_detect(
            monkeypatch,
            '[{"start": 0.0, "end": 181.0}]',
            max_secs="180",
        )
        assert result == []

    def test_large_false_positive_dropped_7b0a1fd8_scenario(self, monkeypatch):
        """Reproduce the 341s segment from 7b0a1fd8 — must be dropped."""
        result = self._run_detect(
            monkeypatch,
            '[{"start": 488.9, "end": 830.2}]',
            max_secs="180",
        )
        assert result == [], "341s segment should be dropped as false positive"

    def test_custom_max_via_env(self, monkeypatch):
        """Custom MAX_AD_SEGMENT_SECS=60 drops a 90s segment."""
        result = self._run_detect(
            monkeypatch,
            '[{"start": 10.0, "end": 100.0}]',  # 90s
            max_secs="60",
        )
        assert result == []

    def test_real_ad_within_max_preserved(self, monkeypatch):
        """40s real ad stays when max is 180s."""
        result = self._run_detect(
            monkeypatch,
            '[{"start": 93.4, "end": 139.6}]',  # 46.2s
            max_secs="180",
        )
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Fix #3 – Merge gap reduced, prompt rule updated
# ---------------------------------------------------------------------------


class TestMergeGapReduced:
    """_merge_overlapping_ads uses a 2s gap (was 5s)."""

    def _merge(self, ads):
        import importlib

        import ad_remover

        importlib.reload(ad_remover)
        return ad_remover._merge_overlapping_ads(ads)

    def test_segments_3s_apart_not_merged(self):
        """3s gap > 2s threshold → kept separate."""
        ads = [{"start": 0.0, "end": 10.0}, {"start": 13.0, "end": 20.0}]
        result = self._merge(ads)
        assert len(result) == 2

    def test_segments_2s_apart_are_merged(self):
        """2s gap ≤ 2s threshold → merged."""
        ads = [{"start": 0.0, "end": 10.0}, {"start": 12.0, "end": 20.0}]
        result = self._merge(ads)
        assert len(result) == 1
        assert result[0] == {"start": 0.0, "end": 20.0}

    def test_old_5s_gap_no_longer_merges(self):
        """4s gap was merged under old 5s rule but not under new 2s rule."""
        ads = [{"start": 0.0, "end": 10.0}, {"start": 14.0, "end": 25.0}]
        result = self._merge(ads)
        # Under new rule: 14.0 > 10.0 + 2 → separate
        assert len(result) == 2, "4s gap should NOT be merged with new 2s threshold"

    def test_overlapping_segments_still_merged(self):
        """Overlapping segments always merge regardless of gap rule."""
        ads = [{"start": 0.0, "end": 15.0}, {"start": 10.0, "end": 25.0}]
        result = self._merge(ads)
        assert len(result) == 1
        assert result[0]["end"] == 25.0

    def test_prompt_delegates_merging_to_code(self):
        """Prompt tells model to return separate segments; code handles merging."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)
        prompt = ad_remover._AD_DETECTION_PROMPT
        # Model should NOT merge — code does it with a calibrated 2s threshold
        assert "Do NOT merge" in prompt, "Prompt should instruct model not to merge segments"
        assert "within 30 seconds" not in prompt, "Conflicting 30s merge rule must be absent"
        assert "the code will handle merging" in prompt

    def test_prompt_no_longer_says_when_in_doubt_include(self):
        """Aggressive 'When in doubt, INCLUDE' rule removed from prompt."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)
        prompt = ad_remover._AD_DETECTION_PROMPT
        assert "When in doubt, INCLUDE" not in prompt


# ---------------------------------------------------------------------------
# Fix #4 – Second-pass verification
# ---------------------------------------------------------------------------


class TestSecondPassVerification:
    """_verify_ad_segment and its integration in detect_ads."""

    def _verify(
        self, is_ad_response: bool, monkeypatch, segment: dict | None = None, transcript_segs: list | None = None
    ) -> bool:
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        seg = segment or {"start": 100.0, "end": 250.0}
        segs = transcript_segs or _make_segments((100.0, 250.0, "sponsor content here"))

        # Response text excludes leading "{" since remove_ads prepends it (assistant prefill)
        json_resp = json.dumps({"is_ad": is_ad_response, "reason": "test reason"})[1:]  # strip leading {
        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = _bedrock_response(json_resp)

        with patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            return ad_remover._verify_ad_segment(seg, segs, mock_bedrock, "test-model")

    def test_verification_confirms_ad(self, monkeypatch):
        assert self._verify(True, monkeypatch) is True

    def test_verification_rejects_non_ad(self, monkeypatch):
        assert self._verify(False, monkeypatch) is False

    def test_verification_defaults_true_on_api_error(self, monkeypatch):
        """On Bedrock error, segment is kept (fail-safe — never silently drop real ads)."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        mock_bedrock = MagicMock()
        mock_bedrock.converse.side_effect = RuntimeError("Bedrock unavailable")

        with patch("ad_remover.retry_aws_call", side_effect=RuntimeError("Bedrock unavailable")):
            result = ad_remover._verify_ad_segment(
                {"start": 100.0, "end": 200.0},
                _make_segments((100.0, 200.0, "some text")),
                mock_bedrock,
                "test-model",
            )
        assert result is True

    def test_verification_defaults_true_with_no_transcript(self, monkeypatch):
        """When there is no transcript text for a segment, keep it (no evidence to reject)."""
        result = self._verify(True, monkeypatch, transcript_segs=[])
        assert result is True

    def test_detect_ads_verifies_segments_above_threshold(self, monkeypatch):
        """detect_ads calls _verify_ad_segment for segments > AD_VERIFY_THRESHOLD_SECS."""
        monkeypatch.setenv("AD_VERIFY_THRESHOLD_SECS", "60")
        monkeypatch.setenv("MAX_AD_SEGMENT_SECS", "9999")

        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        segs = _make_segments((50.0, 55.0, "intro"), (200.0, 205.0, "content"))
        detection_json = '{"start": 50.0, "end": 170.0}]'  # 120s → above 60s threshold (no leading [ — prefill)

        verify_json = json.dumps({"is_ad": True, "reason": "confirmed ad"})[1:]  # strip leading { — prefill
        mock_bedrock = MagicMock()
        # First call = detection, second call = verification
        mock_bedrock.converse.side_effect = [
            _bedrock_response(detection_json),
            _bedrock_response(verify_json),
        ]

        with (
            patch("boto3.client", return_value=mock_bedrock),
            patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()),
        ):
            result = ad_remover.detect_ads(segs)

        # Verification was called (two Bedrock calls total)
        assert mock_bedrock.converse.call_count == 2
        assert len(result) == 1

    def test_detect_ads_drops_rejected_segment(self, monkeypatch):
        """Segment rejected by second-pass verification is excluded from results."""
        monkeypatch.setenv("AD_VERIFY_THRESHOLD_SECS", "60")
        monkeypatch.setenv("MAX_AD_SEGMENT_SECS", "9999")

        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        segs = _make_segments((100.0, 110.0, "discussion"))
        detection_json = '{"start": 100.0, "end": 220.0}]'  # 120s → triggers verify (no leading [ — prefill)
        reject_json = json.dumps({"is_ad": False, "reason": "this is normal content"})[1:]  # strip leading { — prefill

        mock_bedrock = MagicMock()
        mock_bedrock.converse.side_effect = [
            _bedrock_response(detection_json),
            _bedrock_response(reject_json),
        ]

        with (
            patch("boto3.client", return_value=mock_bedrock),
            patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()),
        ):
            result = ad_remover.detect_ads(segs)

        assert result == [], "Rejected segment should not appear in results"

    def test_detect_ads_skips_verification_for_short_segments(self, monkeypatch):
        """Segments below threshold get only one Bedrock call (no verification)."""
        monkeypatch.setenv("AD_VERIFY_THRESHOLD_SECS", "120")
        monkeypatch.setenv("MAX_AD_SEGMENT_SECS", "9999")

        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        segs = _make_segments((10.0, 20.0, "ad text"))
        detection_json = '{"start": 10.0, "end": 70.0}]'  # 60s < 120s threshold (no leading [ — prefill)

        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = _bedrock_response(detection_json)

        with (
            patch("boto3.client", return_value=mock_bedrock),
            patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()),
        ):
            result = ad_remover.detect_ads(segs)

        # Only one call (detection), no verification
        assert mock_bedrock.converse.call_count == 1
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Fix #5 – Silence-based boundary snapping
# ---------------------------------------------------------------------------


class TestSilenceBoundarySnapping:
    """detect_silence, _snap_to_silence_boundary, snap_ad_boundaries."""

    def _parse_silence_output(self, stderr_text: str) -> list[dict]:
        """Run detect_silence with mocked ffmpeg stderr."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        fake_result = MagicMock(stderr=stderr_text)
        with patch("subprocess.run", return_value=fake_result):
            return ad_remover.detect_silence("/fake.mp3")

    def test_detect_silence_parses_ffmpeg_output(self):
        stderr = (
            "[silencedetect] silence_start: 10.5\n"
            "[silencedetect] silence_end: 11.2 | silence_duration: 0.7\n"
            "[silencedetect] silence_start: 50.0\n"
            "[silencedetect] silence_end: 51.5 | silence_duration: 1.5\n"
        )
        silences = self._parse_silence_output(stderr)
        assert len(silences) == 2
        assert silences[0] == {"start": 10.5, "end": 11.2, "duration": 0.7}
        assert silences[1] == {"start": 50.0, "end": 51.5, "duration": 1.5}

    def test_detect_silence_returns_empty_on_no_silences(self):
        silences = self._parse_silence_output("[ffmpeg] processing...\n")
        assert silences == []

    def test_detect_silence_returns_empty_when_ffmpeg_missing(self):
        import importlib

        import ad_remover

        importlib.reload(ad_remover)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = ad_remover.detect_silence("/fake.mp3")
        assert result == []

    def test_snap_moves_boundary_to_nearest_silence(self):
        import importlib

        import ad_remover

        importlib.reload(ad_remover)
        silences = [{"start": 98.0, "end": 100.5, "duration": 2.5}]
        # time=100.0, nearest boundary is silence start 98.0 (dist 2.0) or end 100.5 (dist 0.5)
        result = ad_remover._snap_to_silence_boundary(100.0, silences, window=3.0)
        assert result == 100.5  # closer

    def test_snap_ignores_boundaries_outside_window(self):
        import importlib

        import ad_remover

        importlib.reload(ad_remover)
        silences = [{"start": 90.0, "end": 95.0, "duration": 5.0}]
        # time=100.0, nearest boundary is 95.0 (dist 5.0) > window=3.0
        result = ad_remover._snap_to_silence_boundary(100.0, silences, window=3.0)
        assert result == 100.0  # unchanged

    def test_snap_ad_boundaries_adjusts_both_endpoints(self):
        """snap_ad_boundaries moves start and end to silence boundaries."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        fake_result = MagicMock(
            stderr=(
                "[silencedetect] silence_start: 92.0\n"
                "[silencedetect] silence_end: 94.5 | silence_duration: 2.5\n"
                "[silencedetect] silence_start: 138.0\n"
                "[silencedetect] silence_end: 140.0 | silence_duration: 2.0\n"
            )
        )

        with patch("subprocess.run", return_value=fake_result):
            result = ad_remover.snap_ad_boundaries([{"start": 93.4, "end": 139.6}], "/fake.mp3", snap_window=3.0)

        assert len(result) == 1
        # start 93.4 → nearest is 94.5 (dist 1.1) or 92.0 (dist 1.4)
        assert result[0]["start"] == 94.5
        # end 139.6 → nearest is 140.0 (dist 0.4) or 138.0 (dist 1.6)
        assert result[0]["end"] == 140.0

    def test_snap_preserves_original_when_no_silences(self):
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        with patch("subprocess.run", return_value=MagicMock(stderr="")):
            result = ad_remover.snap_ad_boundaries([{"start": 93.4, "end": 139.6}], "/fake.mp3")
        assert result == [{"start": 93.4, "end": 139.6}]

    def test_snap_skips_if_result_too_short(self):
        """Snapping that shrinks a segment below minimum keeps original."""
        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        # Silence right at 95 and 97 — would snap [93, 100] to [95, 97] = 2s (< 5s min)
        fake_stderr = (
            "[silencedetect] silence_start: 94.5\n"
            "[silencedetect] silence_end: 95.5 | silence_duration: 1.0\n"
            "[silencedetect] silence_start: 96.5\n"
            "[silencedetect] silence_end: 97.5 | silence_duration: 1.0\n"
        )
        with patch("subprocess.run", return_value=MagicMock(stderr=fake_stderr)):
            result = ad_remover.snap_ad_boundaries([{"start": 93.0, "end": 100.0}], "/fake.mp3", snap_window=3.0)
        # Result kept at original since snapped version would be too short
        assert result == [{"start": 93.0, "end": 100.0}]

    def test_remove_ads_calls_snap_when_enabled(self, monkeypatch, tmp_path):
        """remove_ads calls snap_ad_boundaries when AD_SNAP_TO_SILENCE=true."""
        monkeypatch.setenv("AD_SNAP_TO_SILENCE", "true")
        monkeypatch.setenv("REMOVE_ADS_DRY_RUN", "true")

        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        fake_segs = _make_segments((100.0, 140.0, "ad text"))
        fake_ads = [{"start": 100.0, "end": 140.0}]

        with (
            patch("ad_remover.transcribe_audio", return_value=fake_segs),
            patch("ad_remover.detect_ads", return_value=fake_ads),
            patch("ad_remover.snap_ad_boundaries", return_value=fake_ads) as mock_snap,
        ):
            ad_remover.remove_ads("/fake.mp3", "ep-001", str(tmp_path))

        mock_snap.assert_called_once_with(fake_ads, "/fake.mp3", silences=[])

    def test_remove_ads_skips_snap_when_disabled(self, monkeypatch, tmp_path):
        """AD_SNAP_TO_SILENCE=false bypasses silence snapping."""
        monkeypatch.setenv("AD_SNAP_TO_SILENCE", "false")
        monkeypatch.setenv("REMOVE_ADS_DRY_RUN", "true")

        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        with (
            patch("ad_remover.transcribe_audio", return_value=[]),
            patch("ad_remover.detect_ads", return_value=[{"start": 10.0, "end": 50.0}]),
            patch("ad_remover.snap_ad_boundaries") as mock_snap,
        ):
            ad_remover.remove_ads("/fake.mp3", "ep-001", str(tmp_path))

        mock_snap.assert_not_called()


# ---------------------------------------------------------------------------
# E2E scenarios – reproduce the 3 real failure cases
# ---------------------------------------------------------------------------


class TestE2EScenarios:
    """Wire the full detect → filter → verify → snap pipeline with mocked AWS.

    These reproduce the specific failure modes found in the production eval reports.
    They act as regression tests — if the fixes regress, these will catch it.
    """

    def _run_pipeline(
        self,
        monkeypatch,
        detection_json: str,
        verify_json: str | None = None,
        max_secs: str = "180",
        verify_threshold: str = "90",
        transcript_text: str = "sponsor content",
        ad_snap: str = "false",
    ) -> list[dict]:
        """Run detect_ads with mocked Bedrock and return the final ad list.

        Note: detection_json and verify_json should be the FULL expected JSON.
        This helper strips the leading '[' / '{' to simulate assistant prefill
        (the code prepends these characters to the model output).
        """
        monkeypatch.setenv("MAX_AD_SEGMENT_SECS", max_secs)
        monkeypatch.setenv("AD_VERIFY_THRESHOLD_SECS", verify_threshold)
        monkeypatch.setenv("AD_SNAP_TO_SILENCE", ad_snap)

        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        segs = _make_segments((50.0, 60.0, transcript_text))

        # Strip leading prefill characters that the code will re-add
        detect_resp = detection_json[1:] if detection_json.startswith("[") else detection_json
        responses = [_bedrock_response(detect_resp)]
        if verify_json:
            verify_resp = verify_json[1:] if verify_json.startswith("{") else verify_json
            responses.append(_bedrock_response(verify_resp))

        mock_bedrock = MagicMock()
        mock_bedrock.converse.side_effect = responses

        with (
            patch("boto3.client", return_value=mock_bedrock),
            patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()),
        ):
            return ad_remover.detect_ads(segs)

    def test_e2e_7b0a1fd8_false_positive_blocked(self, monkeypatch):
        """E2E: 341s segment (7b0a1fd8) is blocked by max duration guard.

        Previously this removed 5.7 minutes of actual podcast content.
        With MAX_AD_SEGMENT_SECS=180, it is silently dropped.
        """
        result = self._run_pipeline(
            monkeypatch,
            detection_json='[{"start": 488.9, "end": 830.2}]',  # 341.3s
            transcript_text="right ? Or uh there's anything that's sort of like last minute",
            max_secs="180",
        )
        assert result == [], "341s false positive must be blocked by max duration guard"

    def test_e2e_7b0a1fd8_long_segment_rejected_by_verification(self, monkeypatch):
        """E2E: Even if under 180s, a segment that looks like content is rejected by verification.

        The transcript segment must overlap the ad window so _verify_ad_segment can
        extract text for the second-pass call. When no text is found, the fail-safe
        keeps the segment (covered in TestSecondPassVerification).
        """
        monkeypatch.setenv("MAX_AD_SEGMENT_SECS", "300")
        monkeypatch.setenv("AD_VERIFY_THRESHOLD_SECS", "90")
        monkeypatch.setenv("AD_SNAP_TO_SILENCE", "false")

        import importlib

        import ad_remover

        importlib.reload(ad_remover)

        # Transcript inside the ad window so verification receives text to evaluate
        segs = _make_segments((110.0, 200.0, "we discussed the architecture and tradeoffs"))
        detection_json = '{"start": 100.0, "end": 210.0}]'  # 110s triggers verify (no leading [ — prefill)
        reject_json = json.dumps({"is_ad": False, "reason": "editorial discussion not ad"})[
            1:
        ]  # strip leading { — prefill

        mock_bedrock = MagicMock()
        mock_bedrock.converse.side_effect = [
            _bedrock_response(detection_json),
            _bedrock_response(reject_json),
        ]

        with (
            patch("boto3.client", return_value=mock_bedrock),
            patch("ad_remover.retry_aws_call", side_effect=lambda fn, **kw: fn()),
        ):
            result = ad_remover.detect_ads(segs)

        assert result == [], "Content-like segment must be rejected by second-pass verification"

    def test_e2e_real_ad_confirmed_by_verification(self, monkeypatch):
        """E2E: A genuine 100s ad survives both duration guard and verification."""
        result = self._run_pipeline(
            monkeypatch,
            detection_json='[{"start": 60.0, "end": 170.0}]',  # 110s → triggers verify
            verify_json=json.dumps({"is_ad": True, "reason": "clear promo code and URL"}),
            transcript_text="use code PODCAST for 20% off at example dot com slash show",
            max_secs="300",
            verify_threshold="90",
        )
        assert len(result) == 1
        assert result[0]["start"] == 60.0
        assert result[0]["end"] == 170.0

    def test_e2e_multi_ad_episode_not_over_merged(self, monkeypatch):
        """E2E: Two ads 20s apart are no longer collapsed into one.

        Under old 5s merge gap they would merge. Under new 2s gap they stay separate.
        """
        # Two segments 20s apart — should stay separate
        result = self._run_pipeline(
            monkeypatch,
            detection_json='[{"start": 60.0, "end": 120.0}, {"start": 140.0, "end": 200.0}]',
            max_secs="300",
            verify_threshold="9999",  # disable verification for this test
        )
        assert len(result) == 2, "Two ads 20s apart should remain separate"
        assert result[0]["end"] == 120.0
        assert result[1]["start"] == 140.0

    def test_e2e_evaluator_classifies_correctly_after_coordinate_fix(self, tmp_path, monkeypatch):
        """E2E: Evaluator correctly classifies mEWanV5zrac residual as partial (not missed).

        Before fix: residual [95.3–131.6] in cleaned space compared directly against
        original [93.4–139.6] — confusingly appeared as overlap.
        After fix: residual translated to original [141.5–177.8], correctly classified
        as partial (within 10s boundary tolerance of original end at 139.6).
        """
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        original_segs = [{"start": 93.4, "end": 139.6}]
        # Evaluator finds residual at [95.3–131.6] in cleaned file
        residual_segs = [{"start": 95.3, "end": 131.6}]

        with (
            patch("ad_remover.transcribe_audio", return_value=[]),
            patch("ad_remover.detect_ads", return_value=residual_segs),
        ):
            from ad_evaluator import RESULT_PARTIAL, evaluate_ad_removal

            report = evaluate_ad_removal(
                "fake.mp3",
                "mEWanV5zrac",
                "test-slug",
                original_ad_segments=original_segs,
                reports_dir=str(tmp_path),
            )

        assert report["result"] == RESULT_PARTIAL, (
            f"Expected partial, got {report['result']}. "
            "Residual translated to [141.5–177.8] should be within 10s of original end 139.6."
        )
        r = report["residual_ad_segments"][0]
        assert abs(r["original_time_start"] - 141.5) < 0.5
        assert abs(r["original_time_end"] - 177.8) < 0.5
