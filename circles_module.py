import asyncio
import calendar
import math
from dataclasses import dataclass
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

STATUS_COLORS = {
    "goal_met":   discord.Colour.dark_green(),
    "on_track":   discord.Colour.green(),
    "behind":     discord.Colour.red(),
    "far_behind": discord.Colour(0x1a1a1a),
}

MAX_EMBEDS_PER_MESSAGE = 10


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MemberStatus:
    category: str
    emoji:    str
    color:    discord.Colour
    line:     str  # one-line status description for the embed


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
            "rankIconSrc": club["rank_icon_src"],
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
# Embed builders
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


def _member_status(monthly_gain: str | None) -> MemberStatus:
    now            = datetime.now(timezone.utc)
    days_in_month  = calendar.monthrange(now.year, now.month)[1]
    days_elapsed   = now.day
    days_remaining = days_in_month - days_elapsed

    monthly_target = DAILY_REQUIREMENT * days_in_month
    expected_today = DAILY_REQUIREMENT * days_elapsed
    gained         = _parse_fans(monthly_gain)

    if gained >= monthly_target:
        over = gained - monthly_target
        return MemberStatus(
            category="goal_met",
            emoji=STATUS_EMOJIS["goal_met"],
            color=STATUS_COLORS["goal_met"],
            line=f"Monthly goal reached — {over:,} ahead of target",
        )

    if gained >= expected_today:
        remaining = monthly_target - gained
        return MemberStatus(
            category="on_track",
            emoji=STATUS_EMOJIS["on_track"],
            color=STATUS_COLORS["on_track"],
            line=f"On track — {remaining:,} more fans to finish the month",
        )

    remaining    = monthly_target - gained
    catchup      = expected_today - gained

    if days_remaining > 0:
        daily_needed = math.ceil(remaining / days_remaining)
        details      = f"+{catchup:,} to get on pace · ~{daily_needed:,}/day for {days_remaining} days"

        if daily_needed >= STARE_THRESHOLD:
            return MemberStatus(
                category="far_behind",
                emoji=STATUS_EMOJIS["far_behind"],
                color=STATUS_COLORS["far_behind"],
                line=f"Very behind — {details}",
            )
        return MemberStatus(
            category="behind",
            emoji=STATUS_EMOJIS["behind"],
            color=STATUS_COLORS["behind"],
            line=f"Behind — {details}",
        )

    return MemberStatus(
        category="far_behind",
        emoji=STATUS_EMOJIS["far_behind"],
        color=STATUS_COLORS["far_behind"],
        line=f"Month ended — {remaining:,} short of the monthly goal",
    )


def _build_club_embed(data: dict) -> discord.Embed:
    tier         = data["rankTier"]
    needed       = data.get("needed", "N/A")
    needed_delta = data.get("neededDelta") or ""
    members      = data.get("members", [])
    scraped_at   = data.get("scraped_at", 0)
    rank_icon    = data.get("rankIconSrc") or ""

    emoji      = RANK_EMOJIS.get(tier, tier)
    next_emoji = _next_rank_emoji(tier)

    needed_int   = int(needed.replace(",", "")) if needed != "N/A" else 0
    active_count = len([m for m in members if not m.get("isInactive")])
    per_member   = f"{needed_int // active_count:,}" if active_count else "N/A"

    embed = discord.Embed(title="Club Monthly Fan Count", colour=discord.Colour.gold())
    embed.add_field(name="Rank", value=f"{emoji} {tier}", inline=True)
    embed.add_field(
        name=f"Until {next_emoji}" if next_emoji else "Until next rank",
        value=f"{needed} · {per_member}/member",
        inline=True,
    )
    if needed_delta:
        embed.add_field(name="Gained since yesterday", value=needed_delta, inline=True)

    if rank_icon:
        icon_url = rank_icon if rank_icon.startswith("http") else f"https://uma.moe{rank_icon}"
        embed.set_thumbnail(url=icon_url)

    embed.timestamp = datetime.fromtimestamp(scraped_at, tz=timezone.utc)
    embed.set_footer(text="Last updated")

    return embed


def _build_member_embed(member: dict, rank: int) -> discord.Embed:
    name    = member["name"]
    monthly = member.get("monthlyGain") or "0"
    gained  = _parse_fans(monthly)
    status  = _member_status(monthly)

    embed = discord.Embed(
        title=f"#{rank} {status.emoji} {name}",
        description=f"**{gained:,}** fans this month\n{status.line}",
        colour=status.color,
    )
    return embed


def _build_member_embeds(members: list[dict]) -> list[discord.Embed]:
    active = [m for m in members if not m.get("isInactive")]
    # Rank by monthly fans descending so position reflects current standing
    active.sort(key=lambda m: _parse_fans(m.get("monthlyGain")), reverse=True)
    return [_build_member_embed(m, i + 1) for i, m in enumerate(active)]


# ---------------------------------------------------------------------------
# Post / edit logic
# ---------------------------------------------------------------------------

async def _send_or_edit_embed(
    channel: discord.TextChannel,
    conn,
    key: str,
    embed: discord.Embed,
) -> str:
    """Edit the saved single-embed message if it still exists, else post new."""
    msg_id = await _get(conn, key)
    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(content=None, embed=embed)
            return msg_id
        except (discord.NotFound, discord.HTTPException):
            pass
    msg = await channel.send(embed=embed)
    return str(msg.id)


async def _send_or_edit_embeds(
    channel: discord.TextChannel,
    conn,
    key: str,
    embeds: list[discord.Embed],
) -> str:
    """Edit the saved multi-embed message if it still exists, else post new."""
    msg_id = await _get(conn, key)
    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(content=None, embeds=embeds)
            return msg_id
        except (discord.NotFound, discord.HTTPException):
            pass
    msg = await channel.send(embeds=embeds)
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

        # Header embed (single message)
        header_id = await _send_or_edit_embed(
            channel, conn, "circle_header_msg", _build_club_embed(data)
        )
        await _set(conn, "circle_header_msg", header_id)

        # Member embeds batched into groups of MAX_EMBEDS_PER_MESSAGE
        all_embeds = _build_member_embeds(data["members"])
        batches = [
            all_embeds[i : i + MAX_EMBEDS_PER_MESSAGE]
            for i in range(0, len(all_embeds), MAX_EMBEDS_PER_MESSAGE)
        ] if all_embeds else []

        # Collect previously-stored batch message IDs
        old_ids: list[str] = []
        for i in range(20):
            mid = await _get(conn, f"circle_members_msg_{i}")
            if mid is None:
                break
            old_ids.append(mid)

        new_ids: list[str] = []
        for i, batch in enumerate(batches):
            mid = await _send_or_edit_embeds(
                channel, conn, f"circle_members_msg_{i}", batch
            )
            new_ids.append(mid)
            await _set(conn, f"circle_members_msg_{i}", mid)

        # Delete surplus messages if batch count shrank
        for mid in old_ids[len(batches):]:
            try:
                await channel.get_partial_message(int(mid)).delete()
            except discord.NotFound:
                pass

        # Remove DB keys for deleted batches
        for i in range(len(new_ids), len(old_ids)):
            await conn.execute(
                "DELETE FROM circle_messages WHERE key=?",
                (f"circle_members_msg_{i}",),
            )

        await _set(conn, "last_scraped_at", scraped_at)
        await conn.commit()

    logger.info(
        f"[Circles] Updated — rank {data['rankTier']}, "
        f"{len(all_embeds)} member embed(s) in {len(batches)} message(s)"
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
