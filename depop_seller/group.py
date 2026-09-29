"""Propose item boundaries for a batch of photos.

Two engines produce the same output (one Decision per photo):
  - "claude": vision model, sliding windows of consecutive thumbnails, structured JSON.
  - "local":  offline fallback using the time gap and garment colour distance.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel

from .config import DEFAULT_MODEL, MODEL_VIEWS, BatchPaths, load_api_key
from .manifest import Row, normalize
from .scan import Photo, thumb_path

log = logging.getLogger(__name__)

ViewName = Literal["front", "back", "side", "label", "detail", "flaw", "other"]
Confidence = Literal["high", "medium", "low"]


@dataclass
class Decision:
    raw_file: str
    same_item_as_previous: bool
    view: str = "other"
    confidence: str = ""
    note: str = ""
    is_product: bool = True


def decisions_to_rows(photos: list[Photo], decisions: list[Decision]) -> list[Row]:
    """Consecutive photos with same_item_as_previous form one item; non-product photos are skipped."""
    by_file = {d.raw_file: d for d in decisions}
    rows = []
    item = 0
    for p in photos:
        d = by_file.get(p.raw_file)
        if d is None:
            raise ValueError(f"no grouping decision for {p.raw_file}")
        if not d.same_item_as_previous or item == 0:
            item += 1
        rows.append(Row(
            seq=p.seq,
            raw_file=p.raw_file,
            taken_at=p.taken_at.isoformat(timespec="seconds") if p.taken_at else "",
            gap_s=int(round(p.gap_s)),
            item_id=item,
            skip=not d.is_product,
            view=d.view if d.view in MODEL_VIEWS else "other",
            confidence=d.confidence,
            note=d.note if d.is_product else (d.note or "not a product photo"),
        ))
    return normalize(rows)


# --------------------------------------------------------------------------- local

def _mean_colour(thumb: Path) -> tuple[float, float, float]:
    """Average RGB of the central region, where the garment sits on the mannequin."""
    with Image.open(thumb) as im:
        w, h = im.size
        box = (int(w * 0.30), int(h * 0.15), int(w * 0.70), int(h * 0.80))
        px = im.convert("RGB").crop(box).resize((1, 1), Image.BOX).getpixel((0, 0))
    return tuple(float(c) for c in px)


def group_local(
    paths: BatchPaths,
    photos: list[Photo],
    *,
    gap_break: float = 240.0,
    gap_soft: float = 45.0,
    colour_break: float = 40.0,
) -> list[Decision]:
    """Boundary when the gap is long, or moderately long AND the garment colour changed."""
    out: list[Decision] = []
    prev_colour: tuple[float, float, float] | None = None
    for p in photos:
        colour = _mean_colour(thumb_path(paths, p))
        if prev_colour is None:
            out.append(Decision(p.raw_file, False, "other", "high", "first photo"))
        else:
            dist = sum((a - b) ** 2 for a, b in zip(colour, prev_colour)) ** 0.5
            if p.gap_s > gap_break:
                same, conf, note = False, "medium", f"gap {p.gap_s:.0f}s"
            elif p.gap_s > gap_soft and dist > colour_break:
                same, conf, note = False, "low", f"gap {p.gap_s:.0f}s, colour delta {dist:.0f}"
            elif dist > colour_break * 2:
                same, conf, note = False, "low", f"colour delta {dist:.0f}"
            else:
                same, conf, note = True, "medium" if dist > colour_break else "high", ""
            out.append(Decision(p.raw_file, same, "other", conf, note))
        prev_colour = colour
    return out


# -------------------------------------------------------------------------- claude

PROMPT_VERSION = "2"
WINDOW = 8       # photos decided per request
CONTEXT = 2      # trailing photos of the previous window, shown again for continuity

SYSTEM_PROMPT = """\
You are sorting product photos for a second-hand clothing seller. All photos come from one
shooting session in chronological order. Items are photographed either on a mannequin in
front of the same door and wall, or laid flat on a bed; the item can be a garment, a bag, or
another accessory. The seller photographs one item at a time, taking 1-9 shots of it
(typically: front styled with accessories, a close-up, plain front, side, back, and often the
brand/size label, a measuring tape shot, or a flaw), then moves on to the next item.
Consecutive photos of the same item therefore appear together.

