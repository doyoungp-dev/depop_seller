from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from depop_seller import db
from depop_seller.apply import apply_batch
from depop_seller.group import Decision, decisions_to_rows, group_local
from depop_seller.manifest import Row, items, normalize, read_manifest, summary, unused, write_manifest
from depop_seller.review import ReviewSession
from depop_seller.scan import scan_batch, thumb_path


def test_scan_orders_by_time_and_makes_thumbnails(batch):
    photos = scan_batch(batch)
    assert [p.raw_file for p in photos] == [f"IMG_{1000 + i}.jpg" for i in range(1, 9)]
    assert [round(p.gap_s) for p in photos] == [0, 10, 20, 300, 15, 400, 60, 5]
    assert all(thumb_path(batch, p).is_file() for p in photos)
    assert (batch.cache / "scan.json").is_file()
    # second scan reuses the cache and thumbnails without error
    assert [p.seq for p in scan_batch(batch)] == list(range(1, 9))


def test_scan_limit(batch):
    assert len(scan_batch(batch, limit=3)) == 3


def test_local_engine_splits_on_gap_and_colour(batch):
    photos = scan_batch(batch)
    rows = decisions_to_rows(photos, group_local(batch, photos))
    assert [r.item_id for r in rows] == [1, 1, 1, 2, 2, 3, 4, 4]  # the 400 s gap splits the blue item: expected fallback behaviour
    assert [r.photo_no for r in rows] == [1, 2, 3, 1, 2, 1, 1, 2]


def test_non_product_decisions_become_skips(batch):
    photos = scan_batch(batch)
    decisions = [Decision(p.raw_file, same_item_as_previous=i > 0, view="front", confidence="high") for i, p in enumerate(photos)]
    decisions[3].is_product = False
    decisions[3].same_item_as_previous = False
    rows = decisions_to_rows(photos, decisions)
    assert rows[3].skip and rows[3].item_id == 0 and rows[3].note == "not a product photo"
    assert [r.item_id for r in rows] == [1, 1, 1, 0, 2, 2, 2, 2]


def _rows(spec: list[tuple[int, int, bool]]) -> list[Row]:
    """spec: (item_id, photo_no, skip) per photo."""
    return [Row(seq=i, raw_file=f"IMG_{i}.HEIC", taken_at="", gap_s=0, item_id=it, photo_no=no, skip=sk)
            for i, (it, no, sk) in enumerate(spec, 1)]


def test_normalize_renumbers_items_and_photos():
    rows = _rows([(7, 2, False), (7, 1, False), (3, 0, False), (0, 0, True), (99, 0, False), (7, 0, False)])
    normalize(rows)
    # item 7 appears first -> 1 (photos ordered by photo_no then seq), item 3 -> 2, item 99 -> 3
    assert [(r.item_id, r.photo_no) for r in rows] == [(1, 2), (1, 1), (2, 1), (0, 0), (3, 1), (1, 3)]


def test_deleted_photos_are_skipped_and_kept_apart_from_unused(tmp_path: Path):
    from depop_seller.manifest import deleted

    rows = _rows([(1, 1, False), (1, 2, False), (2, 1, False)])
    rows[1].deleted = True                      # deleted implies skip, even if skip was left at 0
    rows[2].skip = True                         # a spare
    normalize(rows)
    assert [(r.item_id, r.skip, r.deleted) for r in rows] == [(1, False, False), (0, True, True), (0, True, False)]
    assert [r.seq for r in unused(rows)] == [3] and [r.seq for r in deleted(rows)] == [2]
    path = tmp_path / "manifest.csv"
    write_manifest(path, rows)
    back = read_manifest(path)
    assert [(r.skip, r.deleted) for r in back] == [(False, False), (True, True), (True, False)]
    assert "1 deleted" in summary(back)


def test_normalize_gives_unassigned_photos_their_own_item():
    rows = _rows([(1, 1, False), (0, 0, False), (1, 2, False)])
    normalize(rows)
    assert [r.item_id for r in rows] == [1, 2, 1]


def test_manifest_roundtrip(tmp_path: Path):
    rows = [
        Row(seq=1, raw_file="a.HEIC", taken_at="2026-01-01T10:00:00", gap_s=0, item_id=1, view="detail"),
        Row(seq=2, raw_file="b.HEIC", taken_at="", gap_s=5, item_id=1, view="front"),
        Row(seq=3, raw_file="c.HEIC", taken_at="", gap_s=9, item_id=1, view="back", confidence="low", note="x"),
        Row(seq=4, raw_file="d.HEIC", taken_at="", gap_s=200, item_id=2, view="other"),
        Row(seq=5, raw_file="e.HEIC", taken_at="", gap_s=1, skip=True),
    ]
    path = tmp_path / "manifest.csv"
    write_manifest(path, rows)
    back = read_manifest(path)
    assert [(r.item_id, r.photo_no, r.skip) for r in back] == [(1, 1, False), (1, 2, False), (1, 3, False), (2, 1, False), (0, 0, True)]
    assert [r.view for r in back] == ["detail", "front", "back", "other", "other"]
    assert back[2].confidence == "low" and back[2].note == "x"
    assert [r.raw_file for r in unused(back)] == ["e.HEIC"]


def test_manifest_rejects_bad_view(tmp_path: Path):
    path = tmp_path / "manifest.csv"
    path.write_text("raw_file,item_id,view\na.jpg,1,sideways\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown view"):
        read_manifest(path)


def test_review_session_state_and_save(batch):
    photos = scan_batch(batch)
    rows = decisions_to_rows(photos, group_local(batch, photos))
    write_manifest(batch.manifest, rows)
    session = ReviewSession(batch, rows)

    state = session.state()
    assert [i["id"] for i in state["items"]] == [1, 2, 3, 4]
    assert [len(i["photos"]) for i in state["items"]] == [3, 2, 1, 2]

    # move photo 6 back into item 2, drop photo 8 into unused, delete photo 7, reorder item 1, relabel a view
    state = session.save({
        "items": [[3, 1, 2], [4, 5, 6]],
        "unused": [8],
        "deleted": [7],
        "views": {"3": "back"},
    })
    assert [[p["seq"] for p in i["photos"]] for i in state["items"]] == [[3, 1, 2], [4, 5, 6]]
    assert [p["seq"] for p in state["unused"]] == [8] and [p["seq"] for p in state["deleted"]] == [7]
    assert state["items"][0]["photos"][0]["view"] == "back"
    assert (batch.root / "manifest.bak.csv").is_file()
    reloaded = read_manifest(batch.manifest)
    assert [(r.item_id, r.photo_no, r.deleted) for r in reloaded] == [
        (1, 2, False), (1, 3, False), (1, 1, False), (2, 1, False), (2, 2, False), (2, 3, False), (0, 0, True), (0, 0, False)]
    # bring the deleted photo back as its own item
    state = session.save({"items": [[3, 1, 2], [4, 5, 6], [7]], "unused": [8], "deleted": [], "views": {}})
    assert state["deleted"] == [] and [i["id"] for i in state["items"]] == [1, 2, 3]

    with pytest.raises(ValueError, match="exactly once"):
        session.save({"items": [[1, 2]], "unused": [], "views": {}})

    # a manifest changed behind the page's back must not be overwritten
    write_manifest(batch.manifest, rows)
    batch.manifest.touch()
    import os
    os.utime(batch.manifest, ns=(batch.manifest.stat().st_atime_ns, batch.manifest.stat().st_mtime_ns + 10**9))
    with pytest.raises(FileExistsError, match="changed by something else"):
        session.save({"items": [[3, 1, 2], [4, 5, 6], [7]], "unused": [8], "views": {}})

    # background apply: after a Reload (session re-synced with disk), 6 kept photos end up in sort_image
    session = ReviewSession(batch, read_manifest(batch.manifest))
    session.save({"items": [[3, 1, 2], [4, 5, 6]], "unused": [8], "deleted": [7], "views": {}})
    session.start_apply(force=False)
    for _ in range(200):
        if not session.apply_state["running"]:
            break
        time.sleep(0.05)
    assert session.apply_state == {"running": False, "done": 6, "total": 6, "written": 6, "error": None}
    written = sorted(p.name for p in batch.sort_image.glob("*.jpg"))
    assert written == ["1_1.jpg", "1_2.jpg", "1_3.jpg", "2_1.jpg", "2_2.jpg", "2_3.jpg"]   # no deleted, no unused
    with pytest.raises(FileExistsError):
        session.start_apply(force=False)


