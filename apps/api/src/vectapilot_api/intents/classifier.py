"""The intent classifier as the API runs it (ADR 0015 decisions 3-5, M4 PR 3): a committed JSON
artifact, scored with the standard library only.

**The artifact** (`ClassifierArtifact`, closed: an unknown field is refused) holds everything
needed to score a message and to say where the numbers came from: the intent order of its rows,
the embedding model id, dimension and input type (`query`, D2), the weights and biases of a
multinomial logistic regression, the temperature (`calibration.py`), the train config, the
dataset version, the freeze hash and the sha256 of the embedding cache it was trained on. PR 4's
`train` writes it; nothing here trains.

**Scoring** (`score`): logits `z = W·x + b`, then `softmax(z / T)`, computed as
`exp((z - max z) / T)` normalised. Subtracting the maximum first is the same softmax and keeps
every exponent at or below 0, so no logit, however large, can overflow `exp`; a logit that is not
finite (an overflowing dot product) is refused rather than turned into NaN probabilities.

**Refusals** (`ClassifierError`): a vector from another embedding model, of another dimension,
or embedded as a `document` instead of a `query`, is refused: its numbers would be meaningless to
these weights, and a model change means retraining (ADR 0015 decision 3). An artifact whose rows
are not in `taxonomy.DECISION_ORDER` is refused at load: every consumer (the safety floor, ties in
decision order, PR 4's reports) reads the rows in that order.

No ML library is imported here, ever: the API image installs no dev dependency (`--no-dev`).
Errors name fields and rules, never a vector or message text.
"""

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    ValidationError,
    model_validator,
)

from vectapilot_api.gateway.types import OpaqueId, Slug
from vectapilot_api.intents.calibration import TEMPERATURE_MAX, TEMPERATURE_MIN
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

ARTIFACT_FORMAT: Final = 1
"""Bump with any change to the artifact's fields or their meaning."""

PROBABILITY_SUM_TOLERANCE: Final = 1e-9
"""How far `IntentScores`' probabilities may sum from 1 (float rounding stays far below it)."""

QueryInputType = Literal["query"]
"""The only input type the classifier accepts (D2: the message is embedded as a query)."""

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DatasetVersion = Annotated[str, Field(pattern=r"^v[0-9]+\.[0-9]+$")]
"""Defined here, not imported from `evalset`: the router is a platform module and does not depend
on the eval tooling. Same patterns as `evalset.corpus.Sha256` and `evalset.questions`."""


class ClassifierError(ValueError):
    """The artifact or a vector cannot be used. The message names the rule, never values."""


def _in_temperature_range(value: float) -> float:
    if not TEMPERATURE_MIN <= value <= TEMPERATURE_MAX:
        raise ValueError(
            f"temperature must lie in [{TEMPERATURE_MIN}, {TEMPERATURE_MAX}] (calibration.py)"
        )
    return value


class _ArtifactModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False
    )


class TrainConfig(_ArtifactModel):
    """How the weights were fitted (PR 4: L2 logistic regression, C = 1.0, no search)."""

    library: OpaqueId
    """The training library, e.g. `scikit-learn` (D1 (c)); never imported by the API."""
    library_version: OpaqueId
    solver: Slug
    penalty: Literal["l2"]
    c: StrictFloat = Field(gt=0)
    """The inverse regularisation strength."""
    max_iter: int = Field(gt=0, strict=True)
    tol: StrictFloat = Field(gt=0)
    n_iter: int = Field(gt=0, strict=True)
    """Iterations the solver used; at most `max_iter` (a convergence warning is an error)."""

    @model_validator(mode="after")
    def _converged_within_max_iter(self) -> Self:
        if self.n_iter > self.max_iter:
            raise ValueError("n_iter must not exceed max_iter")
        return self


