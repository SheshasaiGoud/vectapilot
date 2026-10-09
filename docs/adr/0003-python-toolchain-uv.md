# ADR 0003 — Python toolchain: uv

- **Status:** accepted · 2026-10-09

**Context.** The backend needs Python 3.12; the Mac has only the system Python 3.9. We want
reproducible installs (same versions locally and in CI), fast setup, and no changes to the system
Python.

**Options.** (a) Homebrew `python@3.12` + `venv` + `pip` + requirements files; (b) Poetry;
(c) uv — one tool that installs Python versions, manages the virtualenv and writes a lockfile.

**Decision.** (c) uv. `apps/api/.python-version` pins 3.12; `uv.lock` pins every package;
CI installs with `uv sync --locked` (fails if the lockfile is out of date) and pins the same uv
version as local development. Quality tools: ruff (format + lint, incl. security rules), mypy
`--strict` with the pydantic plugin, pytest with `xfail_strict` and warnings-as-errors.

**Consequences.** One command (`uv sync`) sets up a working environment in seconds; the system
Python is untouched. Contributors need uv installed. Known exception to warnings-as-errors:
Starlette 1.x's deprecation of `httpx` for `TestClient` (moving to `httpx2` awaits a decision).
