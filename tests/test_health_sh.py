"""health.sh argument parsing, run against a stub Python that echoes its argv."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def run_health(tmp_path):
    shutil.copy(REPO_ROOT / "health.sh", tmp_path / "health.sh")
    stub = tmp_path / ".venv" / "bin" / "python3"
    stub.parent.mkdir(parents=True)
    stub.write_text('#!/bin/bash\necho "ARGS: $*"\n')
    stub.chmod(0o755)

    def run(*args):
        return subprocess.run(
            ["bash", str(tmp_path / "health.sh"), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    return run


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((), "ARGS: -m health_report --days 7 --output md"),
        (("--days", "14"), "ARGS: -m health_report --days 14 --output md"),
        (("--json", "--days", "14"), "ARGS: -m health_report --days 14 --output json"),
        (("--days", "3", "--json"), "ARGS: -m health_report --days 3 --output json"),
        (("--heal",), "ARGS: -m self_heal --dry-run"),
        (("--apply", "--heal"), "ARGS: -m self_heal"),
    ],
)
def test_options_parse_in_any_order(run_health, args, expected):
    result = run_health(*args)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == expected


@pytest.mark.parametrize("args", [("--days",), ("--days", "abc"), ("--days", "0"), ("--bogus",)])
def test_invalid_options_exit_2(run_health, args):
    result = run_health(*args)
    assert result.returncode == 2
    assert "ARGS:" not in result.stdout
    assert "health.sh:" in result.stderr


def test_help_exits_0(run_health):
    result = run_health("--help")
    assert result.returncode == 0
    assert "Usage: ./health.sh" in result.stdout
