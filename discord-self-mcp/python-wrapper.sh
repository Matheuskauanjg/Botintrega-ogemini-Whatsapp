#!/bin/sh
# Live transcription is consent-gated in bridge_conversation.py; non-consenting users are filtered before PCM decoding.
exec /opt/venv/bin/python -m uvicorn bridge_conversation:app --host 127.0.0.1 --port "${BRIDGE_INTERNAL_PORT:-10001}"
