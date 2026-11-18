"""Log redaction (G10; M7 PR 3): what the API's own log lines may still say about a customer.

Installed only by `create_app`, through `log.configure_logging(level, redact=True)`. The M1
measurement CLI (`llm_cli.py`) and the MCP server keep `log.JsonFormatter`, unchanged.

**`RedactingJsonFormatter`** (a `JsonFormatter` subclass) works on the final payload, after the
record's message is built, its extras are read and its exception is formatted: a
`logging.Filter` cannot, because `JsonFormatter.format` builds `exception` and stringifies extras
after every filter has run. Field by field:
- **Metadata records pass by key.** A record of ours (`vectapilot_api.*`) whose message is one
  of `METADATA_RECORDS` and whose one extra under that name is a dict (`llm_call`, `agent_turn`,
  `tool_call`, `guard`, ...) is metadata only by construction: closed codes, counts, ids and the
  business's own guidance, built from validated models. It is written unchanged, so a redacted
  `llm_call` line is byte-identical to `JsonFormatter`'s and parses to the same `CallRecord`
  (M3's window). Masking it would mangle it: a cost of `0.0000123456789012` or a request id has
  a digit run a card pattern would take.
- **Id fields are checked by key** (`ID_FORMATS`: 32-hex trace ids, UUID action ids, a 64-hex
  sha256, the business slug), inside a metadata record or not. A valid id (for the UUID keys a
  `uuid.UUID` too), or None, passes unchanged; anything else becomes `[redaction_failed]`.
- **Everything else is free text** (the message, every other extra, any value `json.dumps`
  would have turned into text with `str()`): cleaned (`normalize.clean`, as the masker reads
  it), cut (`FIELD_CAP`, `safe_cut`), then masked, so the cut and the masker see the same text.
  A field it cannot mask (the masker raises, or no safe place to cut) becomes
  `[redaction_failed]`: fail closed. A container is walked within bounds (`MAX_DEPTH`,
  `MAX_ITEMS`, identifier-like keys); one outside them is `[redaction_failed]`.
- **`exception`.** For every logger (D-error-line, G10-S1; widened from `uvicorn.error` to every
  record with `exc_info` on 2026-11-18 by shesha), the exception's class name and stack frames
  only, never its message: not the escaped exception's, not a chained one's, not a note, not a
  sub-exception's (masking removes only shapes, so names and symptoms in a message would stay).
  Then every exception is
  masked line by line, at most `EXCEPTION_LINES` lines, each line's leading whitespace kept as
  it is (it holds no data, and it tells a frame line from a source line).
- `format` never raises: logging's own error path (`Handler.handleError`) would print the record's
  message and arguments to stderr. Anything unexpected writes `REDACTION_FAILED` as the message,
  with the time, level and logger only.

**The masker** is `log_masker()`: H2's `mask_pii` over `normalize.clean`, with
`DEFAULT_LOCALE_IDS`. While H2 is the stub (probed once at startup: it raises
`NotImplementedError`) there is no masker (the interim, decided 2026-11-18, shesha): free text is
not masked or cleaned; the container bounds, the key checks and the id checks already apply (a dict
keyed by a number, which `JsonFormatter` writes, is `[redaction_failed]`; no record we write has
one), as do the uvicorn.error rewrite and the access line; and `create_app`'s startup warning
`log_masking status=masker_unavailable` says so. Turning every free-text field into
`[redaction_failed]` instead would blank uvicorn's own lines and every warning while hiding nothing
that was not already written before M7. Once H2 is written, the probe passes and masking is on,
failing closed field by field.

**The access line (G10-S2)** [proposal, diego's PR 1 review]: `AccessLine` (a stricter
`DropQueryString`) rewrites uvicorn's access record at log time. The path becomes the matched
route's template (`/v1/agent/actions/{action_id}/confirm`), or `[unmatched]` for a 404: masking
alone would leave the free text of a malformed `{action_id}`. The template comes from the request
scope, which `RequestScope` (a pure ASGI middleware that screens nothing and changes nothing)
puts in a context variable: uvicorn writes the access line inside the request's task, after
routing, so the filter reads the route the router put in the scope. Also [proposal]: a method
outside the standard ones is `[other]` (h11 accepts any token, so `+919876543210` is a valid
method), as is an unknown HTTP version, and a client address that is not an IP and a port
(`--proxy-headers` copies it from `X-Forwarded-For`) is `[redaction_failed]`.
"""

import ipaddress
import json
import logging
import re
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType, TracebackType
from typing import Any, Final

from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from vectapilot_api.guards.caps import INPUT_CAPS, Surface
from vectapilot_api.guards.locales import DEFAULT_LOCALE_IDS, ascii_view, locales
from vectapilot_api.guards.normalize import clean
from vectapilot_api.guards.pii import mask_pii
from vectapilot_api.guards.types import Clean
from vectapilot_api.log import _STANDARD_ATTRS, DropQueryString, JsonFormatter

