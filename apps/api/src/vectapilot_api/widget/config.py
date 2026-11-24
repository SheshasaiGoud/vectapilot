"""The widget configuration folder (`VECTAPILOT_WIDGET_CONFIG_DIR`, M8 PR 1; rule 9, S8, S9, S16,
D9, D11, D12 (b)).

    <dir>/README.md                        optional, ignored
    <dir>/businesses/<business_id>.json    one business's widget (`WidgetFile`)

**Configuration names the widget, code does not** (rule 9): whether a business has one
(`enabled`), the origins that may frame it (`allowed_origins`, written into the page's
`frame-ancestors`, PR 2), its UI languages, its display name, and per language the greeting, the
follow-up texts, the contact fallback lines and the quick replies; how long its hand-offs are kept
(`handoff_retention_days`, P5) and an optional accent colour. A business without a file has no
widget (the config route and session creation answer 404).

**The guidance is never copied here** (S8). It comes from the guard folder
(`guards.config.BusinessGuards.guidance`, kept equal to `tools.json` by M7's test), English only,
and the config route sends it with `guidance_language: "en"`. **A health business with
`enabled: true` and no guard guidance is refused** (S8): its widget would have nothing to show
first in an emergency or in any failure state (S16). A non-health business without guidance
shows none.

**Follow-up texts** (D11 (b)): `handoff_follow_up` ("a person will reply", shown only when a
non-urgent hand-off was stored) and `urgent_follow_up` (shown only when an urgent hand-off was
stored) are optional, and absent means nothing is shown. Both are shesha's to write with Q9, so
the sample files leave them out.

**Startup refuses** (fail fast, `create_app`): a missing folder or `businesses` folder, an
unexpected entry, a file whose name is not a business id or differs from its `business_id`, a
file over `MAX_FILE_BYTES` or not UTF-8, any invalid field (`WidgetFile`), a business without a
corpus manifest or whose manifest names another business or an unknown type, a UI language its
manifest does not support, an origin that is not in the strict form (`auth.origins`: no path,
wildcard, whitespace, `;` or `,`, lowercase, no default port; https, or http for `localhost` and
`127.0.0.1` only where the caller allows it), and the health rule above. An enabled widget names
at least one origin [proposal]; a disabled one may name none.

**Accent colour** [proposal]: `#rrggbb` in lowercase, refused below `MIN_ACCENT_CONTRAST` (4.5:1,
WCAG 2.2 SC 1.4.3 for normal text) against `ACCENT_TEXT_COLOR`, the text the widget writes on it.
White text at 4.5:1 also clears 1.4.11's 3:1 for the accent against a white page (the same ratio).
WCAG's relative luminance, computed exactly as the standard writes it (floating point: the inputs
are 8-bit, the threshold a ratio).

**Errors name the file and the location, never a value** (as `guards.config`): the models hide
their input, our validators raise fixed sentences, the files use lists rather than mappings (no
text a file wrote becomes a location), and an unknown key is reported as `(unknown key)`.

The bounds below are proposals, configuration rather than measurements.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vectapilot_api.auth.origins import InvalidOrigin, parse_origin
from vectapilot_api.auth.store import UI_LANGUAGE_MAX
from vectapilot_api.db import InvalidBusinessId, parse_business_id
from vectapilot_api.evalset.corpus import (
    CORPUS_MANIFEST_FILE,
    CorpusError,
    CorpusManifest,
    LanguageTag,
    load_corpus_manifest,
)
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.guards.config import GuardConfig, describe_validation_error
from vectapilot_api.intents.policy import HEALTH_TYPES, BusinessType
from vectapilot_api.tools.config import SingleLine

README: Final = "README.md"
BUSINESSES: Final = "businesses"
JSON_SUFFIX: Final = ".json"

# ── Bounds [proposal] ──────────────────────────────────────────────────────────────────────────
MAX_FILE_BYTES: Final = 64 * 1024
"""A bound, not a measurement: a widget file is about a kilobyte."""
MAX_MANIFEST_BYTES: Final = 256 * 1024
"""As `guards.config`'s bound for the same manifests."""
DISPLAY_NAME_MAX: Final = 80
GREETING_MAX: Final = 300
FOLLOW_UP_MAX: Final = 300
CONTACT_LINE_MAX: Final = 200
CONTACT_LINES_MAX: Final = 5
QUICK_REPLY_MAX: Final = 80
QUICK_REPLIES_MAX: Final = 4
LANGUAGES_MAX: Final = 10
ORIGINS_MAX: Final = 10
REVIEW_MAX: Final = 200
RETENTION_DAYS: Final = (1, 90)
"""P5: hand-offs are kept 1 to 90 days per business."""
DEFAULT_RETENTION_DAYS: Final = 30
"""P5's default."""
ACCENT_TEXT_COLOR: Final = "#ffffff"
"""The text the widget writes on its accent colour [proposal; PR 4 (sofia) confirms]."""
MIN_ACCENT_CONTRAST: Final = 4.5
"""WCAG 2.2 SC 1.4.3, normal text (AA)."""
GUIDANCE_LANGUAGE: Final = "en"
"""The guard files are English only (S8, D9 (a)); localised guidance waits for Q8."""

