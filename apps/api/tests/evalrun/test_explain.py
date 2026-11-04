"""M3 PR 8's latency measurement (`evalrun/explain.py`): the pure parts, without a database.

The measurement itself runs against Postgres as the app role (`python -m vectapilot_api.evalrun
latency`); its committed report is the end-to-end proof. These tests pin the parsing of EXPLAIN's
JSON, the index question, the sample questions and the report's label.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.evalrun import explain
from vectapilot_api.evalset.files import read_dataset
from vectapilot_api.measure.latency import latency_summary

REPO = Path(__file__).resolve().parents[4]
DATASET = REPO / "evals" / "datasets" / "support_qa" / "v0.1"


def plan(**top: object) -> str:
    """A small EXPLAIN (FORMAT JSON) document: a Limit over a Sort over an index scan."""
    document = {
        "Plan": {
            "Node Type": "Limit",
            "Actual Rows": 3,
            "Actual Loops": 1,
            "Shared Read Blocks": 2,
            "Plans": [
                {
                    "Node Type": "Sort",
                    "Plans": [
                        {
                            "Node Type": "Index Scan",
                            "Relation Name": "chunks",
                            "Index Name": "chunks_embedding_hnsw",
                            "Order By": "(embedding <=> '[1,0]'::vector)",
                        },
                        {"Node Type": "Seq Scan", "Relation Name": "documents"},
                    ],
                }
            ],
        },
        "Planning Time": 0.25,
        "Execution Time": 1.5,
    }
    document.update(top)
    return json.dumps([document])


def test_parse_explain_reads_times_rows_blocks_and_every_node() -> None:
    sample = explain.parse_explain(plan())

    assert (sample.planning_ms, sample.execution_ms) == (0.25, 1.5)
    assert (sample.rows, sample.shared_read_blocks) == (3, 2)
    assert [node.shape for node in sample.nodes] == [
        ("Limit", None, None),
        ("Sort", None, None),
        ("Index Scan", "chunks", "chunks_embedding_hnsw"),
        ("Seq Scan", "documents", None),
    ]


@pytest.mark.parametrize(
    ("operator", "used"),
    [("<=>", True), ("@@", False)],
)
def test_an_index_counts_only_when_it_evaluates_the_operator_itself(
    operator: str, used: bool
) -> None:
    assert explain.index_used_for(explain.parse_explain(plan()).nodes, operator) is used


def test_a_filter_on_an_index_node_is_not_the_index_evaluating_the_operator() -> None:
    document = json.loads(plan())
    scan = document[0]["Plan"]["Plans"][0]["Plans"][0]
    del scan["Order By"]
    scan["Filter"] = "(tsv @@ q)"  # evaluated on the rows afterwards, not by the index

    nodes = explain.parse_explain(json.dumps(document)).nodes

    assert not explain.index_used_for(nodes, "@@")


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps([]),
        json.dumps([{"Plan": {"Node Type": "Limit"}}]),
        plan(**{"Execution Time": -1}),
        plan(**{"Planning Time": True}),
    ],
)
def test_a_malformed_explain_is_refused(raw: str) -> None:
    with pytest.raises(explain.PlanError):
        explain.parse_explain(raw)


def test_sample_questions_are_customer_visible_headings_distinct_per_business() -> None:
    dataset = read_dataset(DATASET, None)
    businesses = list(dataset.manifest.businesses)

    questions = explain.sample_questions(dataset.corpus, businesses)

    assert questions
    assert {q.business_id for q in questions} == set(businesses)
    for business in businesses:
        texts = [q.text for q in questions if q.business_id == business]
        assert len(texts) == len(set(texts))
        visible = {
            d.source.source_id
            for d in dataset.corpus.documents(business)
            if d.source.answers_customers
        }
        assert {q.source_id for q in questions if q.business_id == business} <= visible


def test_the_request_refuses_no_repeats_and_no_questions() -> None:
    dataset = read_dataset(DATASET, None)
    questions = explain.sample_questions(dataset.corpus, list(dataset.manifest.businesses))
    mapping = {b: f"{b}-ev-test" for b in dataset.manifest.businesses}
    fake = load_embedding_catalog().resolve("fake", None, 1024)

    def request(questions: tuple[explain.SampleQuestion, ...], repeats: int) -> None:
        explain.LatencyRequest(
            mapping=mapping,
            questions=questions,
            expected_chunks={},
            spec=fake,
            k=20,
            rrf_k=60,
            repeats=repeats,
            warmup=0,
        )

    with pytest.raises(ValueError, match="repeats"):
        request(questions, 0)
    with pytest.raises(ValueError, match="question"):
        request((), 1)


def test_the_report_names_its_label_and_never_claims_a_target() -> None:
    measured_at = datetime(2026, 11, 2, 9, tzinfo=UTC)
    assert explain.report_paths(Path("x"), measured_at) == (
        Path("x/2026-11-02-local.json"),
        Path("x/2026-11-02-local.md"),
    )
    assert "8 GB Mac" in explain.LABEL
    assert "fake embeddings" in explain.LABEL
    # p95 only at n >= 20 (M1's rule), which the report relies on.
    assert latency_summary([1.0] * 19).p95_ms is None
    assert latency_summary([1.0] * 20).p95_ms is not None


@pytest.mark.parametrize(
    ("output", "expected"),
    [("4109217792\n", 4109217792), ("", None), ("0", None), ("4 GiB", None)],
)
def test_docker_memory_is_parsed_or_left_unknown(output: str, expected: int | None) -> None:
    assert explain.parse_mem_total(output) == expected
