import asyncio
import calendar
import json
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

_monthly_requirement: int = 20_000_000   # kept in sync with DB; use get/set_monthly_requirement()
STARE_THRESHOLD      = 3_000_000

STATUS_EMOJIS = {
    "goal_met":   "<a:diapat:1508665594013286400>",
    "on_track":   "<a:dianod:1508662343322697839>",
    "behind":     "<a:diashake:1508662342060081253>",
    "far_behind": "<a:diastare:1508665580071161967>",
    "hitlist":    "<:diascared:1528840090451837088>"
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
        # Load persisted monthly requirement into the module variable
        global _monthly_requirement
        raw = await _get(conn, "monthly_requirement")
        if raw:
            try:
                _monthly_requirement = int(json.loads(raw).get("value", _monthly_requirement))
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


async def get_monthly_requirement() -> dict:
    """Return {"value": int, "changed_at": str|None, "changed_by": int|None}."""
    async with aiosqlite.connect(LOCAL_DB) as conn:
        raw = await _get(conn, "monthly_requirement")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"value": _monthly_requirement, "changed_at": None, "changed_by": None}


async def set_monthly_requirement(new_val: int, changed_by: int) -> None:
    """Persist a new fan requirement and update the in-memory copy."""
    global _monthly_requirement
    _monthly_requirement = new_val
    data = {
        "value": new_val,
        "changed_at": datetime.now(timezone.utc).isoformat(),
        "changed_by": changed_by,
    }
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _set(conn, "monthly_requirement", json.dumps(data))
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


def _last_daily_gains(daily_fans: list[int], n: int = 3) -> list[int]:
    """Return fan gains for the last *n* calendar days (today-n+1 through today).

    daily_fans[i] is cumulative lifetime fans as of day i.  daily_fans[0] may
    be negative — a baseline marker storing the previous month's total negated.
    """
    now   = datetime.now(timezone.utc)
    today = now.day
    gains = []
    for day in range(max(1, today - n + 1), today + 1):
        if 1 <= day < len(daily_fans):
            curr = daily_fans[day]
            prev = daily_fans[day - 1]
            if curr > 0 and prev < 0:
                # Day 1: prev is the negative baseline marker
                gains.append(max(0, curr - abs(prev)))
            elif curr > 0 and prev > 0:
                gains.append(max(0, curr - prev))
            else:
                gains.append(0)
        else:
            gains.append(0)
    return gains


def _classify_lists(members: list[dict]) -> tuple[list[dict], list[dict]]:
    """Classify members into (watchlist_entries, hitlist_entries).

    Each entry: {"viewer_id": int, "trainer_name": str, "daily_needed": int, "gains3": list[int]}
    Hitlist takes priority and is checked first.
    """
    now            = datetime.now(timezone.utc)
    days_in_month  = calendar.monthrange(now.year, now.month)[1]
    days_remaining = days_in_month - now.day
    daily_quota    = _monthly_requirement // days_in_month

    watchlist: list[dict] = []
    hitlist:   list[dict] = []

    for m in members:
        df           = m.get("daily_fans") or []
        monthly_gain = _monthly_gain(df)
        gains3       = _last_daily_gains(df, n=3)

        # Members who reached the goal are never on either list
        if monthly_gain >= _monthly_requirement or days_remaining <= 0:
            continue

        remaining    = _monthly_requirement - monthly_gain
        daily_needed = math.ceil(remaining / days_remaining)

        entry: dict = {
            "viewer_id":    m.get("viewer_id"),
            "trainer_name": m.get("trainer_name", "Unknown"),
            "daily_needed": daily_needed,
            "gains3":       gains3,
        }

        # Hitlist: all 3 gains ≤ 10k AND needs 2× daily_quota+/day (checked first, takes priority)
        if all(g <= 10_000 for g in gains3) and daily_needed >= 2 * daily_quota:
            hitlist.append(entry)
        # Watchlist: not reached goal AND all 3 gains < daily_quota AND needs 1.5× daily_quota+/day
        elif all(g < daily_quota for g in gains3) and daily_needed >= int(1.5 * daily_quota):
            watchlist.append(entry)

    return watchlist, hitlist


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
    """Unix timestamp of the next scheduled refresh (XX:15, XX:35, or XX:55 UTC)."""
    now = datetime.now(timezone.utc)
    for minute in (15, 35, 55):
        candidate = now.replace(minute=minute, second=0, microsecond=0)
        if candidate > now:
            return int(candidate.timestamp())
    # All three passed this hour; wrap to next hour's :15
    return int((now + timedelta(hours=1)).replace(minute=15, second=0, microsecond=0).timestamp())


