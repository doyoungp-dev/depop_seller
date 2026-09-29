#!/bin/bash
# One-time sign-in of the Claude Code command line (the Draft button uses it).
# A prompt opens: type /login , press Enter, finish the sign-in in the browser, then type /exit .
set -eu
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "The virtual environment is missing - run ./setup.sh first."
    exit 1
fi

exec .venv/bin/python -m depop_seller claude-login
