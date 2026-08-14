"""Generate a short episode summary from a transcript using AWS Bedrock."""

import logging
import os

import boto3

from utils import retry_aws_call

logger = logging.getLogger(__name__)


def generate_episode_summary(
    segments: list[dict],
    episode_title: str,
    model_id: str | None = None,
) -> str:
    """Summarise a podcast episode transcript in 2–4 sentences.

    Args:
        segments: Transcript segment list (each has "start", "end", "text").
        episode_title: Episode title for context.
        model_id: Bedrock model ID. Defaults to BEDROCK_MODEL_ID env var,
                  then "us.anthropic.claude-sonnet-4-6".

    Returns:
        A 2–4 sentence plain-text summary, or "" on any failure.
    """
    if not segments:
        return ""

    if model_id is None:
        model_id = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

    full_text = " ".join(s["text"] for s in segments)
    # Truncate to 40,000 chars to stay within context limits
    full_text = full_text[:40_000]

    prompt = (
        f'You are summarising a podcast episode titled "{episode_title}".\n\n'
        "Read the transcript below and write a 2–4 sentence plain-text summary\n"
        "describing what the episode covers. Be concise and factual. Do not mention\n"
        "ads or sponsor segments. Do not use bullet points. Output only the summary.\n\n"
        f"Transcript:\n{full_text}"
    )

    try:
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        client = boto3.client("bedrock-runtime", region_name=region)

        response = retry_aws_call(
            lambda: client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 256, "temperature": 0.3},
            ),
            label="bedrock.converse[summary]",
            max_attempts=3,
        )

        text = response["output"]["message"]["content"][0]["text"].strip()
        logger.info("[Summary] Generated summary for '%s' (%d chars)", episode_title, len(text))
        return text

    except Exception as exc:
        logger.warning("[Summary] Failed to generate summary for '%s': %s", episode_title, exc)
        return ""
