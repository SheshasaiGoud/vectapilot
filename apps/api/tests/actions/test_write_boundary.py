"""W1 and W2 in the source tree: one path to a write. Unit (reads files, runs nothing).

- **Only the confirm service imports `tools.execute`** (W1). Every module under `src/` (and the
  MCP server's folder, once it exists) is parsed and every import it makes is resolved to an
  absolute module name: `import a.b`, `from a import b`, relative imports (`from .execute`,
  `from ..tools.execute`, `from . import execute`), attribute chains through an imported package
  (`vectapilot_api.tools.execute.execute`) and `importlib.import_module` / `__import__` with a
  string. Any importer but `actions/confirm.py` fails.
- **Only `main.py` imports the pending-action store** (`actions.pending`), the only code that holds
  a confirm token, and the MCP server imports nothing of `vectapilot_api.actions` at all: a token
  can never reach an MCP result (W2). PR 4 names its agent service here explicitly.
- **Only the confirm service names `confirmed_at` and `confirm_trace_id` in SQL** (W2): any
  string literal under `src/` (docstrings aside) that mentions either column, outside
  `actions/confirm.py` and the migrations, fails, whatever the statement's form (`SET col =`,
  `SET (a, b) = (...)`, an INSERT's column list).
- **Two routes under `/v1/agent`, and only the confirm writes:** PR 4 adds `POST /v1/agent`,
  which only proposes (its service stores a pending action through the store `main.py` hands it,
  and imports neither `tools.execute` nor `actions.pending`).

These are tripwires for ordinary spellings, not a sandbox: `getattr` with a computed name
defeats any static check. The database is the other fence (migration 0006's deferred keys tie
every write row to an executed, confirmed action), and review the third. Red proofs show each
spelling above turns the checks red.
"""

import ast
import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from tests.conftest import make_settings
from tests.tools.helpers import REPO_ROOT
from vectapilot_api.main import create_app

SRC: Final = REPO_ROOT / "apps" / "api" / "src" / "vectapilot_api"
MCP_SERVER: Final = REPO_ROOT / "packages" / "mcp_server"
EXECUTE: Final = "vectapilot_api.tools.execute"
PENDING: Final = "vectapilot_api.actions.pending"
ACTIONS: Final = "vectapilot_api.actions"
CONFIRM_SERVICE: Final = SRC / "actions" / "confirm.py"
MAIN: Final = SRC / "main.py"
DYNAMIC_IMPORTS: Final = frozenset({"import_module", "__import__"})


def python_files(*roots: Path) -> Iterator[Path]:
    for root in roots:
        if root.exists():
            yield from sorted(root.rglob("*.py"))


def module_of(path: Path) -> str:
    """`path`'s dotted module name: under `src/` its real one, elsewhere a name of its own root
    (an MCP file's relative import can never resolve into `vectapilot_api`)."""
    if path.is_relative_to(SRC):
        parts = ("vectapilot_api", *path.relative_to(SRC).with_suffix("").parts)
    else:
        parts = ("mcp_server", *path.relative_to(MCP_SERVER).with_suffix("").parts)
    return ".".join(parts)


def _dotted(node: ast.expr) -> list[str] | None:
    """`a.b.c` as `["a", "b", "c"]`, or None for anything but a name and attributes."""
    names: list[str] = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return [node.id, *reversed(names)]


def imported_modules(source: str, module: str) -> tuple[set[str], set[str]]:
    """The absolute module names `source` (the module `module`) loads or reaches, and the
    strings it hands to a dynamic import."""
    is_package = module.endswith(".__init__")
    package = module.removesuffix(".__init__") if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    dynamic: set[str] = set()
    aliases: dict[str, str] = {}  # local name -> the module it is bound to
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    top = alias.name.partition(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom):
            base = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            found.add(base)
            for alias in node.names:
                found.add(f"{base}.{alias.name}")
                aliases[alias.asname or alias.name] = f"{base}.{alias.name}"
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name and name[-1] in DYNAMIC_IMPORTS:
                dynamic.update(
                    arg.value
                    for arg in node.args
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                )
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name = _dotted(node)
            if name and name[0] in aliases:
                found.add(".".join([aliases[name[0]], *name[1:]]))
    return found, dynamic


def reaches(source: str, module: str, target: str) -> bool:
    """Whether `source` imports `target` or anything in it, in any spelling above. A dynamic
    import's string counts if it names `target`'s path after `vectapilot_api` as whole dotted
    parts anywhere (`..tools.execute` too, never `tools.executor`)."""
    found, dynamic = imported_modules(source, module)
    short = re.compile(rf"(^|\.){re.escape(target.removeprefix('vectapilot_api.'))}($|\.)")
    return any(name == target or name.startswith(target + ".") for name in found) or any(
        short.search(name) for name in dynamic
    )


def importers(target: str, *roots: Path) -> list[Path]:
    return [
        path
        for path in python_files(*roots)
        if reaches(path.read_text("utf-8"), module_of(path), target)
    ]


