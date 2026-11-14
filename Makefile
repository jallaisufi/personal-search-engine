.PHONY: up up-scaled down logs ps rebuild reindex test test-unit lint fmt smoke smoke-scaled bench bench-report

SCALED = -f compose.yaml -f compose.scaled.yaml
LABEL ?= lite

up:            ## Start the lite stack
	docker compose up -d

up-scaled:     ## Start with API replicas, more workers and a Postgres read replica
	docker compose $(SCALED) up -d

down:          ## Stop everything (keeps data)
	docker compose $(SCALED) down

logs:          ## Follow api, scheduler and worker logs
	docker compose logs -f api scheduler worker

ps:
	docker compose ps

rebuild:       ## Rebuild backend image and restart
	docker compose up -d --build

reindex:       ## Drop the index (e.g. after changing EMBEDDING_MODEL) and rebuild it
	docker compose exec postgres psql -U app -d search -c \
	  "TRUNCATE sources, documents CASCADE; UPDATE index_state SET embedding_model = NULL;"
	docker compose exec redis redis-cli FLUSHDB
	docker compose restart scheduler worker

# A separate database, so tests never touch your real index.
TEST_DB ?= postgresql+asyncpg://app:app@localhost:5433/search_test
TEST_REDIS ?= redis://localhost:6380/1

test:          ## Run all backend tests against the compose Postgres (make up first)
	docker compose exec postgres createdb -U app search_test 2>/dev/null || true
	cd backend && DATABASE_URL=$(TEST_DB) EMBEDDING_PROVIDER=fake alembic upgrade head
	cd backend && DATABASE_URL=$(TEST_DB) REDIS_URL=$(TEST_REDIS) pytest

test-unit:     ## Run tests that need no database
	cd backend && pytest -m "not integration"

lint:
	cd backend && ruff check . && ruff format --check .

fmt:
	cd backend && ruff format . && ruff check --fix .

smoke:         ## End-to-end check against the running stack
	./scripts/smoke_test.sh

smoke-scaled:  ## Checks for scaled mode (run after `make up-scaled`)
	./scripts/smoke_scaled.sh

bench:         ## Load test the running stack: make bench LABEL=lite (or scaled). See bench/README.md
	docker compose --profile bench run --rm -e LABEL=$(LABEL) -e SCENARIO=cold -e VUS -e DURATION k6 run /bench/search.js
	docker compose --profile bench run --rm -e LABEL=$(LABEL) -e SCENARIO=warm -e VUS -e DURATION k6 run /bench/search.js

bench-report:  ## Markdown table of all load test results
	python3 bench/report.py
