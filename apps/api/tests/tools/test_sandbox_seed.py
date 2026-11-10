"""M5 PR 1: the seed model and the reset's statements, without a database.

The reset runs as the owner, which skips RLS: what keeps it inside one business is the text of its
statements. So these read that text. The database proof is `test_sandbox_reset.py`.
"""

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any, Final, get_args

import pytest
from pydantic import ValidationError
from sqlalchemy import TextClause

from tests.tools.helpers import SANDBOX_DIR, read_json
from vectapilot_api.business import HEADER_ENVIRONMENTS
from vectapilot_api.tools import config as tools_config
from vectapilot_api.tools import sandbox
from vectapilot_api.tools.config import SEED_FILE, ToolName, ToolsConfigError
from vectapilot_api.tools.sandbox import (
    APPEND_ONLY,
    DELETES,
    RESET_STATEMENTS,
    RESET_TABLES,
    SANDBOX_ENVIRONMENTS,
    OrderStatus,
    SeedOrder,
    SeedSlot,
    parse_seed,
)

API_ROOT: Final = Path(__file__).resolve().parents[2]
MIGRATION_0006: Final = API_ROOT / "migrations" / "versions" / "0006_sandbox_and_tool_tables.py"
GOOD: Final[dict[str, Any]] = read_json(SANDBOX_DIR / "paws-and-co" / SEED_FILE)


def migration_0006() -> ModuleType:
    spec = importlib.util.spec_from_file_location(MIGRATION_0006.stem, MIGRATION_0006)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_text(**changes: Any) -> str:
    return json.dumps(GOOD | changes, ensure_ascii=False)


def first(rows: str, **changes: Any) -> list[dict[str, Any]]:
    """`GOOD[rows]` with its first row changed."""
    return [GOOD[rows][0] | changes, *GOOD[rows][1:]]


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"synthetic": False}, "the sandbox seed is synthetic"),
        ({"customers": ["cust-001"]}, None),
        ({"customers": ["cust-001", "cust-001"]}, "each customer is listed once"),
        ({"slots": [*GOOD["slots"], GOOD["slots"][0]]}, "each slot id is listed once"),
        ({"bookings": first("bookings", slot_id="S-99")}, "every booking's slot is a seeded slot"),
        (
            {"bookings": [*GOOD["bookings"], GOOD["bookings"][0] | {"id": "B-999"}]},
            "at most one active booking per slot",
        ),
        (
            {"orders": first("orders", customer_ref="cust-999")},
            "every booking and order belongs to a listed customer",
        ),
        (
            {"bookings": first("bookings", customer_ref="cust-999")},
            "every booking and order belongs to a listed customer",
        ),
        ({"orders": [*GOOD["orders"], GOOD["orders"][0]]}, "each order id is listed once"),
        (
            # B-101 again, on a free slot: only the id repeats.
            {"bookings": [*GOOD["bookings"], GOOD["bookings"][0] | {"slot_id": "S-09"}]},
            "each booking id is listed once",
        ),
        ({"canary": "sbx-canary-not-in-any-row"}, "the canary appears in at least one"),
        ({"slots": first("slots", id="s-01")}, None),  # ids are upper case
        ({"slots": first("slots", starts_at="2026-11-13T17:00:00")}, None),  # no timezone
        ({"slots": first("slots", label="Vet room\n1")}, "one trimmed line"),
        ({"slots": first("slots", label="Vet room\u20281")}, "one trimmed line"),
        ({"orders": first("orders", status="refunded")}, None),
        ({"orders": first("orders", note="x" * 501)}, None),
        ({"bookings": first("bookings", status="active")}, None),  # a seed names no status
    ],
)
def test_an_invalid_seed_is_refused(changes: dict[str, Any], rule: str | None) -> None:
    with pytest.raises(ToolsConfigError) as refused:
        parse_seed(seed_text(**changes), where="biz/seed.json")

    message = str(refused.value)
    assert message.startswith("biz/seed.json is not a valid sandbox seed: ")
    if rule is not None:
        assert rule in message
    assert "cust-999" not in message and "x" * 20 not in message


def test_a_seed_needs_two_customers_on_its_own() -> None:
    # Every row belongs to the one listed customer, so only the two-customer rule can refuse it.
    one = {
        "customers": ["cust-001"],
        "bookings": [b | {"customer_ref": "cust-001"} for b in GOOD["bookings"]],
        "orders": [o | {"customer_ref": "cust-001"} for o in GOOD["orders"]],
    }
    with pytest.raises(ToolsConfigError) as refused:
        parse_seed(seed_text(**one), where="biz/seed.json")
    assert str(refused.value) == "biz/seed.json is not a valid sandbox seed: customers"


def test_a_short_canary_is_refused() -> None:
    # 16 characters at least: a short one could match other text by chance.
    with pytest.raises(ToolsConfigError) as refused:
        parse_seed(seed_text(canary="sbx-short"), where="biz/seed.json")
    assert str(refused.value) == "biz/seed.json is not a valid sandbox seed: canary"


def test_the_canary_may_sit_in_an_order_note_alone() -> None:
    canary = GOOD["canary"]
    slots = [slot | {"label": slot["label"].replace(canary, "")} for slot in GOOD["slots"]]
    slots = [slot | {"label": slot["label"].strip() or "Room"} for slot in slots]
    orders = first("orders", note=f"Leave at the door ({canary})")
    assert not any(canary in slot["label"] for slot in slots)

    seed = parse_seed(seed_text(slots=slots, orders=orders), where="biz/seed.json")

    assert canary in (seed.orders[0].note or "")


