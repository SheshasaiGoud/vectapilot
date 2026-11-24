"""S3's tokens: `<business_id>.<secret>`, 256 random bits, only the SHA-256 stored."""

import hashlib
import hmac
import re

import pytest

from tests.auth.fakes import BUSINESS, OTHER, OTHER_TOKEN, SECRET, TOKEN
from vectapilot_api.auth.tokens import (
    SECRET_BYTES,
    SECRET_LENGTH,
    SHA256_PATTERN,
    TOKEN_MAX,
    InvalidToken,
    ParsedToken,
    make_token,
    parse_token,
    same_hash,
    token_sha256,
)
from vectapilot_api.db import InvalidBusinessId


def test_a_fresh_token_is_the_business_and_43_url_safe_characters() -> None:
    token = make_token(BUSINESS)

    business, separator, secret = token.partition(".")
    assert (business, separator) == (BUSINESS, ".")
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", secret)
    # 256 bits (S3): ceil(32 x 4 / 3) = 43 base64url characters, arithmetic.
    assert (SECRET_BYTES * 8, SECRET_LENGTH) == (256, 43)
    assert -(-SECRET_BYTES * 4 // 3) == SECRET_LENGTH
    assert TOKEN_MAX == 40 + 1 + 43


def test_fresh_tokens_differ() -> None:
    assert len({make_token(BUSINESS) for _ in range(100)}) == 100


def test_a_token_is_never_made_for_an_invalid_business() -> None:
    with pytest.raises(InvalidBusinessId):
        make_token("Not A Business")


def test_parsing_gives_the_business_and_the_hash_of_the_whole_token() -> None:
    parsed = parse_token(TOKEN)

    assert parsed == ParsedToken(BUSINESS, hashlib.sha256(TOKEN.encode()).hexdigest())
    assert parsed.sha256 == token_sha256(TOKEN)
    assert re.fullmatch(SHA256_PATTERN, parsed.sha256)
    assert parse_token(make_token(BUSINESS)).business_id == BUSINESS


def test_an_edited_prefix_names_the_other_business_and_changes_the_hash() -> None:
    # S3: the same secret under another prefix is another token: looked up in the other business,
    # with another hash, so it is found nowhere.
    mine, edited = parse_token(TOKEN), parse_token(OTHER_TOKEN)

    assert (mine.business_id, edited.business_id) == (BUSINESS, OTHER)
    assert mine.sha256 != edited.sha256


def test_the_repr_never_shows_the_hash() -> None:
    parsed = parse_token(TOKEN)

    assert parsed.sha256 not in repr(parsed)
    assert SECRET not in repr(parsed)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("", id="empty"),
        pytest.param(SECRET, id="no prefix"),
        pytest.param(f".{SECRET}", id="empty prefix"),
        pytest.param(f"{BUSINESS}.", id="no secret"),
        pytest.param(f"{BUSINESS}.{SECRET[:-1]}", id="secret one short"),
        pytest.param(f"{BUSINESS}.{SECRET}A", id="secret one long"),
        pytest.param(f"{BUSINESS}.{SECRET[:-1]}=", id="padding"),
        pytest.param(f"{BUSINESS}.{SECRET[:-1]}+", id="not url-safe"),
        pytest.param(f"{BUSINESS}.{SECRET[:-1]}.", id="a second separator"),
        pytest.param(f"Paws-And-Co.{SECRET}", id="uppercase business"),
        pytest.param(f"ab.{SECRET}", id="business too short"),
        pytest.param(f"{'a' * 41}.{SECRET}", id="business too long"),
        pytest.param(f"{BUSINESS} .{SECRET}", id="space"),
        pytest.param(f"{BUSINESS}.{SECRET}\n", id="newline"),
        pytest.param("x" * 10_000, id="huge"),
    ],
)
def test_anything_else_is_one_invalid_token_error_without_the_value(token: str) -> None:
    with pytest.raises(InvalidToken) as refused:
        parse_token(token)

    assert str(refused.value) == "not a valid token"
    error = refused.value  # nothing chained that could carry the value
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


def test_hashes_compare_in_constant_time_and_exactly() -> None:
    digest = token_sha256(TOKEN)

    assert same_hash(digest, token_sha256(TOKEN))
    assert not same_hash(digest, token_sha256(OTHER_TOKEN))
    assert not same_hash(digest, digest[:-1])


def test_hashes_are_compared_by_hmac_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    # The result alone cannot tell `==` from a constant-time compare (the mutation review).
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def spy(first: str, second: str) -> bool:
        calls.append((first, second))
        return real(first, second)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    digest, other = token_sha256(TOKEN), token_sha256(OTHER_TOKEN)

    assert same_hash(digest, digest)
    assert not same_hash(digest, other)
    assert calls == [(digest, digest), (digest, other)]  # S3: constant time, never ==