For each photo you are asked about, decide:

1. is_product: false only when the photo shows no sellable item at all - an empty bed or
   backdrop, a wall, shelves, room decor, a person, a blurry accident. Everything else,
   including label close-ups and measuring shots, is true.
2. same_item_as_previous: does this photo show the SAME item as the photo listed immediately
   before it (which may be a context photo)? Judge by the item itself: colour, fabric,
   pattern, cut, hardware, and details. Different framing, zoom, angle, lighting, setting
   (mannequin vs flat), or whether styling accessories are present does NOT make it a
   different item. A close-up of fabric or a label belongs to the item photographed around
   it. The mannequin's base outfit (shorts, skirt) and recurring styling props such as a
   handbag that appears with many different tops are NOT the product - unless a photo is
   clearly of an accessory alone, in which case that accessory is the item. A non-product
   photo never starts an item: give it same_item_as_previous = false and is_product = false.
3. view: front (item seen from the front, styled or plain), back, side, label (close-up of a
   brand/size/care tag), detail (close-up of fabric, print, buttons, hardware, a measuring
   tape), flaw (close-up of damage, stain or wear), other.
4. confidence: high / medium / low for the same_item_as_previous decision. Use low whenever a
   boundary is ambiguous, e.g. two similar items in a row.
5. note: empty for high confidence; otherwise a few words on why.

The time gap since the previous photo is a hint: gaps within one item are usually under about
two minutes, and a longer gap often - not always - means the seller switched items. Use it as
a tie-breaker, never as the sole reason.