def _member_status(gained: int) -> MemberStatus:
    now            = datetime.now(timezone.utc)
    days_in_month  = calendar.monthrange(now.year, now.month)[1]
    days_elapsed   = now.day
    days_remaining = days_in_month - days_elapsed

    monthly_target = _monthly_requirement
    expected_today = _monthly_requirement * days_elapsed // days_in_month

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
    daily_quota   = _monthly_requirement // days_in_month

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

    # Color driven by total daily fans vs collective daily quota
    total_daily    = sum(d for _, d in goal_met) + sum(d for _, d in behind)
    total_required = daily_quota * len(members)
    if total_daily >= total_required:
        embed.colour = STATUS_COLORS["goal_met"]
    elif total_daily >= total_required * 0.75:
        embed.colour = STATUS_COLORS["behind"]
    else:
        embed.colour = STATUS_COLORS["far_behind"]

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


def _format_list_line(e: dict) -> str:
    if e.get("sniped"):
        return f"~~**{e['trainer_name']}**~~ - Eliminated"
    g = e["gains3"]
    return (
        f"**{e['trainer_name']}** — {e['daily_needed']:,}/day needed · "
        f"last 3d: {g[0]:,} / {g[1]:,} / {g[2]:,}"
    )


def _build_list_lines(entries: list[dict]) -> list[str]:
    active  = [e for e in entries if not e.get("sniped")]
    sniped  = [e for e in entries if e.get("sniped")]
    lines   = [_format_list_line(e) for e in sorted(active, key=lambda x: x["daily_needed"], reverse=True)]
    lines  += [_format_list_line(e) for e in sorted(sniped, key=lambda x: x["daily_needed"], reverse=True)]
    return lines


def _build_requirement_embed(req_data: dict) -> discord.Embed:
    value      = req_data.get("value", _monthly_requirement)
    changed_at = req_data.get("changed_at")
    changed_by = req_data.get("changed_by")

    embed = discord.Embed(
        title="Monthly Fan Requirement",
        description=f"**{value:,}** fans per member",
        colour=discord.Colour.blurple(),
    )
    if changed_at:
        try:
            ts = int(datetime.fromisoformat(changed_at).timestamp())
            embed.add_field(name="Last changed", value=f"<t:{ts}:F>\n<t:{ts}:R>", inline=True)
        except Exception:
            pass
    if changed_by:
        embed.add_field(name="Changed by", value=f"<@{changed_by}>", inline=True)
    if not changed_at and not changed_by:
        embed.set_footer(text="Default value — never manually changed")
    return embed


def _build_watchlist_embed(entries: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title=f"Dia's Watchlist {STATUS_EMOJIS['far_behind']}",
        colour=STATUS_COLORS["behind"],
    )
    lines = _build_list_lines(entries)
    embed.description = "\n".join(lines) if lines else "*Nobody here — great work!*"
    return embed


