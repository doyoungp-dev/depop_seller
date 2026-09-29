"""manifest.csv: one row per photo, the human-reviewed truth for a batch.

Editable columns: item_id (which item the photo belongs to), photo_no (its position in the
item; 1 is Depop's cover photo), skip (1 = spare, not used), deleted (1 = not for sale at all;
implies skip), view. raw_image/ is never touched by any of this. The review UI edits these; by
hand, give a photo the number of the item it belongs to (any unused number starts a new
item) and run `sort` again - ids are renumbered densely in order of first appearance and
photo_no is renumbered within each item, so gaps and duplicates never matter.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .config import VIEWS

COLUMNS = [
    "seq", "raw_file", "taken_at", "gap_s",
    "item_id", "photo_no", "skip", "deleted",
    "view", "confidence", "note",
]
EDITABLE = ("item_id", "photo_no", "skip", "deleted", "view")
UNUSED = 0  # item_id of skipped photos


@dataclass
class Row:
    seq: int
    raw_file: str
    taken_at: str
    gap_s: int
    item_id: int = UNUSED
    photo_no: int = 0
    skip: bool = False
    deleted: bool = False      # excluded for good (raw file untouched); implies skip
    view: str = "other"
    confidence: str = ""
    note: str = ""

    @property
    def stem(self) -> str:
        return Path(self.raw_file).stem


def _flag(value: str, column: str, line: int) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "y", "x"):
        return True
    if v in ("", "0", "false", "no", "n"):
        return False
    raise ValueError(f"line {line}: {column} must be 0 or 1, got {value!r}")


def _to_int(value: str | None) -> int:
    try:
        return int(round(float(value or 0)))
    except ValueError:
        return 0


def normalize(rows: list[Row]) -> list[Row]:
    """Make item_id / photo_no canonical, in place.

    Skipped photos get item 0. Non-skipped photos without an item each become a new item.
    Items are renumbered 1..n by the position of their first photo; photos are renumbered
    within each item by (photo_no, seq).
    """
    next_fresh = max((r.item_id for r in rows), default=0) + 1
    for r in rows:
        if r.deleted:
            r.skip = True
        if r.skip:
            r.item_id, r.photo_no = UNUSED, 0
        elif r.item_id <= 0:
            r.item_id, next_fresh = next_fresh, next_fresh + 1

    by_item: dict[int, list[Row]] = {}
    for r in rows:
        if not r.skip:
            by_item.setdefault(r.item_id, []).append(r)
    ordered = sorted(by_item.values(), key=lambda g: min(r.seq for r in g))
    for new_id, group in enumerate(ordered, 1):
        group.sort(key=lambda r: (r.photo_no if r.photo_no > 0 else 10**9, r.seq))
        for n, r in enumerate(group, 1):
            r.item_id, r.photo_no = new_id, n
    return rows


def read_manifest(path: Path) -> list[Row]:
    rows: list[Row] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in ("raw_file", "item_id") if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: missing column(s) {missing}")
        for line, rec in enumerate(reader, 2):
            raw_file = (rec.get("raw_file") or "").strip()
            if not raw_file:
                continue
            view = (rec.get("view") or "other").strip().lower() or "other"
            if view not in VIEWS:
                raise ValueError(f"line {line}: unknown view {view!r}; expected one of {VIEWS}")
            rows.append(Row(
                seq=len(rows) + 1,
                raw_file=raw_file,
                taken_at=(rec.get("taken_at") or "").strip(),
                gap_s=_to_int(rec.get("gap_s")),
                item_id=_to_int(rec.get("item_id")),
                photo_no=_to_int(rec.get("photo_no")),
                skip=_flag(rec.get("skip", ""), "skip", line),
                deleted=_flag(rec.get("deleted", ""), "deleted", line),
                view=view,
                confidence=(rec.get("confidence") or "").strip(),
                note=(rec.get("note") or "").strip(),
            ))
    if not rows:
        raise ValueError(f"{path.name}: no rows")
    return normalize(rows)


def write_manifest(path: Path, rows: list[Row]) -> None:
    normalize(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "seq": r.seq,
                "raw_file": r.raw_file,
                "taken_at": r.taken_at,
                "gap_s": r.gap_s,
                "item_id": r.item_id or "",
                "photo_no": r.photo_no or "",
                "skip": int(r.skip),
                "deleted": int(r.deleted),
                "view": r.view,
                "confidence": r.confidence,
                "note": r.note,
            })


def items(rows: list[Row]) -> dict[int, list[Row]]:
    """item_id -> its photos in photo_no order (skipped photos excluded)."""
    out: dict[int, list[Row]] = {}
    for r in sorted(rows, key=lambda r: (r.item_id, r.photo_no)):
        if not r.skip:
            out.setdefault(r.item_id, []).append(r)
    return out


def unused(rows: list[Row]) -> list[Row]:
    """Spare photos: skipped but not deleted."""
    return [r for r in rows if r.skip and not r.deleted]


def deleted(rows: list[Row]) -> list[Row]:
    return [r for r in rows if r.deleted]


def summary(rows: list[Row]) -> str:
    grouped = items(rows)
    kept = sum(len(g) for g in grouped.values())
    sizes = [len(g) for g in grouped.values()] or [0]
    low = sum(1 for r in rows if r.confidence == "low" and not r.skip)
    n_deleted = sum(1 for r in rows if r.deleted)
    return (
        f"{len(grouped)} items, {kept} photos kept, {len(rows) - kept - n_deleted} unused, {n_deleted} deleted; "
        f"photos per item min/avg/max = {min(sizes)}/{sum(sizes) / len(sizes):.1f}/{max(sizes)}; "
        f"{low} low-confidence photos"
    )
