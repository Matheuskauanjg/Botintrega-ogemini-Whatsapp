#!/bin/sh
exec /opt/venv/bin/python -m uvicorn bridge_auto:app --host 127.0.0.1 --port "${BRIDGE_INTERNAL_PORT:-10001}"
