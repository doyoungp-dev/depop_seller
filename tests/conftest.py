"""Shared fixtures: a tiny synthetic batch of JPEGs with EXIF timestamps."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from depop_seller.config import BatchPaths

# (colour, seconds after the previous shot). Colour changes mark a new "garment".
SEQUENCE = [
    ((200, 30, 30), 0), ((200, 30, 30), 10), ((200, 30, 30), 20),       # item 1: red
    ((30, 30, 200), 300), ((30, 30, 200), 15),                          # item 2: blue, after a long gap
    ((30, 30, 200), 400),                                               # still blue, long gap (tricky)
    ((30, 180, 30), 60), ((30, 180, 30), 5),                            # item 3: green
]


def _exif_time(ts: datetime) -> Image.Exif:
    exif = Image.Exif()
    exif[0x0132] = ts.strftime("%Y:%m:%d %H:%M:%S")
    exif.get_ifd(0x8769)[0x9003] = ts.strftime("%Y:%m:%d %H:%M:%S")
    return exif


def make_image(path: Path, colour: tuple[int, int, int], ts: datetime, size: int = 64) -> None:
    im = Image.new("RGB", (size, size), (240, 240, 240))
    # garment in the centre, neutral border, like the mannequin shots
    im.paste(colour, (size // 4, size // 6, 3 * size // 4, 5 * size // 6))
    im.save(path, "JPEG", exif=_exif_time(ts))


@pytest.fixture
def batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BatchPaths:
    root = tmp_path / "product_image" / "20990101"
    (root / "raw_image").mkdir(parents=True)
    ts = datetime(2099, 1, 1, 12, 0, 0)
    for i, (colour, gap) in enumerate(SEQUENCE, 1):
        ts += timedelta(seconds=gap)
        make_image(root / "raw_image" / f"IMG_{1000 + i}.jpg", colour, ts)
    monkeypatch.setattr("depop_seller.config.DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("depop_seller.config.PRODUCT_IMAGE_DIR", tmp_path / "product_image")
    monkeypatch.setattr("depop_seller.review.load_api_key", lambda: None)  # never touch the real key in tests
    return BatchPaths(batch="20990101", root=root)
