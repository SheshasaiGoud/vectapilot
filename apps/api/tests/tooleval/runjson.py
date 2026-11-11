"""A TEST-ONLY tool-call run.json and fingerprint, for the spend and discipline tests."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.tooleval.runinfo import Fingerprint, Limits, ToolRunInfo
from vectapilot_api.tooleval.split import SandboxPins

T0: Final = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
FINGERPRINT: Final = Fingerprint(
    freeze_sha256="f" * 64,
    agent_prompt_version="agent-step.1",
    alias="small",
    catalog_version="test-catalog",
    model="test-model",
    step_schemas={"paws-and-co": "a" * 64},
    sandbox={"paws-and-co": SandboxPins(tools_sha256="b" * 64, seed_sha256="c" * 64)},
    limits=Limits(
        max_steps=4,
        turn_timeout_s=35.0,
        attempt_timeout_s=6.0,
        deadline_s=8.0,
        max_retries=2,
        max_repairs=1,
        action_lifetime_s=900,
    ),
)


def tool_run_info(
    name: str,
    *,
    total: str = "0.40",
    dry: bool = False,
    split: str = "dev",
    status: str = "complete",
    fingerprint: Fingerprint = FINGERPRINT,
    complete: bool = True,
) -> ToolRunInfo:
    return ToolRunInfo.model_validate(
        {
            "run_id": name,
            "status": status,
            "dry_run": dry,
            "environment": "local",
            "dataset": "tool_calls",
            "dataset_version": "v0.1",
            "dataset_stage": "frozen",
            "freeze_sha256": "f" * 64,
            "dataset_committed": True,
            "split": split,
            "scenarios": 2,
            "commit": "c" * 40,
            "uncommitted_changes": False,
            "started_at": T0,
            "ended_at": T0,
            "fingerprint": fingerprint,
            "fingerprint_sha256": fingerprint.sha256(),
            "llm_provider": "fake" if dry else "anthropic",
            "confidence": "19/20",
            "cost": CostInfo(
                cap_usd=Decimal("1.00"),
                embedding_usd=Decimal(0),
                llm_usd=Decimal(total),
                total_usd=Decimal(total),
                llm_cost_complete=complete,
                embedding_requests=0,
                llm_calls=3,
            ),
        }
    )


def tool_run_json(name: str, **fields: object) -> bytes:
    info = tool_run_info(name, **fields)  # type: ignore[arg-type]
    return (info.model_dump_json(indent=2) + "\n").encode("utf-8")