def _build_hitlist_embed(entries: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title=f"Dia's Hitlist {STATUS_EMOJIS['hitlist']}",
        colour=STATUS_COLORS["far_behind"],
    )
    lines = _build_list_lines(entries)
    embed.description = "\n".join(lines) if lines else "*Nobody here — great work!*"
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
    _cutoff = (
        datetime.fromisoformat(_current_ts.replace("Z", "+00:00")) - timedelta(hours=24)
        if _current_ts else None
    )
    members = [
        m for m in _raw
        if _cutoff is not None
        and m.get("last_updated")
        and datetime.fromisoformat(m["last_updated"].replace("Z", "+00:00")) >= _cutoff
    ]

    if snapshots is None:
        async with aiosqlite.connect(LOCAL_DB) as conn:
            snapshots = await _load_snapshots(conn)

    embed = _build_report_embed(members, snapshots)

    # Compute watchlist/hitlist and append any list-change section
    wl_entries, hl_entries = _classify_lists(members)

    async with aiosqlite.connect(LOCAL_DB) as conn:
        prev_wl_json = await _get(conn, "circle_prev_watchlist")
        prev_hl_json = await _get(conn, "circle_prev_hitlist")

    prev_wl: list[dict] = json.loads(prev_wl_json) if prev_wl_json else []
    prev_hl: list[dict] = json.loads(prev_hl_json) if prev_hl_json else []

    prev_wl_ids   = {int(e["viewer_id"]) for e in prev_wl}
    prev_hl_ids   = {int(e["viewer_id"]) for e in prev_hl}
    prev_wl_names = {int(e["viewer_id"]): e["trainer_name"] for e in prev_wl}
    prev_hl_names = {int(e["viewer_id"]): e["trainer_name"] for e in prev_hl}
    curr_wl_ids   = {int(e["viewer_id"]) for e in wl_entries if e["viewer_id"] is not None}
    curr_hl_ids   = {int(e["viewer_id"]) for e in hl_entries if e["viewer_id"] is not None}
    curr_wl_names = {int(e["viewer_id"]): e["trainer_name"] for e in wl_entries if e["viewer_id"] is not None}
    curr_hl_names = {int(e["viewer_id"]): e["trainer_name"] for e in hl_entries if e["viewer_id"] is not None}

    change_lines: list[str] = []
    for vid in sorted(curr_hl_ids - prev_hl_ids, key=lambda v: curr_hl_names.get(v, "")):
        change_lines.append(f"{STATUS_EMOJIS['hitlist']} **{curr_hl_names[vid]}** added to Hitlist")
    for vid in sorted(prev_hl_ids - curr_hl_ids, key=lambda v: prev_hl_names.get(v, "")):
        change_lines.append(f"➖ **{prev_hl_names[vid]}** removed from Hitlist")
    for vid in sorted(curr_wl_ids - prev_wl_ids, key=lambda v: curr_wl_names.get(v, "")):
        change_lines.append(f"{STATUS_EMOJIS['far_behind']} **{curr_wl_names[vid]}** added to Watchlist")
    for vid in sorted(prev_wl_ids - curr_wl_ids, key=lambda v: prev_wl_names.get(v, "")):
        change_lines.append(f"➖ **{prev_wl_names[vid]}** removed from Watchlist")

    if change_lines:
        SEP = "\u2500" * 28
        _add_group_field(embed, "List Changes (last 24h)", [SEP] + change_lines)

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

    # Persist current list state so tomorrow's report can diff against it
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _set(conn, "circle_prev_watchlist", json.dumps(
            [{"viewer_id": e["viewer_id"], "trainer_name": e["trainer_name"]} for e in wl_entries]
        ))
        await _set(conn, "circle_prev_hitlist", json.dumps(
            [{"viewer_id": e["viewer_id"], "trainer_name": e["trainer_name"]} for e in hl_entries]
        ))
        await conn.commit()

    logger.info(f"[Circles] Daily report sent to {len(user_ids)} owner(s)")


