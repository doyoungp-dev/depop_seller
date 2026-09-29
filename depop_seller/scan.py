"""Scan a batch's raw_image folder: capture order, timestamps, and a thumbnail cache.

raw_image/ is treated as read-only. Everything derived goes to thumbs/ and cache/.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps

from .config import RAW_EXTENSIONS, THUMB_SIZE, BatchPaths

pillow_heif.register_heif_opener()
log = logging.getLogger(__name__)

EXIF_IFD_POINTER = 0x8769
TAG_DATETIME_ORIGINAL = 0x9003
TAG_DATETIME = 0x0132
_DIGITS = re.compile(r"(\d+)")


@dataclass(frozen=True)
class Photo:
    seq: int                    # 1-based position in capture order
    raw_file: str               # file name inside raw_image/
    taken_at: datetime | None   # EXIF DateTimeOriginal
    gap_s: float                # seconds since the previous photo (0 for the first)
    width: int
    height: int

    @property
    def stem(self) -> str:
        return Path(self.raw_file).stem


def natural_key(name: str) -> list[object]:
    """IMG_4127 < IMG_4129 < IMG_4130, regardless of digit count."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in _DIGITS.split(name)]


def read_metadata(path: Path) -> tuple[datetime | None, int, int]:
    """EXIF capture time (DateTimeOriginal, falling back to DateTime) and pixel size."""
    with Image.open(path) as im:
        exif = im.getexif()
        raw = exif.get_ifd(EXIF_IFD_POINTER).get(TAG_DATETIME_ORIGINAL) or exif.get(TAG_DATETIME)
        width, height = im.size
    taken_at = None
    if raw:
        try:
            taken_at = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            log.warning("%s: unparseable EXIF timestamp %r", path.name, raw)
    return taken_at, width, height


def make_thumbnail(src: Path, dst: Path, size: int = THUMB_SIZE) -> None:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        im.thumbnail((size, size))
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, "JPEG", quality=85, optimize=True)


def _load_meta_cache(path: Path) -> dict[str, dict]:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("ignoring unreadable scan cache %s", path)
    return {}


def scan_batch(
    paths: BatchPaths,
    limit: int | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> list[Photo]:
    """Return the batch's photos in capture order, with thumbnails generated.

    progress(phase, done, total) is called while reading metadata and making thumbnails.
    """
    files = sorted(
        (p for p in paths.raw.iterdir() if p.is_file() and p.suffix.lower() in RAW_EXTENSIONS),
        key=lambda p: natural_key(p.name),
    )
    if not files:
        raise FileNotFoundError(f"no images in {paths.raw}")

    paths.cache.mkdir(parents=True, exist_ok=True)
    cache_path = paths.cache / "scan.json"
    cache = _load_meta_cache(cache_path)
    meta: dict[str, dict] = {}
    for i, f in enumerate(files, 1):
        stat = f.stat()
        entry = cache.get(f.name)
        if not entry or entry.get("mtime") != stat.st_mtime or entry.get("size") != stat.st_size:
            taken_at, width, height = read_metadata(f)
            entry = {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "taken_at": taken_at.isoformat() if taken_at else None,
                "width": width,
                "height": height,
            }
            if i % 50 == 0:
                log.info("read metadata %d/%d", i, len(files))
            if progress:
                progress("metadata", i, len(files))
        meta[f.name] = entry
    cache_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")

    def order_key(f: Path) -> tuple:
        ts = meta[f.name]["taken_at"]
        return (ts is None, ts or "", natural_key(f.name))

    files.sort(key=order_key)
    if limit:
        files = files[:limit]

    photos: list[Photo] = []
    prev: datetime | None = None
    for seq, f in enumerate(files, 1):
        m = meta[f.name]
        taken_at = datetime.fromisoformat(m["taken_at"]) if m["taken_at"] else None
        gap = (taken_at - prev).total_seconds() if (taken_at and prev) else 0.0
        photos.append(Photo(seq, f.name, taken_at, gap, m["width"], m["height"]))
        prev = taken_at or prev

    missing = [p for p in photos if p.taken_at is None]
    if missing:
        log.warning("%d photo(s) have no EXIF timestamp; they are ordered by name at the end", len(missing))

    ensure_thumbnails(paths, photos, progress)
    return photos


def thumb_path(paths: BatchPaths, photo: Photo) -> Path:
    return paths.thumbs / f"{photo.stem}.jpg"


def ensure_thumbnails(
    paths: BatchPaths,
    photos: list[Photo],
    progress: Callable[[str, int, int], None] | None = None,
) -> None:
    todo = []
    for p in photos:
        src, dst = paths.raw / p.raw_file, thumb_path(paths, p)
        if not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime:
            todo.append((src, dst))
    if not todo:
        log.info("thumbnails up to date (%d)", len(photos))
        return
    log.info("generating %d thumbnail(s) in %s", len(todo), paths.thumbs)
    for i, (src, dst) in enumerate(todo, 1):
        make_thumbnail(src, dst)
        if i % 50 == 0 or i == len(todo):
            log.info("thumbnails %d/%d", i, len(todo))
        if progress:
            progress("thumbnails", i, len(todo))
