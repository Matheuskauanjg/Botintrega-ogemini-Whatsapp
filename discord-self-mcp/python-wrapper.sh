#!/bin/sh
exec /opt/venv/bin/python -m uvicorn bridge_chain:app --host 127.0.0.1 --port "${BRIDGE_INTERNAL_PORT:-10001}"
