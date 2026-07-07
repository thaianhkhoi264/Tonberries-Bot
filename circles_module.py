import asyncio
import calendar
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord

import uma_moe_api
from bot import bot, logger
from global_config import CIRCLE_CHANNEL_ID, GENERAL_CHANNEL_ID, LOCAL_DB, OWNER_USER_IDS

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

# 1-indexed to match API's club_rank field (1=D, 2=D+, ... 11=SS)
RANK_ORDER = ["", "D", "D+", "C", "C+", "B", "B+", "A", "A+", "S", "S+", "SS"]

MONTHLY_REQUIREMENT = 20_000_000
STARE_THRESHOLD     = 3_000_000

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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS circle_member_snapshots (
                viewer_id    INTEGER PRIMARY KEY,
                trainer_name TEXT,
                monthly_fans INTEGER NOT NULL,
                saved_at     TEXT NOT NULL,
                year         INTEGER,
                month        INTEGER
            )
        """)
        # Migrate existing rows that predate the year/month columns
        for col in ("year", "month"):
            try:
                await conn.execute(f"ALTER TABLE circle_member_snapshots ADD COLUMN {col} INTEGER")
            except Exception:
                pass
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


async def _save_snapshots(conn, members: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    for m in members:
        vid = m.get("viewer_id")
        if vid is None:
            continue
        await conn.execute(
            """INSERT OR REPLACE INTO circle_member_snapshots
               (viewer_id, trainer_name, monthly_fans, saved_at, year, month)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (int(vid), m.get("trainer_name", ""), _monthly_gain(m.get("daily_fans") or []),
             now.isoformat(), now.year, now.month),
        )


async def _load_snapshots(conn) -> dict:
    now = datetime.now(timezone.utc)
    snapshots: dict = {}
    async with conn.execute(
        "SELECT viewer_id, trainer_name, monthly_fans FROM circle_member_snapshots "
        "WHERE year=? AND month=?",
        (now.year, now.month),
    ) as cur:
        async for row in cur:
            snapshots[row[0]] = {"trainer_name": row[1], "monthly_fans": row[2]}
    return snapshots


async def get_shaming_enabled() -> bool:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        val = await _get(conn, "shaming_enabled")
    return val == "1"


async def set_shaming_enabled(enabled: bool) -> None:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _set(conn, "shaming_enabled", "1" if enabled else "0")
        await conn.commit()


# ---------------------------------------------------------------------------
# Member helpers
# ---------------------------------------------------------------------------

