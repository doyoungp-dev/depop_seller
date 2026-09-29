"""Check for updates, installing one, handing over to the new copy, and the release that feeds it."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from depop_seller import release, share, updates

OLD_INIT = '"""depop_seller."""' + chr(10) + chr(10) + '__version__ = "1.0.0"' + chr(10)
NEW_INIT = '"""depop_seller."""' + chr(10) + chr(10) + '__version__ = "1.1.0"' + chr(10)


def _pyproject(version: str, deps: list[str]) -> str:
    return (f'[project]{chr(10)}name = "depop_seller"{chr(10)}version = "{version}"{chr(10)}'
            f"dependencies = {json.dumps(deps)}{chr(10)}")


def _tree(folder: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(body, encoding="utf-8")
    return folder


def _release_zip(tmp_path: Path, monkeypatch, files: dict[str, str]) -> Path:
    """A zip in exactly the shape a real release has: built by the same share code."""
    project = _tree(tmp_path / "release_src", files)
    monkeypatch.setattr(share, "PROJECT_ROOT", project)
    dest = tmp_path / "DepopSeller.zip"
    share.make_share_zip(dest)
    return dest


def _installed_app(tmp_path: Path, pyproject: str) -> Path:
    """An app folder as a seller has it: the app, its .venv, and some leftovers of theirs."""
    return _tree(tmp_path / "app", {
        "depop_seller/__init__.py": OLD_INIT,
        "depop_seller/review.py": "old code",
        "README.md": "same readme",
        "pyproject.toml": pyproject,
        ".venv/Scripts/python.exe": "the environment",
        "product_image/Spring/raw_image/IMG_1.HEIC": "a photo nobody migrated",
        "description_style.md": "the seller's own style",
        ".env": "ANTHROPIC_API_KEY=sk-ant-test-not-a-real-key",
    })


def test_versions_compare_as_numbers_and_the_first_release_keeps_its_number():
    assert updates.parse_version("v1.10.0") > updates.parse_version("1.9.9")
    assert updates.parse_version("1.0.1") > updates.parse_version("v1.0.0")
    with pytest.raises(ValueError):
        updates.parse_version("latest")
    assert release.next_version("1.0.0", set()) == "1.0.0"              # never released: release it as is
    assert release.next_version("1.0.0", {"v1.0.0"}) == "1.0.1"
    assert release.next_version("1.2.9", {"v1.0.0", "v1.2.9"}) == "1.2.10"


def test_check_says_what_is_new_and_never_raises(monkeypatch):
    latest = {
        "tag_name": "v9.1.0", "published_at": "2026-10-02T09:00:00Z", "body": "- Faster grouping",
        "html_url": updates.RELEASES_PAGE + "/tag/v9.1.0",
        "assets": [{"name": "DepopSeller.zip",
                    "browser_download_url": updates.DOWNLOAD_PREFIX + "v9.1.0/DepopSeller.zip"}],
    }
    monkeypatch.setattr(updates, "_get_json", lambda url: latest)
    monkeypatch.setattr(updates, "managed_by_git", lambda root=None: False)
    found = updates.check()
    assert found["ok"] and found["newer"] and found["can_install"]
    assert found["latest"] == "9.1.0" and found["published"] == "2026-10-02" and "Faster" in found["notes"]

    monkeypatch.setattr(updates, "managed_by_git", lambda root=None: True)       # the developer's checkout
    found = updates.check()
    assert found["newer"] and not found["can_install"] and "git pull" in found["why_not"]

    monkeypatch.setattr(updates, "managed_by_git", lambda root=None: False)
    monkeypatch.setattr(updates, "_get_json", lambda url: latest | {"assets": []})
    assert not updates.check()["can_install"]                                   # nothing to install

    monkeypatch.setattr(updates, "_get_json", lambda url: latest | {"tag_name": "v" + updates.__version__})
    found = updates.check()
    assert found["ok"] and not found["newer"] and found["why_not"] is None

    def fails(error):
        def get(url):
            raise error
        return get

    monkeypatch.setattr(updates, "_get_json", fails(urllib.error.HTTPError(updates.LATEST_API, 404, "nf", {}, None)))
    assert updates.check() == {"ok": False, "current": updates.__version__, "reason": "No version has been published yet."}
    monkeypatch.setattr(updates, "_get_json", fails(urllib.error.URLError("offline")))
    assert "internet" in updates.check()["reason"]
    monkeypatch.setattr(updates, "_get_json", fails(urllib.error.HTTPError(updates.LATEST_API, 403, "rate", {}, None)))
    assert "try again" in updates.check()["reason"]


def test_an_update_is_checked_before_anything_is_written(tmp_path: Path, monkeypatch):
    good = _release_zip(tmp_path, monkeypatch, {"depop_seller/__init__.py": NEW_INIT, "README.md": "docs"})
    version, files = updates.read_update(good)
    assert version == "1.1.0"
    assert {str(inside) for _, inside in files} == {"depop_seller/__init__.py", "README.md", "START HERE.txt"}

    def zip_with(*names: str) -> Path:
        path = tmp_path / "bad.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("depop_seller_app/depop_seller/__init__.py", NEW_INIT)
            for name in names:
                info = zipfile.ZipInfo("placeholder")
                info.filename = name                     # exactly as a hostile zip could carry it
                z.writestr(info, "x")
        return path

    for hostile in ("depop_seller_app/../outside.py", "depop_seller_app/.." + chr(92) + "outside.py",
                    "/etc/outside.py", "C:/outside.py", "another_root/x.py",
                    "depop_seller_app/.venv/Scripts/python.exe", "depop_seller_app/.env",
                    "depop_seller_app/product_image/b/raw_image/a.heic", "depop_seller_app/description_style.md",
                    "depop_seller_app/depop_seller.db"):
        with pytest.raises(ValueError):
            updates.read_update(zip_with(hostile))

    path = tmp_path / "no_version.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("depop_seller_app/README.md", "docs")
    with pytest.raises(ValueError, match="not a Depop Seller release"):
        updates.read_update(path)
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="not a zip"):
        updates.read_update(tmp_path / "junk.zip")
    with pytest.raises(ValueError, match="outside this project"):
        updates._download("https://example.com/DepopSeller.zip", tmp_path / "x.zip")


@pytest.mark.parametrize("new_deps, reinstall", [(["pillow>=11"], False), (["pillow>=11", "newlib>=1"], True)])
def test_install_replaces_only_changed_app_files_and_nothing_of_the_seller_s(tmp_path: Path, monkeypatch,
                                                                            new_deps, reinstall):
    zip_path = _release_zip(tmp_path, monkeypatch, {
        "depop_seller/__init__.py": NEW_INIT,
        "depop_seller/review.py": "new code",
        "depop_seller/brand_new.py": "a module this version adds",
        "README.md": "same readme",
        "pyproject.toml": _pyproject("1.1.0", new_deps),
    })
    app = _installed_app(tmp_path, _pyproject("1.0.0", ["pillow>=11"]))
    old = time.time() - 86400
    os.utime(app / "README.md", (old, old))
    monkeypatch.setattr(updates, "_download", lambda url, dest: shutil.copy(zip_path, dest))
    monkeypatch.setattr(updates, "backup_dir", lambda: tmp_path / "previous-version")
    finished = []
    phases = []

    out = updates.install(updates.DOWNLOAD_PREFIX + "v1.1.0/DepopSeller.zip", root=app,
                          progress=phases.append, after=lambda root, changed: finished.append(changed))

    assert out["installed"] == "1.1.0"
    assert finished == [reinstall]                     # libraries are fetched only when they changed
    assert phases[0] == "Downloading" and "Installing" in phases
    assert (app / "depop_seller/review.py").read_text() == "new code"
    assert '"1.1.0"' in (app / "depop_seller/__init__.py").read_text()
    assert (app / "depop_seller/brand_new.py").is_file()
    assert (app / "README.md").stat().st_mtime == pytest.approx(old)    # unchanged, so never rewritten
    for untouched, body in ((".venv/Scripts/python.exe", "the environment"),
                            ("product_image/Spring/raw_image/IMG_1.HEIC", "a photo nobody migrated"),
                            ("description_style.md", "the seller's own style"),
                            (".env", "ANTHROPIC_API_KEY=sk-ant-test-not-a-real-key")):
        assert (app / untouched).read_text() == body, untouched
    kept = {p.relative_to(tmp_path / "previous-version").as_posix()
            for p in (tmp_path / "previous-version").rglob("*") if p.is_file()}
    assert kept == {"depop_seller/__init__.py", "depop_seller/review.py", "pyproject.toml"}   # just what changed


def test_a_failed_update_puts_the_previous_version_back(tmp_path: Path, monkeypatch):
    zip_path = _release_zip(tmp_path, monkeypatch, {
        "depop_seller/__init__.py": NEW_INIT, "depop_seller/review.py": "new code", "depop_seller/brand_new.py": "x"})
    app = _installed_app(tmp_path, _pyproject("1.0.0", []))
    monkeypatch.setattr(updates, "_download", lambda url, dest: shutil.copy(zip_path, dest))
    monkeypatch.setattr(updates, "backup_dir", lambda: tmp_path / "previous-version")

    def broken(root, changed):
        raise RuntimeError("the new version does not start: SyntaxError")

    with pytest.raises(RuntimeError, match="nothing has changed"):
        updates.install(updates.DOWNLOAD_PREFIX + "v1.1.0/DepopSeller.zip", root=app, after=broken)
    assert (app / "depop_seller/review.py").read_text() == "old code"
    assert '"1.0.0"' in (app / "depop_seller/__init__.py").read_text()
    assert not (app / "depop_seller/brand_new.py").exists()

    (app / ".git").mkdir()                               # a developer's checkout is never overwritten
    with pytest.raises(RuntimeError, match="git pull"):
        updates.install(updates.DOWNLOAD_PREFIX + "v1.1.0/DepopSeller.zip", root=app)


def test_an_installed_copy_takes_new_libraries_from_the_installer_not_pip(tmp_path: Path, monkeypatch):
    """The installer's Python has no pip. A release that changes the libraries must say to run the
    new installer - before touching anything - while a code-only release installs as usual."""
    app = _installed_app(tmp_path, _pyproject("1.0.0", ["pillow>=11"]))
    monkeypatch.setattr(updates, "backup_dir", lambda: tmp_path / "previous-version")
    monkeypatch.setattr(updates.config, "bundled_runtime", lambda: True)

    new_libs = _release_zip(tmp_path, monkeypatch, {
        "depop_seller/__init__.py": NEW_INIT, "depop_seller/review.py": "new code",
        "pyproject.toml": _pyproject("1.1.0", ["pillow>=11", "newlib>=1"])})
    monkeypatch.setattr(updates, "_download", lambda url, dest: shutil.copy(new_libs, dest))
    with pytest.raises(RuntimeError, match="DepopSellerSetup.exe"):
        updates.install(updates.DOWNLOAD_PREFIX + "v1.1.0/DepopSeller.zip", root=app, after=lambda r, c: None)
    assert (app / "depop_seller/review.py").read_text() == "old code"          # nothing was touched

    shutil.rmtree(tmp_path / "release_src")
    same_libs = _release_zip(tmp_path, monkeypatch, {
        "depop_seller/__init__.py": NEW_INIT, "depop_seller/review.py": "new code",
        "pyproject.toml": _pyproject("1.1.0", ["pillow>=11"])})
    monkeypatch.setattr(updates, "_download", lambda url, dest: shutil.copy(same_libs, dest))
    out = updates.install(updates.DOWNLOAD_PREFIX + "v1.1.0/DepopSeller.zip", root=app, after=lambda r, c: None)
    assert out["installed"] == "1.1.0" and (app / "depop_seller/review.py").read_text() == "new code"


def test_new_code_must_import_before_the_app_restarts_into_it(tmp_path: Path, monkeypatch):
    from depop_seller import config

    monkeypatch.setattr(updates, "_app_python", lambda: Path(sys.executable))
    broken = _tree(tmp_path / "broken", {"depop_seller/__init__.py": "this is not python"})
    with pytest.raises(RuntimeError, match="does not start"):
        updates.finish(broken, dependencies_changed=False)
    updates.finish(config.PROJECT_ROOT, dependencies_changed=False)     # the real code imports fine


def test_the_new_copy_starts_windowless_and_waits_for_the_old_one(tmp_path: Path, monkeypatch):
    from depop_seller.cli import build_parser

    scripts = _tree(tmp_path / ".venv" / "Scripts", {"python.exe": "", "pythonw.exe": ""})
    monkeypatch.setattr(updates.sys, "executable", str(scripts / "python.exe"))
    cmd = updates.restart_command(8765)
    assert Path(cmd[0]).name == "pythonw.exe"                                  # no console window
    assert cmd[1:] == ["-m", "depop_seller", "hub", "--no-browser", "--port", "8765", "--wait-for-port", "30"]
    args = build_parser().parse_args(cmd[3:])
    assert args.wait_for_port == 30 and args.no_browser and args.port == 8765

    seen = {}
    monkeypatch.setattr(updates.subprocess, "Popen", lambda argv, **kw: seen.update(kw, argv=argv))
    updates.spawn_replacement(8765)
    assert seen["argv"] == cmd and seen["env"]["DEPOP_QUIET"] == "1"          # it logs to the file
    if os.name == "nt":
        assert seen["creationflags"] & 0x08                                      # DETACHED_PROCESS
    else:
        assert seen["start_new_session"]


def test_the_new_copy_takes_the_port_once_the_old_one_lets_go():
    from depop_seller.review import ReviewServer, _bind

    old = socket.socket()
    old.bind(("127.0.0.1", 0))
    old.listen()
    port = old.getsockname()[1]
    assert _bind(port, ReviewServer(), wait_s=0) is None                  # taken, and not waiting

    threading.Timer(0.6, old.close).start()
    started = time.monotonic()
    server = _bind(port, ReviewServer(), wait_s=10)
    try:
        assert server is not None and time.monotonic() - started >= 0.5
    finally:
        server.server_close()


def test_updating_waits_for_running_work_then_hands_over(monkeypatch):
    import depop_seller.review as review

    server = review.ReviewServer()

    class FakeHttp:
        server_address = ("127.0.0.1", 8765)

        def __init__(self):
            self.stopped = threading.Event()

        def shutdown(self):
            self.stopped.set()

    server.style_job["running"] = True
    with pytest.raises(RuntimeError, match="still rewriting the style"):
        server.start_update(FakeHttp())
    server.style_job["running"] = False

    release_install = threading.Event()
    spawned = []
    monkeypatch.setattr(updates, "check", lambda: {"ok": True, "current": "1.0.0", "newer": True, "can_install": True,
                                                   "why_not": None, "download": updates.DOWNLOAD_PREFIX + "x"})

    def install(url, progress):
        progress("Installing")
        release_install.wait(5)
        return {"installed": "1.1.0"}

    monkeypatch.setattr(updates, "install", install)
    monkeypatch.setattr(updates, "can_restart", lambda: True)
    monkeypatch.setattr(updates, "spawn_replacement", spawned.append)
    http = FakeHttp()
    job = server.start_update(http)
    time.sleep(0.2)
    assert job["running"] and job["phase"] == "Installing"
    assert server.busy() == "installing an update"                    # closing the window must not stop it
    with pytest.raises(RuntimeError, match="already being installed"):
        server.start_update(http)
    release_install.set()
    assert http.stopped.wait(5)                                       # the old copy steps aside...
    assert spawned == [8765]                                          # ...for a new one on the same port
    assert job == {"running": False, "phase": "Restarting", "error": None, "installed": "1.1.0", "restart": "auto"}

    monkeypatch.setattr(updates, "can_restart", lambda: False)             # macOS: the user reopens it
    spawned.clear()
    http = FakeHttp()
    job = server.start_update(http)
    assert http.stopped.wait(5) and spawned == []
    assert job["restart"] == "manual" and job["installed"] == "1.1.0"

    monkeypatch.setattr(updates, "check", lambda: {"ok": True, "current": "1.1.0", "newer": False})
    job = server.start_update(FakeHttp())
    for _ in range(50):
        if not job["running"]:
            break
        time.sleep(0.05)
    assert "already have the newest version" in job["error"] and job["installed"] is None


def test_a_restarted_copy_nobody_comes_back_to_does_not_linger():
    """The window may have been closed while the update ran. The copy that starts afterwards gives
    it a minute to reconnect (serve() arms this), then stops like any closed app."""
    import depop_seller.review as review

    class FakeHttp:
        stopped = False

        def shutdown(self):
            self.stopped = True

    server = review.ReviewServer()
    http = FakeHttp()
    assert server.arm_close(http, grace=0.1) == {"closing_in": 0.1}
    time.sleep(0.5)
    assert http.stopped

    http = FakeHttp()                                                       # the page does come back
    server.arm_close(http, grace=0.1)
    server.note_request()
    time.sleep(0.5)
    assert not http.stopped


def test_version_and_update_routes(monkeypatch):
    from functools import partial

    from depop_seller.review import ReviewHandler, ReviewServer, _Server

    monkeypatch.setattr(updates, "check", lambda: {"ok": True, "current": "1.0.0", "newer": False})
    srv = _Server(("127.0.0.1", 0), partial(ReviewHandler, ReviewServer()))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/version") as r:
            v = json.load(r)
        assert v["version"] == updates.__version__ and v["pid"] == os.getpid() and v["stale"] is False
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/updates/status") as r:
            assert json.load(r)["running"] is False
        req = urllib.request.Request(f"http://127.0.0.1:{port}/updates/check", data=b"{}", method="POST")
        with urllib.request.urlopen(req) as r:
            assert json.load(r)["newer"] is False
        req.add_header("Origin", "https://evil.example")                # no other site may trigger it
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_release_sets_the_version_everywhere_it_lives(tmp_path: Path):
    from depop_seller import config

    for name in release.VERSION_FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.PROJECT_ROOT / name, tmp_path / name)
    assert sorted(release.set_version(tmp_path, "7.8.9")) == sorted(release.VERSION_FILES)
    assert release.read_version(tmp_path) == "7.8.9"
    assert 'version = "7.8.9"' in (tmp_path / "pyproject.toml").read_text()
    plist = (tmp_path / "Depop Seller.app/Contents/Info.plist").read_text()
    assert plist.count("<string>7.8.9</string>") == 2                    # short version and build
    assert release.set_version(tmp_path, "7.8.9") == []                  # running it again changes nothing


def test_release_zip_holds_only_committed_files_and_passes_the_updater(tmp_path: Path, monkeypatch):
    project = _tree(tmp_path / "checkout", {
        "depop_seller/__init__.py": OLD_INIT, "depop_seller/review.py": "code", "README.md": "docs",
        "notes-to-self.txt": "never committed", "description_style.md": "a seller's own style",
    })
    monkeypatch.setattr(share, "PROJECT_ROOT", project)
    tracked = ["depop_seller/__init__.py", "depop_seller/review.py", "README.md", ""]
    zip_path = release.build_zip(project, tracked, "1.0.0")
    names = {n.split("/", 1)[1] for n in zipfile.ZipFile(zip_path).namelist()}
    assert names == {"depop_seller/__init__.py", "depop_seller/review.py", "README.md", "START HERE.txt"}
    with pytest.raises(release.ReleaseError, match="expected 2.0.0"):
        release.build_zip(project, tracked, "2.0.0")
