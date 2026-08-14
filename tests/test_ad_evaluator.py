"""Tests for src/ad_evaluator.py."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ad_evaluator import (
    RESULT_CLEAN,
    RESULT_MISSED,
    RESULT_PARTIAL,
    _build_proposals,
    _classify_residual,
    evaluate_ad_removal,
)

# ---------------------------------------------------------------------------
# _classify_residual
# ---------------------------------------------------------------------------


class TestClassifyResidual:
    def _seg(self, start, end):
        return {"start": start, "end": end}

    def test_overlap_with_original_is_partial(self):
        """Residual that overlaps an original segment → PARTIAL."""
        residual = self._seg(60, 90)
        originals = [self._seg(55, 85)]
        assert _classify_residual(residual, originals) == RESULT_PARTIAL

    def test_within_boundary_tolerance_is_partial(self):
        """Residual within 10 s of an original boundary → PARTIAL."""
        residual = self._seg(90, 100)  # starts 5 s after original ends at 85
        originals = [self._seg(55, 85)]
        assert _classify_residual(residual, originals) == RESULT_PARTIAL

    def test_outside_tolerance_is_missed(self):
        """Residual > 10 s away from any original → MISSED."""
        residual = self._seg(200, 230)
        originals = [self._seg(55, 85)]
        assert _classify_residual(residual, originals) == RESULT_MISSED

    def test_empty_originals_always_missed(self):
        residual = self._seg(10, 30)
        assert _classify_residual(residual, []) == RESULT_MISSED

    def test_exact_boundary_match_is_partial(self):
        """Residual exactly at boundary (gap=0) → PARTIAL."""
        residual = self._seg(85, 95)
        originals = [self._seg(55, 85)]
        assert _classify_residual(residual, originals) == RESULT_PARTIAL

    def test_custom_tolerance(self):
        """Custom tolerance of 2 s — residual 5 s away → MISSED."""
        residual = self._seg(90, 100)  # 5 s after original ends at 85
        originals = [self._seg(55, 85)]
        assert _classify_residual(residual, originals, boundary_tolerance=2.0) == RESULT_MISSED


# ---------------------------------------------------------------------------
# _build_proposals
# ---------------------------------------------------------------------------


class TestBuildProposals:
    def _seg(self, start, end):
        return {"start": start, "end": end}

    def test_partial_residual_produces_boundary_extension_proposal(self):
        """A PARTIAL residual should generate a boundary_extension proposal."""
        residuals = [{"start": 88.0, "end": 95.0, "text": "buy now at example.com"}]
        originals = [self._seg(55, 85)]
        proposals = _build_proposals(residuals, originals)
        assert len(proposals) == 1
        assert proposals[0]["type"] == "boundary_extension"
        assert "affected_segment" in proposals[0]
        assert "suggestion" in proposals[0]
        assert "boundary" in proposals[0]["suggestion"].lower() or "padding" in proposals[0]["suggestion"].lower()

    def test_missed_residual_produces_missed_detection_proposal(self):
        """A MISSED residual should generate a missed_detection proposal."""
        residuals = [{"start": 300.0, "end": 330.0, "text": "use code PROMO for 20% off"}]
        originals = [self._seg(55, 85)]
        proposals = _build_proposals(residuals, originals)
        assert len(proposals) == 1
        assert proposals[0]["type"] == "missed_detection"
        assert "suggestion" in proposals[0]
        assert "PROMO" in proposals[0]["suggestion"]

    def test_empty_residuals_returns_empty_proposals(self):
        proposals = _build_proposals([], [])
        assert proposals == []

    def test_multiple_residuals_produce_multiple_proposals(self):
        residuals = [
            {"start": 88.0, "end": 95.0, "text": "near boundary"},  # PARTIAL
            {"start": 500.0, "end": 530.0, "text": "far from any"},  # MISSED
        ]
        originals = [self._seg(55, 85)]
        proposals = _build_proposals(residuals, originals)
        assert len(proposals) == 2
        types = {p["type"] for p in proposals}
        assert "boundary_extension" in types
        assert "missed_detection" in types

    def test_missed_no_text_uses_placeholder(self):
        """Missed residual with no 'text' key uses placeholder in suggestion."""
        residuals = [{"start": 400.0, "end": 420.0}]
        originals = []
        proposals = _build_proposals(residuals, originals)
        assert "(no transcript text)" in proposals[0]["suggestion"]


# ---------------------------------------------------------------------------
# evaluate_ad_removal — env-var gate
# ---------------------------------------------------------------------------


class TestEvaluateAdRemovalGate:
    def test_skipped_when_env_not_set(self, monkeypatch):
        monkeypatch.delenv("EVALUATE_AD_REMOVAL", raising=False)
        result = evaluate_ad_removal("fake.mp3", "ep001", "my-podcast")
        assert result == {"skipped": True}

    def test_skipped_when_env_is_false(self, monkeypatch):
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "false")
        result = evaluate_ad_removal("fake.mp3", "ep001", "my-podcast")
        assert result == {"skipped": True}

    def test_skipped_when_env_is_zero(self, monkeypatch):
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "0")
        result = evaluate_ad_removal("fake.mp3", "ep001", "my-podcast")
        assert result == {"skipped": True}

    def test_runs_when_env_is_true(self, monkeypatch, tmp_path):
        """When EVALUATE_AD_REMOVAL=true, the function should NOT return {"skipped": True}."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        with (
            patch("ad_remover.transcribe_audio", return_value=[]) as mock_t,
            patch("ad_remover.detect_ads", return_value=[]) as mock_d,
        ):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                reports_dir=str(tmp_path),
            )

        assert result.get("skipped") is not True
        assert result["result"] == RESULT_CLEAN
        mock_t.assert_called_once()
        mock_d.assert_called_once()

    def test_runs_when_env_is_one(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "1")

        with patch("ad_remover.transcribe_audio", return_value=[]), patch("ad_remover.detect_ads", return_value=[]):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                reports_dir=str(tmp_path),
            )
        assert result.get("skipped") is not True

    def test_runs_when_env_is_yes(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "yes")

        with patch("ad_remover.transcribe_audio", return_value=[]), patch("ad_remover.detect_ads", return_value=[]):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                reports_dir=str(tmp_path),
            )
        assert result.get("skipped") is not True


