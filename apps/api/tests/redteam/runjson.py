"""A TEST-ONLY red-team run.json and fingerprint, for the spend tests."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.redteam.runinfo import Fingerprint, RedteamRunInfo

T0: Final = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
FINGERPRINT: Final = Fingerprint(
    dataset_version="v0.1",
    freeze_sha256="f" * 64,
    gates_sha256="a" * 64,
    rules_version=None,
    pii_generator="redteam-pii.1",
    pii_seed=20261116,
    guard_routes="on",
    guard_answer="on",
    provider="anthropic",
    models={"small": "test-model"},
    prompt_version="rag-answer.1",
    agent_prompt_version="agent-step.1",
    agent_loop="run_turn",
)


def redteam_run_info(name: str, *, total: str = "0.40", dry: bool = False) -> RedteamRunInfo:
    return RedteamRunInfo(
        run_id=name,
        status="complete",
        dry_run=dry,
        environment="local",
        dataset="redteam",
        dataset_version="v0.1",
        dataset_stage="frozen",
        cases=140,
        commit="c" * 40,
        uncommitted_changes=False,
        started_at=T0,
        ended_at=T0,
        fingerprint=FINGERPRINT,
        fingerprint_sha256=FINGERPRINT.sha256(),
        cost=CostInfo(
            cap_usd=Decimal("1.00"),
            embedding_usd=Decimal(0),
            llm_usd=Decimal(total),
            total_usd=Decimal(total),
            llm_cost_complete=True,
            embedding_requests=0,
            llm_calls=48,
        ),
    )


def redteam_run_json(name: str, **fields: object) -> bytes:
    info = redteam_run_info(name, **fields)  # type: ignore[arg-type]
    return (info.model_dump_json(indent=2) + "\n").encode("utf-8")
