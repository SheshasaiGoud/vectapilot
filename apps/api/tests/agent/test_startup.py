"""The agent's fail-fast startup (M5 PR 4): its gateway settings, W11's business-type check, and
`agent_router = on` refused without a live M4 model."""

import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings
from tests.intenteval.harness_helpers import (
    FakeRepo,
    Now,
    dry_model,
    frozen_copy,
    pinned_model,
    real_model,
)
from tests.tools.helpers import CORPUS_DIR, SANDBOX_DIR, copy_sandbox, read_json, write_json
from vectapilot_api.agent.llm import build_agent_gateway
from vectapilot_api.agent.profiles import (
    MAX_MANIFEST_BYTES,
    AgentConfigError,
    build_profiles,
    business_types,
)
from vectapilot_api.agent.routing import (
    REAL_MODEL_TAG,
    ClassifierRouter,
    RouterModelError,
    load_router_model,
)
from vectapilot_api.agent.service import AgentService
from vectapilot_api.agent.types import MAX_STEPS_LIMIT
from vectapilot_api.answer.service import AnswerService
from vectapilot_api.embeddings.base import EmbeddingSpec
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.intenteval.ledger import LEDGER_PATH, PinEntry, parse_entries
from vectapilot_api.intenteval.model import MODEL_FILE, THRESHOLDS_FILE, ModelInfo
from vectapilot_api.intents.decide import RouterThresholds
from vectapilot_api.intents.fallback import LLMIntentFallback
from vectapilot_api.main import create_app
from vectapilot_api.tools.executor import TransactionSource
from vectapilot_api.tools.files import load_tools_dir

WITH_TOOLS: dict[str, Any] = {"tools_config_dir": SANDBOX_DIR, "corpus_dir": CORPUS_DIR}


def problems(**fields: Any) -> str:
    with pytest.raises(ValidationError) as raised:
        make_settings(**fields)
    return str(raised.value)


# ── Settings ─────────────────────────────────────────────────────────────────────────────────


def test_the_defaults_fit_the_plans_proposal() -> None:
    settings = make_settings()
    # [proposal] 4 steps, 6 s, 8 s, 35 s per turn: bounds, never measurements.
    assert (
        settings.agent_max_steps,
        settings.agent_llm_attempt_timeout_seconds,
        settings.agent_llm_deadline_seconds,
        settings.agent_turn_timeout_seconds,
        settings.agent_router,
    ) == (4, 6.0, 8.0, 35.0, "off")
    assert settings.agent_llm_deadline_seconds * settings.agent_max_steps <= (
        settings.agent_turn_timeout_seconds
    )


def test_an_attempt_longer_than_the_deadline_is_refused() -> None:
    assert "agent_llm_deadline_seconds must be >= agent_llm_attempt_timeout_seconds" in problems(
        agent_llm_attempt_timeout_seconds=9.0
    )


def test_steps_that_cannot_fit_the_turn_are_refused() -> None:
    message = "agent_turn_timeout_seconds must be >= agent_llm_deadline_seconds x agent_max_steps"
    assert message in problems(agent_max_steps=5)
    assert message in problems(agent_turn_timeout_seconds=31.9)
    make_settings(agent_turn_timeout_seconds=32.0)  # exactly 8 x 4 fits


def test_the_step_cap_is_bounded_like_the_loops_own() -> None:
    # A turn long enough for 11 steps, so only the field's own bound can refuse it (C05).
    text = problems(agent_max_steps=MAX_STEPS_LIMIT + 1, agent_turn_timeout_seconds=300)
    assert "agent_max_steps\n  Input should be less than or equal to 10" in text
    assert "agent_max_steps" in problems(agent_max_steps=0)
    make_settings(agent_max_steps=MAX_STEPS_LIMIT, agent_turn_timeout_seconds=80)


@pytest.mark.parametrize(
    ("field", "value", "bound"),
    [
        ("agent_llm_attempt_timeout_seconds", 61, 60),
        ("agent_llm_deadline_seconds", 121, 120),
        ("agent_turn_timeout_seconds", 301, 300),
    ],
)
def test_the_agents_time_limits_have_upper_bounds(field: str, value: float, bound: int) -> None:
    # C06: a typo cannot make a step's attempt ten minutes long.
    text = problems(**{field: value})
    assert f"{field}\n  Input should be less than or equal to {bound}" in text


