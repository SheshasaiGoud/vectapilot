"""The widget routes' bodies (M8 PR 1 and 2; S1, S3, S6, S8, S16, S17, P7). Requests are closed
(`extra="forbid"`): a body naming `customer_ref` or any other field is a 422 that echoes nothing
(`auth.http.SessionRoute`). PR 4's fixtures are validated against the response models."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    Secret,
    SerializationInfo,
    field_serializer,
    model_validator,
)

from vectapilot_api.actions.tokens import ConfirmToken
from vectapilot_api.agent.responses import AgentResponse
from vectapilot_api.auth.store import UiLanguage
from vectapilot_api.auth.tokens import TOKEN_MAX
from vectapilot_api.gateway.types import BusinessId, TraceId
from vectapilot_api.handoffs.store import HandoffStatus, Rating
from vectapilot_api.retrieval.types import Question
from vectapilot_api.widget.config import GUIDANCE_LANGUAGE, WidgetBusiness, WidgetTexts

SessionToken = Secret[Annotated[str, Field(min_length=1, max_length=TOKEN_MAX)]]
"""A session token in a response: masked in every Python-mode dump (a log extra, a repr), revealed
only in the response's JSON, as `agent.responses.ConfirmationResponse.confirm_token` (S3)."""


class _Request(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class _Response(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SessionRequest(_Request):
    """`POST /v1/widget/sessions`' body: the only request that names a business, because no
    session exists yet [proposal: the widget page's URL names it already, `/widget/{business_id}`;
    it is not a secret]. Every later widget request takes it from the session (S1)."""

    business_id: BusinessId
    ui_language: UiLanguage
    """The UI language at session start: one of the widget's `languages` (§6's breakdown)."""


class SessionCreated(_Response):
    """201 of `POST /v1/widget/sessions`: the bearer token, once, and the timings (P7)."""

    token: SessionToken
    """`Authorization: Bearer <token>` on every later widget request; kept in the iframe's
    `sessionStorage` and nowhere else (S18). Only its SHA-256 is stored."""
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - the scheme's name, not a secret
    expires_at: AwareDatetime
    """Set by the database's clock (P1); after it every request is the one 401."""
    lifetime_seconds: int
    ui_language: str
    message_timeout_seconds: float
    """P7, D15 (a): the server's turn bound plus a margin. The widget never re-sends by itself."""
    confirm_timeout_seconds: float
    """P7: above the confirm route's write backstop."""

    @field_serializer("token")
    def _reveal_token(self, token: SessionToken, info: SerializationInfo) -> object:
        # The one deliberate reveal: only this response's JSON carries the token.
        return token.get_secret_value() if info.mode_is_json() else token


class WidgetConfigResponse(_Response):
    """200 of `GET /v1/widget/{business_id}/config`: what the widget shows before any session,
    and keeps in memory for every failure state (S16). Read from files only."""

    business_id: BusinessId
    display_name: str
    languages: tuple[str, ...]
    """The first one is the default."""
    texts: tuple[WidgetTexts, ...]
    guidance: tuple[str, ...]
    """The business's emergency guidance, verbatim from its guard file, one line per item (S8);
    empty for a business without any. Shown first, with `lang` = `guidance_language`."""
    guidance_language: Literal["en"] = GUIDANCE_LANGUAGE
    accent_color: str | None

    @classmethod
    def of(cls, widget: WidgetBusiness) -> "WidgetConfigResponse":
        file = widget.file
        return cls(
            business_id=file.business_id,
            display_name=file.display_name,
            languages=file.languages,
            texts=file.texts,
            guidance=widget.guidance,
            accent_color=file.accent_color,
        )


# ── PR 2: messages and hand-offs ───────────────────────────────────────────────────────────────


class MessageRequest(_Request):
    """`POST /v1/widget/messages`' body: the message and nothing else. The business and the
    customer come from the session (S1, S4): the route builds `AgentRequest` itself, so no client
    can name a `customer_ref` (ADR 0016's IDOR stays closed)."""

    message: Question = Field(repr=False)
    """At most `MAX_QUESTION_CHARS` code points (S16: the widget counts the same unit)."""


class HandoffQueued(_Response):
    """Whether a hand-off is in the business's queue (S8, S17): `queued` is true exactly when its
    row exists, with its id. That row is the session's open one, which may be an earlier urgent or
    `safety` hand-off (S17), so the server decides D11 once, from the row as stored (zara's PR 2
    review): `follow_up` is true only for a queued row that is neither urgent nor `safety`. The
    widget shows "a person will reply" and polls only when `follow_up` is true; for any other it
    shows D11 (b)'s `urgent_follow_up` (when queued) or nothing, and a missing field counts as
    false."""

    id: UUID | None
    queued: bool
    follow_up: bool

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.queued != (self.id is not None):
            raise ValueError("a queued hand-off has its id, and only a queued one")
        if self.follow_up and not self.queued:
            raise ValueError("only a queued hand-off can invite a follow-up")
        return self


class MessageResponse(_Response):
    """200 of `POST /v1/widget/messages`: `/v1/agent`'s response, unchanged and whole (a
    `confirmation_required` turn carries its token: ADR 0016 W2's second route, ADR 0019), and
    for a hand-off whether it was queued. None for every other kind."""

    turn: AgentResponse
    handoff: HandoffQueued | None


class HandoffStateResponse(_Response):
    """200 of `GET /v1/widget/handoffs/{handoff_id}`: the session's own hand-off's status and the
    staff reply (D7 (a)), shown as text. Never the masked message, the guidance or who works it.
    `follow_up` as `HandoffQueued`'s, from the row as it is now (a raise to urgent or `safety`
    turns it false): the widget stops polling when it is false (D11)."""

    id: UUID
    status: HandoffStatus
    reply: str | None
    replied_at: AwareDatetime | None
    follow_up: bool


# ── PR 2b: confirm and feedback ──────────────────────────────────────────────────────────────


class WidgetConfirmRequest(_Request):
    """`POST /v1/widget/actions/{action_id}/confirm`'s body: the one-time token and nothing else.
    The customer comes from the session (S4): the route builds `actions.types.ConfirmRequest`
    itself with the session's `customer_ref`, so no client can confirm as anyone else (ADR 0016's
    IDOR stays closed). The token is a `Secret`, masked in every dump, and never echoed by a 422."""

    confirm_token: ConfirmToken


class FeedbackRequest(_Request):
    """`POST /v1/widget/feedback`'s body (D6 (a)): one of this session's own turns, named by the
    trace id its body carried, and the rating. No text."""

    trace_id: TraceId
    rating: Rating


class FeedbackRecorded(_Response):
    """201 of `POST /v1/widget/feedback`: the rating stored, as sent [proposal]."""

    trace_id: str
    rating: Rating
