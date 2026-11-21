"""`ScreeningProposalStore`: a proposal's ticket text screened before it is stored (M7 PR 6; G7, G8,
G13).

`AgentService` takes its pending-action store as a `ProposalStore` (a protocol), so this seam wraps
the store `main.build_action_services` builds (its source pinned, G13) instead of editing either.
For a `create_ticket` proposal it screens the `summary` argument, in G1's order:
1. the input cap (500 characters, `caps`): over it is withheld (`input_cap`);
2. the injection screen on the raw text's hidden characters and the folded text's phrases
   (`injection:ticket_text`);
3. H3 (`check_reply`) on the text, as `ticket_text`, with the request's context.

**A hit** (G7 as amended 2026-11-17): the proposal is stored through the wrapped store's
`create_withheld`, already expired (`actions.pending`: a lifetime of one microsecond, audited
`withheld`), so confirming it, with its real token included, is the same 404 as an unknown id
(W6), and staff can see what was stopped. Then `GuardWithheld` is raised naming the stored action
id; its token is dropped here and never leaves the process. The exception crosses `AgentService`
through its `propagate` (`screens.GUARD_PROPAGATE`), and the route answers with a whole-response
`guard_output` hand-off whose `guard` record names the action id (G2: a guard never edits a
`confirmation_required` response).

**A guard that raises** (a pending stub included), or a withheld proposal that cannot be stored,
raises `GuardFailed` (recorded in the request's tally, `screens.seam_failed`): nothing is stored
for it or no token leaves, and the route answers `guard_error`. Every other write tool's proposal
holds no free text and passes to the store unchanged.

The wrapped store is `actions.pending.PendingActionStore` (only `main.py` imports it, W2's fence),
typed here by the two methods this seam calls (`WithholdingStore`).
"""

from typing import Protocol

from vectapilot_api.actions.types import PendingAction
from vectapilot_api.guards.caps import Surface, within_cap
from vectapilot_api.guards.config import BusinessGuards
from vectapilot_api.guards.output import Matcher, OutputCheck, OutputKind, check_reply
from vectapilot_api.guards.screens import (
    NO_GUARD_SCOPE,
    GuardFailed,
    GuardWithheld,
    ScreenTally,
    current_screens,
    injection_findings,
    noted,
    output_context,
    output_withheld,
    prepare,
    seam_failed,
)
from vectapilot_api.guards.types import Check, DecidedBy, Finding
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.types import ToolContext, WriteProposal

TICKET_TEXT_ARGUMENT = "summary"
"""`create_ticket`'s free-text argument (`tools/create_ticket.py`)."""


class WithholdingStore(Protocol):
    """The pending-action store this seam wraps (`actions.pending.PendingActionStore`)."""

    async def create(self, proposal: WriteProposal, context: ToolContext) -> PendingAction: ...

    async def create_withheld(
        self, proposal: WriteProposal, context: ToolContext
    ) -> PendingAction: ...


class ScreeningProposalStore:
    """A `ProposalStore` that screens a ticket's text first (module docstring)."""

    def __init__(
        self, store: WithholdingStore, *, matcher: Matcher, check: OutputCheck = check_reply
    ) -> None:
        self._store = store
        self._matcher = matcher
        self._check = check

    async def create(self, proposal: WriteProposal, context: ToolContext) -> PendingAction:
        if proposal.tool is ToolName.CREATE_TICKET:
            tally = current_screens()
            withheld: GuardWithheld | None = None
            failure: str | None = None
            try:
                withheld = self._screen(proposal, tally)
            except GuardFailed as failed:
                failure = failed.error_type
            except Exception as error:  # fail closed (module docstring); a cancel is not caught
                failure = type(error).__name__
            if failure is not None:
                raise seam_failed(tally, failure)
            if withheld is not None:
                raise await self._store_withheld(withheld, proposal, context, tally)
        return await self._store.create(proposal, context)

    async def _store_withheld(
        self,
        withheld: GuardWithheld,
        proposal: WriteProposal,
        context: ToolContext,
        tally: ScreenTally | None,
    ) -> GuardWithheld:
        """`withheld`, naming the action it is now stored as, already expired (module
        docstring). Its token is dropped here. A failure to store it fails closed."""
        failure: str | None = None
        try:
            action = await self._store.create_withheld(proposal, context)
        except Exception as error:  # a cancel is not caught
            failure = type(error).__name__
        if failure is not None:
            raise seam_failed(tally, failure)
        return withheld.stored_as(action.id)

    def _screen(self, proposal: WriteProposal, tally: ScreenTally | None) -> GuardWithheld | None:
        """The withholding, or None (its findings travel in the exception: the route counts
        them once)."""
        business = None if tally is None else tally.business
        if tally is None or business is None:
            raise GuardFailed(NO_GUARD_SCOPE)
        text = proposal.arguments.get(TICKET_TEXT_ARGUMENT)
        if not isinstance(text, str):
            raise TypeError("a ticket's text is a string")
        return self._withheld(text, business, tally)

    def _withheld(
        self, text: str, business: BusinessGuards, tally: ScreenTally
    ) -> GuardWithheld | None:
        if not within_cap(Surface.TICKET_TEXT, text):
            return GuardWithheld(DecidedBy.INPUT_CAP, {Finding.OVER_CAP: 1})
        noted(tally, Check.INJECTION)
        found = injection_findings(prepare(text), business.injection, self._matcher)
        if found:
            return GuardWithheld(DecidedBy.INJECTION_TICKET_TEXT, found)
        noted(tally, Check.OUTPUT)
        context = output_context(tally, OutputKind.TICKET_TEXT, self._matcher)
        return output_withheld(self._check(text, context))
