"""The widget routes' bodies (M8 PR 1; S1, S3, S8, S16, P7). Requests are closed
(`extra="forbid"`): a body naming `customer_ref` or any other field is a 422 that echoes nothing
(`auth.http.SessionRoute`). PR 4's fixtures are validated against the response models."""

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    Secret,
    SerializationInfo,
    field_serializer,
)

from vectapilot_api.auth.store import UiLanguage
from vectapilot_api.auth.tokens import TOKEN_MAX
from vectapilot_api.gateway.types import BusinessId
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
