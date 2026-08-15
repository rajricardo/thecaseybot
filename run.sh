#!/bin/bash

# Get the directory of this script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Kill any leftover bot.py from a previous run (or a manually-started one)
# before spawning a new one — a stale process still holding the IBKR
# clientId (config.yaml's ibkr.client_id) would make the new connection
# fail, and a stale Discord session would double-process every message.
echo "Stopping any leftover bot.py from a previous run..."
pkill -f "$DIR/bot.py" 2>/dev/null
sleep 1
pkill -9 -f "$DIR/bot.py" 2>/dev/null

# Setup (venv + dependencies) lives in install.sh now, not here — run that
# first if either of these is missing.
if [ ! -d "$DIR/venv" ]; then
    echo "Error: venv not found. Run ./install.sh first."
    exit 1
fi

# config.yaml holds real secrets (Discord token, IBKR settings) and is
# gitignored/untracked on purpose — fail early with a clear message rather
# than letting bot.py crash on a missing key deep in main().
if [ ! -f "$DIR/config.yaml" ]; then
    echo "Error: config.yaml not found. Run ./install.sh first, then fill in real values."
    exit 1
fi

echo "Starting bot.py... Casey Bridge UI will be at http://127.0.0.1:8787"
exec "$DIR/venv/bin/python" "$DIR/bot.py"
