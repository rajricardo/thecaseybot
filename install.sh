#!/bin/bash
set -e

# Get the directory of this script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

echo "Setting up theCaseyBot (macOS/Linux)..."

if [ ! -d "$DIR/venv" ]; then
    echo "Creating virtual environment (venv)..."
    python3 -m venv "$DIR/venv"
else
    echo "venv already exists, skipping creation."
fi

echo "Installing dependencies from requirements.txt..."
"$DIR/venv/bin/pip" install --upgrade pip
"$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"

if [ ! -f "$DIR/config.yaml" ]; then
    echo "Creating config.yaml from config.example.yaml — edit it with your real"
    echo "Discord/IBKR/Anthropic values before running the bot (see README.md)."
    cp "$DIR/config.example.yaml" "$DIR/config.yaml"
else
    echo "config.yaml already exists, leaving it as-is."
fi

echo ""
echo "Setup complete. Edit config.yaml, then run ./run.sh to start the bot."
