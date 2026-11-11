"""The `mcp` job's gate (scripts/integration_gate.py): red unless integration tests ran."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

GATE_PATH: Final = Path(__file__).resolve().parents[1] / "scripts" / "integration_gate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("integration_gate", GATE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("integration_gate", module)
    spec.loader.exec_module(module)
    return module


gate = _load()


def report(*cases: str) -> str:
    return f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>"


def case(classname: str, name: str = "test_x", inner: str = "") -> str:
    return f'<testcase classname="{classname}" name="{name}">{inner}</testcase>'


def test_passes_when_an_integration_test_passed() -> None:
    counts = gate.suite_counts(
        report(case("tests.integration.test_server_db"), case("tests.test_server"))
    )
    assert counts.passed == 1
    assert gate.problems(counts) == []


@pytest.mark.parametrize(
    "xml",
    [
        report(case("tests.test_server")),  # none ran: only unit tests
        report(case("tests.integration.test_server_db", inner='<skipped message="no db"/>')),
        report(
            case("tests.integration.test_a"),
            case("tests.integration.test_b", inner='<skipped type="pytest.xfail"/>'),
        ),
        report(),
    ],
    ids=["unit-only", "all-skipped", "one-xfailed", "empty"],
)
def test_fails_closed(xml: str) -> None:
    assert gate.problems(gate.suite_counts(xml)) != []


def test_main_exit_codes(tmp_path: Path) -> None:
    good, bad = tmp_path / "good.xml", tmp_path / "bad.xml"
    good.write_text(report(case("tests.integration.test_a")), encoding="utf-8")
    bad.write_text(report(case("tests.test_server")), encoding="utf-8")
    assert gate.main(["gate", str(good)]) == 0
    assert gate.main(["gate", str(bad)]) == 1
    assert gate.main(["gate"]) == 1
