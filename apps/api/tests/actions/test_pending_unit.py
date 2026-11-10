"""M5 PR 3: storing a proposal, without a database (`fakes.ScriptedSource`). Unit.

The real INSERT, the database-set expiry and the stored hash are in `test_confirm_db.py`; here:

- **A caller's bug is refused before the database:** a business without tools, a disabled tool,
  arguments that do not fit, and arguments that validating would change (the summary was
  rendered from other values). No transaction is opened.
- **The store's transaction** is READ COMMITTED, asked for explicitly, with the server timeouts;
  the action and its `stored` audit row go in it.
- **One `tool_call` record per call (W12),** failures included: `rolled_back` for a database or
  network error before the COMMIT, `outcome_unknown` for a timeout or a failed COMMIT,
  `cancelled`, `error` for a bug. A failure is logged only, never audited (there is no action id
  to name), and no record holds the token.
- **The lifetime is checked again** by the constructor, for a store built by hand.
"""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Final

import pytest
from pydantic import field_validator

from tests.actions.fakes import ACTION, ScriptedSource
from tests.tools.core import PAWS, TRACE, config_for
from vectapilot_api.actions.pending import PendingActionStore
from vectapilot_api.actions.tokens import token_sha256
from vectapilot_api.log import JsonFormatter
from vectapilot_api.tools.book import BookInput
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.executor import TOOL_CALL
from vectapilot_api.tools.types import ToolContext, WriteProposal

pytestmark = pytest.mark.anyio

BUSINESS: Final = PAWS.config.business_id
PROPOSAL: Final = WriteProposal(
    tool=ToolName.BOOK, arguments={"slot_id": "S-09"}, summary="Book slot S-09."
)
CONTEXT: Final = ToolContext(business_id=BUSINESS, customer_ref="cust-001", trace_id=TRACE)


def store(source: ScriptedSource, **configs: Any) -> PendingActionStore:
    return PendingActionStore(source, configs or {BUSINESS: PAWS.config}, lifetime_s=900)


def stored_row() -> list[Any]:
    """`INSERT_ACTION`'s returned row."""
    return [SimpleNamespace(business_id=BUSINESS, id=ACTION, expires_at=datetime.now(UTC))]


def records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [getattr(r, TOOL_CALL) for r in caplog.records if r.getMessage() == TOOL_CALL]


@pytest.fixture(autouse=True)
def _capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)


async def test_a_stored_proposal_and_its_audit_row_share_one_transaction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = ScriptedSource(script=[stored_row(), []])

    action = await store(source).create(PROPOSAL, CONTEXT)

    assert source.transactions == [(BUSINESS, False, True)]
    [insert] = source.params_of("INSERT INTO pending_actions")
    assert insert["token_sha256"] == token_sha256(action.confirm_token)
    assert (insert["customer_ref"], insert["lifetime_s"]) == ("cust-001", 900.0)
    [audit] = source.main_audits()
    assert (audit["phase"], audit["outcome"], audit["kind"], audit["action_id"]) == (
        "propose",
        "stored",
        "write",
        ACTION,
    )
    assert [(r["outcome"], r["audited"], r["action_id"]) for r in records(caplog)] == [
        ("stored", True, str(ACTION))
    ]


class Normalizing(BookInput):
    """A business input model whose validator changes a value (none does today)."""

    @field_validator("slot_id", mode="after")
    @classmethod
    def _pad(cls, value: str) -> str:
        return value.replace("S-", "S-0")


@pytest.mark.parametrize(
    ("configs", "proposal", "match"),
    [
        pytest.param({}, PROPOSAL, "no tools", id="no tools"),
        pytest.param(
            {BUSINESS: config_for(BUSINESS, enabled_tools=["check_slots"])},
            PROPOSAL,
            "not enabled",
            id="disabled",
        ),
        pytest.param(
            None,
            PROPOSAL.model_copy(update={"arguments": {"slot_id": "not an id"}}),
            "do not fit",
            id="does not fit",
        ),
    ],
)
async def test_a_callers_bug_is_refused_before_the_database(
    configs: dict[str, Any] | None, proposal: WriteProposal, match: str
) -> None:
    source = ScriptedSource()
    made = PendingActionStore(
        source, {BUSINESS: PAWS.config} if configs is None else configs, lifetime_s=900
    )

    with pytest.raises(ValueError, match=match):
        await made.create(proposal, CONTEXT)

    assert source.transactions == []


async def test_arguments_that_validating_would_change_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "vectapilot_api.actions.pending.business_input_model", lambda spec, config: Normalizing
    )
    source = ScriptedSource()

    with pytest.raises(ValueError, match="changed when validated again"):
        await store(source).create(PROPOSAL, CONTEXT)

    assert source.transactions == []


@pytest.mark.parametrize(
    ("script", "exit_error", "raised", "outcome"),
    [
        pytest.param([OSError("refused")], None, OSError, "rolled_back", id="error"),
        pytest.param([TimeoutError()], None, TimeoutError, "outcome_unknown", id="timeout"),
        pytest.param(
            [stored_row(), []], OSError("reset"), OSError, "outcome_unknown", id="at the commit"
        ),
        pytest.param(
            [asyncio.CancelledError()], None, asyncio.CancelledError, "cancelled", id="cancelled"
        ),
        pytest.param([KeyError("a bug")], None, KeyError, "error", id="bug"),
    ],
)
async def test_a_failed_store_leaves_one_record_never_an_audit_row_or_the_token(
    caplog: pytest.LogCaptureFixture,
    script: list[list[Any] | BaseException],
    exit_error: BaseException | None,
    raised: type[BaseException],
    outcome: str,
) -> None:
    source = ScriptedSource(script=script, exit_error=exit_error)

    with pytest.raises(raised):
        await store(source).create(PROPOSAL, CONTEXT)

    assert [(r["outcome"], r["audited"], r["action_id"], r["tool"]) for r in records(caplog)] == [
        (outcome, False, None, "book")
    ]
    assert len(source.transactions) == 1  # nothing audited after
    [insert] = source.params_of("INSERT INTO pending_actions")
    digest = str(insert["token_sha256"])
    logged = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert digest not in logged


@pytest.mark.parametrize("seconds", [0, -1, 3601, 86400])
def test_a_store_built_by_hand_checks_its_lifetime_again(seconds: int) -> None:
    with pytest.raises(ValueError, match="at most 3600"):
        PendingActionStore(ScriptedSource(), {}, lifetime_s=seconds)