def test_a_deadline_equal_to_the_attempt_timeout_is_allowed() -> None:
    # C01: one attempt and no retry is a valid (if strict) configuration.
    settings = make_settings(agent_llm_attempt_timeout_seconds=8.0)
    assert settings.agent_llm_attempt_timeout_seconds == settings.agent_llm_deadline_seconds


@pytest.mark.parametrize("value", ["none", "auto", "true"])
def test_agent_router_is_off_or_on_only(value: str) -> None:
    # `none` is the eval harness's (`respond_unrouted`): no setting can select it.
    assert "agent_router" in problems(agent_router=value)


def test_on_needs_a_model_folder_and_a_tool_configuration() -> None:
    text = problems(agent_router="on")
    assert "agent_router_model_dir is required when agent_router is 'on'" in text
    assert "tools_config_dir is required when agent_router is 'on'" in text


def test_a_tool_configuration_needs_the_corpus_folder() -> None:
    assert "corpus_dir (VECTAPILOT_CORPUS_DIR) is required with tools_config_dir" in problems(
        tools_config_dir=SANDBOX_DIR
    )


def test_the_agent_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from vectapilot_api.config import Settings

    monkeypatch.setenv("VECTAPILOT_AGENT_MAX_STEPS", "3")
    monkeypatch.setenv("VECTAPILOT_AGENT_ROUTER", "off")
    monkeypatch.setenv("VECTAPILOT_CORPUS_DIR", str(CORPUS_DIR))

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/db",
        redis_url="redis://127.0.0.1:1/0",
    )
    assert (settings.agent_max_steps, settings.corpus_dir) == (3, CORPUS_DIR)


# ── W11's business types ─────────────────────────────────────────────────────────────────────


def test_the_committed_businesses_agree_on_their_types() -> None:
    types = business_types(load_tools_dir(SANDBOX_DIR), CORPUS_DIR)

    assert {business: kind.value for business, kind in types.items()} == {
        "paws-and-co": "veterinary",
        "sunrise-clinic": "human_healthcare",
    }


def test_the_app_builds_every_businesss_agent_profile_at_startup() -> None:
    from fastapi.testclient import TestClient

    app = create_app(make_settings(**WITH_TOOLS))
    with TestClient(app):
        service: AgentService = app.state.agent_service
        assert service.routing == "off"
        assert {b for b, p in service._profiles.items() if p.health} == {
            "paws-and-co",
            "sunrise-clinic",
        }


def corpus_copy(tmp_path: Path) -> Path:
    return Path(shutil.copytree(CORPUS_DIR, tmp_path / "corpus"))


def test_a_tools_json_typo_that_would_switch_w11_off_stops_startup(tmp_path: Path) -> None:
    sandbox = copy_sandbox(tmp_path / "sandbox")
    tools = sandbox / "sunrise-clinic" / "tools.json"
    data = read_json(tools)
    data["business_type"] = "service"  # a clinic configured as a non-health type
    write_json(tools, data)

    with pytest.raises(AgentConfigError, match="different business types"):
        create_app(make_settings(tools_config_dir=sandbox, corpus_dir=CORPUS_DIR))


def test_a_manifest_typo_stops_startup_too(tmp_path: Path) -> None:
    corpus = corpus_copy(tmp_path)
    manifest = corpus / "paws-and-co" / "manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    data["business_type"] = "retail"
    manifest.write_text(json.dumps(data), "utf-8")

    with pytest.raises(AgentConfigError, match="different business types"):
        create_app(make_settings(tools_config_dir=SANDBOX_DIR, corpus_dir=corpus))


def test_an_unknown_manifest_type_stops_startup(tmp_path: Path) -> None:
    corpus = corpus_copy(tmp_path)
    manifest = corpus / "paws-and-co" / "manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    data["business_type"] = "pharmacy"
    manifest.write_text(json.dumps(data), "utf-8")

    with pytest.raises(AgentConfigError, match="not a known type"):
        create_app(make_settings(tools_config_dir=SANDBOX_DIR, corpus_dir=corpus))