class ClassifierArtifact(_ArtifactModel):
    """A trained, calibrated classifier. Load it with `load_artifact`; write it with
    `artifact_json`."""

    artifact_format: Literal[1]
    """`ARTIFACT_FORMAT`; another value is refused."""
    intents: tuple[Intent, ...]
    """The order of `weights`' rows and `biases`: always `DECISION_ORDER`."""
    embedding_model: OpaqueId
    dimension: int = Field(gt=0, strict=True)
    input_type: QueryInputType
    weights: tuple[tuple[StrictFloat, ...], ...]
    """One row per intent, `dimension` values each."""
    biases: tuple[StrictFloat, ...]
    temperature: Annotated[StrictFloat, AfterValidator(_in_temperature_range)]
    train_config: TrainConfig
    dataset_version: DatasetVersion
    freeze_sha256: Sha256
    """The frozen dataset version's hash, as PR 4's ledger keys it."""
    embedding_cache_sha256: Sha256

    @model_validator(mode="after")
    def _shapes(self) -> Self:
        if self.intents != DECISION_ORDER:
            raise ValueError("intents must be every intent, in the decision order")
        if len(self.weights) != len(self.intents) or len(self.biases) != len(self.intents):
            raise ValueError("weights and biases need exactly one row per intent")
        if any(len(row) != self.dimension for row in self.weights):
            raise ValueError("every weight row needs exactly `dimension` values")
        return self

    def logits(self, vector: Sequence[float], *, model: str, input_type: str) -> tuple[float, ...]:
        """The raw logits `W·x + b` (before the temperature), in `DECISION_ORDER`: what PR 4's
        `calibrate` fits T on. Refuses another model, input type or dimension."""
        if model != self.embedding_model:
            raise ClassifierError(
                f"the vector comes from another embedding model than {self.embedding_model}: "
                "retrain for it (ADR 0015 decision 3)"
            )
        if input_type != self.input_type:
            raise ClassifierError("the vector must be embedded with input type `query`")
        if len(vector) != self.dimension:
            raise ClassifierError(f"the vector must have exactly {self.dimension} values")
        if not all(math.isfinite(value) for value in vector):
            raise ClassifierError("the vector holds a value that is not finite")
        values = [float(value) for value in vector]
        logits = tuple(
            math.sumprod(row, values) + bias
            for row, bias in zip(self.weights, self.biases, strict=True)
        )
        if not all(math.isfinite(z) for z in logits):
            raise ClassifierError("a logit is not finite: the weights and the vector overflow")
        return logits

    def score(self, vector: Sequence[float], *, model: str, input_type: str) -> "IntentScores":
        """The calibrated probability of every intent for one query vector."""
        logits = self.logits(vector, model=model, input_type=input_type)
        probabilities = softmax(logits, self.temperature)
        return IntentScores(dict(zip(DECISION_ORDER, probabilities, strict=True)))


def softmax(logits: Sequence[float], temperature: float = 1.0) -> tuple[float, ...]:
    """`softmax(logits / temperature)`, as `exp((z - max z) / T)` normalised (no overflow)."""
    if not logits:
        raise ValueError("softmax needs at least one logit")
    if not (math.isfinite(temperature) and temperature > 0):
        raise ValueError("temperature must be a finite number > 0")
    if not all(math.isfinite(z) for z in logits):
        raise ValueError("every logit must be finite")
    top = max(logits)
    exps = [math.exp((z - top) / temperature) for z in logits]  # the largest is exp(0) = 1
    total = math.fsum(exps)
    return tuple(e / total for e in exps)


@dataclass(frozen=True)
class IntentScores:
    """The classifier's probability for every intent; the input of H1's `route_message`.

    `probabilities` always holds all eight intents, iterated in `DECISION_ORDER` whatever order
    they were given in, each a finite float in [0, 1], summing to 1 within
    `PROBABILITY_SUM_TOLERANCE`. Read-only.
    """

    probabilities: Mapping[Intent, float]

    def __post_init__(self) -> None:
        given = self.probabilities
        if set(given) != set(Intent) or len(given) != len(DECISION_ORDER):
            raise ValueError("scores need a probability for every intent, and only for those")
        for intent in DECISION_ORDER:
            p = given[intent]
            if isinstance(p, bool) or not isinstance(p, float | int):
                raise ValueError(f"the probability of {intent.value} is not a number")
            if not (math.isfinite(p) and 0 <= p <= 1):
                raise ValueError(f"the probability of {intent.value} must lie in [0, 1]")
        ordered = {intent: float(given[intent]) for intent in DECISION_ORDER}
        if abs(math.fsum(ordered.values()) - 1) > PROBABILITY_SUM_TOLERANCE:
            raise ValueError("the probabilities must sum to 1")
        object.__setattr__(self, "probabilities", MappingProxyType(ordered))


def load_artifact(text: str) -> ClassifierArtifact:
    """Parse and validate an artifact's JSON. Raises `ClassifierError` naming the failing fields
    and our own rules, never a value."""
    try:
        return ClassifierArtifact.model_validate_json(text)
    except ValidationError as error:
        errors = error.errors(include_input=False)
        fields = sorted({".".join(map(str, e["loc"])) or "(artifact)" for e in errors})
        rules = sorted({e["msg"] for e in errors if e["type"] == "value_error"})
        detail = ", ".join(fields) + (f" ({'; '.join(rules)})" if rules else "")
    raise ClassifierError(f"not a valid classifier artifact: {detail}")


def artifact_json(artifact: ClassifierArtifact) -> str:
    """The artifact as canonical JSON (sorted keys, one space indent, a final newline): the same
    artifact always gives the same bytes, so its sha256 can pin it. Floats round-trip exactly."""
    return json.dumps(artifact.model_dump(mode="json"), sort_keys=True, indent=1) + "\n"
