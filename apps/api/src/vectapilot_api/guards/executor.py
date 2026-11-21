"""`ScreeningToolExecutor`: the tool-result screen around each profile's executor (M7 PR 6; G5, G6,
G7, G13).

A subclass of `ToolExecutor`, as G13's seams are (`AgentProfile.executor` is the concrete class),
built with the same constructor arguments. It overrides `run` and `propose`, the two I/O methods
the agent's tools call (`agent/tools.py`'s `BoundTools`); `kind`, `enabled` and `input_model` only
read the configuration.

**`run`** (a read tool): the executor's own call first (its validation, snapshot, leak fence, caps
and audit row, unchanged), then each **free-text string field** of the typed result is screened
(`screens.screen_tool_field`): over its cap or with an injection signal it reads `[withheld]`
(G7), otherwise its clean text is masked (H2, G6). The observation is re-rendered with `render`
from the screened copy. The envelope's `result`, outcome and business ids are never touched: the
eval's leak check reads them, and only the observation ever reaches a model (W10).

**A free-text field is decided by its type, not by a list:** a `str` value whose field is not a
closed set (an enum or a `Literal`, such as an order's `status`) and carries no `pattern`
constraint, at any depth (a slot's `label` inside `check_slots`' list). Every record id and code
of the platform's tools has a pattern, and every status a closed type (`tools/config.py`,
`tools/sandbox.py`), so a lookup by id still works (a phone-like order id is never masked, G6)
and a free-text field added later is screened by default. A string inside a list or tuple is
screened unless its field has a pattern (no platform tool has one today).

**`propose`** (a write tool) returns the executor's envelope unchanged: a `WriteProposal`'s summary
is a code template (W3), its arguments are what the store re-validates and stores, and
`create_ticket`'s free text is screened by the ticket store (`guards.store`) instead (G7).

**Fail closed.** Outside a request the route tracks (`screens.track_screens`), nothing runs. When
the matcher, H2 or the re-render raises, the call raises `GuardFailed` with the class name only,
recorded in the request's tally first (`screens.seam_failed`): the loop turns any exception from
its tools into its own `tool_failure` hand-off (M5 H1 rule 7), and the route then answers
`guard_error` from the tally (G3). A cancel propagates unchanged.

**An observation that grows past the byte cap once screened** (a placeholder longer than the text
it replaces: `[withheld]` for a short field, `[EMAIL]` for a shorter address) is never sent longer
(W10), and is counted `over_cap`. W10's own row rule runs again on the screened copy: a tool with
rows (`ReadTool.cap_rows`, `check_slots`) keeps its first n rows for the largest n that fits, with
`truncated` set, exactly as `ToolExecutor._observe` cuts a result. Only a tool without rows, or a
result that fits at no row count, reads as the `unavailable` error. The loop then sees an `ok`
envelope whose observation is that error (the envelope's `result` is never touched); every
`order_status` field is bounded (`tools/sandbox.py`'s `ITEMS_MAX` and `NOTE_MAX`), so this needs
a cap far below the 8 KiB default.
"""

import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import replace
from enum import Enum
from types import MappingProxyType, UnionType
from typing import Literal, Union, cast, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from vectapilot_api.agent.profiles import AgentProfile
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.guards.config import BusinessGuards
from vectapilot_api.guards.output import Matcher
from vectapilot_api.guards.pii import mask_pii
from vectapilot_api.guards.screens import (
    NO_GUARD_SCOPE,
    Masker,
    ScreenTally,
    current_screens,
    noted,
    screen_tool_field,
    seam_failed,
)
from vectapilot_api.guards.types import Check, Finding
from vectapilot_api.tools.config import ToolName, ToolsConfig
from vectapilot_api.tools.executor import ObservationCaps, ToolExecutor, TransactionSource
from vectapilot_api.tools.registry import REGISTRY
from vectapilot_api.tools.spec import AnyTool, ReadTool
from vectapilot_api.tools.types import (
    ToolContext,
    ToolEnvelope,
    ToolError,
    ToolErrorCode,
    WriteProposal,
    render,
)

Screen = Callable[[str], str]


def has_pattern(field: FieldInfo) -> bool:
    """Whether the field's own constraints hold a `pattern` (an id or a code, never free text)."""
    return any(getattr(item, "pattern", None) is not None for item in field.metadata)


def closed_set(annotation: object) -> bool:
    """Whether `annotation` is a `Literal`, or an optional one (`OrderStatus | None`)."""
    origin = get_origin(annotation)
    if origin is Literal:
        return True
    if origin is Union or origin is UnionType:
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        return bool(members) and all(closed_set(arg) for arg in members)
    return False


def free_text(value: object, field: FieldInfo) -> bool:
    """A string the screen reads (module docstring): not an enum or a `Literal`, its field
    without a pattern."""
    if not isinstance(value, str) or isinstance(value, Enum):
        return False
    return not closed_set(field.annotation) and not has_pattern(field)


