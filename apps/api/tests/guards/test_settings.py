"""M7 PR 1: the guard settings (`guard_routes`, `guard_answer`, `guard_config_dir`) and
`create_app`'s requirement and startup load (G4, G14).

Files only, no database: `create_app` validates everything before any connection is made.
"""

import json
import logging
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, NoReturn

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings
from tests.guards.helpers import (
    BUSINESSES,
    CORPUS_DIR,
    GUARD_DIR,
    copy_fixtures,
    edit_json,
    sample_guard_dir,
    use_answer_key_selftest,
)
from tests.tools.helpers import CORPUS_DIR as SAMPLE_CORPUS
from tests.tools.helpers import REPO_ROOT, SANDBOX_DIR
from vectapilot_api import main
from vectapilot_api.business import HEADER_ENVIRONMENTS
from vectapilot_api.config import (
    GUARD_CONFIG_REFUSED,
    GUARD_OFF_ENVIRONMENTS,
    Settings,
)
from vectapilot_api.guards.config import GuardConfig, GuardConfigError
from vectapilot_api.guards.selftest import SelftestStatus, run_selftest
from vectapilot_api.guards.types import Folded, TermSet
from vectapilot_api.log import LOG_MASKING
from vectapilot_api.main import GUARD_SELFTEST, GUARDS_NOT_CONFIGURED, create_app

EXTRACTION_DIR: Final = REPO_ROOT / "data" / "extraction"
PRODUCTION: Final[dict[str, Any]] = {
    "environment": "production",
    "llm_provider": "anthropic",
    "anthropic_api_key": "not-a-real-key",
    "api_allow_paid_llm": True,  # production is never fake, so the API needs the opt-in
    "embeddings_provider": "voyage",
    "voyage_api_key": "not-a-real-key",
}
GUARD_ENV: Final = (
    "VECTAPILOT_GUARD_ROUTES",
    "VECTAPILOT_GUARD_ANSWER",
    "VECTAPILOT_GUARD_CONFIG_DIR",
)


def refused(**overrides: Any) -> str:
    with pytest.raises(ValidationError) as error:
        make_settings(**overrides)
    return str(error.value.errors(include_input=False)[0]["msg"])


# ── Settings ─────────────────────────────────────────────────────────────────────────────────


def test_the_defaults_guard_the_routes_and_leave_the_answer_path_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in GUARD_ENV:  # a developer's shell (or, from PR 4, compose) may set them
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/db",
        redis_url="redis://127.0.0.1:1/0",
    )

    assert (settings.guard_routes, settings.guard_answer) == ("on", "off")
    assert settings.guard_config_dir is None


def test_the_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VECTAPILOT_GUARD_CONFIG_DIR", str(GUARD_DIR))
    monkeypatch.setenv("VECTAPILOT_CORPUS_DIR", str(CORPUS_DIR))
    monkeypatch.setenv("VECTAPILOT_GUARD_ROUTES", "off")
    monkeypatch.setenv("VECTAPILOT_GUARD_ANSWER", "off")

    def from_env() -> Settings:
        return Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@127.0.0.1:1/db",
            redis_url="redis://127.0.0.1:1/0",
        )

    settings = from_env()
    assert settings.guard_config_dir == GUARD_DIR
    assert (settings.guard_routes, settings.guard_answer) == ("off", "off")
    monkeypatch.setenv("VECTAPILOT_GUARD_ANSWER", "on")  # accepted since M7 PR 8 wires it
    assert from_env().guard_answer == "on"


def test_guard_routes_off_is_allowed_only_where_the_header_is() -> None:
    assert GUARD_OFF_ENVIRONMENTS == HEADER_ENVIRONMENTS
    for environment in sorted(GUARD_OFF_ENVIRONMENTS):
        assert make_settings(environment=environment, guard_routes="off").guard_routes == "off"

    assert refused(**PRODUCTION, guard_routes="off") == (
        "Value error, guard_routes may be 'off' only when environment is local, test or ci"
    )


@pytest.mark.parametrize("field", ["guard_routes", "guard_answer"])
def test_only_off_and_on_are_accepted(field: str) -> None:
    with pytest.raises(ValidationError) as error:
        make_settings(**{field: "maybe"})
    assert error.value.errors(include_input=False)[0]["loc"] == (field,)


def test_the_guard_folder_needs_the_corpus_folder() -> None:
    assert refused(guard_config_dir=GUARD_DIR) == (
        "Value error, corpus_dir (VECTAPILOT_CORPUS_DIR) is required with guard_config_dir: each"
        " business's type and languages come from its corpus manifest"
    )


