"""M4 PR 3: the classifier artifact and its standard-library scorer (`intents/classifier.py`).

Every artifact here is made up (tiny dimensions, hand-picked weights); no number is a result.
"""

import ast
import json
import math
import random
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from vectapilot_api.intents import calibration, classifier
from vectapilot_api.intents.calibration import TEMPERATURE_MAX, TEMPERATURE_MIN
from vectapilot_api.intents.classifier import (
    ARTIFACT_FORMAT,
    ClassifierArtifact,
    ClassifierError,
    IntentScores,
    artifact_json,
    load_artifact,
    softmax,
)
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

MODEL: Final = "voyage-4"
K: Final = len(DECISION_ORDER)


def artifact_dict(
    *,
    dimension: int = 2,
    weights: list[list[float]] | None = None,
    biases: list[float] | None = None,
    temperature: float = 1.0,
) -> dict[str, Any]:
    return {
        "artifact_format": ARTIFACT_FORMAT,
        "intents": [i.value for i in DECISION_ORDER],
        "embedding_model": MODEL,
        "dimension": dimension,
        "input_type": "query",
        "weights": weights if weights is not None else [[0.0] * dimension for _ in range(K)],
        "biases": biases if biases is not None else [0.0] * K,
        "temperature": temperature,
        "train_config": {
            "library": "scikit-learn",
            "library_version": "1.7.2",
            "solver": "lbfgs",
            "penalty": "l2",
            "c": 1.0,
            "max_iter": 1000,
            "tol": 1e-6,
            "n_iter": 37,
        },
        "dataset_version": "v0.1",
        "freeze_sha256": "a" * 64,
        "embedding_cache_sha256": "b" * 64,
    }


def build(**overrides: Any) -> ClassifierArtifact:
    return load_artifact(json.dumps(artifact_dict(**overrides)))


def score(artifact: ClassifierArtifact, vector: list[float]) -> IntentScores:
    return artifact.score(vector, model=MODEL, input_type="query")


# ── Scoring ─────────────────────────────────────────────────────────────────────────────────


def test_scores_match_a_hand_computed_softmax_with_the_temperature() -> None:
    # Zero weights, biases ln(4), ln(2), 0, ...: logits ln 4, ln 2, 0 x 6. At T = 2 the
    # unnormalised probabilities are 2, sqrt 2, 1 x 6, so p = (2, sqrt 2, 1, ...) / (8 + sqrt 2).
    artifact = build(biases=[math.log(4), math.log(2)] + [0.0] * 6, temperature=2.0)

    probabilities = score(artifact, [0.3, -0.7]).probabilities

    total = 8 + math.sqrt(2)
    expected = [2 / total, math.sqrt(2) / total] + [1 / total] * 6
    assert list(probabilities) == list(DECISION_ORDER)
    assert list(probabilities.values()) == pytest.approx(expected, rel=1e-12)


def test_the_logits_are_the_weights_times_the_vector_plus_the_bias() -> None:
    weights = [[float(i), -1.0] for i in range(K)]
    artifact = build(weights=weights, biases=[0.5] * K)

    logits = artifact.logits([2.0, 3.0], model=MODEL, input_type="query")

    assert logits == tuple(2.0 * i - 3.0 + 0.5 for i in range(K))


def test_huge_logits_stay_finite_and_sum_to_1() -> None:
    # Logits +-1e308 at T = 0.05: dividing first would overflow (1e308 / 0.05 = inf); the scorer
    # subtracts the maximum first, so every exponent is <= 0.
    artifact = build(biases=[-1e308, 1e308] + [0.0] * 6, temperature=TEMPERATURE_MIN)

    probabilities = list(score(artifact, [0.0, 0.0]).probabilities.values())

    assert probabilities == [0.0, 1.0] + [0.0] * 6
    # Close huge logits keep their difference: 1000 and 999 give 1 / (1 + e^-1) and the rest.
    near = build(biases=[1000.0, 999.0] + [-1000.0] * 6)
    p = list(score(near, [0.0, 0.0]).probabilities.values())
    assert p[0] == pytest.approx(1 / (1 + math.exp(-1)), rel=1e-12)
    assert math.fsum(p) == pytest.approx(1.0, abs=1e-12)


def test_probabilities_sum_to_1_and_lie_in_0_1_on_seeded_random_artifacts() -> None:
    rng = random.Random(42)  # noqa: S311 (seeded test data, not crypto)
    for _ in range(50):
        dimension = rng.randint(1, 16)
        artifact = build(
            dimension=dimension,
            weights=[[rng.gauss(0, 5) for _ in range(dimension)] for _ in range(K)],
            biases=[rng.gauss(0, 5) for _ in range(K)],
            temperature=rng.uniform(TEMPERATURE_MIN, TEMPERATURE_MAX),
        )
        vector = [rng.gauss(0, 1) for _ in range(dimension)]

        values = list(score(artifact, vector).probabilities.values())

        assert all(0.0 <= p <= 1.0 for p in values)
        assert math.fsum(values) == pytest.approx(1.0, abs=1e-12)