def test_apply_matches_merges_chains_and_appends_photos():
    from depop_seller.group import Match, apply_matches

    rows = _rows([(1, 1, False), (1, 2, False), (2, 1, False), (3, 1, False), (3, 2, False), (4, 1, False)])
    matches = [
        Match(query_item=3, catalog_item=1, confidence="high", note=""),
        Match(query_item=4, catalog_item=3, confidence="high", note=""),      # chain: 4 -> 3 -> 1
        Match(query_item=2, catalog_item=1, confidence="medium", note=""),    # below threshold
    ]
    assert apply_matches(rows, matches, min_confidence="high") == 2
    assert [(r.item_id, r.photo_no) for r in rows] == [(1, 1), (1, 2), (2, 1), (1, 3), (1, 4), (1, 5)]


def test_learn_compare_and_merge_scoring():
    from depop_seller.group import Match
    from depop_seller.learn import compare, expected_merges, score_merges

    proposal = _rows([(1, 1, False), (1, 2, False), (2, 1, False), (3, 1, False), (4, 1, False), (5, 1, False)])
    truth = _rows([(1, 1, False), (1, 2, False), (1, 3, False), (3, 1, False), (0, 0, True), (3, 2, False)])
    normalize(proposal); normalize(truth)
    c = compare(proposal, truth)
    assert c.proposed_items == 5 and c.truth_items == 2
    assert c.false_splits == [3] and c.missed_splits == [] and c.dropped == [5]
    assert [s["item"] for s in c.stitched_items] == [2] and c.stitched_items[0]["from_proposed_items"] == [3, 5]
    # both the adjacent merge (1+2) and the stitched one (3+5) are merges the pass should find
    assert expected_merges(proposal, truth) == {frozenset((1, 2)), frozenset((3, 5))}
    scores = score_merges([Match(query_item=5, catalog_item=3, confidence="high", note=""),
                           Match(query_item=4, catalog_item=1, confidence="medium", note="")],
                          expected_merges(proposal, truth))
    assert scores == {"expected": [(1, 2), (3, 5)], "found": {"3/5": "high"}, "missed": [(1, 2)], "false": {"1/4": "medium"}}


def test_review_server_lists_batches_and_guards_grouping(batch):
    from depop_seller.review import ReviewServer

    server = ReviewServer()
    info = server.batches()
    assert [b["name"] for b in info["batches"]] == ["20990101"]
    first = info["batches"][0]
    assert {k: first[k] for k in ("name", "photos", "has_manifest", "sorted", "items", "listed")} ==         {"name": "20990101", "photos": 8, "has_manifest": False, "sorted": 0, "items": 0, "listed": 0}
    assert first["updated"][:2] == "20"                       # shown on the batches page
    assert info["api_key"] is False and "claude-opus-5" in info["models"]

    with pytest.raises(LookupError, match="no manifest"):
        server.session("20990101")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        server.start_group("20990101", "claude-opus-5")
    with pytest.raises(ValueError, match="unknown model"):
        server.start_group("20990101", "gpt-9")

    photos = scan_batch(batch)
    write_manifest(batch.manifest, decisions_to_rows(photos, group_local(batch, photos)))
    listing = server.batches()["batches"][0]
    assert listing["has_manifest"] is True and listing["items"] == 4 and listing["listed"] == 0
    assert server.session(None).state()["batch"] == "20990101"       # None -> newest batch
    assert server.session("20990101") is server.session(None)         # cached
    assert Path(server.new_batch("Autumn knits")["raw"]).parts[-2:] == ("Autumn knits", "raw_image")
    assert [b["name"] for b in server.batches()["batches"]] == ["Autumn knits", "20990101"]  # newest first


def test_apply_writes_sorted_files_and_db(batch, tmp_path: Path):
    photos = scan_batch(batch)
    rows = decisions_to_rows(photos, group_local(batch, photos))
    rows[1].skip = True
    write_manifest(batch.manifest, rows)
    rows = read_manifest(batch.manifest)

    n = apply_batch(batch, rows)
    assert n == 7
    names = sorted(p.name for p in batch.sort_image.glob("*.jpg"))
    assert names == sorted(["1_1.jpg", "1_2.jpg", "2_1.jpg", "2_2.jpg", "3_1.jpg", "4_1.jpg", "4_2.jpg"])
    assert (batch.sort_image / "manifest.csv").is_file()

    with pytest.raises(FileExistsError):
        apply_batch(batch, rows)
    seen = []
    assert apply_batch(batch, rows, force=True, progress=lambda d, t: seen.append((d, t))) == 7
    assert seen[-1] == (7, 7)

    conn = sqlite3.connect(tmp_path / "test.db")
    assert conn.execute("SELECT COUNT(*) FROM items WHERE batch='20990101'").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE batch='20990101'").fetchone()[0] == 7
    conn.close()


def test_db_keeps_description_when_photos_unchanged(batch, tmp_path: Path):
    rows = normalize([
        Row(seq=1, raw_file="a.jpg", taken_at="", gap_s=0, item_id=1),
        Row(seq=2, raw_file="b.jpg", taken_at="", gap_s=0, item_id=2),
    ])
    conn = db.connect(tmp_path / "test.db")
    db.replace_batch(conn, "b", rows, {"a.jpg": "1_1.jpg", "b.jpg": "2_1.jpg"})
    conn.execute("UPDATE items SET description='nice red top', status='described' WHERE batch='b' AND item_id=1")
    conn.commit()
    # re-apply with items swapped: the description follows the photo set, not the number
    rows = normalize([
        Row(seq=1, raw_file="b.jpg", taken_at="", gap_s=0, item_id=1),
        Row(seq=2, raw_file="a.jpg", taken_at="", gap_s=0, item_id=2),
    ])
    db.replace_batch(conn, "b", rows, {"b.jpg": "1_1.jpg", "a.jpg": "2_1.jpg"})
    row = conn.execute("SELECT description, status FROM items WHERE batch='b' AND item_id=2").fetchone()
    assert (row["description"], row["status"]) == ("nice red top", "described")
    conn.close()
    assert items(rows)[2][0].raw_file == "a.jpg"


def test_manual_photo_order_survives_reload_and_apply(batch):
    """The reviewer's photo order is the contract: sort_image names and the DB must follow it."""
    import csv

    photos = scan_batch(batch)
    rows = decisions_to_rows(photos, group_local(batch, photos))
    # reviewer puts the third shot first in item 1 and deletes the rest of the batch
    by_seq = {r.seq: r for r in rows}
    by_seq[3].photo_no, by_seq[1].photo_no, by_seq[2].photo_no = 1, 2, 3
    for r in rows:
        if r.seq > 3:
            r.deleted = True
    write_manifest(batch.manifest, rows)

    # `sort` on an existing manifest only re-reads and re-writes it
    rows = read_manifest(batch.manifest)
    write_manifest(batch.manifest, rows)
    rows = read_manifest(batch.manifest)
    assert [(r.seq, r.photo_no) for r in rows if not r.skip] == [(1, 2), (2, 3), (3, 1)]

    apply_batch(batch, rows)
    with (batch.sort_image / "manifest.csv").open(newline="", encoding="utf-8") as fh:
        written = {rec["sorted_file"]: rec["raw_file"] for rec in csv.DictReader(fh)}
    assert written == {"1_1.jpg": "IMG_1003.jpg", "1_2.jpg": "IMG_1001.jpg", "1_3.jpg": "IMG_1002.jpg"}
    conn = db.connect()
    assert [r["raw_file"] for r in conn.execute("SELECT raw_file FROM photos ORDER BY photo_no")] == ["IMG_1003.jpg", "IMG_1001.jpg", "IMG_1002.jpg"]
    conn.close()