@pytest.mark.parametrize(
    ("overrides", "answer", "serves"),
    [
        ({}, False, False),
        ({"tools_config_dir": SANDBOX_DIR, "corpus_dir": SAMPLE_CORPUS}, False, True),
        ({"extraction_config_dir": EXTRACTION_DIR, "corpus_dir": SAMPLE_CORPUS}, False, True),
        ({}, True, True),
        (
            {"tools_config_dir": SANDBOX_DIR, "corpus_dir": SAMPLE_CORPUS, "guard_routes": "off"},
            False,
            False,
        ),
        ({"guard_routes": "off"}, True, True),
    ],
)
def test_when_a_guarded_route_serves_businesses(
    overrides: dict[str, Any], answer: bool, serves: bool
) -> None:
    def built(fields: dict[str, Any]) -> Settings:
        return make_settings(**fields, guard_answer="on" if answer else "off")

    settings = built(overrides)
    assert settings.guarded_routes_serve_businesses() is serves
    assert settings.guards_not_configured() is serves
    configured = built({"corpus_dir": CORPUS_DIR} | overrides | {"guard_config_dir": GUARD_DIR})
    assert configured.guards_not_configured() is False


# ── create_app: the requirement ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "serving",
    [
        {"tools_config_dir": SANDBOX_DIR, "corpus_dir": SAMPLE_CORPUS},
        {"extraction_config_dir": EXTRACTION_DIR, "corpus_dir": SAMPLE_CORPUS},
        {"guard_answer": "on"},
    ],
    ids=["tools", "extraction", "answer"],
)
def test_production_refuses_to_start_guarded_routes_without_the_guard_folder(
    serving: dict[str, Any],
) -> None:
    settings = make_settings(**PRODUCTION, **serving)

    with pytest.raises(ValidationError) as error:
        create_app(settings)
    (only,) = error.value.errors(include_input=False)
    assert only["loc"] == ("guard_config_dir",)
    assert only["msg"] == GUARD_CONFIG_REFUSED


def test_production_without_a_served_business_starts_without_the_folder() -> None:
    app = create_app(make_settings(**PRODUCTION))

    assert app.state.guards is None
    assert app.state.guard_selftest is None


@pytest.mark.parametrize("environment", sorted(GUARD_OFF_ENVIRONMENTS))
def test_local_test_and_ci_start_and_the_routes_will_hand_off(environment: str) -> None:
    settings = make_settings(
        environment=environment, tools_config_dir=SANDBOX_DIR, corpus_dir=SAMPLE_CORPUS
    )

    app = create_app(settings)

    assert app.state.guards is None
    # PR 6-8 read this to hand off every message as `guards_not_configured`.
    assert settings.guards_not_configured()


# ── create_app: loading the folder ───────────────────────────────────────────────────────────


def test_the_app_loads_and_self_tests_the_folder_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_answer_key_selftest(monkeypatch)  # the self-test's plumbing, not H1 (test_match.py)
    app = create_app(make_settings(guard_config_dir=GUARD_DIR, corpus_dir=CORPUS_DIR))

    assert isinstance(app.state.guards, GuardConfig)
    assert tuple(app.state.guards.businesses) == BUSINESSES
    result = app.state.guard_selftest
    assert result.status is SelftestStatus.PASSED
    assert result.terms == len(app.state.guards.terms) and result.positives > 0


def test_an_unavailable_matcher_lets_the_app_start_and_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stand-in for the stub (not H1): the app starts, the routes will hand off (G3, PR 6-8).
    def stub(text: Folded, terms: TermSet) -> NoReturn:
        raise NotImplementedError

    monkeypatch.setattr(main, "run_selftest", lambda config: run_selftest(config, stub))
    app = create_app(make_settings(guard_config_dir=GUARD_DIR, corpus_dir=CORPUS_DIR))

    assert app.state.guard_selftest.status is SelftestStatus.MATCHER_UNAVAILABLE


