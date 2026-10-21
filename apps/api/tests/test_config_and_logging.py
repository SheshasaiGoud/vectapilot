import io
import json
import logging
import os
import sys
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from vectapilot_api.config import Settings
from vectapilot_api.log import DropQueryString, JsonFormatter, configure_logging

CANARY = "canary-pw-7f3a"  # fake on purpose: a recognisable marker, not a real credential
CANARY_DATABASE_URL = f"postgresql+asyncpg://u:{CANARY}-db@db.example:5432/app"
CANARY_REDIS_URL = f"redis://:{CANARY}-redis@cache.example:6379/0"


@pytest.fixture
def no_vectapilot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings reads every VECTAPILOT_* variable, so clear them all: an exported shell value
    # (e.g. VECTAPILOT_LOG_LEVEL=debug) must not change what these tests see.
    # `_env_file=None` in each test separately keeps the local apps/api/.env out.
    for name in [k for k in os.environ if k.startswith("VECTAPILOT_")]:
        monkeypatch.delenv(name)


@pytest.mark.usefixtures("no_vectapilot_env")
def test_missing_required_settings_fail_fast() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    missing = {e["loc"][0] for e in error.value.errors()}
    assert missing == {"database_url", "redis_url"}


@pytest.mark.usefixtures("no_vectapilot_env")
@pytest.mark.parametrize(
    ("bad_database_url", "error_type"),
    [
        pytest.param(f"mysql://u:{CANARY}@h/db", "url_scheme", id="url_scheme"),
        pytest.param(f"postgresql://u:{CANARY}@h:99999/db", "url_parsing", id="url_parsing"),
    ],
)
def test_settings_error_does_not_echo_the_bad_value(bad_database_url: str, error_type: str) -> None:
    # A DSN can carry a password; a startup error lands in logs, so it must not repeat the value.
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None, database_url=bad_database_url, redis_url="redis://localhost:6379/0"
        )

    # Still fail fast on the DSN itself (wrong scheme, port > 65535), not just "some error".
    assert [(e["loc"], e["type"]) for e in error.value.errors()] == [
        (("database_url",), error_type)
    ]
    for text in (str(error.value), repr(error.value)):
        assert CANARY not in text
        assert "input_value" not in text
    # `.errors()` still holds the raw input (the flag does not cover it); `include_input=False`
    # drops it. Safe only for pydantic's own messages: a validator that puts the value into its
    # message leaks it through both str() and this call.
    assert CANARY not in str(error.value.errors(include_input=False))


def _settings_with_canary_passwords() -> Settings:
    return Settings(
        _env_file=None,
        database_url=CANARY_DATABASE_URL,
        redis_url=CANARY_REDIS_URL,
        anthropic_api_key=f"{CANARY}-key",
        voyage_api_key=f"{CANARY}-voyage-key",
    )


def _json_log_line(settings: Settings) -> str:
    record = logging.LogRecord("vectapilot_api", logging.INFO, __file__, 1, "startup", None, None)
    record.__dict__["settings"] = settings  # what `logger.info(..., extra={"settings": ...})` does
    return JsonFormatter().format(record)


@pytest.mark.usefixtures("no_vectapilot_env")
@pytest.mark.parametrize(
    "render",
    [
        pytest.param(repr, id="repr"),
        pytest.param(str, id="str"),
        pytest.param(lambda s: s.model_dump_json(), id="model_dump_json"),
        pytest.param(lambda s: str(s.model_dump()), id="model_dump"),
        pytest.param(_json_log_line, id="json_log_extra"),
    ],
)
def test_valid_settings_never_print_a_password(render: Callable[[Settings], str]) -> None:
    # The DSNs and the API key carry the canary; every way of turning Settings into text masks it.
    assert CANARY not in render(_settings_with_canary_passwords())


@pytest.mark.usefixtures("no_vectapilot_env")
def test_secret_dsns_unwrap_to_the_real_url() -> None:
    settings = _settings_with_canary_passwords()

    assert str(settings.database_url.get_secret_value()) == CANARY_DATABASE_URL
    assert str(settings.redis_url.get_secret_value()) == CANARY_REDIS_URL


@pytest.mark.usefixtures("client")  # runs the lifespan, which builds the clients
def test_app_factory_gives_the_clients_the_real_urls(app: FastAPI, settings: Settings) -> None:
    engine_url = app.state.db_engine.url.render_as_string(hide_password=False)
    assert engine_url == str(settings.database_url.get_secret_value())
    redis_dsn = settings.redis_url.get_secret_value()
    redis_kwargs = app.state.redis.connection_pool.connection_kwargs
    assert (redis_kwargs["host"], redis_kwargs["port"], redis_kwargs["password"]) == (
        redis_dsn.host,
        redis_dsn.port,
        redis_dsn.password,
    )


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


def test_access_log_drops_the_query_string(monkeypatch: pytest.MonkeyPatch) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])  # configure_logging fills this list; restored after
    monkeypatch.setattr(root, "level", root.level)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    configure_logging("INFO")
    configure_logging("INFO")  # once per app: the filter must not stack up
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, DropQueryString) for f in access.filters) == 1

    # The same call uvicorn makes for every request.
    path = "/v1/kb/documents?title=canary-title&content_kind=faq"
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5123", "POST", path, "1.1", 201)

    assert json.loads(stdout.getvalue())["message"] == (
        '127.0.0.1:5123 - "POST /v1/kb/documents HTTP/1.1" 201'
    )
    assert "canary" not in stdout.getvalue()


@pytest.mark.parametrize(
    "args",
    [("127.0.0.1:5123", "GET", "/healthz", "1.1", 200), ("only", "two"), None],
)
def test_drop_query_string_leaves_other_records_alone(args: tuple[object, ...] | None) -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "msg", args, None)

    assert DropQueryString().filter(record) is True
    assert record.args == args
