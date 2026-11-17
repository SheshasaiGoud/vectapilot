"""The suite's fixtures are runnable as written (M7 PR 2, before the freeze): a case whose script
the real schemas refuse would only ever be "not scored".

- `rt-shop`'s tool files pass the real parsers and agree with its corpus manifest; the app
  folders a served app needs hold every business.
- Every agent step a case scripts validates against its business's real `AgentStep` schema
  (`agent.steps.build_agent_step`), exactly as the gateway validates the model's JSON, with every
  placeholder filled (B2a's proposal: the attacker's best case must reach the check).
- The inline fixture is what runs; the readable corpus under `fixtures/poisoned/` must equal it,
  record by record, and list exactly the cases that use it (B2a's proposal 1).
- The inline tool results are the **full** output of every scripted read call against the
  business's seed, as the customer the case runs as (leo's PR 2 review: an in-process run and a
  database run must see the same thing). The only exception is text the seed cannot hold
  (invisible characters, refused by the sandbox's `single_line`): its record id is absent from
  the seed and its case never runs in the database environment.
- Every script plays as the replay plays it, within the default step cap, and every A case's
  script also on the read-only service; `rt-shop`'s safety file is a valid guard business file
  whose guidance equals its `tools.json` lines, each verbatim in its `emergency.md`.
"""

import json
from collections.abc import Mapping
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Final, cast

import pytest

from tests.redteam.suite import CASES, DATASET, DATASET_DIR, END_TO_END, GENERATOR
from tests.tools.helpers import CORPUS_DIR, SANDBOX_DIR
from vectapilot_api.agent.steps import build_agent_step
from vectapilot_api.config import Settings
from vectapilot_api.evalset.corpus import EvalModel
from vectapilot_api.guards.config import BusinessFile
from vectapilot_api.redteam.businesses import (
    customer_ref_for,
    fixture_business_tools,
    write_app_folders,
)
from vectapilot_api.redteam.cases import (
    CallToolStep,
    Case,
    Category,
    ChunkFixture,
    DocumentFixture,
    ToolResultFixture,
)
from vectapilot_api.redteam.checks import check_dataset
from vectapilot_api.redteam.harness import fill
from vectapilot_api.redteam.plan import environment_of
from vectapilot_api.redteam.replay import agent_steps, is_write, playable, step_json
from vectapilot_api.tools.check_slots import CheckSlotsInput, SlotView
from vectapilot_api.tools.config import ToolName, ToolsConfig, ToolsConfigError, single_line
from vectapilot_api.tools.executor import ToolExecutor, TransactionSource
from vectapilot_api.tools.files import BusinessTools, load_tools_dir
from vectapilot_api.tools.order_status import OrderStatusOutput
from vectapilot_api.tools.sandbox import SandboxSeed

POISONED: Final = DATASET_DIR / "fixtures" / "poisoned"
NO_DATABASE: Final = cast(TransactionSource, None)


def business_tools() -> dict[str, BusinessTools]:
    found = dict(load_tools_dir(SANDBOX_DIR))
    for business_id in DATASET.manifest.fixture_businesses:
        found[business_id] = fixture_business_tools(DATASET_DIR, business_id)
    return found


TOOLS: Final = business_tools()
CONFIGS: Final = {business_id: tools.config for business_id, tools in TOOLS.items()}
MAX_STEPS: Final = Settings.model_fields["agent_max_steps"].default
SCRIPTED: Final = sorted(c.id for c in CASES.values() if agent_steps(c))


def test_rt_shop_is_a_valid_ecommerce_fixture_business() -> None:
    tools = fixture_business_tools(DATASET_DIR, "rt-shop")

    assert tools.config.business_type.value == "ecommerce"
    assert tools.seed.business_id == "rt-shop"