# ---------------------------------------------------------------------------
# evaluate_ad_removal — clean result
# ---------------------------------------------------------------------------


class TestEvaluateAdRemovalClean:
    def test_clean_result_when_no_residuals(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        segments = [{"start": 0.0, "end": 5.0, "text": "Hello world"}]

        with (
            patch("ad_remover.transcribe_audio", return_value=segments),
            patch("ad_remover.detect_ads", return_value=[]),
        ):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                original_ad_segments=[{"start": 60.0, "end": 90.0}],
                reports_dir=str(tmp_path),
            )

        assert result["result"] == RESULT_CLEAN
        assert result["residual_ad_segments"] == []
        assert result["proposals"] == []
        assert result["episode_id"] == "ep001"
        assert result["podcast_slug"] == "my-podcast"

    def test_report_file_written_to_correct_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        with patch("ad_remover.transcribe_audio", return_value=[]), patch("ad_remover.detect_ads", return_value=[]):
            evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                reports_dir=str(tmp_path),
            )

        report_path = tmp_path / "my-podcast" / "ep001_eval.json"
        assert report_path.exists()
        data = json.loads(report_path.read_text())
        assert data["episode_id"] == "ep001"
        assert data["result"] == RESULT_CLEAN

    def test_report_contains_all_required_keys(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        with patch("ad_remover.transcribe_audio", return_value=[]), patch("ad_remover.detect_ads", return_value=[]):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep-xyz",
                "test-slug",
                reports_dir=str(tmp_path),
            )

        required_keys = {
            "episode_id",
            "podcast_slug",
            "evaluated_at",
            "result",
            "original_ad_segments",
            "residual_ad_segments",
            "total_removed_seconds",
            "residual_seconds",
            "proposals",
        }
        assert required_keys.issubset(result.keys())

    def test_total_removed_seconds_calculated(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")
        original_segs = [{"start": 60.0, "end": 90.0}, {"start": 200.0, "end": 220.0}]

        with patch("ad_remover.transcribe_audio", return_value=[]), patch("ad_remover.detect_ads", return_value=[]):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                original_ad_segments=original_segs,
                reports_dir=str(tmp_path),
            )

        assert result["total_removed_seconds"] == pytest.approx(50.0)
        assert result["residual_seconds"] == 0.0


# ---------------------------------------------------------------------------
# evaluate_ad_removal — partial/missed residuals
# ---------------------------------------------------------------------------


