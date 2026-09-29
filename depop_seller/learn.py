"""Compare the engine's proposal with the human-reviewed manifest and describe the errors.

The reviewed manifest is ground truth. Errors are described at the level a reviewer thinks
in - items split or merged, photos moved, photos dropped - so they can be turned into
prompt lessons and used as a regression test for the engine.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import Row, read_manifest


@dataclass
class Comparison:
    n_photos: int
    proposed_items: int
    truth_items: int
    # boundaries: seq numbers of photos that start an item (in capture order)
    boundaries_correct: int
    false_splits: list[int] = field(default_factory=list)      # proposed a boundary the reviewer removed
    missed_splits: list[int] = field(default_factory=list)     # reviewer added a boundary
    stitched_items: list[dict] = field(default_factory=list)   # truth items assembled from non-adjacent proposed groups
    moved_photos: list[dict] = field(default_factory=list)     # single photos moved between otherwise-intact items
    dropped: list[int] = field(default_factory=list)           # reviewer put these in Unused
    restored: list[int] = field(default_factory=list)          # engine skipped, reviewer kept
    view_changes: Counter = field(default_factory=Counter)     # (proposed view, truth view) -> count
    reordered_items: int = 0

    @property
    def boundary_precision(self) -> float:
        p = self.boundaries_correct + len(self.false_splits)
        return self.boundaries_correct / p if p else 1.0

    @property
    def boundary_recall(self) -> float:
        t = self.boundaries_correct + len(self.missed_splits)
        return self.boundaries_correct / t if t else 1.0

    def summary(self) -> str:
        lines = [
            f"{self.n_photos} photos: engine proposed {self.proposed_items} items, reviewer kept {self.truth_items}",
            f"item boundaries: precision {self.boundary_precision:.0%}, recall {self.boundary_recall:.0%} "
            f"({self.boundaries_correct} right, {len(self.false_splits)} false splits, {len(self.missed_splits)} missed splits)",
            f"items stitched from non-adjacent groups: {len(self.stitched_items)}",
            f"single photos moved: {len(self.moved_photos)}; dropped to Unused: {len(self.dropped)}; restored: {len(self.restored)}",
            f"items reordered by hand: {self.reordered_items}; view labels changed: {sum(self.view_changes.values())}",
        ]
        return "\n".join(lines)


def _first_seq_boundaries(rows: list[Row]) -> set[int]:
    """Seqs where a new item begins, walking photos in capture order (skipped photos ignored)."""
    out, prev = set(), None
    for r in sorted(rows, key=lambda r: r.seq):
        if r.skip:
            continue
        if r.item_id != prev:
            out.add(r.seq)
        prev = r.item_id
    return out


def compare(proposal: list[Row], truth: list[Row]) -> Comparison:
    p_by = {r.seq: r for r in proposal}
    t_by = {r.seq: r for r in truth}
    if set(p_by) != set(t_by):
        raise ValueError("proposal and truth cover different photos")

    p_b, t_b = _first_seq_boundaries(proposal), _first_seq_boundaries(truth)
    c = Comparison(
        n_photos=len(truth),
        proposed_items=len({r.item_id for r in proposal if not r.skip}),
        truth_items=len({r.item_id for r in truth if not r.skip}),
        boundaries_correct=len(p_b & t_b),
        dropped=sorted(s for s in t_by if t_by[s].skip and not p_by[s].skip),
        restored=sorted(s for s in t_by if not t_by[s].skip and p_by[s].skip),
    )

    # Which proposed groups feed each truth item?
    truth_items: dict[int, list[Row]] = {}
    for r in truth:
        if not r.skip:
            truth_items.setdefault(r.item_id, []).append(r)
    joined_seqs: set[int] = set()
    for item_id, rows in truth_items.items():
        rows.sort(key=lambda r: r.photo_no)
        sources = Counter(p_by[r.seq].item_id for r in rows if not p_by[r.seq].skip)
        seqs = sorted(r.seq for r in rows)
        gaps = any(b != a + 1 for a, b in zip(seqs, seqs[1:]))
        if len(sources) > 1 and gaps:
            main = min(sources)
            joined = [r.seq for r in rows if p_by[r.seq].item_id != main and not p_by[r.seq].skip]
            joined_seqs.update(joined)
            if len(joined) == 1 and sources[p_by[joined[0]].item_id] == 1 and \
                    sum(1 for r in proposal if r.item_id == p_by[joined[0]].item_id and not r.skip) > 1:
                c.moved_photos.append({"seq": joined[0], "from_item": p_by[joined[0]].item_id, "to_item": item_id})
            else:
                c.stitched_items.append({"item": item_id, "seqs": seqs, "from_proposed_items": sorted(sources)})
        if [r.seq for r in rows] != seqs:
            c.reordered_items += 1
    # Boundaries removed by merging adjacent groups; dropped photos and stitched joins are counted above.
    c.false_splits = sorted(p_b - t_b - set(c.dropped) - joined_seqs)
    c.missed_splits = sorted(t_b - p_b - joined_seqs)

    for s, t in t_by.items():
        if not t.skip and p_by[s].view != t.view:
            c.view_changes[(p_by[s].view, t.view)] += 1
    return c


def load_pair(batch_root: Path, proposal_name: str = "proposal.csv") -> tuple[list[Row], list[Row]]:
    proposal = batch_root / "cache" / proposal_name
    if not proposal.exists():
        raise FileNotFoundError(f"{proposal} not found - the engine writes it when it groups a batch")
    return read_manifest(proposal), read_manifest(batch_root / "manifest.csv")


def expected_merges(proposal: list[Row], truth: list[Row]) -> set[frozenset[int]]:
    """Pairs of proposed items the reviewer merged: {earliest proposed item, each other one}."""
    p_by = {r.seq: r for r in proposal}
    truth_items: dict[int, set[int]] = {}
    for r in truth:
        if not r.skip and not p_by[r.seq].skip:
            truth_items.setdefault(r.item_id, set()).add(p_by[r.seq].item_id)
    pairs: set[frozenset[int]] = set()
    for sources in truth_items.values():
        if len(sources) > 1:
            first = min(sources)
            pairs |= {frozenset((first, s)) for s in sources if s != first}
    return pairs


def score_merges(predicted: list, expected: set[frozenset[int]]) -> dict:
    """predicted: Match objects (query_item, catalog_item, confidence)."""
    got = {frozenset((m.query_item, m.catalog_item)): m.confidence for m in predicted}
    found = {p: got[p] for p in expected if p in got}
    return {
        "expected": sorted(tuple(sorted(p)) for p in expected),
        "found": {"/".join(map(str, sorted(p))): c for p, c in found.items()},
        "missed": sorted(tuple(sorted(p)) for p in expected - set(found)),
        "false": {"/".join(map(str, sorted(p))): c for p, c in got.items() if p not in expected},
    }