def test_sell_state_syncs_db_from_manifest_and_keeps_status(batch):
    from depop_seller.review import ReviewServer

    photos = scan_batch(batch)
    write_manifest(batch.manifest, decisions_to_rows(photos, group_local(batch, photos)))
    server = ReviewServer()

    state = server.sell_state("20990101")
    assert [i["id"] for i in state["items"]] == [1, 2, 3, 4]
    assert state["items"][0]["status"] == "sorted" and state["items"][0]["photos"][0]["stem"] == "IMG_1001"

    meta = server.sell_update("20990101", 2, {"status": "listed", "description": "blue top", "depop_url": "https://depop.com/x"})
    assert (meta["status"], meta["description"]) == ("listed", "blue top")
    with pytest.raises(ValueError, match="status must be"):
        server.sell_update("20990101", 2, {"status": "sold"})

    # re-sync after an unrelated manifest change: item 2's photo set is unchanged, so its status survives
    rows = read_manifest(batch.manifest)
    rows[-1].deleted = True
    write_manifest(batch.manifest, rows)
    state = server.sell_state("20990101")
    item2 = next(i for i in state["items"] if i["id"] == 2)
    assert (item2["status"], item2["description"], item2["depop_url"]) == ("listed", "blue top", "https://depop.com/x")
    assert len(state["items"][-1]["photos"]) == 1


def test_lister_status_and_double_submit_guard():
    from depop_seller.listing import Lister

    lister = Lister(profile_dir=Path("unused"), headless=True)
    assert lister.status("b/1") == {"running": False, "phase": "idle", "error": None, "uploaded": 0}
    # queue a job whose worker thread is replaced by a no-op so no browser starts
    lister._worker = lambda: None
    st = lister.submit("b/1", prepare=lambda: [])
    assert st["running"] and st["phase"] == "queued"
    with pytest.raises(RuntimeError, match="already"):
        lister.submit("b/1", prepare=lambda: [])


def test_sell_photos_lists_upload_urls_in_order(batch):
    from depop_seller.review import ReviewServer

    photos = scan_batch(batch)
    rows = decisions_to_rows(photos, group_local(batch, photos))
    by_seq = {r.seq: r for r in rows}
    by_seq[3].photo_no, by_seq[1].photo_no, by_seq[2].photo_no = 1, 2, 3        # reviewer's order
    write_manifest(batch.manifest, rows)
    out = ReviewServer().sell_photos("20990101", 1, "http://127.0.0.1:8765")
    assert [p["name"] for p in out["photos"]] == ["1_1.jpg", "1_2.jpg", "1_3.jpg"]
    assert [p["url"].split("/large/")[1].split(".jpg")[0] for p in out["photos"]] == ["IMG_1003", "IMG_1001", "IMG_1002"]
    assert (batch.cache / "large" / "IMG_1003.jpg").is_file()                    # converted up front


def test_describe_guards_and_db_migration(batch, tmp_path: Path):
    from depop_seller.describe import pick_photos
    from depop_seller.review import ReviewServer

    photos = scan_batch(batch)
    write_manifest(batch.manifest, decisions_to_rows(photos, group_local(batch, photos)))
    server = ReviewServer()
    with pytest.raises(RuntimeError, match="Claude Code CLI not found"):   # descriptions never touch the API
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("depop_seller.describe.find_claude_cli", lambda: None)
            server.start_describe("20990101", [1], {"1": "preowned"})

    # facts are a first-class item field (added by migration on old databases)
    conn = db.connect(tmp_path / "old.db")
    conn.execute("ALTER TABLE items DROP COLUMN facts")
    conn.commit()
    conn.close()
    conn = db.connect(tmp_path / "old.db")
    assert {r["name"] for r in conn.execute("PRAGMA table_info(items)")} >= {"facts", "measurements"}
    conn.close()
    meta = server.sell_update("20990101", 1, {"facts": "brand X, L, stain on cuff"})
    assert meta["facts"] == "brand X, L, stain on cuff"
    state = server.sell_state("20990101")
    assert state["items"][0]["facts"] == "brand X, L, stain on cuff"
    assert "api_key" not in state and "cost_per_item" not in state          # the Sell page has no API path at all

    # the drafting prompt gets the cover plus label/detail close-ups first
    rows = [Row(seq=i, raw_file=f"IMG_{i}.HEIC", taken_at="", gap_s=0, item_id=1, photo_no=i, view=v)
            for i, v in enumerate(["front", "back", "label", "side", "detail", "flaw", "label"], 1)]
    cover, closeups = pick_photos(rows)
    assert cover.seq == 1 and [r.seq for r in closeups] == [3, 5, 6]

def _stub_claude_cli(tmp_path: Path, inner: dict) -> Path:
    """A fake `claude.cmd` speaking the stream-json protocol: it reports the credential source it
    sees (ANTHROPIC_API_KEY in its environment or not), then a result envelope wrapping `inner`."""
    import json
    import sys
    import textwrap

    (tmp_path / "inner.json").write_text(json.dumps(inner), encoding="utf-8")
    stub_py = tmp_path / "stub.py"
    stub_py.write_text(textwrap.dedent("""
        import json, os, sys
        argv = sys.argv[2:]
        assert argv[:6] == ['-p', '--output-format', 'stream-json', '--verbose', '--allowedTools', 'Read'], argv
        assert argv[6] == '--max-turns' and argv[8:] == ['--model', 'opus'], argv
        prompt = sys.stdin.read()
        if 'HOUSE STYLE' in prompt:                       # a listing draft
            assert 'SELLER' in prompt and 'IMG_1003' in prompt
        else:                                             # a style rewrite
            assert 'INSTRUCTION' in prompt and 'THE FILE AS IT IS NOW' in prompt
        source = 'ANTHROPIC_API_KEY' if os.environ.get('ANTHROPIC_API_KEY') else 'none'
        print(json.dumps({'type': 'system', 'subtype': 'init', 'apiKeySource': source, 'model': 'claude-opus-5'}), flush=True)
        if source != 'none':
            import time
            time.sleep(1.5)          # a model call in flight; the guard must kill the whole tree before it lands
            open(os.environ['STUB_BILLED_MARK'], 'w').write('the model was called with an API key')
        inner = json.load(open(sys.argv[1], encoding='utf-8'))
        nl = chr(10)
        print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                          'result': 'Sure!' + nl + '```json' + nl + json.dumps(inner) + nl + '```',
                          'usage': {'input_tokens': 42, 'output_tokens': 7}}))
        """), encoding="utf-8")
    crlf = chr(13) + chr(10)
    stub = tmp_path / "claude.cmd"
    stub.write_text("@echo off" + crlf + f'"{sys.executable}" "{stub_py}" "{tmp_path / "inner.json"}" %*' + crlf,
                    encoding="utf-8")
    return stub


nl = chr(10)
STUB_DRAFT = {"text": nl.join(["yourshop Vintage Y2K Stub top in red.", "Ribbed knit with a scoop neck. Fitted.",
                               "Very downtown girl vibe.", "Brand: Stub", "Size: M " + chr(8212) + " fits M, see measurements",
                               "Material: Cotton blend", "Color: Red", "Era: Y2K", "Condition: Preowned",
                               "Measurements: see photos", "XS-S mannequin shown for fit reference.",
                               "Bundle for a discount. Shorts not included.", "#y2k #downtowngirl #mcbling #glam"]),
              "title": "Stub Top", "brand": "Stub", "size": "M", "material": "Cotton blend", "color": "Red",
              "condition": "Preowned", "measurements": "see photos", "category_hint": "Tops",
              "missing": ["measurements"], "tip": "Add a tape shot."}


def _cover_first_session(batch):
    from depop_seller.review import ReviewSession

    photos = scan_batch(batch)
    rows = decisions_to_rows(photos, group_local(batch, photos))
    by_seq = {r.seq: r for r in rows}
    by_seq[3].photo_no, by_seq[1].photo_no, by_seq[2].photo_no = 1, 2, 3       # IMG_1003 is the cover
    write_manifest(batch.manifest, rows)
    rows = read_manifest(batch.manifest)
    return rows, ReviewSession(batch, rows)


