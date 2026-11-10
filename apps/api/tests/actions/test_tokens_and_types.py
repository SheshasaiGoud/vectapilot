"""M5 PR 3: the confirm token, the request and response types, and the lifetime setting. Unit.

- **The token (W2):** 128 bits from `secrets.token_urlsafe(16)`, 22 URL-safe characters; only its
  SHA-256 is stored; the comparison is `hmac.compare_digest` (constant time); a repr, a log extra,
  `model_dump` and `model_dump_json` all print the mask, never the token.
- **The body is closed:** customer and token only. Tool arguments, a business id or an action id
  in the body are refused (W2, W7), and a 422 never echoes the token.
- **The values agree with migration 0006** (statuses, failure reasons and audit outcomes fit its
  CHECKs), and every failure has its fixed sentence.
- **The lifetime (W6)** is more than zero and at most an hour, at startup (Settings) and in the
  store's constructor: migration 0006's CHECK, so a typo stops startup.
"""

import hashlib
import hmac
import importlib
import json
import re
import secrets
from typing import Any

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings
from vectapilot_api.actions import tokens
from vectapilot_api.actions.confirm import ConfirmOutcome
from vectapilot_api.actions.pending import MAX_LIFETIME_S, STORED, check_lifetime
from vectapilot_api.actions.tokens import (
    TOKEN_LENGTH,
    ConfirmToken,
    new_confirm_token,
    token_matches,
    token_sha256,
)
from vectapilot_api.actions.types import (
    FAILURE_MESSAGES,
    ActionStatus,
    ConfirmRequest,
    ConfirmResponse,
)
from vectapilot_api.tools.book import BookOutput
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.types import WriteFailure

MIGRATION = importlib.import_module("migrations.versions.0006_sandbox_and_tool_tables")
TOKEN = "AbCdEfGhIjKlMnOpQrStUv"  # noqa: S105 (a test token, not a secret)


def test_a_token_is_128_random_bits_as_22_url_safe_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int | None] = []
    real = secrets.token_urlsafe

    def spy(nbytes: int | None = None) -> str:
        asked.append(nbytes)
        return real(nbytes)

    monkeypatch.setattr(secrets, "token_urlsafe", spy)
    token = new_confirm_token().get_secret_value()

    assert asked == [16] and len(token) == TOKEN_LENGTH == 22
    assert re.fullmatch(tokens.TOKEN_PATTERN, token)
    assert len({new_confirm_token().get_secret_value() for _ in range(50)}) == 50


def test_only_the_tokens_sha256_is_stored_and_it_fits_the_column() -> None:
    token = ConfirmToken(TOKEN)

    stored = token_sha256(token)

    assert stored == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert re.fullmatch(r"^[0-9a-f]{64}$", stored) and TOKEN not in stored


def test_the_token_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    compared: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    stored = token_sha256(ConfirmToken(TOKEN))

    assert token_matches(ConfirmToken(TOKEN), stored)
    assert not token_matches(ConfirmToken("x" * 22), stored)
    assert compared == [(stored, stored), (token_sha256(ConfirmToken("x" * 22)), stored)]


def test_the_token_is_masked_everywhere_but_get_secret_value() -> None:
    request = ConfirmRequest(customer_ref="cust-001", confirm_token=ConfirmToken(TOKEN))
    parsed = ConfirmRequest.model_validate({"customer_ref": "cust-001", "confirm_token": TOKEN})

    shown = [repr(request), str(request), str(request.model_dump()), request.model_dump_json()]
    assert all(TOKEN not in text for text in shown)
    assert parsed.confirm_token.get_secret_value() == TOKEN


@pytest.mark.parametrize(
    "extra",
    [
        {"slot_id": "S-09"},
        {"arguments": {"slot_id": "S-09"}},
        {"business_id": "paws-and-co"},
        {"action_id": "5f0c0b1e-0000-4000-8000-000000000000"},
    ],
)
def test_the_body_holds_the_customer_and_the_token_only(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ConfirmRequest.model_validate({"customer_ref": "cust-001", "confirm_token": TOKEN} | extra)


@pytest.mark.parametrize(
    "token", ["", "q7Zx9", TOKEN + "x", "!" * 22, "a b" + "c" * 19, TOKEN[:-1] + "="]
)
def test_a_malformed_token_is_refused_without_echoing_it(token: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ConfirmRequest.model_validate({"customer_ref": "cust-001", "confirm_token": token})

    assert not token or token not in str(caught.value)


def test_statuses_reasons_and_outcomes_fit_migration_0006() -> None:
    assert tuple(ActionStatus) == MIGRATION.ACTION_STATUSES
    codes = [*WriteFailure, *ConfirmOutcome, STORED]
    assert all(re.fullmatch(MIGRATION.CODE, code) for code in codes)
    assert set(FAILURE_MESSAGES) == set(WriteFailure)
    assert len(set(FAILURE_MESSAGES.values())) == len(WriteFailure)


def test_a_response_is_closed_and_typed() -> None:
    response = ConfirmResponse(
        action_id="5f0c0b1e-0000-4000-8000-000000000000",
        tool=ToolName.BOOK,
        status=ActionStatus.EXECUTED,
        result=BookOutput(booking_id="B-1", slot_id="S-09"),
        message="Done.",
    )

    assert json.loads(response.model_dump_json())["result"] == {
        "booking_id": "B-1",
        "slot_id": "S-09",
    }
    with pytest.raises(ValidationError):
        ConfirmResponse.model_validate(response.model_dump() | {"status": "pending"})


@pytest.mark.parametrize("seconds", [1, 900, 3600])
def test_a_lifetime_within_an_hour_is_accepted(seconds: int) -> None:
    assert make_settings(action_lifetime_seconds=seconds).action_lifetime_seconds == seconds
    assert check_lifetime(seconds) == seconds


@pytest.mark.parametrize("seconds", [0, -1, 3601, 86400])
def test_a_lifetime_outside_migration_0006s_check_stops_startup(seconds: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(action_lifetime_seconds=seconds)
    with pytest.raises(ValueError, match="at most 3600"):
        check_lifetime(seconds)


def test_the_lifetime_bound_is_migration_0006s() -> None:
    assert MIGRATION.MAX_ACTION_LIFETIME == "1 hour" and MAX_LIFETIME_S == 3600
    assert make_settings().action_lifetime_seconds == 900
