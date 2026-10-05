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

Four Discord channels:
  - TRAINING_DASHBOARD_CHANNEL_ID     — one live status message per user
  - TRAINING_MANUAL_CHANNEL_ID        — finished manual-training results
  - TRAINING_INDEPENDENT_CHANNEL_ID   — finished independent-training results
  - TRAINING_ABANDONED_CHANNEL_ID     — manual runs abandoned ("glued") mid-run

All four are shared across every user/API key, so every embed posted to them
carries an "Owner" field (`<@user_id>`) to tell runs apart.

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
import re
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord

from bot import bot
from global_config import (
    LOCAL_DB,
    TRAINING_DASHBOARD_CHANNEL_ID,
    TRAINING_MANUAL_CHANNEL_ID,
    TRAINING_INDEPENDENT_CHANNEL_ID,
    TRAINING_ABANDONED_CHANNEL_ID,
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
                                 ("ready_notification_msg_id", "INTEGER"),
                                 ("single_mode_chara_id", "INTEGER"),
                                 # Last training_progress snapshot — shown on the "glued" (abandoned)
                                 # notice so it isn't just a bare character name with nothing else.
                                 ("last_turn", "INTEGER"), ("last_stats", "TEXT"),
                                 ("last_skill_point", "INTEGER"), ("last_fans", "INTEGER")):
            try:
                await conn.execute(f"ALTER TABLE active_training ADD COLUMN {column} {coltype}")
            except Exception:
                pass  # already has the column
        # Chain events accumulated turn over turn (training_progress never sends a full
        # history, only one entry per turn) — keyed by single_mode_chara_id, not user_id
        # alone, same run-separation reasoning as active_training. UNIQUE on (user_id,
        # single_mode_chara_id, turn) so a retried training_progress POST for a turn
        # already recorded can't double-count that chain event (plan doc decision #6).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chain_events (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL,
                single_mode_chara_id INTEGER NOT NULL,
                turn                INTEGER NOT NULL,
                story_id            INTEGER NOT NULL,
                support_card_id     INTEGER NOT NULL,
                seen_at             TEXT NOT NULL,
                UNIQUE(user_id, single_mode_chara_id, turn)
            )
        """)
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
        "support_card_ids, friend_support_card_id, ready_notification_msg_id, single_mode_chara_id, "
        "last_turn, last_stats, last_skill_point, last_fans "
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
        "single_mode_chara_id": row[9],
        "last_turn": row[10],
        "last_stats": json.loads(row[11]) if row[11] else None,
        "last_skill_point": row[12],
        "last_fans": row[13],
    }


async def _save_active(conn, user_id: int, mode: str, card_id: int, scenario_id: int | None,
                        started_at: datetime, ends_at: datetime | None, dashboard_msg_id: int | None,
                        support_card_ids: list | None = None, friend_support_card_id: int | None = None,
                        single_mode_chara_id: int | None = None, last_turn: int | None = None,
                        last_stats: dict | None = None, last_skill_point: int | None = None,
                        last_fans: int | None = None) -> None:
    # ready_notification_msg_id always starts NULL — it's only ever set later,
    # once the independent-mode timer actually fires (see _set_ready_notification).
    # last_turn/last_stats/last_skill_point/last_fans default to None (reset on a
    # fresh training_start, which doesn't have this data yet) — handle_training_progress
    # passes real values on every turn so a later "glued" notice has something to show.
    await conn.execute(
        """INSERT OR REPLACE INTO active_training
           (user_id, mode, card_id, scenario_id, started_at, ends_at, dashboard_msg_id,
            support_card_ids, friend_support_card_id, ready_notification_msg_id, single_mode_chara_id,
            last_turn, last_stats, last_skill_point, last_fans)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
        (user_id, mode, card_id, scenario_id, started_at.isoformat(),
         ends_at.isoformat() if ends_at else None, dashboard_msg_id,
         json.dumps(support_card_ids) if support_card_ids is not None else None,
         friend_support_card_id, single_mode_chara_id, last_turn,
         json.dumps(last_stats) if last_stats is not None else None,
         last_skill_point, last_fans),
    )


async def _set_single_mode_chara_id(conn, user_id: int, single_mode_chara_id: int) -> None:
    await conn.execute(
        "UPDATE active_training SET single_mode_chara_id=? WHERE user_id=?",
        (single_mode_chara_id, user_id),
    )