def test_a_business_without_its_manifest_stops_startup(tmp_path: Path) -> None:
    corpus = corpus_copy(tmp_path)
    shutil.rmtree(corpus / "sunrise-clinic")

    with pytest.raises(
        AgentConfigError, match=r"sunrise-clinic/manifest\.json: no corpus manifest"
    ):
        create_app(make_settings(tools_config_dir=SANDBOX_DIR, corpus_dir=corpus))
    with pytest.raises(AgentConfigError, match="the corpus folder"):
        create_app(make_settings(tools_config_dir=SANDBOX_DIR, corpus_dir=tmp_path / "nowhere"))


def test_a_manifest_naming_another_business_stops_startup(tmp_path: Path) -> None:
    corpus = corpus_copy(tmp_path)
    manifest = corpus / "paws-and-co" / "manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    data["business_id"] = "sunrise-clinic"
    manifest.write_text(json.dumps(data), "utf-8")

    with pytest.raises(AgentConfigError, match="names another business"):
        create_app(make_settings(tools_config_dir=SANDBOX_DIR, corpus_dir=corpus))


# ── agent_router = on ────────────────────────────────────────────────────────────────────────


def api_spec() -> EmbeddingSpec:
    settings = make_settings()
    return load_embedding_catalog().resolve(
        settings.embeddings_provider, settings.embeddings_model, settings.embeddings_dimension
    )


@pytest.fixture
def world(tmp_path: Path) -> tuple[FakeRepo, Path, Now]:
    root = tmp_path.resolve()
    return FakeRepo(root), frozen_copy(root), Now()


def test_on_without_a_model_folder_stops_startup(tmp_path: Path) -> None:
    settings = make_settings(agent_router="on", agent_router_model_dir=tmp_path, **WITH_TOOLS)

    with pytest.raises(RouterModelError, match="not a complete M4 model folder"):
        create_app(settings)


def test_on_with_a_dry_model_stops_startup(world: tuple[FakeRepo, Path, Now]) -> None:
    repo, dataset, now = world
    folder = dry_model(repo, dataset, now)

    with pytest.raises(RouterModelError, match="not trained on live embeddings"):
        load_router_model(folder, api_spec())
    with pytest.raises(RouterModelError, match="not trained on live embeddings"):
        create_app(make_settings(agent_router="on", agent_router_model_dir=folder, **WITH_TOOLS))


def test_on_with_a_live_model_copied_out_of_its_reports_folder_stops_startup(
    world: tuple[FakeRepo, Path, Now], tmp_path: Path
) -> None:
    repo, dataset, now = world
    folder = real_model(repo, dataset, now)
    moved = Path(shutil.copytree(folder, tmp_path / "elsewhere" / folder.name))

    with pytest.raises(RouterModelError, match="not trained on live embeddings"):
        load_router_model(moved, api_spec())


def test_on_with_a_model_for_another_embedding_model_stops_startup(
    world: tuple[FakeRepo, Path, Now],
) -> None:
    repo, dataset, now = world
    folder = real_model(repo, dataset, now)
    other = api_spec().model_copy(update={"model": "another-embedding-model"})

    with pytest.raises(RouterModelError, match="another embedding model"):
        load_router_model(folder, other)


def voyage_spec() -> EmbeddingSpec:
    return load_embedding_catalog().resolve("voyage", None, 1024)


def test_a_live_model_for_the_apis_embedding_model_loads(
    world: tuple[FakeRepo, Path, Now],
) -> None:
    # The helper's "live" model is labelled voyage-4 (its embed folder is a committed -live-embed
    # one in a fake repository), committed and pinned by M4's own `pin` step: the model the API
    # would embed with when live. Tests run the fake embedder, so the whole app cannot start `on`
    # here; the same check, with voyage-4's spec, accepts the folder and yields its thresholds.
    repo, dataset, now = world
    folder = pinned_model(repo, dataset, now)
    voyage = voyage_spec()

    model = load_router_model(folder, voyage)

    assert model.artifact.embedding_model == voyage.model
    thresholds = json.loads((folder / "thresholds.json").read_text("utf-8"))
    assert (model.thresholds.tau, model.thresholds.sigma) == (
        thresholds["tau"],
        thresholds["sigma"],
    )
    with pytest.raises(RouterModelError, match="another embedding model"):
        create_app(make_settings(agent_router="on", agent_router_model_dir=folder, **WITH_TOOLS))


# ── PR 4 review: the model must be M4's pinned one [kenji, mutation] ────────────────────────


