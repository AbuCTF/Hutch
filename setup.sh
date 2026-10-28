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

echo "[1/4] installing hutch + dependencies"
pip install -e ".[dev,mcp]" -q

echo "[2/4] installing chromium (full build; the headless-only shell is not used)"
python3 -m playwright install chromium --no-shell

echo "[3/4] installing system libraries for chromium"
if [ "$(uname)" = "Linux" ]; then
    if command -v apt-get &>/dev/null; then
        PW="$(command -v playwright || echo "python3 -m playwright")"
        if [ "$(id -u)" -eq 0 ]; then
            $PW install-deps chromium
        else
            sudo $PW install-deps chromium
        fi
    else
        echo "  not a Debian/Ubuntu system — install chromium's libraries (nss, atk, gbm, xkbcommon, fonts)"
        echo "  with your package manager; 'hutch doctor' lists anything missing"
    fi
fi

echo "[4/4] checking"
hutch doctor || true
