"""
leaderboard/data.py

Live data for the Monthly Fan Leaderboard image, read from the bot's databases:

  * monthly fan totals   — LOCAL_DB.circle_member_snapshots  (year, month)
  * fan character         — LOCAL_DB.player_links / user_character_roles /
                            character_roles  ->  TRAINEES_DB.characters
  * petit image + colour  — TRAINEES_DB.petit_images / characters

A member with no linked Discord account, or holding no fan role, gets
`NEUTRAL_COLOR` and no petit.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from global_config import LOCAL_DB, MAIN_SERVER_ID, TRAINEES_DB

REPO_ROOT = Path(__file__).resolve().parent.parent
NEUTRAL_COLOR = "#E9E9EF"
GENERIC_COSTUME = "000101"   # generic default petit — skipped
DEFAULT_REQUIREMENT = 20_000_000
ELIM_RANK_FLOOR = 20         # only ranks >= this can be "eliminated"


def _ro(path: str) -> sqlite3.Connection | None:
    p = REPO_ROOT / path
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True) if p.exists() else None


def month_label(year: int | None = None, month: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    return datetime(year or now.year, month or now.month, 1).strftime("%B %Y")


def top_members(year: int | None = None, month: int | None = None,
                n: int = 30) -> list[tuple[str, int]]:
    """[(trainer_name, monthly_fans)] for the top `n` of the given month
    (default: current), highest first."""
    now = datetime.now(timezone.utc)
    y, m = year or now.year, month or now.month
    conn = _ro(LOCAL_DB)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT trainer_name, monthly_fans FROM circle_member_snapshots "
            "WHERE year=? AND month=? ORDER BY monthly_fans DESC LIMIT ?",
            (y, m, n),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [(name.strip(), int(fans)) for name, fans in rows]


def monthly_requirement() -> int:
    """The club's per-member monthly fan requirement (from circle_messages)."""
    conn = _ro(LOCAL_DB)
    if conn is None:
        return DEFAULT_REQUIREMENT
    try:
        row = conn.execute(
            "SELECT value FROM circle_messages WHERE key='monthly_requirement'"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()
    if row and row[0]:
        try:
            return int(json.loads(row[0]).get("value", DEFAULT_REQUIREMENT))
        except (ValueError, TypeError):
            pass
    return DEFAULT_REQUIREMENT


def eliminated_ranks(members: list[tuple[str, int]], requirement: int) -> set[int]:
    """Ranks (>= ELIM_RANK_FLOOR) whose monthly fans fell below `requirement`."""
    return {i for i, (_, fans) in enumerate(members, 1)
            if i >= ELIM_RANK_FLOOR and fans < requirement}


@lru_cache(maxsize=1)
def links_available() -> bool:
    conn = _ro(LOCAL_DB)
    if conn is None:
        return False
    try:
        conn.execute("SELECT 1 FROM player_links LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


@lru_cache(maxsize=1)
def character_pool() -> list[tuple[str, str]]:
    """[(petit_path, image_color)] for every character with both (generic
    default petit excluded), sorted — used for the calibration fallback."""
    conn = _ro(TRAINEES_DB)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT p.normalized_path, c.image_color
            FROM petit_images p
            JOIN characters c ON c.slug = p.slug
            WHERE p.normalized_path IS NOT NULL AND c.image_color IS NOT NULL
              AND p.costume_id != ?
            GROUP BY p.slug
            """,
            (GENERIC_COSTUME,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return sorted((path, color) for path, color in rows)


def _slug_assets(slug: str) -> tuple[str | None, str | None]:
    conn = _ro(TRAINEES_DB)
    if conn is None:
        return None, None
    try:
        pr = conn.execute(
            "SELECT normalized_path FROM petit_images "
            "WHERE slug=? AND normalized_path IS NOT NULL AND costume_id != ? "
            "ORDER BY costume_id LIMIT 1",
            (slug, GENERIC_COSTUME),
        ).fetchone()
        cr = conn.execute(
            "SELECT image_color FROM characters WHERE slug=?", (slug,)
        ).fetchone()
    finally:
        conn.close()
    return (pr[0] if pr else None), (cr[0] if cr and cr[0] else None)


def fan_character(trainer_name: str) -> tuple[str | None, str | None]:
    """(petit_path, image_color) for the member's **most recent** fan character.
    (None, None) if the trainer isn't linked or holds no fan role."""
    conn = _ro(LOCAL_DB)
    if conn is None:
        return None, None
    try:
        row = conn.execute(
            """
            SELECT cr.character_slug
            FROM player_links pl
            JOIN user_character_roles ucr ON ucr.discord_user_id = pl.discord_user_id
            JOIN character_roles cr
                 ON cr.guild_id = ucr.guild_id AND cr.role_id = ucr.role_id
            WHERE pl.trainer_name = ? COLLATE NOCASE AND cr.guild_id = ?
            ORDER BY ucr.added_at DESC
            LIMIT 1
            """,
            (trainer_name.strip(), MAIN_SERVER_ID),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()
    if not row or not row[0]:
        return None, None
    return _slug_assets(row[0])
