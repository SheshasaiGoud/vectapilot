"""Web origins in their strict form, `scheme://host[:port]` (M8 PR 1; S5, S9, D12 (b)).

An origin is compared byte for byte with a browser's `Origin` header (S5's CSRF check) and written
into a `Content-Security-Policy` header (`frame-ancestors`, `connect-src`, S9). So the configured
text must be exactly what a browser sends, and nothing that could end or extend a header
directive: no path, query, fragment, user info, wildcard, whitespace, `;` or `,`.

- **Lowercase only.** A browser serializes the scheme and host in lowercase; an uppercase letter
  in the configuration would never match, so it is refused rather than silently normalized.
- **No default port.** A browser leaves out `:443` for https and `:80` for http, so a configured
  default port would never match either: refused.
- **https**, or **http only for a loopback host** (`localhost`, `127.0.0.1`), which a caller may
  allow outside production (`config.Settings`; the widget configuration in local). Never an IP
  other than 127.0.0.1, never IPv6 [proposal: none is needed while M8 is local and synthetic]. A
  host is an IPv4 address when its last label is a number, as the WHATWG URL standard's "ends in
  a number" says: decimal digits, or `0x` and hex digits (`https://0x7f000001` is 127.0.0.1 to a
  browser, `https://1.2.3.0x4` is 1.2.3.4).

`parse_origin` raises `InvalidOrigin`, whose message names the rule and never the value: an
origin comes from configuration, and the error may reach a startup log.

This module imports only the standard library and pydantic: `config` imports it.
"""

import re
from typing import Annotated, Final

from pydantic import AfterValidator

LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1"})
"""The only hosts an `http` origin may name, and only where the caller allows it."""
DEFAULT_PORTS: Final = {"https": 443, "http": 80}
ORIGIN_MAX: Final = 253 + len("https://") + len(":65535")
"""A DNS name's longest text form (253) plus the longest scheme and port."""

_LABEL: Final = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_ORIGIN: Final = re.compile(
    rf"(?P<scheme>https?)://(?P<host>{_LABEL}(\.{_LABEL})*)(:(?P<port>[1-9][0-9]{{0,4}}))?"
)
_NUMBER: Final = re.compile(r"0x[0-9a-f]*|[0-9]+")
"""A label a browser reads as an IPv4 number (WHATWG URL, "ends in a number"; the host is already
lowercase here): when the host's last label is one, the host is an IPv4 address."""


class InvalidOrigin(ValueError):
    """Not an origin in the strict form. The message names the rule, never the value."""


def parse_origin(value: str, *, allow_http_loopback: bool) -> str:
    """`value` if it is an origin in the strict form (module docstring), else `InvalidOrigin`.

    `allow_http_loopback`: whether `http://localhost[:port]` and `http://127.0.0.1[:port]` are
    accepted (never in production)."""
    if len(value) > ORIGIN_MAX:
        raise InvalidOrigin(f"an origin is at most {ORIGIN_MAX} characters")
    match = _ORIGIN.fullmatch(value)
    if match is None:
        raise InvalidOrigin(
            "an origin is scheme://host[:port] in lowercase: https (or http for a loopback host),"
            " no path, query, fragment, user, wildcard, whitespace, ';' or ','"
        )
    scheme, host, port = match["scheme"], match["host"], match["port"]
    if host != "127.0.0.1" and _NUMBER.fullmatch(host.rsplit(".", 1)[-1]) is not None:
        raise InvalidOrigin("an origin's host is a name (or 127.0.0.1), never another IP address")
    if port is not None and int(port) > 65535:
        raise InvalidOrigin("an origin's port is 1-65535")
    if port is not None and int(port) == DEFAULT_PORTS[scheme]:
        raise InvalidOrigin("an origin never names its scheme's default port (browsers omit it)")
    if scheme == "http" and not (allow_http_loopback and host in LOOPBACK_HOSTS):
        raise InvalidOrigin(
            "an origin is https; http only for localhost or 127.0.0.1, and never in production"
        )
    return value


def _https_or_loopback(value: str) -> str:
    return parse_origin(value, allow_http_loopback=True)


Origin = Annotated[str, AfterValidator(_https_or_loopback)]
"""An origin in the strict form, with http allowed for a loopback host. Whether that http form is
allowed where it is used (never in production) is the user's rule: `config.Settings` checks it
against the environment."""


def is_loopback_http(origin: str) -> bool:
    """Whether `origin` (already parsed) is an `http` origin, which only a loopback host may be."""
    return origin.startswith("http://")
