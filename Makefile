# VectaPilot developer commands. Run `make` (or `make help`) to list them.

COMPOSE := docker compose -f infra/compose.yaml
API     := cd apps/api &&
# The API's role and the owner's (ADR 0009); the passwords default like infra/compose.yaml's.
LOCAL_DB_URL    := postgresql+asyncpg://vectapilot_app:$${VECTAPILOT_APP_DB_PASSWORD:-vectapilot-app-dev}@127.0.0.1:5432/vectapilot
LOCAL_OWNER_URL := postgresql+asyncpg://vectapilot:$${POSTGRES_PASSWORD:-vectapilot-dev}@127.0.0.1:5432/vectapilot
LOCAL_REDIS_URL := redis://127.0.0.1:6379/0

.DEFAULT_GOAL := help
.PHONY: help up down logs ps migrate reset-db lint format typecheck test coverage test-integration check llm-demo eval-check

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

check: lint typecheck coverage ## Everything CI checks, locally

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
