"""The red-team suite's collection-time check (M7 PR 2; scoring rules, "CI semantics").

pytest's JUnit report drops an xfail's `strict` and `raises`, so the gate script cannot see them.
This hook checks every collected test under tests/redteam/ before anything runs: its xfail, if
any, is the one `marks.redteam_param` makes (`marks.mark_problems`). A wrong mark stops the run.
"""

from pathlib import Path
from typing import Final

import pytest

from tests.redteam.marks import mark_problems

HERE: Final = Path(__file__).resolve().parent


def _case_id(item: pytest.Item) -> str | None:
    callspec = getattr(item, "callspec", None)
    value = None if callspec is None else callspec.params.get("case_id")
    return value if isinstance(value, str) else None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    problems = [
        f"{item.nodeid}: {problem}"
        for item in items
        if HERE in item.path.resolve().parents
        for problem in mark_problems(_case_id(item), item.iter_markers())
    ]
    if problems:
        raise pytest.UsageError("red-team marks:\n" + "\n".join(problems))
