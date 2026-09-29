#!/bin/bash
# Start the app from a terminal (macOS, Linux). On macOS you can double-click
# "Depop Seller.app" instead. Ctrl+C stops the server.
#
#   ./depop-seller.sh            the Batches page (hub)
#   ./depop-seller.sh review     the review page for the newest batch
#   ./depop-seller.sh sell       the Sell page for the newest batch
set -eu
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "The virtual environment is missing - run ./setup.sh first."
    exit 1
fi

exec .venv/bin/python -m depop_seller "${1:-hub}" "${@:2}"
