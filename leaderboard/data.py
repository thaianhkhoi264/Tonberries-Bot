"""
leaderboard/data.py

Live data for the Monthly Fan Leaderboard image, read from the bot's databases:

  * monthly fan totals   — LOCAL_DB.circle_monthly_finals  (year, month)
                            (falls back to circle_member_snapshots)
  * fan character         — LOCAL_DB.player_links / user_character_roles /
                            character_roles  ->  TRAINEES_DB.characters
  * petit image + colour  — TRAINEES_DB.petit_images / characters

A member with no linked Discord account, or holding no fan role, gets
`NEUTRAL_COLOR` and no petit.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from global_config import LOCAL_DB, MAIN_SERVER_ID, TRAINEES_DB, game_now

REPO_ROOT = Path(__file__).resolve().parent.parent
NEUTRAL_COLOR = "#E9E9EF"
GENERIC_COSTUME = "000101"   # generic default petit — skipped
DEFAULT_REQUIREMENT = 20_000_000


def _ro(path: str) -> sqlite3.Connection | None:
    p = REPO_ROOT / path
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True) if p.exists() else None


def month_label(year: int | None = None, month: int | None = None) -> str:
    now = game_now()
    return datetime(year or now.year, month or now.month, 1).strftime("%B %Y")


def top_members(year: int | None = None, month: int | None = None,
                n: int = 30) -> list[tuple[str, int]]:
    """[(trainer_name, monthly_fans)] for the top `n` of the given month
    (default: current), highest first.

    Prefers `circle_monthly_finals` (per-month history); falls back to the
    rolling `circle_member_snapshots` for the current month / older bot state.
    """
    now = game_now()
    y, m = year or now.year, month or now.month
    conn = _ro(LOCAL_DB)
    if conn is None:
        return []
    try:
        rows: list = []
        for table in ("circle_monthly_finals", "circle_member_snapshots"):
            try:
                rows = conn.execute(
                    f"SELECT trainer_name, monthly_fans FROM {table} "
                    "WHERE year=? AND month=? ORDER BY monthly_fans DESC LIMIT ?",
                    (y, m, n),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            if rows:
                break
    finally:
        conn.close()
    return [(name.strip(), int(fans)) for name, fans in rows]


def latest_finalized_month() -> tuple[int, int] | None:
    """Newest (year, month) in `circle_monthly_finals` that is not the current
    in-game month — i.e. the most recent month whose totals are settled."""
    now = game_now()
    conn = _ro(LOCAL_DB)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT year, month FROM circle_monthly_finals "
            "WHERE year * 12 + month < ? ORDER BY year DESC, month DESC LIMIT 1",
            (now.year * 12 + now.month,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return (int(row[0]), int(row[1])) if row else None


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
    """Every rank whose monthly fans fell below `requirement` — any rank, up to
    1st place: if the whole club misses goal, the whole board is eliminated."""
    return {i for i, (_, fans) in enumerate(members, 1) if fans < requirement}


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


def slug_assets(slug: str) -> tuple[str | None, str | None]:
    """(petit_path, image_color) for a character slug."""
    if not slug:
        return None, None
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
        if not pr:
            pr = conn.execute(
                "SELECT normalized_path FROM petit_images_fallback "
                "WHERE slug=? AND normalized_path IS NOT NULL ORDER BY ordinal LIMIT 1",
                (slug,),
            ).fetchone()
        cr = conn.execute(
            "SELECT image_color FROM characters WHERE slug=?", (slug,)
        ).fetchone()
    finally:
        conn.close()
    return (pr[0] if pr else None), (cr[0] if cr and cr[0] else None)


# --- fan-role resolution (from members' actual Discord roles, not just /role) ---

def linked_discord_ids() -> dict[str, int]:
    """{trainer_name.lower(): discord_user_id} from player_links."""
    conn = _ro(LOCAL_DB)
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT trainer_name, discord_user_id FROM player_links"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {name.strip().lower(): int(uid) for name, uid in rows}


def fan_role_slugs(guild_id: int = MAIN_SERVER_ID) -> dict[int, str]:
    """{role_id: character_slug} for every known fan role (character_roles)."""
    conn = _ro(LOCAL_DB)
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT role_id, character_slug FROM character_roles "
            "WHERE guild_id=? AND character_slug IS NOT NULL",
            (guild_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {int(rid): slug for rid, slug in rows}


def user_fan_role_times(guild_id: int = MAIN_SERVER_ID) -> dict[tuple[int, int], int]:
    """{(user_id, role_id): added_at} from user_character_roles (only /role-assigned)."""
    conn = _ro(LOCAL_DB)
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT user_id, role_id, added_at FROM user_character_roles WHERE guild_id=?",
            (guild_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {(int(u), int(r)): int(t) for u, r, t in rows}


def resolve_fan_slug(discord_user_id: int | None, held_role_ids: set[int],
                     role_slugs: dict[int, str],
                     role_times: dict[tuple[int, int], int]) -> str | None:
    """The member's fan character slug: their held fan role with the newest
    `added_at` (falling back to any held fan role, then to a /role-tracked one)."""
    held = held_role_ids & role_slugs.keys()
    if held:
        best = max(held, key=lambda rid: role_times.get((discord_user_id, rid), 0))
        return role_slugs[best]
    tracked = [(rid, t) for (u, rid), t in role_times.items()
               if u == discord_user_id and rid in role_slugs]
    if tracked:
        return role_slugs[max(tracked, key=lambda x: x[1])[0]]
    return None


def fan_character(trainer_name: str) -> tuple[str | None, str | None]:
    """(petit_path, image_color) via /role-tracked assignments only — kept for
    the calibration sampledata. `build.py` uses the role-based path below."""
    role_slugs = fan_role_slugs()
    role_times = user_fan_role_times()
    uid = linked_discord_ids().get(trainer_name.strip().lower())
    if uid is None:
        return None, None
    slug = resolve_fan_slug(uid, set(), role_slugs, role_times)
    return slug_assets(slug) if slug else (None, None)
