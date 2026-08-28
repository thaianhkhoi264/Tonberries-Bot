"""
scrape_trainees_fandom_fallback.py   ***TEMPORARY / EXPLORATORY***

Some characters on umamusu.wiki have no `Game_Asset_petit_chr_*_0011.png`
uploads (see `characters` rows in data/trainees.db with no `petit_images`).
This script tries to fill those gaps from the Fandom wiki instead:

    https://umamusume.fandom.com/wiki/<Name>/Image_Gallery   -> "Chibi" tab

Fandom chibi files are named  `<Name> Chibi<N>-<V>.png`  where V=2 is the
equivalent of umamusu.wiki's `_0011` variant (V=1 == `_0010`, which we skip).
`<N>` is only an ordinal on Fandom — the real in-game costume id (100xxx) is
not recoverable here, so we store the ordinal and flag `source='fandom'`.

Fandom blocks plain GETs (403) but its MediaWiki API is open:
  * action=parse  &page=<Name>/Image_Gallery  &prop=wikitext   -> gallery list
  * action=query  &titles=File:...            &prop=imageinfo  -> CDN url

Output
------
data/trainees.db  ->  new table  petit_images_fallback
data/petit_images_fandom/  ->  downloaded PNGs

Usage
-----
    python scrape_trainees_fandom_fallback.py
    python scrape_trainees_fandom_fallback.py --no-images
    python scrape_trainees_fandom_fallback.py --only Mejiro_Ryan
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

from global_config import TRAINEES_DB, PETIT_IMAGE_FANDOM_DIR

FANDOM_API = "https://umamusume.fandom.com/api.php"

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / TRAINEES_DB
IMAGE_DIR = REPO_ROOT / PETIT_IMAGE_FANDOM_DIR
IMAGE_DIR_REL = PETIT_IMAGE_FANDOM_DIR  # stored in DB, repo-root-relative

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_DELAY = 1.0
TIMEOUT = 30
MAX_RETRIES = 3

# We want the "-2" variant only (== _0011). Capture the ordinal N.
CHIBI_WANTED_RE = re.compile(r"^(.*?Chibi(\d+)-2\.png)\s*(?:\|.*)?$", re.M)

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


def _api(params: dict) -> dict:
    params = {**params, "format": "json", "formatversion": "2"}
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = _session.get(FANDOM_API, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            wait = REQUEST_DELAY * attempt * 2
            print(f"    ! api {params.get('page') or params.get('titles')} "
                  f"failed ({exc}) — retry {attempt}/{MAX_RETRIES} in {wait:.0f}s")
            time.sleep(wait)
    raise RuntimeError(f"API failed: {params}") from last_exc


def _download(url: str, dest: Path) -> int:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = _session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            time.sleep(REQUEST_DELAY)
            return len(resp.content)
        except Exception as exc:  # noqa: BLE001
            print(f"      ! download {dest.name} failed ({exc}) — retry {attempt}/{MAX_RETRIES}")
            time.sleep(REQUEST_DELAY * attempt * 2)
    raise RuntimeError(f"download failed: {url}")


# ---------------------------------------------------------------------------

def missing_slugs(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT c.slug
        FROM characters c
        LEFT JOIN petit_images p ON p.slug = c.slug
        WHERE p.slug IS NULL
        ORDER BY c.slug
        """
    ).fetchall()
    return [r[0] for r in rows]


def fandom_chibi_files(slug: str) -> tuple[str | None, list[tuple[str, int]]]:
    """Return (fandom_page_title, [(filename, ordinal), ...]) for the Chibi tab.

    Tries the slug verbatim as the Fandom page name (works for all known cases).
    """
    data = _api({"action": "parse", "page": f"{slug}/Image_Gallery", "prop": "wikitext"})
    if "error" in data:
        return None, []
    parse = data["parse"]
    wikitext: str = parse["wikitext"]
    title: str = parse["title"]

    # Isolate the "Chibi=" tabber section (up to the next gallery close).
    m = re.search(r"Chibi\s*=\s*(.*?)</gallery>", wikitext, re.S)
    if not m:
        return title, []
    section = m.group(1)

    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for fm in CHIBI_WANTED_RE.finditer(section):
        fname = fm.group(1).strip()
        ordinal = int(fm.group(2))
        if fname in seen:
            continue
        seen.add(fname)
        out.append((fname, ordinal))
    return title, out


