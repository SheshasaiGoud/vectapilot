"""conftest's CI guard: on GitHub Actions a run without RUN_INTEGRATION=1 must fail, not skip.

Otherwise CI could go green with every database test skipped (ADR 0009's protections untested).
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from tests.conftest import CI_WITHOUT_INTEGRATION, integration_skipped_in_ci

API_DIR: Final = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("environ", "skipped"),
    [
        ({"GITHUB_ACTIONS": "true"}, True),
        ({"GITHUB_ACTIONS": "true", "RUN_INTEGRATION": "0"}, True),
        ({"GITHUB_ACTIONS": "true", "RUN_INTEGRATION": "true"}, True),  # only "1" runs them
        ({"GITHUB_ACTIONS": "true", "RUN_INTEGRATION": "1"}, False),
        ({}, False),  # a developer's machine may skip them
        ({"RUN_INTEGRATION": "1"}, False),
    ],
)
def test_integration_skipped_in_ci(environ: dict[str, str], skipped: bool) -> None:
    assert integration_skipped_in_ci(environ) is skipped


def collect_one_test(run_integration: str) -> subprocess.CompletedProcess[str]:
    """A real pytest run (collection only, nothing connects) as GitHub Actions would start it."""
    environ = os.environ | {"GITHUB_ACTIONS": "true", "RUN_INTEGRATION": run_integration}
    target = f"tests/{Path(__file__).name}::test_integration_skipped_in_ci"
    # Fixed arguments, no shell (S603: the arguments are ours).
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", target],
        cwd=API_DIR,
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_ci_without_run_integration_fails_the_session() -> None:
    done = collect_one_test(run_integration="0")

    assert done.returncode == pytest.ExitCode.USAGE_ERROR, done.stdout + done.stderr
    assert CI_WITHOUT_INTEGRATION in done.stderr
    assert "collected" not in done.stdout  # stopped before collection


def test_ci_with_run_integration_runs() -> None:
    done = collect_one_test(run_integration="1")

    assert done.returncode == pytest.ExitCode.OK, done.stdout + done.stderr
