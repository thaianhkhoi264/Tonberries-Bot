import asyncio
import calendar
import math
from datetime import datetime, timezone

import aiosqlite
import discord

from bot import bot, logger
from global_config import CIRCLE_CHANNEL_ID, CIRCLES_DB, LOCAL_DB

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RANK_EMOJIS = {
    "B":  "<:rank_B:1508640944503914606>",
    "B+": "<:rank_B_plus:1508640946328436896>",
    "A":  "<:rank_A:1508640942335201481>",
    "A+": "<:rank_A_plus:1508640943375384666>",
    "S":  "<:rank_S:1508640947246862558>",
    "S+": "<:rank_S_plus:1508640948152963183>",
}

RANK_ORDER = ["D", "D+", "C", "C+", "B", "B+", "A", "A+", "S", "S+", "SS"]

DAILY_REQUIREMENT = 1_000_000
STARE_THRESHOLD   = 3_000_000

STATUS_EMOJIS = {
    "goal_met":   "<a:diapat:1508665594013286400>",
    "on_track":   "<a:dianod:1508662343322697839>",
    "behind":     "<a:diashake:1508662342060081253>",
    "far_behind": "<a:diastare:1508665580071161967>",
}


# ---------------------------------------------------------------------------
# Database helpers  (stored in LOCAL_DB so IDs survive restarts)
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS circle_messages (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await conn.commit()


async def _get(conn, key: str) -> str | None:
    async with conn.execute(
        "SELECT value FROM circle_messages WHERE key=?", (key,)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def _set(conn, key: str, value: str) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO circle_messages (key, value) VALUES (?, ?)",
        (key, value),
    )


# ---------------------------------------------------------------------------
# Read data from circles.db
# ---------------------------------------------------------------------------

async def _read_data() -> dict | None:
    try:
        async with aiosqlite.connect(CIRCLES_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM circle_club WHERE id=1") as cur:
                club = await cur.fetchone()
            if not club:
                return None
            club = dict(club)

            async with db.execute(
                "SELECT name, monthly_gain, seven_day_avg, is_inactive "
                "FROM circle_members"
            ) as cur:
                members = [dict(r) for r in await cur.fetchall()]

        return {
            "scraped_at":  club["scraped_at"],
            "rankTier":    club["rank_tier"],
            "rankNumber":  club["rank_number"],
            "needed":      club["needed"],
            "neededDelta": club["needed_delta"],
            "members": [
                {
                    "name":        m["name"],
                    "monthlyGain": m["monthly_gain"],
                    "sevenDayAvg": m["seven_day_avg"],
                    "isInactive":  bool(m["is_inactive"]),
                }
                for m in members
            ],
        }
    except Exception as exc:
        logger.warning(f"[Circles] Could not read circles.db: {exc}")
        return None


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _next_rank_emoji(tier: str) -> str:
    try:
        next_tier = RANK_ORDER[RANK_ORDER.index(tier) + 1]
    except (ValueError, IndexError):
        return ""
    return RANK_EMOJIS.get(next_tier, next_tier)


def _parse_fans(s: str | None) -> int:
    if not s:
        return 0
    try:
        return int(s.replace(",", "").lstrip("+"))
    except ValueError:
        return 0


def _member_status(monthly_gain: str | None) -> tuple[str, str]:
    now            = datetime.now(timezone.utc)
    days_in_month  = calendar.monthrange(now.year, now.month)[1]
    days_elapsed   = now.day
    days_remaining = days_in_month - days_elapsed

    monthly_target = DAILY_REQUIREMENT * days_in_month
    expected_today = DAILY_REQUIREMENT * days_elapsed
    gained         = _parse_fans(monthly_gain)

    if gained >= monthly_target:
        over = gained - monthly_target
        return (
            f"Monthly goal reached — {over:,} ahead of target",
            STATUS_EMOJIS["goal_met"],
        )

    if gained >= expected_today:
        remaining = monthly_target - gained
        return (
            f"On track — {remaining:,} more fans to complete the month",
            STATUS_EMOJIS["on_track"],
        )

    remaining = monthly_target - gained
    if days_remaining > 0:
        daily_needed = math.ceil(remaining / days_remaining)
        emoji = (
            STATUS_EMOJIS["far_behind"]
            if daily_needed >= STARE_THRESHOLD
            else STATUS_EMOJIS["behind"]
        )
        return (
            f"Behind — needs ~{daily_needed:,}/day for the remaining {days_remaining} days to hit the goal",
            emoji,
        )

    return (
        f"Month ended — {remaining:,} short of the monthly goal",
        STATUS_EMOJIS["far_behind"],
    )


def _build_club_message(data: dict) -> str:
    tier         = data["rankTier"]
    needed       = data.get("needed", "N/A")
    needed_delta = data.get("neededDelta") or ""
    members      = data.get("members", [])
    emoji        = RANK_EMOJIS.get(tier, tier)
    scraped_at   = data.get("scraped_at", 0)

    needed_int = int(needed.replace(",", "")) if needed != "N/A" else 0
    count      = len(members)
    per_member = f"{needed_int // count:,}" if count else "N/A"

    delta_line = f"\nFans gained since yesterday: {needed_delta}" if needed_delta else ""

    return (
        f"# Club Monthly Fan Count\n"
        f"## Rank {emoji}\n"
        f"## Needed until rank {_next_rank_emoji(tier)}: {needed} / {per_member} per member"
        f"{delta_line}\n"
        f"-# Last updated <t:{scraped_at}:F> · <t:{scraped_at}:R>"
    )


def _build_member_messages(members: list[dict]) -> list[str]:
    active  = [m for m in members if not m.get("isInactive")]
    chunks  = []
    current = "## Members\n"

    for i, m in enumerate(active, 1):
        monthly            = m["monthlyGain"] or "N/A"
        status_text, emoji = _member_status(m.get("monthlyGain"))
        entry = f"{i}. **{m['name']}** — {monthly} {emoji}\n-# {status_text}\n"

        if len(current) + len(entry) > 1900:
            chunks.append(current.rstrip())
            current = entry
        else:
            current += entry

    if current.strip():
        chunks.append(current.rstrip())

    return chunks


# ---------------------------------------------------------------------------
# Post / edit logic
# ---------------------------------------------------------------------------

async def _send_or_edit(
    channel: discord.TextChannel,
    conn,
    key: str,
    content: str,
) -> str:
    """Edit the saved message if it exists, otherwise post a new one. Returns message ID."""
    msg_id = await _get(conn, key)
    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(content=content)
            return msg_id
        except (discord.NotFound, discord.HTTPException):
            pass  # fall through to re-post
    msg = await channel.send(content)
    return str(msg.id)


async def post_or_edit(force: bool = False) -> None:
    if not bot.is_ready():
        return

    channel = bot.get_channel(CIRCLE_CHANNEL_ID)
    if not channel:
        logger.error("[Circles] Channel not found — check CIRCLE_CHANNEL_ID in global_config.py")
        return

    data = await _read_data()
    if not data:
        logger.warning("[Circles] No data available in circles.db yet — skipping")
        return

    scraped_at = str(data["scraped_at"])

    async with aiosqlite.connect(LOCAL_DB) as conn:
        if not force and await _get(conn, "last_scraped_at") == scraped_at:
            logger.debug("[Circles] Data unchanged since last post — skipping")
            return

        # Header message
        header_id = await _send_or_edit(
            channel, conn, "circle_header_msg", _build_club_message(data)
        )
        await _set(conn, "circle_header_msg", header_id)

        # Member messages (variable number of chunks)
        new_texts = _build_member_messages(data["members"])

        old_ids: list[str] = []
        for i in range(20):
            mid = await _get(conn, f"circle_members_msg_{i}")
            if mid is None:
                break
            old_ids.append(mid)

        new_ids: list[str] = []
        for i, text in enumerate(new_texts):
            mid = await _send_or_edit(
                channel, conn,
                f"circle_members_msg_{i}",
                text,
            )
            new_ids.append(mid)

        # Delete surplus messages if chunk count shrank
        for mid in old_ids[len(new_texts):]:
            try:
                await channel.get_partial_message(int(mid)).delete()
            except discord.NotFound:
                pass

        # Persist new IDs, remove keys for deleted chunks
        for i, mid in enumerate(new_ids):
            await _set(conn, f"circle_members_msg_{i}", mid)
        for i in range(len(new_ids), len(old_ids)):
            await conn.execute(
                "DELETE FROM circle_messages WHERE key=?",
                (f"circle_members_msg_{i}",),
            )

        await _set(conn, "last_scraped_at", scraped_at)
        await conn.commit()

    logger.info(
        f"[Circles] Updated — rank {data['rankTier']}, "
        f"{len(new_texts)} member message(s)"
    )


# ---------------------------------------------------------------------------
# Background loop + startup
# ---------------------------------------------------------------------------

async def _circle_update_loop() -> None:
    while True:
        await asyncio.sleep(1800)  # check every 30 min
        try:
            await post_or_edit()
        except Exception as exc:
            logger.error(f"[Circles] Loop error: {exc}")


async def start_background_task() -> None:
    await init_db()
    await post_or_edit()
    asyncio.create_task(_circle_update_loop())
    logger.info("[Circles] Background task started")
