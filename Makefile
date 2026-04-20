.PHONY: install test smoke docs lint lint-ruff lint-imports lint-types lint-secrets typecheck clean dev e2e-live e2e-live-keep

install:
	uv sync

test:
	uv run pytest tests/unit tests/integration -v --cov=ib_trader

smoke:
	uv run pytest tests/smoke -v -m smoke

docs:
	uv run mkdocs serve

lint: lint-ruff lint-imports lint-types lint-secrets

lint-ruff:
	@echo "==> ruff (functional rules)"
	uv run ruff check .

lint-imports:
	@echo "==> import-linter (architectural contracts)"
	uv run lint-imports

lint-types:
	@echo "==> mypy (strict on hot-path modules)"
	uv run python -m mypy ib_trader/

lint-secrets:
	@echo "==> detect-secrets (baseline diff)"
	uv run python -m detect_secrets scan --baseline .secrets.baseline \
		--exclude-files '\.venv/|node_modules/|\.git/|frontend/dist/|\.db$$|uv\.lock$$|package-lock\.json$$|logs/'

typecheck: lint-types

# Default `make dev` targets the LIVE IB Gateway (port 4001, IB_ACCOUNT_ID).
# Pass PAPER=1 to flip the engine onto the paper Gateway (port 4002,
# IB_ACCOUNT_ID_PAPER). Only ib-engine takes the --paper/--live flag; the
# API and bots processes connect via engine's internal API and inherit mode.
IB_MODE_FLAG := $(if $(PAPER),--paper,--live)

dev:
	@echo "Starting all services in $(if $(PAPER),PAPER,LIVE) mode... (Ctrl+C to stop all)"
	@mkdir -p run/redis-data logs
	@if .local/bin/redis-cli ping >/dev/null 2>&1; then \
		echo "[DEV] Redis already running."; \
	else \
		echo "[DEV] Starting Redis..."; \
		.local/bin/redis-server config/redis.conf --daemonize yes; \
		sleep 0.5; \
	fi
	@trap 'trap "" INT TERM; .local/bin/redis-cli shutdown nosave >/dev/null 2>&1; kill -TERM 0; wait; exit 0' INT TERM; \
	uv run ib-engine $(IB_MODE_FLAG) & \
	uv run ib-api & \
	uv run ib-bots & \
	(cd frontend && VITE_DATA_MODE=live npm run dev) & \
	wait

e2e-live:
	./scripts/e2e-live.sh

e2e-live-keep:
	IB_TRADER_E2E_KEEP_RUNNING=1 ./scripts/e2e-live.sh

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
	find . -name "*.pyo" -delete
