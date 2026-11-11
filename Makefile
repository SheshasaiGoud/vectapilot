# VectaPilot developer commands. Run `make` (or `make help`) to list them.

COMPOSE := docker compose -f infra/compose.yaml
API     := cd apps/api &&
MCP     := cd packages/mcp_server &&
# The API's role and the owner's (ADR 0009); the passwords default like infra/compose.yaml's.
LOCAL_DB_URL    := postgresql+asyncpg://vectapilot_app:$${VECTAPILOT_APP_DB_PASSWORD:-vectapilot-app-dev}@127.0.0.1:5432/vectapilot
LOCAL_OWNER_URL := postgresql+asyncpg://vectapilot:$${POSTGRES_PASSWORD:-vectapilot-dev}@127.0.0.1:5432/vectapilot
LOCAL_REDIS_URL := redis://127.0.0.1:6379/0

.DEFAULT_GOAL := help
.PHONY: help up down logs ps migrate reset-db lint format typecheck test coverage test-integration check mcp-check mcp-test-integration mcp-lock llm-demo eval-check eval-dry-run intent-check intent-dry-run tool-check tool-dry-run

help: ## List the available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-17s %s\n", $$1, $$2}'

# ── Local stack (Docker) ────────────────────────────────────────────────────────────────
up: ## Start Postgres + Redis, run migrations, start the API (http://127.0.0.1:8000)
	$(COMPOSE) up --build --detach --wait

down: ## Stop the local stack (database data is kept)
	$(COMPOSE) down

logs: ## Follow the logs of every service
	$(COMPOSE) logs --follow

ps: ## Show the status of every service
	$(COMPOSE) ps --all

migrate: ## Apply database migrations (as the owner) and set the app role's login
	$(COMPOSE) run --rm migrate

reset-db: ## DELETE all local database data (asks for confirmation)
	@read -p "This deletes ALL local Postgres data. Type 'yes' to continue: " answer && [ "$$answer" = yes ]
	$(COMPOSE) down --volumes

# ── Quality checks (the same ones CI runs) ──────────────────────────────────────────────
lint: ## Format check + lint (API and web)
	$(API) uv run ruff format --check . && uv run ruff check .
	cd apps/web && npm run lint

format: ## Auto-format the API code
	$(API) uv run ruff format .

typecheck: ## Strict type check of the API
	$(API) uv run mypy

test: ## API tests that need no services
	$(API) uv run pytest

coverage: ## API tests under coverage, then the gateway gate (>= 95 %, ADR 0006)
	$(API) uv run coverage run -m pytest && uv run python scripts/coverage_gate.py

test-integration: up ## API tests including real Postgres + Redis (starts the stack)
	$(API) RUN_INTEGRATION=1 VECTAPILOT_DATABASE_URL="$(LOCAL_DB_URL)" \
		VECTAPILOT_MIGRATION_DATABASE_URL="$(LOCAL_OWNER_URL)" \
		VECTAPILOT_REDIS_URL="$(LOCAL_REDIS_URL)" uv run pytest

# The MCP lock records apps/api's dependencies (the API package comes in by path), so a PR that
# changes them also re-locks here, or the `mcp` CI job's `uv sync --locked` fails. Plain `uv lock`
# keeps every version it can; then the lock tests check the shared versions and new packages.
mcp-lock: ## Re-lock the MCP server after apps/api's dependencies change (reads PyPI)
	$(MCP) uv lock && uv run pytest -p no:cacheprovider tests/test_lock.py

check: lint typecheck coverage ## Everything CI checks, locally

# ── MCP server (M5, packages/mcp_server: its own uv project and lock, ADR 0016 decision 8) ──
mcp-check: ## MCP server: format check, lint, strict types and the tests that need no services
	$(MCP) uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest

mcp-test-integration: ## MCP server tests against the running local stack (run `make up` first)
	$(MCP) RUN_INTEGRATION=1 VECTAPILOT_DATABASE_URL="$(LOCAL_DB_URL)" \
		VECTAPILOT_MIGRATION_DATABASE_URL="$(LOCAL_OWNER_URL)" \
		VECTAPILOT_REDIS_URL="$(LOCAL_REDIS_URL)" uv run pytest

# ── LLM gateway (M1) ────────────────────────────────────────────────────────────────────
# Always the dry run: there is deliberately no make target for `--live` (shesha runs it by hand).
# Settings needs the two URLs like the app does; the CLI never connects to them.
llm-demo: ## Dry run of the LLM measurement CLI: fake provider, canned replies, no key, no cost
	$(API) VECTAPILOT_LLM_PROVIDER=fake VECTAPILOT_DATABASE_URL="$(LOCAL_DB_URL)" \
		VECTAPILOT_REDIS_URL="$(LOCAL_REDIS_URL)" uv run vectapilot-llm

