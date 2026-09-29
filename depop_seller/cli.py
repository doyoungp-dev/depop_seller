"""Command line entry point: `python -m depop_seller ...` or `depop ...`."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import DB_PATH, DEFAULT_MODEL, PRODUCT_IMAGE_DIR, BatchPaths, batch_paths, load_api_key

log = logging.getLogger("depop_seller")


def cmd_check(_: argparse.Namespace) -> int:
    import anthropic
    import PIL
    import pillow_heif

    print(f"depop_seller {__version__}  python {sys.version.split()[0]}")
    print(f"pillow {PIL.__version__}, pillow-heif {pillow_heif.__version__}, anthropic {anthropic.__version__}")
    key = load_api_key()
    print(f"ANTHROPIC_API_KEY: {'found (...' + key[-4:] + ')' if key else 'NOT FOUND - set it in the environment or in .env'}")
    print(f"database: {DB_PATH} ({'exists' if DB_PATH.exists() else 'not created yet'})")
    print(f"product_image: {PRODUCT_IMAGE_DIR}")
    if PRODUCT_IMAGE_DIR.is_dir():
        for d in sorted(p for p in PRODUCT_IMAGE_DIR.iterdir() if (p / "raw_image").is_dir()):
            b = BatchPaths(d.name, d)
            raw = sum(1 for p in b.raw.iterdir() if p.is_file())
            sorted_n = sum(1 for p in b.sort_image.glob("*.jpg")) if b.sort_image.is_dir() else 0
            print(f"  {d.name}: {raw} raw, manifest {'yes' if b.manifest.exists() else 'no'}, "
                  f"review {'yes' if b.review_html.exists() else 'no'}, sort_image {sorted_n}")
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    import json

    from .learn import compare, expected_merges, load_pair, score_merges

    paths = batch_paths(args.batch)
    proposal, truth = load_pair(paths.root)
    c = compare(proposal, truth)
    print(f"batch {paths.batch}")
    print(c.summary())
    if c.false_splits:
        print("false splits before photos:", c.false_splits)
    if c.missed_splits:
        print("missed splits before photos:", c.missed_splits)
    for s in c.stitched_items:
        print(f"item {s['item']} was stitched from proposed items {s['from_proposed_items']}")
    if c.view_changes:
        print("view changes:", {f"{a}->{b}": n for (a, b), n in c.view_changes.items()})

    report: dict = {"batch": paths.batch, "summary": c.summary()}
    if args.test_merge_pass:
        from .group import Usage, find_duplicates

        if not load_api_key():
            print("ANTHROPIC_API_KEY not found", file=sys.stderr)
            return 2
        windows, _ = load_pair(paths.root, "proposal_windows.csv")
        expected = expected_merges(windows, truth)
        usage = Usage()
        predicted = find_duplicates(paths, windows, model=args.model, usage=usage)
        scores = score_merges(predicted, expected)
        cost = usage.cost(args.model)
        print(f"\nmerge pass ({args.model}): {len(scores['found'])}/{len(expected)} expected merges found, "
              f"{len(scores['false'])} unexpected; {usage.calls} call(s)"
              + (f", about ${cost:.2f}" if cost is not None else ""))
        print("found:", scores["found"])
        print("missed:", scores["missed"])
        print("unexpected:", scores["false"])
        report["merge_pass"] = scores | {"model": args.model}
    (paths.cache / "learn_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


def cmd_claude_login(_: argparse.Namespace) -> int:
    """Run the Claude Code CLI interactively so the user can /login once (the Sell page's Draft button uses it)."""
    import subprocess

    from .describe import find_claude_cli

    cli = find_claude_cli()
    if cli is None:
        print("Claude Code was not found. Install the Claude desktop app (it ships the command line), "
              "or set DEPOP_CLAUDE_CLI to the path of claude.exe.", file=sys.stderr)
        return 2
    print(f"Using {cli}")
    print("Type /login and press Enter, finish the sign-in in the browser, then type /exit")
    print()
    return subprocess.call([str(cli)])


def cmd_review(args: argparse.Namespace) -> int:
    from .review import serve

    batch = batch_paths(args.batch).batch if args.batch else None  # validate the name if one was given
    page = {"sell": "sell", "hub": "hub"}.get(args.command, "")
    serve(batch, port=args.port, open_browser=not args.no_browser, page=page,
          quiet=getattr(args, "quiet", False), wait_for_port=getattr(args, "wait_for_port", 0))
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    from .release import ReleaseError, release

    try:
        release(args.version, notes=args.notes, dry_run=args.dry_run)
    except ReleaseError as e:
        print(f"{chr(10)}not released: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_sort(args: argparse.Namespace) -> int:
    from .manifest import read_manifest, summary, write_manifest

    paths = batch_paths(args.batch)

    if args.apply:
        from .apply import apply_batch

        rows = read_manifest(paths.manifest)
        log.info("applying manifest: %s", summary(rows))
        n = apply_batch(paths, rows, force=args.force)
        print(f"wrote {n} photo(s) to {paths.sort_image}")
        return 0

    from .scan import scan_batch

    photos = scan_batch(paths, limit=args.limit)
    log.info("%d photo(s) in capture order", len(photos))

    if paths.manifest.exists() and not args.regroup:
        if args.limit:
            print("manifest.csv already exists; --limit only makes sense together with --regroup", file=sys.stderr)
            return 2
        rows = read_manifest(paths.manifest)
        scanned, listed = {p.raw_file for p in photos}, {r.raw_file for r in rows}
        if scanned != listed:
            log.warning("raw_image and manifest.csv differ: %d file(s) not in manifest, %d manifest row(s) missing on disk",
                        len(scanned - listed), len(listed - scanned))
        log.info("reusing existing manifest.csv (pass --regroup to run the grouping again)")
        write_manifest(paths.manifest, rows)  # refresh derived columns after human edits
    else:
        from .group import Usage, decisions_to_rows, group_batch, group_local

        if args.engine == "claude":
            if not load_api_key():
                print("ANTHROPIC_API_KEY not found. Put it in the environment or in .env, "
                      "or use --engine local.", file=sys.stderr)
                return 2
            usage = Usage()
            rows = group_batch(paths, photos, model=args.model, effort=args.effort, usage=usage,
                               merge_pass=not args.no_merge_pass)
            cost = usage.cost(args.model)
            log.info("%d API call(s) (%d served from cache), %d input / %d output tokens%s",
                     usage.calls, usage.cached_calls, usage.input_tokens, usage.output_tokens,
                     f", about ${cost:.2f}" if cost is not None else "")
        else:
            rows = decisions_to_rows(photos, group_local(paths, photos))

        if paths.manifest.exists():
            backup = paths.manifest.with_name(f"manifest.bak-{datetime.now():%Y%m%d-%H%M%S}.csv")
            shutil.copy2(paths.manifest, backup)
            log.info("previous manifest backed up to %s", backup.name)
        write_manifest(paths.manifest, rows)
        log.info("wrote %s", paths.manifest)

    print(summary(rows))
    print(f"next:   `review {args.batch}` to check and fix the grouping in your browser, "
          f"then approve there (or `sort {args.batch} --apply`)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="depop", description="Depop listing pipeline")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="show environment, batches and API key status")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("sort", help="group a batch's photos into items (then --apply to write sort_image/)")
    p.add_argument("batch", help="batch folder name under product_image/, e.g. a date or 'Autumn knits'")
    p.add_argument("--engine", choices=["claude", "local"], default="claude")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    p.add_argument("--limit", type=int, metavar="N", help="only the first N photos (for testing)")
    p.add_argument("--no-merge-pass", action="store_true", help="skip the second pass that finds re-shoots of earlier items")
    p.add_argument("--regroup", action="store_true", help="redo the grouping even if manifest.csv exists")
    p.add_argument("--apply", action="store_true", help="write sort_image/ from the reviewed manifest")
    p.add_argument("--force", action="store_true", help="with --apply: replace an existing sort_image/")
    p.set_defaults(func=cmd_sort)

    p = sub.add_parser("learn", help="score the engine's proposal against your reviewed manifest")
    p.add_argument("batch", nargs="?", help="batch folder name; default: the newest batch")
    p.add_argument("--test-merge-pass", action="store_true",
                   help="re-run the merge pass on the windowed proposal and score it (calls the API)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser("review", help="open the drag-and-drop review page for a batch in your browser")
    p.add_argument("batch", nargs="?", help="batch folder name; default: the newest batch")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("claude-login", help="sign in the Claude Code command line once (the Sell page's Draft button uses it)")
    p.set_defaults(func=cmd_claude_login)

    p = sub.add_parser("hub", help="open the batches page: every date folder with Review / Sell links")
    p.add_argument("batch", nargs="?")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--wait-for-port", type=float, default=0, help=argparse.SUPPRESS)   # set by an update's restart
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("sell", help="open the selling page: one Depop button per item (photos only, never posts)")
    p.add_argument("batch", nargs="?", help="batch folder name; default: the newest batch")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("release", help="for maintainers: test, tag and publish a new version on GitHub")
    p.add_argument("version", nargs="?", help="e.g. 1.1.0; default: the next patch version")
    p.add_argument("--notes", help="what changed, shown by Check for updates; default: the commit subjects since the last release")
    p.add_argument("--dry-run", action="store_true", help="run every check and build the zip, but commit, push and publish nothing")
    p.set_defaults(func=cmd_release)
    return parser


def windowless() -> bool:
    """True when started with pythonw.exe (the desktop icon): there is no console to print to,
    and touching sys.stdout would raise, so messages go to a log file instead."""
    return sys.stdout is None or sys.stderr is None or os.environ.get("DEPOP_QUIET") == "1"


def _log_to_file() -> Path:
    """Send this run's output to the log file and return where it went."""
    from .config import LOG_FILE

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = LOG_FILE.open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = handle
    print(f"{chr(10)}--- {datetime.now():%Y-%m-%d %H:%M:%S} starting")
    return LOG_FILE


def alert(message: str, title: str = "Depop Seller") -> None:
    """Tell the user something went wrong when there is no window to print it in."""
    try:
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)     # MB_ICONERROR
        elif sys.platform == "darwin":
            import subprocess

            subprocess.run(["osascript", "-e",
                            f'display dialog "{message}" buttons {{"OK"}} with icon caution with title "{title}"'],
                           check=False)
    except Exception:                                    # a failed alert must not hide the real error
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from . import config

    moved = config.migrate_into_data_dir()   # an older install kept its data in the app folder
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and not config.bundled_runtime():   # the way back in, if setup never made one
        from .desktop import ensure_desktop_shortcut      # (the installer makes and owns its own)

        ensure_desktop_shortcut(config.PROJECT_ROOT)
    quiet = windowless()
    args.quiet = quiet                       # the commands print different things with no console
    log_file = _log_to_file() if quiet else None
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    if moved:
        log.info("moved %s into the data folder %s", ", ".join(moved), config.DATA_DIR)
    try:
        return args.func(args)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        if quiet:
            alert(f"{e}")
        return 1
    except Exception as e:
        log.exception("unexpected error")
        if quiet:
            alert(f"Depop Seller could not start.{chr(10)}{chr(10)}{e}{chr(10)}{chr(10)}Details are in:{chr(10)}{log_file}")
        raise