def test_the_temperature_never_changes_the_most_probable_intent() -> None:
    rng = random.Random(5)  # noqa: S311 (seeded test data, not crypto)
    weights = [[rng.gauss(0, 1) for _ in range(4)] for _ in range(K)]
    vector = [rng.gauss(0, 1) for _ in range(4)]
    tops = set()
    for temperature in (TEMPERATURE_MIN, 0.5, 1.0, 3.0, TEMPERATURE_MAX):
        p = score(build(dimension=4, weights=weights, temperature=temperature), vector)
        tops.add(max(p.probabilities, key=lambda i: p.probabilities[i]))
    assert len(tops) == 1


# ── Refusals at score time ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("vector", "model", "input_type", "message"),
    [
        ([0.1, 0.2], "voyage-4-lite", "query", "another embedding model"),
        ([0.1, 0.2], MODEL, "document", "input type `query`"),
        ([0.1, 0.2, 0.3], MODEL, "query", "exactly 2 values"),
        ([0.1], MODEL, "query", "exactly 2 values"),
        ([0.1, math.nan], MODEL, "query", "not finite"),
        ([math.inf, 0.2], MODEL, "query", "not finite"),
    ],
    ids=["model", "input type", "longer", "shorter", "nan", "inf"],
)
def test_a_vector_the_weights_were_not_trained_for_is_refused(
    vector: list[float], model: str, input_type: str, message: str
) -> None:
    with pytest.raises(ClassifierError, match=message):
        build().score(vector, model=model, input_type=input_type)


def test_a_logit_that_overflows_is_refused_never_turned_into_nan() -> None:
    artifact = build(weights=[[1e200, 0.0]] + [[0.0, 0.0]] * (K - 1))

    with pytest.raises(ClassifierError, match="not finite"):
        score(artifact, [1e200, 0.0])


# ── Loading ─────────────────────────────────────────────────────────────────────────────────


def test_the_artifact_round_trips_through_canonical_json_bit_for_bit() -> None:
    rng = random.Random(9)  # noqa: S311 (seeded test data, not crypto)
    artifact = build(
        dimension=3,
        weights=[[rng.gauss(0, 1) for _ in range(3)] for _ in range(K)],
        biases=[rng.gauss(0, 1) for _ in range(K)],
        temperature=0.731,
    )

    text = artifact_json(artifact)

    assert load_artifact(text) == artifact
    assert artifact_json(load_artifact(text)) == text  # the same bytes, so one sha256
    assert text.endswith("}\n")
    # Canonical means sorted keys: PR 4's sha256 pin survives a reordering of the fields.
    assert text == json.dumps(json.loads(text), sort_keys=True, indent=1) + "\n"


def mutate(path: tuple[str | int, ...], value: object) -> str:
    data = artifact_dict()
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return json.dumps(data)


@pytest.mark.parametrize(
    ("text", "field"),
    [
        (mutate(("intents",), [i.value for i in reversed(DECISION_ORDER)]), "decision order"),
        (mutate(("intents",), [i.value for i in DECISION_ORDER[:-1]]), "decision order"),
        (mutate(("intents",), [i.value for i in DECISION_ORDER] + ["question"]), "decision"),
        (mutate(("input_type",), "document"), "input_type"),
        (mutate(("artifact_format",), 2), "artifact_format"),
        (mutate(("weights",), [[0.0, 0.0]] * (K - 1)), "one row per intent"),
        (mutate(("weights", 3), [0.0]), "`dimension` values"),
        (mutate(("biases",), [0.0] * (K + 1)), "one row per intent"),
        (mutate(("weights", 0, 0), True), "weights.0.0"),
        (mutate(("weights", 0, 0), "0.5"), "weights.0.0"),
        (mutate(("biases", 0), 1e400), "biases.0"),
        (mutate(("temperature",), 0.0), "temperature"),
        (mutate(("temperature",), 0.01), "calibration.py"),
        (mutate(("temperature",), TEMPERATURE_MAX * 2), "calibration.py"),
        (mutate(("dimension",), 0), "dimension"),
        (mutate(("embedding_model",), "sk-ant-not-a-model"), "embedding_model"),
        (mutate(("dataset_version",), "0.1"), "dataset_version"),
        (mutate(("freeze_sha256",), "A" * 64), "freeze_sha256"),
        (mutate(("embedding_cache_sha256",), "b" * 63), "embedding_cache_sha256"),
        (mutate(("train_config", "penalty"), "l1"), "train_config.penalty"),
        (mutate(("train_config", "n_iter"), 1001), "n_iter must not exceed max_iter"),
        (mutate(("train_config", "c"), -1.0), "train_config.c"),
        (mutate(("extra",), 1), "extra"),
        (mutate(("train_config", "class_weight"), "balanced"), "train_config.class_weight"),
        ('{"artifact_format": 1', "(artifact)"),
    ],
    ids=[
        "reversed intents",
        "missing intent",
        "extra intent",
        "document vectors",
        "format",
        "missing weight row",
        "short weight row",
        "extra bias",
        "bool weight",
        "string weight",
        "overflowing bias",
        "zero temperature",
        "temperature below the search range",
        "temperature above the search range",
        "zero dimension",
        "secret-shaped model id",
        "dataset version",
        "upper-case hash",
        "short hash",
        "penalty",
        "not converged",
        "negative C",
        "unknown field",
        "unknown train field",
        "invalid json",
    ],
)
def test_an_invalid_artifact_is_refused_by_field_without_values(text: str, field: str) -> None:
    with pytest.raises(ClassifierError) as raised:
        load_artifact(text)

    assert field in str(raised.value)
    assert "sk-ant-not-a-model" not in str(raised.value)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("temperature",), TEMPERATURE_MIN),
        (("temperature",), TEMPERATURE_MAX),
        (("train_config", "n_iter"), 1000),  # == max_iter: "at most max_iter" includes it
    ],
    ids=["temperature at the minimum", "temperature at the maximum", "n_iter at max_iter"],
)
def test_values_on_a_boundary_are_accepted(path: tuple[str, ...], value: object) -> None:
    artifact = load_artifact(mutate(path, value))

    assert artifact.model_dump(mode="json") == json.loads(mutate(path, value))


