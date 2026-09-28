#!/usr/bin/env sh
# Start the API with auto-reload on http://localhost:8000 (docs at /docs).
cd "$(dirname "$0")"
if [ -x .venv/Scripts/python ]; then PY=.venv/Scripts/python; else PY=.venv/bin/python; fi
exec "$PY" -m uvicorn backend.main:app --reload --port 8000 "$@"
