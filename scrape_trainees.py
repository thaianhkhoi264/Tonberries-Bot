"""
scrape_trainees.py  (side project — scraper stage)

Scrapes character data from https://umamusu.wiki

Pipeline
--------
1. https://umamusu.wiki/Game:List_of_Trainees
     -> the "Character" column (3rd column of the trainee wikitable).
        Many rows share a character; we keep each character once.
2. For each unique character page (e.g. /Special_Week):
     -> character name        (<h1 id="firstHeading">)
     -> first "Image Color"   (infobox row "Image Colors", first hex)
3. For each character Gallery page (e.g. /Special_Week/Gallery):
     -> every  Game_Asset_petit_chr_<group>_<costume>_0011.png
        (the "_0011" variant only — the "_0010" twin is ignored)
     -> download each PNG into  data/petit_images/

Output
------
data/trainees.db  (SQLite)
    characters     (slug, name, image_color, gallery_found, scraped_at)
    petit_images   (slug, group_id, costume_id, filename, image_url, local_path)

Notes
-----
* umamusu.wiki sits behind Cloudflare; a normal browser User-Agent is required
  (WebFetch-style bots get a 403).  robots.txt disallows /w/ (the API + the raw
  image path); article paths (/Name, /Name/Gallery) are allowed.  We only crawl
  article paths, and derive raw image URLs from the MediaWiki md5 storage layout
  so we never have to crawl /w/ pages — the image downloads themselves are the
  only /w/ hits, and those are the point of this run.

Usage
-----
    python scrape_trainees.py                 # full run (all ~132 chars)
    python scrape_trainees.py --limit 5       # first 5 characters only
    python scrape_trainees.py --only Special_Week,Gold_Ship
    python scrape_trainees.py --no-images     # skip PNG downloads
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from global_config import TRAINEES_DB, PETIT_IMAGE_DIR

BASE = "https://umamusu.wiki"
LIST_PAGE = "/Game:List_of_Trainees"

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / TRAINEES_DB
IMAGE_DIR = REPO_ROOT / PETIT_IMAGE_DIR
IMAGE_DIR_REL = PETIT_IMAGE_DIR  # stored in DB, repo-root-relative

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_DELAY = 1.0          # seconds between requests (be polite)
TIMEOUT = 30
MAX_RETRIES = 3

PETIT_RE = re.compile(r"Game_Asset_petit_chr_(\d+)_(\d+)_0011\.png")

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, *, binary: bool = False):
    """GET with a browser UA, small retry loop and a politeness delay."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = _session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp.content if binary else resp.text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            wait = REQUEST_DELAY * attempt * 2
            print(f"    ! {url} failed ({exc}) — retry {attempt}/{MAX_RETRIES} in {wait:.0f}s")
            time.sleep(wait)
    raise RuntimeError(f"GET failed after {MAX_RETRIES} tries: {url}") from last_exc


def _image_url(filename: str) -> str:
    """Reconstruct the raw MediaWiki image URL from its md5 storage layout.

    MediaWiki stores uploads at  /w/images/<h[0]>/<h[0:2]>/<filename>  where
    h = md5(filename).hexdigest()  (filename with underscores, as on the wiki).
    """
    h = hashlib.md5(filename.encode("utf-8")).hexdigest()
    return f"{BASE}/w/images/{h[0]}/{h[:2]}/{filename}"


# ---------------------------------------------------------------------------
# Scrape steps
# ---------------------------------------------------------------------------

def fetch_trainee_characters() -> list[tuple[str, str]]:
    """Return an ordered, de-duplicated list of (slug, name) from the
    "Character" column of the trainee list table."""
    html = _get(BASE + LIST_PAGE)
    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table", class_="wikitable")
    if table is None:
        raise RuntimeError("trainee list: could not find wikitable")

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        link = cells[2].find("a", href=True)
        if not link:
            continue
        href = link["href"]
        if not href.startswith("/") or ":" in href.split("/", 2)[-1]:
            # skip odd namespaced links, keep plain article slugs
            continue
        slug = href.lstrip("/")
        if slug in seen:
            continue
        seen.add(slug)
        out.append((slug, link.get_text(strip=True)))
    return out


def parse_character_page(slug: str) -> tuple[str, str | None]:
    """Return (name, image_color) for a character article page."""
    html = _get(f"{BASE}/{slug}")
    soup = BeautifulSoup(html, "html.parser")

    heading = soup.find(id="firstHeading")
    name = heading.get_text(strip=True) if heading else slug.replace("_", " ")

    image_color: str | None = None
    label = soup.find(
        lambda tag: tag.name == "i"
        and tag.get_text(strip=True).lower().startswith("image color")
    )
    if label:
        label_cell = label if label.name == "td" else label.find_parent("td")
        value_cell = label_cell.find_next_sibling("td") if label_cell else None
        if value_cell:
            # Prefer the swatch's background-color; fall back to any bare hex.
            m = re.search(r"background-color:\s*(#[0-9A-Fa-f]{6})", str(value_cell))
            if not m:
                m = re.search(r"#[0-9A-Fa-f]{6}", value_cell.get_text())
            if m:
                image_color = m.group(1 if m.re.groups else 0).upper()

    return name, image_color


