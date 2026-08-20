#!/bin/bash
# Double-click (macOS) or run from a terminal to open the Agent Lab in a browser.
# Everything stays on this machine: the server binds loopback only.
cd "$(dirname "$0")/standalone" || exit 1

PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || {
  echo "No Python found. Install Python 3, then try again."; read -r; exit 1; }

if ! "$PY" -c "import requests, pptx, openpyxl" >/dev/null 2>&1; then
  echo "Installing the agent's Python packages (one time)..."
  "$PY" -m pip install --quiet -r ../requirements.txt || {
    echo; echo "Install failed. Run:  $PY -m pip install -r requirements.txt"
    read -r; exit 1; }
fi

# Optional: gives Agent Lab its own window instead of a browser tab. Not fatal
# if it fails — webui.app falls back to the browser.
if ! "$PY" -c "import webview" >/dev/null 2>&1; then
  echo "Installing pywebview for the app window (one time, optional)..."
  "$PY" -m pip install --quiet pywebview >/dev/null 2>&1 || true
fi

# The lab talks to a local Ollama on 11434.
if ! curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "Starting Ollama..."
  (ollama serve >/dev/null 2>&1 &) 2>/dev/null
  sleep 2
fi

# webui.app opens a native window when pywebview is installed, and falls back to
# the browser when it is not. Either way the server is the same loopback server.
exec "$PY" -m webui.app
