"""The model folder (`<UTC>-local-model`, or `-fake-model` when dry): one trained classifier and
what was chosen for it on dev (M4 PR 4). Written once, file by file, never overwritten:

1. `train` writes `model.json` (`ModelInfo`: where the vectors came from, the commit, the train
   rows) and `artifact-raw.json` (the fitted weights at T = 1);
2. `calibrate` fits T on dev and writes `calibration.json` (`CalibrationInfo`) and
   `artifact.json` (the same weights at the fitted T: what the API would score with);
3. `thresholds` applies D8's rule on dev and writes `thresholds.json` (`ThresholdsInfo`) and
   `dev-curve.md` (the whole curve, for the note).

The artifact's sha256 is of `artifact.json`'s bytes (`classifier.artifact_json` is canonical), the
value the test-run ledger pins. PR 5 commits the folder after step 4, before any dev run.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vectapilot_api.evalset.corpus import Sha256, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.gateway.types import OpaqueId
from vectapilot_api.intents.calibration import Bound
from vectapilot_api.intents.classifier import ClassifierArtifact, ClassifierError, load_artifact

MODEL_FILE: Final = "model.json"
RAW_ARTIFACT_FILE: Final = "artifact-raw.json"
ARTIFACT_FILE: Final = "artifact.json"
CALIBRATION_FILE: Final = "calibration.json"
THRESHOLDS_FILE: Final = "thresholds.json"
DEV_CURVE_FILE: Final = "dev-curve.md"


class _Closed(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False
    )


class ModelInfo(_Closed):
    """`model.json`: where this classifier's vectors came from and who trained it when."""

    record_version: Literal[1] = 1
    dry_run: bool
    dry_reason: str | None
    dataset: str
    dataset_version: str
    freeze_sha256: Sha256
    embed_folder: str
    """The embed folder whose cache trained it (repository-relative)."""
    embedding_model: OpaqueId
    dimension: int = Field(gt=0)
    embedding_cache_sha256: Sha256
    train_rows: int = Field(gt=0)
    commit: str
    uncommitted_changes: bool
    trained_at: datetime


class CalibrationInfo(_Closed):
    """`calibration.json`: the temperature fitted on dev (`intents/calibration.py`)."""

    temperature: float = Field(gt=0)
    nll: float
    nll_at_1: float
    dev_rows: int = Field(gt=0)
    at_bound: Bound | None
    """`lower`/`upper`: the dev rows could not place T (calibration.py); reported, never hidden."""
    raw_artifact_sha256: Sha256
    artifact_sha256: Sha256


class TauPointInfo(_Closed):
    tau: float
    kept: int
    correct: int


class SigmaPointInfo(_Closed):
    sigma: float
    decided_non_safety: tuple[str, ...]
    emergency_as_judgement: tuple[str, ...]
    safety_to_llm: int
    caught_non_safety: int
    non_safety_rows: int


class ThresholdsInfo(_Closed):
    """`thresholds.json`: D8's choice on dev and the whole curve (`thresholds.py`)."""

    rule_version: str
    tau: Annotated[float, Field(gt=0, le=1)] | None
    """None: no confidence band (no grid value qualified; decided 2026-11-09). Required, so the
    file always says so explicitly (`"tau": null`)."""
    sigma: float = Field(gt=0, le=1)
    tau_grid: tuple[float, ...]
    sigma_grid: tuple[float, ...]
    min_kept: int
    target_accuracy: str
    artifact_sha256: Sha256
    dev_rows: int
    safety_rows: int
    still_missed: tuple[str, ...]
    tau_curve: tuple[TauPointInfo, ...]
    sigma_curve: tuple[SigmaPointInfo, ...]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read[M: BaseModel](folder: Path, name: str, model: type[M]) -> M:
    path = folder / name
    if not path.is_file():
        raise DatasetError(f"{folder.name} has no {name}: run the step that writes it first")
    try:
        return model.model_validate_json(path.read_bytes())
    except ValidationError as error:
        detail = describe_validation_error(error, "(file)")
    raise DatasetError(f"{folder.name}/{name} is not valid: {detail}")


def _artifact(folder: Path, name: str) -> ClassifierArtifact:
    path = folder / name
    if not path.is_file():
        raise DatasetError(f"{folder.name} has no {name}: run the step that writes it first")
    try:
        return load_artifact(path.read_text("utf-8"))
    except ClassifierError as error:
        raise DatasetError(f"{folder.name}/{name}: {error}") from None


def read_model_info(folder: Path) -> ModelInfo:
    return _read(folder, MODEL_FILE, ModelInfo)


def read_raw_artifact(folder: Path) -> ClassifierArtifact:
    return _artifact(folder, RAW_ARTIFACT_FILE)


@dataclass(frozen=True)
class Model:
    """A complete model folder: trained, calibrated and thresholded."""

    folder: Path
    info: ModelInfo
    artifact: ClassifierArtifact
    artifact_sha256: str
    calibration: CalibrationInfo
    thresholds: ThresholdsInfo


def read_model(folder: Path) -> Model:
    """Every file of a complete model folder, checked against each other: the calibrated
    artifact is the one calibration.json and thresholds.json name, and T is calibration's."""
    info = read_model_info(folder)
    artifact = _artifact(folder, ARTIFACT_FILE)
    sha = sha256_of(folder / ARTIFACT_FILE)
    calibration = _read(folder, CALIBRATION_FILE, CalibrationInfo)
    thresholds = _read(folder, THRESHOLDS_FILE, ThresholdsInfo)
    if calibration.artifact_sha256 != sha or thresholds.artifact_sha256 != sha:
        raise DatasetError(
            f"{folder.name}: {ARTIFACT_FILE} is not the artifact {CALIBRATION_FILE} and"
            f" {THRESHOLDS_FILE} were made for"
        )
    if artifact.temperature != calibration.temperature:
        raise DatasetError(f"{folder.name}: the artifact's temperature is not calibration's")
    if artifact.embedding_cache_sha256 != info.embedding_cache_sha256:
        raise DatasetError(f"{folder.name}: the artifact names another embedding cache")
    return Model(folder, info, artifact, sha, calibration, thresholds)
