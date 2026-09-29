"""Local review UI: a small HTTP server on 127.0.0.1 that serves the drag-and-drop page,
saves manifests, runs the grouping step for new batches, and runs the apply step.
Stdlib only. Every batch-specific route takes ?batch=<folder name>.

Routes:
  GET  /                      the page (static/review.html)
  GET  /icon.png              the app icon, used as the favicon and so as the app window's icon
  GET  /app.css               the stylesheet every page shares
  GET  /batches/cover         ?batch= : a 512 px JPEG of the batch's first photo (made on demand)
  POST /batches/photos        ?batch=&name= : the raw bytes of one photo, written into raw_image/
  POST /batches/import        {batch, folder}: copy every photo from a folder on this computer
  POST /batches/rename        {name, to}: rename a batch folder and its rows in the DB
  GET  /style                 the description style page (static/style.html)
  GET  /style/state           the style text, its history and any running rewrite
  POST /style/save            {text}: write description_style.md (previous version kept)
  POST /style/instruct        {instruction}: ask Claude Code to rewrite the style; poll /style/status
  GET  /style/status          the proposed rewrite: running / summary / diff / markdown
  POST /style/restore         {name}: bring a previous version back
  GET  /version               the app's version and process, and whether the code on disk is newer
  POST /updates/check         ask GitHub whether a newer release exists (only when the user asks)
  POST /updates/install       download, check and install it in a background thread, then restart
  GET  /updates/status        {running, phase, error, installed, restart}
  POST /quit                  stop the app (the desktop icon runs it without a window)
  POST /closing               the app window went away: stop too, unless something is still running
  POST /settings/share        write a zip of the app to the Desktop, to give to someone else
  GET  /settings              the settings page (static/settings.html)
  GET  /settings/state        API key status (masked), Claude Code status, paths
  POST /settings/key          {key}: write ANTHROPIC_API_KEY into .env
  POST /settings/key/test     check the key with a free models.list call
  POST /settings/login        sign Claude Code in through the browser (`claude auth login`, no window)
  GET  /settings/login/status {running, url, ok, detail} of that sign-in
  POST /settings/login/code   {code}: the code the sign-in page shows when the link had to be opened by hand
  POST /settings/login/terminal  the fallback: a terminal window where /login can be typed
  POST /settings/login/test   `claude auth status`: signed in, and to which plan
  GET  /batches               every batch folder with its status, plus API key availability
  GET  /state?batch=          current grouping as JSON (404 if the batch has no manifest yet)
  GET  /thumbs/<file>?batch=  cached thumbnail
  GET  /large/<file>?batch=   2048 px JPEG of the original, converted on first request (cache/large/)
  POST /save?batch=           {"items": [[seq, ...], ...], "unused": [seq, ...], "deleted": [seq, ...],
                               "views": {seq: view}}  (deleted = not for sale; raw files are never touched)
  POST /group?batch=          {"model": ...}  -> scan + Claude grouping in a background thread
  GET  /group/status?batch=   {"running", "phase", "done", "total", "cost", "error"}
  POST /apply?batch=          {"force": bool}  -> apply step in a background thread
  GET  /apply/status?batch=   {"running", "done", "total", "written", "error"}
  GET  /hub                   the batches page (static/hub.html): every date folder, its status, Review / Sell links
  POST /batches/new           {"name": "YYYYMMDD"} -> creates product_image/<name>/raw_image
  POST /batches/open          {"name"} -> opens the batch's raw_image folder in Explorer
  GET  /sell                  the selling page (static/sell.html)
  GET  /sell/state?batch=     items with photos + listing status/description from the DB (synced from the manifest)
  POST /sell/open?batch=&item=   open Depop's listing form in the listing browser with the item's photos
  GET  /sell/status?batch=&item= progress of that job
  POST /sell/item?batch=&item=   {"status"?, "description"?, "depop_url"?} -> saved to the DB
  GET  /sell/photos?batch=&item= the item's upload list for the Chrome extension: [{name, url}] (max 8) + description
  POST /sell/describe?batch=     {"items": [id, ...], "facts": {id: text}?} -> drafts descriptions in a background thread
  GET  /sell/describe/status?batch=  per-item drafting status

Responses carry CORS headers so the helper extension can call this server from depop.com.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from . import __version__, config, db, desktop
from .apply import apply_batch, convert
from .config import DEFAULT_MODEL, MAX_PHOTOS_PER_ITEM, VIEWS, BatchPaths, batch_paths, load_api_key
from .manifest import Row, deleted, items, normalize, read_manifest, summary, unused, write_manifest

log = logging.getLogger(__name__)
MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]
TEST_FORM = """<!doctype html><title>test form</title>
<input type=file multiple id=f>
<textarea name=description style="display:none"></textarea>
<div id=editor contenteditable="true" aria-label="Description" style="border:1px solid #999;min-height:40px"></div>
<pre id=out></pre>
<script>document.getElementById('f').onchange = e => { document.getElementById('out').textContent =
  [...e.target.files].map(f => f.name + ':' + f.size).join(' | '); };</script>"""


def code_mtime() -> int:
    """Newest change time across the app's own code. The pages are read from disk per request, so
    after an update the browser shows new pages while the running process still has the old routes
    - which looks like a dead link. /version lets a page notice and say "restart the app"."""
    here = Path(__file__).parent
    return max((f.stat().st_mtime_ns for f in [*here.glob("*.py"), *(here / "static").glob("*.html")]), default=0)


CLOSE_GRACE_S = 10          # long enough for another tab of the app to load and cancel the close
HANDOFF_QUIET_S = 120       # after handing an item to the Depop helper, stay up for it

PHOTO_SUFFIXES = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".webp"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024          # one photo; a HEIC is about 4 MB
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_batch_name(name: str) -> str:
    """A batch name is any label the seller likes, as long as it is a safe folder name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("the batch needs a name")
    if len(name) > 60:
        raise ValueError("the name is too long (60 characters max)")
    if any(c in name for c in '\\/:*?"<>|') or any(ord(c) < 32 for c in name):
        raise ValueError('a name cannot contain \\ / : * ? " < > |')
    if name.startswith(".") or name != name.rstrip(". "):
        raise ValueError("a name cannot start with a dot or end with a dot or space")
    if name.split(".")[0].upper() in _RESERVED:
        raise ValueError(f"{name} is a reserved name on Windows")
    return name


