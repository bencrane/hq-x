.PHONY: install dev test lint format docker-build docker-run

install:
	uv sync

dev:
	doppler run --project hq-x --config dev -- uvicorn app.main:app --reload --port 8000

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

docker-build:
	docker build -t hq-x:local .

docker-run:
	docker run --rm -p 8000:8000 -e DOPPLER_TOKEN=$$DOPPLER_TOKEN -e APP_ENV=dev hq-x:local
