"""Apply a reviewed manifest: copy each kept photo into sort_image/ as <item_id>_<photo_no>.jpg."""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps

from . import db
from .config import OUTPUT_JPEG_QUALITY, OUTPUT_MAX_PX, BatchPaths
from .manifest import Row, items

pillow_heif.register_heif_opener()
log = logging.getLogger(__name__)


def convert(src: Path, dst: Path, max_px: int = OUTPUT_MAX_PX, quality: int = OUTPUT_JPEG_QUALITY) -> None:
    """HEIC/JPEG/PNG -> upright RGB JPEG, long edge <= max_px, EXIF stripped."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if max(im.size) > max_px:
            im.thumbnail((max_px, max_px), Image.LANCZOS)
        im.save(dst, "JPEG", quality=quality, optimize=True)


def apply_batch(
    paths: BatchPaths,
    rows: list[Row],
    *,
    force: bool = False,
    max_px: int = OUTPUT_MAX_PX,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Write sort_image/ and update the database. Returns the number of photos written."""
    dst = paths.sort_image
    dst.mkdir(parents=True, exist_ok=True)
    leftovers = [p for p in dst.iterdir() if p.is_file()]
    if leftovers and not force:
        raise FileExistsError(f"{dst} already contains {len(leftovers)} file(s); pass --force to replace them")
    for p in leftovers:
        p.unlink()

    kept = [r for r in rows if not r.skip]
    names = {r.raw_file: f"{r.item_id}_{r.photo_no}.jpg" for r in kept}
    for i, r in enumerate(kept, 1):
        convert(paths.raw / r.raw_file, dst / names[r.raw_file], max_px=max_px)
        if progress:
            progress(i, len(kept))
        if i % 25 == 0 or i == len(kept):
            log.info("converted %d/%d", i, len(kept))

    with (dst / "manifest.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["item_id", "photo_no", "view", "sorted_file", "raw_file", "taken_at"])
        for r in kept:
            w.writerow([r.item_id, r.photo_no, r.view, names[r.raw_file], r.raw_file, r.taken_at])

    conn = db.connect()
    try:
        n_items = db.replace_batch(conn, paths.batch, rows, names)
    finally:
        conn.close()
    log.info("database: %d item(s), %d photo(s) for batch %s", n_items, len(kept), paths.batch)
    return len(kept)