def safe_photo_name(name: str) -> str:
    """The file name of an uploaded photo: a bare file name with a picture extension, never a path."""
    name = Path((name or "").replace("\\", "/")).name.strip()
    if not name or name.startswith("."):
        raise ValueError(f"bad photo name {name!r}")
    if Path(name).suffix.lower() not in PHOTO_SUFFIXES:
        raise ValueError(f"{name} is not a photo ({', '.join(sorted(PHOTO_SUFFIXES))})")
    return name


class ReviewSession:
    """One batch's rows plus the state of its background jobs."""

    def __init__(self, paths: BatchPaths, rows: list[Row]) -> None:
        self.paths = paths
        self.rows = rows
        self.lock = threading.Lock()
        self.apply_state: dict = {"running": False, "done": 0, "total": 0, "written": None, "error": None}
        self.manifest_mtime = self._mtime()

    def _mtime(self) -> int:
        return self.paths.manifest.stat().st_mtime_ns if self.paths.manifest.exists() else 0

    def large_image(self, stem: str):
        """Path of a 2048 px JPEG for the photo with this stem, converting the original once."""
        with self.lock:
            row = next((r for r in self.rows if r.stem == stem), None)
        if row is None:
            return None
        out = self.paths.cache / "large" / f"{stem}.jpg"
        if not out.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            convert(self.paths.raw / row.raw_file, out, max_px=2048, quality=85)
        return out

    def thumb_image(self, stem: str):
        """Path of the 512 px thumbnail, making it from the original if it is not there.

        thumbs/ is a cache and is gitignored, so a batch folder copied from another computer (or
        one trimmed to save space) can arrive without it. Rebuilding beats showing broken tiles.
        """
        out = self.paths.thumbs / f"{stem}.jpg"
        if out.is_file():
            return out
        with self.lock:
            row = next((r for r in self.rows if r.stem == stem), None)
        source = self.paths.raw / row.raw_file if row else None
        if source is None or not source.is_file():
            return None
        from .scan import make_thumbnail

        out.parent.mkdir(parents=True, exist_ok=True)
        make_thumbnail(source, out)
        log.info("rebuilt a missing thumbnail for %s", stem)
        return out

    # ---- state -------------------------------------------------------------------------

    @staticmethod
    def _photo(r: Row) -> dict:
        return {
            "seq": r.seq, "file": r.raw_file, "stem": r.stem, "gap": r.gap_s, "taken": r.taken_at,
            "view": r.view, "confidence": r.confidence, "note": r.note,
        }

    def state(self) -> dict:
        with self.lock:
            grouped = items(self.rows)
            count = sum(1 for p in self.paths.sort_image.glob("*.jpg")) if self.paths.sort_image.is_dir() else 0
            return {
                "batch": self.paths.batch,
                "views": list(VIEWS),
                "max_photos": MAX_PHOTOS_PER_ITEM,
                "items": [{"id": i, "photos": [self._photo(r) for r in g]} for i, g in grouped.items()],
                "unused": [self._photo(r) for r in unused(self.rows)],
                "deleted": [self._photo(r) for r in deleted(self.rows)],
                "summary": summary(self.rows),
                "sort_image_count": count,
                "manifest": str(self.paths.manifest),
            }

    # ---- save --------------------------------------------------------------------------

    def _check_unchanged(self) -> None:
        """Refuse to write over a manifest that changed on disk since this session loaded it."""
        if self._mtime() != self.manifest_mtime:
            raise FileExistsError(
                "manifest.csv was changed by something else (another review window or a tool) since this "
                "page loaded it. Close other review windows, then Reload this page and redo your edits.")

    def _write_manifest(self) -> None:
        normalize(self.rows)
        if self.paths.manifest.exists():
            shutil.copy2(self.paths.manifest, self.paths.manifest.with_name("manifest.bak.csv"))
        write_manifest(self.paths.manifest, self.rows)
        self.manifest_mtime = self._mtime()

    def save(self, payload: dict) -> dict:
        with self.lock:
            self._check_unchanged()
            by_seq = {r.seq: r for r in self.rows}
            # Validate the whole payload before touching a single row.
            assign: dict[int, tuple[int, int, bool, bool]] = {}   # seq -> (item, photo_no, skip, deleted)
            placements = [(item_no, photo_no, seq, False)
                          for item_no, seqs in enumerate(payload.get("items") or [], 1)
                          for photo_no, seq in enumerate(seqs, 1)]
            placements += [(0, 0, seq, False) for seq in payload.get("unused") or []]
            placements += [(0, 0, seq, True) for seq in payload.get("deleted") or []]
            for item_no, photo_no, seq, is_deleted in placements:
                seq = int(seq)
                if seq not in by_seq:
                    raise ValueError(f"unknown photo seq {seq}")
                if seq in assign:
                    raise ValueError(f"photo seq {seq} appears more than once")
                assign[seq] = (item_no, photo_no, item_no == 0, is_deleted)
            if set(assign) != set(by_seq):
                raise ValueError("every photo must appear exactly once across items, unused and deleted")
            views = {int(seq): view for seq, view in (payload.get("views") or {}).items()}
            for seq, view in views.items():
                if seq not in by_seq or view not in VIEWS:
                    raise ValueError(f"bad view {view!r} for photo seq {seq}")

            for seq, (item_no, photo_no, skip, is_deleted) in assign.items():
                r = by_seq[seq]
                r.item_id, r.photo_no, r.skip, r.deleted = item_no, photo_no, skip, is_deleted
            for seq, view in views.items():
                by_seq[seq].view = view
            self._write_manifest()
            log.info("saved %s: %s", self.paths.manifest.name, summary(self.rows))
        return self.state()

    def reorder_item(self, item_id: int, seqs: list[int]) -> list[Row]:
        """Set one item's photo order (the Sell page's drag and drop): `seqs` is the item's
        photos in the new order, first = cover. Writes manifest.csv like save()."""
        with self.lock:
            self._check_unchanged()
            group = items(self.rows).get(item_id)
            if not group:
                raise LookupError(f"item {item_id} not found")
            if sorted(seqs) != sorted(r.seq for r in group) or len(set(seqs)) != len(seqs):
                raise ValueError(f"the new order must list each of item {item_id}'s photos exactly once")
            by_seq = {r.seq: r for r in group}
            for n, seq in enumerate(seqs, 1):
                by_seq[seq].photo_no = n
            self._write_manifest()
            log.info("item %s reordered: %s", item_id, [by_seq[s].stem for s in seqs])
            return [by_seq[s] for s in seqs]

    # ---- apply -------------------------------------------------------------------------

    def start_apply(self, force: bool) -> None:
        with self.lock:
            if self.apply_state["running"]:
                raise RuntimeError("apply is already running")
            leftovers = [p for p in self.paths.sort_image.iterdir() if p.is_file()] if self.paths.sort_image.is_dir() else []
            if leftovers and not force:
                raise FileExistsError(f"sort_image already contains {len(leftovers)} file(s)")
            kept = sum(1 for r in self.rows if not r.skip)
            self.apply_state = {"running": True, "done": 0, "total": kept, "written": None, "error": None}
            rows = list(self.rows)

        def progress(done: int, total: int) -> None:
            self.apply_state["done"], self.apply_state["total"] = done, total

        def work() -> None:
            try:
                n = apply_batch(self.paths, rows, force=force, progress=progress)
                self.apply_state.update(written=n)
            except Exception as e:  # surfaced to the page; the CLI log has the traceback
                log.exception("apply failed")
                self.apply_state.update(error=str(e))
            finally:
                self.apply_state["running"] = False

        threading.Thread(target=work, name="apply", daemon=True).start()