def test_a_validation_error_never_shows_a_value_even_without_load_artifact() -> None:
    # PR 4's train builds the artifact directly: the model itself hides inputs in its errors.
    data = artifact_dict(temperature=123.456)

    with pytest.raises(ValidationError) as raised:
        ClassifierArtifact.model_validate(data)

    assert "temperature" in str(raised.value)
    assert "123.456" not in str(raised.value)


def test_the_artifact_is_read_only() -> None:
    artifact = build()
    with pytest.raises(ValueError, match="frozen"):
        artifact.temperature = 2.0  # type: ignore[misc]


# ── IntentScores ────────────────────────────────────────────────────────────────────────────


def dyadic(**changes: float) -> dict[Intent, float]:
    """1/2, 1/4, ..., 1/128, 1/128: exact floats that sum to exactly 1."""
    values = {intent: 2.0 ** -(n + 1) for n, intent in enumerate(DECISION_ORDER)}
    values[Intent.OUT_OF_SCOPE] = 2.0**-7
    return values | {Intent(k): v for k, v in changes.items()}


def test_scores_iterate_in_decision_order_whatever_order_they_were_given_in() -> None:
    given = dict(reversed(list(dyadic().items())))
    scores = IntentScores(given)

    assert list(scores.probabilities) == list(DECISION_ORDER)
    assert scores.probabilities[Intent.EMERGENCY] == 0.5
    with pytest.raises(TypeError):
        scores.probabilities[Intent.EMERGENCY] = 1.0  # type: ignore[index]


def foreign_key() -> dict[Any, float]:
    """Eight keys, one of them not an intent (refund_or_payment's value under another key)."""
    values: dict[Any, float] = dict(dyadic())
    values["refund"] = values.pop(Intent.REFUND_OR_PAYMENT)
    return values


@pytest.mark.parametrize(
    ("probabilities", "message"),
    [
        ({i: 1 / 7 for i in DECISION_ORDER[:-1]}, "every intent"),
        (foreign_key(), "every intent"),
        # Sums to 1 + 5e-10, within the tolerance: only the upper bound refuses it.
        ({i: 0.0 for i in DECISION_ORDER} | {Intent.EMERGENCY: 1 + 5e-10}, r"\[0, 1\]"),
        (dyadic(emergency=0.6), "sum to 1"),
        (dyadic(emergency=-0.0001, question=0.0626), r"\[0, 1\]"),
        (dyadic(emergency=math.nan), r"\[0, 1\]"),
        (dyadic(emergency=True), "not a number"),
    ],
    ids=["missing intent", "foreign key", "above 1", "sum", "negative", "nan", "bool"],
)
def test_scores_that_are_not_a_distribution_over_the_intents_are_refused(
    probabilities: Mapping[Intent, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        IntentScores(probabilities)


# ── The softmax helper ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("logits", "temperature"), [([], 1.0), ([1.0], 0.0), ([1.0], math.inf), ([math.nan], 1.0)]
)
def test_softmax_refuses_what_has_no_distribution(logits: list[float], temperature: float) -> None:
    with pytest.raises(ValueError):
        softmax(logits, temperature)


# ── The standard library only ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("module", "allowed"),
    [(calibration, set()), (classifier, {"pydantic", "vectapilot_api"})],
    ids=["calibration", "classifier"],
)
def test_the_scorer_and_calibration_import_no_ml_library(module: Any, allowed: set[str]) -> None:
    # The API image installs no dev dependency (D1): scoring and calibration must run on the
    # standard library (and pydantic, an API dependency, for the artifact's schema).
    tree = ast.parse(Path(module.__file__).read_text("utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported - set(sys.stdlib_module_names) <= allowed
