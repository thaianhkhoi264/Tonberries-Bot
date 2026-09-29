"""
training_module.py

Bot-side state and Discord output for HorseACT training-event payloads
(training_start / training_end from horseact_network_probe, delivered via
api_server.py). See Z:\\Claude Projects\\HorseACT RE\\api_contract_plan.md for
the full payload contract and the reasoning behind these design choices.

Per-user state, persisted in SQLite (LOCAL_DB) so an in-progress Independent
Training gap (50 minutes) survives a bot restart — same shape as
autotrain_module.py's timer persistence:
  1. training_start saves {mode, cardId, scenarioId, ends_at} for that user_id.
  2. Independent mode schedules a 50-minute "safe to log in" notification;
     manual mode just waits for training_end.
  3. training_end always carries the full run shape (rank/stats/skills *and*
     factors *and* supportCards) regardless of mode — `mode` only decides
     which channel/embed style to use, resolved as: the saved state (if the
     bot saw this run's training_start) > the payload's own `mode` (if
     present — it's optional) > DM the user (see below). Builds the result
     embed and clears the saved state.

Three Discord channels:
  - TRAINING_DASHBOARD_CHANNEL_ID     — one live status message per user
  - TRAINING_MANUAL_CHANNEL_ID        — finished manual-training results
  - TRAINING_INDEPENDENT_CHANNEL_ID   — finished independent-training results

Ambiguous training_end (no persisted training_start state *and* no `mode` on
the payload itself — e.g. the run was started on a device that isn't running
horseact_network_probe, or the plugin's own start-hook coverage missed this
scenario class): DMs the mapped user to ask manual / independent / cancel.
The run data itself is already complete and doesn't need to wait on this
reply — only the embed-building step is held pending; `cancel` means "post
nothing." Reply is plain text, matching autotrain_module.py's DM-command
style. No timeout — single-user bot, the pending confirmation just waits. A
fresh training_start OR another ambiguous training_end for the same user
cancels (deletes) whatever confirmation is still pending, since it's stale.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord

from bot import bot
from global_config import (
    LOCAL_DB,
    TRAINING_DASHBOARD_CHANNEL_ID,
    TRAINING_MANUAL_CHANNEL_ID,
    TRAINING_INDEPENDENT_CHANNEL_ID,
)
import skills_module
import training_decode as decode
import training_support_cards

logger = logging.getLogger("training_module")

INDEPENDENT_DURATION = timedelta(minutes=50)
DUPLICATE_WINDOW = timedelta(minutes=10)

# Active asyncio notification-timer tasks, keyed by user_id — lets us
# cancel/replace on a re-triggered training_start.
_active_timers: dict[int, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS active_training (
                user_id                INTEGER PRIMARY KEY,
                mode                   TEXT    NOT NULL,
                card_id                INTEGER NOT NULL,
                scenario_id            INTEGER,
                started_at             TEXT    NOT NULL,
                ends_at                TEXT,
                dashboard_msg_id       INTEGER,
                support_card_ids       TEXT,
                friend_support_card_id INTEGER,
                ready_notification_msg_id INTEGER
            )
        """)
        # Migrate pre-existing installs (CREATE TABLE IF NOT EXISTS above is a
        # no-op once the table already exists, so add the new columns here).
        for column, coltype in (("support_card_ids", "TEXT"), ("friend_support_card_id", "INTEGER"),
                                 ("ready_notification_msg_id", "INTEGER")):
            try:
                await conn.execute(f"ALTER TABLE active_training ADD COLUMN {column} {coltype}")
            except Exception:
                pass  # already has the column
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS training_result_posts (
                user_id    INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                posted_at  TEXT    NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_training_confirmations (
                user_id        INTEGER PRIMARY KEY,
                payload        TEXT    NOT NULL,
                dm_channel_id  INTEGER NOT NULL,
                dm_message_id  INTEGER NOT NULL,
                created_at     TEXT    NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_header (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                message_id INTEGER NOT NULL
            )
        """)
        await conn.commit()


async def _get_active(conn, user_id: int) -> dict | None:
    async with conn.execute(
        "SELECT mode, card_id, scenario_id, started_at, ends_at, dashboard_msg_id, "
        "support_card_ids, friend_support_card_id, ready_notification_msg_id "
        "FROM active_training WHERE user_id=?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "mode": row[0], "card_id": row[1], "scenario_id": row[2],
        "started_at": row[3], "ends_at": row[4], "dashboard_msg_id": row[5],
        "support_card_ids": json.loads(row[6]) if row[6] else None,
        "friend_support_card_id": row[7],
        "ready_notification_msg_id": row[8],
    }


async def _save_active(conn, user_id: int, mode: str, card_id: int, scenario_id: int | None,
                        started_at: datetime, ends_at: datetime | None, dashboard_msg_id: int | None,
                        support_card_ids: list | None = None, friend_support_card_id: int | None = None) -> None:
    # ready_notification_msg_id always starts NULL — it's only ever set later,
    # once the independent-mode timer actually fires (see _set_ready_notification).
    await conn.execute(
        """INSERT OR REPLACE INTO active_training
           (user_id, mode, card_id, scenario_id, started_at, ends_at, dashboard_msg_id,
            support_card_ids, friend_support_card_id, ready_notification_msg_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (user_id, mode, card_id, scenario_id, started_at.isoformat(),
         ends_at.isoformat() if ends_at else None, dashboard_msg_id,
         json.dumps(support_card_ids) if support_card_ids is not None else None,
         friend_support_card_id),
    )


async def _set_ready_notification(conn, user_id: int, message_id: int) -> None:
    await conn.execute(
        "UPDATE active_training SET ready_notification_msg_id=? WHERE user_id=?",
        (message_id, user_id),
    )


async def _clear_active(conn, user_id: int) -> None:
    await conn.execute("DELETE FROM active_training WHERE user_id=?", (user_id,))


async def _get_result_post(conn, user_id: int) -> dict | None:
    async with conn.execute(
        "SELECT message_id, channel_id, posted_at FROM training_result_posts WHERE user_id=?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {"message_id": row[0], "channel_id": row[1], "posted_at": row[2]}


async def _save_result_post(conn, user_id: int, message_id: int, channel_id: int, posted_at: datetime) -> None:
    await conn.execute(
        """INSERT OR REPLACE INTO training_result_posts (user_id, message_id, channel_id, posted_at)
           VALUES (?, ?, ?, ?)""",
        (user_id, message_id, channel_id, posted_at.isoformat()),
    )


async def _get_pending_confirmation(conn, user_id: int) -> dict | None:
    async with conn.execute(
        "SELECT payload, dm_channel_id, dm_message_id FROM pending_training_confirmations WHERE user_id=?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {"payload": json.loads(row[0]), "dm_channel_id": row[1], "dm_message_id": row[2]}


async def _save_pending_confirmation(conn, user_id: int, payload: dict, dm_channel_id: int, dm_message_id: int) -> None:
    await conn.execute(
        """INSERT OR REPLACE INTO pending_training_confirmations
           (user_id, payload, dm_channel_id, dm_message_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, json.dumps(payload), dm_channel_id, dm_message_id, datetime.now(timezone.utc).isoformat()),
    )


async def _clear_pending_confirmation(conn, user_id: int) -> None:
    await conn.execute("DELETE FROM pending_training_confirmations WHERE user_id=?", (user_id,))


async def _get_dashboard_header(conn) -> int | None:
    async with conn.execute("SELECT message_id FROM dashboard_header WHERE id=1") as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def _save_dashboard_header(conn, message_id: int) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO dashboard_header (id, message_id) VALUES (1, ?)", (message_id,)
    )


_DASHBOARD_HEADER_TEXT = (
    "# Training Dashboard\n\n"
    "This channel shows live training status. When a run starts, a card appears here with "
    "the character and current progress. Once the run finishes, the card is removed and the "
    "result gets posted to the manual or independent results channel.\n\n"
    "For Independent Training, you also get a ping here once the 50 minute timer ends."
)


async def ensure_dashboard_header() -> None:
    """Posts (and pins) a one time explainer at the top of the dashboard channel, as a
    plain markdown message rather than an embed. Safe to call on every startup: skips
    reposting if it's already there."""
    channel = bot.get_channel(TRAINING_DASHBOARD_CHANNEL_ID)
    if channel is None:
        logger.warning("[Training] Dashboard channel not found/configured, skipping header")
        return

    async with aiosqlite.connect(LOCAL_DB) as conn:
        existing_id = await _get_dashboard_header(conn)

    if existing_id:
        try:
            await channel.fetch_message(existing_id)
            return  # already posted and still there
        except discord.NotFound:
            pass  # was deleted — fall through and repost
        except Exception as exc:
            logger.error(f"[Training] Failed to check dashboard header: {exc}")
            return

    try:
        msg = await channel.send(_DASHBOARD_HEADER_TEXT)
    except Exception as exc:
        logger.error(f"[Training] Failed to post dashboard header: {exc}")
        return

    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _save_dashboard_header(conn, msg.id)
        await conn.commit()

    try:
        await msg.pin()
    except Exception as exc:
        logger.warning(f"[Training] Could not pin dashboard header (missing permission?): {exc}")

    logger.info("[Training] Posted dashboard header")


async def clean_dashboard_channel() -> None:
    """Deletes anything in the dashboard channel that isn't currently tracked: the
    header, or a live status/ready-ping message from an in-progress training. Catches
    non-bot messages and orphaned bot messages left over from a crash or a bug alike,
    since both are just "not in keep_ids" from this function's point of view. Runs on
    every startup, before the API server starts accepting new events."""
    channel = bot.get_channel(TRAINING_DASHBOARD_CHANNEL_ID)
    if channel is None:
        return

    async with aiosqlite.connect(LOCAL_DB) as conn:
        header_id = await _get_dashboard_header(conn)
        async with conn.execute(
            "SELECT dashboard_msg_id, ready_notification_msg_id FROM active_training"
        ) as cur:
            rows = await cur.fetchall()

    keep_ids = {header_id} if header_id else set()
    for dashboard_msg_id, ready_notification_msg_id in rows:
        if dashboard_msg_id:
            keep_ids.add(dashboard_msg_id)
        if ready_notification_msg_id:
            keep_ids.add(ready_notification_msg_id)

    deleted = 0
    async for msg in channel.history(limit=None):
        if msg.id in keep_ids:
            continue
        try:
            await msg.delete()
            deleted += 1
        except Exception as exc:
            logger.error(f"[Training] Failed to delete stray dashboard message {msg.id}: {exc}")

    if deleted:
        logger.info(f"[Training] Cleaned {deleted} stray message(s) from the dashboard channel")


# ---------------------------------------------------------------------------
# Thumbnail — trained-character portrait, shared by the dashboard and result embeds.
# Filename lookup (for the embed's attachment:// URL) and the actual discord.File
# (for the upload itself) are kept separate: a discord.File wraps an open file
# handle, so a fresh one is created right before each individual send/edit call
# rather than reused across a fallback path (e.g. edit-fails-then-send-new).
# ---------------------------------------------------------------------------

def _thumbnail_filename(card_id: int) -> str | None:
    path = decode.character_icon_path(card_id)
    return os.path.basename(path) if path else None


def _thumbnail_file(card_id: int) -> discord.File | None:
    path = decode.character_icon_path(card_id)
    if not path:
        return None
    return discord.File(path, filename=os.path.basename(path))


# Support-card strip — training_end's `image` (bottom, full-width) slot. Same
# filename-vs-File split as the thumbnail above, and for the same reason.
_SUPPORT_STRIP_FILENAME = "support_cards.png"


def _support_strip_file(data: dict) -> discord.File | None:
    support_cards = data.get("supportCards")
    if not support_cards:
        return None
    buf = training_support_cards.build_support_card_strip(support_cards)
    if buf is None:
        return None
    return discord.File(buf, filename=_SUPPORT_STRIP_FILENAME)


def _dashboard_strip_file(support_card_ids: list | None, friend_support_card_id: int | None) -> discord.File | None:
    """training_start version — no limitBreakCount yet, so no LB stamps (see training_support_cards.py)."""
    buf = training_support_cards.build_support_card_ids_strip(support_card_ids, friend_support_card_id)
    if buf is None:
        return None
    return discord.File(buf, filename=_SUPPORT_STRIP_FILENAME)


# ---------------------------------------------------------------------------
# Dashboard channel — one live status message per user
# ---------------------------------------------------------------------------

def _dashboard_embed(mode: str, card_id: int, ends_at: datetime | None, ready: bool,
                      support_card_ids: list | None = None, friend_support_card_id: int | None = None) -> discord.Embed:
    char = decode.character_display(card_id)
    title = char["name"] + (f" {char['outfit']}" if char.get("outfit") else "")

    if ready:
        status = "Ready to collect! Log in to finish the run. <a:diapat:1508665594013286400>"
        colour = discord.Colour.gold()
    elif mode == "independent" and ends_at:
        status = f"Independent Training ongoing — ready <t:{int(ends_at.timestamp())}:R>"
        colour = discord.Colour.blurple()
    else:
        status = "Manual Training ongoing — waiting for it to end"
        colour = discord.Colour.blurple()

    embed = discord.Embed(title=title, description=status, colour=colour)
    filename = _thumbnail_filename(card_id)
    if filename:
        embed.set_thumbnail(url=f"attachment://{filename}")
    if support_card_ids or friend_support_card_id is not None:
        embed.set_image(url=f"attachment://{_SUPPORT_STRIP_FILENAME}")
    return embed


async def _update_dashboard(user_id: int, mode: str, card_id: int, ends_at: datetime | None = None,
                             ready: bool = False, support_card_ids: list | None = None,
                             friend_support_card_id: int | None = None) -> int | None:
    """Post or edit this user's dashboard message. Returns the message id."""
    channel = bot.get_channel(TRAINING_DASHBOARD_CHANNEL_ID)
    if channel is None:
        logger.warning("[Training] Dashboard channel not found/configured")
        return None

    embed = _dashboard_embed(mode, card_id, ends_at, ready, support_card_ids, friend_support_card_id)

    async with aiosqlite.connect(LOCAL_DB) as conn:
        existing = await _get_active(conn, user_id)
    msg_id = existing["dashboard_msg_id"] if existing else None

    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            files = [f for f in (_thumbnail_file(card_id),
                                  _dashboard_strip_file(support_card_ids, friend_support_card_id)) if f is not None]
            await msg.edit(embed=embed, attachments=files)
            return msg_id
        except discord.NotFound:
            pass  # fall through and post a new one
        except Exception as exc:
            logger.error(f"[Training] Failed to edit dashboard message for {user_id}: {exc}")

    try:
        files = [f for f in (_thumbnail_file(card_id),
                              _dashboard_strip_file(support_card_ids, friend_support_card_id)) if f is not None]
        msg = await channel.send(embed=embed, files=files) if files else await channel.send(embed=embed)
        return msg.id
    except Exception as exc:
        logger.error(f"[Training] Failed to send dashboard message for {user_id}: {exc}")
        return None


async def _clear_dashboard(user_id: int) -> None:
    """Deletes the status message and, if the independent-mode ready-ping fired, that too."""
    async with aiosqlite.connect(LOCAL_DB) as conn:
        existing = await _get_active(conn, user_id)
    if not existing:
        return
    channel = bot.get_channel(TRAINING_DASHBOARD_CHANNEL_ID)
    if channel is None:
        return
    for msg_id in (existing["dashboard_msg_id"], existing["ready_notification_msg_id"]):
        if not msg_id:
            continue
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.delete()
        except Exception:
            pass  # already gone — fine


# ---------------------------------------------------------------------------
# Independent-mode "safe to log in" timer
# ---------------------------------------------------------------------------

async def _run_notification_timer(user_id: int, mode: str, card_id: int, ends_at: datetime) -> None:
    now = datetime.now(timezone.utc)
    wait = (ends_at - now).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)

    _active_timers.pop(user_id, None)

    # Only fire if this training is still the one we scheduled for — a
    # training_end may have already cleared it (early finish / restart race).
    async with aiosqlite.connect(LOCAL_DB) as conn:
        row = await _get_active(conn, user_id)
    if row is None or row["mode"] != "independent":
        return

    await _update_dashboard(user_id, mode, card_id, ends_at=ends_at, ready=True,
                             support_card_ids=row["support_card_ids"],
                             friend_support_card_id=row["friend_support_card_id"])

    # Separate pinging message, distinct from the status embed above — deleted
    # alongside it (see _clear_dashboard) once the run actually finishes.
    channel = bot.get_channel(TRAINING_DASHBOARD_CHANNEL_ID)
    if channel is not None:
        try:
            ping_msg = await channel.send(
                f"<@{user_id}> Independent Training has ended — log in to collect the result!"
            )
            async with aiosqlite.connect(LOCAL_DB) as conn:
                await _set_ready_notification(conn, user_id, ping_msg.id)
                await conn.commit()
        except Exception as exc:
            logger.error(f"[Training] Failed to send ready-ping for user {user_id}: {exc}")

    logger.info(f"[Training] Ready-to-collect notification fired for user {user_id}")


def _schedule_notification(user_id: int, mode: str, card_id: int, ends_at: datetime) -> None:
    old = _active_timers.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    _active_timers[user_id] = asyncio.create_task(
        _run_notification_timer(user_id, mode, card_id, ends_at)
    )


def _cancel_timer(user_id: int) -> None:
    old = _active_timers.pop(user_id, None)
    if old and not old.done():
        old.cancel()


async def restore_timers() -> None:
    """Reschedule all pending independent-mode timers on bot startup.

    If a timer already expired while the bot was down, it fires immediately
    (edits the dashboard to "ready") instead of sleeping.
    """
    async with aiosqlite.connect(LOCAL_DB) as conn:
        async with conn.execute(
            "SELECT user_id, mode, card_id, ends_at FROM active_training "
            "WHERE mode='independent' AND ends_at IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()

    count = 0
    for user_id, mode, card_id, ends_at_str in rows:
        ends_at = datetime.fromisoformat(ends_at_str)
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        _schedule_notification(user_id, mode, card_id, ends_at)
        count += 1
    logger.info(f"[Training] Restored {count} pending training timer(s)")


# ---------------------------------------------------------------------------
# Result embeds
#
# Both training_end shapes also carry a `supportCards` array (6 deck slots —
# 5 regular + 1 friend at position 6 — each with the exp/limitBreakCount the
# slot ended the run with). Rendered as the embed's `image` (bottom, full-width
# slot) via training_support_cards.build_support_card_strip — see _post_result.
# ---------------------------------------------------------------------------

_FIELD_VALUE_LIMIT = 1024  # Discord's hard cap on a single embed field's value


def _add_chunked_field(embed: discord.Embed, name: str, lines: list[str], inline: bool = False) -> None:
    """Adds `lines` as one field, splitting into multiple numbered fields of the
    same name if the joined value would exceed Discord's 1024-char field value
    limit. A character with a lot of skills/sparks, each rendered with a custom
    emoji (long raw markup — 45+ characters per star, for example), can blow
    past that limit easily; this is what a real run actually hit in production
    (500 error, "embeds.0.fields.3.value: Must be 1024 or fewer in length")."""
    if not lines:
        return

    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(line) > _FIELD_VALUE_LIMIT:
            line = line[:_FIELD_VALUE_LIMIT - 1] + "…"  # one absurdly long single entry — truncate it alone
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > _FIELD_VALUE_LIMIT:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        field_name = name if len(chunks) == 1 else f"{name} ({i + 1}/{len(chunks)})"
        embed.add_field(name=field_name, value=chunk, inline=inline)


def _add_rank_fans_stats(embed: discord.Embed, data: dict) -> None:
    """Rank/Fans/Stats fields — shared by both result embeds (manual has always
    had this data; independent's payload carries the same fields too)."""
    rank = data.get("rank")
    rank_score = data.get("rankScore")
    rank_emoji = decode.status_rank_emoji(rank) if isinstance(rank, int) else None
    rank_display = rank_emoji or str(rank if rank is not None else "?")
    rank_value = f"{rank_display} {rank_score:,} pts" if isinstance(rank_score, int) else rank_display

    embed.add_field(name="Rank", value=rank_value)
    embed.add_field(name="Fans", value=f"{data.get('fans', 0):,}")

    stats = data.get("stats") or {}
    if stats:
        stat_line = " / ".join(f"{decode.stat_emoji(k) or k.title()} {v}" for k, v in stats.items())
        embed.add_field(name="Stats", value=stat_line, inline=False)


def _build_manual_embed(card_id: int, data: dict) -> discord.Embed:
    char = decode.character_display(card_id)
    title = char["name"] + (f" {char['outfit']}" if char.get("outfit") else "")

    embed = discord.Embed(title=title, colour=discord.Colour.green())
    filename = _thumbnail_filename(card_id)
    if filename:
        embed.set_thumbnail(url=f"attachment://{filename}")
    if data.get("supportCards"):
        embed.set_image(url=f"attachment://{_SUPPORT_STRIP_FILENAME}")

    _add_rank_fans_stats(embed, data)

    skills = data.get("skills") or []
    if skills:
        lines = []
        for s in skills:
            info = decode.skill_display(s["skillId"])
            emoji = skills_module.skill_icon_emoji_for_id(s["skillId"])
            level = s.get("level", 1)
            line = f"{emoji} {info['name']}"
            if level != 1:  # level 1 is the default/base state — not worth calling out
                line += f" Lv. {level}"
            lines.append(line)
        _add_chunked_field(embed, "Skills", lines)

    return embed


# Spark `type` (see training_decode.py / the plan doc's factor-type table) ->
# in-game display priority. 0=blue (stat), 1=pink (aptitude), 5=green (unique
# skill); 2/3/4 (white: race win/skill/scenario) and unresolved factors all
# fall through to the same "everything else" bucket via .get()'s default.
_SPARK_TYPE_PRIORITY = {0: 0, 1: 1, 5: 2}


def _build_independent_embed(card_id: int, data: dict) -> discord.Embed:
    char = decode.character_display(card_id)
    title = char["name"] + (f" {char['outfit']}" if char.get("outfit") else "")

    embed = discord.Embed(title=title, colour=discord.Colour.purple())
    filename = _thumbnail_filename(card_id)
    if filename:
        embed.set_thumbnail(url=f"attachment://{filename}")
    if data.get("supportCards"):
        embed.set_image(url=f"attachment://{_SUPPORT_STRIP_FILENAME}")

    _add_rank_fans_stats(embed, data)

    factors = data.get("factors") or []
    if factors:
        # In-game ordering: blue (stat) > pink (aptitude) > green (character-unique
        # skill) sparks first, then everything else (white: race win/skill/scenario)
        # after, in whatever order the payload had them. Stable sort on original
        # index handles the "everything else keeps its order" part for free.
        entries = [(decode.factor_display(f["factorId"]), i) for i, f in enumerate(factors)]
        entries.sort(key=lambda pair: (_SPARK_TYPE_PRIORITY.get(pair[0]["type"], 3), pair[1]))

        lines = []
        prev_is_priority = None
        for info, _ in entries:
            is_priority = info["type"] in _SPARK_TYPE_PRIORITY
            if prev_is_priority and not is_priority:
                lines.append("")  # gap between the blue/pink/green sparks and everything else
            lines.append(f"{info['name']} {decode.spark_star_emojis(info['level'])}")
            prev_is_priority = is_priority
        _add_chunked_field(embed, "Sparks", lines)

    return embed


# ---------------------------------------------------------------------------
# Duplicate-result handling
# ---------------------------------------------------------------------------

async def _post_result(user_id: int, channel_id: int, embed: discord.Embed, card_id: int, data: dict) -> None:
    """Post the result embed, replacing a same-user post from the last 10 minutes."""
    channel = bot.get_channel(channel_id)
    if channel is None:
        logger.warning(f"[Training] Results channel {channel_id} not found/configured")
        return

    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(LOCAL_DB) as conn:
        existing = await _get_result_post(conn, user_id)

    if existing:
        posted_at = datetime.fromisoformat(existing["posted_at"])
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)
        if now - posted_at < DUPLICATE_WINDOW:
            try:
                old_channel = bot.get_channel(existing["channel_id"]) or channel
                old_msg = await old_channel.fetch_message(existing["message_id"])
                await old_msg.delete()
                logger.info(f"[Training] Replaced duplicate training_end result for user {user_id}")
            except Exception:
                pass  # already gone — fine, we still post the new one

    files = [f for f in (_thumbnail_file(card_id), _support_strip_file(data)) if f is not None]
    msg = await channel.send(embed=embed, files=files) if files else await channel.send(embed=embed)
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _save_result_post(conn, user_id, msg.id, channel_id, now)
        await conn.commit()


# ---------------------------------------------------------------------------
# Ambiguous training_end — no persisted training_start state *and* no `mode`
# on the payload itself, so there's genuinely nothing to resolve it from.
# DM the user to ask which embed style this actually was.
# ---------------------------------------------------------------------------

async def _cancel_pending_confirmation(user_id: int) -> None:
    """Delete a pending confirmation DM (if any) and clear its row. Best-effort."""
    async with aiosqlite.connect(LOCAL_DB) as conn:
        pending = await _get_pending_confirmation(conn, user_id)
        if pending is None:
            return
        await _clear_pending_confirmation(conn, user_id)
        await conn.commit()

    try:
        channel = bot.get_channel(pending["dm_channel_id"]) or await bot.fetch_channel(pending["dm_channel_id"])
        msg = await channel.fetch_message(pending["dm_message_id"])
        await msg.delete()
    except Exception:
        pass  # already gone, or channel/message unreachable — fine either way


async def _request_confirmation(user_id: int, data: dict) -> None:
    """DM the user asking whether an ambiguous training_end was manual or independent."""
    await _cancel_pending_confirmation(user_id)  # supersede any confirmation already pending

    char = decode.character_display(data["cardId"])
    name = char["name"] + (f" {char['outfit']}" if char.get("outfit") else "")

    try:
        user = await bot.fetch_user(user_id)
        dm = await user.create_dm()
        msg = await dm.send(
            f"I got a training_end for **{name}** with no matching training_start on record — "
            "I can't tell if this was a manual or independent training.\n"
            "Reply `manual`, `independent`, or `cancel` here."
        )
    except Exception as exc:
        logger.error(f"[Training] Could not DM user {user_id} for confirmation: {exc}")
        return

    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _save_pending_confirmation(conn, user_id, data, dm.id, msg.id)
        await conn.commit()

    logger.info(f"[Training] Requested manual/independent confirmation from user {user_id}")


async def _finish_training_end(user_id: int, mode: str, data: dict) -> None:
    """Build the result embed, post it, and clear per-user training state."""
    card_id = data["cardId"]

    if mode == "independent":
        embed = _build_independent_embed(card_id, data)
        channel_id = TRAINING_INDEPENDENT_CHANNEL_ID
    else:
        embed = _build_manual_embed(card_id, data)
        channel_id = TRAINING_MANUAL_CHANNEL_ID

    await _post_result(user_id, channel_id, embed, card_id, data)

    _cancel_timer(user_id)
    await _clear_dashboard(user_id)
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _clear_active(conn, user_id)
        await conn.commit()

    logger.info(f"[Training] training_end for user {user_id}: mode={mode}, cardId={card_id}")


# ---------------------------------------------------------------------------
# Public handlers — called from api_server.py / main.py
# ---------------------------------------------------------------------------

async def handle_training_start(user_id: int, data: dict) -> None:
    # A fresh training_start makes any confirmation still awaiting a reply
    # for this user stale — supersede it.
    await _cancel_pending_confirmation(user_id)

    # Likewise, a leftover ready-ping from a previous run that never got a
    # training_end (so _clear_dashboard never ran) would otherwise be orphaned
    # once _save_active below overwrites its row.
    async with aiosqlite.connect(LOCAL_DB) as conn:
        stale = await _get_active(conn, user_id)
    if stale and stale["ready_notification_msg_id"]:
        channel = bot.get_channel(TRAINING_DASHBOARD_CHANNEL_ID)
        if channel is not None:
            try:
                msg = await channel.fetch_message(stale["ready_notification_msg_id"])
                await msg.delete()
            except Exception:
                pass  # already gone — fine

    mode = data["mode"]
    card_id = data["cardId"]
    scenario_id = data.get("scenarioId")
    started_at = datetime.fromtimestamp(data["timestamp"] / 1000, tz=timezone.utc)

    # friendSupportCardId is omitted entirely (not null) on runs with no
    # friend support card, hence .get() rather than an index.
    support_card_ids = data.get("supportCardIds")
    friend_support_card_id = data.get("friendSupportCardId")

    ends_at = started_at + INDEPENDENT_DURATION if mode == "independent" else None

    dashboard_msg_id = await _update_dashboard(user_id, mode, card_id, ends_at=ends_at,
                                                support_card_ids=support_card_ids,
                                                friend_support_card_id=friend_support_card_id)

    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _save_active(conn, user_id, mode, card_id, scenario_id, started_at, ends_at, dashboard_msg_id,
                            support_card_ids, friend_support_card_id)
        await conn.commit()

    if mode == "independent":
        _schedule_notification(user_id, mode, card_id, ends_at)
    else:
        _cancel_timer(user_id)

    logger.info(f"[Training] training_start for user {user_id}: mode={mode}, cardId={card_id}")


async def handle_training_end(user_id: int, data: dict) -> str:
    """Returns "posted" or "pending_confirmation".

    `mode` only decides which channel/embed style to use — training_end always
    carries the full run shape (rank/stats/skills *and* factors *and*
    supportCards) regardless of how it was played, so a wrong/missing mode
    resolution is cosmetic, never a data-loss bug. Resolution order:
      1. Persisted training_start state (authoritative — set at start time,
         the same moment the timer/notification decision was made).
      2. The payload's own `mode`, if present (it's optional now — the plugin
         includes it only when its own local tracking knows it).
      3. Neither: DM the user to pick manual/independent/cancel. The run data
         is already complete and doesn't need to wait on that reply — only
         the embed-building step is held pending.
    """
    async with aiosqlite.connect(LOCAL_DB) as conn:
        active = await _get_active(conn, user_id)

    if active is not None:
        mode = active["mode"]
    elif data.get("mode") in ("independent", "manual"):
        mode = data["mode"]
    else:
        await _request_confirmation(user_id, data)
        return "pending_confirmation"

    await _finish_training_end(user_id, mode, data)
    return "posted"


def _build_abandoned_embed(card_id: int | None, support_card_ids: list | None,
                            friend_support_card_id: int | None) -> discord.Embed:
    if card_id is None:
        # No cardId anywhere — never saw this run's training_start, and the
        # payload didn't have one either. Still notify, just without specifics.
        return discord.Embed(title="A training run was glued", colour=discord.Colour.orange())

    char = decode.character_display(card_id)
    name = char["name"] + (f" {char['outfit']}" if char.get("outfit") else "")
    embed = discord.Embed(title=f"The Training for {name} was glued", colour=discord.Colour.orange())
    filename = _thumbnail_filename(card_id)
    if filename:
        embed.set_thumbnail(url=f"attachment://{filename}")
    if support_card_ids or friend_support_card_id is not None:
        embed.set_image(url=f"attachment://{_SUPPORT_STRIP_FILENAME}")
    return embed


async def handle_training_abandoned(user_id: int, data: dict) -> None:
    """A manual run ended early via the game's own Abandon option — no result
    data exists for it (nothing was ever saved to the roster), so unlike
    training_end there's nothing to post to a results channel. Per instruction
    this skips the plan doc's suggested dashboard-channel notice and instead:
    deletes the ongoing status message outright, and DMs the user directly
    with an embed carrying the same thumbnail and (unstamped — there's no
    limitBreakCount data for an abandoned run) support-card strip the
    dashboard message itself was already showing.
    """
    async with aiosqlite.connect(LOCAL_DB) as conn:
        active = await _get_active(conn, user_id)

    # cardId is best-effort in the payload itself (may be absent) — fall back
    # to whatever training_start persisted, same as training_end's mode fallback.
    card_id = (active["card_id"] if active else None) or data.get("cardId")
    support_card_ids = active["support_card_ids"] if active else None
    friend_support_card_id = active["friend_support_card_id"] if active else None

    embed = _build_abandoned_embed(card_id, support_card_ids, friend_support_card_id)
    files = (
        [f for f in (_thumbnail_file(card_id),
                      _dashboard_strip_file(support_card_ids, friend_support_card_id)) if f is not None]
        if card_id is not None else []
    )

    try:
        user = await bot.fetch_user(user_id)
        dm = await user.create_dm()
        if files:
            await dm.send(embed=embed, files=files)
        else:
            await dm.send(embed=embed)
    except Exception as exc:
        logger.error(f"[Training] Failed to DM abandoned-run notice to user {user_id}: {exc}")

    _cancel_timer(user_id)
    await _clear_dashboard(user_id)
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _clear_active(conn, user_id)
        await conn.commit()

    logger.info(f"[Training] training_abandoned for user {user_id}: cardId={card_id}")


async def handle_confirmation_reply(message: discord.Message) -> bool:
    """Handle a DM reply of manual/independent/cancel to a pending confirmation.

    Returns True if this message was a reply to a pending confirmation (and
    was therefore handled), False if there was nothing pending for this user
    (caller should treat the message as an ordinary/unrecognized command).
    """
    user_id = message.author.id
    async with aiosqlite.connect(LOCAL_DB) as conn:
        pending = await _get_pending_confirmation(conn, user_id)
    if pending is None:
        return False

    cmd = message.content.strip().lower()
    if cmd == "cancel":
        await _cancel_pending_confirmation(user_id)
        await message.channel.send("Cancelled — that training_end will be discarded.")
        return True
    elif cmd in ("manual", "independent"):
        await _cancel_pending_confirmation(user_id)
        await _finish_training_end(user_id, cmd, pending["payload"])
        await message.channel.send(f"Got it — posted as a {cmd} training result.")
        return True
    else:
        await message.channel.send("Reply `manual`, `independent`, or `cancel` for the training_end I asked about.")
        return True
