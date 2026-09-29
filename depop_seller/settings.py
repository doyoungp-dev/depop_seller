"""The Settings tab: the two credentials the app needs, and where everything lives.

Two separate things, easy to confuse:

* the **Anthropic API key** pays for photo grouping, per batch. It lives in the gitignored `.env`
  file next to the project. The key is never sent back to the page or written to the log - only
  whether one is set and its last four characters.
* **Claude Code** writes the descriptions on the seller's own Claude subscription. It has its own
  sign-in, done once in the browser (`claude auth login`, run without a window); nothing about it
  is stored here. The old way - a terminal window where /login is typed - stays as a fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config
from .describe import _kill_tree, find_claude_cli, subscription_env
from .desktop import no_console

log = logging.getLogger(__name__)

ENV_FILE = config.ENV_FILE
KEY_NAME = "ANTHROPIC_API_KEY"


def mask(key: str) -> str:
    """What the page is allowed to see: enough to recognise the key, not enough to use it."""
    key = (key or "").strip()
    return f"{key[:7]}...{key[-4:]}" if len(key) > 14 else "set"


def api_key_status() -> dict:
    """Whether a key is available, where it came from, and its last four characters."""
    from_env = (os.environ.get(KEY_NAME) or "").strip()
    key = config.load_api_key()
    return {
        "set": key is not None,
        "hint": mask(key) if key else "",
        "source": "this computer's environment variables" if from_env else (".env file" if key else ""),
        "editable": not from_env,          # an environment variable wins over the file
        "env_file": str(ENV_FILE),
    }


def save_api_key(key: str) -> dict:
    """Write the key into .env, leaving any other lines in the file alone."""
    key = (key or "").strip().strip('"').strip("'")
    if not key:
        raise ValueError("paste the key first")
    if not key.startswith("sk-ant-") or len(key) < 30 or any(c.isspace() for c in key):
        raise ValueError("that does not look like an Anthropic API key (they start with sk-ant-)")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    kept = [ln for ln in lines if not ln.strip().startswith(f"{KEY_NAME}=")]
    kept.append(f"{KEY_NAME}={key}")
    tmp = ENV_FILE.with_name(".env.part")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    tmp.replace(ENV_FILE)
    log.info("API key saved to %s (%s)", ENV_FILE.name, mask(key))   # never log the key itself
    return api_key_status()


def test_api_key() -> dict:
    """Check the key really works, without spending anything: list the models (no tokens)."""
    import anthropic

    key = config.load_api_key()
    if not key:
        return {"ok": False, "detail": "No key is set yet."}
    try:
        models = anthropic.Anthropic(api_key=key, max_retries=1, timeout=20.0).models.list(limit=4)
    except anthropic.AuthenticationError:
        return {"ok": False, "detail": "Anthropic rejected this key. Check you pasted all of it, or make a new one."}
    except anthropic.PermissionDeniedError:
        return {"ok": False, "detail": "The key is valid but not allowed to use the API - check the workspace it belongs to."}
    except anthropic.APIConnectionError:
        return {"ok": False, "detail": "Could not reach Anthropic - check this computer's internet connection."}
    except anthropic.APIStatusError as e:
        return {"ok": False, "detail": f"Anthropic answered with an error ({e.status_code})."}
    names = [m.id for m in models.data]
    return {"ok": True, "detail": "The key works." + (f" Models available: {len(names)}." if names else "")}


CLAUDE_DOWNLOAD = "https://claude.ai/download"


def claude_code_status() -> dict:
    """Whether Claude Code is on this computer. It ships inside the Claude desktop app, so the
    answer to "how do I install it" is a normal installer, not a terminal."""
    cli = find_claude_cli()
    return {
        "found": cli is not None,
        "path": str(cli) if cli else "",
        "download": CLAUDE_DOWNLOAD,
        "hint": "" if cli else ("Claude Code is not on this computer yet. It comes with the Claude "
                                "desktop app - install that, open it once, then press Reload here."),
    }


def test_claude_login() -> dict:
    """Whether Claude Code is signed in to a Claude plan, asked of the CLI itself: a second, and
    nothing used from the subscription. Run with subscription_env(), like the drafts."""
    cli = find_claude_cli()
    if cli is None:
        return {"ok": False, "detail": claude_code_status()["hint"]}
    try:
        done = subprocess.run([str(cli), "auth", "status", "--json"], env=subscription_env(), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60, **no_console())
        status = json.loads(done.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"ok": False, "detail": "Could not ask Claude Code whether it is signed in - try again."}
    if not status.get("loggedIn"):
        return {"ok": False, "detail": "Not signed in yet - press Sign in with Claude."}
    if status.get("authMethod") != "claude.ai":
        return {"ok": False, "detail": "Claude Code is signed in to an API account, which would be billed separately. "
                                       "Press Sign in with Claude to use your Claude plan instead."}
    plan = str(status.get("subscriptionType") or "").strip().capitalize()
    return {"ok": True, "plan": plan, "detail": f"Signed in with your Claude {plan} plan." if plan
            else "Signed in with your Claude plan."}


# ---- signing in, in the browser --------------------------------------------------------------

SIGN_IN_TIMEOUT_S = 15 * 60


class SignIn:
    """`claude auth login` with no window. It opens the browser; the seller signs in there and the
    CLI finishes by itself. It also prints a link for when no browser opened - that route ends on a
    page showing a code, which the Settings page hands to the CLI (it reads it from stdin)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.state: dict = {"running": False, "url": "", "ok": None, "detail": ""}

    def start(self) -> dict:
        cli = find_claude_cli()
        if cli is None:
            raise RuntimeError(claude_code_status()["hint"])
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return dict(self.state)                                  # already waiting
            extra = no_console() if os.name == "nt" else {"start_new_session": True}
            proc = subprocess.Popen([str(cli), "auth", "login", "--claudeai"], env=subscription_env(),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", **extra)
            self.proc = proc
            self.state = {"running": True, "url": "", "ok": None, "detail": ""}
        threading.Thread(target=self._follow, args=(proc, self.state), name="sign-in", daemon=True).start()
        return dict(self.state)

    def _follow(self, proc: subprocess.Popen, state: dict) -> None:
        timer = threading.Timer(SIGN_IN_TIMEOUT_S, _kill_tree, args=(proc,))
        timer.daemon = True
        timer.start()
        said = []
        try:
            for line in iter(proc.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                said.append(line)
                found = re.search(r"https://[^ ]+", line)
                if found and not state["url"]:
                    state["url"] = found.group(0)
            proc.wait()
        finally:
            timer.cancel()
        result = test_claude_login()
        failed = next((ln.split(">")[-1].strip() for ln in reversed(said) if "fail" in ln.lower()), "")
        state.update(running=False, ok=result["ok"],
                     detail=result["detail"] if result["ok"] else (failed or "The sign-in did not finish.") +
                     " Press Sign in with Claude to try again.")
        log.info("Claude sign-in finished: %s", "signed in" if result["ok"] else "not signed in")

    def send_code(self, code: str) -> dict:
        code = (code or "").strip()
        if not code or len(code) > 1000 or any(c.isspace() for c in code):
            raise ValueError("paste the whole code from the sign-in page")
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise RuntimeError("the sign-in is not waiting for a code any more - press Sign in with Claude again")
            proc.stdin.write(code + chr(10))
            proc.stdin.flush()
        return dict(self.state)

    def status(self) -> dict:
        return dict(self.state)


SIGN_IN = SignIn()


def open_login_terminal() -> dict:
    """The fallback sign-in: a terminal window running Claude Code, where /login can be typed."""
    cli = find_claude_cli()
    if cli is None:
        raise RuntimeError(claude_code_status()["hint"])
    env = subscription_env()                                # the sign-in must not see an API key either
    if sys.platform == "win32":
        subprocess.Popen(["cmd", "/c", "start", "Claude sign-in", "cmd", "/k", str(cli)], env=env)
    elif sys.platform == "darwin":
        # Terminal runs this as a shell command, and the CLI lives under "Application Support",
        # so the path has to be quoted or the sign-in window just says "command not found".
        quoted = "'" + str(cli).replace("'", "'\\''") + "'"
        subprocess.Popen(["osascript",
                          "-e", f'tell application "Terminal" to do script "{quoted}"',
                          "-e", 'tell application "Terminal" to activate'], env=env)
    else:
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            if shutil_which(term):
                subprocess.Popen([term, "-e", str(cli)], env=env)
                break
        else:
            raise RuntimeError(f"Open a terminal yourself and run: {cli}")
    return {"opened": str(cli)}


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def environment() -> dict:
    """Where everything lives, for the bottom of the Settings page."""
    batches = config.batch_dirs()
    return {
        "python": sys.version.split()[0],
        "platform": {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, sys.platform),
        "data_dir": str(config.DATA_DIR),
        "project_dir": str(config.PROJECT_ROOT),
        "photos_dir": str(config.PRODUCT_IMAGE_DIR),
        "photos_dir_override": bool(os.environ.get("DEPOP_SELLER_PRODUCT_DIR")),
        "database": str(config.DB_PATH),
        "style_file": str(config.STYLE_FILE),
        "extension_dir": str(config.PROJECT_ROOT / "chrome_extension"),
        "batches": len(batches),
        "photos": sum(1 for d in batches for p in (d / "raw_image").iterdir() if p.is_file()),
    }


def state() -> dict:
    from .updates import REPO

    return {"api_key": api_key_status(), "claude_code": claude_code_status(), "environment": environment(),
            "cost_per_photo": _cost_per_photo(), "default_model": config.DEFAULT_MODEL,
            "project_page": f"https://github.com/{REPO}"}


def _cost_per_photo() -> dict:
    from .group import COST_PER_PHOTO

    return COST_PER_PHOTO
