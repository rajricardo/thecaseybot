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

# Check if the virtual environment exists, create if missing
if [ ! -d "$DIR/venv" ]; then
    echo "Virtual environment (venv) not found. Creating it..."
    python3 -m venv "$DIR/venv"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to create virtual environment using python3 -m venv."
        exit 1
    fi
fi

# Check if dependencies are installed, install requirements if missing
if ! "$DIR/venv/bin/python" -c "import discord, yaml, ib_async, anthropic, flask, ruamel.yaml" &> /dev/null; then
    echo "Dependencies missing. Installing requirements from requirements.txt..."
    if [ -f "$DIR/requirements.txt" ]; then
        "$DIR/venv/bin/pip" install --upgrade pip
        "$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"
        if [ $? -ne 0 ]; then
            echo "Error: Failed to install requirements."
            exit 1
        fi
    else
        echo "Error: requirements.txt not found. Cannot install dependencies."
        exit 1
    fi
fi

# config.yaml holds real secrets (Discord token, IBKR settings) and is
# gitignored/untracked on purpose — fail early with a clear message rather
# than letting bot.py crash on a missing key deep in main().
if [ ! -f "$DIR/config.yaml" ]; then
    echo "Error: config.yaml not found. Copy config.example.yaml to config.yaml and fill in real values first."
    exit 1
fi

echo "Starting bot.py... Casey Bridge UI will be at http://127.0.0.1:8787"
exec "$DIR/venv/bin/python" "$DIR/bot.py"