def test_only_the_confirm_service_imports_the_execute_module() -> None:
    assert importers(EXECUTE, SRC, MCP_SERVER) == [CONFIRM_SERVICE]


def test_only_main_imports_the_pending_action_store_and_mcp_imports_no_action_module() -> None:
    assert importers(PENDING, SRC) == [MAIN]
    assert importers(ACTIONS, MCP_SERVER) == []


AGENT_LOOP: Final = "vectapilot_api.agent.loop"
TOOLS_BOOK: Final = "vectapilot_api.tools.book"


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import vectapilot_api.tools.execute as e", AGENT_LOOP),
        ("from vectapilot_api.tools.execute import execute", AGENT_LOOP),
        ("from vectapilot_api.tools import execute", AGENT_LOOP),
        ("from vectapilot_api.tools import book, execute as run_it", AGENT_LOOP),
        ("from ..tools.execute import execute, ConfirmedAction", AGENT_LOOP),
        ("from ..tools import execute", AGENT_LOOP),
        ("from .execute import execute", TOOLS_BOOK),
        ("from . import execute", TOOLS_BOOK),
        ("from . import execute", "vectapilot_api.tools.__init__"),
        ("import vectapilot_api.tools\nvectapilot_api.tools.execute.execute()", AGENT_LOOP),
        ("import vectapilot_api.tools as t\nt.execute.execute()", AGENT_LOOP),
        ("from vectapilot_api import tools\ntools.execute.ConfirmedAction", AGENT_LOOP),
        ('import importlib\nimportlib.import_module("vectapilot_api.tools.execute")', AGENT_LOOP),
        ('from importlib import import_module\nimport_module("..tools.execute", "x")', AGENT_LOOP),
        ('__import__("vectapilot_api.tools.execute")', AGENT_LOOP),
    ],
)
def test_red_proof_the_import_check_sees_every_spelling(source: str, module: str) -> None:
    assert reaches(source, module, EXECUTE), source


@pytest.mark.parametrize(
    "source",
    [
        "from vectapilot_api.tools import executor",
        "from .executor import ToolExecutor",
        "from . import executor",
        "await connection.execute(text('SELECT 1'))",
        "import vectapilot_api.tools\nvectapilot_api.tools.executor.ToolExecutor",
    ],
)
def test_the_import_check_lets_the_executor_and_connection_execute_pass(source: str) -> None:
    assert not reaches(source, AGENT_LOOP, EXECUTE), source


@pytest.mark.parametrize(
    "source",
    [
        "from vectapilot_api.actions.pending import PendingActionStore",
        "from vectapilot_api.actions import pending",
        "from vectapilot_api import actions",
        "import vectapilot_api.actions.pending as p",
        "from ..actions.pending import PendingActionStore",
    ],
)
def test_red_proof_the_store_check_sees_every_spelling(source: str) -> None:
    assert reaches(source, AGENT_LOOP, ACTIONS), source
    assert source.endswith("import actions") or reaches(source, AGENT_LOOP, PENDING), source


CONFIRM_COLUMNS: Final = re.compile(r"\b(confirmed_at|confirm_trace_id)\b")


def sql_mentions(source: str) -> list[str]:
    """The string literals in `source`, docstrings aside, that name either confirm column."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and CONFIRM_COLUMNS.search(node.value)
    ]


def test_only_the_confirm_service_names_confirmed_at_in_sql() -> None:
    setters = [
        path
        for path in python_files(SRC)
        if path.parent.name != "versions" and sql_mentions(path.read_text("utf-8"))
    ]

    assert setters == [CONFIRM_SERVICE]


@pytest.mark.parametrize(
    "source",
    [
        'X = text("UPDATE pending_actions SET confirmed_at = now()")',
        'X = text("UPDATE pending_actions SET (confirmed_at, confirm_trace_id) = (now(), :t)")',
        'X = text("UPDATE pending_actions SET confirm_trace_id=:t")',
        'X = text("INSERT INTO pending_actions (business_id, confirmed_at) VALUES (:b, now())")',
        'X = text(\n    "UPDATE pending_actions SET status = :s,"\n    " confirmed_at = now()"\n)',
    ],
)
def test_red_proof_the_column_check_sees_every_form(source: str) -> None:
    assert sql_mentions(source), source


def test_the_column_check_skips_docstrings_and_attributes() -> None:
    source = (
        '"""Sets confirmed_at."""\n\ndef f(a):\n    """confirm_trace_id."""\n    a.confirmed_at\n'
    )

    assert sql_mentions(source) == []


def test_the_routes_under_v1_agent_are_the_turn_and_the_confirm() -> None:
    paths = create_app(make_settings()).openapi()["paths"]
    routes = {path: sorted(paths[path]) for path in paths if path.startswith("/v1/agent")}

    assert routes == {
        "/v1/agent": ["post"],
        "/v1/agent/actions/{action_id}/confirm": ["post"],
    }