SLOT: Final[dict[str, Any]] = GOOD["slots"][0]
ORDER: Final[dict[str, Any]] = GOOD["orders"][0]
LOW, HIGH = sandbox.SLOT_DURATION_MINUTES


@pytest.mark.parametrize(
    ("model", "row", "field", "accepted", "refused"),
    [
        (SeedSlot, SLOT, "duration_minutes", [LOW, HIGH], [LOW - 1, HIGH + 1]),
        (SeedSlot, SLOT, "label", ["x" * sandbox.LABEL_MAX], ["x" * (sandbox.LABEL_MAX + 1)]),
        (SeedOrder, ORDER, "items", ["x" * sandbox.ITEMS_MAX], ["x" * (sandbox.ITEMS_MAX + 1)]),
        (SeedOrder, ORDER, "note", ["x" * sandbox.NOTE_MAX], ["x" * (sandbox.NOTE_MAX + 1)]),
    ],
)
def test_the_seed_bounds_are_exact(
    model: type[SeedSlot | SeedOrder],
    row: dict[str, Any],
    field: str,
    accepted: list[object],
    refused: list[object],
) -> None:
    # Exactly migration 0006's CHECKs (compared below): the last allowed value passes, the next
    # one is refused.
    for value in accepted:
        model.model_validate(row | {field: value})
    for value in refused:
        with pytest.raises(ValidationError):
            model.model_validate(row | {field: value})


# ── The reset's statements ───────────────────────────────────────────────────────────────────


def sql(statement: TextClause) -> str:
    return " ".join(str(statement).split())


def test_every_reset_statement_names_the_business_itself() -> None:
    for statement in RESET_STATEMENTS:
        assert ":business_id" in sql(statement), sql(statement)
    for statement, table in zip(DELETES, RESET_TABLES, strict=True):
        assert sql(statement) == f"DELETE FROM {table} WHERE business_id = :business_id"  # noqa: S608


def test_the_reset_deletes_children_first_and_never_the_audit() -> None:
    # Each table is deleted before every table it references (migration 0006's foreign keys).
    references = {
        "sandbox_tickets": {"pending_actions"},
        "sandbox_bookings": {"sandbox_slots", "pending_actions"},
        "sandbox_orders": set(),
        "pending_actions": set(),
        "sandbox_slots": set(),
    }
    order = {table: index for index, table in enumerate(RESET_TABLES)}
    assert set(order) == set(references)
    for child, parents in references.items():
        assert all(order[child] < order[parent] for parent in parents), child
    assert {"tool_audit"} == APPEND_ONLY
    statements = " ".join(sql(statement) for statement in RESET_STATEMENTS)
    assert "tool_audit" not in statements
    assert "TRUNCATE" not in statements.upper()
    assert "businesses" not in " ".join(sql(d) for d in DELETES)


def test_the_reset_runs_where_the_business_header_does() -> None:
    assert SANDBOX_ENVIRONMENTS == HEADER_ENVIRONMENTS == {"local", "test", "ci"}


def test_the_reset_tables_and_the_audit_are_the_migrations_tables() -> None:
    assert set(RESET_TABLES) | APPEND_ONLY == set(migration_0006().TABLES)


# ── Migration 0006 and the tool core agree (migrations are frozen in time) ─────────────────


def test_the_migrations_patterns_and_names_are_the_tool_cores() -> None:
    module = migration_0006()

    assert module.RECORD_ID == tools_config.RECORD_ID_PATTERN
    assert module.RECORD_ID_MAX == tools_config.RECORD_ID_MAX
    assert module.CUSTOMER_REF == tools_config.CUSTOMER_REF_PATTERN
    assert module.CUSTOMER_REF_LENGTH == tools_config.CUSTOMER_REF_LENGTH
    assert module.CODE == tools_config.CODE_PATTERN
    assert set(module.TOOLS) == {tool.value for tool in ToolName}
    assert set(module.WRITE_TOOLS) == {tool.value for tool in tools_config.WRITE_TOOLS}
    assert set(module.ORDER_STATUSES) == set(get_args(OrderStatus))
    assert module.SLOT_DURATION_MINUTES == sandbox.SLOT_DURATION_MINUTES
    assert module.LABEL_MAX == sandbox.LABEL_MAX
    assert module.ITEMS_MAX == sandbox.ITEMS_MAX
    assert module.NOTE_MAX == sandbox.NOTE_MAX
    assert module.SUMMARY_MAX == tools_config.TICKET_SUMMARY_MAX


@pytest.mark.parametrize(
    ("pattern", "good", "bad"),
    [
        (tools_config.RECORD_ID_PATTERN, ["S-17", "1043", "B-101"], ["s-17", "S--1", "S-", "-1"]),
        (tools_config.CUSTOMER_REF_PATTERN, ["cust-001"], ["Cust-001", "cust_001", "cust-"]),
        (tools_config.CODE_PATTERN, ["vet_consultation"], ["Vet", "1abc", "a", "a-b"]),
    ],
)
def test_the_patterns_accept_and_refuse_what_they_say(
    pattern: str, good: list[str], bad: list[str]
) -> None:
    assert all(re.fullmatch(pattern, value) for value in good)
    assert not any(re.fullmatch(pattern, value) for value in bad)