Return exactly one entry per requested photo, in the order given, using the exact file names.
"""


class PhotoDecision(BaseModel):
    file: str
    is_product: bool
    same_item_as_previous: bool
    view: ViewName
    confidence: Confidence
    note: str


class WindowResult(BaseModel):
    photos: list[PhotoDecision]


# USD per million tokens: (input, output). Cache reads/writes are folded into input.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


@dataclass
class Usage:
    calls: int = 0
    cached_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def cost(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        pin, pout = PRICES[model]
        return (self.input_tokens * pin + self.output_tokens * pout) / 1_000_000


def _image_block(thumb: Path) -> dict:
    data = base64.standard_b64encode(thumb.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _window_content(paths: BatchPaths, context: list[Photo], new: list[Photo]) -> list[dict]:
    content: list[dict] = []
    if context:
        intro = (
            f"The first {len(context)} photo(s) are context from the previous group and were already "
            f"decided; do not include them in your answer. Decide the {len(new)} photos after them: "
            + ", ".join(p.raw_file for p in new) + "."
        )
    else:
        intro = (
            "These are the first photos of the session. The first photo starts the first item "
            f"(same_item_as_previous = false). Decide all {len(new)} photos: "
            + ", ".join(p.raw_file for p in new) + "."
        )
    content.append({"type": "text", "text": intro})
    for k, p in enumerate(context + new, 1):
        tag = " [context]" if k <= len(context) else ""
        gap = f"taken {p.gap_s:.0f}s after the previous photo" if p.seq > 1 else "first photo of the session"
        content.append({"type": "text", "text": f"Photo {k}{tag}: {p.raw_file} - {gap}"})
        content.append(_image_block(thumb_path(paths, p)))
    return content


def _cache_key(model: str, effort: str, files: list[str]) -> str:
    h = hashlib.sha1(f"{PROMPT_VERSION}|{model}|{effort}|{'|'.join(files)}".encode()).hexdigest()
    return h[:16]


def group_claude(
    paths: BatchPaths,
    photos: list[Photo],
    *,
    model: str = DEFAULT_MODEL,
    effort: str = "medium",
    usage: Usage | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[Decision]:
    """Ask the model, window by window, whether each photo continues the previous item."""
    import anthropic

    client = anthropic.Anthropic(api_key=load_api_key(), max_retries=5)
    usage = usage if usage is not None else Usage()
    cache_dir = paths.cache / "group"
    cache_dir.mkdir(parents=True, exist_ok=True)

    decisions: list[Decision] = []
    start = 0
    while start < len(photos):
        if progress:
            progress(start, len(photos))
        new = photos[start:start + WINDOW]
        context = photos[max(0, start - CONTEXT):start]
        key = _cache_key(model, effort, [p.raw_file for p in context + new])
        cache_file = cache_dir / f"{key}.json"

        if cache_file.is_file():
            result = WindowResult.model_validate_json(cache_file.read_text(encoding="utf-8"))
            usage.cached_calls += 1
        else:
            log.info("asking %s about photos %d-%d of %d", model, new[0].seq, new[-1].seq, len(photos))
            t0 = time.monotonic()
            response = client.messages.parse(
                model=model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _window_content(paths, context, new)}],
                output_format=WindowResult,
                output_config={"effort": effort},
            )
            usage.calls += 1
            usage.input_tokens += response.usage.input_tokens + (response.usage.cache_read_input_tokens or 0) \
                + (response.usage.cache_creation_input_tokens or 0)
            usage.output_tokens += response.usage.output_tokens
            if response.stop_reason != "end_turn" or response.parsed_output is None:
                raise RuntimeError(
                    f"unexpected response for photos {new[0].seq}-{new[-1].seq}: "
                    f"stop_reason={response.stop_reason} request_id={response._request_id}"
                )
            result = response.parsed_output
            cache_file.write_text(result.model_dump_json(indent=1), encoding="utf-8")
            log.info("  %.1fs, %d in / %d out tokens", time.monotonic() - t0,
                     response.usage.input_tokens, response.usage.output_tokens)

        decisions.extend(_align(result, new))
        start += WINDOW

    if progress:
        progress(len(photos), len(photos))
    if decisions:
        decisions[0].same_item_as_previous = False
    return decisions


# Measured on a 438-photo batch with claude-opus-5 (52 calls): $1.78, plus the merge pass.
COST_PER_PHOTO = {"claude-opus-5": 0.0055, "claude-sonnet-5": 0.0022, "claude-haiku-4-5": 0.0011}


def estimate_cost(model: str, n_photos: int) -> float | None:
    per = COST_PER_PHOTO.get(model)
    return round(per * n_photos, 2) if per is not None else None


# ------------------------------------------------------------------- merge pass

MATCH_PROMPT_VERSION = "1"
CATALOG_PX = 384            # catalog thumbnails are downsized further to keep the cached prefix small
CATALOG_CHUNK = 80          # catalog items per request (the API allows 100 images per request)
QUERIES_PER_CALL = 8        # query items per request, 2 photos each

MATCH_SYSTEM_PROMPT = """\
A second-hand clothing seller photographed many items in one session. The photos were
already grouped into items, but the seller sometimes re-shoots an item later in the session
- for example a garment first shown on a mannequin and later laid flat on a bed for label
and detail photos - and those re-shoots were grouped as separate items.

You get a CATALOG (one photo per item, numbered) and a few QUERY items (two photos each).
For every query item, decide whether some catalog item shows the SAME PHYSICAL item: the
same garment, bag or accessory, not merely the same kind or colour. Ignore differences in
setting (mannequin vs flat), angle, zoom, lighting, styling accessories, and whether a
label is visible. Compare fabric, print or pattern, cut and neckline, buttons, pockets,
hardware, and distinctive details.

