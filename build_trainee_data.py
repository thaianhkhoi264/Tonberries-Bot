"""
build_trainee_data.py   ***TEMPORARY / EXPLORATORY***

One-shot orchestrator for the trainee/petit data pipeline. Runs, in order:

    1. scrape_trainees.py                 -> data/trainees.db + data/petit_images/
    2. scrape_trainees_fandom_fallback.py -> fills gaps from Fandom
    3. normalize_petit_images.py          -> data/petit_images_normalized/

Driven by the `trainee refresh` bot-DM command (main owner only), but also
runnable directly.

Lines beginning with "== " are stage banners the bot relays to Discord.

Usage
-----
    python build_trainee_data.py
    python build_trainee_data.py --no-images     # skip PNG downloads in stages 1-2
    python build_trainee_data.py --square        # pass-through to the normalizer
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from global_config import TRAINEES_DB

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / TRAINEES_DB

STAGES = [
    ("Stage 1/3 — scraping umamusu.wiki", "scrape_trainees.py", ("images",)),
    ("Stage 2/3 — filling gaps from Fandom", "scrape_trainees_fandom_fallback.py", ("images",)),
    ("Stage 3/3 — normalizing images", "normalize_petit_images.py", ("square",)),
]


def _banner(msg: str) -> None:
    print(f"== {msg}", flush=True)


def _run(script: str, extra: list[str]) -> int:
    cmd = [sys.executable, "-u", str(REPO_ROOT / script), *extra]
    print(f"$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=REPO_ROOT)
    return proc.returncode


def _summary() -> str:
    if not DB_PATH.exists():
        return "trainees.db not found"
    db = sqlite3.connect(DB_PATH)
    try:
        chars = db.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
        umamusu = db.execute("SELECT COUNT(*) FROM petit_images").fetchone()[0]
        try:
            fandom = db.execute("SELECT COUNT(*) FROM petit_images_fallback").fetchone()[0]
            filled = db.execute(
                "SELECT COUNT(DISTINCT slug) FROM petit_images_fallback"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            fandom = filled = 0
        with_umamusu = db.execute(
            "SELECT COUNT(DISTINCT slug) FROM petit_images"
        ).fetchone()[0]
        try:
            norm_u = db.execute(
                "SELECT COUNT(*) FROM petit_images WHERE normalized_path IS NOT NULL"
            ).fetchone()[0]
            norm_f = db.execute(
                "SELECT COUNT(*) FROM petit_images_fallback WHERE normalized_path IS NOT NULL"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            norm_u = norm_f = 0
        still_empty = chars - with_umamusu - filled
        return (
            f"{chars} characters · {umamusu} umamusu petit + {fandom} fandom petit "
            f"({norm_u + norm_f} normalized) · {filled} gap(s) filled from Fandom · "
            f"{still_empty} still without images"
        )
    finally:
        db.close()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-images", action="store_true",
                    help="skip PNG downloads in the scrape stages")
    ap.add_argument("--square", action="store_true",
                    help="normalizer pads each trimmed image to a square canvas")
    args = ap.parse_args()

    started = time.time()
    for banner, script, supported in STAGES:
        _banner(banner)
        extra: list[str] = []
        if "images" in supported and args.no_images:
            extra.append("--no-images")
        if "square" in supported and args.square:
            extra.append("--square")
        rc = _run(script, extra)
        if rc != 0:
            _banner(f"{script} failed (exit {rc}) — aborting")
            return rc

    elapsed = time.time() - started
    _banner(f"Done in {elapsed / 60:.1f} min — {_summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
