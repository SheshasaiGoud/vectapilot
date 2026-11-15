"""A TEST-ONLY extraction run.json and fingerprint, for the spend tests."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.extracteval.runinfo import BusinessPins, ExtractRunInfo, Fingerprint, Limits

T0: Final = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
FINGERPRINT: Final = Fingerprint(
    freeze_sha256="f" * 64,
    extract_prompt_version="extract-fields.1",
    extraction_rules_version="extract-rules.1",
    alias="small",
    model="test-model",
    catalog_version="test-catalog",
    businesses={
        "paws-and-co": BusinessPins(
            business_sha256="b" * 64, templates={"vaccination_record": "c" * 64}
        )
    },
    limits=Limits(
        max_bytes=5 * 1024 * 1024,
        max_pages=5,
        max_characters=20_000,
        attempt_timeout_s=15.0,
        deadline_s=30.0,
        max_retries=2,
        max_repairs=1,
    ),
)


def extract_run_info(
    name: str, *, total: str = "0.40", dry: bool = False, split: str = "dev"
) -> ExtractRunInfo:
    return ExtractRunInfo.model_validate(
        {
            "run_id": name,
            "status": "complete",
            "dry_run": dry,
            "environment": "local",
            "dataset": "doc_extraction",
            "dataset_version": "v0.1",
            "dataset_stage": "frozen",
            "freeze_sha256": "f" * 64,
            "dataset_committed": True,
            "split": split,
            "documents": 12,
            "commit": "c" * 40,
            "uncommitted_changes": False,
            "started_at": T0,
            "ended_at": T0,
            "fingerprint": FINGERPRINT,
            "fingerprint_sha256": FINGERPRINT.sha256(),
            "llm_provider": "fake" if dry else "anthropic",
            "confidence": "19/20",
            "cost": CostInfo(
                cap_usd=Decimal("1.00"),
                embedding_usd=Decimal(0),
                llm_usd=Decimal(total),
                total_usd=Decimal(total),
                llm_cost_complete=True,
                embedding_requests=0,
                llm_calls=12,
            ),
        }
    )


def extract_run_json(name: str, **fields: object) -> bytes:
    info = extract_run_info(name, **fields)  # type: ignore[arg-type]
    return (info.model_dump_json(indent=2) + "\n").encode("utf-8")