def rewrite_model_info(folder: Path, **changes: Any) -> None:
    """Rewrite the model folder's model.json with `changes` (the artifact stays the same)."""
    path = folder / MODEL_FILE
    info = ModelInfo.model_validate_json(path.read_bytes()).model_copy(update=changes)
    path.write_text(info.model_dump_json(indent=2) + "\n", "utf-8")


def rewrite_pin(repo: FakeRepo, **changes: Any) -> None:
    """Rewrite the ledger's one pin with `changes`."""
    path = repo.root / LEDGER_PATH
    entries = parse_entries(path.read_text("utf-8"))
    lines = [
        (e.model_copy(update=changes) if isinstance(e, PinEntry) else e).model_dump_json()
        for e in entries
    ]
    path.write_text("".join(line + "\n" for line in lines), "utf-8")


@pytest.mark.parametrize(
    "changes",
    [
        {"artifact_sha256": "0" * 64},
        {"temperature": 0.5},
        {"tau": 0.123},
        {"sigma": 0.123},
        {"router_prompt_version": "route-intent.0"},
        {"model_folder": "evals/reports/intents/v0.1/another-local-model"},
    ],
    ids=lambda c: next(iter(c)),
)
def test_a_pin_for_anything_else_is_refused(
    world: tuple[FakeRepo, Path, Now], changes: dict[str, Any]
) -> None:
    repo, dataset, now = world
    folder = pinned_model(repo, dataset, now)
    rewrite_pin(repo, **changes)

    with pytest.raises(RouterModelError, match="not the pinned M4 model"):
        load_router_model(folder, voyage_spec())


def test_an_unpinned_live_model_is_refused(world: tuple[FakeRepo, Path, Now]) -> None:
    repo, dataset, now = world
    folder = real_model(repo, dataset, now)  # live and complete, never pinned

    with pytest.raises(RouterModelError, match="not the pinned M4 model"):
        load_router_model(folder, voyage_spec())


def test_an_unreadable_ledger_pins_nothing(world: tuple[FakeRepo, Path, Now]) -> None:
    repo, dataset, now = world
    folder = pinned_model(repo, dataset, now)
    (repo.root / LEDGER_PATH).write_text("{not json\n", "utf-8")

    with pytest.raises(RouterModelError, match="not the pinned M4 model"):
        load_router_model(folder, voyage_spec())


def test_a_model_trained_from_an_uncommitted_tree_is_refused(
    world: tuple[FakeRepo, Path, Now],
) -> None:
    repo, dataset, now = world
    folder = pinned_model(repo, dataset, now)
    rewrite_model_info(folder, uncommitted_changes=True)

    with pytest.raises(RouterModelError, match="uncommitted changes"):
        load_router_model(folder, voyage_spec())


def test_a_pinned_model_without_a_confidence_band_keeps_tau_none(
    world: tuple[FakeRepo, Path, Now],
) -> None:
    # Decided 2026-11-09: tau None means no band, never 1.0; it must pass through unchanged.
    repo, dataset, now = world
    folder = pinned_model(repo, dataset, now)
    thresholds = folder / THRESHOLDS_FILE
    data = json.loads(thresholds.read_text("utf-8"))
    data["tau"] = None
    thresholds.write_text(json.dumps(data), "utf-8")
    rewrite_pin(repo, tau=None)

    model = load_router_model(folder, voyage_spec())

    assert model.thresholds == RouterThresholds(tau=None, sigma=data["sigma"])


@pytest.mark.parametrize("where", ["dry_run", "folder name", "embed folder"])
def test_each_live_model_condition_refuses_on_its_own(
    world: tuple[FakeRepo, Path, Now], where: str
) -> None:
    # RT1, RT2, RT4: a pinned live model with exactly one condition broken.
    repo, dataset, now = world
    folder = pinned_model(repo, dataset, now)
    load_router_model(folder, voyage_spec())  # accepted as it is
    if where == "dry_run":
        rewrite_model_info(folder, dry_run=True)
    elif where == "embed folder":
        info = ModelInfo.model_validate_json((folder / MODEL_FILE).read_bytes())
        rewrite_model_info(folder, embed_folder=info.embed_folder + "-dry-embed")
    else:
        renamed = folder.parent / (folder.name.removesuffix(REAL_MODEL_TAG) + "-local-copy")
        folder = Path(shutil.copytree(folder, renamed))

    with pytest.raises(RouterModelError, match="not trained on live embeddings"):
        load_router_model(folder, voyage_spec())