def _log_draft(paths: BatchPaths, item_id: int, facts: str, result) -> None:
    """One readable file per draft under cache/drafts/ - the human-visible history of what was generated."""
    from datetime import datetime

    d = result.draft
    folder = paths.cache / "drafts"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        f"item {item_id} of batch {paths.batch}\n"
        f"when:    {stamp}\n"
        f"engine:  Claude Code (subscription, credential source {result.credential!r})\n"
        f"facts:   {facts.strip() or '(none)'}\n"
        f"fields:  brand={d.brand!r} size={d.size!r} material={d.material!r} color={d.color!r} "
        f"condition={d.condition!r} measurements={d.measurements!r} category={d.category_hint!r} "
        f"missing={d.missing}\n"
        f"tip:     {d.tip or '(none)'}\n"
        f"chars:   {len(d.text)}\n"
        f"\n----- description -----\n{d.text}\n"
    )
    name = f"item{item_id:03d}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    (folder / name).write_text(body, encoding="utf-8")


class ReviewServer:
    """All batches: sessions are created on demand, grouping jobs run per batch."""

    def __init__(self) -> None:
        self.sessions: dict[str, ReviewSession] = {}
        self.group_state: dict[str, dict] = {}
        self.lock = threading.Lock()
        self._lister = None
        self.describe_state: dict[str, dict] = {}
        self.style_job: dict = {"running": False, "error": None, "summary": None, "diff": None, "markdown": None}
        self.started_at = datetime.now()
        self.code_mtime = code_mtime()          # to spot an update while the app is running
        self.last_request = time.monotonic()
        self.closing_armed: float | None = None
        self.handoff_at = 0.0                   # when the Depop helper was last handed an item
        self.update_job: dict = {"running": False, "phase": None, "error": None, "installed": None, "restart": None}

    # ---- selling ---------------------------------------------------------------------

    @property
    def lister(self):
        if self._lister is None:
            from .listing import Lister
            self._lister = Lister()
        return self._lister

    def sell_state(self, batch: str | None) -> dict:
        s = self.session(batch)
        with s.lock:
            grouped = items(s.rows)
            conn = db.connect()
            try:
                db.sync_from_manifest(conn, s.paths.batch, s.rows)
                meta = db.item_rows(conn, s.paths.batch)
            finally:
                conn.close()
        out = []
        dstate = self.describe_state.get(s.paths.batch, {})
        for item_id, group in grouped.items():
            m = meta.get(item_id, {})
            out.append({
                "id": item_id,
                "photos": [{"seq": r.seq, "stem": r.stem, "file": r.raw_file, "view": r.view} for r in group],
                "status": m.get("status") or "sorted",
                "description": m.get("description") or "",
                "depop_url": m.get("depop_url") or "",
                "facts": m.get("facts") or "",
                "fields": {k: m.get(k) or "" for k in ("brand", "size", "material", "color", "condition", "category", "measurements")},
                "job": self.lister.status(f"{s.paths.batch}/{item_id}") if self._lister else None,
                "draft": dstate.get(item_id),
            })
        from .describe import find_claude_cli
        return {"batch": s.paths.batch, "max_photos": MAX_PHOTOS_PER_ITEM, "items": out,
                "claude_code": find_claude_cli() is not None}

    def start_describe(self, batch: str | None, item_ids: list[int], facts: dict) -> dict:
        """Draft descriptions for the given items, one after another, in a background thread.

        Only the logged-in Claude Code CLI (the user's subscription) is used; the Anthropic API is
        reserved for photo grouping and is never called from here.
        """
        from .describe import draft_description, find_claude_cli, load_style

        s = self.session(batch)
        cli = find_claude_cli()
        if cli is None:
            raise RuntimeError("Claude Code CLI not found on this machine (install the Claude desktop app)")
        style = load_style()
        with s.lock:
            grouped = items(s.rows)
        todo = [i for i in item_ids if i in grouped]
        if not todo:
            raise ValueError("no such items")
        dstate = self.describe_state.setdefault(s.paths.batch, {})
        conn = db.connect()
        try:
            db.sync_from_manifest(conn, s.paths.batch, s.rows)
            for i in todo:
                if str(i) in facts or i in facts:
                    db.update_item(conn, s.paths.batch, i, facts=str(facts.get(str(i), facts.get(i, "")) or ""))
            meta = db.item_rows(conn, s.paths.batch)
        finally:
            conn.close()
        for i in todo:
            if dstate.get(i, {}).get("running"):
                raise RuntimeError(f"item {i} is already being drafted")
            dstate[i] = {"running": True, "error": None, "cost": None, "queued": True}

        def work() -> None:
            for i in todo:
                dstate[i]["queued"] = False
                try:
                    item_facts = meta.get(i, {}).get("facts") or ""
                    result = draft_description(s.paths, grouped[i], item_facts, large_image=s.large_image,
                                               style=style, cli=cli)
                    d = result.draft
                    conn = db.connect()
                    try:
                        db.update_item(conn, s.paths.batch, i, description=d.text, brand=d.brand, size=d.size,
                                       color=d.color, condition=d.condition, category=d.category_hint,
                                       measurements=d.measurements, material=d.material)
                    finally:
                        conn.close()
                    dstate[i].update(cost=None, missing=d.missing, chars=len(d.text), tip=d.tip, engine="claude-code",
                                     credential=result.credential)
                    log.info("drafted item %s (%d chars, missing %s, credential source %r)",
                             i, len(d.text), d.missing, result.credential)
                    _log_draft(s.paths, i, item_facts, result)
                except Exception as e:
                    log.exception("drafting item %s failed", i)
                    dstate[i]["error"] = str(e)
                finally:
                    dstate[i]["running"] = False

        threading.Thread(target=work, name="describe", daemon=True).start()
        return {"started": todo}

    def sell_open(self, batch: str | None, item_id: int) -> dict:
        """The fallback for when the Chrome helper is missing: an automated browser window. Only
        installs that added the optional Playwright library have it; everyone else is sent to the
        helper, which is the way that works."""
        import importlib.util

        if importlib.util.find_spec("playwright") is None:
            raise RuntimeError("Add the Chrome helper first (Settings tab → Chrome helper). "
                               "It puts the photos and the description into Depop's form for you.")
        self.handoff_at = time.monotonic()
        s = self.session(batch)
        with s.lock:
            group = items(s.rows).get(item_id)
        if not group:
            raise LookupError(f"item {item_id} not found")
        stems = [r.stem for r in group[:MAX_PHOTOS_PER_ITEM]]
        conn = db.connect()
        try:
            db.sync_from_manifest(conn, s.paths.batch, s.rows)
            meta = db.item_rows(conn, s.paths.batch).get(item_id, {})
            if meta.get("status") in (None, "sorted"):
                db.update_item(conn, s.paths.batch, item_id, status="opened")
        finally:
            conn.close()
        description = meta.get("description") or ""
        return self.lister.submit(f"{s.paths.batch}/{item_id}", description=description,
                                  prepare=lambda: [s.large_image(stem) for stem in stems])

    def sell_photos(self, batch: str | None, item_id: int, base_url: str) -> dict:
        self.handoff_at = time.monotonic()
        """Upload list for the extension: converts the item's photos (max 8) and returns their URLs."""
        s = self.session(batch)
        with s.lock:
            group = items(s.rows).get(item_id)
        if not group:
            raise LookupError(f"item {item_id} not found")
        photos = []
        for r in group[:MAX_PHOTOS_PER_ITEM]:
            s.large_image(r.stem)  # convert now so the extension's fetches are instant
            photos.append({"name": f"{item_id}_{r.photo_no}.jpg",
                           "url": f"{base_url}/large/{r.stem}.jpg?batch={s.paths.batch}"})
        conn = db.connect()
        try:
            db.sync_from_manifest(conn, s.paths.batch, s.rows)
            description = (db.item_rows(conn, s.paths.batch).get(item_id) or {}).get("description") or ""
        finally:
            conn.close()
        return {"batch": s.paths.batch, "item": item_id, "photos": photos, "description": description}

    def sell_update(self, batch: str | None, item_id: int, body: dict) -> dict:
        s = self.session(batch)
        fields = {k: body[k] for k in ("status", "description", "depop_url", "facts") if k in body}
        if "status" in fields and fields["status"] not in ("sorted", "opened", "listed"):
            raise ValueError("status must be sorted, opened or listed")
        conn = db.connect()
        try:
            db.sync_from_manifest(conn, s.paths.batch, s.rows)
            db.update_item(conn, s.paths.batch, item_id, **fields)
            return db.item_rows(conn, s.paths.batch).get(item_id, {})
        finally:
            conn.close()

    def batches(self) -> dict:
        out = []
        if config.PRODUCT_IMAGE_DIR.is_dir():
            conn = db.connect()
            try:
                for d in reversed(config.batch_dirs()):          # most recently used first
                    b = BatchPaths(d.name, d)
                    raw_files = [p for p in b.raw.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES]
                    entry = {
                        "name": d.name,
                        "photos": len(raw_files),
                        "updated": datetime.fromtimestamp(b.raw.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                        "has_manifest": b.manifest.exists(),
                        "sorted": sum(1 for p in b.sort_image.glob("*.jpg")) if b.sort_image.is_dir() else 0,
                        "items": 0, "listed": 0,
                    }
                    if entry["has_manifest"]:
                        try:
                            entry["items"] = len(items(self.session(d.name).rows))
                            entry["listed"] = sum(1 for m in db.item_rows(conn, d.name).values() if m.get("status") == "listed")
                        except Exception as e:  # a broken manifest must not hide the whole list
                            entry["error"] = str(e)
                    out.append(entry)
            finally:
                conn.close()
        from .group import COST_PER_PHOTO
        return {"batches": out, "api_key": load_api_key() is not None, "models": MODELS,
                "cost_per_photo": COST_PER_PHOTO, "default_model": DEFAULT_MODEL,
                "extension_dir": str(config.PROJECT_ROOT / "chrome_extension"),
                "product_image_dir": str(config.PRODUCT_IMAGE_DIR)}

    def new_batch(self, name: str) -> dict:
        name = safe_batch_name(name)
        root = config.PRODUCT_IMAGE_DIR / name
        if root.exists():
            raise ValueError(f"a batch called {name} already exists")
        (root / "raw_image").mkdir(parents=True)
        log.info("created batch %s", name)
        return {"name": name, "raw": str(root / "raw_image")}

    def rename_batch(self, name: str, to: str) -> dict:
        """Rename a batch folder and carry its rows in the DB across."""
        to = safe_batch_name(to)
        paths = batch_paths(name)
        if to == paths.batch:
            return {"name": to}
        target = config.PRODUCT_IMAGE_DIR / to
        if target.exists():
            raise ValueError(f"a batch called {to} already exists")
        with self.lock:
            if self.group_state.get(paths.batch, {}).get("running"):
                raise RuntimeError("this batch is being grouped right now")
            s = self.sessions.get(paths.batch)
            if s is not None and s.apply_state["running"]:
                raise RuntimeError("this batch is being exported right now")
            paths.root.rename(target)
            self.sessions.pop(paths.batch, None)
            self.group_state.pop(paths.batch, None)
            self.describe_state[to] = self.describe_state.pop(paths.batch, {})
        conn = db.connect()
        try:
            with conn:
                conn.execute("UPDATE items SET batch = ? WHERE batch = ?", (to, paths.batch))
                conn.execute("UPDATE photos SET batch = ? WHERE batch = ?", (to, paths.batch))
        finally:
            conn.close()
        log.info("renamed batch %s to %s", paths.batch, to)
        return {"name": to}

    def add_photo(self, batch: str | None, name: str, data: bytes, replace: bool = False) -> dict:
        """Write one uploaded photo into the batch's raw_image folder."""
        name = safe_photo_name(name)
        paths = batch_paths(batch)
        dest = paths.raw / name
        if dest.exists() and not replace:
            return {"name": name, "written": False, "reason": "a photo with this name is already in the batch"}
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return {"name": name, "written": True, "bytes": len(data)}

    def import_folder(self, batch: str | None, folder: str) -> dict:
        """Copy every photo from a folder on this computer into the batch (the originals stay put)."""
        src = Path(folder.strip().strip('"')).expanduser()
        if not src.is_dir():
            raise FileNotFoundError(f"{src} is not a folder on this computer")
        paths = batch_paths(batch)
        if src.resolve() == paths.raw.resolve():
            raise ValueError("that folder is the batch itself")
        copied, skipped = [], []
        for f in sorted(src.iterdir()):
            if not f.is_file() or f.suffix.lower() not in PHOTO_SUFFIXES:
                continue
            dest = paths.raw / f.name
            if dest.exists():
                skipped.append(f.name)
                continue
            shutil.copy2(f, dest.with_name(dest.name + ".part"))
            dest.with_name(dest.name + ".part").replace(dest)
            copied.append(f.name)
        log.info("imported %d photo(s) into %s from %s", len(copied), paths.batch, src)
        return {"batch": paths.batch, "copied": len(copied), "skipped": len(skipped), "from": str(src)}

    def batch_cover(self, batch: str | None) -> bytes:
        """A small JPEG of the batch's first photo, for the batches page. Made once, then cached."""
        from .scan import make_thumbnail

        paths = batch_paths(batch)
        photos = sorted(p for p in paths.raw.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES)
        if not photos:
            raise LookupError(f"{paths.batch} has no photos yet")
        out = paths.cache / f"cover-{photos[0].stem}.jpg"
        if not out.is_file():
            make_thumbnail(photos[0], out)
        return out.read_bytes()

    def open_batch_folder(self, name: str) -> dict:
        raw = batch_paths(name).raw
        desktop.open_folder(raw)     # Explorer / Finder, local machine only
        return {"opened": str(raw)}

    def session(self, batch: str | None) -> ReviewSession:
        paths = batch_paths(batch)
        with self.lock:
            s = self.sessions.get(paths.batch)
            if s is not None and s._mtime() != s.manifest_mtime and not s.apply_state["running"]:
                log.info("manifest.csv for %s changed on disk; reloading", paths.batch)
                s = None  # picked up fresh below
            if s is None:
                if not paths.manifest.exists():
                    raise LookupError(f"batch {paths.batch} has no manifest yet - run the grouping first")
                s = ReviewSession(paths, read_manifest(paths.manifest))
                self.sessions[paths.batch] = s
            return s

    def suggestions(self, batch: str | None) -> list[dict]:
        f = batch_paths(batch).cache / "suggestions.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []

    def dismiss_suggestion(self, batch: str | None, a_stem: str, b_stem: str) -> list[dict]:
        f = batch_paths(batch).cache / "suggestions.json"
        kept = [s for s in self.suggestions(batch) if {s["a_stem"], s["b_stem"]} != {a_stem, b_stem}]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(kept, indent=1), encoding="utf-8")
        return kept

    # ---- closing ---------------------------------------------------------------------

    def note_request(self) -> None:
        self.last_request = time.monotonic()

    def busy(self) -> str | None:
        """What is still going on, if anything - work that must outlive a closed window."""
        if any(st.get("running") for st in self.group_state.values()):
            return "grouping photos"
        if any(d.get("running") for batch in self.describe_state.values() for d in batch.values()):
            return "writing a description"
        if self.style_job.get("running"):
            return "rewriting the style"
        if any(s.apply_state["running"] for s in self.sessions.values()):
            return "exporting photos"
        if self.update_job.get("running"):
            return "installing an update"
        if time.monotonic() - self.handoff_at < HANDOFF_QUIET_S:
            return "sending photos to Depop"      # the helper may still be fetching them
        return None

    def arm_close(self, http_server, grace: float | None = None) -> dict:
        """The window is going away. Stop the app shortly, unless it turns out we are still needed:
        moving between tabs also closes a page, and the new one arrives within milliseconds."""
        armed_at = time.monotonic()
        self.closing_armed = armed_at
        first_wait = CLOSE_GRACE_S if grace is None else grace

        def watch() -> None:
            wait = first_wait
            while True:
                time.sleep(wait)
                wait = CLOSE_GRACE_S
                if self.closing_armed != armed_at:
                    return                                   # armed again by a later page
                if self.last_request > armed_at:
                    log.debug("window came back (another tab) - staying up")
                    self.closing_armed = None
                    return
                reason = self.busy()
                if reason is None:
                    log.info("the app window was closed - stopping")
                    threading.Thread(target=http_server.shutdown, name="closed", daemon=True).start()
                    return
                log.info("window closed, but still %s - staying up", reason)

        threading.Thread(target=watch, name="closing", daemon=True).start()
        return {"closing_in": first_wait}

    # ---- updating the app itself ---------------------------------------------------------

    def start_update(self, http_server) -> dict:
        """Install the newest release in a background thread, then hand over to it: on Windows a
        fresh copy of the app starts and waits for this one to let go of the port; elsewhere the
        app closes and the user opens it again. What to install is asked of GitHub here, never
        taken from the request."""
        from . import updates

        with self.lock:
            if self.update_job.get("running"):
                raise RuntimeError("the update is already being installed")
            reason = self.busy()
            if reason:
                raise RuntimeError(f"Depop Seller is still {reason} - update once that has finished.")
            self.update_job = {"running": True, "phase": "Checking", "error": None, "installed": None, "restart": None}
        job = self.update_job
        port = http_server.server_address[1]

        def work() -> None:
            try:
                found = updates.check()
                if not found.get("ok"):
                    raise RuntimeError(found.get("reason") or "could not check for updates")
                if not found["newer"]:
                    raise RuntimeError(f"You already have the newest version ({found['current']}).")
                if not found["can_install"]:
                    raise RuntimeError(found["why_not"])
                out = updates.install(found["download"], progress=lambda phase: job.update(phase=phase))
                if updates.can_restart():
                    updates.spawn_replacement(port)
                    job.update(installed=out["installed"], phase="Restarting", restart="auto")
                else:
                    job.update(installed=out["installed"], phase="Done", restart="manual")
            except Exception as e:
                log.exception("update failed")
                job["error"] = str(e)
            finally:
                job["running"] = False
            if job.get("installed"):
                time.sleep(1.5)                  # long enough for the page to read the final state
                log.info("stopping so version %s can take over", job["installed"])
                http_server.shutdown()

        threading.Thread(target=work, name="update", daemon=True).start()
        return job

    # ---- description style ---------------------------------------------------------

    def style_state(self) -> dict:
        from . import style
        from .describe import find_claude_cli

        return {"text": style.read_style(), "history": style.history(),
                "claude_code": find_claude_cli() is not None, "job": self.style_job}

    def save_style(self, text: str) -> dict:
        from . import style

        out = style.save_style(text)
        self.style_job = {"running": False, "error": None, "summary": None, "diff": None, "markdown": None}
        return out | {"history": style.history()}

    def restore_style(self, name: str) -> dict:
        from . import style

        return style.restore(name) | {"history": style.history(), "text": style.read_style()}

    def start_style_edit(self, instruction: str) -> dict:
        """Ask Claude Code to apply an instruction to the style file, in a background thread."""
        from . import style

        with self.lock:
            if self.style_job.get("running"):
                raise RuntimeError("a style change is already being worked out")
            self.style_job = {"running": True, "error": None, "summary": None, "diff": None,
                              "markdown": None, "instruction": instruction.strip()}
        job = self.style_job

        def work() -> None:
            try:
                out = style.propose(instruction)
                job.update(summary=out["summary"], diff=out["diff"], markdown=out["markdown"])
                log.info("style: proposal ready (%s)", out["summary"])
            except Exception as e:
                log.exception("style rewrite failed")
                job["error"] = str(e)
            finally:
                job["running"] = False

        threading.Thread(target=work, name="style", daemon=True).start()
        return job

    def start_group(self, batch: str | None, model: str) -> dict:
        from .group import Usage, group_batch

        paths = batch_paths(batch)
        if model not in MODELS:
            raise ValueError(f"unknown model {model!r}")
        if load_api_key() is None:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (put it in .env)")
        with self.lock:
            st = self.group_state.get(paths.batch)
            if st and st["running"]:
                raise RuntimeError("grouping is already running for this batch")
            st = {"running": True, "phase": "starting", "done": 0, "total": 0, "cost": None, "error": None}
            self.group_state[paths.batch] = st

        def progress(phase: str, done: int, total: int) -> None:
            st.update(phase=phase, done=done, total=total)

        def work() -> None:
            from .scan import scan_batch
            try:
                photos = scan_batch(paths, progress=progress)
                usage = Usage()
                rows = group_batch(paths, photos, model=model, usage=usage, progress=progress)
                if paths.manifest.exists():
                    shutil.copy2(paths.manifest, paths.manifest.with_name("manifest.bak.csv"))
                write_manifest(paths.manifest, rows)
                with self.lock:
                    self.sessions[paths.batch] = ReviewSession(paths, rows)
                st.update(phase="done", cost=usage.cost(model))
                log.info("grouped %s with %s: %s (about $%.2f)", paths.batch, model, summary(rows), usage.cost(model) or 0)
            except Exception as e:
                log.exception("grouping failed")
                st.update(error=str(e))
            finally:
                st["running"] = False

        threading.Thread(target=work, name="group", daemon=True).start()
        return st


