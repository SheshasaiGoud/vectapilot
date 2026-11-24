"""The strict origin form (`auth.origins`): one refusal per rule, and no value in any message."""

import pytest
from pydantic import TypeAdapter, ValidationError

from vectapilot_api.auth.origins import (
    ORIGIN_MAX,
    InvalidOrigin,
    Origin,
    is_loopback_http,
    parse_origin,
)


@pytest.mark.parametrize(
    ("value", "loopback"),
    [
        ("https://example.com", False),
        ("https://api.vectapilot.example", False),
        ("https://widget-host.example:8443", False),
        ("https://localhost", False),
        ("https://127.0.0.1:8000", False),
        ("https://0x7f.example", False),  # a number-like label that is not the last: a name
        ("https://shop.x0", False),
        ("http://localhost", True),
        ("http://localhost:3000", True),
        ("http://127.0.0.1:8000", True),
    ],
)
def test_strict_origins_pass_unchanged(value: str, loopback: bool) -> None:
    assert parse_origin(value, allow_http_loopback=loopback) == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("example.com", id="no scheme"),
        pytest.param("ftp://example.com", id="other scheme"),
        pytest.param("https://example.com/", id="trailing slash"),
        pytest.param("https://example.com/widget", id="path"),
        pytest.param("https://example.com?x=1", id="query"),
        pytest.param("https://example.com#x", id="fragment"),
        pytest.param("https://user@example.com", id="user info"),
        pytest.param("https://*.example.com", id="wildcard"),
        pytest.param("*", id="star"),
        pytest.param("https://example.com https://evil.example", id="space"),
        pytest.param(" https://example.com", id="leading whitespace"),
        pytest.param("https://example.com\n", id="newline"),
        pytest.param("https://example.com;", id="semicolon"),
        pytest.param("https://a.example,https://b.example", id="comma"),
        pytest.param("https://Example.com", id="uppercase host"),
        pytest.param("HTTPS://example.com", id="uppercase scheme"),
        pytest.param("https://example.com:443", id="default https port"),
        pytest.param("http://localhost:80", id="default http port"),
        pytest.param("https://example.com:0", id="port zero"),
        pytest.param("https://example.com:65536", id="port too big"),
        pytest.param("https://example.com:08443", id="leading zero port"),
        pytest.param("https://example.com:", id="empty port"),
        pytest.param("https://-example.com", id="label starts with hyphen"),
        pytest.param("https://example..com", id="empty label"),
        pytest.param("https://10.0.0.1", id="another ipv4"),
        # WHATWG "ends in a number": a browser reads each of these as an IPv4 address.
        pytest.param("https://0x7f000001", id="ipv4 as one hex number"),
        pytest.param("https://1.2.3.0x4", id="ipv4 with a hex last label"),
        pytest.param("https://0x7f.0x0.0x0.0x1", id="ipv4 in hex labels"),
        pytest.param("https://127.1", id="ipv4 shorthand"),
        pytest.param("https://www.example.0x", id="a bare 0x last label"),
        pytest.param("https://[::1]", id="ipv6"),
        pytest.param("http://example.com", id="http not loopback"),
        pytest.param("'none'", id="a csp keyword"),
        pytest.param("https://" + "a" * ORIGIN_MAX, id="too long"),
    ],
)
def test_every_other_form_is_refused_without_the_value(value: str) -> None:
    with pytest.raises(InvalidOrigin) as refused:
        parse_origin(value, allow_http_loopback=True)

    if value:
        assert value not in str(refused.value)


def test_http_loopback_only_when_allowed() -> None:
    with pytest.raises(InvalidOrigin, match="never in production"):
        parse_origin("http://localhost:3000", allow_http_loopback=False)


def test_the_type_allows_http_loopback_and_the_helper_names_it() -> None:
    origin = TypeAdapter(Origin).validate_python("http://127.0.0.1:8000")

    assert is_loopback_http(origin)
    assert not is_loopback_http("https://example.com")
    with pytest.raises(ValidationError):
        TypeAdapter(Origin).validate_python("https://example.com/")
