"""
normalize_petit_images.py   ***TEMPORARY / EXPLORATORY***

The petit/chibi PNGs come from two sources with different framing:
  * umamusu.wiki  -> 256x256, character centred in a lot of transparent padding
  * Fandom        -> already roughly alpha-trimmed, non-square, ~150-205 x 234-256

This normalises every downloaded PNG by cropping it to its alpha bounding box
(fully-transparent border removed), so each image is content-tight. Optionally
pads back out to a square canvas.

Reads the file list from data/trainees.db (both `petit_images` and
`petit_images_fallback`), writes trimmed copies to data/petit_images_normalized/,
and records `normalized_path` / `norm_width` / `norm_height` back on each row.

Usage
-----
    python normalize_petit_images.py
    python normalize_petit_images.py --pad 4          # keep a 4px transparent margin
    python normalize_petit_images.py --square         # pad trimmed result to a square
    python normalize_petit_images.py --square --pad 8
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from PIL import Image

from global_config import TRAINEES_DB, PETIT_IMAGE_NORMALIZED_DIR

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / TRAINEES_DB
OUT_DIR = REPO_ROOT / PETIT_IMAGE_NORMALIZED_DIR
OUT_DIR_REL = PETIT_IMAGE_NORMALIZED_DIR  # stored in DB, repo-root-relative

TABLES = [
    ("petit_images", "filename"),
    ("petit_images_fallback", "fandom_filename"),
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, _ in TABLES:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in (
            ("normalized_path", "TEXT"),
            ("norm_width", "INTEGER"),
            ("norm_height", "INTEGER"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def _trim(img: Image.Image, pad: int, square: bool) -> Image.Image:
    img = img.convert("RGBA")
    bbox = img.getchannel("A").getbbox()  # bounds of non-transparent pixels
    if bbox:
        img = img.crop(bbox)

    if square:
        side = max(img.width, img.height) + 2 * pad
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
        return canvas

    if pad:
        canvas = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
        canvas.paste(img, (pad, pad))
        return canvas

    return img


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pad", type=int, default=0, help="transparent margin to keep/add (px)")
    ap.add_argument("--square", action="store_true", help="pad trimmed image to a square canvas")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"!! {DB_PATH} not found")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    _ensure_columns(conn)

    done = 0
    missing = 0
    dims: list[tuple[int, int]] = []

    for table, name_col in TABLES:
        rows = conn.execute(
            f"SELECT rowid, {name_col}, local_path FROM {table} WHERE local_path IS NOT NULL"
        ).fetchall()
        for rowid, fname, local_path in rows:
            src = REPO_ROOT / local_path
            if not src.exists():
                print(f"  ! missing file: {local_path}")
                missing += 1
                continue

            with Image.open(src) as im:
                before = (im.width, im.height)
                out = _trim(im, args.pad, args.square)

            dest = OUT_DIR / src.name
            out.save(dest)
            rel = f"{OUT_DIR_REL}/{src.name}"
            conn.execute(
                f"UPDATE {table} SET normalized_path=?, norm_width=?, norm_height=? WHERE rowid=?",
                (rel, out.width, out.height, rowid),
            )
            dims.append((out.width, out.height))
            done += 1
            print(f"  {src.name:<48} {before[0]}x{before[1]}  ->  {out.width}x{out.height}")

    conn.commit()
    conn.close()

    print("\n" + "=" * 60)
    print(f"[normalize] wrote {done} image(s) to {OUT_DIR.name}/  ({missing} missing)")
    if dims:
        ws = sorted({w for w, _ in dims})
        hs = sorted({h for _, h in dims})
        print(f"[normalize] width range {ws[0]}-{ws[-1]}, height range {hs[0]}-{hs[-1]}")
        if args.square:
            print(f"[normalize] square sides: {sorted({w for w, _ in dims})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
