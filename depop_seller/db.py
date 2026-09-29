"""Local SQLite state: one row per item and per sorted photo. Step 2 builds on this."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import config
from .manifest import Row, items

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    batch        TEXT    NOT NULL,
    item_id      INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'sorted',   -- sorted | described | listed | skipped
    photo_count  INTEGER NOT NULL,
    description  TEXT,
    category     TEXT,
    brand        TEXT,
    condition    TEXT,
    color        TEXT,
    size         TEXT,
    price        REAL,
    depop_url    TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (batch, item_id)
);
CREATE TABLE IF NOT EXISTS photos (
    batch        TEXT    NOT NULL,
    item_id      INTEGER NOT NULL,
    photo_no     INTEGER NOT NULL,
    view         TEXT    NOT NULL,
    raw_file     TEXT    NOT NULL,
    sorted_file  TEXT    NOT NULL,
    taken_at     TEXT,
    PRIMARY KEY (batch, item_id, photo_no)
);
"""

# Item-level fields that later steps fill in; kept across a re-apply when the item's photos are unchanged.
CARRIED_FIELDS = ("status", "description", "category", "brand", "condition", "color", "size", "price", "depop_url",
                  "facts", "measurements", "material")


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH)  # resolved at call time so tests can redirect it
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release; SQLite has no ADD COLUMN IF NOT EXISTS."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    for column, ddl in (("facts", "facts TEXT"), ("measurements", "measurements TEXT"), ("material", "material TEXT")):
        if column not in existing:
            with conn:
                conn.execute(f"ALTER TABLE items ADD COLUMN {ddl}")


def replace_batch(conn: sqlite3.Connection, batch: str, rows: list[Row], sorted_names: dict[str, str]) -> int:
    """Rewrite a batch's items and photos from the reviewed manifest.

    sorted_names maps raw_file -> file name written to sort_image/. Returns the item count.
    """
    now = datetime.now().isoformat(timespec="seconds")

    # Remember what later steps wrote for items whose photo set has not changed.
    carried: dict[frozenset[str], dict] = {}
    for it in conn.execute("SELECT * FROM items WHERE batch = ?", (batch,)):
        files = frozenset(
            r["raw_file"] for r in conn.execute(
                "SELECT raw_file FROM photos WHERE batch = ? AND item_id = ?", (batch, it["item_id"])
            )
        )
        carried[files] = {k: it[k] for k in CARRIED_FIELDS} | {"created_at": it["created_at"]}

    with conn:
        conn.execute("DELETE FROM photos WHERE batch = ?", (batch,))
        conn.execute("DELETE FROM items WHERE batch = ?", (batch,))
        for item_id, group in items(rows).items():
            kept = [r for r in group if not r.skip]
            if not kept:
                continue
            old = carried.get(frozenset(r.raw_file for r in kept), {})
            conn.execute(
                "INSERT INTO items (batch, item_id, status, photo_count, description, category, brand, condition, "
                "color, size, price, depop_url, facts, measurements, material, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch, item_id, old.get("status") or "sorted", len(kept), old.get("description"),
                 old.get("category"), old.get("brand"), old.get("condition"), old.get("color"),
                 old.get("size"), old.get("price"), old.get("depop_url"), old.get("facts"), old.get("measurements"),
                 old.get("material"), old.get("created_at") or now, now),
            )
            conn.executemany(
                "INSERT INTO photos (batch, item_id, photo_no, view, raw_file, sorted_file, taken_at) VALUES (?,?,?,?,?,?,?)",
                [(batch, item_id, r.photo_no, r.view, r.raw_file, sorted_names[r.raw_file], r.taken_at or None)
                 for r in kept],
            )
    return len(items(rows))


def sync_from_manifest(conn: sqlite3.Connection, batch: str, rows: list[Row]) -> int:
    """Mirror the reviewed manifest into the DB (no files needed); keeps status/description per item."""
    names = {r.raw_file: f"{r.item_id}_{r.photo_no}.jpg" for r in rows if not r.skip}
    return replace_batch(conn, batch, rows, names)


ITEM_FIELDS = ("status", "description", "depop_url", "category", "brand", "condition", "color", "size", "price",
               "facts", "measurements", "material")


def update_item(conn: sqlite3.Connection, batch: str, item_id: int, **fields) -> None:
    bad = set(fields) - set(ITEM_FIELDS)
    if bad:
        raise ValueError(f"unknown item field(s): {sorted(bad)}")
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with conn:
        conn.execute(f"UPDATE items SET {assignments}, updated_at = ? WHERE batch = ? AND item_id = ?",
                     (*fields.values(), datetime.now().isoformat(timespec="seconds"), batch, item_id))


def item_rows(conn: sqlite3.Connection, batch: str) -> dict[int, dict]:
    return {r["item_id"]: dict(r) for r in conn.execute("SELECT * FROM items WHERE batch = ?", (batch,))}
