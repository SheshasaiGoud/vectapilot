"""M7 PR 2: a deliberately failing canary case leaks no generated value (scoring rules, "No
PII-shaped literal is ever committed").

A child pytest runs one test (written to tmp_path, TEST-ONLY) that sends the frozen case with the
most placeholders through `run_case` with a stand-in runner that receives the filled input and
returns a wrong outcome, so the scorer raises `ExpectationMiss`. Its terminal output and its
JUnit report (captured logs included) must contain the case id and the codes, and none of the
values the generator filled in, in any form a log can hold (`pii.leak_forms`: raw,
percent-encoded, JSON-escaped). The positive control: the child really failed on that case.
"""

import subprocess
import sys
from pathlib import Path
from typing import Final

from tests.redteam.suite import CASES, GENERATOR
from vectapilot_api.redteam.pii import PLACEHOLDER, leak_forms

API_DIR: Final = Path(__file__).resolve().parents[2]
CANARY: Final = max(CASES.values(), key=lambda c: len(PLACEHOLDER.findall(c.input)))

DATASET: Final = API_DIR.parents[1] / "evals" / "datasets" / "redteam" / "v0.1"
CHILD: Final = f"""
import logging
from pathlib import Path

import pytest

from vectapilot_api.guards.types import DecidedBy
from vectapilot_api.redteam.checks import generator_of
from vectapilot_api.redteam.files import read_dataset
from vectapilot_api.redteam.harness import RunEnv, run_case
from vectapilot_api.redteam.plan import Build, path_of
from vectapilot_api.redteam.scoring import CaseResult

DATASET = read_dataset(Path({str(DATASET)!r}))
CASES = {{case.id: case for case in DATASET.cases}}
WRITTEN = {{Build.H1: lambda: True, Build.H2: lambda: True, Build.H3: lambda: True}}


def careless(filled, env):
    logging.getLogger("redteam.canary").info("ran %s", filled)  # repr: the case id only
    return CaseResult(case_id=filled.case.id, outcome="allowed", decided_by=DecidedBy.NONE)


@pytest.mark.parametrize("case_id", [{CANARY.id!r}])
def test_case(case_id):
    case = CASES[case_id]
    env = RunEnv(generator_of(DATASET), "in_process")
    run_case(case, "hard", env, runners={{path_of(case): careless}}, probes=WRITTEN)
"""


VALUES: Final = [GENERATOR.fill(CANARY.id, m.group(0)) for m in PLACEHOLDER.finditer(CANARY.input)]


def child_output(tmp_path: Path, source: str, *extra: str) -> tuple[int, str]:
    """Run `source` as a child pytest (with `extra` arguments); its exit code and everything it
    wrote."""
    child = tmp_path / "test_child.py"
    child.write_text(source, "utf-8")
    report = tmp_path / "child.xml"
    # The same interpreter and fixed arguments, no shell (S603: nothing here is parsed).
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            "-o",
            "junit_logging=all",
            "--log-level=INFO",
            f"--junit-xml={report}",
            *extra,
            str(child),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        timeout=120,
    )
    return done.returncode, done.stdout + done.stderr + report.read_text("utf-8")


def test_a_failing_case_prints_codes_and_never_a_generated_value(tmp_path: Path) -> None:
    assert len(VALUES) >= 2, "a canary without values would pass by not looking"

    code, written = child_output(tmp_path, CHILD)

    assert code == 1, written[-2000:]  # the positive control: it failed...
    assert f"case {CANARY.id}: expected {CANARY.expect}" in written  # ...on the codes
    assert f"ran FilledCase({CANARY.id})" in written  # the captured log is in the report
    leaked = [
        i for i, value in enumerate(VALUES) if any(form in written for form in leak_forms(value))
    ]
    assert leaked == [], f"generated values {leaked} (by index) reached the output"


def test_red_proof_a_runner_that_logs_the_filled_input_is_caught(tmp_path: Path) -> None:
    careless = CHILD.replace('"ran %s", filled)', '"ran %s", filled.input)')
    assert careless != CHILD

    _, written = child_output(tmp_path, careless)

    assert all(value in written for value in VALUES)
