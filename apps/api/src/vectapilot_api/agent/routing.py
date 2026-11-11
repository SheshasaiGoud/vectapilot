"""`agent_router`: M4's router in front of the agent loop (decided D7 (a); ADR 0015 decision 10 as
amended 2026-11-10; ADR 0016 decision 7; M5 PR 4).

**Off by default.** While `agent_router` is `off`, a health business's `/v1/agent` fails closed
before any tool (W11, `service.py`), because no deterministic safety step exists without the
router. `on` puts `route_message` (M4's H1, `intents/decide.py`) first: its safety floor is the
deterministic step. The service decides what each route means for the agent; this module only
produces the decision.

**`ClassifierRouter`** embeds the message as a query (the API's own `EmbeddingRunner`, operation
`agent-route`), scores it with the committed classifier artifact (standard library only, ADR 0015
decision 4) and calls `route_message` with the artifact's thresholds and an `IntentFallback` on
the agent's gateway (alias `small`, the router's own prompt `route-intent.1`). It adds nothing to
the decision: no table of its own, no retry. `route_message`'s body is shesha's M4 H1 and is
still a stub, so under `on` every message raises `NotImplementedError` here and the service hands
it off (fail closed). It is never implemented in this module.

Known cost, unmeasured: an `on` message that routes to `rag` is embedded twice (here and by the
answer's search). ADR 0015 decision 3's "reuse the retrieval vector" needs the search to accept a
vector; not in M5.

**Startup refuses `on` without a live model** (`load_router_model`, `RouterModelError`): the
folder named by `agent_router_model_dir` must be a complete M4 model folder (`read_model`: the
artifact, calibration and thresholds made for each other), trained on live embeddings (`dry_run`
false, a `-local-model` folder under `evals/reports/intents/<version>/` whose embed folder is a
`-live-embed` one: M4's "dry or real is decided by where the vectors came from"), for the API's
own embedding model and dimension (a vector from another model would be meaningless to the
weights, ADR 0015 decision 3), trained from a clean tree (`uncommitted_changes` false), and
**the model M4 pinned** [PR 4 review, kenji]. The API cannot ask git whether the folder is
committed (its image has no git and no repository); **the pin is what proves committed**: M4's
`pin` step refuses unless every model file is in HEAD and the tree is clean, and it records the
artifact sha256, T, tau, sigma and `ROUTER_PROMPT_VERSION` in the ledger
(`evals/reports/intents/test-runs.jsonl`, next to the version folders). Startup requires a
`PinEntry` in that ledger naming this folder with exactly the loaded artifact's sha256, T, tau
and sigma and this code's router prompt version: M4's reported numbers describe that tuple, and
tau was chosen together with the fallback's prompt, so anything else (an unpinned retrain, a
bumped prompt) is refused. Under compose that means mounting `evals/reports/intents` (ledger
included), not only the model folder.
"""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Protocol

from vectapilot_api.embeddings.base import EmbeddingSpec
from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.evalrun.ledger import LEDGER_FILE
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.intenteval.ledger import PinEntry, read_entries
from vectapilot_api.intenteval.model import Model, read_model
from vectapilot_api.intents.classifier import ClassifierArtifact
from vectapilot_api.intents.decide import RouteDecision, RouterThresholds, route_message
from vectapilot_api.intents.fallback import ROUTER_PROMPT_VERSION, IntentFallback
from vectapilot_api.intents.policy import RoutePolicy

ROUTE_OPERATION: Final = "agent-route"
"""The operation of the router's `embedding_call` record."""
REAL_MODEL_TAG: Final = "-local-model"
REAL_EMBED_TAG: Final = "-live-embed"
REPORTS_PARTS: Final = ("evals", "reports", "intents")
"""A real model folder is `evals/reports/intents/<version>/<UTC>-local-model` (M4's runinfo);
dry ones live under `evals/reports/tmp/`."""


class RouterModelError(ValueError):
    """`agent_router = on` cannot start: the model folder is missing, dry, uncommitted, not the
    pinned one or for another embedding model. Names the rule, never a value."""


