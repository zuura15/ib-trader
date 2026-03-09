.PHONY: install test smoke docs lint typecheck clean

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

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
	find . -name "*.pyo" -delete
