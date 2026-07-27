# Convenience targets for the three parallel environments.
#
#   production  Real Picnic API, persistent volume, production Telegram bot.
#               docker-compose.yml            (project: picnic-like-a-pro)
#   mock        Mocked Picnic API, persistent volume, separate bot token.
#               docker-compose.mock.yml       (project: picnic-mock)
#   test        Mocked Picnic API, ephemeral volume, pytest + coverage.
#               docker-compose.test.yml       (project: picnic-test)
#
# All three use distinct compose project names, container names, and volumes,
# so they can run side by side on the same host.

.PHONY: help install test test-fast coverage open-coverage wipe-test \
        test-docker test-docker-wipe \
        prod-up prod-down prod-logs prod-pull \
        mock-up mock-down mock-logs mock-seed mock-reset \
        status

help:
	@echo ""
	@echo "  TESTING (local, fastest)"
	@echo "    make install         Install dependencies including the dev group"
	@echo "    make test            Run the suite with terminal + HTML coverage"
	@echo "    make test-fast       Run the suite without coverage"
	@echo "    make coverage        Coverage summary only"
	@echo "    make open-coverage   Open the HTML report in a browser"
	@echo "    make wipe-test       Delete the test DB and coverage artefacts"
	@echo ""
	@echo "  TESTING (Docker, matches CI)"
	@echo "    make test-docker     Build the test stage and run the suite"
	@echo "    make test-docker-wipe  Tear down test containers and volumes"
	@echo ""
	@echo "  PRODUCTION"
	@echo "    make prod-up / prod-down / prod-logs / prod-pull"
	@echo ""
	@echo "  MOCK (persistent playground)"
	@echo "    make mock-up / mock-down / mock-logs"
	@echo "    make mock-seed       Import mock delivery history into the mock DB"
	@echo "    make mock-reset      Destroy and recreate the mock volume"
	@echo ""
	@echo "    make status          Show containers for all three environments"
	@echo ""

# ---------------------------------------------------------------------------
# Local testing
# ---------------------------------------------------------------------------

install:
	uv sync --group dev

test: install
	uv run pytest --cov=picnic_meal_planner \
	              --cov-report=term-missing \
	              --cov-report=html

test-fast: install
	uv run pytest

coverage: install
	uv run pytest --cov=picnic_meal_planner --cov-report=term-missing -q

open-coverage: test
	@python -m webbrowser htmlcov/index.html 2>/dev/null \
	    || open htmlcov/index.html 2>/dev/null \
	    || echo "Open htmlcov/index.html in your browser"

wipe-test:
	rm -f data/picnic-test.db .coverage
	rm -rf htmlcov/ .pytest_cache/
	@echo "Test artefacts wiped."

# ---------------------------------------------------------------------------
# Dockerised testing
# ---------------------------------------------------------------------------

test-docker:
	docker compose -f docker-compose.test.yml run --rm picnic-test

test-docker-wipe:
	docker compose -f docker-compose.test.yml down -v
	@echo "Test environment wiped."

# ---------------------------------------------------------------------------
# Production
# ---------------------------------------------------------------------------

prod-up:
	docker compose up -d

prod-down:
	docker compose down

prod-logs:
	docker compose logs -f picnic-bot

prod-pull:
	docker compose pull && docker compose up -d && docker image prune -f

# ---------------------------------------------------------------------------
# Mock playground
# ---------------------------------------------------------------------------

mock-up:
	docker compose -f docker-compose.mock.yml up -d

mock-down:
	docker compose -f docker-compose.mock.yml down

mock-logs:
	docker compose -f docker-compose.mock.yml logs -f picnic-bot-mock

mock-seed:
	docker compose -f docker-compose.mock.yml run --rm picnic-bot-mock \
	    python scripts/import_history.py

mock-reset:
	docker compose -f docker-compose.mock.yml down -v
	docker compose -f docker-compose.mock.yml up -d
	@echo "Mock environment reset."

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

status:
	@docker ps --filter name=picnic --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