async def _fix_message_order(channel: discord.TextChannel, conn) -> None:
    """Clean up and reorder tracked circle messages.

    1. Scans channel history and deletes any bot message that is not tracked in
       the DB (orphans left by crashes or internet drops).
    2. If the remaining tracked messages are out of order (IDs not ascending in
       the expected header → members → watchlist → hitlist sequence), deletes
       them all and clears the DB so the caller can repost them fresh.
    """
    # Collect tracked message IDs in expected display order
    order_keys = ["circle_header_msg"]
    for i in range(20):
        mid = await _get(conn, f"circle_members_msg_{i}")
        if mid is None:
            break
        order_keys.append(f"circle_members_msg_{i}")
    order_keys += ["circle_watchlist_msg", "circle_hitlist_msg", "circle_req_msg"]

    known: list[tuple[str, str]] = []
    for key in order_keys:
        val = await _get(conn, key)
        if val:
            known.append((key, val))

    tracked_ids = {int(v) for _, v in known}

    # Delete orphaned bot messages (not in tracked set)
    try:
        async for msg in channel.history(limit=200):
            if msg.author.id == bot.user.id and msg.id not in tracked_ids:
                try:
                    await msg.delete()
                    logger.info(f"[Circles] Deleted orphaned bot message {msg.id}")
                except discord.NotFound:
                    pass
    except Exception as exc:
        logger.warning(f"[Circles] History scan failed: {exc}")

    # Check ordering of tracked messages — delete all if out of sequence
    ids = [int(v) for _, v in known]
    if ids == sorted(ids):
        return  # All in order — nothing to do

    logger.info("[Circles] Messages out of order — deleting all tracked messages for full repost")
    for key, mid in known:
        try:
            await channel.get_partial_message(int(mid)).delete()
        except discord.NotFound:
            pass
        await conn.execute("DELETE FROM circle_messages WHERE key=?", (key,))


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

        await _fix_message_order(channel, conn)

        header_id = await _send_or_edit_embed(
            channel, conn, "circle_header_msg", _build_club_embed(api_data)
        )
        await _set(conn, "circle_header_msg", header_id)

        _cutoff = (
            datetime.fromisoformat(last_updated.replace("Z", "+00:00")) - timedelta(hours=24)
            if last_updated else None
        )
        current_members = [
            m for m in _raw_members
            if _cutoff is not None
            and m.get("last_updated")
            and datetime.fromisoformat(m["last_updated"].replace("Z", "+00:00")) >= _cutoff
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

        # Build watchlist/hitlist, carrying forward sniped (kicked) members.
        prev_wl_json = await _get(conn, "circle_cur_watchlist")
        prev_hl_json = await _get(conn, "circle_cur_hitlist")
        prev_wl_all: list[dict] = json.loads(prev_wl_json) if prev_wl_json else []
        prev_hl_all: list[dict] = json.loads(prev_hl_json) if prev_hl_json else []

        # viewer_ids present in the 24h-filtered member list.
        # Members absent here were either kicked or haven't updated in >24h — both
        # are treated the same way (the 24h rule is the club's membership boundary).
        all_member_ids = {int(m["viewer_id"]) for m in current_members if m.get("viewer_id")}

        def _carry_sniped(prev_all: list[dict]) -> list[dict]:
            """Keep entries from the previous list whose member left the circle."""
            return [
                {**e, "sniped": True}
                for e in prev_all
                if int(e["viewer_id"]) not in all_member_ids
            ]

        wl_entries, hl_entries = _classify_lists(current_members)
        full_wl = wl_entries + _carry_sniped(prev_wl_all)
        full_hl = hl_entries + _carry_sniped(prev_hl_all)

        # If new member batch messages were posted, the existing list messages are now
        # out of order — delete them so _send_or_edit_embed re-posts them at the end.
        if len(new_ids) > len(old_ids):
            for key in ("circle_watchlist_msg", "circle_hitlist_msg"):
                mid = await _get(conn, key)
                if mid:
                    try:
                        await channel.get_partial_message(int(mid)).delete()
                    except discord.NotFound:
                        pass
                    await conn.execute("DELETE FROM circle_messages WHERE key=?", (key,))
        wl_id = await _send_or_edit_embed(
            channel, conn, "circle_watchlist_msg", _build_watchlist_embed(full_wl)
        )
        hl_id = await _send_or_edit_embed(
            channel, conn, "circle_hitlist_msg", _build_hitlist_embed(full_hl)
        )
        await _set(conn, "circle_watchlist_msg", wl_id)
        await _set(conn, "circle_hitlist_msg", hl_id)

        req_raw = await _get(conn, "monthly_requirement")
        req_data: dict = json.loads(req_raw) if req_raw else {"value": _monthly_requirement}
        req_id = await _send_or_edit_embed(
            channel, conn, "circle_req_msg", _build_requirement_embed(req_data)
        )
        await _set(conn, "circle_req_msg", req_id)

        # Persist full list state (active + sniped) for next update's snipe detection
        def _serialise_list(entries: list[dict]) -> str:
            return json.dumps([
                {
                    "viewer_id":    e["viewer_id"],
                    "trainer_name": e["trainer_name"],
                    "daily_needed": e["daily_needed"],
                    "gains3":       e["gains3"],
                    "sniped":       e.get("sniped", False),
                }
                for e in entries
            ])
        await _set(conn, "circle_cur_watchlist", _serialise_list(full_wl))
        await _set(conn, "circle_cur_hitlist",   _serialise_list(full_hl))

        if save_snapshots:
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
# Background loops + startup
# ---------------------------------------------------------------------------

async def _circle_hourly_loop() -> None:
    """Refresh the circle channel embeds every 20 minutes at XX:15, XX:35, XX:55."""
    while True:
        now      = datetime.now(timezone.utc)
        next_run = None
        for minute in (15, 35, 55):
            candidate = now.replace(minute=minute, second=0, microsecond=0)
            if candidate > now:
                next_run = candidate
                break
        if next_run is None:
            next_run = (now + timedelta(hours=1)).replace(minute=15, second=0, microsecond=0)

        await asyncio.sleep((next_run - now).total_seconds())

        try:
            await post_or_edit()
        except Exception as exc:
            logger.error(f"[Circles] Refresh error: {exc}")


async def _circle_update_loop() -> None:
    """Send the daily fan report once per day at UPDATE_HOUR_UTC."""
    while True:
        now          = datetime.now(timezone.utc)
        today_update = now.replace(hour=UPDATE_HOUR_UTC, minute=0, second=0, microsecond=0)
        next_run     = today_update if now < today_update else today_update + timedelta(days=1)

        wait_secs = (next_run - now).total_seconds()
        logger.info(
            f"[Circles] Next daily report in {wait_secs / 3600:.1f}h "
            f"at {next_run.strftime('%Y-%m-%d %H:%M UTC')}"
        )
        await asyncio.sleep(wait_secs)

        try:
            today = datetime.now(timezone.utc).date().isoformat()
            async with aiosqlite.connect(LOCAL_DB) as conn:
                old_snapshots = await _load_snapshots(conn)
                last_report_date = await _get(conn, "last_report_date")

            if last_report_date == today:
                logger.info("[Circles] Daily report already sent today — skipping")
                continue

            await post_or_edit(force=True, save_snapshots=True)
            shaming = await get_shaming_enabled()
            await send_daily_report(
                list(OWNER_USER_IDS),
                snapshots=old_snapshots,
                post_channel_id=GENERAL_CHANNEL_ID if shaming else None,
            )

            async with aiosqlite.connect(LOCAL_DB) as conn:
                await _set(conn, "last_report_date", today)
                await conn.commit()
        except Exception as exc:
            logger.error(f"[Circles] Loop error: {exc}")


async def start_background_task() -> None:
    await init_db()
    await post_or_edit(force=True)
    asyncio.create_task(_circle_hourly_loop())
    asyncio.create_task(_circle_update_loop())
    logger.info("[Circles] Background tasks started")