def test_red_proof_a_fixture_business_whose_type_differs_from_its_manifest_is_refused(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "fixtures" / "rt-shop"
    folder.mkdir(parents=True)
    source = DATASET_DIR / "fixtures" / "rt-shop"
    for name in ("tools.json", "seed.json"):
        (folder / name).write_bytes((source / name).read_bytes())
    manifest = json.loads((source / "manifest.json").read_text("utf-8"))
    (folder / "manifest.json").write_text(
        json.dumps(manifest | {"business_type": "retail"}), "utf-8"
    )

    with pytest.raises(ToolsConfigError, match="different business types"):
        fixture_business_tools(tmp_path, "rt-shop")


def test_the_app_folders_hold_every_business_and_load(tmp_path: Path) -> None:
    folders = write_app_folders(
        tmp_path,
        dataset_dir=DATASET_DIR,
        sandbox_dir=SANDBOX_DIR,
        corpus_dir=CORPUS_DIR,
        fixture_businesses=DATASET.manifest.fixture_businesses,
    )

    assert set(load_tools_dir(folders.tools_dir)) == {"paws-and-co", "sunrise-clinic", "rt-shop"}
    assert (folders.corpus_dir / "rt-shop" / "manifest.json").is_file()


def test_some_cases_script_the_agent() -> None:
    assert len(SCRIPTED) > 20, "the check below would pass by not looking"


@pytest.mark.parametrize("case_id", SCRIPTED)
def test_every_scripted_step_validates_against_its_businesss_step_schema(case_id: str) -> None:
    case = CASES[case_id]
    schema = build_agent_step(ToolExecutor(NO_DATABASE, CONFIGS[case.business_id]))
    filled = fill(case, GENERATOR)

    for step in agent_steps(case):
        schema.model_validate(step_json(step, filled.text))


@pytest.mark.parametrize("case_id", SCRIPTED)
def test_every_script_plays_as_the_replay_plays_it_within_the_step_cap(case_id: str) -> None:
    case = CASES[case_id]
    steps = case.fixture.scripted_model

    assert len(playable(agent_steps(case))) <= MAX_STEPS
    if case.category is Category.A:  # A runs on the read-only service when its guard holds
        assert len(playable(steps, read_only=True)) <= MAX_STEPS


# ── The inline tool results are the seed's full output ──────────────────────────────────────
# A restatement of the two read tools' statements (`tools/order_status.py`, `tools/check_slots.py`)
# over the seed in memory, for a test that needs no database. The executors themselves are
# tested against Postgres in tests/tools/.


def _seed_output(
    step: CallToolStep, seed: SandboxSeed, config: ToolsConfig, customer: str
) -> list[dict[str, Any]]:
    if step.tool is ToolName.ORDER_STATUS:
        return [
            OrderStatusOutput(
                order_id=order.id,
                status=order.status,
                items=order.items,
                note=order.note,
                placed_at=order.placed_at.astimezone(config.zone),
                expected_delivery=order.expected_delivery,
            ).model_dump(mode="json")
            for order in seed.orders
            if order.id == step.arguments["order_id"] and order.customer_ref == customer
        ]
    assert step.tool is ToolName.CHECK_SLOTS, step.tool
    query = CheckSlotsInput.model_validate(step.arguments)
    now = datetime.combine(config.sandbox_date, time(0), config.zone)
    starts = max(datetime.combine(query.date_from, time(0), config.zone), now)
    ends = datetime.combine(query.date_to + timedelta(days=1), time(0), config.zone)
    services = {query.service} if query.service is not None else set(config.service_ids)
    booked = {booking.slot_id for booking in seed.bookings}
    free = sorted(
        (
            slot
            for slot in seed.slots
            if starts <= slot.starts_at < ends
            and slot.service in services
            and slot.id not in booked
        ),
        key=lambda slot: (slot.starts_at, slot.id),
    )
    return [
        SlotView(
            slot_id=slot.id,
            service=slot.service,
            service_name=config.service_name(slot.service) or slot.service,
            starts_at=slot.starts_at.astimezone(config.zone),
            duration_minutes=slot.duration_minutes,
            label=slot.label,
        ).model_dump(mode="json")
        for slot in free
    ]


def _seed_cannot_hold(fields: Mapping[str, object]) -> bool:
    for value in fields.values():
        if isinstance(value, str):
            try:
                single_line(value)
            except ValueError:
                return True
    return False


READS: Final = sorted(
    c.id
    for c in CASES.values()
    if any(not is_write(s) and isinstance(s, CallToolStep) for s in agent_steps(c))
)


def test_some_cases_script_a_read_tool() -> None:
    assert len(READS) >= 15, "the check below would pass by not looking"


@pytest.mark.parametrize("case_id", READS)
def test_the_inline_tool_results_are_the_seeds_full_output(case_id: str) -> None:
    case = CASES[case_id]
    tools = TOOLS[case.business_id]
    customer = customer_ref_for(case, tools.seed)
    inline = [dict(result.fields) for result in case.fixture.tool_results]
    reads = [s for s in agent_steps(case) if isinstance(s, CallToolStep) and not is_write(s)]

    expected: list[dict[str, Any]] = []
    for step in reads:
        output = _seed_output(step, tools.seed, tools.config, customer)
        unheld = [fields for fields in inline if _seed_cannot_hold(fields)]
        if not output and unheld:
            # Text the seed cannot hold: the record is absent from the seed, and the case runs
            # in process only (its inline result is the only one that exists).
            assert step.tool is ToolName.ORDER_STATUS
            assert all(o.id != step.arguments["order_id"] for o in tools.seed.orders)
            assert environment_of(case, END_TO_END) == "in_process", case_id
            output = unheld
        expected += output

    assert inline == expected, case_id


@pytest.mark.parametrize(
    ("case_id", "customer"),
    [
        ("c-003", "cust-002"),
        ("c-005", "cust-002"),
        ("c-008", "cust-002"),
        ("a-008", "cust-002"),
        ("c-001", "cust-001"),
        ("a-006", "cust-001"),  # a booking's owner
        ("c-006", "cust-001"),  # an order the seed does not hold: the first customer
        ("l-006", "cust-001"),  # no record named
    ],
)
def test_each_case_runs_as_the_owner_of_the_first_record_it_names(
    case_id: str, customer: str
) -> None:
    case = CASES[case_id]

    assert customer_ref_for(case, TOOLS[case.business_id].seed) == customer


def test_rt_shops_safety_file_is_a_guard_business_file_with_its_own_guidance() -> None:
    folder = DATASET_DIR / "fixtures" / "rt-shop"
    safety = BusinessFile.model_validate_json((folder / "safety.json").read_text("utf-8"))
    emergency = (folder / "emergency.md").read_text("utf-8")

    assert safety.business_id == "rt-shop"
    assert safety.guidance and safety.guidance == TOOLS["rt-shop"].config.emergency_guidance
    assert all(line in emergency for line in safety.guidance)


def _records(name: str) -> list[dict[str, Any]]:
    lines = (POISONED / name).read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _inline(case: Case, kind: str) -> list[Mapping[str, Any]]:
    fixture = case.fixture.model_dump(mode="json")
    if kind == "chunks":
        return list(fixture["chunks"])
    if kind == "tool-results":
        return list(fixture["tool_results"])
    document = fixture["document"]
    return [] if document is None else [document]


_MODELS: Final[dict[str, type[EvalModel]]] = {
    "chunks": ChunkFixture,
    "tool-results": ToolResultFixture,
    "documents": DocumentFixture,
}


def _content(record: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """The record's content through its fixture model, so defaults compare equal."""
    model = _MODELS[kind]
    fields = {key: record[key] for key in model.model_fields if key in record}
    content: dict[str, Any] = model.model_validate(fields).model_dump(mode="json")
    return content


@pytest.mark.parametrize("kind", ["chunks", "tool-results", "documents"])
def test_the_poisoned_corpus_equals_the_inline_fixtures_that_run(kind: str) -> None:
    records = _records(f"{kind}.jsonl")
    assert records, "an empty corpus file would pass by not looking"
    for record in records:
        users = record["used_by"]
        assert users, f"{record['id']}: a record no case uses"
        for case_id in users:
            case = CASES[case_id]
            assert case.business_id == record["business_id"], (case_id, record["id"])
            inline = [_content(item, kind) for item in _inline(case, kind)]
            assert _content(record, kind) in inline, (case_id, record["id"])
    used = {
        (case_id, json.dumps(_content(r, kind), sort_keys=True))
        for r in records
        for case_id in r["used_by"]
    }
    for case in CASES.values():
        for item in _inline(case, kind):
            key = (case.id, json.dumps(_content(item, kind), sort_keys=True))
            assert key in used, f"{case.id}: an inline {kind} fixture without its corpus record"


def test_the_suite_runs_on_a_frozen_version_that_passes_check() -> None:
    result = check_dataset(DATASET)

    assert DATASET.manifest.stage == "frozen"
    assert result.problems == ()
    assert DATASET.manifest.counts is not None and DATASET.manifest.counts.total == len(CASES)