HexColor = Annotated[str, Field(pattern=r"^#[0-9a-f]{6}$")]
Line = Annotated[SingleLine, Field(max_length=CONTACT_LINE_MAX)]
QuickReply = Annotated[SingleLine, Field(max_length=QUICK_REPLY_MAX)]
UiLanguageTag = Annotated[LanguageTag, Field(max_length=UI_LANGUAGE_MAX)]
"""A UI language: a language tag that fits `sessions.ui_language` (migration 0008)."""


class WidgetConfigError(ValueError):
    """The widget configuration cannot be used. The message names the file, the location and the
    rule, never a value."""


# ── Colour contrast (WCAG 2.2, "relative luminance" and "contrast ratio") ─────────────────────


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    """WCAG's relative luminance of `#rrggbb`."""
    red, green, blue = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue)


def contrast_ratio(first: str, second: str) -> float:
    """WCAG's contrast ratio of two `#rrggbb` colours, from 1 to 21."""
    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


# ── File models ────────────────────────────────────────────────────────────────────────────────


class WidgetModel(BaseModel):
    """Base of the widget file's models: immutable, closed, strict and values kept out of errors
    (as `guards.config.GuardModel`)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)


class WidgetTexts(WidgetModel):
    """One UI language's business texts, each shown verbatim with its own `lang` (D9 (a)). The
    config route sends them as they are."""

    language: UiLanguageTag
    greeting: Annotated[SingleLine, Field(max_length=GREETING_MAX)]
    handoff_follow_up: Annotated[SingleLine, Field(max_length=FOLLOW_UP_MAX)] | None = None
    """Shown only when a non-urgent hand-off was stored (S8); absent: nothing."""
    urgent_follow_up: Annotated[SingleLine, Field(max_length=FOLLOW_UP_MAX)] | None = None
    """D11 (b): shown only when an urgent hand-off was stored; absent: nothing."""
    contact_fallback: tuple[Line, ...] = Field(min_length=1, max_length=CONTACT_LINES_MAX)
    """How to reach the business when the chat cannot hand off (S8, S14, S16), line by line."""
    quick_replies: tuple[QuickReply, ...] = Field(default=(), max_length=QUICK_REPLIES_MAX)
    """Optional (cut 2): each is sent as the customer's own message, never as an instruction."""

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if len(set(self.quick_replies)) != len(self.quick_replies):
            raise ValueError("each quick reply is listed once")
        return self


