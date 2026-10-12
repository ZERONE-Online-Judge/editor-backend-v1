#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export EDITOR_ENV=development EDITOR_MAIL_MODE=console
# Standalone fixtures only. Shared-login development runs both services.
export EDITOR_AUTH_MODE=local
export EDITOR_DATABASE_URL=sqlite:///./data/preview/editor.db
export EDITOR_DATA_DIR=./data/preview EDITOR_WORK_DIR=./data/preview/work
export EDITOR_PUBLIC_ORIGIN=http://127.0.0.1:5190
.venv/bin/python -m app.demo
pids=()
cleanup() { for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8011 & pids+=("$!")
.venv/bin/python -m app.worker & pids+=("$!")
(cd ../editor_frontend_v1 && npm run dev) & pids+=("$!")
wait
