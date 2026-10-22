SHELL := /bin/bash
PY := backend/.venv/bin/python
PIP := backend/.venv/bin/pip

.PHONY: help setup api worker ui test test-ui test-all demo docker-up docker-down build-ui clean share

help:
	@echo "setup       create the venv and install backend + frontend deps"
	@echo "api         run the API on :8000 (inline processing, SQLite)"
	@echo "worker      run the background worker (use with PROCESS_INLINE=0)"
	@echo "ui          run the Vite dev server on :5173"
	@echo "build-ui    build the SPA so FastAPI serves it on :8000"
	@echo "demo        generate demo audio and ingest it into a running API"
	@echo "test        run the backend test suite"
	@echo "test-ui     run the frontend test suite (vitest)"
	@echo "test-all    run both suites — the pre-push gate"
	@echo "share       expose the running API on a free public URL (cloudflared)"
	@echo "docker-up   docker compose up --build"
	@echo "clean       remove venv, node_modules, build output and demo data"

setup:
	python3 -m venv backend/.venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r backend/requirements-dev.txt
	cd frontend && npm install --no-audit --no-fund

api:
	cd backend && DATA_DIR=../data PROCESS_INLINE=1 ./.venv/bin/uvicorn app.main:app --reload --port 8000

worker:
	cd backend && DATA_DIR=../data PROCESS_INLINE=0 ./.venv/bin/python -m app.worker

ui:
	cd frontend && npm run dev

build-ui:
	cd frontend && npm run build

demo:
	cd backend && ./.venv/bin/python scripts/make_demo_data.py --upload http://127.0.0.1:8000

test:
	cd backend && ./.venv/bin/python -m pytest -q

# The frontend had no tests before the AI-UX slice; `tsc` + `vite build` only
# prove it compiles, not that a mode chip changes the request or that a semantic
# hit carries its warning. Runs in jsdom against a mocked API.
test-ui:
	cd frontend && npm test

# One command for "is this safe to push": both suites, backend first because it
# is the slower one and a backend regression outranks a UI detail.
test-all: test test-ui

# Public demo link. Keeps the real pipeline (ffmpeg + ASR + denoiser) because it
# tunnels the machine already running it — no free PaaS can host those.
share:
	./scripts/share_demo.sh

docker-up:
	docker compose up --build

docker-down:
	docker compose down

clean:
	rm -rf backend/.venv frontend/node_modules frontend/dist
	rm -rf data/app.db data/app.db-* data/originals/* data/working/* data/denoised/* data/exports/*
	find backend -name __pycache__ -type d -exec rm -rf {} +
