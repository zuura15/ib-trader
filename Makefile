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

# `make dev` auto-detects paper vs live from the running Gateway. Pass
# FORCE_MODE=paper or FORCE_MODE=live to assert and fail fast on mismatch.
# API and bots processes connect via engine's internal API and inherit mode.
IB_MODE_FLAG := $(if $(FORCE_MODE),--force-mode $(FORCE_MODE),)

# Ports owned by `make dev`. Stale binders (orphaned processes after a hard
# kill) get reaped before we restart so 8081 doesn't EADDRINUSE on relaunch.
DEV_PORTS := 8000 8081 8082 5173

dev:
	@echo "Starting all services (auto-detect $(if $(FORCE_MODE),forced=$(FORCE_MODE),mode))... (Ctrl+C to stop all)"
	@mkdir -p run/redis-data logs
	@for port in $(DEV_PORTS); do \
		pids=$$(lsof -ti tcp:$$port -sTCP:LISTEN 2>/dev/null); \
		if [ -n "$$pids" ]; then \
			echo "[DEV] Port $$port in use by PID(s) $$pids — killing."; \
			kill $$pids 2>/dev/null || true; \
			sleep 0.3; \
			pids=$$(lsof -ti tcp:$$port -sTCP:LISTEN 2>/dev/null); \
			if [ -n "$$pids" ]; then kill -9 $$pids 2>/dev/null || true; fi; \
		fi; \
	done
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
