"""Make a copy of the app to give to someone else.

What must never travel: the `.env` file (the owner's paid API key), the photos, the listings
database, the owner's own description style, and the virtual environment (it is built for one
machine). What must: the code, the launchers, the Chrome extension, and the style template.

The same rules decide what a release may contain, so the updater (`updates.py`) refuses to write
any file into an app folder that a copy of the app could not have held.

The result is a zip of a few hundred KB with a plain-language START HERE.txt inside it, and a hard
check that nothing secret slipped in.
"""

from __future__ import annotations

import logging
import time
import zipfile
from datetime import date
from pathlib import Path, PurePath, PurePosixPath

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

# Folders that never travel: build output, secrets, the owner's own work.
SKIP_DIRS = {".git", ".venv", ".pytest_cache", "__pycache__", "product_image", "style_history",
             ".chrome-profile", "node_modules", ".idea", ".vscode", ".claude", "dist", "build"}
SKIP_SUFFIXES = {".db", ".pyc", ".log", ".zip"}
SKIP_NAMES = {".DS_Store", "Thumbs.db", "description_style.md"}      # the last one is the seller's own
# Written fresh into every zip. A copy of the app that came out of a zip already holds one, so the
# one in the folder is left out when packing - but a release may of course contain it.
GUIDE = "START HERE.txt"
SECRET_MARKERS = (".env", "credentials", "secret", ".key")       # checked again before writing

# Windows has no executable bit, so zipping there would strip it from every script - and a macOS
# app bundle whose executable is not executable cannot be launched at all (macOS reports it as
# needing Rosetta, which is nothing to do with the real problem). Set the modes explicitly.
EXECUTABLE_SUFFIXES = {".sh", ".command"}


def _mode_for(arcname: PurePosixPath) -> int:
    if "Contents/MacOS" in str(arcname.parent) or arcname.suffix in EXECUTABLE_SUFFIXES:
        return 0o755
    return 0o644


def _add(z: zipfile.ZipFile, arcname: PurePosixPath, data: bytes, mtime: float) -> None:
    info = zipfile.ZipInfo(str(arcname), date_time=time.localtime(mtime)[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3                                       # Unix, so the modes are honoured
    info.external_attr = (0o100000 | _mode_for(arcname)) << 16
    z.writestr(info, data)

START_HERE = """DEPOP SELLER - how to set it up on this computer
================================================

EASIEST ON WINDOWS: skip this folder and use the installer instead.
  Download DepopSellerSetup.exe from
  https://github.com/doyoungp-dev/depop_seller/releases/latest
  open it, click Install - it needs no Python and nothing else.

To set up this folder instead, you need, once:
  * Google Chrome
  * Python 3.12 or newer
      Windows - easiest: open the Microsoft Store, search "Python 3.13", click Get.
                or from https://www.python.org/downloads/windows/ - if you use the
                installer, tick "Add python.exe to PATH" on its FIRST screen.
      Mac     - https://www.python.org/downloads/ (the app offers to open this for
                you if it is missing). Default options are fine.
  * an Anthropic API key of your own, and your own Claude Pro or Max account
    (the app asks for both and explains what each one pays for)

SETTING IT UP - there is nothing to type

  1. Unzip this folder somewhere you will keep it, for example Documents.
     Do not work inside the zip itself.

  2. WINDOWS   double-click  "Depop Seller.cmd"  in the folder.
     MAC       double-click  "Depop Seller"      in the folder.

     The first time, it sets everything up - a minute or two - and then opens.
     On Windows it also puts a "Depop Seller" icon on your Desktop; use that
     from then on. On a Mac, drag the app to the Dock to keep it handy.

  3. If your Mac says the app "cannot be opened" or is from an unidentified
     developer, that is macOS being careful about anything downloaded rather
     than anything wrong with the app. Open System Settings -> Privacy &
     Security, scroll down, and click "Open Anyway" next to Depop Seller, then
     double-click it again. (Copying the folder from a USB stick or a shared
     drive instead of downloading it avoids this completely.)

THEN, IN THE APP
  Open the Settings tab and work down it:
    * Anthropic API key - paste yours, press Save, then Test.
      This pays for sorting photos into items, about $2.50 for 440 photos.
      Get one at console.anthropic.com -> API keys -> Create key, and add credit.
    * Claude sign-in - this needs the Claude desktop app, which carries the piece
      that writes descriptions. If Settings says it is not found, click
      "Get the Claude app", install it, open it once, then press Reload here.
      Then press "Sign in with Claude" and finish signing in in your browser.
      Descriptions run on your own Claude subscription (Pro or Max). No charge.
    * Chrome helper - follow the three steps to add it to Chrome. It puts your
      photos into Depop's form for you.

HOW YOU USE IT
  Batches tab  - make a batch, drag your photos onto it, press "Group with Claude".
  Review tab   - check the grouping, drag photos around, Save.
  Sell tab     - write the description, then "Depop" to open the listing with your
                 photos already in it. You always press Post yourself.
  Style tab    - change how descriptions are written, in your own words.

TO CLOSE IT
  Close the app window - that closes the app. (It waits if it is still busy,
  for example writing a description.) Settings -> "Close Depop Seller" also works.

UPDATING
  Settings tab -> "Check for updates". If there is a newer version, "Update"
  installs it and the app reopens by itself. Your photos, listings, style and
  key are never touched by an update.

Everything stays on this computer. Nothing is posted for you, and nothing is
uploaded except the photos you choose to group.
"""


def is_shareable(rel: PurePath) -> bool:
    """Whether a file at this path inside the app folder belongs in a copy of the app."""
    name = rel.name
    if name in SKIP_NAMES or PurePosixPath(name).suffix.lower() in SKIP_SUFFIXES or name.startswith(".env"):
        return False
    return not any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts)


def _included(path: Path) -> bool:
    rel = path.relative_to(PROJECT_ROOT)
    return is_shareable(rel) and rel.as_posix() != GUIDE


def files_to_share() -> list[Path]:
    return sorted(p for p in PROJECT_ROOT.rglob("*") if p.is_file() and _included(p))


def default_destination() -> Path:
    desktop = Path.home() / "Desktop"
    folder = desktop if desktop.is_dir() else Path.home()
    return folder / f"Depop Seller {date.today():%Y-%m-%d}.zip"


def make_share_zip(destination: Path | None = None, files: list[Path] | None = None) -> dict:
    """Write a zip of the app for someone else, and refuse to include anything secret. `files`
    narrows it to a given list - a release passes the files git tracks, so nothing that merely
    sits in the folder can end up in a download."""
    destination = Path(destination) if destination else default_destination()
    chosen = sorted(f for f in files if f.is_file() and _included(f)) if files is not None else files_to_share()
    for f in chosen:                                   # belt and braces before anything is written
        low = f.name.lower()
        if any(m in low for m in SECRET_MARKERS):
            raise RuntimeError(f"refusing to share: {f.name} looks like it holds a secret")
    destination.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as z:
        for f in chosen:
            arc = PurePosixPath("depop_seller_app") / f.relative_to(PROJECT_ROOT).as_posix()
            _add(z, arc, f.read_bytes(), f.stat().st_mtime)
        _add(z, PurePosixPath("depop_seller_app") / GUIDE, START_HERE.encode("utf-8"), now)
    size = destination.stat().st_size
    log.info("share copy written: %s (%d files, %.0f KB)", destination, len(chosen) + 1, size / 1024)
    return {"path": str(destination), "files": len(chosen) + 1, "kb": round(size / 1024)}
