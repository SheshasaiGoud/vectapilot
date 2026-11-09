"""The train config and the fit (M4 PR 4, D1 (c)). Fixed made-up vectors only; no result.

The key check (plan PR 4): PR 3's standard-library scorer gives scikit-learn's own probabilities
within 1e-9 on fixed vectors, so the API scores exactly what was trained.
"""

import math
from typing import Final

import pytest

from vectapilot_api.embeddings.fake import fake_vector
from vectapilot_api.intenteval.train import (
    TRAIN_SETTINGS,
    TrainError,
    TrainSettings,
    build_artifact,
    fit_logistic,
)
from vectapilot_api.intents.classifier import artifact_json, load_artifact
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

DIM: Final = 16
SHA: Final = "a" * 64


def vectors(per_intent: int = 4) -> tuple[list[tuple[float, ...]], list[Intent]]:
    """Made-up, roughly separable vectors: each intent's own direction plus hashed noise."""
    xs: list[tuple[float, ...]] = []
    ys: list[Intent] = []
    for k, intent in enumerate(DECISION_ORDER):
        for j in range(per_intent):
            noise = fake_vector(f"TEST-ONLY {intent.value} {j}", DIM)
            raw = [0.3 * v + (1.0 if i == k else 0.0) for i, v in enumerate(noise)]
            norm = math.sqrt(sum(v * v for v in raw))
            xs.append(tuple(v / norm for v in raw))
            ys.append(intent)
    return xs, ys


def test_the_train_config_is_written_down() -> None:
    # Changing a value is a decision (plan PR 4), never an edit: this pins every one.
    assert (
        TrainSettings(
            library="scikit-learn",
            solver="lbfgs",
            c=1.0,
            l1_ratio=0.0,
            max_iter=1000,
            tol=1e-6,
            class_weight=None,
        )
        == TRAIN_SETTINGS
    )


def test_the_stdlib_scorer_matches_the_librarys_probabilities() -> None:
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    xs, ys = vectors()
    fit = fit_logistic(xs, ys)
    artifact = build_artifact(
        fit,
        embedding_model="fake-embedding-1024",
        dimension=DIM,
        temperature=1.0,
        dataset_version="v0.1",
        freeze_sha256=SHA,
        embedding_cache_sha256=SHA,
    )
    # The library's own model, fitted the same way, on the same rows.
    s = TRAIN_SETTINGS
    model = LogisticRegression(
        C=s.c, l1_ratio=s.l1_ratio, solver=s.solver, max_iter=s.max_iter, tol=s.tol
    )
    model.fit(np.asarray(xs), np.asarray([DECISION_ORDER.index(y) for y in ys]))
    probes = [fake_vector(f"TEST-ONLY probe {n}", DIM) for n in range(10)]
    expected = model.predict_proba(np.asarray(probes))

    for probe, row in zip(probes, expected, strict=True):
        scores = artifact.score(probe, model="fake-embedding-1024", input_type="query")
        got = [scores.probabilities[i] for i in DECISION_ORDER]
        assert max(abs(a - float(b)) for a, b in zip(got, row, strict=True)) < 1e-9


def test_the_artifact_records_the_fit_and_round_trips() -> None:
    xs, ys = vectors()
    fit = fit_logistic(xs, ys)
    artifact = build_artifact(
        fit,
        embedding_model="fake-embedding-1024",
        dimension=DIM,
        temperature=1.0,
        dataset_version="v0.1",
        freeze_sha256=SHA,
        embedding_cache_sha256=SHA,
    )

    config = artifact.train_config
    assert (config.library, config.solver, config.penalty, config.c) == (
        "scikit-learn",
        "lbfgs",
        "l2",
        1.0,
    )
    assert 0 < config.n_iter <= config.max_iter == 1000
    assert config.library_version == "1.9.1"
    assert load_artifact(artifact_json(artifact)) == artifact
    assert artifact_json(load_artifact(artifact_json(artifact))) == artifact_json(artifact)


def test_every_intent_needs_training_rows() -> None:
    xs, ys = vectors()
    keep = [i for i, y in enumerate(ys) if y is not Intent.OUT_OF_SCOPE]

    with pytest.raises(TrainError, match="out_of_scope"):
        fit_logistic([xs[i] for i in keep], [ys[i] for i in keep])


@pytest.mark.parametrize(
    ("xs", "ys"),
    [([], []), ([(1.0,)], []), ([(1.0,), (1.0, 0.0)], [Intent.QUESTION, Intent.QUESTION])],
    ids=["empty", "mismatch", "ragged"],
)
def test_malformed_training_input_is_refused(xs: list[tuple[float, ...]], ys: list[Intent]) -> None:
    with pytest.raises(TrainError):
        fit_logistic(xs, ys)


def test_vectors_of_two_dimensions_are_refused_by_name() -> None:
    # One row per intent, so only the dimension rule can refuse it (not the missing-intent one).
    xs = [(1.0, 0.0, 0.0) if n else (1.0, 0.0, 0.0, 0.0) for n in range(len(DECISION_ORDER))]

    with pytest.raises(TrainError, match="same dimension"):
        fit_logistic(xs, list(DECISION_ORDER))


def test_a_convergence_warning_is_an_error() -> None:
    xs, ys = vectors()
    one_step = TrainSettings(
        library="scikit-learn",
        solver="lbfgs",
        c=1.0,
        l1_ratio=0.0,
        max_iter=1,
        tol=1e-12,
        class_weight=None,
    )

    with pytest.raises(TrainError, match="did not converge within max_iter 1"):
        fit_logistic(xs, ys, one_step)
