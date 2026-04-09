.PHONY: install test smoke docs lint typecheck clean dev

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
	@trap 'trap "" INT TERM; kill -TERM 0; wait; exit 0' INT TERM; \
	uv run ib-engine & \
	uv run ib-api & \
	(cd frontend && VITE_DATA_MODE=live npm run dev) & \
	wait

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
	find . -name "*.pyo" -delete
