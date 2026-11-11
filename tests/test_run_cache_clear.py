"""Tests for the per-podcast ad-cache clearing in run.sh.

``--clear-cache <slug>`` and ``--reprocess <slug>`` both used to list
``transcribe-cache/`` non-recursively and delete every ``*_ads.json`` they
found, ignoring the slug entirely. Two layouts coexist in that prefix: RSS
podcasts namespace their cache under ``transcribe-cache/<slug>/``, and YouTube
playlists predate namespacing and still write flat keys named after the video
id. So asking for one podcast silently threw away every *other* podcast's
Bedrock results, and (because the listing was not recursive) frequently did not
touch the requested podcast's own entries at all.

The shell is where this runs, so the real functions are extracted verbatim from
``run.sh`` and exercised against a stub ``aws`` rather than asserted on as text.

What must hold:
  * a namespaced podcast loses its own entries under its prefix, and nothing else;
  * a flat-layout playlist loses only the keys named after episodes it owns;
  * revision-suffixed objects (``<id>.rN.mp3``) map back to the base episode id;
  * transcripts and summaries are never deleted — they are what makes a repair cheap.
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

RUN_SH = Path(__file__).resolve().parent.parent / "run.sh"
RUN_SH_TEXT = RUN_SH.read_text(encoding="utf-8")

BUCKET = "test-bucket"


def _function(name: str) -> str:
    """Return the body of shell function *name*, extracted from run.sh."""
    start = RUN_SH_TEXT.index(f"{name}() {{")
    end = RUN_SH_TEXT.index("\n}\n", start) + len("\n}\n")
    return RUN_SH_TEXT[start:end]


@pytest.fixture
def harness(tmp_path):
    """Build a runnable script around the real cache-clearing helpers.

    *keys* seeds a fake bucket; the stub ``aws`` answers ``s3 ls`` from it and
    appends every ``s3 rm`` target to ``deletions.txt`` instead of calling AWS.
    """

    def _build(keys: list[str]) -> Path:
        (tmp_path / "keys.txt").write_text("\n".join(keys) + "\n", encoding="utf-8")
        deletions = tmp_path / "deletions.txt"
        deletions.write_text("", encoding="utf-8")

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        aws = bin_dir / "aws"
        # `aws s3 ls s3://bucket/prefix/` lists one level and collapses deeper
        # keys into `PRE dir/` rows, exactly like the real CLI; --recursive
        # lists every key under the prefix. Columns match the real output, so
        # the awk '{print $4}' in run.sh is being tested for real.
        aws.write_text(
            textwrap.dedent(f"""\
                #!/bin/bash
                KEYS="{tmp_path}/keys.txt"
                DELETIONS="{deletions}"
                if [ "$1" = "s3" ] && [ "$2" = "rm" ]; then
                    echo "${{3#s3://{BUCKET}/}}" >> "$DELETIONS"
                    exit 0
                fi
                if [ "$1" = "s3" ] && [ "$2" = "ls" ]; then
                    PREFIX="${{3#s3://{BUCKET}/}}"
                    RECURSIVE=no
                    for arg in "$@"; do
                        [ "$arg" = "--recursive" ] && RECURSIVE=yes
                    done
                    while IFS= read -r key; do
                        case "$key" in
                            "$PREFIX"*) ;;
                            *) continue ;;
                        esac
                        REST="${{key#"$PREFIX"}}"
                        if [ "$RECURSIVE" = yes ]; then
                            echo "2026-01-01 00:00:00        10 $key"
                        elif [ "${{REST%%/*}}" = "$REST" ]; then
                            echo "2026-01-01 00:00:00        10 $REST"
                        else
                            echo "                           PRE ${{REST%%/*}}/"
                        fi
                    done < "$KEYS" | sort -u
                    exit 0
                fi
                exit 1
                """),
            encoding="utf-8",
        )
        aws.chmod(0o755)

        script = tmp_path / "harness.sh"
        script.write_text(
            "#!/bin/bash\nset -uo pipefail\n"
            f'S3_BUCKET="{BUCKET}"\n'
            'info() { echo "INFO $*"; }\n'
            + _function("episode_ids_for_slug")
            + _function("clear_ad_cache_for_slug")
            + 'IDS="$(episode_ids_for_slug "$1")"\n'
            'printf "IDS:%s\\n" "$(echo "$IDS" | tr "\\n" ",")"\n'
            'clear_ad_cache_for_slug "$1" "$IDS"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    return _build


def _run(script: Path, slug: str) -> tuple[str, list[str]]:
    env = {**os.environ, "PATH": f"{script.parent / 'bin'}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        ["bash", str(script), slug],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    deleted = [line for line in (script.parent / "deletions.txt").read_text().splitlines() if line]
    return result.stdout, deleted


# A bucket holding both layouts: one namespaced RSS podcast, two flat-layout
# YouTube playlists, plus the transcripts that must survive.
KEYS = [
    "cold-call/episodes/tag_hbr_2026-11-01.mp3",
    "cold-call/episodes/tag_hbr_2026-10-25.r2.mp3",
    "transcribe-cache/cold-call/tag_hbr_2026-11-01_ads.json",
    "transcribe-cache/cold-call/tag_hbr_2026-11-01.json",
    "transcribe-cache/cold-call/tag_hbr_2026-11-01.txt",
    "transcribe-cache/cold-call/tag_hbr_2026-10-25_ads.json",
    "@pamphlet_in/episodes/vid000000001.mp3",
    "@pamphlet_in/episodes/vid000000002.r1.mp3",
    "transcribe-cache/vid000000001_ads.json",
    "transcribe-cache/vid000000001.json",
    "transcribe-cache/vid000000002_ads.json",
    "transcribe-cache/vid000000002_summary.txt",
    "@johnnyharris/episodes/other0000001.mp3",
    "transcribe-cache/other0000001_ads.json",
    "transcribe-cache/other0000001.json",
]


class TestEpisodeIds:
    def test_revision_suffix_maps_back_to_the_base_episode_id(self, harness):
        stdout, _ = _run(harness(KEYS), "@pamphlet_in")

        # vid000000002.r1.mp3 is a repaired copy of vid000000002.
        assert "IDS:vid000000001,vid000000002," in stdout

    def test_slug_with_no_episodes_yields_no_ids(self, harness):
        stdout, deleted = _run(harness(KEYS), "never-synced")

        assert "IDS:," in stdout
        assert deleted == []


class TestNamespacedLayout:
    def test_clears_every_ad_cache_under_the_slug_prefix(self, harness):
        _, deleted = _run(harness(KEYS), "cold-call")

        assert sorted(deleted) == [
            "transcribe-cache/cold-call/tag_hbr_2026-10-25_ads.json",
            "transcribe-cache/cold-call/tag_hbr_2026-11-01_ads.json",
        ]

    def test_leaves_other_podcasts_alone(self, harness):
        _, deleted = _run(harness(KEYS), "cold-call")

        assert not [key for key in deleted if "pamphlet" in key or "other" in key]


class TestFlatLayout:
    def test_clears_only_the_playlists_own_episode_ids(self, harness):
        _, deleted = _run(harness(KEYS), "@pamphlet_in")

        assert sorted(deleted) == [
            "transcribe-cache/vid000000001_ads.json",
            "transcribe-cache/vid000000002_ads.json",
        ]

    def test_another_playlists_flat_cache_survives(self, harness):
        _, deleted = _run(harness(KEYS), "@pamphlet_in")

        assert "transcribe-cache/other0000001_ads.json" not in deleted

    def test_reports_only_keys_that_existed(self, harness):
        """`aws s3 rm` succeeds on a missing key, so the count must come from a listing."""
        keys = [key for key in KEYS if key != "transcribe-cache/vid000000002_ads.json"]
        stdout, deleted = _run(harness(keys), "@pamphlet_in")

        assert deleted == ["transcribe-cache/vid000000001_ads.json"]
        assert "Cleared 1 ad-cache" in stdout


class TestTranscriptsSurvive:
    @pytest.mark.parametrize("slug", ["cold-call", "@pamphlet_in", "@johnnyharris"])
    def test_only_ads_json_is_deleted(self, harness, slug):
        _, deleted = _run(harness(KEYS), slug)

        assert deleted, f"nothing cleared for {slug}"
        assert all(key.endswith("_ads.json") for key in deleted)
