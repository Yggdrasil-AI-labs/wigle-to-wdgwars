#!/usr/bin/env bash
# Double-click (or run) this file to update wigle-to-wdgwars (refreshes
# deps + script).
#
# Refresh order: pull requirements.txt first (it may have grown a new
# dep since this clone), install deps, THEN pull the script. That way
# the newly-pulled script can import every dep on the first run.

set -e
cd "$(dirname "$0")"

if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
    echo "wigle-to-wdgwars requires Python 3.10 or newer. Your current python3 is:"
    python3 --version 2>/dev/null || echo "  (not found on PATH)"
    echo
    echo "Install Python 3.10+ from your package manager or https://python.org/downloads/ and re-run."
    exit 1
fi

VENV_DIR=".venv"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "No $VENV_DIR/ found — creating it (running setup.sh once is the usual way)..."
    if ! python3 -m venv "$VENV_DIR" 2>/dev/null; then
        echo
        echo "Failed to create venv. On Debian/Ubuntu/Raspberry Pi OS install:"
        echo "  sudo apt install -y python3-venv python3-full"
        echo "Then re-run ./update.sh."
        exit 1
    fi
fi
VENV_PY="$VENV_DIR/bin/python"

echo "[1/3] Refreshing requirements.txt from GitHub..."
"$VENV_PY" -c "import urllib.request as u; u.urlretrieve('https://raw.githubusercontent.com/Yggdrasil-AI-labs/wigle-to-wdgwars/main/requirements.txt', 'requirements.txt')"

echo
echo "[2/3] Installing/refreshing dependencies..."
"$VENV_PY" -m pip install --upgrade -r requirements.txt

echo
echo "[3/3] Refreshing wigle_to_wdgwars.py from GitHub..."
"$VENV_PY" -c "import urllib.request as u; u.urlretrieve('https://raw.githubusercontent.com/Yggdrasil-AI-labs/wigle-to-wdgwars/main/wigle_to_wdgwars.py', 'wigle_to_wdgwars.py')"
"$VENV_PY" wigle_to_wdgwars.py --version

if [ -t 0 ]; then
    echo
    read -n 1 -s -r -p "Press any key to close..."
    echo
fi