@dataclass(frozen=True)
class RouterModel:
    """A live classifier and the thresholds chosen for it on dev (M4 PR 4)."""

    artifact: ClassifierArtifact
    artifact_sha256: str
    thresholds: RouterThresholds


def load_router_model(folder: Path, embedding: EmbeddingSpec) -> RouterModel:
    """The router's model from `folder`, or `RouterModelError` (module docstring)."""
    try:
        model = read_model(folder)
    except (DatasetError, OSError):
        raise RouterModelError(
            "agent_router is on, but agent_router_model_dir is not a complete M4 model folder"
        ) from None
    resolved = folder.resolve()
    if (
        model.info.dry_run
        or not resolved.name.endswith(REAL_MODEL_TAG)
        or tuple(resolved.parts[-5:-2]) != REPORTS_PARTS
        or not model.info.embed_folder.endswith(REAL_EMBED_TAG)
    ):
        raise RouterModelError(
            "agent_router is on, but the model folder was not trained on live embeddings (a"
            " -local-model folder under evals/reports/intents from a -live-embed folder)"
        )
    if model.info.uncommitted_changes:
        raise RouterModelError(
            "agent_router is on, but the model was trained from a tree with uncommitted changes"
        )
    artifact = model.artifact
    if artifact.embedding_model != embedding.model or artifact.dimension != embedding.dimension:
        raise RouterModelError(
            "agent_router is on, but the classifier was trained for another embedding model or"
            " dimension than the API's: retrain it (ADR 0015 decision 3)"
        )
    if not _pinned(resolved, model):
        raise RouterModelError(
            "agent_router is on, but the model folder is not the pinned M4 model (a pin in"
            " evals/reports/intents/test-runs.jsonl naming this folder, its artifact sha256, T,"
            " tau and sigma, and this router prompt version)"
        )
    thresholds = RouterThresholds(tau=model.thresholds.tau, sigma=model.thresholds.sigma)
    return RouterModel(
        artifact=artifact, artifact_sha256=model.artifact_sha256, thresholds=thresholds
    )


def _pinned(resolved: Path, model: Model) -> bool:
    """Whether the ledger next to the version folders pins exactly this model (module docstring).
    An unreadable ledger pins nothing."""
    try:
        entries = read_entries(resolved.parents[1] / LEDGER_FILE)
    except (OSError, ValueError):  # pydantic's ValidationError is a ValueError
        return False
    here = PurePosixPath(*resolved.parts[-5:])
    wanted = (
        here,
        model.artifact_sha256,
        model.artifact.temperature,
        model.thresholds.tau,
        model.thresholds.sigma,
        ROUTER_PROMPT_VERSION,
    )
    return any(
        (
            PurePosixPath(entry.model_folder),
            entry.artifact_sha256,
            entry.temperature,
            entry.tau,
            entry.sigma,
            entry.router_prompt_version,
        )
        == wanted
        for entry in entries
        if isinstance(entry, PinEntry)
    )


class IntentRouter(Protocol):
    """Where one message goes (`ClassifierRouter`; a fake in tests, while M4's H1 is a stub)."""

    async def route(
        self, message: str, *, policy: RoutePolicy, business_id: str, trace_id: str
    ) -> RouteDecision: ...


class ClassifierRouter:
    """M4's router as the API runs it (module docstring)."""

    def __init__(
        self, runner: EmbeddingRunner, model: RouterModel, fallback: IntentFallback
    ) -> None:
        self._runner = runner
        self._model = model
        self._fallback = fallback

    async def route(
        self, message: str, *, policy: RoutePolicy, business_id: str, trace_id: str
    ) -> RouteDecision:
        embedded = await self._runner.embed(
            [message],
            input_type="query",
            business_id=business_id,
            operation=ROUTE_OPERATION,
            trace_id=trace_id,
        )
        scores = self._model.artifact.score(
            embedded.vectors[0], model=embedded.model, input_type="query"
        )
        return await route_message(
            scores,
            message,
            policy,
            self._fallback,
            thresholds=self._model.thresholds,
            business_id=business_id,
            trace_id=trace_id,
        )