class TestEvaluateAdRemovalResiduals:
    def test_partial_result_when_residual_near_original(self, monkeypatch, tmp_path):
        """Residual near original boundary → overall result PARTIAL (Fix #1: using translated coords).

        original_segs = [55–85] (30s removed).
        Residual at cleaned [58–65] translates to original [88–95]:
          keep [0–55] = 55s; cleaned 58 > 55 → original = 85 + (58-55) = 88.
        Original [88–95] is 3s past the end of [55–85], within 10s boundary tolerance → PARTIAL.
        """
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        original_segs = [{"start": 55.0, "end": 85.0}]
        # Residual in cleaned-file space: [58–65] → translates to [88–95] in original space
        residual = {"start": 58.0, "end": 65.0}

        segments = [{"start": 58.0, "end": 65.0, "text": "sponsor message"}]

        with (
            patch("ad_remover.transcribe_audio", return_value=segments),
            patch("ad_remover.detect_ads", return_value=[residual]),
        ):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                original_ad_segments=original_segs,
                reports_dir=str(tmp_path),
            )

        assert result["result"] == RESULT_PARTIAL
        assert len(result["residual_ad_segments"]) == 1
        assert len(result["proposals"]) == 1
        assert result["proposals"][0]["type"] == "boundary_extension"

    def test_missed_result_when_residual_far_from_original(self, monkeypatch, tmp_path):
        """Residual far from all originals → overall result MISSED."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        original_segs = [{"start": 55.0, "end": 85.0}]
        residual = {"start": 300.0, "end": 330.0}
        segments = [{"start": 300.0, "end": 330.0, "text": "use promo code XYZ"}]

        with (
            patch("ad_remover.transcribe_audio", return_value=segments),
            patch("ad_remover.detect_ads", return_value=[residual]),
        ):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                original_ad_segments=original_segs,
                reports_dir=str(tmp_path),
            )

        assert result["result"] == RESULT_MISSED
        assert result["proposals"][0]["type"] == "missed_detection"

    def test_missed_wins_over_partial_for_overall_result(self, monkeypatch, tmp_path):
        """If any residual is MISSED, overall result should be MISSED even if others are PARTIAL."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        original_segs = [{"start": 55.0, "end": 85.0}]
        residuals = [
            {"start": 88.0, "end": 95.0},  # PARTIAL (near boundary)
            {"start": 400.0, "end": 430.0},  # MISSED (far away)
        ]
        segments = [
            {"start": 88.0, "end": 95.0, "text": "near"},
            {"start": 400.0, "end": 430.0, "text": "far away ad"},
        ]

        with (
            patch("ad_remover.transcribe_audio", return_value=segments),
            patch("ad_remover.detect_ads", return_value=residuals),
        ):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                original_ad_segments=original_segs,
                reports_dir=str(tmp_path),
            )

        assert result["result"] == RESULT_MISSED
        assert len(result["proposals"]) == 2

    def test_none_original_segments_treated_as_empty(self, monkeypatch, tmp_path):
        """Passing original_ad_segments=None should not crash — treated as []."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        with patch("ad_remover.transcribe_audio", return_value=[]), patch("ad_remover.detect_ads", return_value=[]):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                original_ad_segments=None,
                reports_dir=str(tmp_path),
            )

        assert result["result"] == RESULT_CLEAN
        assert result["original_ad_segments"] == []

    def test_residual_text_attached_from_segments(self, monkeypatch, tmp_path):
        """Residual segments should have transcript text attached from transcription."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        segments = [
            {"start": 298.0, "end": 302.0, "text": "use code PODCAST20"},
            {"start": 302.0, "end": 310.0, "text": "for a discount"},
        ]
        residual = {"start": 300.0, "end": 308.0}

        with (
            patch("ad_remover.transcribe_audio", return_value=segments),
            patch("ad_remover.detect_ads", return_value=[residual]),
        ):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                original_ad_segments=[],
                reports_dir=str(tmp_path),
            )

        # The residual segment should have text attached
        assert "text" in result["residual_ad_segments"][0]


# ---------------------------------------------------------------------------
# evaluate_ad_removal — non-blocking error handling
# ---------------------------------------------------------------------------


