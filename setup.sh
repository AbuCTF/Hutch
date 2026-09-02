#!/usr/bin/env bash
set -euo pipefail

echo "hutch — setup"
echo

if ! command -v python3 &>/dev/null; then
    echo "error: python3 not found"
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJ=$(echo "$PY_VER" | cut -d. -f1)
PY_MIN=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJ" -lt 3 ] || { [ "$PY_MAJ" -eq 3 ] && [ "$PY_MIN" -lt 10 ]; }; then
    echo "error: python 3.10+ required (found $PY_VER)"
    exit 1
fi

echo "[1/3] installing hutch + dependencies"
pip install -e ".[dev]" -q

echo "[2/3] installing chromium browser"
python3 -m playwright install chromium

echo "[3/3] installing system dependencies for chromium"
python3 -m playwright install-deps chromium 2>/dev/null || true

echo
echo "done. run 'hutch --version' to verify."