def parse_gallery_page(slug: str) -> tuple[bool, list[dict]]:
    """Return (gallery_found, petit_images).

    petit_images: [{group_id, costume_id, filename}] for every *_0011 petit
    chibi asset, de-duplicated, in page order.
    """
    try:
        html = _get(f"{BASE}/{slug}/Gallery")
    except RuntimeError:
        return False, []

    seen: set[str] = set()
    images: list[dict] = []
    for group_id, costume_id in PETIT_RE.findall(html):
        filename = f"Game_Asset_petit_chr_{group_id}_{costume_id}_0011.png"
        if filename in seen:
            continue
        seen.add(filename)
        images.append(
            {"group_id": group_id, "costume_id": costume_id, "filename": filename}
        )
    return True, images


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS characters (
            slug          TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            image_color   TEXT,
            gallery_found INTEGER NOT NULL DEFAULT 0,
            scraped_at    INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS petit_images (
            slug        TEXT NOT NULL,
            group_id    TEXT NOT NULL,
            costume_id  TEXT NOT NULL,
            filename    TEXT NOT NULL,
            image_url   TEXT NOT NULL,
            local_path  TEXT,
            PRIMARY KEY (slug, filename)
        );
        """
    )
    conn.commit()


def upsert_character(conn: sqlite3.Connection, slug: str, name: str,
                     color: str | None, gallery_found: bool) -> None:
    conn.execute(
        """
        INSERT INTO characters (slug, name, image_color, gallery_found, scraped_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            name=excluded.name,
            image_color=excluded.image_color,
            gallery_found=excluded.gallery_found,
            scraped_at=excluded.scraped_at
        """,
        (slug, name, color, int(gallery_found), int(time.time())),
    )
    conn.commit()


def upsert_petit_image(conn: sqlite3.Connection, slug: str, img: dict,
                       local_path: str | None) -> None:
    conn.execute(
        """
        INSERT INTO petit_images
            (slug, group_id, costume_id, filename, image_url, local_path)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug, filename) DO UPDATE SET
            group_id=excluded.group_id,
            costume_id=excluded.costume_id,
            image_url=excluded.image_url,
            local_path=COALESCE(excluded.local_path, petit_images.local_path)
        """,
        (slug, img["group_id"], img["costume_id"], img["filename"],
         img["image_url"], local_path),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp932
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="only scrape the first N unique characters")
    ap.add_argument("--only", type=str, default=None,
                    help="comma-separated slugs to scrape (e.g. Special_Week,Gold_Ship)")
    ap.add_argument("--no-images", action="store_true",
                    help="do not download the petit PNGs")
    args = ap.parse_args()

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    print(f"[trainees] fetching {LIST_PAGE} …")
    characters = fetch_trainee_characters()
    print(f"[trainees] {len(characters)} unique characters found")

    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        characters = [c for c in characters if c[0] in wanted]
    if args.limit is not None:
        characters = characters[: args.limit]
    print(f"[trainees] scraping {len(characters)} this run\n")

    total_imgs = 0
    for i, (slug, list_name) in enumerate(characters, 1):
        print(f"[{i}/{len(characters)}] {slug}")
        try:
            name, color = parse_character_page(slug)
        except Exception as exc:  # noqa: BLE001
            print(f"    ! character page failed: {exc}")
            continue
        gallery_found, petit = parse_gallery_page(slug)
        upsert_character(conn, slug, name, color, gallery_found)
        print(f"    name={name!r}  color={color}  petit={len(petit)}"
              f"{'' if gallery_found else '  (no gallery)'}")

        for img in petit:
            img["image_url"] = _image_url(img["filename"])
            local_path: str | None = None
            if not args.no_images:
                dest = IMAGE_DIR / img["filename"]
                if dest.exists():
                    local_path = f"{IMAGE_DIR_REL}/{img['filename']}"
                else:
                    try:
                        data = _get(img["image_url"], binary=True)
                        dest.write_bytes(data)
                        local_path = f"{IMAGE_DIR_REL}/{img['filename']}"
                        print(f"      ↓ {img['filename']} ({len(data)} B)")
                    except Exception as exc:  # noqa: BLE001
                        print(f"      ! download failed {img['filename']}: {exc}")
            upsert_petit_image(conn, slug, img, local_path)
            total_imgs += 1

    conn.close()
    print(f"\n[trainees] done — {len(characters)} characters, {total_imgs} petit images")
    print(f"[trainees] db: {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