def test_claude_code_engine_parses_cli_envelope(batch, tmp_path: Path, monkeypatch):
    """draft_description drives `claude -p` over stdin (stream-json) and parses the result envelope."""
    from depop_seller.describe import draft_description

    stub = _stub_claude_cli(tmp_path, STUB_DRAFT)
    monkeypatch.setenv("STUB_BILLED_MARK", str(tmp_path / "billed.txt"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rows, session = _cover_first_session(batch)
    res = draft_description(batch, items(rows)[1], "preowned", large_image=session.large_image, style="STYLE", cli=stub)
    assert res.model == "claude-code" and res.cost is None and res.usage.input_tokens == 42 and res.credential == "none"
    assert res.draft.text.startswith("yourshop") and res.draft.missing == ["measurements"]
    assert res.draft.material == "Cotton blend" and res.draft.tip == "Add a tape shot."


def test_claude_code_never_sees_the_api_key(batch, tmp_path: Path, monkeypatch):
    """The server holds ANTHROPIC_API_KEY for photo grouping; the CLI must not inherit it (that
    silently bills the API instead of the subscription - the bug of 2026-09-20)."""
    from depop_seller.describe import BillingRefused, draft_description, run_claude_code, subscription_env

    stub = _stub_claude_cli(tmp_path, STUB_DRAFT)
    mark = tmp_path / "billed.txt"
    monkeypatch.setenv("STUB_BILLED_MARK", str(mark))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://example.invalid")
    assert not any(k.upper().startswith("ANTHROPIC_") for k in subscription_env())
    rows, session = _cover_first_session(batch)
    res = draft_description(batch, items(rows)[1], "preowned", large_image=session.large_image, style="STYLE", cli=stub)
    assert res.credential == "none" and not mark.exists()

    # belt and braces: if Claude Code still reports a key (e.g. one stored in its own config), the run is killed
    monkeypatch.setattr("depop_seller.describe.subscription_env", lambda: dict(os.environ))
    with pytest.raises(BillingRefused, match="ANTHROPIC_API_KEY"):
        run_claude_code("HOUSE STYLE SELLER IMG_1003", cli=stub)
    assert not mark.exists()


def test_start_describe_end_to_end_with_stub_cli(batch, tmp_path: Path, monkeypatch):
    """The Sell page's Draft button: start_describe -> Claude Code -> DB + describe_state + drafts log."""
    from depop_seller.review import ReviewServer

    stub = _stub_claude_cli(tmp_path, STUB_DRAFT)
    monkeypatch.setenv("STUB_BILLED_MARK", str(tmp_path / "billed.txt"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")           # as when the grouping key is set system-wide
    monkeypatch.setattr("depop_seller.describe.find_claude_cli", lambda: stub)
    monkeypatch.setattr("depop_seller.describe.load_style", lambda: "STYLE")
    _cover_first_session(batch)
    server = ReviewServer()
    assert server.sell_state("20990101")["claude_code"] is True
    assert server.start_describe("20990101", [1], {"1": "preowned"}) == {"started": [1]}
    for _ in range(100):
        st = server.describe_state["20990101"][1]
        if not st["running"]:
            break
        time.sleep(0.1)
    assert st["error"] is None and st["engine"] == "claude-code" and st["credential"] == "none" and st["cost"] is None
    assert not (tmp_path / "billed.txt").exists()
    item = server.sell_state("20990101")["items"][0]
    assert item["description"].startswith("yourshop") and item["facts"] == "preowned"
    assert item["fields"]["brand"] == "Stub" and item["fields"]["material"] == "Cotton blend"
    logs = list((batch.cache / "drafts").glob("item001_*.txt"))
    assert len(logs) == 1 and "credential source 'none'" in logs[0].read_text(encoding="utf-8")


def test_sell_reorder_writes_manifest_and_keeps_item_fields(batch):
    """The Sell page's drag and drop: reorder_item rewrites photo_no, backs the manifest up, keeps
    the item's DB fields (same photo set) and refuses bad orders and stale sessions."""
    from depop_seller.review import ReviewServer

    photos = scan_batch(batch)
    write_manifest(batch.manifest, decisions_to_rows(photos, group_local(batch, photos)))
    server = ReviewServer()
    server.sell_update("20990101", 1, {"description": "keep me", "status": "listed"})
    state = server.sell_state("20990101")
    item = state["items"][0]
    seqs = [p["seq"] for p in item["photos"]]
    assert seqs == [1, 2, 3]

    session = server.session("20990101")
    with pytest.raises(ValueError):
        session.reorder_item(1, [3, 1])                        # one photo missing
    with pytest.raises(ValueError):
        session.reorder_item(1, [3, 1, 1])                     # duplicate
    with pytest.raises(LookupError):
        session.reorder_item(99, [3, 1, 2])

    rows = session.reorder_item(1, [3, 1, 2])
    assert [r.stem for r in rows] == ["IMG_1003", "IMG_1001", "IMG_1002"]
    assert (batch.root / "manifest.bak.csv").is_file()
    on_disk = read_manifest(batch.manifest)
    assert [(r.seq, r.photo_no) for r in on_disk if r.item_id == 1] == [(1, 2), (2, 3), (3, 1)]
    assert [i.item_id for i in on_disk if not i.skip][:3] == [1, 1, 1]           # item ids untouched

    state = server.sell_state("20990101")
    item = state["items"][0]
    assert [p["stem"] for p in item["photos"]] == ["IMG_1003", "IMG_1001", "IMG_1002"]
    assert item["description"] == "keep me" and item["status"] == "listed"
    out = server.sell_photos("20990101", 1, "http://127.0.0.1:8765")
    assert [p["url"].split("/large/")[1].split(".jpg")[0] for p in out["photos"]] == ["IMG_1003", "IMG_1001", "IMG_1002"]

    # another window wrote the manifest since this session loaded it -> 409-style refusal
    time.sleep(0.01)
    write_manifest(batch.manifest, on_disk)
    with pytest.raises(FileExistsError):
        session.reorder_item(1, [1, 2, 3])


def test_style_template_matches_the_draft_contract_and_holds_nobody_s_shop():
    """The template is what ships and what a new machine starts from, so it must spell out every
    field the Draft model carries - and must not carry anyone's actual shop."""
    from depop_seller.describe import STYLE_TEMPLATE

    style = STYLE_TEMPLATE.read_text(encoding="utf-8")
    for line in ("Brand:", "Size:", "Material:", "Color:", "Condition:", "Measurements:"):
        assert line in style, line
    assert "## Hashtags" in style and "## Examples" in style
    assert "#y2k" in style                                          # a bank to pick from
    assert "no #fashion" in style                                   # and the ban on filler tags
    assert "[size?]" in style and "[material?]" not in style.split("## Rules")[0].split("Material:")[0]
    # the placeholder handle is the tell: if somebody's real style file were committed over the
    # template it would be gone, along with the rest of this
    assert style.count("yourshop") >= 3
    assert "Replace this paragraph with your own shop" in style


def test_personal_style_starts_from_the_template_and_is_never_overwritten(tmp_path: Path, monkeypatch):
    """A new machine has no description_style.md - the app writes one from the template. Once it
    exists, nothing regenerates it: it is the seller's own file."""
    from depop_seller import describe

    style, template = tmp_path / "description_style.md", tmp_path / "description_style.example.md"
    template.write_text("# Template" + chr(10) + "Brand: ..." + chr(10), encoding="utf-8")
    monkeypatch.setattr(describe, "STYLE_FILE", style)
    monkeypatch.setattr(describe, "STYLE_TEMPLATE", template)

    assert not style.exists()
    assert describe.load_style().startswith("# Template")
    assert style.exists()

    style.write_text("# Mine" + chr(10), encoding="utf-8")
    assert describe.load_style() == "# Mine" + chr(10)               # never clobbered afterwards

    style.unlink()
    template.unlink()
    with pytest.raises(FileNotFoundError):
        describe.load_style()


def test_app_window_uses_a_chromium_browser_and_falls_back(tmp_path: Path, monkeypatch):
    """The pages must open in the user's own Chrome (the helper extension lives there), as an
    --app window; with no Chromium browser installed we fall back to the default browser."""
    from depop_seller import desktop

    fake = tmp_path / "Google Chrome"
    fake.write_text("", encoding="utf-8")
    launched: list[list[str]] = []
    opened: list[str] = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: opened.append(url))

    monkeypatch.setenv("DEPOP_BROWSER", str(fake))
    assert desktop.app_window("http://127.0.0.1:8765/hub") in (fake.name, fake.stem)
    assert launched[0][0] == str(fake)
    assert launched[0][1] == "--app=http://127.0.0.1:8765/hub"
    assert any(a.startswith("--window-size=") for a in launched[0])
    assert not any(a.startswith("--user-data-dir") for a in launched[0])   # must use the real profile
    assert not opened

    monkeypatch.setenv("DEPOP_BROWSER", str(tmp_path / "nope"))
    assert desktop.app_window("http://127.0.0.1:8765/hub") == "default browser"
    assert opened == ["http://127.0.0.1:8765/hub"]


def test_claude_cli_is_found_on_macos_without_PATH(tmp_path: Path, monkeypatch):
    """An app launched from Finder inherits a bare PATH, so the desktop app's bundle and the
    standalone install locations are checked explicitly."""
    from depop_seller import describe

    monkeypatch.setattr(describe.sys, "platform", "darwin")
    monkeypatch.setattr(describe.os, "name", "posix")
    monkeypatch.setattr(describe, "CLI_NAME", "claude")
    monkeypatch.setattr(describe, "CLAUDE_CLI_CANDIDATES", [])
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(describe.Path, "home", classmethod(lambda cls: tmp_path))

    assert describe.find_claude_cli() is None                      # nothing installed yet

    bundle = tmp_path / "Library" / "Application Support" / "Claude" / "claude-code"
    for version in ("2.1.9", "2.1.275"):                            # newest wins, numerically
        (bundle / version).mkdir(parents=True)
        (bundle / version / "claude").write_text("", encoding="utf-8")
    assert describe.find_claude_cli() == bundle / "2.1.275" / "claude"


def test_open_folder_uses_the_platform_command(tmp_path: Path, monkeypatch):
    from depop_seller import desktop

    calls: list[list[str]] = []
    monkeypatch.setattr(desktop.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    desktop.open_folder(tmp_path)
    assert calls == [["open", str(tmp_path)]]


def test_batch_names_are_free_text_but_safe():
    from depop_seller.review import safe_batch_name, safe_photo_name

    for good in ("20261005", "Autumn knits", "y2k-drop_2", "Rory's picks"):
        assert safe_batch_name(f"  {good} ") == good
    assert safe_batch_name("name ") == "name"                     # surrounding blanks are trimmed, not rejected
    for bad in ("", "   ", "a/b", "a" + chr(92) + "b", "..", ".hidden", "name.", "CON", "nul.txt",
                "x" * 61, "a:b"):
        with pytest.raises(ValueError):
            safe_batch_name(bad)

    bs = chr(92)
    assert safe_photo_name("IMG_0001.HEIC") == "IMG_0001.HEIC"
    assert safe_photo_name(f"C:{bs}Users{bs}me{bs}IMG_2.heic") == "IMG_2.heic"   # a path is reduced to its name
    assert safe_photo_name("../../evil.jpg") == "evil.jpg"
    for bad in ("notes.txt", "", "  ", ".hidden.jpg", "movie.mov"):
        with pytest.raises(ValueError):
            safe_photo_name(bad)


def test_photos_can_be_added_from_the_app(batch, tmp_path: Path):
    """Uploading a photo and importing a folder both land in raw_image; neither can escape it."""
    from depop_seller.review import ReviewServer

    server = ReviewServer()
    made = server.new_batch("Autumn knits")
    assert (Path(made["raw"])).is_dir()
    with pytest.raises(ValueError, match="already exists"):
        server.new_batch("Autumn knits")

    data = (batch.raw / "IMG_1001.jpg").read_bytes()
    assert server.add_photo("Autumn knits", "IMG_9001.jpg", data) == {"name": "IMG_9001.jpg", "written": True,
                                                                     "bytes": len(data)}
    again = server.add_photo("Autumn knits", "IMG_9001.jpg", data)
    assert again["written"] is False                                   # never overwrite by accident
    assert server.add_photo("Autumn knits", "IMG_9001.jpg", data, replace=True)["written"] is True
    server.add_photo("Autumn knits", "../escape.jpg", data)            # a path is stripped to its file name
    assert not (Path(made["raw"]).parent.parent / "escape.jpg").exists()
    with pytest.raises(ValueError):
        server.add_photo("Autumn knits", "../escape.txt", data)         # and must still be a photo
    assert sorted(p.name for p in Path(made["raw"]).iterdir()) == ["IMG_9001.jpg", "escape.jpg"]

    # importing a folder copies the photos and leaves the originals alone
    src = tmp_path / "camera"
    src.mkdir()
    (src / "notes.txt").write_text("ignored", encoding="utf-8")
    for name in ("IMG_9001.jpg", "IMG_9002.jpg"):
        (src / name).write_bytes(data)
    out = server.import_folder("Autumn knits", str(src))
    assert (out["copied"], out["skipped"]) == (1, 1)                   # 9001 was already there
    assert sorted(p.name for p in Path(made["raw"]).iterdir()) == ["IMG_9001.jpg", "IMG_9002.jpg", "escape.jpg"]
    assert sorted(p.name for p in src.iterdir()) == ["IMG_9001.jpg", "IMG_9002.jpg", "notes.txt"]
    with pytest.raises(FileNotFoundError):
        server.import_folder("Autumn knits", str(tmp_path / "nope"))

    assert server.batch_cover("Autumn knits")[:2] == bytes([0xFF, 0xD8])   # a JPEG for the batches page
    assert [b["name"] for b in server.batches()["batches"]][0] == "Autumn knits"   # most recent first


def test_rename_batch_moves_the_folder_and_the_db_rows(batch):
    from depop_seller.review import ReviewServer

    photos = scan_batch(batch)
    write_manifest(batch.manifest, decisions_to_rows(photos, group_local(batch, photos)))
    server = ReviewServer()
    server.sell_update("20990101", 1, {"description": "keep me", "status": "listed"})

    assert server.rename_batch("20990101", "Autumn knits") == {"name": "Autumn knits"}
    assert not batch.root.exists()
    state = server.sell_state("Autumn knits")
    assert state["items"][0]["description"] == "keep me" and state["items"][0]["status"] == "listed"
    with pytest.raises(ValueError):
        server.rename_batch("Autumn knits", "a/b")
    server.new_batch("Other")
    with pytest.raises(ValueError, match="already exists"):
        server.rename_batch("Autumn knits", "Other")


def test_only_depop_gets_cors_and_other_sites_cannot_post(batch):
    """The extension fetches photos from depop.com; no other page may read from or drive the server."""
    import threading
    import urllib.error
    import urllib.request

    from depop_seller.review import ReviewServer, _Server, ReviewHandler
    from functools import partial

    photos = scan_batch(batch)
    write_manifest(batch.manifest, decisions_to_rows(photos, group_local(batch, photos)))
    srv = _Server(("127.0.0.1", 8771), partial(ReviewHandler, ReviewServer()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path, origin=None):
            req = urllib.request.Request(f"http://127.0.0.1:8771{path}")
            if origin:
                req.add_header("Origin", origin)
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get("Access-Control-Allow-Origin")

        assert get("/sell/photos?batch=20990101&item=1", "https://www.depop.com")[1] == "https://www.depop.com"
        assert get("/sell/photos?batch=20990101&item=1", "https://evil.example")[1] is None
        assert get("/batches", "https://www.depop.com")[1] is None      # depop has no business here
        assert get("/batches")[1] is None

        def post(path, origin):
            req = urllib.request.Request(f"http://127.0.0.1:8771{path}", data=b"{}", method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Origin", origin)
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        assert post("/batches/new", "https://evil.example") == 403
        assert post("/batches/new", "https://www.depop.com") == 403     # not one of the extension's routes
        assert post("/batches/new", "http://127.0.0.1:8771") == 400     # our own page: reaches the handler
    finally:
        srv.shutdown()
        srv.server_close()


STYLE_SAMPLE = chr(10).join([
    "# Description style", "", "## The template", "", "yourshop [era] [brand] [item] in [colour].",
    "Brand: ...", "Size: ...", "Material: ...", "Condition: ...", "Measurements: ...", "",
    "## Hashtags", "", "**Y2K glam** `#y2k #mcbling`", "", "## Examples", "", "yourshop Vintage Y2K top in red.",
]) + chr(10) * 2 + "Filler so the file is long enough to be a real style file. " * 12


def _style_in_tmp(tmp_path: Path, monkeypatch, text: str = STYLE_SAMPLE):
    from depop_seller import style

    f = tmp_path / "description_style.md"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setattr(style, "STYLE_FILE", f)
    monkeypatch.setattr(style, "HISTORY_DIR", tmp_path / "style_history")
    return style, f


def test_style_save_keeps_every_previous_version(tmp_path: Path, monkeypatch):
    style, f = _style_in_tmp(tmp_path, monkeypatch)

    assert style.history() == []
    first = style.save_style(STYLE_SAMPLE + chr(10) + "## Extra")
    assert first["backup"] is None or (tmp_path / "style_history" / first["backup"]).is_file()
    assert "## Extra" in style.read_style()

    style.save_style(STYLE_SAMPLE + chr(10) + "## Second")     # same second: must not overwrite the first
    hist = style.history()
    assert len(hist) == 2 and hist[0]["when"] >= hist[1]["when"]          # newest first
    assert len({h["name"] for h in hist}) == 2

    # restoring brings the old text back and keeps the current one as a version too
    style.restore(hist[-1]["name"])
    assert "## Extra" not in style.read_style() and "## Second" not in style.read_style()
    assert len(style.history()) == 3
    with pytest.raises(FileNotFoundError):
        style.restore("description_style-nope.md")


def test_style_refuses_to_destroy_itself(tmp_path: Path, monkeypatch):
    style, f = _style_in_tmp(tmp_path, monkeypatch)

    for bad in ("", "   ", "too short to be a style"):
        with pytest.raises(ValueError):
            style.save_style(bad)
    with pytest.raises(ValueError, match="headings"):
        style.save_style("no headings here, just prose. " * 30)
    assert style.read_style() == STYLE_SAMPLE                              # untouched by the failures


def test_style_instruction_is_applied_by_claude_code_and_shown_as_a_diff(tmp_path: Path, monkeypatch):
    """The Style tab: an instruction goes to Claude Code (subscription only) and comes back as a
    proposal with a diff - the file itself is not touched until the seller accepts."""
    from depop_seller import style

    style_mod, f = _style_in_tmp(tmp_path, monkeypatch)
    proposed = STYLE_SAMPLE.replace("**Y2K glam** `#y2k #mcbling`",
                                    "**Y2K glam** `#y2k #mcbling`" + chr(10) * 2 + "**Surf** `#surfergirl`")
    stub = _stub_claude_cli(tmp_path, {"markdown": proposed, "summary": "Added a surf group."})
    monkeypatch.setenv("STUB_BILLED_MARK", str(tmp_path / "billed.txt"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")                 # must not reach the CLI

    out = style_mod.propose("From now on add a surf group", cli=stub)
    assert out["summary"] == "Added a surf group." and out["credential"] == "none"
    assert "#surfergirl" in out["markdown"]
    assert not (tmp_path / "billed.txt").exists()
    added = [l for l in out["diff"] if l.startswith("+") and not l.startswith("+++")]
    assert any("#surfergirl" in l for l in added)
    assert style_mod.read_style() == STYLE_SAMPLE                          # nothing written yet

    style_mod.save_style(out["markdown"])
    assert "#surfergirl" in style_mod.read_style()

    with pytest.raises(ValueError, match="type what should change"):
        style_mod.propose("   ", cli=stub)


def _settings_in_tmp(tmp_path: Path, monkeypatch):
    """Point the settings module at a throwaway .env - never the real one in the data folder."""
    from depop_seller import config, settings

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")      # load_api_key reads this one
    monkeypatch.setattr(settings, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return settings


def test_api_key_is_saved_to_env_and_never_handed_back(tmp_path: Path, monkeypatch):
    settings = _settings_in_tmp(tmp_path, monkeypatch)
    env = tmp_path / ".env"
    env.write_text("OTHER_SETTING=keep me" + chr(10) + "ANTHROPIC_API_KEY=sk-ant-old000000000000000000000000" + chr(10),
                   encoding="utf-8")

    key = "sk-ant-test-" + "x" * 40 + "ABCD"          # obviously fake: real keys are api03-...
    out = settings.save_api_key(f"  {key}  ")                       # pasted with stray spaces
    assert key not in str(out) and out["set"] is True
    assert out["hint"] == "sk-ant-...ABCD" and out["source"] == ".env file"

    body = env.read_text(encoding="utf-8").splitlines()
    assert "OTHER_SETTING=keep me" in body                          # other settings survive
    assert body.count(f"ANTHROPIC_API_KEY={key}") == 1 and len(body) == 2   # replaced, not appended

    for bad in ("", "   ", "hello", "sk-ant-short", "sk-ant-with space" + "x" * 30):
        with pytest.raises(ValueError):
            settings.save_api_key(bad)
    assert f"ANTHROPIC_API_KEY={key}" in env.read_text(encoding="utf-8")    # unchanged by the failures

    assert settings.mask(key) == "sk-ant-...ABCD" and settings.mask("") == "set"
    assert key not in json.dumps(settings.state())                  # nothing on the page can see it


def test_api_key_status_notices_an_environment_variable_wins(tmp_path: Path, monkeypatch):
    settings = _settings_in_tmp(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-fromfile" + "y" * 30, encoding="utf-8")
    assert settings.api_key_status()["editable"] is True

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fromenv" + "z" * 30)
    status = settings.api_key_status()
    assert status["editable"] is False and "environment" in status["source"]


def test_api_key_test_reports_a_rejected_key_in_plain_words(tmp_path: Path, monkeypatch):
    import anthropic

    settings = _settings_in_tmp(tmp_path, monkeypatch)
    assert settings.test_api_key() == {"ok": False, "detail": "No key is set yet."}

    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-test" + "q" * 30, encoding="utf-8")

    class Rejecting:
        def __init__(self, **kw): self.models = self
        def list(self, **kw):
            raise anthropic.AuthenticationError("bad key", response=httpx_response(401), body=None)

    def httpx_response(status):
        import httpx2
        return httpx2.Response(status, request=httpx2.Request("GET", "https://api.anthropic.com/v1/models"))

    monkeypatch.setattr(anthropic, "Anthropic", Rejecting)
    out = settings.test_api_key()
    assert out["ok"] is False and "rejected" in out["detail"]


def test_claude_sign_in_window_never_inherits_the_api_key(tmp_path: Path, monkeypatch):
    """The sign-in terminal must use the subscription, so it must not see a key either."""
    from depop_seller import settings

    launched = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-passed" + "w" * 20)
    monkeypatch.setattr(settings, "find_claude_cli", lambda: tmp_path / "claude.exe")
    monkeypatch.setattr(settings.subprocess, "Popen", lambda cmd, **kw: launched.update(cmd=cmd, env=kw.get("env")))
    settings.open_login_terminal()
    assert "claude" in " ".join(launched["cmd"]).lower()
    assert not any(k.upper().startswith("ANTHROPIC_") for k in launched["env"])


def test_claude_sign_in_window_quotes_the_mac_path(tmp_path: Path, monkeypatch):
    """On macOS the CLI lives under "Application Support", so the command Terminal runs must be
    quoted - unquoted it fails with "command not found" in a window the user cannot debug."""
    from depop_seller import settings

    cli = tmp_path / "Application Support" / "Claude" / "claude-code" / "2.1.275" / "claude"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    launched = {}
    monkeypatch.setattr(settings.sys, "platform", "darwin")
    monkeypatch.setattr(settings, "find_claude_cli", lambda: cli)
    monkeypatch.setattr(settings.subprocess, "Popen", lambda cmd, **kw: launched.update(cmd=cmd, env=kw.get("env")))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-nope" + "v" * 25)

    settings.open_login_terminal()
    cmd = launched["cmd"]
    assert cmd[0] == "osascript" and cmd.count("-e") == 2
    do_script = cmd[2]
    assert do_script.startswith('tell application "Terminal" to do script ')
    assert f"'{cli}'" in do_script                      # the whole path, in single quotes
    assert "activate" in cmd[4]
    assert not any(k.upper().startswith("ANTHROPIC_") for k in launched["env"])


def test_share_copy_carries_the_app_but_never_the_secrets(tmp_path: Path, monkeypatch):
    """The zip that goes to another person must have the code and the launchers in it, and must
    not have the API key, the photos, the listings database or the virtual environment."""
    import zipfile

    from depop_seller import share

    project = tmp_path / "depop_seller"
    (project / "depop_seller" / "static").mkdir(parents=True)
    (project / "chrome_extension").mkdir()
    (project / ".venv" / "Scripts").mkdir(parents=True)
    (project / "product_image" / "20260105" / "raw_image").mkdir(parents=True)
    (project / "style_history").mkdir()
    (project / "__pycache__").mkdir()
    (project / "Depop Seller.app" / "Contents" / "MacOS").mkdir(parents=True)
    written = {
        "Depop Seller.app/Contents/MacOS/depop-seller": "#!/bin/bash",
        "depop_seller/review.py": "code",
        "depop_seller/static/hub.html": "page",
        "chrome_extension/manifest.json": "{}",
        "description_style.md": "# the owner's own style",
        "description_style.example.md": "# the template",
        "setup.sh": "#!/bin/bash",
        "README.md": "docs",
        ".env": "ANTHROPIC_API_KEY=sk-ant-secret",
        ".env.backup": "ANTHROPIC_API_KEY=sk-ant-secret",
        "depop_seller.db": "sqlite",
        "app.log": "noise",
        ".venv/Scripts/python.exe": "binary",
        "product_image/20260105/raw_image/IMG_1.HEIC": "photo",
        "product_image/20260105/manifest.csv": "rows",
        "style_history/description_style-20260101-000000.md": "old style",
        "__pycache__/review.cpython-314.pyc": "bytecode",
    }
    for name, body in written.items():
        (project / name).write_text(body, encoding="utf-8")

    monkeypatch.setattr(share, "PROJECT_ROOT", project)
    dest = tmp_path / "share.zip"
    out = share.make_share_zip(dest)

    inside = {n.split("depop_seller_app/", 1)[-1] for n in zipfile.ZipFile(dest).namelist()}
    assert {"depop_seller/review.py", "depop_seller/static/hub.html", "chrome_extension/manifest.json",
            "description_style.example.md", "setup.sh", "README.md", "START HERE.txt"} <= inside
    for secret in (".env", ".env.backup", "depop_seller.db", "app.log", ".venv/Scripts/python.exe", "description_style.md",
                   "product_image/20260105/raw_image/IMG_1.HEIC", "product_image/20260105/manifest.csv",
                   "style_history/description_style-20260101-000000.md", "__pycache__/review.cpython-314.pyc"):
        assert secret not in inside, secret
    assert "sk-ant-secret" not in zipfile.ZipFile(dest).read("depop_seller_app/README.md").decode()
    assert out["files"] == len(inside) and out["kb"] >= 0

    # Windows has no executable bit; without fixing it the macOS app cannot launch at all
    z = zipfile.ZipFile(dest)
    for name in z.namelist():
        mode = z.getinfo(name).external_attr >> 16
        should_run = name.endswith(".sh") or "Contents/MacOS/" in name
        assert bool(mode & 0o111) == should_run, (name, oct(mode))
        assert z.getinfo(name).create_system == 3, name          # Unix, or the modes are ignored

    # the guide must describe the double-click install, not a terminal one
    guide = zipfile.ZipFile(dest).read("depop_seller_app/START HERE.txt").decode()
    assert "Depop Seller.cmd" in guide and "Settings tab" in guide and "Open Anyway" in guide
    assert "Check for updates" in guide and "leaves it running" not in guide
    assert "bash setup.sh" not in guide and "Run with PowerShell" not in guide

    # a file that looks like a secret but slipped past the folder rules stops the whole thing
    monkeypatch.setattr(share, "files_to_share", lambda: [project / "secret-key.txt"])
    (project / "secret-key.txt").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="refusing to share"):
        share.make_share_zip(tmp_path / "again.zip")


def test_every_page_script_has_no_string_broken_across_lines():
    """A quoted JS string cannot span lines: one real newline inside one kills the whole <script>,
    and the page then sits there with everything still saying "checking...". Easy to introduce by
    writing files through a tool that turns backslash-n into a newline, and invisible in review."""
    import re
    from importlib.resources import files as pkg_files

    quote, backtick = chr(39), chr(96)
    for page in ("hub", "review", "sell", "style", "settings"):
        html = (pkg_files("depop_seller") / "static" / f"{page}.html").read_text(encoding="utf-8")
        script = html.split("<script>")[-1].split("</script>")[0]
        for number, line in enumerate(script.splitlines(), 1):
            if line.strip().startswith("//"):
                continue                                  # a comment may hold an apostrophe
            escaped = chr(92) + chr(92) + "."          # a regex matching backslash-anything
            probe = re.sub(escaped, "", line)          # an escaped character is not a quote
            probe = re.sub(backtick + "[^" + backtick + "]*" + backtick, "", probe)   # template literals may span lines
            assert probe.count(quote) % 2 == 0, f"{page}.html line {number}: {line.strip()[:80]}"
            assert probe.count(chr(34)) % 2 == 0, f"{page}.html line {number}: {line.strip()[:80]}"


def test_closing_the_window_stops_the_app_unless_something_is_running(monkeypatch):
    """Closing the app window must close the app - but moving between tabs also closes a page, and
    work already under way has to outlive it."""
    import depop_seller.review as review

    monkeypatch.setattr(review, "CLOSE_GRACE_S", 0.05)
    server = review.ReviewServer()

    class FakeHttp:
        stopped = False

        def shutdown(self):
            self.stopped = True

    def close_and_wait():
        http = FakeHttp()
        server.arm_close(http)
        time.sleep(0.4)
        return http.stopped

    assert server.busy() is None
    assert close_and_wait() is True                        # nothing going on: stop

    http = FakeHttp()                                      # a tab switch: the next page arrives
    server.arm_close(http)
    server.note_request()
    time.sleep(0.4)
    assert http.stopped is False

    for label, setup, expected in (
        ("grouping", lambda: server.group_state.update(b={"running": True}), "grouping photos"),
        ("drafting", lambda: server.describe_state.update(b={1: {"running": True}}), "writing a description"),
        ("style", lambda: server.style_job.update(running=True), "rewriting the style"),
        ("depop handoff", lambda: setattr(server, "handoff_at", time.monotonic()), "sending photos to Depop"),
    ):
        server.group_state.clear()
        server.describe_state.clear()
        server.style_job["running"] = False
        server.handoff_at = 0.0
        setup()
        assert server.busy() == expected, label
        assert close_and_wait() is False, label            # stays up while it works

    server.style_job["running"] = False
    server.handoff_at = 0.0
    server.group_state.clear()
    server.describe_state.clear()
    assert close_and_wait() is True                        # and stops once the work is done


def test_thumbnails_rebuild_when_a_batch_arrives_without_them(batch):
    """thumbs/ is a cache and is not copied by git, so a batch folder moved from another computer
    can turn up without it. The page must rebuild rather than show broken tiles."""
    from depop_seller.review import ReviewSession

    photos = scan_batch(batch)
    rows = decisions_to_rows(photos, group_local(batch, photos))
    write_manifest(batch.manifest, rows)
    session = ReviewSession(batch, rows)

    stem = rows[0].stem
    made = thumb_path(batch, photos[0])
    assert made.is_file()
    for f in batch.thumbs.iterdir():                     # arrive with the cache stripped
        f.unlink()
    assert not made.is_file()

    rebuilt = session.thumb_image(stem)
    assert rebuilt is not None and rebuilt.is_file() and rebuilt.read_bytes()[:2] == bytes([0xFF, 0xD8])
    assert session.thumb_image(stem) == rebuilt          # second time it is just served
    assert session.thumb_image("IMG_does_not_exist") is None


def test_data_folder_is_found_in_order_and_is_the_only_thing_to_carry(tmp_path: Path, monkeypatch):
    """Everything personal sits in one folder outside the app, so moving computers is one copy."""
    from depop_seller import config

    monkeypatch.delenv("DEPOP_SELLER_DATA", raising=False)
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config, "machine_config_file", lambda: tmp_path / "nowhere" / "config.json")
    assert config.resolve_data_dir() == tmp_path / "DepopSeller"          # the default

    chosen = tmp_path / "elsewhere" / "MyStuff"
    machine = tmp_path / "machine" / "config.json"
    monkeypatch.setattr(config, "machine_config_file", lambda: machine)
    config.set_data_dir(chosen)
    assert config.resolve_data_dir() == chosen                            # remembered per machine

    monkeypatch.setenv("DEPOP_SELLER_DATA", str(tmp_path / "env-wins"))
    assert config.resolve_data_dir() == tmp_path / "env-wins"             # the env var still wins


def test_an_older_install_has_its_data_moved_out_of_the_app_folder(tmp_path: Path, monkeypatch):
    """Upgrading must not strand the seller's work in the app folder - it is moved once, and an
    existing file in the data folder is never overwritten."""
    from depop_seller import config

    app, data = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    (app / "product_image" / "20260105" / "raw_image").mkdir(parents=True)
    (app / "product_image" / "20260105" / "manifest.csv").write_text("rows", encoding="utf-8")
    (app / "depop_seller.db").write_text("sqlite", encoding="utf-8")
    (app / "description_style.md").write_text("# mine", encoding="utf-8")
    (app / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-x", encoding="utf-8")

    monkeypatch.setattr(config, "PROJECT_ROOT", app)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "PRODUCT_IMAGE_DIR", data / "product_image")
    monkeypatch.setattr(config, "DB_PATH", data / "depop_seller.db")
    monkeypatch.setattr(config, "STYLE_FILE", data / "description_style.md")
    monkeypatch.setattr(config, "STYLE_HISTORY", data / "style_history")
    monkeypatch.setattr(config, "ENV_FILE", data / ".env")

    moved = config.migrate_into_data_dir()
    assert set(moved) == {"product_image", "depop_seller.db", "description_style.md", ".env"}
    assert (data / "product_image" / "20260105" / "manifest.csv").read_text(encoding="utf-8") == "rows"
    assert (data / "description_style.md").read_text(encoding="utf-8") == "# mine"
    assert not (app / "product_image").exists() and not (app / ".env").exists()

    assert config.migrate_into_data_dir() == []                           # nothing left to move

    (app / "description_style.md").write_text("# a stale leftover", encoding="utf-8")
    assert config.migrate_into_data_dir() == []                           # never clobbers the real one
    assert (data / "description_style.md").read_text(encoding="utf-8") == "# mine"


def test_the_app_puts_its_own_icon_back_when_it_is_missing(tmp_path: Path, monkeypatch):
    """Setup makes the Desktop icon, but setup is skipped when a .venv already exists and its last
    step can fail - leaving no way into the app. Starting it must repair that, and must never fail
    because of it."""
    from depop_seller import desktop

    project, fake_desktop = tmp_path / "app", tmp_path / "Desktop"
    project.mkdir()
    fake_desktop.mkdir()
    maker = project / "make_desktop_icon.ps1"
    link = fake_desktop / "Depop Seller.lnk"
    monkeypatch.setattr(desktop, "windows_desktop_dir", lambda: fake_desktop)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        link.write_text("shortcut", encoding="utf-8")     # what the real script would do

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)

    assert desktop.ensure_desktop_shortcut(project) is None   # no maker script: nothing to do
    assert calls == []

    maker.write_text("# makes the icon", encoding="utf-8")
    assert desktop.ensure_desktop_shortcut(project) == link
    assert len(calls) == 1 and str(maker) in calls[0]

    assert desktop.ensure_desktop_shortcut(project) == link   # already there: left alone
    assert len(calls) == 1

    link.unlink()
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no powershell")))
    assert desktop.ensure_desktop_shortcut(project) is None   # a failure here is never fatal

    monkeypatch.setattr(desktop, "windows_desktop_dir", lambda: None)
    assert desktop.ensure_desktop_shortcut(project) is None   # not Windows, or no Desktop folder


def test_background_programs_start_without_a_console_window(tmp_path: Path, monkeypatch):
    """The app runs windowless, and Windows gives every console program it starts a window of its
    own - the black screen that appeared on Generate Description. Claude Code, taskkill and
    PowerShell must all be started with CREATE_NO_WINDOW."""
    from depop_seller import describe, desktop

    monkeypatch.setattr(desktop.os, "name", "nt")
    assert desktop.no_console() == {"creationflags": 0x08000000}

    seen = {}

    class Stop(Exception):
        pass

    def fake_popen(cmd, **kw):
        seen.update(kw)
        raise Stop

    monkeypatch.setattr(describe.subprocess, "Popen", fake_popen)
    with pytest.raises(Stop):
        describe.run_claude_code("Reply with exactly: ok", cli=tmp_path / "claude.exe")
    assert seen.get("creationflags") == 0x08000000
    assert "start_new_session" not in seen                  # that one is for macOS / Linux

    monkeypatch.setattr(desktop.os, "name", "posix")
    assert desktop.no_console() == {}


def test_every_page_uses_the_shared_design(batch):
    """One stylesheet carries the look; a page that redefines the palette or skips the link would
    drift back to its own colours."""
    import threading
    import urllib.request
    from functools import partial
    from importlib.resources import files as pkg_files

    from depop_seller.review import ReviewHandler, ReviewServer, _Server

    for page in ("hub", "review", "sell", "style", "settings"):
        html = (pkg_files("depop_seller") / "static" / f"{page}.html").read_text(encoding="utf-8")
        assert html.count('<link rel="stylesheet" href="/app.css">') == 1, page
        own = html.split("<style>")[1].split("</style>")[0]
        assert ":root" not in own and "#2563eb" not in own, f"{page}.html redefines the palette"

    srv = _Server(("127.0.0.1", 8781), partial(ReviewHandler, ReviewServer()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8781/app.css") as r:
            css = r.read().decode("utf-8")
            assert r.headers["Content-Type"].startswith("text/css")
        assert "--primary:" in css and "--blue: var(--primary)" in css      # old page rules follow the palette
    finally:
        srv.shutdown()
        srv.server_close()


def test_sell_page_generates_descriptions_for_the_ticked_items_only():
    """The wording the sellers asked for, and a bulk action driven by ticks - not "Draft all"."""
    from importlib.resources import files as pkg_files

    html = (pkg_files("depop_seller") / "static" / "sell.html").read_text(encoding="utf-8")
    assert "Generate Selected Descriptions" in html and "'Generate Description'" in html
    assert 'id="btn-gen-selected" class="primary"' in html                 # blue, like the per-item button
    assert "btn-draft-all" not in html and "'Draft'" not in html
    assert "SELECTED.has(" in html and "label.pick" in html
    # bulk runs take minutes: the page must redraw changed cards only, never the one being typed in
    assert "node.contains(document.activeElement)" in html
