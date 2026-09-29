"""Draft a Depop description for an item from its photos and the seller's facts line.

Runs the Claude Code CLI (the one bundled with the Claude desktop app) on the user's
subscription - never the API. The house style lives in description_style.md at the project
root (editable). Facts typed by the seller override the photos; anything unknown becomes a
[placeholder] and is reported in `missing`, never invented.

Billing guarantee: the CLI is started with every ANTHROPIC_* variable removed from its
environment (Claude Code prefers an inherited API key over the subscription login, and the
photo-grouping key used to leak in this way), and the run is killed if Claude Code reports
any credential source other than "none" (the subscription).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from .config import PROJECT_ROOT, STYLE_FILE, STYLE_TEMPLATE, BatchPaths
from .desktop import no_console
from .group import Usage
from .manifest import Row

log = logging.getLogger(__name__)

MAX_CHARS = 1000            # Depop's limit
MAX_CLOSEUPS = 3
CC_TIMEOUT_S = 240
CC_MODEL = os.environ.get("DEPOP_CLAUDE_CODE_MODEL", "opus")   # same family the user's chat project runs on

SYSTEM_PROMPT = """\
You write Depop listings for a second-hand clothing seller, in the house style given by the
seller. You receive the item's photos (the cover and, when available, label or detail close-ups)
and a short line of facts typed by the seller.

Rules:
- Follow the house style's template line by line, including which lines are always present, the
  line that names the styling props, and its character limit. Imitate the examples.
- The seller's facts win over anything you see. Never invent a brand, size, material, measurement
  or flaw. When a fact is missing and not clearly legible on a tag in the photos, write a
  placeholder such as [size?] in the text and name the field in `missing`.
- Read brand, size, fibre content and care details from label photos when the seller did not state
  them and the tag is legible. Brand, Size, Material, Condition and Measurements always appear,
  with a placeholder when unknown.
