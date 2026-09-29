"""The Windows installer: how its pieces are staged, what the installer script promises, and how
an installed copy behaves differently from one run out of a .venv."""

from __future__ import annotations

import argparse
import collections
import importlib.metadata
import os
import re
import sys
import zipfile
from pathlib import Path

import pytest

from depop_seller import config, installer, share, updates

ISS = (config.PROJECT_ROOT / "installer" / "DepopSeller.iss").read_text(encoding="utf-8")


def test_the_runtime_looks_only_at_itself_the_libraries_and_the_app():
    lines = installer.pth_text("314").splitlines()
    assert lines[:4] == ["python314.zip", ".", "Lib" + chr(92) + "site-packages", ".." + chr(92) + "app"]
    assert "import site" in lines
    assert installer.version_tag("3.14.2") == "314"


def test_the_bundle_gets_exactly_the_libraries_the_tests_ran_with():
    pins = installer.constraints()
    names = [p.split("==")[0].lower().replace("_", "-") for p in pins]
    assert len(names) == len(set(names))                                        # one pin per package
    assert f"pillow=={importlib.metadata.version('pillow')}" in [p.lower() for p in pins]
    assert "depop-seller" not in names                                          # the app is not on PyPI
    deps = installer.dependencies(config.PROJECT_ROOT / "pyproject.toml")
    assert any(d.startswith("anthropic") for d in deps)
    assert not any(d.startswith("playwright") for d in deps)                    # optional, never bundled


def test_the_app_folder_is_the_release_zip_unpacked_with_one_guide(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    for rel, body in {"depop_seller/__init__.py": '__version__ = "1.0.0"', "README.md": "docs",
                      "START HERE.txt": "an old guide from a copy that came out of a zip"}.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(body, encoding="utf-8")
    monkeypatch.setattr(share, "PROJECT_ROOT", project)
    release_zip = tmp_path / "DepopSeller.zip"
    share.make_share_zip(release_zip)
    counts = collections.Counter(zipfile.ZipFile(release_zip).namelist())
    assert counts["depop_seller_app/START HERE.txt"] == 1                       # written fresh, never twice
    assert updates.read_update(release_zip)[0] == "1.0.0"                      # and every app still accepts it

    app = installer.stage_app(tmp_path / "stage", release_zip)
    assert sorted(p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file()) == [
        "README.md", "START HERE.txt", "depop_seller/__init__.py"]
    assert "DEPOP SELLER" in (app / "START HERE.txt").read_text(encoding="utf-8")


def test_no_file_in_the_bundle_is_too_long_a_path_once_installed(tmp_path: Path):
    ok = tmp_path / "runtime" / "Lib" / "site-packages" / "pkg" / "module.py"
    ok.parent.mkdir(parents=True)
    ok.write_text("", encoding="utf-8")
    installer.check_path_lengths(tmp_path)
    deep = tmp_path / "runtime" / ("x" * 100) / ("y" * 100) / "z.py"
    deep.parent.mkdir(parents=True)
    deep.write_text("", encoding="utf-8")
    with pytest.raises(installer.BuildError, match="too long"):
        installer.check_path_lengths(tmp_path)


def test_inno_setup_is_found_where_it_installs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    for var in ("DEPOP_ISCC", "ProgramFiles(x86)", "ProgramFiles"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert installer.find_iscc() is None
    per_user = tmp_path / "local" / "Programs" / "Inno Setup 6" / "ISCC.exe"
    per_user.parent.mkdir(parents=True)
    per_user.write_text("", encoding="utf-8")
    assert installer.find_iscc() == per_user
    chosen = tmp_path / "ISCC.exe"
    chosen.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEPOP_ISCC", str(chosen))
    assert installer.find_iscc() == chosen


def test_the_installer_script_keeps_its_promises():
    setting = dict(re.findall(r"(?m)^(\w+)=(.*)$", ISS.split("[Setup]")[1].split("[InstallDelete]")[0]))
    assert setting["AppId"] == "{{D3ACB89F-C80D-4399-93C8-32AC74F40CB5}"          # never change: upgrades find it
    assert setting["PrivilegesRequired"] == "lowest"                              # no administrator password
    assert setting["DefaultDirName"] == "{localappdata}" + chr(92) + "Programs" + chr(92) + "{#AppName}"
    assert setting["OutputBaseFilename"] + ".exe" == updates.INSTALLER_NAME      # what the updater points at
    launch = "runtime" + chr(92) + "pythonw.exe"                                   # no console window
    assert ISS.count(launch) >= 3 and '#define Launch "-m depop_seller hub"' in ISS
    assert "WorkingDir: " + chr(34) + "{app}" + chr(92) + "app" in ISS
    assert "http://127.0.0.1:8765/quit" in ISS                                   # an open copy is asked to close
    for section in ("[InstallDelete]", "[UninstallDelete]"):
        body = ISS.split(section)[1].split("[")[0]
        assert "{app}" + chr(92) + "runtime" in body and "{app}" + chr(92) + "app" in body
    assert "DepopSeller" not in ISS.split("[UninstallDelete]")[1].split("[Messages]")[0].replace("{#AppName}", "")


def test_an_installed_copy_is_recognised_by_where_python_runs(tmp_path: Path, monkeypatch):
    app, runtime = tmp_path / "Depop Seller" / "app", tmp_path / "Depop Seller" / "runtime"
    app.mkdir(parents=True)
    runtime.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", app)
    monkeypatch.setattr(config.sys, "executable", str(runtime / "pythonw.exe"))
    assert config.bundled_runtime() is True
    monkeypatch.setattr(config.sys, "executable", str(app / ".venv" / "Scripts" / "pythonw.exe"))
    assert config.bundled_runtime() is False


@pytest.mark.skipif(os.name != "nt", reason="the Desktop icon is a Windows thing")
def test_an_installed_copy_leaves_the_desktop_icon_to_the_installer(tmp_path: Path, monkeypatch):
    from depop_seller import cli, desktop

    made = []
    monkeypatch.setattr(desktop, "ensure_desktop_shortcut", lambda root: made.append(root))
    monkeypatch.setattr(config, "migrate_into_data_dir", lambda: [])
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_subparsers(dest="command", required=True).add_parser("noop").set_defaults(func=lambda args: 0)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)

    monkeypatch.setattr(config, "bundled_runtime", lambda: True)
    assert cli.main(["noop"]) == 0 and made == []          # the installer made it; deleting it is the user's call
    monkeypatch.setattr(config, "bundled_runtime", lambda: False)
    assert cli.main(["noop"]) == 0 and len(made) == 1      # a manual install repairs a missing icon


def test_the_depop_button_without_the_helper_says_to_add_it(monkeypatch):
    """The automated-browser fallback needs the optional Playwright library, which installs do
    not have. Without it the button must say what to do instead of waiting forever."""
    import importlib.util

    from depop_seller.review import ReviewServer

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "playwright" else real(name, *a))
    with pytest.raises(RuntimeError, match="Chrome helper"):
        ReviewServer().sell_open("any batch", 1)


def test_launcher_in_an_installed_copy_starts_the_bundled_app():
    cmd = (config.PROJECT_ROOT / "Depop Seller.cmd").read_text(encoding="utf-8")
    bundled = cmd.index(r'if exist "..\runtime\pythonw.exe"')
    assert bundled < cmd.index("setup.ps1")               # checked before any setup could start
    assert r'start "" "..\runtime\pythonw.exe" -m depop_seller hub' in cmd
