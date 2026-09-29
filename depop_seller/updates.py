"""Check for a newer release and install it.

Releases are published on GitHub as `DepopSeller.zip`. Checking asks GitHub's public API for the
latest release - only when the user presses the button, never in the background. Installing
replaces files in the **app folder** only; the data folder (photos, listings, style, key) is never
touched, which is what makes an update safe to apply at all.

An update is validated before a single file is replaced: it must come from this project's own
release downloads, be a zip in the shape `share.make_share_zip` produces, say which version it is,
and hold only files a copy of the app may hold (`share.is_shareable`), none of which may land
outside the app folder. Only files whose content changed are written, and each one is copied
aside first; if the new version cannot be made to run, the old files are put back.

The updater adds and replaces files but never deletes one: a release that drops a file must still
work with the old copy lying around.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from . import __version__, config, share
from .desktop import no_console

log = logging.getLogger(__name__)

REPO = "doyoungp-dev/depop_seller"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
ASSET_NAME = "DepopSeller.zip"
INSTALLER_NAME = "DepopSellerSetup.exe"    # the Windows installer, attached to the same release
ZIP_ROOT = "depop_seller_app"              # the one folder inside the zip (see share.make_share_zip)
MAX_DOWNLOAD = 50 * 1024 * 1024            # a release is ~200 KB; anything near this is wrong
TIMEOUT_S = 20
RESTART_WAIT_S = 30                        # how long the new instance waits for the old one's port


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.10.2' -> (1, 10, 2). Compared as numbers, so 1.10 is newer than 1.9."""
    numbers = re.findall(r"\d+", text or "")
    if not numbers:
        raise ValueError(f"not a version: {text!r}")
    return tuple(int(n) for n in numbers[:3])


def managed_by_git(root: Path | None = None) -> bool:
    """A git checkout is updated with git pull; overwriting it from a zip would make a mess of it."""
    return ((root or config.PROJECT_ROOT) / ".git").exists()


def can_restart() -> bool:
    """Windows starts the new version itself. The macOS app is a script that owns the server
    process, so there the app closes and the user opens it again."""
    return os.name == "nt"


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"DepopSeller/{__version__}",       # GitHub refuses requests without one
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def check() -> dict:
    """Is there a newer release? Never raises: the answer is always something to show the user."""
    current = __version__
    try:
        release = _get_json(LATEST_API)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "current": current, "reason": "No version has been published yet."}
        if e.code in (403, 429):
            return {"ok": False, "current": current, "reason": "GitHub is limiting checks right now - try again in an hour."}
        return {"ok": False, "current": current, "reason": f"GitHub answered with an error ({e.code}) - try again later."}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"ok": False, "current": current, "reason": "Could not reach GitHub - check the internet connection."}
    except ValueError:
        return {"ok": False, "current": current, "reason": "GitHub sent something unreadable - try again later."}

    latest = str(release.get("tag_name") or "").lstrip("vV")
    assets = {a.get("name"): str(a.get("browser_download_url") or "") for a in release.get("assets") or []}
    download = assets.get(ASSET_NAME, "")
    try:
        newer = parse_version(latest) > parse_version(current)
    except ValueError:
        return {"ok": False, "current": current, "reason": "The newest release has no version number."}

    why_not = None
    if managed_by_git():
        why_not = "This copy of the app is a git checkout - update it with git pull instead."
    elif not download.startswith(DOWNLOAD_PREFIX):
        why_not = f"That release has no {ASSET_NAME} to install - download it from the releases page instead."
    return {
        "ok": True,
        "current": current,
        "latest": latest,
        "newer": newer,
        "published": str(release.get("published_at") or "")[:10],
        "notes": str(release.get("body") or "").strip()[:4000],
        "page": str(release.get("html_url") or RELEASES_PAGE),
        "download": download,
        "installer": assets.get(INSTALLER_NAME, ""),
        "can_install": newer and why_not is None,
        "why_not": why_not if newer else None,
    }


# ---- reading an update --------------------------------------------------------------------

def read_update(zip_path: Path) -> tuple[str, list[tuple[str, PurePosixPath]]]:
    """Validate an update zip. Returns its version and (zip member, path inside the app) pairs."""
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise ValueError("the download is not a zip file") from e
    with archive:
        names = archive.namelist()
        files = []
        for member in names:
            path = PurePosixPath(member)
            if (path.is_absolute() or ".." in path.parts or ":" in member or chr(92) in member
                    or not path.parts or path.parts[0] != ZIP_ROOT):
                raise ValueError(f"unexpected path in the update: {member!r}")
            if member.endswith("/") or len(path.parts) < 2:
                continue                                           # a folder entry
            inside = PurePosixPath(*path.parts[1:])
            if not share.is_shareable(inside):
                raise ValueError(f"the update carries a file an app never ships: {inside}")
            files.append((member, inside))
        init = f"{ZIP_ROOT}/depop_seller/__init__.py"
        if init not in names:
            raise ValueError("the zip is not a Depop Seller release")
        found = re.search(r'__version__\s*=\s*"([^"]+)"', archive.read(init).decode("utf-8", "replace"))
        if not found:
            raise ValueError("the release does not say which version it is")
        return found.group(1), files


def _dependencies(pyproject: bytes | None) -> list[str]:
    if not pyproject:
        return []
    try:
        return sorted(tomllib.loads(pyproject.decode("utf-8")).get("project", {}).get("dependencies", []))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return ["unreadable"]                                      # treat as changed: reinstall