def resolve_urls(filenames: list[str]) -> dict[str, dict]:
    """filename -> {url, width, height, size} via imageinfo (batched)."""
    result: dict[str, dict] = {}
    for i in range(0, len(filenames), 40):
        batch = filenames[i : i + 40]
        titles = "|".join(f"File:{f}" for f in batch)
        data = _api({"action": "query", "titles": titles,
                     "prop": "imageinfo", "iiprop": "url|size|sha1"})
        for page in data.get("query", {}).get("pages", []):
            info = (page.get("imageinfo") or [{}])[0]
            if not info.get("url"):
                continue
            # page title comes back as "File:Name With Spaces.png"
            fname = page["title"].split(":", 1)[1]
            result[fname] = {
                "url": info["url"],
                "width": info.get("width"),
                "height": info.get("height"),
                "size": info.get("size"),
                "sha1": info.get("sha1"),
            }
    return result


def init_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS petit_images_fallback (
            slug            TEXT NOT NULL,
            ordinal         INTEGER NOT NULL,
            source          TEXT NOT NULL DEFAULT 'fandom',
            fandom_page     TEXT,
            fandom_filename TEXT NOT NULL,
            image_url       TEXT NOT NULL,
            width           INTEGER,
            height          INTEGER,
            local_path      TEXT,
            scraped_at      INTEGER NOT NULL,
            PRIMARY KEY (slug, fandom_filename)
        );
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", type=str, default=None,
                    help="comma-separated slugs (default: all with no petit_images)")
    ap.add_argument("--no-images", action="store_true")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"!! {DB_PATH} not found — run scrape_trainees.py first")
        return 1

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_table(conn)

    if args.only:
        slugs = [s.strip() for s in args.only.split(",") if s.strip()]
    else:
        slugs = missing_slugs(conn)
    print(f"[fandom] {len(slugs)} character(s) with no petit images to try\n")

    filled: list[str] = []
    still_empty: list[str] = []
    total_imgs = 0

    for i, slug in enumerate(slugs, 1):
        print(f"[{i}/{len(slugs)}] {slug}")
        try:
            page_title, files = fandom_chibi_files(slug)
        except Exception as exc:  # noqa: BLE001
            print(f"    ! fandom lookup failed: {exc}")
            still_empty.append(slug)
            continue

        if page_title is None:
            print("    - no Fandom Image_Gallery page")
            still_empty.append(slug)
            continue
        if not files:
            print(f"    - Fandom page {page_title!r}: no Chibi '-2' files")
            still_empty.append(slug)
            continue

        urls = resolve_urls([f for f, _ in files])
        got = 0
        for fname, ordinal in files:
            meta = urls.get(fname)
            if not meta:
                print(f"    ! could not resolve URL for {fname}")
                continue
            norm = fname.replace(" ", "_")
            local_path = None
            if not args.no_images:
                dest = IMAGE_DIR / norm
                if dest.exists():
                    local_path = f"{IMAGE_DIR_REL}/{norm}"
                else:
                    n = _download(meta["url"], dest)
                    local_path = f"{IMAGE_DIR_REL}/{norm}"
                    print(f"      v {norm}  ({meta['width']}x{meta['height']}, {n} B)")
            conn.execute(
                """
                INSERT INTO petit_images_fallback
                    (slug, ordinal, source, fandom_page, fandom_filename,
                     image_url, width, height, local_path, scraped_at)
                VALUES (?, ?, 'fandom', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug, fandom_filename) DO UPDATE SET
                    ordinal=excluded.ordinal,
                    fandom_page=excluded.fandom_page,
                    image_url=excluded.image_url,
                    width=excluded.width,
                    height=excluded.height,
                    local_path=COALESCE(excluded.local_path, petit_images_fallback.local_path),
                    scraped_at=excluded.scraped_at
                """,
                (slug, ordinal, page_title, fname, meta["url"],
                 meta["width"], meta["height"], local_path, int(time.time())),
            )
            conn.commit()
            got += 1
            total_imgs += 1

        if got:
            filled.append(slug)
            print(f"    -> {got} image(s) from {page_title!r}")
        else:
            still_empty.append(slug)

    conn.close()
    print("\n" + "=" * 60)
    print(f"[fandom] filled {len(filled)}/{len(slugs)} — {total_imgs} images")
    if filled:
        print("  filled: " + ", ".join(filled))
    if still_empty:
        print(f"  still empty ({len(still_empty)}): " + ", ".join(still_empty))
    return 0


if __name__ == "__main__":
    sys.exit(main())