def _screen_value(value: object, field: FieldInfo, screen: Screen) -> object:
    if isinstance(value, BaseModel):
        return screen_model(value, screen)
    if isinstance(value, tuple | list):
        items = [_screen_value(item, field, screen) for item in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return tuple(items) if isinstance(value, tuple) else items
    if free_text(value, field):
        return screen(cast(str, value))
    return value


def screen_model[M: BaseModel](model: M, screen: Screen) -> M:
    """A copy of `model` whose free-text fields went through `screen` (module docstring); `model`
    itself when nothing changed. Never validated again: a mask may be a character longer than the
    text it replaces, and only `render` reads the copy."""
    changes: dict[str, object] = {}
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        screened = _screen_value(value, field, screen)
        if screened is not value:
            changes[name] = screened
    return model.model_copy(update=changes) if changes else model


class ScreeningToolExecutor(ToolExecutor):
    """`ToolExecutor` with G7's tool-result screen on every read (module docstring)."""

    def __init__(
        self,
        database: TransactionSource,
        config: ToolsConfig,
        *,
        matcher: Matcher,
        mask: Masker = mask_pii,
        registry: Mapping[ToolName, AnyTool] = REGISTRY,
        caps: ObservationCaps | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        """As `ToolExecutor`, plus the matcher (H1, or `create_app`'s test seam) and the masker
        (H2; unit tests may pass a stand-in)."""
        super().__init__(database, config, registry=registry, caps=caps, clock=clock)
        self._matcher = matcher
        self._mask = mask

    async def run(
        self, tool: str, arguments: Mapping[str, object], context: ToolContext
    ) -> ToolEnvelope:
        tally, business = _scope()
        envelope = await super().run(tool, arguments, context)
        failure: str | None = None
        try:
            screened = self._screened(envelope, business, tally)
        except Exception as error:  # fail closed (module docstring); a cancel is not caught
            failure = type(error).__name__
        if failure is not None:
            raise seam_failed(tally, failure)
        return screened

    async def propose(
        self, tool: str, arguments: Mapping[str, object], context: ToolContext
    ) -> ToolEnvelope:
        _scope()
        return await super().propose(tool, arguments, context)

    def _screened(
        self, envelope: ToolEnvelope, business: BusinessGuards, tally: ScreenTally
    ) -> ToolEnvelope:
        result = envelope.result
        if isinstance(result, ToolError | WriteProposal):  # fixed text, or the store's (G7)
            return envelope
        noted(tally, Check.INJECTION, Check.MASK)
        found: Counter[Finding] = Counter()
        withheld = 0

        def screen(text: str) -> str:
            nonlocal withheld
            screened, findings = screen_tool_field(
                text, business.injection, self._matcher, self._mask
            )
            found.update(findings)
            withheld += bool(findings)
            return screened

        screened = screen_model(result, screen)
        observation = render(envelope.tool, screened)
        if not self._fits(observation):
            found[Finding.OVER_CAP] += 1
            observation = self._cut(envelope.tool, screened)
        tally.findings.update(found)
        tally.withheld_fields += withheld
        return replace(envelope, observation=observation)

    def _fits(self, observation: str) -> bool:
        return len(observation.encode("utf-8")) <= self._caps.max_bytes

    def _cut(self, tool: ToolName | None, screened: BaseModel) -> str:
        """W10's row rule on the screened copy, or the `unavailable` error (module docstring)."""
        spec = None if tool is None else self._registry.get(tool)
        if isinstance(spec, ReadTool) and spec.cap_rows is not None:
            for rows in range(self._caps.max_rows, -1, -1):
                observation = render(tool, spec.cap_rows(screened, rows))
                if self._fits(observation):
                    return observation
        return render(tool, ToolError.of(ToolErrorCode.UNAVAILABLE))


def _scope() -> tuple[ScreenTally, BusinessGuards]:
    """The request's tally and the business's guards, or `GuardFailed` (module docstring)."""
    tally = current_screens()
    business = None if tally is None else tally.business
    if tally is None or business is None:
        raise seam_failed(tally, NO_GUARD_SCOPE)
    return tally, business


def screening_profiles(
    profiles: Mapping[BusinessId, AgentProfile],
    database: TransactionSource,
    *,
    matcher: Matcher,
    mask: Masker = mask_pii,
) -> Mapping[BusinessId, AgentProfile]:
    """`profiles` with each executor replaced by a `ScreeningToolExecutor` over the same database
    and configuration (G13). Every other field is the same object: the configuration, the route
    policy, W11's `health` and the step schema (it reads only the configuration's tools)."""
    screened = {
        business_id: replace(
            profile,
            executor=ScreeningToolExecutor(database, profile.config, matcher=matcher, mask=mask),
        )
        for business_id, profile in profiles.items()
    }
    return MappingProxyType(screened)
