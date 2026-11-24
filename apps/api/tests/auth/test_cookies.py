"""The staff cookie's attributes (S5): `__Host-vp_staff; HttpOnly; Secure; SameSite=Strict;
Path=/`, no `Domain`; and `vp_staff` without `Secure` only under `cookie_insecure_dev`."""

from http.cookies import SimpleCookie

from fastapi import Response

from tests.auth.fakes import TOKEN
from tests.conftest import make_settings
from vectapilot_api.auth.cookies import (
    DEV_STAFF_COOKIE,
    STAFF_COOKIE,
    clear_staff_cookie,
    set_staff_cookie,
    staff_cookie_name,
)

SECURE = make_settings()
INSECURE = make_settings(cookie_insecure_dev=True)


def set_cookie_header(response: Response) -> str:
    [header] = [value for key, value in response.raw_headers if key == b"set-cookie"]
    return header.decode("latin-1")


def attributes(header: str) -> dict[str, str]:
    """The cookie's attributes, lower-cased names; flags map to ''."""
    parts = [part.strip() for part in header.split(";")]
    found: dict[str, str] = {}
    for part in parts[1:]:
        key, _, value = part.partition("=")
        found[key.lower()] = value
    return found


def test_the_staff_cookie_is_host_prefixed_secure_http_only_and_strict() -> None:
    response = Response()

    set_staff_cookie(response, TOKEN, max_age_s=28_800, settings=SECURE)

    header = set_cookie_header(response)
    assert header.startswith(f"{STAFF_COOKIE}={TOKEN};")  # cookie-safe: never quoted
    assert attributes(header) == {
        "httponly": "",
        "max-age": "28800",
        "path": "/",
        "samesite": "strict",
        "secure": "",
    }
    assert SimpleCookie(header)[STAFF_COOKIE].value == TOKEN


def test_clearing_keeps_the_attributes_a_host_cookie_needs() -> None:
    response = Response()

    clear_staff_cookie(response, settings=SECURE)

    header = set_cookie_header(response)
    assert header.startswith(f'{STAFF_COOKIE}="";')
    found = attributes(header)
    assert (found["max-age"], found["path"], found["samesite"]) == ("0", "/", "strict")
    assert "secure" in found and "httponly" in found and "domain" not in found


def test_the_insecure_dev_cookie_drops_secure_and_so_the_prefix() -> None:
    response = Response()

    set_staff_cookie(response, TOKEN, max_age_s=60, settings=INSECURE)

    header = set_cookie_header(response)
    assert header.startswith(f"{DEV_STAFF_COOKIE}={TOKEN};")
    assert attributes(header) == {
        "httponly": "",
        "max-age": "60",
        "path": "/",
        "samesite": "strict",
    }
    assert staff_cookie_name(INSECURE) == DEV_STAFF_COOKIE != staff_cookie_name(SECURE)
    cleared = Response()
    clear_staff_cookie(cleared, settings=INSECURE)
    assert "secure" not in attributes(set_cookie_header(cleared))