class TestEvaluateAdRemovalErrorHandling:
    def test_transcription_failure_returns_skipped_with_error(self, monkeypatch, tmp_path):
        """If transcribe_audio raises, should return {"skipped": True, "error": ...}."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        with patch("ad_remover.transcribe_audio", side_effect=RuntimeError("AWS error")):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                reports_dir=str(tmp_path),
            )

        assert result.get("skipped") is True
        assert "error" in result
        assert "AWS error" in result["error"]

    def test_detect_ads_failure_returns_skipped_with_error(self, monkeypatch, tmp_path):
        """If detect_ads raises, should return {"skipped": True, "error": ...}."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        with (
            patch("ad_remover.transcribe_audio", return_value=[]),
            patch("ad_remover.detect_ads", side_effect=RuntimeError("Bedrock error")),
        ):
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                reports_dir=str(tmp_path),
            )

        assert result.get("skipped") is True
        assert "Bedrock error" in result["error"]

    def test_report_write_oserror_does_not_raise(self, monkeypatch, tmp_path):
        """If the report file cannot be written, the function should still return the report dict."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        with (
            patch("ad_remover.transcribe_audio", return_value=[]),
            patch("ad_remover.detect_ads", return_value=[]),
            patch("ad_evaluator.os.makedirs", side_effect=OSError("permission denied")),
        ):
            # Should NOT raise
            result = evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                reports_dir=str(tmp_path),
            )

        # Report dict is still returned even if file write failed
        assert result["result"] == RESULT_CLEAN

    def test_transcription_called_with_eval_prefix(self, monkeypatch, tmp_path):
        """transcribe_audio job name should be prefixed with 'eval-' to avoid collision."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")

        with (
            patch("ad_remover.transcribe_audio", return_value=[]) as mock_t,
            patch("ad_remover.detect_ads", return_value=[]),
        ):
            evaluate_ad_removal(
                "fake.mp3",
                "ep-abc123",
                "my-podcast",
                reports_dir=str(tmp_path),
            )

        call_args = mock_t.call_args
        # Second positional arg (job name) should start with "eval-"
        job_name = call_args[0][1]
        assert job_name.startswith("eval-")
        assert "ep-abc123" in job_name


# ---------------------------------------------------------------------------
# evaluate_ad_removal — reports_dir resolution
# ---------------------------------------------------------------------------


class TestEvaluateAdRemovalReportsDir:
    def test_reports_dir_defaults_to_reports(self, monkeypatch, tmp_path):
        """When reports_dir is None and EVAL_REPORTS_DIR is unset, defaults to 'reports'."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")
        monkeypatch.delenv("EVAL_REPORTS_DIR", raising=False)

        with (
            patch("ad_remover.transcribe_audio", return_value=[]),
            patch("ad_remover.detect_ads", return_value=[]),
            patch("ad_evaluator.os.makedirs") as mock_makedirs,
            patch("builtins.open", side_effect=OSError("skip write")),
        ):
            evaluate_ad_removal("fake.mp3", "ep001", "my-podcast")

        # First call to makedirs should use "reports/my-podcast"
        call_path = mock_makedirs.call_args[0][0]
        assert call_path == os.path.join("reports", "my-podcast")

    def test_reports_dir_uses_eval_reports_dir_env(self, monkeypatch, tmp_path):
        """EVAL_REPORTS_DIR env var overrides the default."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")
        monkeypatch.setenv("EVAL_REPORTS_DIR", str(tmp_path / "custom_reports"))

        with patch("ad_remover.transcribe_audio", return_value=[]), patch("ad_remover.detect_ads", return_value=[]):
            evaluate_ad_removal("fake.mp3", "ep001", "my-podcast")

        expected_dir = tmp_path / "custom_reports" / "my-podcast"
        assert expected_dir.exists()

    def test_explicit_reports_dir_overrides_env(self, monkeypatch, tmp_path):
        """Explicit reports_dir arg takes precedence over EVAL_REPORTS_DIR env var."""
        monkeypatch.setenv("EVALUATE_AD_REMOVAL", "true")
        monkeypatch.setenv("EVAL_REPORTS_DIR", str(tmp_path / "env_reports"))
        explicit_dir = tmp_path / "explicit_reports"

        with patch("ad_remover.transcribe_audio", return_value=[]), patch("ad_remover.detect_ads", return_value=[]):
            evaluate_ad_removal(
                "fake.mp3",
                "ep001",
                "my-podcast",
                reports_dir=str(explicit_dir),
            )

        assert (explicit_dir / "my-podcast" / "ep001_eval.json").exists()
        assert not (tmp_path / "env_reports").exists()