- Describe only what is visible: garment type, colour, fabric, cut, notable details. Recurring
  styling props (the mannequin's shorts, a handbag, a necklace) are not part of the item.
- Hashtags: only the specific style, era and character tags the house style lists, chosen for the
  aesthetic this piece genuinely belongs to, and matching the vibe line. Never generic filler tags.
Return the finished listing text in `text`, plus the individual fields, and in `tip` one short
practical note for the seller when there is one (otherwise an empty string).
"""

# The Claude Code CLI, usually the copy bundled with the Claude desktop app; override with the
# DEPOP_CLAUDE_CLI env var. PATH is searched, but never relied on: an app launched from Finder or
# Explorer inherits a minimal PATH, so every known install location is checked explicitly.
# On Windows the desktop app is a packaged (Store) app: what it writes to %APPDATA%\Claude really
# lives in %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude and is only visible at the
# %APPDATA% path from the app's own processes, so the real path is searched first.
_APPDATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
CLI_NAME = "claude.exe" if os.name == "nt" else "claude"
CLAUDE_CLI_CANDIDATES = [Path(p) for p in os.environ.get("DEPOP_CLAUDE_CLI", "").split(os.pathsep) if p]


def _newest_bundle(bundle: Path) -> Path | None:
    if not bundle.is_dir():
        return None
    versions = sorted((d for d in bundle.iterdir() if (d / CLI_NAME).is_file()),
                      key=lambda d: [int(x) if x.isdigit() else x for x in d.name.split(".")])
    return versions[-1] / CLI_NAME if versions else None


def _bundle_dirs() -> list[Path]:
    """Folders where the Claude desktop app keeps versioned copies of Claude Code."""
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"]
    if os.name == "nt":
        out = [pkg / "LocalCache" / "Roaming" / "Claude" / "claude-code"
               for pkg in sorted((_LOCALAPPDATA / "Packages").glob("Claude_*"))
               if (_LOCALAPPDATA / "Packages").is_dir()]
        out.append(_APPDATA / "Claude" / "claude-code")
        return out
    return [Path.home() / ".config" / "Claude" / "claude-code"]


def _standalone_paths() -> list[Path]:
    """Installs outside the desktop app: the native installer, homebrew, npm."""
    home = Path.home()
    if os.name == "nt":
        return [home / ".claude" / "local" / "claude.exe", _APPDATA / "npm" / "claude.cmd"]
    return [home / ".claude" / "local" / "claude", Path("/opt/homebrew/bin/claude"),
            Path("/usr/local/bin/claude")]


def find_claude_cli() -> Path | None:
    """The `claude` executable: DEPOP_CLAUDE_CLI, PATH, the desktop app's bundle, then the
    standalone install locations for this OS."""
    for c in CLAUDE_CLI_CANDIDATES:
        if c.is_file():
            return c
    from shutil import which
    on_path = which("claude")
    if on_path:
        return Path(on_path)
    for bundle in _bundle_dirs():
        found = _newest_bundle(bundle)
        if found:
            return found
    return next((p for p in _standalone_paths() if p.is_file()), None)


class Draft(BaseModel):
    text: str
    title: str
    brand: str
    size: str
    material: str
    color: str
    condition: str
    measurements: str
    category_hint: str
    missing: list[str]
    tip: str = ""


@dataclass
class DraftResult:
    draft: Draft
    usage: Usage
    model: str = "claude-code"
    credential: str = "none"    # Claude Code's apiKeySource for the run; "none" = subscription

    @property
    def cost(self) -> float | None:
        return None             # subscription usage is not billed per token


def ensure_style_file(style: Path, template: Path) -> Path:
    """The style is personal, so it is not in the repo: on a new machine, start it from the
    template that ships with the app. Editing it afterwards never touches the template."""
    if not style.exists():
        if not template.exists():
            raise FileNotFoundError(f"{style} is missing, and so is the template {template.name}")
        style.write_text(template.read_text(encoding="utf-8"), encoding="utf-8", newline=chr(10))
        log.info("started %s from %s", style.name, template.name)
    return style


def load_style() -> str:
    return ensure_style_file(STYLE_FILE, STYLE_TEMPLATE).read_text(encoding="utf-8")


def pick_photos(group: list[Row]) -> tuple[Row, list[Row]]:
    """The cover plus up to MAX_CLOSEUPS label/detail/flaw shots (or the next photos if none)."""
    cover = group[0]
    closeups = [r for r in group[1:] if r.view in ("label", "detail", "flaw")][:MAX_CLOSEUPS]
    if not closeups:
        closeups = group[1:1 + MAX_CLOSEUPS]
    return cover, closeups


def _extract_json(text: str) -> dict:
    """The CLI returns prose around the JSON now and then; take the outermost object."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


def subscription_env() -> dict[str, str]:
    """The environment for the CLI: everything except Anthropic credentials and endpoints."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("ANTHROPIC_")}


class BillingRefused(RuntimeError):
    """Claude Code was about to use something other than the subscription login."""


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop the CLI and everything it spawned. `claude` may be a shim script (npm installs one),
    so killing the process alone can leave the real one running: Windows gets `taskkill /T`, and
    elsewhere the whole process group, which the CLI is given by start_new_session below."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, **no_console())
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def run_claude_code(prompt: str, *, cli: Path, cwd: Path = PROJECT_ROOT, timeout: float = CC_TIMEOUT_S,
                    model: str = CC_MODEL, max_turns: int = 12) -> tuple[dict, str]:
    """Run `claude -p` headlessly and return (result envelope, credential source).

    Streams the CLI's JSON events: the first `system/init` event names the credential source;
    anything but "none" (subscription) kills the process before the model is called.
    """
    cmd = [str(cli), "-p", "--output-format", "stream-json", "--verbose", "--allowedTools", "Read",
           "--max-turns", str(max_turns), "--model", model]
    # no console window on Windows; on other systems, an own process group for _kill_tree
    extra = no_console() if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=str(cwd), text=True, encoding="utf-8", errors="replace",
                            env=subscription_env(), **extra)
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    watchdog = threading.Timer(timeout, _kill_tree, args=(proc,))
    watchdog.start()
    err_lines: list[str] = []

    def feed() -> None:                    # the prompt is bigger than a pipe buffer: never block the reader
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass

    threading.Thread(target=feed, name="claude-stdin", daemon=True).start()
    threading.Thread(target=lambda: err_lines.extend(proc.stderr), name="claude-stderr", daemon=True).start()
    credential = "unknown"
    result: dict | None = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                credential = str(event.get("apiKeySource") or "none")
                if credential != "none":
                    _kill_tree(proc)
                    raise BillingRefused(
                        f"Claude Code reported credential source {credential!r}; only the subscription "
                        f"login is allowed for descriptions")
            elif event.get("type") == "result":
                result = event
    finally:
        watchdog.cancel()
        if result is None:                 # refused, timed out or crashed: nothing may keep running
            _kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait()
    if result is None:
        err = "".join(err_lines).strip()
        raise RuntimeError(f"claude code produced no result (exit {proc.returncode}): {err[:300]}")
    return result, credential


def draft_description(
    paths: BatchPaths,
    group: list[Row],
    facts: str,
    *,
    large_image,                       # callable(stem) -> Path of a 2048 px JPEG
    style: str | None = None,
    cli: Path | None = None,
) -> DraftResult:
    """Draft one item's description with Claude Code on the subscription."""
    cli = cli or find_claude_cli()
    if cli is None:
        raise RuntimeError("Claude Code CLI not found (install the Claude desktop app, or set DEPOP_CLAUDE_CLI)")
    style = style if style is not None else load_style()
    cover, closeups = pick_photos(group)
    images = [large_image(cover.stem)] + [large_image(r.stem) for r in closeups]
    listing = "\n".join(f"- {p}  ({'cover' if i == 0 else closeups[i - 1].view})" for i, p in enumerate(images))
    prompt = (
        SYSTEM_PROMPT
        + "\n\nFirst read every photo listed below with the Read tool (they are JPEG files), then answer.\n"
        + "Answer with ONLY a JSON object - no prose, no markdown fence - with these string keys: "
        + "text, title, brand, size, material, color, condition, measurements, category_hint, tip, "
        + "and missing (a JSON array of strings).\n\n"
        + "HOUSE STYLE (follow exactly):\n\n" + style
        + "\n\nSELLER'S FACTS: " + (facts.strip() or "(none given - use placeholders for anything not visible)")
        + "\n\nPHOTOS:\n" + listing + "\n"
    )
    log.info("claude code: drafting item with %d photo(s)", len(images))
    envelope, credential = run_claude_code(prompt, cli=cli)
    result = envelope.get("result") or ""
    if envelope.get("is_error"):
        hint = " - sign in once with the Claude login launcher" if "log" in result.lower() else ""
        raise RuntimeError(f"claude code: {result[:200]}{hint}")
    data = _extract_json(result)
    data.setdefault("missing", [])
    data.setdefault("tip", "")
    for k in ("title", "brand", "size", "material", "color", "condition", "measurements", "category_hint"):
        data.setdefault(k, "")
    draft = Draft.model_validate(data)
    u = envelope.get("usage") or {}
    usage = Usage(calls=1, input_tokens=int(u.get("input_tokens") or 0), output_tokens=int(u.get("output_tokens") or 0))
    if len(draft.text) > MAX_CHARS:
        log.warning("draft is %d characters, over Depop's %d", len(draft.text), MAX_CHARS)
    return DraftResult(draft=draft, usage=usage, credential=credential)


# Kept as the name the server and tests used for the Claude Code path.
draft_with_claude_code = draft_description