def test_the_app_refuses_to_start_on_an_invalid_folder(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    edit_json(guard / "businesses" / "fx-vet.json", lambda d: d.pop("guidance"))

    with pytest.raises(GuardConfigError, match="a health business has guidance lines"):
        create_app(make_settings(guard_config_dir=guard, corpus_dir=corpus))
    with pytest.raises(GuardConfigError, match="is missing"):
        create_app(make_settings(guard_config_dir=tmp_path / "nowhere", corpus_dir=corpus))


def corpus_with_samples(destination: Path) -> Path:
    """The fixture corpus plus the sample businesses' manifests, as one corpus folder."""
    corpus = shutil.copytree(CORPUS_DIR, destination)
    for business in ("paws-and-co", "sunrise-clinic"):
        (corpus / business).mkdir()
        shutil.copy(SAMPLE_CORPUS / business / "manifest.json", corpus / business)
    return corpus


@pytest.mark.parametrize(
    "serving",
    [{"tools_config_dir": SANDBOX_DIR}, {"extraction_config_dir": EXTRACTION_DIR}],
    ids=["tools", "extraction"],
)
def test_a_health_business_a_guarded_route_serves_needs_its_guard_file(
    tmp_path: Path, serving: dict[str, Any]
) -> None:
    corpus = corpus_with_samples(tmp_path / "corpus")
    settings = make_settings(**serving, guard_config_dir=GUARD_DIR, corpus_dir=corpus)

    with pytest.raises(GuardConfigError) as error:
        create_app(settings)
    assert str(error.value) == (
        "paws-and-co: a guarded health business has no businesses/paws-and-co.json (its"
        " guidance, G4)"
    )


def test_with_guard_routes_off_the_served_businesses_still_need_their_files(
    tmp_path: Path,
) -> None:
    # The folder is read whenever it is set, so a test run with `off` cannot hide a broken file.
    corpus = corpus_with_samples(tmp_path / "corpus")
    settings = make_settings(
        tools_config_dir=SANDBOX_DIR,
        guard_config_dir=GUARD_DIR,
        corpus_dir=corpus,
        guard_routes="off",
    )

    with pytest.raises(GuardConfigError, match="paws-and-co: a guarded health business"):
        create_app(settings)


def test_the_sample_businesses_guard_folder_lets_production_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The helper other tests use to start production with a guarded route (fixture terms only).
    use_answer_key_selftest(monkeypatch)
    settings = make_settings(
        **PRODUCTION,
        tools_config_dir=SANDBOX_DIR,
        extraction_config_dir=EXTRACTION_DIR,
        corpus_dir=SAMPLE_CORPUS,
        guard_config_dir=sample_guard_dir(tmp_path / "guard"),
    )

    app = create_app(settings)
    guards = app.state.guards
    assert tuple(guards.businesses) == ("paws-and-co", "sunrise-clinic")
    assert guards.for_business("paws-and-co").not_covered == {"te"}
    assert app.state.guard_selftest.status is SelftestStatus.PASSED


# ── create_app: the startup warnings [proposal] ──────────────────────────────────────────────

with_restored_logging = pytest.mark.usefixtures("restored_logging")


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """`create_app` points the root logger at `capsys`'s stream, which closes after the test: put
    the previous handlers back, or a later test's records go to a closed stream (and logging's
    error report, record included, to that test's stderr)."""
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, root.handlers[:]
    try:
        yield
    finally:
        root.setLevel(saved_level)
        root.handlers[:] = saved_handlers


def warnings(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """The guards' WARNING lines `create_app` wrote (its JSON handler writes to stdout). The log
    formatter's own `log_masking` warning (while H2 is the stub; tests/guards/test_redact.py) is
    not one of them."""
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    return [line for line in lines if line["level"] == "WARNING" and line["message"] != LOG_MASKING]


@with_restored_logging
def test_local_routes_without_the_folder_warn_once_with_a_fixed_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_app(make_settings(tools_config_dir=SANDBOX_DIR, corpus_dir=SAMPLE_CORPUS))

    [warning] = warnings(capsys)
    assert (warning["message"], warning["environment"]) == (GUARDS_NOT_CONFIGURED, "test")


@with_restored_logging
def test_an_unavailable_matcher_warns_with_its_status_and_never_a_term(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def stub(text: Folded, terms: TermSet) -> NoReturn:
        raise NotImplementedError

    monkeypatch.setattr(main, "run_selftest", lambda config: run_selftest(config, stub))
    create_app(make_settings(guard_config_dir=GUARD_DIR, corpus_dir=CORPUS_DIR))

    [warning] = warnings(capsys)
    assert (warning["message"], warning["status"]) == (GUARD_SELFTEST, "matcher_unavailable")
    assert "chest" not in json.dumps(warning)  # a fixture term


@with_restored_logging
def test_a_configured_folder_that_passes_warns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_answer_key_selftest(monkeypatch)
    create_app(
        make_settings(
            tools_config_dir=SANDBOX_DIR,
            corpus_dir=SAMPLE_CORPUS,
            guard_config_dir=sample_guard_dir(tmp_path / "guard"),
        )
    )

    assert warnings(capsys) == []