Masker = Callable[[str], str]
"""Free text in, the same text with every personal-data span replaced out."""

REDACTION_FAILED: Final = "[redaction_failed]"
TRUNCATED: Final = "[truncated]"
UNMATCHED: Final = "[unmatched]"
OTHER: Final = "[other]"

FIELD_CAP: Final = INPUT_CAPS[Surface.MESSAGE]
"""Characters of one cleaned free-text field (or one exception line) masked: a message's cap
(2,000), within the masker's own (`locales.MASK_CAP`, G3); anything longer is cut first
(`safe_cut`)."""
RAW_CAP: Final = 4 * FIELD_CAP
"""Characters of one raw field cleaned at most, so a huge field costs no more than this. A field
longer than it is always cut before the end of what was cleaned (`RedactingJsonFormatter.text`):
a value may cross `RAW_CAP`."""
EXCEPTION_LINES: Final = 200
"""Lines of one `exception` field kept: the last ones, where the exception itself is."""
MAX_DEPTH: Final = 8
MAX_ITEMS: Final = 256
"""Bounds on a free-text extra that is a container; a bigger one is `[redaction_failed]`."""
MAX_FRAMES: Final = 64
MAX_CHAIN: Final = 16
MAX_GROUP: Final = 16
"""Bounds on the uvicorn.error rewrite: frames per traceback, exceptions per chain, and
sub-exceptions per exception group (Python's own default width is 15)."""

METADATA_RECORDS: Final = frozenset(
    {
        "llm_call",
        "embedding_call",
        "rag_answer",
        "agent_turn",
        "tool_call",
        "extraction",
        "extraction_not_stored",
        "guard",
    }
)
"""The metadata-only records, by message and key. Copied, not imported (their modules pull in the
gateway, the agent or the database); `tests/guards/test_redact.py` pins each to its source."""

_HEX32: Final = re.compile(r"[0-9a-f]{32}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SLUG: Final = re.compile(r"(?=.{3,40}\Z)[a-z0-9]+(?:-[a-z0-9]+)*")
ID_FORMATS: Final[Mapping[str, re.Pattern[str]]] = MappingProxyType(
    {
        "trace_id": _HEX32,
        "action_id": _UUID,
        "withheld_action_id": _UUID,
        "router_artifact": _HEX64,
        "business_id": _SLUG,
    }
)
"""Id fields by key, at any depth, and the one format each may have (`fullmatch`)."""
_KEY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}")
"""A key of a free-text extra's dict: identifier-like, or the whole dict is `[redaction_failed]`
(a key can hold text as well as a value can)."""

HTTP_METHODS: Final = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)
HTTP_VERSIONS: Final = frozenset({"1.0", "1.1", "2", "2.0", "3"})

_CAUSE: Final = "\nThe above exception was the direct cause of the following exception:\n\n"
_CONTEXT: Final = "\nDuring handling of the above exception, another exception occurred:\n\n"


# ── The masker ───────────────────────────────────────────────────────────────────────────────


def log_masker(locale_ids: Sequence[str] = DEFAULT_LOCALE_IDS) -> Masker | None:
    """H2's `mask_pii` as a `Masker`, or None while H2 is the stub (module docstring)."""
    chosen = locales(locale_ids)

    def mask(text: str) -> str:
        return mask_pii(clean(text), chosen).text

    try:
        mask("")
    except NotImplementedError:
        return None
    return mask


def safe_cut(text: str, cap: int = FIELD_CAP) -> str | None:
    """`text` if it fits `cap`, else its longest prefix that ends just before a whitespace with no
    digit, `+` or digit-like form on either side; None if there is none.

    Why there: every pattern that spans whitespace (grouped phones, Aadhaar, cards) has it
    between digits (`locales`), so such a whitespace is never inside a span. Cutting anywhere
    else could leave half a number, too short for any pattern, in the line. `text` is the
    cleaned text the masker reads (`RedactingJsonFormatter.text`): on raw text, a zero-width
    character `clean` drops could make a space between two digit groups look safe."""
    if len(text) <= cap:
        return text
    view = ascii_view(Clean(text[: cap + 2]))  # the cut's own character and the one after it
    for i in range(cap, 0, -1):
        if view[i].isspace() and not _numeric(view[i - 1]) and not _numeric(view[i + 1 : i + 2]):
            return text[:i]
    return None


def _numeric(character: str) -> bool:
    """A digit (of the ASCII view) or a plus; False for the empty string past the text's end."""
    return character.isdigit() or character == "+"


# ── The formatter ────────────────────────────────────────────────────────────────────────────


