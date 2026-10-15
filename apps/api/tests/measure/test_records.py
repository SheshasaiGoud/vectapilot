"""PR 8: reading `llm_call` lines back and pairing them with the calls made (`measure/records.py`).

The line format and the count check are in docs/plans/m1-pr-checks.md, PR 8. The last test runs the
whole gateway_smoke dataset through the real gateway on the fake provider (no key, no network) and
reads the records back exactly as the CLI will: the parser reads what the gateway really writes.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from vectapilot_api.gateway import gateway as gateway_module
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.errors import GatewayError
from vectapilot_api.gateway.gateway import LLMGateway
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.gateway.types import LLMRequest, Message, RetryPolicy
from vectapilot_api.log import JsonFormatter
from vectapilot_api.measure.dataset import load_cases, load_manifest
from vectapilot_api.measure.records import (
    LLM_CALL,
    CallMade,
    RecordCountMismatch,
    RecordError,
    join_records,
    parse_llm_call_lines,
)
from vectapilot_api.measure.schemas import SCHEMAS
from vectapilot_api.measure.summary import RunInfo, RunLimits, summarise_run

FIXTURE: Final = Path(__file__).parent / "fixtures" / "summary_run"
LOG_LINES: Final = (FIXTURE / "llm_call_log.jsonl").read_text().splitlines()
CALLS_MADE: Final = tuple(
    CallMade.model_validate_json(line)
    for line in (FIXTURE / "calls_made.jsonl").read_text().splitlines()
)
DATASET: Final = Path(__file__).parents[4] / "evals" / "datasets" / "gateway_smoke" / "v0.1"
CANARY: Final = "canary-text-7f3a"


def test_the_record_key_is_the_gateways() -> None:
    assert LLM_CALL == gateway_module.LLM_CALL == "llm_call"


def test_only_llm_call_lines_are_read() -> None:
    # The fixture log: one line from another logger, one blank line, then 27 llm_call lines.
    assert json.loads(LOG_LINES[0])["message"] != LLM_CALL
    assert LOG_LINES[1] == ""

    records = parse_llm_call_lines(LOG_LINES)

    assert len(records) == 27
    assert records[0].trace_id == CALLS_MADE[0].trace_id


def test_json_that_is_not_an_object_is_skipped() -> None:
    assert parse_llm_call_lines(["[1, 2]", '"llm_call"', "null"]) == ()


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (f"{CANARY} is not JSON", "log line 1 is not JSON"),
        (json.dumps({"message": LLM_CALL}), "log line 1: the llm_call record is missing"),
        (
            json.dumps({"message": LLM_CALL, LLM_CALL: {"business_id": CANARY}}),
            "log line 1: the llm_call record is missing or invalid",
        ),
    ],
)
def test_an_unreadable_llm_call_line_fails_without_echoing_it(line: str, message: str) -> None:
    with pytest.raises(RecordError) as caught:
        parse_llm_call_lines([line])

    assert str(caught.value).startswith(message)
    assert CANARY not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_every_call_is_paired_with_its_record_by_trace_id() -> None:
    records = parse_llm_call_lines(LOG_LINES)

    in_order = join_records(CALLS_MADE, records)
    reversed_records = join_records(CALLS_MADE, records[::-1])

    assert in_order == reversed_records  # paired by trace id, not by position
    assert [(c.case_id, c.repeat) for c in in_order][-2:] == [("s01", 1), ("s02", 1)]
    assert all(c.record.trace_id == m.trace_id for c, m in zip(in_order, CALLS_MADE, strict=True))


@pytest.mark.parametrize("records_read", [26, 28])
def test_the_run_fails_unless_it_read_exactly_one_record_per_call(records_read: int) -> None:
    records = parse_llm_call_lines(LOG_LINES)
    wrong = records[:records_read] if records_read < 27 else (*records, records[0])

    with pytest.raises(RecordCountMismatch) as caught:
        join_records(CALLS_MADE, wrong)

    assert (caught.value.calls_made, caught.value.records_read) == (27, records_read)
    assert str(caught.value) == (
        f"expected exactly 27 llm_call records (one per call made), read {records_read}:"
        " the run fails"
    )


def test_records_that_cannot_be_paired_fail_the_run() -> None:
    records = parse_llm_call_lines(LOG_LINES)
    first = records[0]
    no_trace = first.model_copy(update={"trace_id": None})
    unknown_trace = first.model_copy(update={"trace_id": "f" * 32})

    with pytest.raises(RecordError, match="has no trace_id"):
        join_records(CALLS_MADE, (no_trace, *records[1:]))
    with pytest.raises(RecordError, match="share a trace_id"):
        join_records(CALLS_MADE, (records[1], *records[1:]))
    with pytest.raises(RecordError, match="no llm_call record for case s01 \\(repeat 0\\)"):
        join_records(CALLS_MADE, (unknown_trace, *records[1:]))


def test_calls_made_must_be_unique() -> None:
    records = parse_llm_call_lines(LOG_LINES)
    same_trace = CALLS_MADE[1].model_copy(update={"trace_id": CALLS_MADE[0].trace_id})
    same_case = CALLS_MADE[1].model_copy(update={"case_id": "s01"})

    with pytest.raises(RecordError, match="own trace_id"):
        join_records((CALLS_MADE[0], same_trace, *CALLS_MADE[2:]), records)
    with pytest.raises(RecordError, match="own case_id and repeat"):
        join_records((CALLS_MADE[0], same_case, *CALLS_MADE[2:]), records)


# ── End to end: the dataset through the real gateway on the fake provider ───────────────────


@pytest.mark.anyio
async def test_a_dry_run_of_the_dataset_reads_back_into_a_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    manifest = load_manifest((DATASET / "manifest.json").read_text("utf-8"))
    cases = load_cases((DATASET / "cases.jsonl").read_text("utf-8"))
    catalog = load_catalog()
    # One source for the gateway and the summary, so the summary names the limits that were used.
    limits = RunLimits(
        max_retries=2,
        max_repairs=1,
        attempt_timeout_s=15.0,
        deadline_s=30.0,
        backoff_base_s=0.5,
        backoff_cap_s=8.0,
        max_output_tokens={
            name: alias.max_output_tokens for name, alias in catalog.aliases.items()
        },
    )
    calls_made: list[CallMade] = []

    for number, case in enumerate(cases, start=1):
        replies = [case.fake_reply, *([case.fake_repair_reply] if case.fake_repair_reply else [])]
        gateway = LLMGateway(
            FakeProvider([FakeReply(text=text) for text in replies]),
            catalog,
            attempt_timeout_s=limits.attempt_timeout_s,
            deadline_s=limits.deadline_s,
            retry_policy=RetryPolicy(
                max_retries=limits.max_retries,
                base_delay_s=limits.backoff_base_s,
                max_delay_s=limits.backoff_cap_s,
            ),
            max_repairs=limits.max_repairs,
        )
        trace_id = f"{number:032x}"
        request = LLMRequest(
            business_id=case.business_id,
            alias=case.alias,
            messages=(Message(role="user", content=case.prompt),),
            trace_id=trace_id,
        )
        try:
            if case.schema_name is None:
                await gateway.complete(request)
            else:
                await gateway.complete_structured(request, SCHEMAS[case.schema_name])
        except GatewayError:
            pass  # an expected outcome is still a call with its record
        calls_made.append(CallMade(case_id=case.id, repeat=0, trace_id=trace_id))

    lines = [JsonFormatter().format(r) for r in caplog.records]  # as the CLI's log handler writes
    measured = join_records(calls_made, parse_llm_call_lines(lines))
    summary = summarise_run(
        RunInfo(
            dataset=manifest.dataset,
            dataset_version=manifest.version,
            dataset_review=manifest.review,
            dataset_committed=False,
            commit="0" * 40,
            uncommitted_changes=True,
            provider="fake",
            started_at=datetime(2026, 10, 15, tzinfo=UTC),
            catalog_version=catalog.catalog_version,
            alias_models={name: alias.model for name, alias in catalog.aliases.items()},
            limits=limits,
            planned_calls=len(cases),
        ),
        cases,
        measured,
    )

    small, large = summary.per_alias[1], summary.per_alias[0]
    assert (summary.distinct_calls, small.distinct_prompts, large.distinct_prompts) == (24, 12, 12)
    assert {a.alias: a.max_output_tokens for a in summary.per_alias} == limits.max_output_tokens
    # Every canned structured reply is valid on the first try: 5/5 per alias, none unvalidated.
    assert small.first_try is not None and large.first_try is not None
    assert (str(small.first_try.valid), str(large.first_try.valid)) == ("5/5", "5/5")
    [forced] = small.forced_repairs
    assert (forced.case_id, forced.result) == ("northwind-en-bill-forced-repair", "repair worked")
    assert summary.feeds_note is False
    assert "dry run: fake provider, not a measurement" in summary.not_for_note
