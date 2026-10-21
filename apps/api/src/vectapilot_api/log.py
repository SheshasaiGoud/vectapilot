"""Structured logging: one JSON object per line — easy to search, filter and ship to a log store."""

import json
import logging
import sys
from datetime import UTC, datetime

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


def configure_logging(level: str) -> None:
    """Send every log line — ours and uvicorn's — to stdout as JSON."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn installs its own plain-text handlers; route its loggers through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, DropQueryString) for f in access.filters):  # one per process
        access.addFilter(DropQueryString())