async def _add_chain_event(conn, user_id: int, single_mode_chara_id: int, turn: int,
                            story_id: int, support_card_id: int) -> None:
    # INSERT OR IGNORE: the UNIQUE(user_id, single_mode_chara_id, turn) constraint
    # means a retried training_progress POST for a turn already recorded is a no-op
    # here, not a duplicate row (plan doc decision #6).
    await conn.execute(
        """INSERT OR IGNORE INTO chain_events
           (user_id, single_mode_chara_id, turn, story_id, support_card_id, seen_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, single_mode_chara_id, turn, story_id, support_card_id,
         datetime.now(timezone.utc).isoformat()),
    )


async def _get_chain_events(conn, user_id: int, single_mode_chara_id: int) -> list[dict]:
    async with conn.execute(
        "SELECT turn, story_id, support_card_id FROM chain_events "
        "WHERE user_id=? AND single_mode_chara_id=? ORDER BY turn",
        (user_id, single_mode_chara_id),
    ) as cur:
        rows = await cur.fetchall()
    return [{"turn": r[0], "story_id": r[1], "support_card_id": r[2]} for r in rows]


async def _clear_chain_events(conn, user_id: int) -> None:
    await conn.execute("DELETE FROM chain_events WHERE user_id=?", (user_id,))


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

def _dashboard_embed(user_id: int, mode: str, card_id: int, ends_at: datetime | None, ready: bool,
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
    embed.add_field(name="Owner", value=f"<@{user_id}>")
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

    embed = _dashboard_embed(user_id, mode, card_id, ends_at, ready, support_card_ids, friend_support_card_id)

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
        # Only the first chunk keeps the real title — later ones use a zero-width
        # space (Discord requires a non-empty field name, but this renders blank),
        # so a multi-chunk field reads as one seamless block instead of repeating
        # "Name (i/N)" headers down the embed.
        field_name = name if i == 0 else "​"
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


def _build_manual_embed(user_id: int, card_id: int, data: dict) -> discord.Embed:
    char = decode.character_display(card_id)
    title = char["name"] + (f" {char['outfit']}" if char.get("outfit") else "")

    embed = discord.Embed(title=title, colour=discord.Colour.green())
    embed.add_field(name="Owner", value=f"<@{user_id}>")
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


def _build_independent_embed(user_id: int, card_id: int, data: dict) -> discord.Embed:
    char = decode.character_display(card_id)
    title = char["name"] + (f" {char['outfit']}" if char.get("outfit") else "")

    embed = discord.Embed(title=title, colour=discord.Colour.purple())
    embed.add_field(name="Owner", value=f"<@{user_id}>")
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
        embed = _build_independent_embed(user_id, card_id, data)
        channel_id = TRAINING_INDEPENDENT_CHANNEL_ID
    else:
        embed = _build_manual_embed(user_id, card_id, data)
        channel_id = TRAINING_MANUAL_CHANNEL_ID

    await _post_result(user_id, channel_id, embed, card_id, data)

    _cancel_timer(user_id)
    await _clear_dashboard(user_id)
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _clear_active(conn, user_id)
        await _clear_chain_events(conn, user_id)
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


def _build_abandoned_embed(user_id: int, card_id: int | None, support_card_ids: list | None,
                            friend_support_card_id: int | None, last_turn: int | None = None,
                            last_stats: dict | None = None, last_skill_point: int | None = None,
                            last_fans: int | None = None) -> discord.Embed:
    """last_turn/last_stats/last_skill_point/last_fans come from the most recent
    training_progress seen for this run, if any — a run abandoned before any
    training_progress ever fired (or one from before this feature shipped) simply
    won't have them, and the fields are skipped rather than shown empty/zeroed."""
    if card_id is None:
        # No cardId anywhere — never saw this run's training_start, and the
        # payload didn't have one either. Still notify, just without specifics.
        embed = discord.Embed(title="A training run was glued", colour=discord.Colour.orange())
        embed.add_field(name="Owner", value=f"<@{user_id}>")
        return embed

    char = decode.character_display(card_id)
    name = char["name"] + (f" {char['outfit']}" if char.get("outfit") else "")
    embed = discord.Embed(title=f"The Training for {name} was glued", colour=discord.Colour.orange())

    if last_turn is not None:
        embed.description = f"Last seen: {decode.turn_to_date(last_turn)['label']}"

    embed.add_field(name="Owner", value=f"<@{user_id}>")

    if last_stats:
        stat_line = " / ".join(f"{decode.stat_emoji(k) or k.title()} {v}" for k, v in last_stats.items())
        embed.add_field(name="Stats", value=stat_line, inline=False)
    if last_skill_point is not None or last_fans is not None:
        embed.add_field(name="Skill Points / Fans",
                         value=f"{last_skill_point or 0} / {(last_fans or 0):,}", inline=True)

    filename = _thumbnail_filename(card_id)
    if filename:
        embed.set_thumbnail(url=f"attachment://{filename}")
    if support_card_ids or friend_support_card_id is not None:
        embed.set_image(url=f"attachment://{_SUPPORT_STRIP_FILENAME}")
    return embed


async def _glue_active_run(user_id: int, card_id: int | None, support_card_ids: list | None,
                            friend_support_card_id: int | None, reason: str, last_turn: int | None = None,
                            last_stats: dict | None = None, last_skill_point: int | None = None,
                            last_fans: int | None = None) -> None:
    """Shared "this run never got a real ending" path: posts the abandoned-runs
    notice, deletes the dashboard card, clears active state. Used both by a real
    training_abandoned POST and by handle_training_progress when a new
    singleModeCharaId shows up while an old run is still active (see the plan
    doc's "singleModeCharaId mismatch handling" decision — that case reuses this
    exact pipeline rather than silently overwriting the stale run)."""
    embed = _build_abandoned_embed(user_id, card_id, support_card_ids, friend_support_card_id,
                                    last_turn, last_stats, last_skill_point, last_fans)
    files = (
        [f for f in (_thumbnail_file(card_id),
                      _dashboard_strip_file(support_card_ids, friend_support_card_id)) if f is not None]
        if card_id is not None else []
    )

    channel = bot.get_channel(TRAINING_ABANDONED_CHANNEL_ID)
    if channel is None:
        logger.warning("[Training] Abandoned-runs channel not found/configured")
    else:
        try:
            if files:
                await channel.send(embed=embed, files=files)
            else:
                await channel.send(embed=embed)
        except Exception as exc:
            logger.error(f"[Training] Failed to post abandoned-run notice for user {user_id}: {exc}")

    _cancel_timer(user_id)
    await _clear_dashboard(user_id)
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _clear_active(conn, user_id)
        await _clear_chain_events(conn, user_id)
        await conn.commit()

    logger.info(f"[Training] glued active run for user {user_id}: cardId={card_id}, reason={reason}")


async def handle_training_abandoned(user_id: int, data: dict) -> None:
    """A manual run ended early via the game's own Abandon option — no result
    data exists for it (nothing was ever saved to the roster), so unlike
    training_end there's nothing to post to a results channel. Per instruction
    this skips the plan doc's suggested dashboard-channel notice and instead:
    deletes the ongoing status message outright, and posts an embed to the
    shared TRAINING_ABANDONED_CHANNEL_ID (tagged with an Owner field, since
    the channel is shared across every user/API key) carrying the same
    thumbnail and (unstamped — there's no limitBreakCount data for an
    abandoned run) support-card strip the dashboard message itself was
    already showing.
    """
    async with aiosqlite.connect(LOCAL_DB) as conn:
        active = await _get_active(conn, user_id)

    # cardId is best-effort in the payload itself (may be absent) — fall back
    # to whatever training_start persisted, same as training_end's mode fallback.
    card_id = (active["card_id"] if active else None) or data.get("cardId")
    support_card_ids = active["support_card_ids"] if active else None
    friend_support_card_id = active["friend_support_card_id"] if active else None

    await _glue_active_run(
        user_id, card_id, support_card_ids, friend_support_card_id, reason="training_abandoned",
        last_turn=active["last_turn"] if active else None,
        last_stats=active["last_stats"] if active else None,
        last_skill_point=active["last_skill_point"] if active else None,
        last_fans=active["last_fans"] if active else None,
    )


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


# ---------------------------------------------------------------------------
# Training progress (live dashboard view) — Event 4 in the plan doc.
#
# Unlike training_start/training_end/training_abandoned (one-shot posts),
# training_progress fires once per turn and just edits the same dashboard
# message in place with the latest state — see "Handling training_progress"
# in the plan doc. Two embeds are sent together on one message: the main
# status card, and (Live scenario only, when the payload has a `liveShow`
# block) a separate "Live Show" embed for song/concert bookkeeping.
# ---------------------------------------------------------------------------

# Song catalog for the Live Show embed: {liveId: (title, statBonus, concertBonus,
# cost, year)}. No sync source exists for this anywhere (not uma.moe/GitHub-hosted
# like the factor/skill/character tables), so it's a hardcoded table here, same
# convention as training_decode.py's _STATUS_EFFECT_NAMES — resolved this session
# from the daftuyda.moe token planner cross-checked against master.mdb, not guessed.
# Update by hand if new Grand Live songs ship. 1006/1036 are free/auto-granted (never
# purchased with tokens, year=None), everything else is bought with performance-point
# tokens once its `year` tier unlocks — see _SONG_YEAR_UNLOCK_TURN below.
_SONG_CATALOG: dict[int, tuple[str, str, str | None, dict[str, int], str | None]] = {
    1006: ("Make Debut!", "All Performance Points +10", None, {}, None),
    1036: ("Girls' Legend U", "All Attributes +10", "Friendship Bonus +10%", {}, None),
    1040: ("Here Comes Our Time", "Power +22", "Friendship Bonus +5%", {"vo": 32, "me": 12}, "1"),
    1003: ("Run n' Run!", "Skill Pts +22", "Friendship Bonus +5%", {"da": 14, "vi": 16, "me": 14}, "1"),
    1044: ("Full Speed Ahead! Umadol Power☆", "Speed +22", "Friendship Bonus +5%", {"da": 32, "vi": 12}, "1"),
    1057: ("Zero Is Where the Center Stands!", "Training Speed Gain +1", "Support Chain Event Frequency +1", {"da": 21, "vi": 21}, "1"),
    1038: ("Believe in Miracles!", "Training Wit Gain +1", "Speciality Priority Up +5", {"pa": 21, "me": 21}, "1"),
    1042: ("Go This Way", "Training Power Gain +1", "Support Chain Event Frequency +1", {"vo": 21, "me": 21}, "1"),
    1047: ("Ring Ring Diary", "Training Stamina Gain +1", "Support Chain Event Frequency +1", {"pa": 21, "vi": 21}, "1"),
    1046: ("Getaway! Fallin' Love", "Training Guts Gain +1", "Support Chain Event Frequency +1", {"da": 21, "vi": 21}, "1"),
    1032: ("Run for Our Dream!", "Skill Point Bonus +2", "Speciality Priority Up +5", {"pa": 21, "vi": 21}, "2"),
    1023: ("Our Blue Bird Days", "Training Speed Gain +2", "Speciality Priority Up +5", {"da": 21, "vi": 42}, "2"),
    1011: ("Hey, Guess What!", "Training Guts Gain +2", "Speciality Priority Up +5", {"da": 42, "vi": 21}, "2"),
    1012: ("Grow Up and Shine!", "Skill Point Bonus +3", "Support Chain Event Frequency +1", {"da": 21, "vo": 21, "me": 21}, "2.5"),
    1045: ("Seven Colors Scenery", "Training Power Gain +2", "Speciality Priority Up +5", {"vo": 21, "me": 42}, "2.5"),
    1043: ("Sunbeam Cheer", "Training Wit Gain +2", "Support Chain Event Frequency +1", {"pa": 42, "me": 21}, "2.5"),
    1034: ("Hoppity Sunny Days♪", "Training Stamina Gain +2", "Speciality Priority Up +5", {"pa": 42, "vo": 21}, "2.5"),
    1024: ("Precious Treasure Box", "Speed +26", "Friendship Bonus +10%", {"da": 42, "vi": 26}, "3"),
    1020: ("Fanfare for the Future!", "Guts +26", "Friendship Bonus +10%", {"da": 26, "vi": 42}, "3"),
    1039: ("Present March♪", "Power +22", "Friendship Bonus +5%", {"vo": 22, "me": 22}, "3"),
    1041: ("Dream Sky", "Wit +22", "Friendship Bonus +5%", {"pa": 22, "me": 22}, "3"),
    1021: ("The World's at Our Whim", "Stamina +22", "Friendship Bonus +5%", {"pa": 32, "vo": 12}, "3"),
    1014: ("Sky-Blue Spring", "Guts +22", "Friendship Bonus +5%", {"da": 12, "vi": 32}, "3"),
}
_FREE_SONG_IDS = {1006, 1036, 1029}  # auto-granted, never shown as "not yet learned"
_TOKEN_TO_PERFORMANCE_TYPE = {"da": 1, "pa": 2, "vo": 3, "vi": 4, "me": 5}

# Turn a song's "year" tier (daftuyda.moe's 1/2/2.5/3 tags) first becomes purchasable.
# Checked against the 15 captured runs in network_events.jsonl (each song's earliest
# turn on offer in next_square_info_array, and earliest turn actually bought):
#   Year 1   -> turn 7   (first offered at 7 in every run captured from turn 1-2, first
#                         bought at 8-10; NOT 13 — that's debut, songs open up earlier)
#   Year 2   -> turn 25  (first offered 25, first bought 25)
#   Year 2.5 -> turn 37  (first bought at 37; offers were first seen in captures at 38)
#   Year 3   -> turn 49  (first offered 49, first bought 49)
# Within a tier, which songs actually show up on offer is a random roll of 3 squares at
# a time, so a song can go unbought/unoffered long after its tier unlocks.
_SONG_YEAR_UNLOCK_TURN = {"1": 7, "2": 25, "2.5": 37, "3": 49}

# training_progress's `facilities[].commandId` — two overlapping ID spaces, both
# confirmed (not guessed): 601-605 are the Live scenario's own performance-lesson
# facilities (user-confirmed Speed/Stamina/Power/Guts/Wits), and 101/102/103/105/106
# are the standard stat-training facilities used in every scenario. The link between
# the two: master.mdb's single_mode_training table gives each 601-605 row a
# base_command_id pointing at one of 101/102/103/105/106 (601->101, 602->105,
# 603->102, 604->103, 605->106) — combined with the user's own 601-605 naming, that
# fixes the standard facilities' names too (101=Speed, 102=Power, 103=Guts,
# 105=Stamina, 106=Wits). Cross-checked against the real captured payload: in every
# one of the 5 facilities, the derived "base" stat has the largest gain value among
# that facility's listed statGains, even though each gives a multi-stat hybrid.
# Note: command_id 104 doesn't exist anywhere in this table — a real gap, not a typo
# on this end; unclear what (if anything) it corresponds to.
# _FACILITY_RAINBOW_TYPE spells "Wit" (no "s") to match support_card_type()'s own
# output exactly, for the rainbow-count comparison below.
_FACILITY_DISPLAY_NAMES = {
    101: "Speed", 102: "Power", 103: "Guts", 105: "Stamina", 106: "Wits",
    601: "Speed", 602: "Stamina", 603: "Power", 604: "Guts", 605: "Wits",
}
_FACILITY_RAINBOW_TYPE = {
    101: "Speed", 102: "Power", 103: "Guts", 105: "Stamina", 106: "Wit",
    601: "Speed", 602: "Stamina", 603: "Power", 604: "Guts", 605: "Wit",
}


def _deck_positions(active: dict | None) -> list | None:
    """The 6 supportCardIds in bond/facility-partner position order (1-5 regular,
    6 friend), or None if this run's deck was never captured (training_progress
    arrived without ever seeing this run's training_start — see handle_training_progress)."""
    if not active or not active.get("support_card_ids"):
        return None
    positions = list(active["support_card_ids"])
    if active.get("friend_support_card_id") is not None:
        positions.append(active["friend_support_card_id"])
    return positions if len(positions) == 6 else None


def _resolve_partner(target_id: int, deck_positions: list | None) -> str:
    name = decode.bond_partner_name(target_id, deck_positions=deck_positions)
    if name:
        return name
    if 1 <= target_id <= 6:
        return f"Deck position {target_id}"  # deck unknown for this run — see _deck_positions
    return f"Unknown (id {target_id})"


def _resolve_partner_styled(target_id: int, rainbow_type: str | None, deck_positions: list | None) -> str:
    """Same as _resolve_partner, bold+underlined only when it's a support card (deck
    position 1-6) actually contributing to a rainbow at this facility."""
    label = _resolve_partner(target_id, deck_positions)
    if deck_positions and 1 <= target_id <= 6 and rainbow_type:
        cid = deck_positions[target_id - 1]
        if decode.support_card_type(cid) == rainbow_type:
            return f"**__{label}__**"
    return label


def _energy_bar(vital: int, max_vital: int) -> str:
    """~1 block per 10 energy ("Vital" in the payload, "Energy" in the user-facing
    embed — same field, just the display name the user settled on)."""
    if max_vital <= 0:
        return f"{vital}/{max_vital}"
    total_blocks = max(round(max_vital / 10), 1)
    filled_blocks = min(max(round(vital / 10), 0), total_blocks)
    return "▰" * filled_blocks + "▱" * (total_blocks - filled_blocks) + f" {vital}/{max_vital}"


def _bonds_text(bonds: list[dict], deck_positions: list | None) -> str | None:
    """Merged deck + other-partner bonds, blank line between the two groups. A
    non-deck partner with 0 bond is dropped (per explicit instruction) — deck
    positions always show regardless of value."""
    deck_lines, other_lines = [], []
    for b in bonds:
        tid = b["targetId"]
        if 1 <= tid <= 6:
            deck_lines.append(f"{_resolve_partner(tid, deck_positions)}: {b['evaluation']}")
        elif b.get("evaluation", 0) > 0:
            other_lines.append(f"{_resolve_partner(tid, deck_positions)}: {b['evaluation']}")
    if not deck_lines and not other_lines:
        return None
    return "\n".join(deck_lines) + ("\n\n" + "\n".join(other_lines) if other_lines else "")


def _status_effects_text(status_effect_ids: list[int]) -> str | None:
    if not status_effect_ids:
        return None
    return ", ".join(decode.status_effect_name(i) or f"Unknown ({i})" for i in status_effect_ids)


_RAINBOW_BOND_THRESHOLD = 80  # a same-type card only actually contributes to the
                               # rainbow count once its bond hits this — user-confirmed.
                               # Styling (bold/underline) stays type-only, no bond
                               # gate — see _resolve_partner_styled.


def _training_lines(facilities: list[dict], deck_positions: list | None,
                     bonds: list[dict] | None = None) -> list[str]:
    bond_by_target_id = {b["targetId"]: b.get("evaluation", 0) for b in (bonds or [])}

    lines = []
    for f in facilities:
        cmd = f["commandId"]
        fname = _FACILITY_DISPLAY_NAMES.get(cmd, f"Facility {cmd}")
        rainbow_type = _FACILITY_RAINBOW_TYPE.get(cmd)
        partner_ids = f.get("partnerTargetIds", f.get("supportCardIds", []))  # tolerate the old field name too

        # Rainbow count: same-type AND bond >= _RAINBOW_BOND_THRESHOLD. E.g. Agnes
        # Tachyon (Speed, bond 80+) and Kitasan Black (Speed, bond <80) both in Speed
        # training counts as 1x rainbow (only Agnes Tachyon qualifies), but both still
        # get bold/underlined below — that styling is type-only, not bond-gated.
        rainbow_count = 0
        if deck_positions and rainbow_type:
            for pid in partner_ids:
                if 1 <= pid <= 6:
                    cid = deck_positions[pid - 1]
                    if (decode.support_card_type(cid) == rainbow_type
                            and bond_by_target_id.get(pid, 0) >= _RAINBOW_BOND_THRESHOLD):
                        rainbow_count += 1

        if rainbow_count:
            facility_emoji = decode.stat_rainbow_emoji(rainbow_type.lower()) if rainbow_type else None
            facility_emoji = facility_emoji or decode.stat_emoji(fname.lower()) or ""
            rainbow_suffix = f" ({rainbow_count}x Rainbows)"
        else:
            facility_emoji = decode.stat_emoji(fname.lower()) or ""
            rainbow_suffix = ""

        perf = ", ".join(
            f"{'+' if g['value'] >= 0 else ''}{g['value']} "
            f"{decode.performance_type_emoji(g['performanceType']) or decode.performance_type_display(g['performanceType'])}"
            for g in f.get("performanceGains", [])
        )
        partners = ", ".join(_resolve_partner_styled(pid, rainbow_type, deck_positions) for pid in partner_ids)
        line = f"{facility_emoji} **{fname}** Lv {f['level']}{rainbow_suffix}"
        if perf:
            line += f" — {perf}"
        if partners:
            line += f"\nw/ {partners}"
        lines.append(line)
    return lines


def _token_cost_text(cost: dict[str, int]) -> str:
    parts = []
    for k, v in cost.items():
        emoji = decode.performance_type_emoji(_TOKEN_TO_PERFORMANCE_TYPE[k])
        parts.append(f"{emoji or k} {v}")
    return ", ".join(parts)


def _live_show_summary_lines(live_show: dict) -> list[str]:
    permanent_line = ", ".join(
        f"{'+' if b['effectValue'] >= 0 else ''}{b['effectValue']} {decode.target_type_display(b['targetType'])}"
        for b in live_show.get("trainingBonuses", [])
    ) or "None yet"

    learned_ids = set(live_show.get("masterLiveIds", []))
    concert_totals: dict[str, int] = {}
    for lid in learned_ids:
        entry = _SONG_CATALOG.get(lid)
        if entry and entry[2]:
            concert_totals[entry[2]] = concert_totals.get(entry[2], 0) + 1

    # Same-type concert bonuses stack additively (the game's own "effects added
    # together" rule — see the plan doc's liveShow section).
    aggregated: dict[tuple[str, str], int] = {}
    for text, count in concert_totals.items():
        m = re.match(r"^(.*?)([+-]?\d+)(%?)$", text)
        if m:
            base, num, pct = m.group(1).strip(), int(m.group(2)), m.group(3)
            aggregated[(base, pct)] = aggregated.get((base, pct), 0) + num * count
        else:
            aggregated[(text, "")] = aggregated.get((text, ""), 0) + count
    concert_line = ", ".join(f"{base} +{total}{pct}" for (base, pct), total in aggregated.items()) or "None yet"

    return [f"**Permanent Bonus:** {permanent_line}", f"**Concert Bonus:** {concert_line}"]


def _live_show_not_learned_blocks(live_show: dict, turn: int) -> list[str]:
    """Songs not yet learned AND already available to purchase at this turn — a
    song whose `year` tier hasn't unlocked yet (see _SONG_YEAR_UNLOCK_TURN) is
    skipped entirely rather than shown as "not yet learned", since it isn't
    actually purchasable yet regardless of tokens saved up."""
    learned_ids = set(live_show.get("masterLiveIds", []))
    blocks = []
    for lid, (title, stat_bonus, concert_bonus, cost, year) in _SONG_CATALOG.items():
        if lid in learned_ids or lid in _FREE_SONG_IDS:
            continue
        unlock_turn = _SONG_YEAR_UNLOCK_TURN.get(year, 0)
        if turn < unlock_turn:
            continue
        effect = f"{stat_bonus} / {concert_bonus}" if concert_bonus else stat_bonus
        blocks.append(f"**{title}**\n{effect}\n{_token_cost_text(cost)}")
    return blocks


def _chain_events_text(chain_events: list[dict]) -> str | None:
    """One line per distinct support card with a chain event seen so far this run,
    showing its most-advanced known position/total (see chain_progress.json,
    tests/extract_chain_progress.py). Entries with supportCardId 0 (a non-card-
    specific story beat — character's own story, or date-triggered) are skipped,
    same as entries whose storyId isn't a chain event at all (a standalone side
    event, or the table just hasn't caught up to it yet)."""
    best: dict[int, dict] = {}
    for ce in chain_events:
        cid = ce["support_card_id"]
        if not cid:
            continue
        progress = decode.chain_progress(ce["story_id"])
        if not progress:
            continue
        if cid not in best or progress["position"] > best[cid]["position"]:
            best[cid] = progress
    if not best:
        return None
    lines = []
    for cid, progress in best.items():
        name = decode.support_card_name(cid) or f"Card {cid}"
        ctype = decode.support_card_type(cid)
        label = f"{name} ({ctype})" if ctype else name
        lines.append(f"[{label}] {progress['position']}/{progress['total']}")
    return "\n".join(lines)


def _build_progress_embed(user_id: int, data: dict, deck_positions: list | None,
                           chain_events: list[dict] | None = None) -> discord.Embed:
    card_id = data["cardId"]
    char = decode.character_display(card_id)
    title = char["name"] + (f" {char['outfit']}" if char.get("outfit") else "")

    date_label = decode.turn_to_date(data["turn"])["label"]
    mood_display = decode.mood_emojis(data["mood"]) if "mood" in data else None
    description = f"{date_label} — Manual Training ongoing\n{_energy_bar(data['vital'], data['maxVital'])}"
    if mood_display:
        description += f" {mood_display}"

    embed = discord.Embed(title=title, description=description, colour=discord.Colour.blurple())

    stats = data.get("stats") or {}
    if stats:
        stat_line = " / ".join(f"{decode.stat_emoji(k) or k.title()} {v}" for k, v in stats.items())
        embed.add_field(name="Stats", value=stat_line, inline=False)

    embed.add_field(name="Skill Points / Fans", value=f"{data.get('skillPoint', 0)} / {data.get('fans', 0):,}", inline=True)
    embed.add_field(name="Owner", value=f"<@{user_id}>", inline=True)

    bonds_text = _bonds_text(data.get("bonds", []), deck_positions)
    if bonds_text:
        _add_chunked_field(embed, "Bonds", bonds_text.split("\n"))

    status_text = _status_effects_text(data.get("statusEffects", []))
    if status_text:
        _add_chunked_field(embed, "Status Effects", [status_text])

    chain_text = _chain_events_text(chain_events or [])
    if chain_text:
        _add_chunked_field(embed, "Chain Events", chain_text.split("\n"))

    training_lines = _training_lines(data.get("facilities", []), deck_positions, data.get("bonds", []))
    if training_lines:
        _add_chunked_field(embed, "Training", training_lines)

    filename = _thumbnail_filename(card_id)
    if filename:
        embed.set_thumbnail(url=f"attachment://{filename}")
    if deck_positions:
        embed.set_image(url=f"attachment://{_SUPPORT_STRIP_FILENAME}")

    return embed


def _build_live_show_embed(data: dict) -> discord.Embed | None:
    live_show = data.get("liveShow")
    if not live_show:
        return None
    embed = discord.Embed(title="Live Show", colour=discord.Colour.gold())
    _add_chunked_field(embed, "Bonuses", _live_show_summary_lines(live_show))
    not_learned = _live_show_not_learned_blocks(live_show, data["turn"])
    if not_learned:
        _add_chunked_field(embed, "Not Yet Learned", not_learned)
    return embed


# Per-user throttle on actual Discord edits — training_progress can fire every few
# seconds on a fast turn, and this bot has already hit a 429 once on this exact
# message-edit endpoint (see plan doc decision #7). State is always persisted on
# every POST regardless; only the visible Discord edit is skipped when too soon
# after the last one, so the next edit that does go through is still fully caught up.
# 1s (down from 3s) is possible now that an edit no longer rebuilds/re-uploads the
# images; Discord's edit limit is roughly 5 per 5s per channel, so 1s sits right at it
# — discord.py waits and retries on a 429, so the worst case is a delayed edit.
_PROGRESS_EDIT_MIN_INTERVAL = timedelta(seconds=1)
_last_progress_edit: dict[int, datetime] = {}


async def _update_progress_dashboard(user_id: int, data: dict, deck_positions: list | None,
                                      dashboard_msg_id: int | None,
                                      chain_events: list[dict] | None = None) -> int | None:
    """Edits (or creates, if missing) this user's dashboard message with the live
    training_progress view. Returns the message id. Throttled per _PROGRESS_EDIT_MIN_INTERVAL
    — returns the existing msg_id unchanged without editing if called too soon."""
    now = datetime.now(timezone.utc)
    last = _last_progress_edit.get(user_id)
    if dashboard_msg_id and last and now - last < _PROGRESS_EDIT_MIN_INTERVAL:
        return dashboard_msg_id

    channel = bot.get_channel(TRAINING_DASHBOARD_CHANNEL_ID)
    if channel is None:
        logger.warning("[Training] Dashboard channel not found/configured")
        return dashboard_msg_id

    embed = _build_progress_embed(user_id, data, deck_positions, chain_events)
    live_embed = _build_live_show_embed(data)
    embeds = [embed, live_embed] if live_embed else [embed]

    card_id = data["cardId"]

    def build_files() -> list[discord.File]:
        return [f for f in (
            _thumbnail_file(card_id),
            _dashboard_strip_file(deck_positions[:5] if deck_positions else None,
                                   deck_positions[5] if deck_positions and len(deck_positions) == 6 else None),
        ) if f is not None]

    if dashboard_msg_id:
        try:
            msg = await channel.fetch_message(dashboard_msg_id)
            # Files only referenced by an embed (attachment://...) don't show up in
            # msg.attachments — Discord reports them via the embed's own image/thumbnail
            # URL instead — so check there for "this card already carries its images".
            first = msg.embeds[0] if msg.embeds else None
            already_has_images = bool(first and (first.thumbnail.url or first.image.url))
            if already_has_images:
                # The thumbnail and support-card strip never change within a run, and
                # the card was already posted with them (by training_start or an
                # earlier training_progress) — omitting `attachments` keeps them as-is
                # instead of re-uploading ~3 MB of PNG on every turn.
                await msg.edit(embeds=embeds)
            else:
                await msg.edit(embeds=embeds, attachments=build_files())
            _last_progress_edit[user_id] = now
            return dashboard_msg_id
        except discord.NotFound:
            pass  # fall through and post a new one
        except Exception as exc:
            logger.error(f"[Training] Failed to edit progress dashboard for {user_id}: {exc}")
            return dashboard_msg_id

    try:
        files = build_files()
        msg = await (channel.send(embeds=embeds, files=files) if files else channel.send(embeds=embeds))
        _last_progress_edit[user_id] = now
        return msg.id
    except Exception as exc:
        logger.error(f"[Training] Failed to send progress dashboard for {user_id}: {exc}")
        return None


async def handle_training_progress(user_id: int, data: dict) -> None:
    """One POST per turn during a manual run — see "Handling training_progress" in
    the plan doc. Live-editing surface, not a one-shot post: every call just renders
    the latest state onto the same dashboard message (throttled, see
    _update_progress_dashboard), no duplicate-check needed."""
    single_mode_chara_id = data["singleModeCharaId"]
    card_id = data["cardId"]

    async with aiosqlite.connect(LOCAL_DB) as conn:
        active = await _get_active(conn, user_id)

    if active is not None and active["single_mode_chara_id"] is not None \
            and active["single_mode_chara_id"] != single_mode_chara_id:
        # A different run is already active — the old one never got a real ending.
        # Glue it (same pipeline as a real training_abandoned) before starting fresh.
        await _glue_active_run(
            user_id, active["card_id"], active["support_card_ids"],
            active["friend_support_card_id"], reason="new singleModeCharaId",
            last_turn=active["last_turn"], last_stats=active["last_stats"],
            last_skill_point=active["last_skill_point"], last_fans=active["last_fans"],
        )
        active = None

    started_at = datetime.fromtimestamp(data["timestamp"] / 1000, tz=timezone.utc)
    dashboard_msg_id = active["dashboard_msg_id"] if active else None
    # A fresh row (no prior training_start seen for this exact run) has no deck data —
    # _deck_positions/_bonds_text/_training_lines all degrade gracefully without it.
    support_card_ids = active["support_card_ids"] if active else None
    friend_support_card_id = active["friend_support_card_id"] if active else None
    deck_positions = _deck_positions({
        "support_card_ids": support_card_ids, "friend_support_card_id": friend_support_card_id,
    })

    # Record this turn's chain event(s) first, so the embed's Chain Events field
    # reflects this turn too, not just prior ones.
    async with aiosqlite.connect(LOCAL_DB) as conn:
        for ce in data.get("chainEvents", []):
            await _add_chain_event(conn, user_id, single_mode_chara_id, data["turn"],
                                    ce["storyId"], ce["supportCardId"])
        await conn.commit()
        chain_events = await _get_chain_events(conn, user_id, single_mode_chara_id)

    dashboard_msg_id = await _update_progress_dashboard(user_id, data, deck_positions, dashboard_msg_id, chain_events)

    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _save_active(conn, user_id, "manual", card_id, data.get("scenarioId"), started_at, None,
                            dashboard_msg_id, support_card_ids, friend_support_card_id, single_mode_chara_id,
                            last_turn=data["turn"], last_stats=data.get("stats"),
                            last_skill_point=data.get("skillPoint"), last_fans=data.get("fans"))
        await conn.commit()

    logger.info(f"[Training] training_progress for user {user_id}: turn={data['turn']}, cardId={card_id}")
