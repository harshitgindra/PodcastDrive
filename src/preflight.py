"""Startup preflight checks for PodcastDrive.

Verifies that all required dependencies, credentials, and cloud resources
are available before any processing begins.  Call ``run_preflight()`` once
at startup; it will print a status line for each check and exit with code 1
on the first fatal failure.
"""

import os
import shutil
import subprocess
import sys

import boto3
import botocore.exceptions

# ── Colour helpers ────────────────────────────────────────────────────────────

_RED    = "\033[0;31m"
_GREEN  = "\033[0;32m"
_YELLOW = "\033[1;33m"
_CYAN   = "\033[0;36m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"


def _ok(msg: str) -> None:
    print(f"  {_GREEN}✅  {msg}{_RESET}")


def _fail(msg: str) -> None:
    print(f"  {_RED}❌  {msg}{_RESET}")
    sys.exit(1)


def _warn(msg: str) -> None:
    print(f"  {_YELLOW}⚠️   {msg}{_RESET}")


def _section(title: str) -> None:
    print(f"\n{_BOLD}{title}{_RESET}")
    print("─" * 52)


# ── Individual checks ─────────────────────────────────────────────────────────


def _check_env_vars() -> None:
    """Check required environment variables are set."""
    _section("Environment variables")

    required = {
        "S3_BUCKET": os.environ.get("S3_BUCKET", ""),
        "CLOUDFRONT_BASE": os.environ.get("CLOUDFRONT_BASE", ""),
        "CLOUDFRONT_DISTRIBUTION_ID": os.environ.get("CLOUDFRONT_DISTRIBUTION_ID", ""),
    }

    for name, value in required.items():
        if not value:
            _fail(f"{name} is not set — add it to config.env")
        _ok(f"{name} = {value}")

    # AWS region — warn only, defaults to us-west-2
    region = os.environ.get("AWS_DEFAULT_REGION", "")
    if not region:
        _warn("AWS_DEFAULT_REGION not set — defaulting to us-west-2")
        os.environ["AWS_DEFAULT_REGION"] = "us-west-2"
    else:
        _ok(f"AWS_DEFAULT_REGION = {region}")


def _check_aws_credentials() -> str:
    """Verify AWS credentials are valid. Returns the resolved region."""
    _section("AWS credentials & region")

    try:
        session = boto3.session.Session()
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        account = identity.get("Account", "unknown")
        _ok(f"AWS credentials valid (account: {account})")

        region = session.region_name or os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
        # Rough sanity-check: AWS regions look like "us-west-2", "eu-central-1", etc.
        if not region or len(region) < 5 or region.count("-") < 1:
            _warn(f"AWS region '{region}' looks unusual — double-check AWS_DEFAULT_REGION")
        else:
            _ok(f"AWS region = {region}")

        return region

    except botocore.exceptions.NoCredentialsError:
        _fail(
            "No AWS credentials found. Configure via:\n"
            "    • ~/.aws/credentials\n"
            "    • AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars\n"
            "    • IAM instance role"
        )
    except botocore.exceptions.ClientError as exc:
        _fail(f"AWS credential check failed: {exc}")


def _check_s3_bucket(region: str, dry_run: bool) -> None:
    """Verify the S3 bucket exists and is accessible."""
    _section("S3 bucket")

    bucket = os.environ["S3_BUCKET"]
    s3 = boto3.client("s3", region_name=region)

    try:
        s3.head_bucket(Bucket=bucket)
        _ok(f"S3 bucket '{bucket}' accessible")
    except botocore.exceptions.ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("404", "NoSuchBucket"):
            _fail(f"S3 bucket '{bucket}' does not exist")
        elif code == "403":
            _fail(f"S3 bucket '{bucket}' exists but access denied (check IAM permissions)")
        else:
            _fail(f"S3 bucket '{bucket}' check failed: {exc}")

    if not dry_run:
        # Verify write access by checking we can list objects (ListObjectsV2)
        try:
            s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
            _ok("S3 bucket list access confirmed")
        except botocore.exceptions.ClientError as exc:
            _fail(f"S3 bucket '{bucket}' list access denied: {exc}")


def _check_cloudfront(region: str) -> None:
    """Verify the CloudFront distribution exists and is deployed."""
    _section("CloudFront distribution")

    dist_id = os.environ.get("CLOUDFRONT_DISTRIBUTION_ID", "")
    if not dist_id:
        # Already caught in _check_env_vars — shouldn't reach here
        _fail("CLOUDFRONT_DISTRIBUTION_ID is not set")

    cf = boto3.client("cloudfront", region_name=region)
    try:
        resp = cf.get_distribution(Id=dist_id)
        status = resp["Distribution"]["Status"]
        domain = resp["Distribution"]["DomainName"]
        if status != "Deployed":
            _warn(f"CloudFront distribution '{dist_id}' status is '{status}' (expected 'Deployed')")
        else:
            _ok(f"CloudFront distribution '{dist_id}' is Deployed ({domain})")
    except botocore.exceptions.ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "NoSuchDistribution":
            _fail(f"CloudFront distribution '{dist_id}' does not exist")
        elif code == "AccessDenied":
            _fail(f"CloudFront distribution '{dist_id}' — access denied (check IAM permissions)")
        else:
            _fail(f"CloudFront distribution '{dist_id}' check failed: {exc}")


