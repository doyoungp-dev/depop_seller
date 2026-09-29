"""Desktop integration: the bits that differ between Windows, macOS and Linux.

The pages have to run in the user's own Chrome - that is where the helper extension lives, and
without it the Depop button falls back to an automated browser Depop blocks. So "open the app"
means Chrome's `--app=` mode: the same browser and the same profile, but a window with no tabs and
no address bar, with its own icon in the dock or taskbar. Any Chromium browser will do; if none is
found the default browser is used instead.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

log = logging.getLogger(__name__)

WINDOW_SIZE = (1360, 900)


def no_console() -> dict:
    """Keyword arguments for starting a console program in the background.

    The app runs without a console (pythonw on Windows). Windows then gives any console program it
    starts - Claude Code, taskkill, PowerShell - a console window of its own: a black box popping
    up in front of someone who has no idea what it is. CREATE_NO_WINDOW stops that. Only for work
    that happens out of sight; the sign-in terminal is meant to be seen and does not use this.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}

# Most preferred first. No --user-data-dir: the window must use the profile the extension is in.
_MAC = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
_WINDOWS = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
]
_LINUX = ["google-chrome", "chromium", "chromium-browser", "brave-browser", "microsoft-edge"]


def app_browser() -> Path | None:
    """The Chromium browser to open app windows with, or None to fall back to the default browser."""
    override = os.environ.get("DEPOP_BROWSER", "").strip()
    if override:
        p = Path(os.path.expandvars(override)).expanduser()
        return p if p.exists() else None
    if sys.platform == "darwin":
        candidates = [Path(c).expanduser() for c in _MAC]
    elif sys.platform == "win32":
        candidates = [Path(os.path.expandvars(c)) for c in _WINDOWS]
    else:
        candidates = [Path(found) for name in _LINUX if (found := shutil.which(name))]
    return next((c for c in candidates if c.exists()), None)


def app_window(url: str, *, size: tuple[int, int] = WINDOW_SIZE) -> str:
    """Open `url` as an app window. Returns the name of whatever opened it (for the console line)."""
    exe = app_browser()
    if exe is None:
        webbrowser.open(url)
        return "default browser"
    cmd = [str(exe), f"--app={url}", f"--window-size={size[0]},{size[1]}"]
    kwargs = {"start_new_session": True} if os.name != "nt" else {}
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except OSError as e:                      # a browser that moved or is not runnable
        log.warning("could not open %s as an app window (%s); using the default browser", exe.name, e)
        webbrowser.open(url)
        return "default browser"
    return exe.stem if sys.platform == "win32" else exe.name


def open_folder(path: Path) -> None:
    """Show a folder in Explorer / Finder / the Linux file manager."""
    path = Path(path)
    if sys.platform == "win32":
        os.startfile(str(path))               # noqa: S606 - Windows only, path comes from our own config
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def windows_desktop_dir() -> Path | None:
    """The real Desktop, which OneDrive's folder backup often moves out of the home directory."""
    if os.name != "nt":
        return None
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            raw, _ = winreg.QueryValueEx(handle, "Desktop")
        folder = Path(os.path.expandvars(raw))
        if folder.is_dir():
            return folder
    except OSError:
        pass
    fallback = Path.home() / "Desktop"
    return fallback if fallback.is_dir() else None


def ensure_desktop_shortcut(project_root: Path) -> Path | None:
    """Put the app's icon on the Desktop if it is not there.

    Setup makes it, but setup gets skipped when a .venv already exists and its last step can fail,
    and then there is no way into the app except finding a file in a folder. Best effort: nothing
    here may stop the app from starting.
    """
    desktop = windows_desktop_dir()
    if desktop is None:
        return None
    link = desktop / "Depop Seller.lnk"
    if link.exists():
        return link
    maker = project_root / "make_desktop_icon.ps1"
    if not maker.is_file():
        return None
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(maker)],
                       cwd=str(project_root), capture_output=True, timeout=60, check=False, **no_console())
    except (OSError, subprocess.SubprocessError):
        return None
    if link.exists():
        log.info("put the Depop Seller icon on the Desktop")
        return link
    return None
