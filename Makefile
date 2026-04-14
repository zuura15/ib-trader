.PHONY: install test smoke docs lint typecheck clean dev e2e-live e2e-live-keep

install:
	uv sync

test:
	uv run pytest tests/unit tests/integration -v --cov=ib_trader

smoke:
	uv run pytest tests/smoke -v -m smoke

docs:
	uv run mkdocs serve

lint:
	uv run ruff check .

typecheck:
	uv run mypy ib_trader/

dev:
	@echo "Starting all services... (Ctrl+C to stop all)"
	@mkdir -p run/redis-data logs
	@if .local/bin/redis-cli ping >/dev/null 2>&1; then \
		echo "[DEV] Redis already running."; \
	else \
		echo "[DEV] Starting Redis..."; \
		.local/bin/redis-server config/redis.conf --daemonize yes; \
		sleep 0.5; \
	fi
	@trap 'trap "" INT TERM; .local/bin/redis-cli shutdown nosave >/dev/null 2>&1; kill -TERM 0; wait; exit 0' INT TERM; \
	uv run ib-engine & \
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
