.PHONY: up down logs ps rebuild reindex test test-unit lint fmt smoke

up:            ## Start the lite stack
	docker compose up -d

down:          ## Stop everything (keeps data)
	docker compose down

logs:          ## Follow api + worker logs
	docker compose logs -f api worker

ps:
	docker compose ps

rebuild:       ## Rebuild backend image and restart
	docker compose up -d --build

reindex:       ## Drop the index (e.g. after changing EMBEDDING_MODEL) and rebuild it
	docker compose exec postgres psql -U app -d search -c \
	  "TRUNCATE sources, documents CASCADE; UPDATE index_state SET embedding_model = NULL;"
	docker compose restart worker

# A separate database, so tests never touch your real index.
TEST_DB ?= postgresql+asyncpg://app:app@localhost:5433/search_test

test:          ## Run all backend tests against the compose Postgres (make up first)
	docker compose exec postgres createdb -U app search_test 2>/dev/null || true
	cd backend && DATABASE_URL=$(TEST_DB) EMBEDDING_PROVIDER=fake alembic upgrade head
	cd backend && DATABASE_URL=$(TEST_DB) pytest

test-unit:     ## Run tests that need no database
	cd backend && pytest -m "not integration"

lint:
	cd backend && ruff check . && ruff format --check .

fmt:
	cd backend && ruff format . && ruff check --fix .

smoke:         ## End-to-end check against the running stack
	./scripts/smoke_test.sh
