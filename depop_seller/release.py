"""Publish a new version in one go - for whoever maintains the app; sellers never use this.

    python -m depop_seller release              # the next patch version (1.0.3 -> 1.0.4)
    python -m depop_seller release 1.1.0        # or name it
    python -m depop_seller release --dry-run    # every check and the zip, but nothing is published

In order: the checkout must be clean and on main; GitHub must be reachable, signed in, public, and
hold the same history as this checkout; the tests must pass. Then the version is written into the
three files that carry it and committed, `DepopSeller.zip` is built from the committed files only
and checked exactly the way the app's updater will check it, the tag and main are pushed, and the
GitHub release is created with the zip attached. From then on every copy of the app finds it under
Settings -> Check for updates.

Every step can be run again after a failure: the commit, the tag and the pushes are only made when
missing, and a version counts as released only once GitHub has the release.

The push is guarded. The public repository started from a clean snapshot; a checkout whose history
does not start at the same commit as the remote's main (one still carrying the private history the
snapshot was cut from) is refused, so that history can never reach it - not even through a tag.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config, installer, share, updates

VERSION_FILES = ("pyproject.toml", "depop_seller/__init__.py", "Depop Seller.app/Contents/Info.plist")
DIST = "dist"                                       # gitignored; the zip is built here


class ReleaseError(RuntimeError):
    pass


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":                             # a fresh shell may not have them on PATH yet
        for folder in (r"C:\Program Files\Git\cmd", r"C:\Program Files\GitHub CLI"):
            guess = Path(folder) / f"{name}.exe"
            if guess.is_file():
                return str(guess)
    raise ReleaseError(f"{name} is not installed, or not on PATH")


def _run(*args: str, root: Path, check: bool = True) -> str:
    done = subprocess.run(list(args), cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if check and done.returncode != 0:
        what = " ".join([Path(args[0]).stem, *args[1:3]])
        raise ReleaseError(f"{what} failed: {(done.stderr or done.stdout).strip()[-600:]}")
    return done.stdout.strip()


# ---- versions -------------------------------------------------------------------------------

def read_version(root: Path) -> str:
    text = (root / "depop_seller" / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not found:
        raise ReleaseError("depop_seller/__init__.py has no __version__")
    return found.group(1)


def next_version(current: str, released: set[str]) -> str:
    """The version a release gets when none is named: the current one if it has never been
    released (the very first release), otherwise the next patch number."""
    if f"v{current}" not in released:
        return current
    major, minor, patch = (list(updates.parse_version(current)) + [0, 0])[:3]
    return f"{major}.{minor}.{patch + 1}"


def set_version(root: Path, version: str) -> list[str]:
    """Write the version into every file that carries it. Returns the files that changed."""
    rules = {
        "pyproject.toml": (r'(?m)^version = "[^"]*"', f'version = "{version}"'),
        "depop_seller/__init__.py": (r'__version__ = "[^"]*"', f'__version__ = "{version}"'),
        "Depop Seller.app/Contents/Info.plist": (
            r"(<key>CFBundle(?:ShortVersionString|Version)</key>\s*<string>)[^<]*(</string>)",
            rf"\g<1>{version}\g<2>"),
    }
    changed = []
    for name, (pattern, replacement) in rules.items():
        path = root / name
        text = path.read_text(encoding="utf-8")
        new, count = re.subn(pattern, replacement, text)
        if count == 0:
            raise ReleaseError(f"no version found in {name}")
        if new != text:
            path.write_text(new, encoding="utf-8", newline=chr(10))
            changed.append(name)
    return changed


def default_notes(root: Path, git: str, previous: str | None) -> str:
    """What changed, for the release page and for Check for updates: the commit subjects since the
    last release. They are public already, and written for people rather than for machines."""
    if previous is None:
        return "The first release."
    subjects = _run(git, "log", "--no-merges", "--format=%s", f"{previous}..HEAD", root=root)
    lines = [f"- {s}" for s in subjects.splitlines() if s and not s.startswith("Release ")]
    return chr(10).join(lines) or "Small fixes."


# ---- the zip ----------------------------------------------------------------------------------

def build_zip(root: Path, tracked: list[str], expect: str) -> Path:
    """DepopSeller.zip from the files git tracks - never whatever else sits in the folder - checked
    with the same code every installed app runs before it installs an update."""
    dest = root / DIST / updates.ASSET_NAME
    dest.parent.mkdir(exist_ok=True)
    dest.unlink(missing_ok=True)
    share.make_share_zip(dest, files=[root / name for name in tracked if name])
    version, files = updates.read_update(dest)
    if version != expect:
        raise ReleaseError(f"the zip says it is version {version}, expected {expect}")
    return dest


# ---- the whole thing ------------------------------------------------------------------------

def release(version: str | None = None, *, notes: str | None = None, dry_run: bool = False,
            root: Path | None = None, say=print) -> dict:
    root = root or config.PROJECT_ROOT
    problems: list[str] = []

    def blocker(message: str) -> None:
        """A dry run lists everything that would stop the release; a real one stops at the first."""
        if not dry_run:
            raise ReleaseError(message)
        problems.append(message)
        say(f"   ! {message}")

    if not (root / ".git").exists():
        raise ReleaseError(f"{root} is not a git checkout")
    git = _tool("git")

    say("Checking the checkout")
    branch = _run(git, "rev-parse", "--abbrev-ref", "HEAD", root=root)
    if branch != "main":
        blocker(f"on branch {branch} - releases are made from main")
    dirty = _run(git, "status", "--porcelain", root=root)
    if dirty:
        blocker("there are uncommitted changes:" + chr(10) + dirty)

    say(f"Checking GitHub ({updates.REPO})")
    gh = _tool("gh")
    released: set[str] = set()
    origin = _run(git, "remote", "get-url", "origin", root=root, check=False).removesuffix(".git").rstrip("/")
    if not origin.lower().endswith(f"github.com/{updates.REPO}".lower()):
        blocker(f"origin is {origin or 'not set'}, but the app looks for updates at github.com/{updates.REPO}")
    elif subprocess.run([gh, "auth", "status"], capture_output=True).returncode != 0:
        blocker("the GitHub CLI is not signed in - run: gh auth login")
    else:
        private = _run(gh, "api", f"repos/{updates.REPO}", "--jq", ".private", root=root, check=False)
        if private != "false":
            blocker(f"github.com/{updates.REPO} is private (or missing): the app's Check for updates cannot see its releases")
        released = set(_run(gh, "api", f"repos/{updates.REPO}/releases", "--paginate", "--jq", ".[].tag_name",
                            root=root, check=False).split())
        _run(git, "fetch", "--quiet", "--tags", "origin", root=root)
        remote = _run(git, "rev-parse", "--verify", "--quiet", "origin/main", root=root, check=False)
        mine = sorted(_run(git, "rev-list", "--max-parents=0", "HEAD", root=root).split())
        if not remote:
            blocker("origin has no main branch yet - push the first snapshot by hand")
        elif mine != sorted(_run(git, "rev-list", "--max-parents=0", "origin/main", root=root).split()):
            blocker("this checkout's history does not start where origin's main does - refusing to push it there")
        elif subprocess.run([git, "merge-base", "--is-ancestor", "origin/main", "HEAD"], cwd=str(root)).returncode != 0:
            blocker("origin/main has commits this checkout does not - pull them first")

    if installer.find_iscc() is None:
        blocker("Inno Setup is not installed, so the Windows installer cannot be built - "
                "get it from https://jrsoftware.org/isdl.php (it can be installed for the current user only)")

    current = read_version(root)
    version = (version or next_version(current, released)).lstrip("vV")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ReleaseError(f"{version!r} is not a version like 1.2.0")
    if f"v{version}" in released:
        blocker(f"v{version} has already been released")
    elif released and updates.parse_version(version) <= max(updates.parse_version(t) for t in released):
        blocker(f"{version} is not newer than the last release")
    if updates.parse_version(version) < updates.parse_version(current):
        blocker(f"{version} is older than the version already in the code ({current})")
    previous = max(released, key=updates.parse_version) if released else None
    say(f"Version {version}" + (f" (last release {previous})" if previous else " (the first release)"))

    say("Running the tests")
    tests = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=str(root),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    summary = (tests.stdout.strip().splitlines() or ["no output"])[-1]
    if tests.returncode != 0:
        blocker("the tests fail:" + chr(10) + tests.stdout[-2000:] + tests.stderr[-500:])
    else:
        say(f"   {summary}")

    notes = (notes or "").strip() or default_notes(root, git, previous)
    if dry_run:
        tracked = _run(git, "ls-files", "-z", root=root).split(chr(0))
        zip_path = build_zip(root, tracked, current)
        say(f"Built {zip_path} ({zip_path.stat().st_size // 1024} KB) from {len([t for t in tracked if t])} tracked files; the updater accepts it")
        setup = None
        if installer.find_iscc() is not None:
            say("Building the Windows installer")
            try:
                setup = installer.build(root, zip_path, current, say=say)
            except installer.BuildError as e:
                blocker(str(e))
        say("Release notes:" + chr(10) + notes)
        say("Dry run: nothing was committed, tagged, pushed or published.")
        if problems:
            say(f"{len(problems)} thing(s) would stop a real release - see the ! lines above.")
        else:
            say(f"Ready: `python -m depop_seller release {version}` would publish it.")
        return {"version": version, "zip": str(zip_path), "setup": str(setup) if setup else None, "problems": problems}

    say("Setting the version")
    changed = set_version(root, version)
    if changed:
        _run(git, "add", "--", *changed, root=root)
        _run(git, "commit", "--quiet", "-m", f"Release {version}", root=root)

    say("Building the zip")
    tracked = _run(git, "ls-files", "-z", root=root).split(chr(0))
    zip_path = build_zip(root, tracked, version)
    say("Building the Windows installer")
    try:
        setup = installer.build(root, zip_path, version, say=say)
    except installer.BuildError as e:
        raise ReleaseError(str(e)) from e

    say("Tagging and pushing")
    tag = f"v{version}"
    head = _run(git, "rev-parse", "HEAD", root=root)
    tagged = _run(git, "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}", root=root, check=False)
    if not tagged:
        _run(git, "tag", "-a", tag, "-m", f"Depop Seller {version}", root=root)
    elif tagged != head:
        raise ReleaseError(f"the tag {tag} already exists on another commit")
    _run(git, "push", "--quiet", "origin", "main", root=root)
    _run(git, "push", "--quiet", "origin", tag, root=root)

    say("Publishing the release")
    with tempfile.TemporaryDirectory() as tmp:
        notes_file = Path(tmp) / "notes.md"
        notes_file.write_text(notes + chr(10), encoding="utf-8")
        url = _run(gh, "release", "create", tag, str(setup), str(zip_path), "--repo", updates.REPO, "--verify-tag",
                   "--title", f"Depop Seller {version}", "--notes-file", str(notes_file), root=root)
    say(f"Released {version}: {url}")
    return {"version": version, "zip": str(zip_path), "setup": str(setup), "url": url, "problems": []}