def _monthly_gain(daily_fans: list[int]) -> int:
    """Calculate fans gained this month from the daily_fans array.

    uma.moe stores cumulative lifetime fan counts per day.
    The first non-zero entry may be negative: this is the previous month's
    ending total stored negated as a baseline marker.  Monthly gain =
    last positive value - abs(first non-zero).
    """
    non_zero = [f for f in daily_fans if f != 0]
    if not non_zero:
        return 0
    baseline = abs(non_zero[0])
    positives = [f for f in non_zero if f > 0]
    if not positives:
        return 0
    return positives[-1] - baseline


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

    monthly_target = MONTHLY_REQUIREMENT
    expected_today = MONTHLY_REQUIREMENT * days_elapsed // days_in_month

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

    tier     = RANK_ORDER[min(max(int(api_data.get("club_rank", 1)), 1), len(RANK_ORDER) - 1)]
    emoji    = RANK_EMOJIS.get(tier, tier)
    next_emoji = _next_rank_emoji(tier)

    fans_to_next  = int(api_data.get("fans_to_next_tier", 0))

    # Circle-level fans gained since yesterday (always positive unless data anomaly)
    monthly_point   = int(circle.get("monthly_point", 0))
    yesterday_point = int(circle.get("yesterday_points", monthly_point))
    gained_today    = monthly_point - yesterday_point

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
    if gained_today > 0:
        embed.add_field(
            name="Gained since yesterday",
            value=f"+{gained_today:,}",
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


def _add_group_field(embed: discord.Embed, title: str, lines: list[str]) -> None:
    """Add lines as one or more fields, splitting at the 1024-char limit."""
    chunks: list[str] = []
    chunk: list[str] = []
    chunk_len = 0
    for line in lines:
        if chunk_len + len(line) + 1 > 1024 and chunk:
            chunks.append("\n".join(chunk))
            chunk = []
            chunk_len = 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        chunks.append("\n".join(chunk))
    for i, c in enumerate(chunks):
        embed.add_field(name=title if i == 0 else "\u200b", value=c, inline=False)


def _build_report_embed(members: list[dict], snapshots: dict) -> discord.Embed:
    embed = discord.Embed(
        title="Daily Fan Report",
        colour=discord.Colour.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Generated")

    now           = datetime.now(timezone.utc)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    daily_quota   = MONTHLY_REQUIREMENT // days_in_month

    goal_met: list[tuple] = []
    behind:   list[tuple] = []
    no_gain:  list[tuple] = []

    for m in members:
        vid     = m.get("viewer_id")
        name    = m.get("trainer_name", "Unknown")
        current = _monthly_gain(m.get("daily_fans") or [])
        snap    = snapshots.get(int(vid)) if vid is not None else None
        daily   = current - snap["monthly_fans"] if snap else None

        if daily is not None and daily >= daily_quota:
            goal_met.append((name, daily))
        elif daily is not None and daily > 0:
            behind.append((name, daily))
        else:
            no_gain.append((name, current))

    goal_met.sort(key=lambda r: r[1], reverse=True)
    behind.sort(  key=lambda r: r[1], reverse=True)
    no_gain.sort( key=lambda r: r[1], reverse=True)

    if not goal_met and not behind and not no_gain:
        embed.description = "No members."
        return embed

    # Color driven by whichever group has the most members; ties favour the worse status
    dominant_color = max(
        [
            (len(no_gain),  STATUS_COLORS["far_behind"]),
            (len(behind),   STATUS_COLORS["behind"]),
            (len(goal_met), STATUS_COLORS["goal_met"]),
        ],
        key=lambda x: x[0],
    )[1]
    embed.colour = dominant_color

    SEP = "\u2500" * 28

    if goal_met:
        lines = [f"**{name}** — +{daily:,}" for name, daily in goal_met]
        if behind or no_gain:
            lines.append(SEP)
        _add_group_field(embed, f"Goal Reached! {STATUS_EMOJIS['goal_met']}", lines)

    if behind:
        lines = [f"**{name}** — +{daily:,}" for name, daily in behind]
        if no_gain:
            lines.append(SEP)
        _add_group_field(embed, f"Behind Goal {STATUS_EMOJIS['behind']}", lines)

    if no_gain:
        lines = [f"**{name}**" for name, _ in no_gain]
        _add_group_field(embed, f"Did nothing {STATUS_EMOJIS['far_behind']}", lines)

    return embed


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


async def send_daily_report(
    user_ids: list[int],
    *,
    api_data: dict | None = None,
    snapshots: dict | None = None,
    post_channel_id: int | None = None,
) -> None:
    """DM the daily fan-gain report to each user in *user_ids*.

    Pass *snapshots* to use pre-loaded snapshot data (e.g. captured before the
    current pull overwrites them).  If omitted, snapshots are loaded from DB.
    Pass *post_channel_id* to also post the embed to a channel (used for
    the public shaming feature).
    """
    if api_data is None:
        try:
            api_data = await uma_moe_api.fetch_circle()
        except Exception as exc:
            logger.error(f"[Circles] Report API fetch failed: {exc}")
            return

    _raw = api_data.get("members", [])
    _current_ts = (
        max((m.get("last_updated", "") for m in _raw), default="")
        or api_data.get("circle", {}).get("last_updated", "")
    )
    members = [m for m in _raw if m.get("last_updated") == _current_ts]

    if snapshots is None:
        async with aiosqlite.connect(LOCAL_DB) as conn:
            snapshots = await _load_snapshots(conn)

    embed = _build_report_embed(members, snapshots)

    for uid in user_ids:
        try:
            user = await bot.fetch_user(uid)
            await user.send(embed=embed)
        except Exception as exc:
            logger.error(f"[Circles] Failed to DM report to {uid}: {exc}")

    if post_channel_id is not None:
        channel = bot.get_channel(post_channel_id)
        if channel:
            try:
                await channel.send(embed=embed)
            except Exception as exc:
                logger.error(f"[Circles] Failed to post report to channel {post_channel_id}: {exc}")

    logger.info(f"[Circles] Daily report sent to {len(user_ids)} owner(s)")


async def post_or_edit(force: bool = False, save_snapshots: bool = False) -> bool:
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

    _raw_members = api_data.get("members", [])
    last_updated = (
        max((m.get("last_updated", "") for m in _raw_members), default="")
        or api_data.get("circle", {}).get("last_updated", "")
    )

    async with aiosqlite.connect(LOCAL_DB) as conn:
        stored_last_updated = await _get(conn, "last_circle_updated")
        is_new_data = stored_last_updated != last_updated

        if not force and not is_new_data:
            logger.debug("[Circles] Data unchanged since last post — skipping")
            return False

        header_id = await _send_or_edit_embed(
            channel, conn, "circle_header_msg", _build_club_embed(api_data)
        )
        await _set(conn, "circle_header_msg", header_id)

        current_members = [
            m for m in api_data.get("members", [])
            if m.get("last_updated") == last_updated
        ]
        all_embeds = _build_member_embeds(current_members)
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

        if save_snapshots or is_new_data:
            await _save_snapshots(conn, current_members)
        await _set(conn, "last_circle_updated", last_updated)
        await conn.commit()

    tier = RANK_ORDER[min(max(int(api_data.get("club_rank", 1)), 1), len(RANK_ORDER) - 1)]
    logger.info(
        f"[Circles] Updated — tier {tier}, "
        f"rank #{api_data.get('circle', {}).get('monthly_rank', '?')}, "
        f"{len(all_embeds)} member embed(s) in {len(batches)} message(s)"
    )
    return True


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
            async with aiosqlite.connect(LOCAL_DB) as conn:
                old_snapshots = await _load_snapshots(conn)
            updated = await post_or_edit(save_snapshots=True)
            if updated:
                shaming = await get_shaming_enabled()
                await send_daily_report(
                    list(OWNER_USER_IDS),
                    snapshots=old_snapshots,
                    post_channel_id=GENERAL_CHANNEL_ID if shaming else None,
                )
            else:
                logger.info("[Circles] API data unchanged — skipping daily report")
        except Exception as exc:
            logger.error(f"[Circles] Loop error: {exc}")


async def start_background_task() -> None:
    await init_db()
    await post_or_edit(force=True)
    asyncio.create_task(_circle_update_loop())
    logger.info("[Circles] Background task started")