class WidgetFile(WidgetModel):
    """`businesses/<business_id>.json`: one business's widget (module docstring)."""

    business_id: BusinessId
    synthetic: bool
    """True while Q4c is open: no real business's widget is configured in M8."""
    review: Annotated[SingleLine, Field(max_length=REVIEW_MAX)]
    """Who wrote the texts and who reviewed them (D9 (a): hi and te say "not reviewed by a native
    speaker" until a named reviewer signs off)."""
    enabled: bool
    display_name: Annotated[SingleLine, Field(max_length=DISPLAY_NAME_MAX)]
    allowed_origins: tuple[str, ...] = Field(max_length=ORIGINS_MAX)
    """The origins that may frame the widget (`frame-ancestors`, PR 2). Each in the strict form,
    checked by the loader against the environment (`load_widget_dir`)."""
    languages: tuple[UiLanguageTag, ...] = Field(min_length=1, max_length=LANGUAGES_MAX)
    """The UI languages offered, the first one the default; each one its corpus manifest
    supports. A session's `ui_language` is one of these."""
    texts: tuple[WidgetTexts, ...]
    """One entry per language in `languages`, in any order."""
    handoff_retention_days: int = Field(
        default=DEFAULT_RETENTION_DAYS, ge=RETENTION_DAYS[0], le=RETENTION_DAYS[1]
    )
    """P5: how long this business's hand-offs are kept (PR 2's table)."""
    accent_color: HexColor | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.synthetic:
            raise ValueError("a widget file is synthetic while Q4c is open (synthetic: true)")
        if len(set(self.languages)) != len(self.languages):
            raise ValueError("each language is listed once")
        written = [texts.language for texts in self.texts]
        if len(set(written)) != len(written) or set(written) != set(self.languages):
            raise ValueError("texts has exactly one entry per language in languages")
        if len(set(self.allowed_origins)) != len(self.allowed_origins):
            raise ValueError("each allowed origin is listed once")
        if self.enabled and not self.allowed_origins:
            raise ValueError("an enabled widget names at least one allowed origin")
        if (
            self.accent_color is not None
            and contrast_ratio(self.accent_color, ACCENT_TEXT_COLOR) < MIN_ACCENT_CONTRAST
        ):
            raise ValueError(
                f"accent_color has less than {MIN_ACCENT_CONTRAST}:1 contrast against the text"
                f" written on it ({ACCENT_TEXT_COLOR}, WCAG 2.2 SC 1.4.3)"
            )
        return self

    def texts_for(self, language: str) -> WidgetTexts:
        """The texts of `language`, one of `languages` (`KeyError` otherwise)."""
        for texts in self.texts:
            if texts.language == language:
                return texts
        raise KeyError(language)


# ── What the routes run with ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WidgetBusiness:
    """One business's widget, validated against its manifest and its guard file."""

    file: WidgetFile
    business_type: BusinessType
    health: bool
    """The manifest's type is a health type, or the guard file says the business is health."""
    guidance: tuple[str, ...]
    """The guard file's guidance lines, verbatim (S8); empty for a business without any."""

    @property
    def business_id(self) -> BusinessId:
        return self.file.business_id

    @property
    def enabled(self) -> bool:
        return self.file.enabled


@dataclass(frozen=True)
class WidgetRegistry:
    """Every configured widget (read-only). Empty without a folder."""

    businesses: Mapping[BusinessId, WidgetBusiness]

    @classmethod
    def empty(cls) -> Self:
        return cls(MappingProxyType({}))

    def enabled(self, business_id: str) -> WidgetBusiness | None:
        """`business_id`'s widget when it has one and it is enabled, else None (one answer for
        an unknown id, a business without a file and a disabled widget)."""
        widget = self.businesses.get(business_id)
        return widget if widget is not None and widget.enabled else None


# ── Reading the folder ─────────────────────────────────────────────────────────────────────────


def _read(path: Path, where: str, limit: int) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        raise WidgetConfigError(f"{where} cannot be read") from None
    if len(data) > limit:
        raise WidgetConfigError(f"{where} is larger than {limit} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise WidgetConfigError(f"{where} is not UTF-8") from None


def parse_widget_file(text: str, where: str) -> WidgetFile:
    """`text` as a `WidgetFile`, or `WidgetConfigError` naming `where` and the locations."""
    try:
        return WidgetFile.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error)
    raise WidgetConfigError(f"{where} is not a valid widget file: {detail}")


def _manifest(corpus_dir: Path, business_id: str, where: str) -> CorpusManifest:
    manifest_where = f"{business_id}/{CORPUS_MANIFEST_FILE}"
    path = corpus_dir / business_id / CORPUS_MANIFEST_FILE
    if not path.is_file():
        raise WidgetConfigError(
            f"{where}: {manifest_where}: no corpus manifest in the corpus folder"
            " (VECTAPILOT_CORPUS_DIR)"
        )
    text = _read(path, f"{where}: {manifest_where}", MAX_MANIFEST_BYTES)
    problem = None
    try:
        manifest = load_corpus_manifest(text, where=manifest_where)
    except CorpusError as error:
        problem = str(error)  # names the manifest, its fields and the rule, never a value
    if problem is not None:
        raise WidgetConfigError(f"{where}: {problem}")
    if manifest.business_id != business_id:
        raise WidgetConfigError(f"{where}: {manifest_where} names another business")
    return manifest


