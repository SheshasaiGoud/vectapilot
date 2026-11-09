"""The embedding cache (M4 PR 4): float32 base64 vectors keyed by row id and text hash, checked
against the frozen rows. Made-up rows (`helpers`); no text is stored, only its hash."""

import unicodedata

import pytest

from tests.intenteval.helpers import eval_row, train_row
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.intenteval.cache import (
    CacheEntry,
    cache_problems,
    decode_vector,
    encode_vector,
    normalise_text,
    parse_cache,
    render_cache,
    text_sha256,
)
from vectapilot_api.intenteval.rows import EvalRow, TrainRow


def entry(row: TrainRow | EvalRow, file: str, **changes: object) -> CacheEntry:
    values: dict[str, object] = {
        "id": row.id,
        "file": file,
        "text_sha256": text_sha256(row.text),
        "model": "voyage-4",
        "dimension": 2,
        "input_type": "query",
        "vector": encode_vector((0.6, 0.8)),
    }
    return CacheEntry.model_validate(values | changes)


def test_vectors_round_trip_as_float32() -> None:
    # 0.6 and 0.8 are not float32-exact: the decoded values are the float32 ones, for train and
    # scoring alike.
    decoded = decode_vector(encode_vector((0.6, 0.8, 1.0)), 3)

    assert decoded[2] == 1.0
    assert abs(decoded[0] - 0.6) < 1e-7 and decoded[0] != 0.6
    with pytest.raises(ValueError, match="dimension"):
        decode_vector(encode_vector((0.6, 0.8)), 3)
    with pytest.raises(ValueError, match="not finite"):
        encode_vector((float("nan"),))


def test_the_hash_is_of_the_nfc_text() -> None:
    composed = "café"
    decomposed = unicodedata.normalize("NFD", composed)

    assert decomposed != composed
    assert normalise_text(decomposed) == composed
    assert text_sha256(decomposed) == text_sha256(composed)
    assert text_sha256("café ") != text_sha256(composed)


def test_entries_render_and_parse_without_text() -> None:
    row = train_row(id="example-t1")
    text = render_cache([entry(row, "train")])

    assert row.text not in text
    assert parse_cache(text) == (entry(row, "train"),)
    with pytest.raises(DatasetError, match="line 1 is not a valid entry"):
        parse_cache('{"id": "x"}\n')


def test_a_cache_that_matches_the_rows_has_no_problems() -> None:
    t, e = train_row(id="example-t1"), eval_row(id="example-e1")

    assert cache_problems([entry(t, "train"), entry(e, "eval")], [t], [e], model="voyage-4") == []


def test_every_mismatch_is_named_by_id() -> None:
    t1, t2 = train_row(id="example-t1"), train_row(id="example-t2")
    e1 = eval_row(id="example-e1")
    changed = eval_row(id="example-e1", text="TEST-ONLY another text")
    entries = [
        entry(t1, "train"),
        entry(t1, "train"),  # twice
        entry(e1, "train"),  # filed under the wrong file
        entry(train_row(id="example-gone"), "train"),  # not in the dataset
    ]

    problems = cache_problems(entries, [t1, t2], [changed], model="voyage-4")

    assert problems == [
        "embeddings.jsonl: row example-t1 has 2 entries",
        "embeddings.jsonl: train row example-t2 has no vector",
        "embeddings.jsonl: row example-e1 is cached as a train row",
        "embeddings.jsonl: row example-gone is not in the dataset",
    ]
    assert cache_problems([entry(e1, "eval")], [], [changed]) == [
        "embeddings.jsonl: row example-e1's text changed since it was embedded"
    ]


def test_mixed_or_other_models_are_refused() -> None:
    t1, t2 = train_row(id="example-t1"), train_row(id="example-t2")
    entries = [entry(t1, "train"), entry(t2, "train", model="voyage-4-lite")]

    assert "mixes models or dimensions" in " ".join(cache_problems(entries, [t1, t2], []))
    # One model, two dimensions: refused too (ADR 0008: never mixed vectors).
    mixed = [
        entry(t1, "train"),
        entry(t2, "train", dimension=4, vector=encode_vector((0.5, 0.5, 0.5, 0.5))),
    ]
    assert "mixes models or dimensions" in " ".join(cache_problems(mixed, [t1, t2], []))
    assert cache_problems([entry(t1, "train")], [t1], [], model="voyage-4-lite") == [
        "embeddings.jsonl was embedded with another model than voyage-4-lite"
    ]
    assert cache_problems([entry(t1, "train")], [t1], [], dimension=1024) == [
        "embeddings.jsonl has another dimension than 1024"
    ]
