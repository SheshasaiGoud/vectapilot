"""Structured logging: one JSON object per line — easy to search, filter and ship to a log store."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Final

# Attributes every LogRecord has; anything else on a record came from `extra=` and is kept.
# `color_message` is uvicorn's copy of the message with terminal colour codes — noise in JSON.
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "color_message",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class DropQueryString(logging.Filter):
    """Cut the query string off uvicorn's access-log line.

    Query parameters can carry customer text (an uploaded document's title, for example), and
    access logs are kept longer and read more widely than the data itself. Uvicorn logs
    `(client, method, path, http_version, status)` with the path percent-encoded, so the first
    `?` is always where the query string starts.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


UNTRUSTED_PARSER_LOGGERS: tuple[str, ...] = ("pypdf",)
"""Loggers of libraries that parse uploaded files. pypdf's warnings quote bytes of the file (a
duplicated dictionary key, an unknown encoding name), so a hostile PDF could write into our logs
(M6 E9; ingestion's PDFs in M2 too). Decided by shesha, 2026-11-13: silenced here, centrally."""
SILENT_LEVEL = logging.CRITICAL + 1
"""Above every level these libraries log at: every record is dropped before any handler."""


def silence_untrusted_parsers() -> None:
    """Drop every record of `UNTRUSTED_PARSER_LOGGERS`. Their child loggers set no level of their
    own, so they inherit this one. Idempotent; called by `configure_logging` at startup and by the
    modules that parse uploads, so it holds whatever the entry point."""
    for name in UNTRUSTED_PARSER_LOGGERS:
        logging.getLogger(name).setLevel(SILENT_LEVEL)


LOG_MASKING: Final = "log_masking"
"""The startup warning of `configure_logging(..., redact=True)` if its probe finds H2 a stub (not
since H2 was written, 2026-11-20): free text in the API's log lines is then not masked
(`guards/redact.py`, the interim). Fixed codes only."""


def configure_logging(level: str, *, redact: bool = False) -> None:
    """Send every log line — ours and uvicorn's — to stdout as JSON.

    `redact=True` is `create_app`'s alone (M7 PR 3, G10): the lines are redacted
    (`guards.redact.RedactingJsonFormatter`) and the access line names the route's template
    (`AccessLine`, which needs `create_app`'s `RequestScope`). The guards are imported only then,
    so the M1 measurement CLI and the MCP server, which import this module for `JsonFormatter`,
    never load them.
    """
    access_filter: DropQueryString = DropQueryString()
    formatter: logging.Formatter = JsonFormatter()
    masking_off = False
    if redact:
        from vectapilot_api.guards.redact import AccessLine, RedactingJsonFormatter, log_masker

        masker = log_masker()
        masking_off = masker is None
        access_filter, formatter = AccessLine(), RedactingJsonFormatter(masker)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn installs its own plain-text handlers; route its loggers through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    silence_untrusted_parsers()
    # Exactly one access filter per process, the one this call asks for (`AccessLine` is a
    # stricter `DropQueryString`), whatever an earlier call installed.
    access = logging.getLogger("uvicorn.access")
    for installed in [f for f in access.filters if isinstance(f, DropQueryString)]:
        access.removeFilter(installed)
    access.addFilter(access_filter)
    if masking_off:
        logging.getLogger("vectapilot_api").warning(
            LOG_MASKING, extra={"status": "masker_unavailable"}
        )
