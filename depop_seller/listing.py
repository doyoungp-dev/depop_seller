"""Open Depop's "List an item" page with an item's photos already added.

A single worker thread owns one Playwright browser (the installed Google Chrome with its own
profile under .chrome-profile/, so the Depop login survives between runs). Jobs are queued;
each one opens a new tab, uploads the photos in the reviewer's order, pastes the description
if there is one, and leaves the tab open for the human to finish and post. Nothing is ever
posted automatically.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

CREATE_URL = "https://www.depop.com/products/create/"
PROFILE_DIR = PROJECT_ROOT / ".chrome-profile"
LOGIN_WAIT_S = 15 * 60          # how long to wait for the human to log in
FILE_INPUT = "input[type=file]"
DESCRIPTION_FIELDS = ["textarea[name='description']", "textarea#description", "textarea"]


@dataclass
class Job:
    key: str                     # "<batch>/<item>"
    photos: list[Path]           # JPEGs in upload order (cover first)
    description: str = ""
    url: str = CREATE_URL        # overridable for tests
    prepare: Callable[[], list[Path]] | None = None   # runs in the worker; returns the JPEGs
    state: dict = field(default_factory=lambda: {"running": True, "phase": "queued", "error": None, "uploaded": 0})


class Lister:
    """Queue of listing jobs processed by one browser-owning thread."""

    def __init__(self, profile_dir: Path = PROFILE_DIR, headless: bool = False) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.jobs: dict[str, Job] = {}
        self._queue: queue.Queue[Job] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pw = None
        self._context = None

    # ---- public ------------------------------------------------------------------------

    def submit(self, key: str, photos: list[Path] | None = None, description: str = "", url: str = CREATE_URL,
               prepare: Callable[[], list[Path]] | None = None) -> dict:
        with self._lock:
            existing = self.jobs.get(key)
            if existing and existing.state["running"]:
                raise RuntimeError("this item is already being opened")
            job = Job(key=key, photos=photos or [], description=description, url=url, prepare=prepare)
            self.jobs[key] = job
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="lister", daemon=True)
                self._thread.start()
        self._queue.put(job)
        return job.state

    def status(self, key: str) -> dict:
        job = self.jobs.get(key)
        return dict(job.state) if job else {"running": False, "phase": "idle", "error": None, "uploaded": 0}

    # ---- worker ------------------------------------------------------------------------

    def _worker(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            self._pw = pw
            while True:
                job = self._queue.get()
                try:
                    self._run(job)
                except Exception as e:  # reported to the page; traceback in the console
                    log.exception("listing %s failed", job.key)
                    job.state.update(error=str(e), phase="failed")
                finally:
                    job.state["running"] = False

    def _browser(self):
        """The persistent Chrome context, (re)launched if the human closed the window."""
        if self._context is not None:
            return self._context
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for channel in ("chrome", "msedge", None):
            try:
                # Fallback path only (the Chrome extension is preferred): drop the automation
                # banner and the navigator.webdriver flag, which bot protection keys on.
                kwargs = {"headless": self.headless, "viewport": None,
                          "args": ["--start-maximized", "--disable-blink-features=AutomationControlled"],
                          "ignore_default_args": ["--enable-automation"]}
                if channel:
                    kwargs["channel"] = channel
                ctx = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kwargs)
                ctx.on("close", self._on_closed)   # fires when the human closes the window
                self._context = ctx
                log.info("listing browser started (%s)", channel or "bundled chromium")
                return ctx
            except Exception as e:
                last_error = e
        raise RuntimeError(f"could not start a browser (install Google Chrome, or run "
                           f"`.venv/Scripts/playwright install chromium`): {last_error}")

    def _on_closed(self, *_: object) -> None:
        self._context = None

    def _new_page(self):
        try:
            return self._browser().new_page()
        except Exception:                # window closed between jobs but no close event yet
            self._context = None
            return self._browser().new_page()

    def _run(self, job: Job) -> None:
        if job.prepare is not None:
            job.state.update(phase="converting photos")
            job.photos = job.prepare()
        if not job.photos:
            raise RuntimeError("this item has no photos")
        job.state.update(phase="opening browser")
        page = self._new_page()
        page.goto(job.url, wait_until="domcontentloaded")

        # Depop redirects to login when the profile has no session: wait for the human.
        job.state.update(phase="waiting for the listing form (log in to Depop in the window if it asks)")
        deadline = time.monotonic() + LOGIN_WAIT_S
        while page.locator(FILE_INPUT).count() == 0:
            if time.monotonic() > deadline:
                raise RuntimeError("gave up waiting for Depop's listing form (not logged in?)")
            if "/products/create" not in page.url and "login" not in page.url and "depop.com" in page.url:
                page.goto(job.url, wait_until="domcontentloaded")   # logged in now, back to the form
            page.wait_for_timeout(1500)

        job.state.update(phase="uploading photos")
        self._upload(page, job)
        if job.description.strip():
            job.state.update(phase="pasting description")
            self._fill_description(page, job.description)
        job.state.update(phase="done - finish the listing in the Chrome window and press Post yourself")

    def _upload(self, page, job: Job) -> None:
        files = [str(p) for p in job.photos]
        inputs = page.locator(FILE_INPUT)
        first = inputs.first
        if inputs.count() == 1 and first.get_attribute("multiple") is not None:
            first.set_input_files(files)
            job.state["uploaded"] = len(files)
            page.wait_for_timeout(1500)
            return
        # One slot at a time: re-query each round because the page re-renders after every upload.
        for i, f in enumerate(files):
            inputs = page.locator(FILE_INPUT)
            n = inputs.count()
            if n == 0:
                raise RuntimeError(f"file input disappeared after {i} photo(s)")
            target = inputs.nth(min(i, n - 1))
            target.set_input_files(f)
            job.state["uploaded"] = i + 1
            page.wait_for_timeout(1200)

    def _fill_description(self, page, text: str) -> None:
        for selector in DESCRIPTION_FIELDS:
            box = page.locator(selector)
            if box.count():
                box.first.fill(text)
                return
        log.warning("no description field found on the page; skipped")
