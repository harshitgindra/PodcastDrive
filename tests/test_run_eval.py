"""Unit tests for eval/run_eval.py pure-logic functions.

These tests cover score_against_ground_truth(), ci_check_scores(),
check_phrases_absent(), check_phrases_present(), and check_duration_reduction()
without any AWS calls, ffmpeg, or real file I/O.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Add eval/ to path so we can import run_eval
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))


def _seg(start: float, end: float, label: str = "ad") -> dict:
    return {"start": start, "end": end, "label": label}


def _transcript_seg(start: float, end: float, text: str) -> dict:
    return {"start": start, "end": end, "text": text}


@pytest.fixture(autouse=True)
def _patch_imports(monkeypatch):
    """Stub out heavyweight imports that run_eval pulls from src/."""
    # ad_remover functions are imported at module level — mock them
    monkeypatch.setenv("PYTHONPATH", os.path.join(os.path.dirname(__file__), "..", "src"))


def _get_module():
    """Import run_eval (re-import to pick up patches)."""
    import importlib
    if "run_eval" in sys.modules:
        return importlib.reload(sys.modules["run_eval"])
    import run_eval
    return run_eval


# ---------------------------------------------------------------------------
# score_against_ground_truth
# ---------------------------------------------------------------------------

class TestScoreAgainstGroundTruth:
    def test_perfect_detection(self):
        m = _get_module()
        gt = {"ep.mp3": [_seg(100, 200)]}
        results = {"ep.mp3": {"ad_segments": [{"start": 100, "end": 200}]}}
        scores = m.score_against_ground_truth(results, gt)
        assert scores["ep.mp3"]["f1"] == 1.0

    def test_missed_ad_zero_recall(self):
        m = _get_module()
        gt = {"ep.mp3": [_seg(100, 200)]}
        results = {"ep.mp3": {"ad_segments": []}}
        scores = m.score_against_ground_truth(results, gt)
        assert scores["ep.mp3"]["recall"] == 0.0
        assert scores["ep.mp3"]["missed"] == 1

    def test_false_positive_zero_precision(self):
        m = _get_module()
        gt = {"ep.mp3": []}
        results = {"ep.mp3": {"ad_segments": [{"start": 50, "end": 100}]}}
        scores = m.score_against_ground_truth(results, gt)
        assert scores["ep.mp3"]["precision"] == 0.0
        assert scores["ep.mp3"]["false_positives"] == 1

    def test_both_empty_is_perfect(self):
        m = _get_module()
        gt = {"ep.mp3": []}
        results = {"ep.mp3": {"ad_segments": []}}
        scores = m.score_against_ground_truth(results, gt)
        assert scores["ep.mp3"]["f1"] == 1.0

    def test_no_overlap_is_false_positive(self):
        m = _get_module()
        gt = {"ep.mp3": [_seg(500, 600)]}
        results = {"ep.mp3": {"ad_segments": [{"start": 100, "end": 150}]}}
        scores = m.score_against_ground_truth(results, gt)
        assert scores["ep.mp3"]["false_positives"] == 1
        assert scores["ep.mp3"]["missed"] == 1


# ---------------------------------------------------------------------------
# ci_check_scores
# ---------------------------------------------------------------------------

class TestCiCheckScores:
    def _scores(self, f1, recall, precision=1.0, ep="ep.mp3"):
        return {ep: {"f1": f1, "recall": recall, "precision": precision,
                     "missed": 0, "false_positives": 0}}

    def test_passes_when_both_thresholds_met(self):
        m = _get_module()
        ok, msgs = m.ci_check_scores(self._scores(0.80, 0.80))
        assert ok is True
        assert msgs == []

    def test_fails_on_low_f1(self):
        m = _get_module()
        ok, msgs = m.ci_check_scores(self._scores(0.60, 0.80))
        assert ok is False
        assert "F1=" in msgs[0]

    def test_fails_on_low_recall_even_if_f1_ok(self):
        m = _get_module()
        ok, msgs = m.ci_check_scores(
            self._scores(0.80, 0.60), f1_threshold=0.75, recall_threshold=0.70)
        assert ok is False
        assert "recall=" in msgs[0]

    def test_custom_thresholds(self):
        m = _get_module()
        ok, _ = m.ci_check_scores(self._scores(0.70, 0.65), f1_threshold=0.65, recall_threshold=0.60)
        assert ok is True

    def test_exactly_at_threshold_passes(self):
        m = _get_module()
        ok, _ = m.ci_check_scores(self._scores(0.75, 0.70), f1_threshold=0.75, recall_threshold=0.70)
        assert ok is True


# ---------------------------------------------------------------------------
# check_phrases_absent
# ---------------------------------------------------------------------------

class TestCheckPhrasesAbsent:
    def _segs(self, *texts):
        return [_transcript_seg(i * 10.0, (i + 1) * 10.0, t) for i, t in enumerate(texts)]

    def test_phrase_not_present_returns_empty(self):
        m = _get_module()
        segs = self._segs("hello world", "great episode today")
        assert m.check_phrases_absent(segs, ["use code", "promo"]) == []

    def test_phrase_present_returns_violation(self):
        m = _get_module()
        segs = self._segs("use code HELLO for 20% off", "back to the show")
        result = m.check_phrases_absent(segs, ["use code"])
        assert len(result) == 1
        assert result[0]["phrase"] == "use code"

    def test_case_insensitive(self):
        m = _get_module()
        segs = self._segs("Sign Up Today for free")
        result = m.check_phrases_absent(segs, ["sign up today"])
        assert len(result) == 1

    def test_empty_phrases_list(self):
        m = _get_module()
        assert m.check_phrases_absent(self._segs("anything"), []) == []

    def test_empty_segments(self):
        m = _get_module()
        assert m.check_phrases_absent([], ["promo"]) == []


# ---------------------------------------------------------------------------
# check_phrases_present
# ---------------------------------------------------------------------------

class TestCheckPhrasesPresent:
    def _segs(self, *texts):
        return [_transcript_seg(i * 10.0, (i + 1) * 10.0, t) for i, t in enumerate(texts)]

    def test_phrase_present_returns_empty(self):
        m = _get_module()
        segs = self._segs("welcome to the show", "today we discuss markets")
        assert m.check_phrases_present(segs, ["welcome to the show"]) == []

    def test_missing_phrase_returned(self):
        m = _get_module()
        segs = self._segs("hello everyone")
        result = m.check_phrases_present(segs, ["markets discussion"])
        assert "markets discussion" in result

    def test_case_insensitive(self):
        m = _get_module()
        segs = self._segs("The Market Opened Higher Today")
        assert m.check_phrases_present(segs, ["market opened higher"]) == []

    def test_empty_segments_all_missing(self):
        m = _get_module()
        result = m.check_phrases_present([], ["some phrase"])
        assert "some phrase" in result


# ---------------------------------------------------------------------------
# check_duration_reduction
# ---------------------------------------------------------------------------

class TestCheckDurationReduction:
    def test_within_tolerance_passes(self):
        m = _get_module()
        with patch.object(m, "get_audio_duration", side_effect=[1800.0, 1680.0]):
            ok, msg = m.check_duration_reduction("orig.mp3", "clean.mp3", 90.0, 60.0)
        assert ok is True
        assert "deviation=30s" in msg

    def test_exceeds_tolerance_fails(self):
        m = _get_module()
        with patch.object(m, "get_audio_duration", side_effect=[1800.0, 1680.0]):
            ok, msg = m.check_duration_reduction("orig.mp3", "clean.mp3", 30.0, 60.0)
        assert ok is False
        assert "exceeds" in msg

    def test_cleaned_longer_than_original_fails(self):
        m = _get_module()
        with patch.object(m, "get_audio_duration", side_effect=[1800.0, 1900.0]):
            ok, msg = m.check_duration_reduction("orig.mp3", "clean.mp3", 60.0, 60.0)
        assert ok is False
        assert "LONGER" in msg

    def test_zero_expected_within_tolerance(self):
        m = _get_module()
        with patch.object(m, "get_audio_duration", side_effect=[1800.0, 1800.0]):
            ok, _ = m.check_duration_reduction("orig.mp3", "clean.mp3", 0.0, 60.0)
        assert ok is True

    def test_exactly_at_tolerance_boundary_passes(self):
        m = _get_module()
        with patch.object(m, "get_audio_duration", side_effect=[1800.0, 1640.0]):
            ok, _ = m.check_duration_reduction("orig.mp3", "clean.mp3", 100.0, 60.0)
        assert ok is True