class RedactingJsonFormatter(JsonFormatter):
    """`JsonFormatter`, redacted field by field (module docstring)."""

    def __init__(self, masker: Masker | None) -> None:
        super().__init__()
        self.masker = masker

    def format(self, record: logging.LogRecord) -> str:
        try:
            return json.dumps(self._payload(record), default=str)
        except Exception:  # never the record's text (module docstring)
            return json.dumps(
                {
                    "time": _time(record),
                    "level": str(record.levelname),
                    "logger": str(record.name),
                    "message": REDACTION_FAILED,
                }
            )

    def _payload(self, record: logging.LogRecord) -> dict[str, object]:
        metadata = _metadata_key(record)
        message = record.getMessage()
        payload: dict[str, object] = {
            "time": _time(record),
            "level": record.levelname,
            "logger": record.name,
            "message": message if metadata is not None else self.text(message),
        }
        for key, value in vars(record).items():
            if key in _STANDARD_ATTRS:
                continue
            if key == metadata:
                payload[key] = _ids_checked(value)
            elif key in ID_FORMATS:
                payload[key] = _id(key, value)
            else:
                payload[key] = self._free(value, 0)
        if record.exc_info:
            payload["exception"] = self.exception(record)
        return payload

    def text(self, text: str) -> str:
        """One free-text field: as it is without a masker, else cleaned, cut to `FIELD_CAP` and
        masked (diego's PR 3 review: `safe_cut` reads the cleaned text the masker masks, so a
        zero-width character dropped by `clean` cannot turn a cut that looked safe into half a
        number)."""
        if self.masker is None:
            return text
        whole = len(text) <= RAW_CAP
        cleaned = clean(text if whole else text[:RAW_CAP])
        # A field cleaned only in part ends wherever RAW_CAP fell: never keep that end.
        cut = safe_cut(cleaned, FIELD_CAP if whole else min(FIELD_CAP, len(cleaned) - 1))
        if cut is None:
            return REDACTION_FAILED
        try:
            masked = self.masker(cut)
        except Exception:  # fail closed: the field, never the line
            return REDACTION_FAILED
        if not isinstance(masked, str):
            return REDACTION_FAILED
        return masked if whole and len(cut) == len(cleaned) else f"{masked} {TRUNCATED}"

    def line(self, line: str) -> str:
        """One exception line: its leading whitespace as it is, the rest as free text."""
        rest = line.lstrip()
        if not rest:
            return line
        return line[: len(line) - len(rest)] + self.text(rest)

    def exception(self, record: logging.LogRecord) -> str:
        exc_info = record.exc_info
        if not exc_info:
            return ""
        text = frames_only(exc_info[1])  # every logger: class names and frames, no message
        if self.masker is None:
            return text
        lines = text.splitlines()
        cut = max(0, len(lines) - EXCEPTION_LINES)
        masked = [self.line(line) for line in lines[cut:]]
        return "\n".join([f"[{cut} lines cut]", *masked] if cut else masked)

    def _free(self, value: object, depth: int) -> object:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            # A number is written as digits, and digits can be a phone number.
            masked = self.text(str(value))
            return value if masked == str(value) else masked
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping | list | tuple):
            if depth >= MAX_DEPTH or len(value) > MAX_ITEMS:
                return REDACTION_FAILED
            if isinstance(value, Mapping):
                return self._free_mapping(value, depth)
            return [self._free(item, depth + 1) for item in value]
        try:
            text = str(value)  # what `json.dumps(default=str)` would write
        except Exception:  # fail closed: the field, never the line
            return REDACTION_FAILED
        return self.text(text)

    def _free_mapping(self, value: Mapping[Any, object], depth: int) -> object:
        if not all(isinstance(key, str) and _KEY.fullmatch(key) for key in value):
            return REDACTION_FAILED
        return {
            key: _id(key, item) if key in ID_FORMATS else self._free(item, depth + 1)
            for key, item in value.items()
        }