def _business_type(manifest: CorpusManifest, where: str) -> BusinessType:
    try:
        return BusinessType(manifest.business_type)
    except ValueError:
        raise WidgetConfigError(
            f"{where}: {manifest.business_id}/{CORPUS_MANIFEST_FILE}: its business_type is not a"
            " known type"
        ) from None


def _check_origins(file: WidgetFile, where: str, *, allow_http_loopback: bool) -> None:
    for index, origin in enumerate(file.allowed_origins):
        try:
            parse_origin(origin, allow_http_loopback=allow_http_loopback)
        except InvalidOrigin as error:
            raise WidgetConfigError(f"{where}: allowed_origins.{index}: {error}") from None


def resolve_widget(
    file: WidgetFile,
    where: str,
    corpus_dir: Path,
    guards: GuardConfig | None,
    *,
    allow_http_loopback: bool,
) -> WidgetBusiness:
    """`file` checked against its manifest, its origins' strict form and the health rule (module
    docstring)."""
    _check_origins(file, where, allow_http_loopback=allow_http_loopback)
    manifest = _manifest(corpus_dir, file.business_id, where)
    business_type = _business_type(manifest, where)
    for index, language in enumerate(file.languages):
        if language not in manifest.languages:
            raise WidgetConfigError(
                f"{where}: languages.{index}: a UI language its corpus manifest does not support"
            )
    guarded = None if guards is None else guards.businesses.get(file.business_id)
    health = business_type in HEALTH_TYPES or (guarded is not None and guarded.health)
    guidance = () if guarded is None else guarded.guidance
    if file.enabled and health and not guidance:
        raise WidgetConfigError(
            f"{where}: an enabled widget of a health business needs its guard guidance"
            " (VECTAPILOT_GUARD_CONFIG_DIR, businesses/<business_id>.json; S8)"
        )
    return WidgetBusiness(file=file, business_type=business_type, health=health, guidance=guidance)


def load_widget_dir(
    directory: Path,
    corpus_dir: Path,
    guards: GuardConfig | None,
    *,
    allow_http_loopback: bool,
) -> WidgetRegistry:
    """The whole folder, read and validated (module docstring); `WidgetConfigError` otherwise.
    `guards` is the app's guard configuration (None without a guard folder: then no business has
    guidance, and an enabled health widget is refused). `allow_http_loopback`: whether an `http`
    origin on `localhost` or `127.0.0.1` is accepted (`create_app`: local only,
    `config.WIDGET_HTTP_ORIGIN_ENVIRONMENTS`)."""
    if not directory.is_dir():
        raise WidgetConfigError(
            "the widget configuration folder (VECTAPILOT_WIDGET_CONFIG_DIR) is missing"
        )
    if not corpus_dir.is_dir():
        raise WidgetConfigError("the corpus folder (VECTAPILOT_CORPUS_DIR) is missing")
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith(".") or (entry.name == README and entry.is_file()):
            continue
        if entry.name != BUSINESSES or not entry.is_dir():
            raise WidgetConfigError(
                f"unexpected entry {entry.name!r} in the widget configuration folder"
            )
    folder = directory / BUSINESSES
    if not folder.is_dir():
        raise WidgetConfigError(f"the widget configuration folder has no {BUSINESSES} folder")
    found: dict[BusinessId, WidgetBusiness] = {}
    for entry in sorted(folder.iterdir()):
        if entry.name.startswith("."):
            continue
        if not entry.is_file() or entry.suffix != JSON_SUFFIX:
            raise WidgetConfigError(f"unexpected entry {entry.name!r} in {BUSINESSES}/")
        where = f"{BUSINESSES}/{entry.name}"
        try:
            business_id = parse_business_id(entry.stem)
        except InvalidBusinessId:
            raise WidgetConfigError(f"{where}: the file is not named after a business id") from None
        file = parse_widget_file(_read(entry, where, MAX_FILE_BYTES), where)
        if file.business_id != business_id:
            raise WidgetConfigError(f"{where} names a different business_id")
        found[business_id] = resolve_widget(
            file, where, corpus_dir, guards, allow_http_loopback=allow_http_loopback
        )
    return WidgetRegistry(MappingProxyType(found))
