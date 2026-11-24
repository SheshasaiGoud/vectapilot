"""The staff session cookie (S5, D4 (a)): `__Host-vp_staff`, `HttpOnly; Secure; SameSite=Strict;
Path=/`.

- **`__Host-`**: a browser accepts the cookie only with `Secure`, `Path=/` and no `Domain`, so a
  sibling subdomain can never set or overwrite it.
- **`HttpOnly`**: no script, the dashboard's own included, can read it.
- **`SameSite=Strict`**: a cross-site request never carries it; with S5's Origin check
  (`dependencies.require_same_origin`) it is the CSRF control.
- **Lifetime**: `Max-Age` is the session's absolute lifetime (P1); the database's `expires_at`
  is what counts, the browser's copy may outlive it and is then refused.
- **The value is the token** (`<business_id>.<secret>`, all cookie-safe characters, so it is
  never quoted); only its hash is stored.

**`cookie_insecure_dev`** (local and test only, refused elsewhere at startup by `Settings`): a
browser may refuse a `Secure` cookie over plain `http://localhost` (ADR 0019, Consequences; rule 7:
checked by hand in PR 1). With it the cookie is `vp_staff`, without `Secure` and so without the
`__Host-` prefix, which requires it; `HttpOnly`, `SameSite=Strict` and `Path=/` stay.

**Reading** takes exactly one copy across every `Cookie` header: with two, a planted cookie and
the real one could each be read by a different layer (as `business.get_business_id` for its
header). None otherwise, and the caller's one 401 follows.
"""

from typing import Final

from fastapi import Request, Response

from vectapilot_api.config import Settings

STAFF_COOKIE: Final = "__Host-vp_staff"
DEV_STAFF_COOKIE: Final = "vp_staff"
"""The name under `cookie_insecure_dev`: `__Host-` requires `Secure`."""
COOKIE_PATH: Final = "/"


def staff_cookie_name(settings: Settings) -> str:
    return DEV_STAFF_COOKIE if settings.cookie_insecure_dev else STAFF_COOKIE


def set_staff_cookie(response: Response, token: str, *, max_age_s: int, settings: Settings) -> None:
    """Put the staff session's token into `response`'s cookie, for `max_age_s` seconds."""
    response.set_cookie(
        staff_cookie_name(settings),
        token,
        max_age=max_age_s,
        path=COOKIE_PATH,
        secure=not settings.cookie_insecure_dev,
        httponly=True,
        samesite="strict",
    )


def clear_staff_cookie(response: Response, *, settings: Settings) -> None:
    """Tell the browser to drop the staff cookie (logout), with the same attributes it was set
    with: a browser ignores a `__Host-` deletion without `Secure`."""
    response.delete_cookie(
        staff_cookie_name(settings),
        path=COOKIE_PATH,
        secure=not settings.cookie_insecure_dev,
        httponly=True,
        samesite="strict",
    )


def read_staff_cookie(request: Request, *, settings: Settings) -> str | None:
    """The staff cookie's value when the request carries exactly one copy of it, else None."""
    name = staff_cookie_name(settings)
    values = [
        value
        for header in request.headers.getlist("cookie")
        for part in header.split(";")
        for key, separator, value in [part.strip().partition("=")]
        if separator and key == name
    ]
    return values[0] if len(values) == 1 else None
