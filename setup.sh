#!/bin/bash
# One-time setup on macOS or Linux: create the virtual environment, install the project, and
# build the app icon. Run it from Terminal inside the project folder:   ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n== %s\n' "$1"; }

# ---------------------------------------------------------------- Python
PY=""
for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
            PY="$candidate"
            break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.12 or newer is required."
    echo "macOS:  brew install python@3.13     (or download it from python.org)"
    echo "Linux:  use your package manager, e.g. sudo apt install python3.13 python3.13-venv"
    exit 1
fi
say "using $($PY --version) at $(command -v "$PY")"

say "creating .venv"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
say "installing depop_seller and its dependencies"
.venv/bin/python -m pip install --quiet -e .

# ---------------------------------------------------------------- macOS app bundle
if [ "$(uname)" = "Darwin" ]; then
    APP="Depop Seller.app"
    chmod +x "$APP/Contents/MacOS/depop-seller"
    xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true   # in case it arrived as a ZIP
    # remember where the project is, so a copy of the app on the Desktop or in /Applications works
    SUPPORT="$HOME/Library/Application Support/Depop Seller"
    mkdir -p "$SUPPORT"
    pwd > "$SUPPORT/project-path"
    if command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
        say "building the app icon"
        ICONSET="$(mktemp -d)/AppIcon.iconset"
        mkdir -p "$ICONSET"
        for size in 16 32 64 128 256 512; do
            sips -z $size $size depop_seller/static/icon.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
            sips -z $((size * 2)) $((size * 2)) depop_seller/static/icon.png \
                 --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
        done
        mkdir -p "$APP/Contents/Resources"
        iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
        touch "$APP"                      # make Finder notice the new icon
    fi
fi

cat <<'DONE'

== Setup finished.

Open the app - on a Mac, double-click "Depop Seller" (drag it to the Dock to keep it
there); on Linux, ./depop-seller.sh - and finish in its Settings tab:
  * your Anthropic API key   (pays for sorting photos into items)
  * the Claude sign-in       (writes descriptions, on your own subscription)
  * the Chrome helper        (puts the photos into Depop's form)

To stop the app: Settings -> Close Depop Seller, or Quit it in the Dock.
DONE
