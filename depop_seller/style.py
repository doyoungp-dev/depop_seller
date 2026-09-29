"""The description style, editable from the app.

`description_style.md` is the whole prompt for descriptions, so changing it changes every draft
from the next one on. The Style tab writes it two ways: the seller edits the text directly, or
types an instruction ("from now on ...") and Claude Code rewrites the file, which is shown as a
diff and only saved once the seller accepts it.

Every write keeps the previous version under style_history/, so a change can always be undone.
"""

from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime
from pathlib import Path

from .config import STYLE_HISTORY
from .describe import STYLE_FILE, STYLE_TEMPLATE, ensure_style_file, find_claude_cli, run_claude_code, _extract_json

log = logging.getLogger(__name__)

HISTORY_DIR = STYLE_HISTORY
KEEP_VERSIONS = 40
MIN_CHARS = 400                  # a rewrite that comes back this short lost something

REWRITE_PROMPT = """\
You maintain the house style a second-hand clothing seller uses for their Depop listings. The
style lives in one Markdown file, which is used as-is as the prompt that writes every listing.

The seller has asked for a change. Apply it to the file and return the complete new file.

Rules:
- Change only what the instruction asks for, plus whatever else must change to stay consistent
  (for example: a new hashtag group also belongs in the bank, a new field also belongs in the
  template, the examples must still match the template).
- Keep everything else exactly as it is, including wording, section order and the examples that
  are not affected.
- Keep the file's shape: the same Markdown headings, the template block, the field rules, the
  hashtag bank, the rules, and the examples.
- Never invent facts about the seller's shop.

Answer with ONLY a JSON object, no prose and no markdown fence, with two string keys:
  "markdown": the complete new file, from its first line to its last
  "summary":  one sentence, in plain language, saying what you changed
"""


def read_style() -> str:
    return ensure_style_file(STYLE_FILE, STYLE_TEMPLATE).read_text(encoding="utf-8")


def _validate(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    if len(text) < MIN_CHARS:
        raise ValueError(f"that would leave only {len(text)} characters of style - refusing to save it")
    if not re.search(r"^#+ ", text, re.M):
        raise ValueError("the style file needs its Markdown headings")
    return text + "\n"


def save_style(text: str) -> dict:
    """Write the style file, keeping the current version in style_history/."""
    text = _validate(text)
    backup = None
    if STYLE_FILE.exists():
        HISTORY_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # two saves in the same second must not overwrite each other's version
        backup = next(name for n in range(1, 100)
                      if not (HISTORY_DIR / (name := f"description_style-{stamp}{'' if n == 1 else f'-{n}'}.md")).exists())
        (HISTORY_DIR / backup).write_text(read_style(), encoding="utf-8")
        for stale in sorted(HISTORY_DIR.glob("description_style-*.md"),
                            key=lambda f: f.stat().st_mtime_ns)[:-KEEP_VERSIONS]:
            stale.unlink()
    STYLE_FILE.write_text(text, encoding="utf-8", newline="\n")
    log.info("description style saved (%d characters, previous version in %s)", len(text), backup)
    return {"chars": len(text), "backup": backup}


def history() -> list[dict]:
    """Previous versions, newest first. Ordered by when they were written, not by name: two
    versions saved in the same second share a timestamp and only differ by a suffix."""
    if not HISTORY_DIR.is_dir():
        return []
    files = sorted(HISTORY_DIR.glob("description_style-*.md"), key=lambda f: f.stat().st_mtime_ns, reverse=True)
    return [{"name": f.name,
             "when": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
             "chars": len(f.read_text(encoding="utf-8"))}
            for f in files]


def restore(name: str) -> dict:
    """Bring a previous version back (the current one is kept as a new backup)."""
    f = HISTORY_DIR / Path(name).name
    if not f.is_file():
        raise FileNotFoundError(f"no saved version called {name}")
    return save_style(f.read_text(encoding="utf-8")) | {"restored": f.name}


def diff(before: str, after: str) -> list[str]:
    return list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                     "style now", "style after the change", lineterm="", n=3))


def propose(instruction: str, *, cli: Path | None = None) -> dict:
    """Ask Claude Code (the seller's subscription) to apply an instruction to the style file.

    Returns the proposed file, a one-line summary and a unified diff. Nothing is written: the
    seller reads the diff and decides.
    """
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("type what should change")
    cli = cli or find_claude_cli()
    if cli is None:
        raise RuntimeError("Claude Code CLI not found (install the Claude desktop app, or set DEPOP_CLAUDE_CLI)")
    current = read_style()
    prompt = (f"{REWRITE_PROMPT}\n\nTHE SELLER'S INSTRUCTION:\n{instruction}\n\n"
              f"THE FILE AS IT IS NOW:\n\n{current}\n")
    log.info("style: asking Claude Code to apply %r", instruction[:80])
    envelope, credential = run_claude_code(prompt, cli=cli, max_turns=4)
    result = envelope.get("result") or ""
    if envelope.get("is_error"):
        raise RuntimeError(f"claude code: {result[:200]}")
    data = _extract_json(result)
    proposed = _validate(str(data.get("markdown") or ""))
    if proposed.strip() == current.strip():
        raise RuntimeError("Claude Code did not change anything - try saying it differently")
    return {"markdown": proposed, "summary": str(data.get("summary") or "").strip(),
            "diff": diff(current, proposed), "credential": credential}