# ---- installing ---------------------------------------------------------------------------

def backup_dir() -> Path:
    """Where replaced files are kept: this machine's app-data folder, not the data folder - that
    one travels between computers, and old app files have no business travelling with it."""
    return config.machine_config_file().parent / "previous-version"


def _download(url: str, dest: Path) -> None:
    if not url.startswith(DOWNLOAD_PREFIX):
        raise ValueError("refusing to download an update from outside this project's releases")
    request = urllib.request.Request(url, headers={"User-Agent": f"DepopSeller/{__version__}"})
    size = 0
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response, dest.open("wb") as out:
        while chunk := response.read(64 * 1024):
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise ValueError("the download is far larger than an update should be")
            out.write(chunk)


def _app_python() -> Path | None:
    """The Python this app runs on, as the console program so its answers can be read: the
    .venv's in a manual install, the bundled runtime's in one made by the Windows installer."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe") if os.name == "nt" else exe
    return console if console.exists() else None


def finish(root: Path, dependencies_changed: bool) -> None:
    """Make the new files runnable: fetch new libraries if the release needs them, then prove the
    new code imports, in a separate process, before anything restarts into it."""
    python = _app_python()
    if python is None:
        return                                                     # nothing to prove it with
    if dependencies_changed:
        done = subprocess.run([str(python), "-m", "pip", "install", "--quiet", "-e", "."], cwd=str(root),
                              capture_output=True, text=True, timeout=600, check=False, **no_console())
        if done.returncode != 0:
            raise RuntimeError("could not install what the new version needs: "
                               + (done.stderr or done.stdout).strip()[-400:])
    probe = "import depop_seller.cli, depop_seller.review, depop_seller.settings, depop_seller.updates"
    done = subprocess.run([str(python), "-c", probe], cwd=str(root), capture_output=True, text=True,
                          timeout=120, check=False, **no_console())
    if done.returncode != 0:
        raise RuntimeError("the new version does not start: " + done.stderr.strip()[-400:])


def install(download_url: str, *, root: Path | None = None, progress=lambda phase: None,
            after=finish) -> dict:
    """Download, validate, keep a copy of what changes, replace, prove it runs - or put it back."""
    root = root or config.PROJECT_ROOT
    if managed_by_git(root):
        raise RuntimeError("This copy of the app is a git checkout - update it with git pull instead.")
    with tempfile.TemporaryDirectory(prefix="depop-update-") as tmp:
        zip_path = Path(tmp) / ASSET_NAME
        progress("Downloading")
        _download(download_url, zip_path)
        version, files = read_update(zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            incoming = {inside: archive.read(member) for member, inside in files}

    base = root.resolve()
    changes: dict[Path, bytes] = {}
    for inside, data in incoming.items():
        target = root.joinpath(*inside.parts)
        if not target.resolve().is_relative_to(base):
            raise ValueError(f"unexpected path in the update: {inside}")
        if not (target.is_file() and target.read_bytes() == data):
            changes[target] = data
    old_pyproject = (root / "pyproject.toml").read_bytes() if (root / "pyproject.toml").is_file() else None
    dependencies_changed = _dependencies(old_pyproject) != _dependencies(incoming.get(PurePosixPath("pyproject.toml")))
    if dependencies_changed and config.bundled_runtime():
        # The installer's Python has no pip: new libraries come with a new installer, which
        # replaces the runtime as well as the app. Nothing has been touched yet.
        raise RuntimeError(f"This version also updates the parts Depop Seller runs on, so it comes as an "
                           f"installer: download {INSTALLER_NAME} from {RELEASES_PAGE}/latest and open it. "
                           "Your photos, listings, style and key stay as they are.")

    progress("Keeping a copy of this version")
    backup = backup_dir()
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)
    added = []
    for target in changes:
        if target.is_file():
            copy = backup / target.relative_to(root)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, copy)
        else:
            added.append(target)

    progress("Installing")
    try:
        for target, data in changes.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        progress("Getting it ready")
        after(root, dependencies_changed)
    except Exception as e:
        log.exception("update to %s failed - putting the previous version back", version)
        _put_back(root, backup, added)
        raise RuntimeError(f"The update could not be finished, so nothing has changed. ({e})") from e
    log.info("updated the app to %s: %d file(s) changed, previous copies in %s", version, len(changes), backup)
    return {"installed": version, "changed": len(changes), "backup": str(backup)}


def _put_back(root: Path, backup: Path, added: list[Path]) -> None:
    for copy in backup.rglob("*"):
        if copy.is_file():
            target = root / copy.relative_to(backup)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copy, target)
    for target in added:
        target.unlink(missing_ok=True)


# ---- restarting into the new version ---------------------------------------------------------

def restart_command(port: int) -> list[str]:
    """Start the app again, waiting for this instance to let go of the port first. pythonw on
    Windows, so the replacement has no console window either."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    runner = windowless if windowless.exists() else exe
    return [str(runner), "-m", "depop_seller", "hub", "--no-browser", "--port", str(port),
            "--wait-for-port", str(RESTART_WAIT_S)]


def spawn_replacement(port: int) -> None:
    """Launch the next instance fully detached, so it outlives this one. DEPOP_QUIET sends its
    messages to the log file, as for any instance started without a console."""
    kwargs: dict = {"cwd": str(config.PROJECT_ROOT), "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True,
                    "env": {**os.environ, "DEPOP_QUIET": "1"}}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x08)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(restart_command(port), **kwargs)