# The helper extension runs on depop.com and fetches the item's photos and description from this
# server, so those routes need CORS - and only those. Everything else (including every route that
# writes) stays same-origin, or any web page the seller has open could drive the server.
DEPOP_ORIGINS = ("https://www.depop.com", "https://depop.com")
CORS_PATHS = ("/sell/photos", "/sell/item", "/large/")


class ReviewHandler(BaseHTTPRequestHandler):
    server_state: ReviewServer  # injected via functools.partial

    def __init__(self, server_state: ReviewServer, *args, **kwargs) -> None:
        self.server_state = server_state
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        allowed = self._cors_origin()
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
        self.end_headers()
        self.wfile.write(body)

    def _cors_origin(self) -> str | None:
        """The Origin to allow for this request: depop.com, on the routes the extension uses."""
        origin = self.headers.get("Origin")
        path = urlsplit(self.path).path
        return origin if origin in DEPOP_ORIGINS and path.startswith(CORS_PATHS) else None

    def _foreign_origin(self) -> bool:
        """True when a POST comes from a page that is not ours and not the extension's depop.com."""
        origin = self.headers.get("Origin")
        if origin is None:                        # the extension's fetch and our own pages on same-origin
            return False
        if origin in DEPOP_ORIGINS:
            return not urlsplit(self.path).path.startswith(CORS_PATHS)
        host = urlsplit(origin).hostname
        return host not in ("127.0.0.1", "localhost", "::1")

    def do_OPTIONS(self) -> None:  # noqa: N802 - CORS preflight for the extension's POSTs
        allowed = self._cors_origin()
        self.send_response(HTTPStatus.NO_CONTENT if allowed else HTTPStatus.FORBIDDEN)
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    def _text(self, text: str, status: HTTPStatus) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _route(self) -> tuple[str, str | None]:
        parts = urlsplit(self.path)
        batch = (parse_qs(parts.query).get("batch") or [None])[0]
        return parts.path, batch

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        path, batch = self._route()
        self.server_state.note_request()
        try:
            if path == "/":
                page = (files("depop_seller") / "static" / "review.html").read_bytes()
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif path == "/sell":
                page = (files("depop_seller") / "static" / "sell.html").read_bytes()
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif path == "/hub":
                page = (files("depop_seller") / "static" / "hub.html").read_bytes()
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif path == "/style":
                page = (files("depop_seller") / "static" / "style.html").read_bytes()
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif path == "/version":
                self._json({"version": __version__, "pid": os.getpid(),
                            "started": self.server_state.started_at.strftime("%Y-%m-%d %H:%M"),
                            "stale": code_mtime() > self.server_state.code_mtime,
                            "platform": {"win32": "windows", "darwin": "mac"}.get(sys.platform, "linux")})
            elif path == "/updates/status":
                self._json(self.server_state.update_job)
            elif path == "/settings":
                page = (files("depop_seller") / "static" / "settings.html").read_bytes()
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif path == "/settings/state":
                from . import settings as app_settings
                self._json(app_settings.state())
            elif path == "/settings/login/status":
                from . import settings as app_settings
                self._json(app_settings.SIGN_IN.status())
            elif path == "/style/state":
                self._json(self.server_state.style_state())
            elif path == "/style/status":
                self._json(self.server_state.style_job)
            elif path == "/batches/cover":
                self._send(HTTPStatus.OK, self.server_state.batch_cover(batch), "image/jpeg")
            elif path == "/app.css":         # the look every page shares
                css = (files("depop_seller") / "static" / "app.css").read_bytes()
                self._send(HTTPStatus.OK, css, "text/css; charset=utf-8")
            elif path == "/icon.png":        # the app window's dock / taskbar icon
                icon = (files("depop_seller") / "static" / "icon.png").read_bytes()
                self._send(HTTPStatus.OK, icon, "image/png")
            elif path == "/sell/testform":   # stand-in for Depop's form, used by the listing smoke test
                self._send(HTTPStatus.OK, TEST_FORM.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/sell/state":
                self._json(self.server_state.sell_state(batch))
            elif path == "/sell/status":
                item = int((parse_qs(urlsplit(self.path).query).get("item") or ["0"])[0])
                self._json(self.server_state.lister.status(f"{batch_paths(batch).batch}/{item}"))
            elif path == "/sell/describe/status":
                self._json(self.server_state.describe_state.get(batch_paths(batch).batch, {}))
            elif path == "/sell/photos":
                item = int((parse_qs(urlsplit(self.path).query).get("item") or ["0"])[0])
                host = self.headers.get("Host") or "127.0.0.1:8765"
                self._json(self.server_state.sell_photos(batch, item, f"http://{host}"))
            elif path == "/batches":
                self._json(self.server_state.batches())
            elif path == "/state":
                self._json(self.server_state.session(batch).state())
            elif path == "/apply/status":
                self._json(self.server_state.session(batch).apply_state)
            elif path == "/group/status":
                self._json(self.server_state.group_state.get(batch_paths(batch).batch) or {"running": False})
            elif path == "/suggestions":
                self._json(self.server_state.suggestions(batch))
            elif path.startswith("/thumbs/") or path.startswith("/large/"):
                kind, name = path[1:].split("/", 1)
                if "/" in name or "\\" in name or not name.endswith(".jpg"):
                    self._text("not found", HTTPStatus.NOT_FOUND)
                    return
                if kind == "thumbs":
                    file = self.server_state.session(batch).thumb_image(name[:-4])
                else:
                    file = self.server_state.session(batch).large_image(name[:-4])
                if file is None:
                    self._text("not found", HTTPStatus.NOT_FOUND)
                    return
                self._send(HTTPStatus.OK, file.read_bytes(), "image/jpeg")
            else:
                self._text("not found", HTTPStatus.NOT_FOUND)
        except (LookupError, FileNotFoundError) as e:
            self._text(str(e), HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path, batch = self._route()
        self.server_state.note_request()
        if self._foreign_origin():
            # Read a small refused body first: Windows resets a connection closed with unread data,
            # and the caller then sees the connection abort instead of this refusal.
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= 1024 * 1024:
                self.rfile.read(length)
            self._text("this server only accepts requests from its own pages", HTTPStatus.FORBIDDEN)
            return
        try:
            if path == "/save":
                self._json(self.server_state.session(batch).save(self._body()))
            elif path == "/apply":
                self.server_state.session(batch).start_apply(bool(self._body().get("force")))
                self._json({"started": True}, HTTPStatus.ACCEPTED)
            elif path == "/group":
                model = self._body().get("model") or DEFAULT_MODEL
                self._json(self.server_state.start_group(batch, model), HTTPStatus.ACCEPTED)
            elif path == "/batches/new":
                self._json(self.server_state.new_batch(str(self._body().get("name") or "")))
            elif path == "/batches/open":
                self._json(self.server_state.open_batch_folder(str(self._body().get("name") or "")))
            elif path.startswith("/settings/"):
                from . import settings as app_settings

                if path == "/settings/key":
                    self._json(app_settings.save_api_key(str(self._body().get("key") or "")))
                elif path == "/settings/key/test":
                    self._json(app_settings.test_api_key())
                elif path == "/settings/login":
                    self._json(app_settings.SIGN_IN.start())
                elif path == "/settings/login/code":
                    self._json(app_settings.SIGN_IN.send_code(str(self._body().get("code") or "")))
                elif path == "/settings/login/terminal":
                    self._json(app_settings.open_login_terminal())
                elif path == "/settings/login/test":
                    self._json(app_settings.test_claude_login())
                elif path == "/settings/share":
                    from .share import make_share_zip

                    self._json(make_share_zip())
                else:
                    self._text("not found", HTTPStatus.NOT_FOUND)
            elif path == "/closing":
                self._json(self.server_state.arm_close(self.server))
            elif path == "/updates/check":
                from . import updates

                self._json(updates.check())
            elif path == "/updates/install":
                self._json(self.server_state.start_update(self.server), HTTPStatus.ACCEPTED)
            elif path == "/quit":
                log.info("quit requested from the app")
                self._json({"stopping": True})
                threading.Thread(target=self.server.shutdown, name="quit", daemon=True).start()
            elif path == "/style/save":
                self._json(self.server_state.save_style(str(self._body().get("text") or "")))
            elif path == "/style/instruct":
                self._json(self.server_state.start_style_edit(str(self._body().get("instruction") or "")),
                           HTTPStatus.ACCEPTED)
            elif path == "/style/restore":
                self._json(self.server_state.restore_style(str(self._body().get("name") or "")))
            elif path == "/batches/rename":
                body = self._body()
                self._json(self.server_state.rename_batch(str(body.get("name") or ""), str(body.get("to") or "")))
            elif path == "/batches/import":
                body = self._body()
                self._json(self.server_state.import_folder(batch, str(body.get("folder") or "")))
            elif path == "/batches/photos":
                query = parse_qs(urlsplit(self.path).query)
                name = (query.get("name") or [""])[0]
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    raise ValueError("no photo in the request")
                if length > MAX_UPLOAD_BYTES:
                    raise ValueError(f"{name} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
                data = self.rfile.read(length)
                replace = (query.get("replace") or ["0"])[0] == "1"
                self._json(self.server_state.add_photo(batch, name, data, replace))
            elif path == "/sell/describe":
                body = self._body()
                self._json(self.server_state.start_describe(
                    batch, [int(i) for i in body.get("items") or []], body.get("facts") or {}), HTTPStatus.ACCEPTED)
            elif path in ("/sell/open", "/sell/item", "/sell/reorder"):
                item = int((parse_qs(urlsplit(self.path).query).get("item") or ["0"])[0])
                if path == "/sell/open":
                    self._json(self.server_state.sell_open(batch, item), HTTPStatus.ACCEPTED)
                elif path == "/sell/reorder":
                    seqs = [int(x) for x in self._body().get("seqs") or []]
                    rows = self.server_state.session(batch).reorder_item(item, seqs)
                    self._json({"photos": [{"seq": r.seq, "stem": r.stem, "file": r.raw_file, "view": r.view} for r in rows]})
                else:
                    self._json(self.server_state.sell_update(batch, item, self._body()))
            elif path == "/suggestions/dismiss":
                body = self._body()
                self._json(self.server_state.dismiss_suggestion(batch, str(body.get("a_stem")), str(body.get("b_stem"))))
            else:
                self._text("not found", HTTPStatus.NOT_FOUND)
        except FileExistsError as e:
            self._text(str(e), HTTPStatus.CONFLICT)
        except (LookupError, FileNotFoundError) as e:
            self._text(str(e), HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError) as e:
            self._text(str(e), HTTPStatus.BAD_REQUEST)


class _Server(ThreadingHTTPServer):
    # HTTPServer enables SO_REUSEADDR, which on Windows lets a second process bind the same
    # port instead of failing. We want a hard failure so a double launch is detected.
    allow_reuse_address = False


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _bind(port: int, state: ReviewServer, wait_s: float = 0) -> _Server | None:
    """Take the port, or None if another copy of the app has it. After an update the new copy
    waits here for the old one to finish handing over."""
    deadline = time.monotonic() + wait_s
    while True:
        if not _port_in_use(port):
            try:
                return _Server(("127.0.0.1", port), partial(ReviewHandler, state))
            except OSError:
                pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def serve(batch: str | None = None, port: int = 8765, open_browser: bool = True, page: str = "",
          quiet: bool = False, wait_for_port: float = 0) -> None:
    url = f"http://127.0.0.1:{port}/{page}" + (f"?batch={batch}" if batch else "")
    state = ReviewServer()
    server = _bind(port, state, wait_for_port)
    if server is None:
        # Most likely a window is already running (e.g. the icon was double-clicked twice).
        print(f"port {port} is busy - the app is probably already open; showing it: {url}")
        if open_browser:
            desktop.app_window(url)
        return
    stop = "stop it from the app's Settings tab" if quiet else "close this window or press Ctrl+C to stop"
    print(f"depop_seller: {url}   ({stop})")
    if wait_for_port:
        # Started by an update, in place of the copy that installed it: that copy's window
        # reconnects within seconds. If it never does, do not linger unseen in the background.
        state.arm_close(server, grace=60)
    if open_browser:
        print(f"opened an app window with {desktop.app_window(url)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
