import asyncio
import calendar
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord

import uma_moe_api
from bot import bot, logger
from global_config import CIRCLE_CHANNEL_ID, LOCAL_DB

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

# uma.moe refreshes ~15:10 UTC daily; we pull 1 h later to be safe
UPDATE_HOUR_UTC = 16


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
# Database helpers  (stored in LOCAL_DB so message IDs survive restarts)
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
# Member helpers
# ---------------------------------------------------------------------------

def _monthly_gain(daily_fans: list[int]) -> int:
    """Sum of daily fan gains for the month.

    uma.moe returns daily_fans as an array of per-day gain values.
    If this assumption is wrong (e.g. cumulative values), adjust here.
    """
    return sum(daily_fans) if daily_fans else 0


def _is_inactive(daily_fans: list[int]) -> bool:
    return _monthly_gain(daily_fans) == 0


# ---------------------------------------------------------------------------
# Embed helpers
# ---------------------------------------------------------------------------

def _next_rank_emoji(tier: str) -> str:
    try:
        next_tier = RANK_ORDER[RANK_ORDER.index(tier) + 1]
    except (ValueError, IndexError):
        return ""
    return RANK_EMOJIS.get(next_tier, next_tier)


def _next_update_ts() -> int:
    """Unix timestamp of the next scheduled 16:00 UTC pull."""
    now = datetime.now(timezone.utc)
    today_update = now.replace(hour=UPDATE_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now >= today_update:
        return int((today_update + timedelta(days=1)).timestamp())
    return int(today_update.timestamp())


def _member_status(gained: int) -> MemberStatus:
    now            = datetime.now(timezone.utc)
    days_in_month  = calendar.monthrange(now.year, now.month)[1]
    days_elapsed   = now.day
    days_remaining = days_in_month - days_elapsed

    monthly_target = DAILY_REQUIREMENT * days_in_month
    expected_today = DAILY_REQUIREMENT * days_elapsed

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

    remaining = monthly_target - gained
    catchup   = expected_today - gained

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


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def _build_club_embed(api_data: dict) -> discord.Embed:
    circle  = api_data.get("circle", {})
    members = api_data.get("members", [])

    tier_idx = min(max(int(api_data.get("club_rank", 0)), 0), len(RANK_ORDER) - 1)
    tier     = RANK_ORDER[tier_idx]
    emoji    = RANK_EMOJIS.get(tier, tier)
    next_emoji = _next_rank_emoji(tier)

    fans_to_next          = int(api_data.get("fans_to_next_tier", 0))
    yesterday_fans_to_next = int(api_data.get("yesterday_fans_to_next_tier", fans_to_next))
    gained_today          = yesterday_fans_to_next - fans_to_next  # positive = gained fans

    monthly_rank  = int(circle.get("monthly_rank", 0))
    active_count  = sum(1 for m in members if not _is_inactive(m.get("daily_fans") or []))
    per_member    = f"{fans_to_next // active_count:,}" if active_count else "N/A"

    next_ts = _next_update_ts()

    raw_ts = circle.get("last_updated", "")
    try:
        last_updated = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        last_updated = datetime.now(timezone.utc)

    embed = discord.Embed(title="Club Monthly Fan Count", colour=discord.Colour.gold())
    embed.add_field(
        name="Rank",
        value=f"{emoji} {tier} · #{monthly_rank:,}",
        inline=True,
    )
    embed.add_field(
        name=f"Until {next_emoji}" if next_emoji else "Until next rank",
        value=f"{fans_to_next:,} · {per_member}/member",
        inline=True,
    )
    if gained_today != 0:
        sign = "+" if gained_today > 0 else ""
        embed.add_field(
            name="Gained since yesterday",
            value=f"{sign}{gained_today:,}",
            inline=True,
        )
    embed.add_field(
        name="Next refresh",
        value=f"<t:{next_ts}:F>\n<t:{next_ts}:R>",
        inline=False,
    )
    embed.timestamp = last_updated
    embed.set_footer(text="Last updated")

    return embed


def _build_member_embed(member: dict, rank: int) -> discord.Embed:
    name       = member.get("trainer_name", "Unknown")
    daily_fans = member.get("daily_fans") or []
    gained     = _monthly_gain(daily_fans)
    status     = _member_status(gained)

    embed = discord.Embed(
        title=f"#{rank} {status.emoji} {name}",
        description=f"**{gained:,}** fans this month\n{status.line}",
        colour=status.color,
    )
    return embed


def _build_member_embeds(members: list[dict]) -> list[discord.Embed]:
    active = [m for m in members if not _is_inactive(m.get("daily_fans") or [])]
    active.sort(key=lambda m: _monthly_gain(m.get("daily_fans") or []), reverse=True)
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

    try:
        api_data = await uma_moe_api.fetch_circle()
    except Exception as exc:
        logger.error(f"[Circles] API fetch failed: {exc}")
        return

    last_updated = api_data.get("circle", {}).get("last_updated", "")

    async with aiosqlite.connect(LOCAL_DB) as conn:
        if not force and await _get(conn, "last_circle_updated") == last_updated:
            logger.debug("[Circles] Data unchanged since last post — skipping")
            return

        header_id = await _send_or_edit_embed(
            channel, conn, "circle_header_msg", _build_club_embed(api_data)
        )
        await _set(conn, "circle_header_msg", header_id)

        all_embeds = _build_member_embeds(api_data.get("members", []))
        batches = [
            all_embeds[i : i + MAX_EMBEDS_PER_MESSAGE]
            for i in range(0, len(all_embeds), MAX_EMBEDS_PER_MESSAGE)
        ] if all_embeds else []

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

        for mid in old_ids[len(batches):]:
            try:
                await channel.get_partial_message(int(mid)).delete()
            except discord.NotFound:
                pass

        for i in range(len(new_ids), len(old_ids)):
            await conn.execute(
                "DELETE FROM circle_messages WHERE key=?",
                (f"circle_members_msg_{i}",),
            )

        await _set(conn, "last_circle_updated", last_updated)
        await conn.commit()

    tier_idx = min(max(int(api_data.get("club_rank", 0)), 0), len(RANK_ORDER) - 1)
    logger.info(
        f"[Circles] Updated — tier {RANK_ORDER[tier_idx]}, "
        f"rank #{api_data.get('circle', {}).get('monthly_rank', '?')}, "
        f"{len(all_embeds)} member embed(s) in {len(batches)} message(s)"
    )


# ---------------------------------------------------------------------------
# Background loop + startup
# ---------------------------------------------------------------------------

async def _circle_update_loop() -> None:
    while True:
        now          = datetime.now(timezone.utc)
        today_update = now.replace(hour=UPDATE_HOUR_UTC, minute=0, second=0, microsecond=0)
        next_run     = today_update if now < today_update else today_update + timedelta(days=1)

        wait_secs = (next_run - now).total_seconds()
        logger.info(
            f"[Circles] Next scheduled pull in {wait_secs / 3600:.1f}h "
            f"at {next_run.strftime('%Y-%m-%d %H:%M UTC')}"
        )
        await asyncio.sleep(wait_secs)

        try:
            await post_or_edit()
        except Exception as exc:
            logger.error(f"[Circles] Loop error: {exc}")


async def start_background_task() -> None:
    await init_db()
    await post_or_edit()
    asyncio.create_task(_circle_update_loop())
    logger.info("[Circles] Background task started")