def test_a_model_for_another_dimension_is_refused(world: tuple[FakeRepo, Path, Now]) -> None:
    # RT6.
    repo, dataset, now = world
    folder = pinned_model(repo, dataset, now)
    other = voyage_spec().model_copy(update={"dimension": 512})

    with pytest.raises(RouterModelError, match="another embedding model"):
        load_router_model(folder, other)


# ── PR 4 review: Settings reach the agent's gateway and service [mutation] ──────────────────


@pytest.mark.anyio
async def test_the_agent_gateway_uses_the_agents_own_limits() -> None:
    # L01, L02, L04: values that differ from every llm_* default.
    settings = make_settings(
        agent_llm_attempt_timeout_seconds=5.0, agent_llm_deadline_seconds=7.0, llm_max_repairs=1
    )
    async with build_agent_gateway(settings, provider=FakeProvider()) as gateway:
        assert (gateway._attempt_timeout_s, gateway._deadline_s, gateway._max_repairs) == (
            5.0,
            7.0,
            1,
        )
        assert gateway._retry_policy == settings.llm_retry_policy()


def test_the_app_wires_the_agents_settings_and_its_own_gateway() -> None:
    # M02, M03, M04: /v1/answer's gateway is frozen by M3's window; the agent never shares it.
    from fastapi.testclient import TestClient

    settings = make_settings(**WITH_TOOLS, agent_max_steps=3, agent_turn_timeout_seconds=30.0)
    app = create_app(settings)
    with TestClient(app):
        service: AgentService = app.state.agent_service
        answers: AnswerService = app.state.answer_service
        assert service._gateway is not answers._gateway
        assert service._gateway._deadline_s == settings.agent_llm_deadline_seconds
        assert service._gateway._attempt_timeout_s == settings.agent_llm_attempt_timeout_seconds
        assert answers._gateway._deadline_s == settings.llm_deadline_seconds
        assert (service._limits.max_steps, service._turn_timeout_s) == (3, 30.0)
        assert (service.routing, service._router, service._router_artifact) == ("off", None, None)


def test_the_app_starts_on_with_the_router_on_the_agents_gateway(
    world: tuple[FakeRepo, Path, Now], monkeypatch: pytest.MonkeyPatch
) -> None:
    # M01, M05. The pinned model is voyage-4's and tests embed with the fake, so startup's check
    # is run with voyage-4's spec (load_router_model is tested above); no message is routed, so
    # M4's route_message (shesha's H1, a stub) is never called.
    from fastapi.testclient import TestClient

    import vectapilot_api.main as main_module

    repo, dataset, now = world
    folder = pinned_model(repo, dataset, now)
    model = load_router_model(folder, voyage_spec())
    monkeypatch.setattr(main_module, "load_router_model", lambda path, spec: model)
    app = create_app(make_settings(agent_router="on", agent_router_model_dir=folder, **WITH_TOOLS))
    with TestClient(app):
        service: AgentService = app.state.agent_service
        assert service.routing == "on"
        assert isinstance(service._router, ClassifierRouter)
        assert service._router._model is model
        assert isinstance(service._router._fallback, LLMIntentFallback)
        assert service._router._fallback._gateway is service._gateway
        assert service._router_artifact == model.artifact_sha256


# ── PR 4 review: profiles [mutation] ─────────────────────────────────────────────────────────


def test_profiles_need_a_checked_type_for_every_business() -> None:
    # P04.
    tools = load_tools_dir(SANDBOX_DIR)
    with pytest.raises(AgentConfigError, match="checked business type"):
        build_profiles(cast(TransactionSource, None), tools, {})


def test_an_oversized_manifest_stops_startup(tmp_path: Path) -> None:
    # P07: read before it is parsed, so a huge file cannot slow startup.
    corpus = corpus_copy(tmp_path)
    manifest = corpus / "paws-and-co" / "manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    text = json.dumps(data, indent=2)
    manifest.write_text(text + " " * (MAX_MANIFEST_BYTES + 1 - len(text.encode())), "utf-8")

    with pytest.raises(AgentConfigError, match="larger than"):
        business_types(load_tools_dir(SANDBOX_DIR), corpus)