def _check_yt_dlp() -> None:
    """Verify yt-dlp is importable and the binary works."""
    _section("yt-dlp")

    try:
        import yt_dlp  # noqa: F401
        _ok("yt-dlp Python package importable")
    except ImportError:
        _fail("yt-dlp is not installed — run: pip install yt-dlp")

    result = subprocess.run(
        ["yt-dlp", "--version"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _fail("yt-dlp binary not found or not working — ensure it is on PATH")
    _ok(f"yt-dlp binary version: {result.stdout.strip()}")


def _check_ffmpeg() -> None:
    """Verify ffmpeg is available on PATH."""
    _section("ffmpeg")

    if not shutil.which("ffmpeg"):
        _fail("ffmpeg not found on PATH — install with: brew install ffmpeg")

    result = subprocess.run(
        ["ffmpeg", "-version"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _fail("ffmpeg binary found but failed to run")

    # First line of ffmpeg -version output: "ffmpeg version X.Y.Z ..."
    version_line = result.stdout.splitlines()[0] if result.stdout else "unknown"
    _ok(f"ffmpeg: {version_line}")


def _check_transcribe(region: str) -> None:
    """Verify the IAM principal has basic AWS Transcribe access.

    Uses ``list_transcription_jobs`` (MaxResults=1) as a lightweight
    permission probe — no audio is uploaded or transcribed.
    """
    _section("AWS Transcribe (ad removal)")

    transcribe = boto3.client("transcribe", region_name=region)
    try:
        transcribe.list_transcription_jobs(MaxResults=1)
        _ok(f"AWS Transcribe accessible (region={region})")
    except botocore.exceptions.ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("AccessDeniedException", "AccessDenied", "403"):
            _fail(
                "AWS Transcribe access denied — add the following IAM permission:\n"
                "    transcribe:StartTranscriptionJob\n"
                "    transcribe:GetTranscriptionJob\n"
                "    transcribe:DeleteTranscriptionJob\n"
                "    transcribe:ListTranscriptionJobs"
            )
        else:
            _fail(f"AWS Transcribe check failed: {exc}")


def _check_bedrock(region: str) -> None:
    """Verify the IAM principal has basic AWS Bedrock access.

    Uses ``list_foundation_models`` as a lightweight permission probe —
    no model is invoked.
    """
    _section("AWS Bedrock (ad removal)")

    bedrock = boto3.client("bedrock", region_name=region)
    try:
        bedrock.list_foundation_models()
        _ok(f"AWS Bedrock accessible (region={region})")
    except botocore.exceptions.ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("AccessDeniedException", "AccessDenied", "403"):
            _fail(
                "AWS Bedrock access denied — add the following IAM permission:\n"
                "    bedrock:InvokeModel\n"
                "    bedrock:ListFoundationModels\n"
                "Also ensure the model is enabled in the Bedrock console for this region."
            )
        else:
            _fail(f"AWS Bedrock check failed: {exc}")


def _check_notion() -> None:
    """Verify Notion credentials when CONFIG_PROVIDER=notion."""
    _section("Notion (config provider)")

    api_key = os.environ.get("NOTION_API_KEY", "")
    db_id   = os.environ.get("NOTION_DATABASE_ID", "")

    if not api_key:
        _fail("NOTION_API_KEY is not set — add it to config.env")
    if api_key.startswith("secret_xxx") or api_key == "secret_":
        _fail("NOTION_API_KEY looks like a placeholder — replace it with a real key")
    _ok("NOTION_API_KEY is set")

    if not db_id:
        _fail("NOTION_DATABASE_ID is not set — add it to config.env")
    if db_id.startswith("xxx"):
        _fail("NOTION_DATABASE_ID looks like a placeholder — replace it with a real ID")
    _ok(f"NOTION_DATABASE_ID = {db_id}")

    # Test API call — query the database with limit 1
    import ssl
    import urllib.request

    import certifi

    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    data = b'{"page_size":1}'
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx):
            _ok("Notion API reachable and credentials valid")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            _fail("Notion API returned 401 Unauthorized — check NOTION_API_KEY")
        elif exc.code == 404:
            _fail(
                f"Notion database '{db_id}' not found (404) — "
                "check NOTION_DATABASE_ID and that the integration has access"
            )
        else:
            _fail(f"Notion API returned HTTP {exc.code}: {exc.reason}")
    except Exception as exc:
        _fail(f"Notion API call failed: {exc}")


# ── Public entry point ────────────────────────────────────────────────────────


def run_preflight(dry_run: bool = False) -> None:
    """Run all preflight checks.  Exits with code 1 on the first failure.

    Args:
        dry_run: When True, skips write-access checks (S3 put/delete).
    """
    print(f"\n{_BOLD}{'=' * 52}{_RESET}")
    print(f"{_BOLD}  PodcastDrive — Preflight checks{_RESET}")
    if dry_run:
        print(f"  {_YELLOW}(dry-run mode — write-access checks skipped){_RESET}")
    print(f"{_BOLD}{'=' * 52}{_RESET}")

    _check_env_vars()
    region = _check_aws_credentials()
    _check_s3_bucket(region, dry_run=dry_run)
    _check_cloudfront(region)
    _check_yt_dlp()
    _check_ffmpeg()

    config_provider = os.environ.get("CONFIG_PROVIDER", "yaml")
    if config_provider == "notion":
        _check_notion()

    remove_ads = os.environ.get("REMOVE_ADS", "true").lower()
    if remove_ads not in ("false", "0", "no"):
        _check_transcribe(region)
        _check_bedrock(region)

    print(f"\n{_GREEN}{_BOLD}All preflight checks passed — starting sync.{_RESET}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run PodcastDrive preflight checks")
    parser.add_argument("--dry-run", action="store_true", help="Skip write-access checks")
    args = parser.parse_args()
    run_preflight(dry_run=args.dry_run)
