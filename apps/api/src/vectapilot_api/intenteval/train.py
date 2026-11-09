"""Fitting the intent classifier (M4 PR 4; ADR 0015 decision 4, D1 (c): scikit-learn, dev group).

**The train config, written down before any live embedding** (`TRAIN_SETTINGS`, plan PR 4):
multinomial logistic regression with L2 regularisation, C = 1.0 (the library's default; no search
over C), no class weights (train is balanced by construction: at least 40 rows per intent),
solver `lbfgs`, and a fixed `max_iter` and `tol`. Why L2: about 400 rows in 1024 dimensions are
very likely linearly separable, and without a penalty the weights would grow without bound.
Train is used for the fit only; dev only for T, tau and sigma (`calibrate`, `thresholds`).

**A convergence warning is an error** here as in the tests (`TrainError`): a fit that stopped at
`max_iter` is not the fit the config describes. `n_iter`, C and the solver go into the artifact.
In scikit-learn 1.9 `penalty` is deprecated; L2 is `l1_ratio = 0.0`, passed explicitly so the
config does not depend on a default.

**Isolation.** scikit-learn and numpy are imported inside `fit_logistic` only: the API never
imports this module, the image installs no dev dependency (`uv sync --no-dev`), and
`python -m vectapilot_api.intenteval check` runs without them
(tests/intenteval/test_ml_isolation.py). The API scores with the standard library
(`intents/classifier.py`); a test checks that its probabilities match the library's within 1e-9.

Labels are class indices in `DECISION_ORDER`, so the library's sorted classes are the artifact's
rows in decision order. No text reaches this module: vectors and labels only.
"""

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from vectapilot_api.intents.classifier import ARTIFACT_FORMAT, ClassifierArtifact, TrainConfig
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent


@dataclass(frozen=True)
class TrainSettings:
    """The fixed train config (module docstring). Changing a value is a decision, not an edit:
    tests/intenteval/test_train.py pins every one."""

    library: str
    solver: str
    c: float
    l1_ratio: float
    max_iter: int
    tol: float
    class_weight: None


TRAIN_SETTINGS: Final = TrainSettings(
    library="scikit-learn",
    solver="lbfgs",
    c=1.0,
    l1_ratio=0.0,
    max_iter=1000,
    tol=1e-6,
    class_weight=None,
)


class TrainError(ValueError):
    """The classifier cannot be trained as configured. Names the rule, never a row's text."""


@dataclass(frozen=True)
class Fit:
    """A fitted model's numbers, rows in `DECISION_ORDER`."""

    weights: tuple[tuple[float, ...], ...]
    biases: tuple[float, ...]
    n_iter: int
    library_version: str


def fit_logistic(
    vectors: Sequence[Sequence[float]],
    labels: Sequence[Intent],
    settings: TrainSettings = TRAIN_SETTINGS,
) -> Fit:
    """Fit the multinomial L2 logistic regression on `vectors` (one per row) and their gold
    `labels`. Raises `TrainError` when an intent has no row, the rows are not one vector per
    label of one dimension, or the solver does not converge within `max_iter`."""
    if len(vectors) != len(labels) or not vectors:
        raise TrainError("training needs one vector per label, and at least one row")
    if len({len(v) for v in vectors}) != 1:
        raise TrainError("every training vector must have the same dimension")
    missing = [i.value for i in DECISION_ORDER if i not in set(labels)]
    if missing:
        raise TrainError(f"every intent needs training rows; none for {missing}")
    # The dev group's libraries, imported here and nowhere at module level (module docstring).
    import numpy as np
    import sklearn
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import LogisticRegression

    x = np.asarray(vectors, dtype=np.float64)
    y = np.asarray([DECISION_ORDER.index(label) for label in labels], dtype=np.int64)
    model = LogisticRegression(
        C=settings.c,
        l1_ratio=settings.l1_ratio,
        solver=settings.solver,
        max_iter=settings.max_iter,
        tol=settings.tol,
        class_weight=settings.class_weight,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        try:
            model.fit(x, y)
        except ConvergenceWarning:
            raise TrainError(
                f"the solver did not converge within max_iter {settings.max_iter}: a convergence"
                " warning is an error (plan PR 4); the config is fixed, so look at the data"
            ) from None
    if [int(c) for c in model.classes_] != list(range(len(DECISION_ORDER))):
        raise TrainError("the fitted classes are not every intent in decision order")
    weights = tuple(tuple(float(w) for w in row) for row in model.coef_)
    biases = tuple(float(b) for b in model.intercept_)
    if not all(math.isfinite(v) for row in weights for v in row) or not all(
        math.isfinite(b) for b in biases
    ):
        raise TrainError("a fitted weight or bias is not finite")
    return Fit(
        weights=weights,
        biases=biases,
        n_iter=int(np.max(model.n_iter_)),
        library_version=str(sklearn.__version__),
    )


def build_artifact(
    fit: Fit,
    *,
    embedding_model: str,
    dimension: int,
    temperature: float,
    dataset_version: str,
    freeze_sha256: str,
    embedding_cache_sha256: str,
    settings: TrainSettings = TRAIN_SETTINGS,
) -> ClassifierArtifact:
    """The artifact the API scores with (`intents/classifier.py`), at `temperature`."""
    return ClassifierArtifact(
        artifact_format=ARTIFACT_FORMAT,
        intents=DECISION_ORDER,
        embedding_model=embedding_model,
        dimension=dimension,
        input_type="query",
        weights=fit.weights,
        biases=fit.biases,
        temperature=temperature,
        train_config=TrainConfig(
            library=settings.library,
            library_version=fit.library_version,
            solver=settings.solver,
            penalty="l2",
            c=settings.c,
            max_iter=settings.max_iter,
            tol=settings.tol,
            n_iter=fit.n_iter,
        ),
        dataset_version=dataset_version,
        freeze_sha256=freeze_sha256,
        embedding_cache_sha256=embedding_cache_sha256,
    )
