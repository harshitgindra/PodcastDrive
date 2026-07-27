"""Unit tests for src/summary_generator.py.

Covers generate_episode_summary(): happy path, edge cases, truncation,
model selection, and failure handling.  All AWS calls are mocked.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from summary_generator import generate_episode_summary

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_segments(text: str = "Hello, welcome to the show. Today we talk about AI.") -> list[dict]:
    return [{"start": 0.0, "end": 5.0, "text": text}]


def _make_client(response_text: str) -> MagicMock:
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": response_text}]}}
    }
    return client


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestGenerateEpisodeSummaryHappyPath:
    def test_returns_bedrock_text(self, monkeypatch):
        """Bedrock response text is returned as-is (stripped)."""
        client = _make_client("  A great episode about AI.  ")
        with patch("summary_generator.boto3") as mock_boto3, \
             patch("summary_generator.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            mock_boto3.client.return_value = client
            result = generate_episode_summary(_make_segments(), "AI Today")
        assert result == "A great episode about AI."

    def test_episode_title_appears_in_prompt(self, monkeypatch):
        """The episode title is embedded in the Bedrock prompt."""
        captured: list[dict] = []

        def fake_converse(**kwargs):
            captured.append(kwargs)
            return {"output": {"message": {"content": [{"text": "summary"}]}}}

        client = MagicMock()
        client.converse.side_effect = fake_converse
        with patch("summary_generator.boto3") as mock_boto3, \
             patch("summary_generator.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            mock_boto3.client.return_value = client
            generate_episode_summary(_make_segments(), "Tech Deep Dive")

        prompt = captured[0]["messages"][0]["content"][0]["text"]
        assert "Tech Deep Dive" in prompt

    def test_transcript_text_appears_in_prompt(self, monkeypatch):
        """Transcript text from segments is included in the prompt."""
        captured: list[dict] = []

        def fake_converse(**kwargs):
            captured.append(kwargs)
            return {"output": {"message": {"content": [{"text": "summary"}]}}}

        client = MagicMock()
        client.converse.side_effect = fake_converse
        with patch("summary_generator.boto3") as mock_boto3, \
             patch("summary_generator.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            mock_boto3.client.return_value = client
            generate_episode_summary(
                [{"start": 0.0, "end": 5.0, "text": "unique phrase xyz789"}],
                "Episode"
            )

        prompt = captured[0]["messages"][0]["content"][0]["text"]
        assert "unique phrase xyz789" in prompt


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestGenerateEpisodeSummaryEdgeCases:
    def test_empty_segments_returns_empty_string(self):
        """No segments → return '' without calling Bedrock."""
        result = generate_episode_summary([], "Episode")
        assert result == ""

    def test_truncates_long_transcript_to_40k_chars(self, monkeypatch):
        """Transcripts longer than 40,000 chars are truncated before the prompt."""
        captured: list[dict] = []

        def fake_converse(**kwargs):
            captured.append(kwargs)
            return {"output": {"message": {"content": [{"text": "summary"}]}}}

        client = MagicMock()
        client.converse.side_effect = fake_converse
        long_text = "word " * 20_000  # ~100k chars
        with patch("summary_generator.boto3") as mock_boto3, \
             patch("summary_generator.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            mock_boto3.client.return_value = client
            generate_episode_summary(
                [{"start": 0.0, "end": 5.0, "text": long_text}], "Long Episode"
            )

        prompt = captured[0]["messages"][0]["content"][0]["text"]
        # The transcript section after the "Transcript:\n" marker must be ≤ 40k chars
        transcript_part = prompt.split("Transcript:\n", 1)[1]
        assert len(transcript_part) <= 40_000

    def test_multiple_segments_joined(self, monkeypatch):
        """Multiple segments are joined with spaces into a single text block."""
        captured: list[dict] = []

        def fake_converse(**kwargs):
            captured.append(kwargs)
            return {"output": {"message": {"content": [{"text": "summary"}]}}}

        client = MagicMock()
        client.converse.side_effect = fake_converse
        with patch("summary_generator.boto3") as mock_boto3, \
             patch("summary_generator.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            mock_boto3.client.return_value = client
            generate_episode_summary(
                [
                    {"start": 0.0, "end": 5.0, "text": "first"},
                    {"start": 5.0, "end": 10.0, "text": "second"},
                ],
                "Episode",
            )

        prompt = captured[0]["messages"][0]["content"][0]["text"]
        assert "first second" in prompt


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

class TestGenerateEpisodeSummaryModel:
    def test_uses_bedrock_model_id_env_var(self, monkeypatch):
        """BEDROCK_MODEL_ID env var is used as the model."""
        captured: list[dict] = []

        def fake_converse(**kwargs):
            captured.append(kwargs)
            return {"output": {"message": {"content": [{"text": "ok"}]}}}

        client = MagicMock()
        client.converse.side_effect = fake_converse
        with patch("summary_generator.boto3") as mock_boto3, \
             patch("summary_generator.retry_aws_call", side_effect=lambda fn, **kw: fn()), \
             patch.dict(os.environ, {"BEDROCK_MODEL_ID": "my-custom-model-v1"}):
            mock_boto3.client.return_value = client
            generate_episode_summary(_make_segments(), "Episode")

        assert captured[0]["modelId"] == "my-custom-model-v1"

    def test_falls_back_to_claude_sonnet_when_env_unset(self, monkeypatch):
        """Defaults to Claude Sonnet when BEDROCK_MODEL_ID is not set."""
        captured: list[dict] = []

        def fake_converse(**kwargs):
            captured.append(kwargs)
            return {"output": {"message": {"content": [{"text": "ok"}]}}}

        client = MagicMock()
        client.converse.side_effect = fake_converse
        clean_env = {k: v for k, v in os.environ.items() if k != "BEDROCK_MODEL_ID"}
        with patch("summary_generator.boto3") as mock_boto3, \
             patch("summary_generator.retry_aws_call", side_effect=lambda fn, **kw: fn()), \
             patch.dict(os.environ, clean_env, clear=True):
            mock_boto3.client.return_value = client
            generate_episode_summary(_make_segments(), "Episode")

        assert "claude" in captured[0]["modelId"].lower()

    def test_explicit_model_id_overrides_env(self, monkeypatch):
        """Explicitly passing model_id= overrides the env var."""
        captured: list[dict] = []

        def fake_converse(**kwargs):
            captured.append(kwargs)
            return {"output": {"message": {"content": [{"text": "ok"}]}}}

        client = MagicMock()
        client.converse.side_effect = fake_converse
        with patch("summary_generator.boto3") as mock_boto3, \
             patch("summary_generator.retry_aws_call", side_effect=lambda fn, **kw: fn()), \
             patch.dict(os.environ, {"BEDROCK_MODEL_ID": "env-model"}):
            mock_boto3.client.return_value = client
            generate_episode_summary(_make_segments(), "Episode", model_id="explicit-model")

        assert captured[0]["modelId"] == "explicit-model"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestGenerateEpisodeSummaryFailures:
    def test_bedrock_exception_returns_empty_string(self, monkeypatch):
        """Any Bedrock exception returns '' without re-raising."""
        with patch("summary_generator.boto3") as mock_boto3, \
             patch("summary_generator.retry_aws_call", side_effect=RuntimeError("Bedrock down")):
            mock_boto3.client.return_value = MagicMock()
            result = generate_episode_summary(_make_segments(), "Episode")
        assert result == ""

    def test_malformed_response_returns_empty_string(self, monkeypatch):
        """Malformed Bedrock response (missing keys) returns '' without crashing."""
        client = MagicMock()
        client.converse.return_value = {}  # missing 'output' key
        with patch("summary_generator.boto3") as mock_boto3, \
             patch("summary_generator.retry_aws_call", side_effect=lambda fn, **kw: fn()):
            mock_boto3.client.return_value = client
            result = generate_episode_summary(_make_segments(), "Episode")
        assert result == ""
