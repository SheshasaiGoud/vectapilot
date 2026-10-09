import json
import logging

import pytest
from pydantic import ValidationError

from vectapilot_api.config import Settings
from vectapilot_api.log import JsonFormatter


def test_missing_required_settings_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VECTAPILOT_DATABASE_URL", raising=False)
    monkeypatch.delenv("VECTAPILOT_REDIS_URL", raising=False)

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    missing = {e["loc"][0] for e in error.value.errors()}
    assert missing == {"database_url", "redis_url"}


def test_json_formatter_writes_one_json_object_including_extras() -> None:
    record = logging.LogRecord(
        "vectapilot_api", logging.INFO, __file__, 1, "hello %s", ("world",), None
    )
    record.__dict__["request_id"] = "req-123"  # what `logger.info(..., extra={...})` does

    line = JsonFormatter().format(record)

    data = json.loads(line)
    assert data["message"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "vectapilot_api"
    assert data["request_id"] == "req-123"
    assert "\n" not in line