# ── Eval v0 (M2 PR 7) ───────────────────────────────────────────────────────────────────
# Reads evals/datasets/support_qa/v0.1 and data/sample; no model, no network, writes nothing.
eval-check: ## Check the eval v0 dataset for its stage, then print its coverage counts
	$(API) uv run python -m vectapilot_api.evalset check && uv run python -m vectapilot_api.evalset report

# ── Eval harness (M2 PR 8) ──────────────────────────────────────────────────────────────
# Always a dry run: fake embedder, canned answers, $0, into the git-ignored evals/reports/tmp/.
# Needs `make up` (Postgres), H1 and H2 written, and questions in the dataset. There is
# deliberately no make target for `--live` (shesha runs it by hand: docs/learning/m2-eval-run.md).
eval-dry-run: ## Dry run of the eval harness on dev: fake embedder, canned answers, no cost
	$(API) VECTAPILOT_LLM_PROVIDER=fake VECTAPILOT_EMBEDDINGS_PROVIDER=fake \
		VECTAPILOT_DATABASE_URL="$(LOCAL_DB_URL)" VECTAPILOT_MIGRATION_DATABASE_URL="$(LOCAL_OWNER_URL)" \
		VECTAPILOT_REDIS_URL="$(LOCAL_REDIS_URL)" uv run python -m vectapilot_api.evalrun run --split dev

# ── Intents dataset (M4 PR 1) ───────────────────────────────────────────────────────────
# Reads evals/datasets/intents/v0.1 and the business manifests in data/sample; no model, no
# network, no database, writes nothing.
intent-check: ## Check the intents dataset for its stage, then print its counts
	$(API) uv run python -m vectapilot_api.intenteval check && uv run python -m vectapilot_api.intenteval report

# ── Intent router harness (M4 PR 4) ─────────────────────────────────────────────────────
# Always a dry run: fake embedder, canned fake routing answers, $0, into the git-ignored
# evals/reports/tmp/intents/. Needs a frozen dataset (eval rows and `freeze`), and H1 and H2
# written for the summaries and the hybrid. No database: Settings only needs the two URLs.
# There is deliberately no make target for `--live` or the test split (shesha runs them by hand:
# docs/plans/m4-pr-checks.md, PR 5).
intent-dry-run: ## Dry run of the intent harness on dev: embed, train, calibrate, thresholds, both modes, hybrid
	$(API) VECTAPILOT_LLM_PROVIDER=fake VECTAPILOT_EMBEDDINGS_PROVIDER=fake \
		VECTAPILOT_DATABASE_URL="$(LOCAL_DB_URL)" VECTAPILOT_REDIS_URL="$(LOCAL_REDIS_URL)" \
		uv run python -m vectapilot_api.intenteval dry-run

# ── Tool-call eval (M5 PR 6) ────────────────────────────────────────────────────────────
# Reads evals/datasets/tool_calls/v0.1, the business manifests in data/sample and the sandbox in
# data/sandbox; no model, no network, no database, writes nothing.
tool-check: ## Check the tool_calls dataset for its stage, then print its counts
	$(API) uv run python -m vectapilot_api.tooleval check && uv run python -m vectapilot_api.tooleval report

# Always a dry run: a scripted fake model reading fixture plans from apps/api/tests/, $0, into the
# git-ignored evals/reports/tmp/tool_calls/. Needs `make up` (the database at migration 0006), a
# frozen dataset, and H1 written (until then it stops after the first scenario and says so). It
# resets each business's sandbox as the owner. There is deliberately no make target for `--live`
# or the test split (shesha runs them by hand: docs/plans/m5-pr-checks.md, PR 7).
tool-dry-run: ## Dry run of the tool-call harness on dev: scripted fake model, no cost
	$(API) VECTAPILOT_LLM_PROVIDER=fake VECTAPILOT_EMBEDDINGS_PROVIDER=fake \
		VECTAPILOT_DATABASE_URL="$(LOCAL_DB_URL)" VECTAPILOT_MIGRATION_DATABASE_URL="$(LOCAL_OWNER_URL)" \
		VECTAPILOT_REDIS_URL="$(LOCAL_REDIS_URL)" uv run python -m vectapilot_api.tooleval run --split dev