Report a match with confidence "high" only when those details clearly agree, "medium"
when it is likely but a detail cannot be verified, and leave out doubtful pairs. A query
item matches at most one catalog item, and never itself (the same item number appears in
both lists). Return an empty list when nothing matches.
"""


class Match(BaseModel):
    query_item: int
    catalog_item: int
    confidence: Confidence
    note: str


class MatchResult(BaseModel):
    matches: list[Match]


def _small_image_block(thumb: Path, px: int) -> dict:
    """A JPEG downsized from the cached thumbnail, deterministic so the cached prefix stays identical."""
    import io

    with Image.open(thumb) as im:
        im = im.convert("RGB")
        im.thumbnail((px, px))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80, optimize=True)
    data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _query_photos(group: list[Row]) -> list[Row]:
    """The cover plus the photo most likely to show the whole item from another angle."""
    rest = [r for r in group[1:] if r.view in ("front", "back", "side")] or group[1:]
    return [group[0]] + rest[:1]


def find_duplicates(
    paths: BatchPaths,
    rows: list[Row],
    *,
    model: str = DEFAULT_MODEL,
    effort: str = "medium",
    usage: Usage | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[Match]:
    """Find items that are re-shoots of other items. Returns matches with query > catalog id."""
    import anthropic

    from .manifest import items

    client = anthropic.Anthropic(api_key=load_api_key(), max_retries=5)
    usage = usage if usage is not None else Usage()
    grouped = items(rows)
    ids = list(grouped)
    if len(ids) < 2:
        return []
    cache_dir = paths.cache / "match"
    cache_dir.mkdir(parents=True, exist_ok=True)

    def stem_of(item_id: int, k: int = 0) -> str:
        return grouped[item_id][k].stem

    catalog_chunks = [ids[i:i + CATALOG_CHUNK] for i in range(0, len(ids), CATALOG_CHUNK)]
    query_chunks = [ids[i:i + QUERIES_PER_CALL] for i in range(0, len(ids), QUERIES_PER_CALL)]
    total = len(catalog_chunks) * len(query_chunks)
    found: dict[int, Match] = {}
    done = 0
    for cat in catalog_chunks:
        catalog_content: list[dict] = [{"type": "text", "text":
                                        f"CATALOG: {len(cat)} items, one photo each. Item numbers are shown before each photo."}]
        for item_id in cat:
            catalog_content.append({"type": "text", "text": f"Catalog item {item_id}"})
            catalog_content.append(_small_image_block(paths.thumbs / f"{stem_of(item_id)}.jpg", CATALOG_PX))
        catalog_content[-1]["cache_control"] = {"type": "ephemeral"}  # the catalog is identical across query chunks
        for qs in query_chunks:
            key = hashlib.sha1(
                f"{MATCH_PROMPT_VERSION}|{model}|{effort}|{','.join(map(str, cat))}|{','.join(map(str, qs))}|"
                f"{'|'.join(stem_of(i) for i in cat + qs)}".encode()
            ).hexdigest()[:16]
            cache_file = cache_dir / f"{key}.json"
            if cache_file.is_file():
                result = MatchResult.model_validate_json(cache_file.read_text(encoding="utf-8"))
                usage.cached_calls += 1
            else:
                content = list(catalog_content)
                content.append({"type": "text", "text":
                                f"QUERY items: {', '.join(map(str, qs))}. Two photos each. Which catalog items "
                                f"(numbers {cat[0]}-{cat[-1]}) show the same physical item as a query item?"})
                for item_id in qs:
                    content.append({"type": "text", "text": f"Query item {item_id}"})
                    for r in _query_photos(grouped[item_id]):
                        content.append(_small_image_block(paths.thumbs / f"{r.stem}.jpg", CATALOG_PX))
                log.info("merge pass: catalog %d-%d, query items %s", cat[0], cat[-1], qs)
                response = client.messages.parse(
                    model=model,
                    max_tokens=16000,
                    system=MATCH_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": content}],
                    output_format=MatchResult,
                    output_config={"effort": effort},
                )
                usage.calls += 1
                u = response.usage
                usage.input_tokens += u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
                usage.output_tokens += u.output_tokens
                if response.stop_reason != "end_turn" or response.parsed_output is None:
                    raise RuntimeError(f"unexpected response in merge pass: stop_reason={response.stop_reason}")
                result = response.parsed_output
                cache_file.write_text(result.model_dump_json(indent=1), encoding="utf-8")
                log.info("  %d in (%d cached) / %d out tokens, %d match(es)", u.input_tokens,
                         u.cache_read_input_tokens or 0, u.output_tokens, len(result.matches))
            for m in result.matches:
                if m.query_item == m.catalog_item or m.query_item not in grouped or m.catalog_item not in grouped:
                    continue
                a, b = sorted((m.query_item, m.catalog_item))
                m = Match(query_item=b, catalog_item=a, confidence=m.confidence, note=m.note)
                prev = found.get(b)
                if prev is None or (prev.confidence != "high" and m.confidence == "high"):
                    found[b] = m
            done += 1
            if progress:
                progress(done, total)
    return sorted(found.values(), key=lambda m: (m.query_item, m.catalog_item))


def group_batch(
    paths: BatchPaths,
    photos: list[Photo],
    *,
    model: str = DEFAULT_MODEL,
    effort: str = "medium",
    usage: Usage | None = None,
    merge_pass: bool = True,
    progress: Callable[[str, int, int], None] | None = None,
) -> list[Row]:
    """The full engine: windowed grouping, then the merge pass. Writes cache/proposal*.csv and
    cache/suggestions.json (medium-confidence matches for the review page)."""
    from .manifest import items, write_manifest

    usage = usage if usage is not None else Usage()
    decisions = group_claude(paths, photos, model=model, effort=effort, usage=usage,
                             progress=(lambda d, t: progress("grouping", d, t)) if progress else None)
    rows = decisions_to_rows(photos, decisions)
    paths.cache.mkdir(parents=True, exist_ok=True)
    write_manifest(paths.cache / "proposal_windows.csv", rows)

    suggestions: list[dict] = []
    if merge_pass:
        matches = find_duplicates(paths, rows, model=model, effort=effort, usage=usage,
                                  progress=(lambda d, t: progress("matching", d, t)) if progress else None)
        grouped = items(rows)
        for m in matches:
            if m.confidence != "high":
                suggestions.append({
                    "a_stem": grouped[m.catalog_item][0].stem, "b_stem": grouped[m.query_item][0].stem,
                    "confidence": m.confidence, "note": m.note,
                })
        merged = apply_matches(rows, matches, min_confidence="high")
        log.info("merge pass: %d match(es), %d merged automatically, %d left as suggestions",
                 len(matches), merged, len(suggestions))
    (paths.cache / "suggestions.json").write_text(json.dumps(suggestions, indent=1), encoding="utf-8")
    write_manifest(paths.cache / "proposal.csv", rows)
    return rows


def apply_matches(rows: list[Row], matches: list[Match], *, min_confidence: str = "high") -> int:
    """Merge matched items into the earlier one (chains follow). Returns the number of merges."""
    order = {"high": 2, "medium": 1, "low": 0}
    parent: dict[int, int] = {}

    def root(i: int) -> int:
        while parent.get(i, i) != i:
            i = parent[i]
        return i

    merged = 0
    for m in matches:
        if order[m.confidence] < order[min_confidence]:
            continue
        a, b = root(m.catalog_item), root(m.query_item)
        if a != b:
            parent[max(a, b)] = min(a, b)
            merged += 1
    for r in rows:
        if not r.skip and root(r.item_id) != r.item_id:
            r.item_id = root(r.item_id)
            r.photo_no = 1000 + r.seq  # append after the earlier group's photos, in capture order
    normalize(rows)
    return merged


def _align(result: WindowResult, new: list[Photo]) -> list[Decision]:
    """Match the model's entries to the requested photos; fall back to positional order."""
    by_file = {d.file.strip(): d for d in result.photos}
    expected = [p.raw_file for p in new]
    if set(by_file) != set(expected) or len(result.photos) != len(new):
        log.warning("model returned %s for %s; matching by position", [d.file for d in result.photos], expected)
        entries = result.photos[:len(new)]
        while len(entries) < len(new):
            entries.append(PhotoDecision(file="", is_product=True, same_item_as_previous=True, view="other",
                                         confidence="low", note="missing from model output"))
        return [_decision(p, e) for p, e in zip(new, entries)]
    return [_decision(p, by_file[p.raw_file]) for p in new]


def _decision(p: Photo, e: PhotoDecision) -> Decision:
    return Decision(p.raw_file, e.same_item_as_previous, e.view, e.confidence, e.note, e.is_product)