def _time(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat()


def _metadata_key(record: logging.LogRecord) -> str | None:
    """The record's metadata key when it is one of ours (module docstring), else None."""
    name = record.msg
    if (
        isinstance(name, str)
        and name in METADATA_RECORDS
        and not record.args
        and (record.name == "vectapilot_api" or record.name.startswith("vectapilot_api."))
        and isinstance(vars(record).get(name), dict)
    ):
        return name
    return None


def _id(key: str, value: object) -> object:
    """`value` unchanged when it is None or a valid id for `key`, else `[redaction_failed]`. A
    `uuid.UUID` is checked as the text `json.dumps(default=str)` writes for it."""
    text = str(value) if isinstance(value, uuid.UUID) and ID_FORMATS[key] is _UUID else value
    if value is None or (isinstance(text, str) and ID_FORMATS[key].fullmatch(text)):
        return value
    return REDACTION_FAILED


def _ids_checked(value: object) -> object:
    """A metadata record as it is, but every id field checked (module docstring)."""
    if isinstance(value, dict):
        return {
            key: _id(key, item) if key in ID_FORMATS else _ids_checked(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_ids_checked(item) for item in value]
    return value


# ── The uvicorn.error rewrite (G10-S1) ───────────────────────────────────────────────────────


def frames_only(exc: BaseException | None) -> str:
    """`exc` as Python prints it, chain included, but every exception as its class name only:
    no message, no note, no `SyntaxError` text (D-error-line). Sub-exceptions of a group too."""
    if exc is None:
        return ""
    # Without the last line break, as `logging.Formatter.formatException` writes it.
    return "".join(_frames_of_chain(exc, depth=0)).removesuffix("\n")


def _frames_of_chain(exc: BaseException, depth: int) -> list[str]:
    chain: list[tuple[str, BaseException]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    link = ""
    while current is not None and id(current) not in seen and len(chain) < MAX_CHAIN:
        seen.add(id(current))
        chain.append((link, current))
        if current.__cause__ is not None:
            link, current = _CAUSE, current.__cause__
        elif current.__context__ is not None and not current.__suppress_context__:
            link, current = _CONTEXT, current.__context__
        else:
            current = None
    parts: list[str] = []
    following = ""
    for link, one in reversed(chain):  # the oldest first, as Python prints it
        parts.append(following)
        parts.extend(_frames_of_one(one, depth))
        following = link
    return parts


def _frames_of_one(exc: BaseException, depth: int) -> list[str]:
    lines: list[str] = []
    tb: TracebackType | None = exc.__traceback__
    if tb is not None:
        lines.append("Traceback (most recent call last):\n")
        lines.extend(traceback.format_tb(tb, limit=-MAX_FRAMES))
    lines.append(_class_name(type(exc)) + "\n")
    if isinstance(exc, BaseExceptionGroup) and depth < MAX_CHAIN:
        for number, sub in enumerate(exc.exceptions[:MAX_GROUP], start=1):
            lines.append(f"+---------------- {number} ----------------\n")
            lines.extend(_frames_of_chain(sub, depth + 1))
        if len(exc.exceptions) > MAX_GROUP:
            lines.append(f"+---------------- and {len(exc.exceptions) - MAX_GROUP} more\n")
    return lines


def _class_name(cls: type[BaseException]) -> str:
    module = cls.__module__
    name = cls.__qualname__
    return name if module in ("builtins", "__main__") else f"{module}.{name}"


# ── The access line (G10-S2) ─────────────────────────────────────────────────────────────────

_REQUEST_SCOPE: Final[ContextVar[Scope | None]] = ContextVar(
    "vectapilot_request_scope", default=None
)


class RequestScope:
    """Pure ASGI middleware for the access line only (module docstring): it puts the request's
    scope in a context variable and changes nothing else. Never reset: uvicorn runs each request
    in a task with a fresh context, and writes the 500 of an escaped exception after the app has
    returned, in the same task."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            _REQUEST_SCOPE.set(scope)
        await self.app(scope, receive, send)


def route_template(scope: Scope | None) -> str:
    """The template of the route the router matched in `scope`, or `[unmatched]`.

    FastAPI names the route in `scope["route"]`; a plain Starlette route (the docs pages) only
    leaves its endpoint, so it is looked up among the app's routes by identity."""
    if scope is None:
        return UNMATCHED
    path = getattr(scope.get("route"), "path", None)
    if isinstance(path, str):
        return path
    endpoint = scope.get("endpoint")
    routes = getattr(scope.get("app"), "routes", ())
    if endpoint is not None:
        for route in routes:
            if isinstance(route, Route) and route.endpoint is endpoint:
                return route.path
    return UNMATCHED


class AccessLine(DropQueryString):
    """uvicorn's access record with every field it takes from the request checked (module
    docstring). A record of another shape keeps nothing of its own: `[redaction_failed]`."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not (isinstance(args, tuple) and len(args) == 5):
            record.msg, record.args = REDACTION_FAILED, None
            return True
        client, method, _path, version, status = args
        if not isinstance(status, int) or isinstance(status, bool):
            record.msg, record.args = REDACTION_FAILED, None
            return True
        record.args = (
            _client(client),
            method if isinstance(method, str) and method in HTTP_METHODS else OTHER,
            route_template(_REQUEST_SCOPE.get()),
            version if isinstance(version, str) and version in HTTP_VERSIONS else OTHER,
            status,
        )
        return True


def _client(client: object) -> str:
    """`host:port` as uvicorn writes it, or `""` without a client; else `[redaction_failed]`."""
    if client == "":
        return ""
    if isinstance(client, str):
        host, _, port = client.rpartition(":")
        if port.isascii() and port.isdigit():
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return REDACTION_FAILED
            return client
    return REDACTION_FAILED
