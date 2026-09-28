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
  3. training_end looks up the saved mode (falling back to the payload's own
     mode if the bot missed training_start), builds the result embed, and
     clears the saved state.

Three Discord channels:
  - TRAINING_DASHBOARD_CHANNEL_ID     — one live status message per user
  - TRAINING_MANUAL_CHANNEL_ID        — finished manual-training results
  - TRAINING_INDEPENDENT_CHANNEL_ID   — finished independent-training results

Ambiguous training_end (no persisted training_start state — e.g. the run was
started on a device that isn't running horseact_network_probe): rather than
silently trusting the payload's own `mode` field with no corroborating
context, this DMs the mapped user to ask manual / independent / cancel. The
reply is plain text, matching autotrain_module.py's DM-command style. No
timeout — single-user bot, the pending confirmation just waits. A fresh
training_start OR another ambiguous training_end for the same user cancels
(deletes) whatever confirmation is still pending, since it's now stale.
"""

import asyncio
import json
import logging
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
import training_decode as decode

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
                friend_support_card_id INTEGER
            )
        """)
        # Migrate pre-existing installs (CREATE TABLE IF NOT EXISTS above is a
        # no-op once the table already exists, so add the new columns here).
        for column, coltype in (("support_card_ids", "TEXT"), ("friend_support_card_id", "INTEGER")):
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
        await conn.commit()


async def _get_active(conn, user_id: int) -> dict | None:
    async with conn.execute(
        "SELECT mode, card_id, scenario_id, started_at, ends_at, dashboard_msg_id, "
        "support_card_ids, friend_support_card_id FROM active_training WHERE user_id=?",
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
    }


async def _save_active(conn, user_id: int, mode: str, card_id: int, scenario_id: int | None,
                        started_at: datetime, ends_at: datetime | None, dashboard_msg_id: int | None,
                        support_card_ids: list | None = None, friend_support_card_id: int | None = None) -> None:
    await conn.execute(
        """INSERT OR REPLACE INTO active_training
           (user_id, mode, card_id, scenario_id, started_at, ends_at, dashboard_msg_id,
            support_card_ids, friend_support_card_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, mode, card_id, scenario_id, started_at.isoformat(),
         ends_at.isoformat() if ends_at else None, dashboard_msg_id,
         json.dumps(support_card_ids) if support_card_ids is not None else None,
         friend_support_card_id),
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


# ---------------------------------------------------------------------------
# Dashboard channel — one live status message per user
# ---------------------------------------------------------------------------

def _dashboard_embed(mode: str, card_id: int, ends_at: datetime | None, ready: bool) -> discord.Embed:
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

    return discord.Embed(title=title, description=status, colour=colour)


async def _update_dashboard(user_id: int, mode: str, card_id: int,
                             ends_at: datetime | None = None, ready: bool = False) -> int | None:
    """Post or edit this user's dashboard message. Returns the message id."""
    channel = bot.get_channel(TRAINING_DASHBOARD_CHANNEL_ID)
    if channel is None:
        logger.warning("[Training] Dashboard channel not found/configured")
        return None

    embed = _dashboard_embed(mode, card_id, ends_at, ready)

    async with aiosqlite.connect(LOCAL_DB) as conn:
        existing = await _get_active(conn, user_id)
    msg_id = existing["dashboard_msg_id"] if existing else None

    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return msg_id
        except discord.NotFound:
            pass  # fall through and post a new one
        except Exception as exc:
            logger.error(f"[Training] Failed to edit dashboard message for {user_id}: {exc}")

    try:
        msg = await channel.send(embed=embed)
        return msg.id
    except Exception as exc:
        logger.error(f"[Training] Failed to send dashboard message for {user_id}: {exc}")
        return None


async def _clear_dashboard(user_id: int) -> None:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        existing = await _get_active(conn, user_id)
    if not existing or not existing["dashboard_msg_id"]:
        return
    channel = bot.get_channel(TRAINING_DASHBOARD_CHANNEL_ID)
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(existing["dashboard_msg_id"])
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

    await _update_dashboard(user_id, mode, card_id, ends_at=ends_at, ready=True)
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
# slot ended the run with). It's accepted in `data` here like everything
# else, but intentionally not rendered yet — deferred pending a decision on
# how to display it (and its own supportCardId lookup table).
# ---------------------------------------------------------------------------

def _build_manual_embed(card_id: int, data: dict) -> discord.Embed:
    char = decode.character_display(card_id)
    title = char["name"] + (f" {char['outfit']}" if char.get("outfit") else "")

    rank = data.get("rank", "?")
    rank_score = data.get("rankScore")
    rank_value = f"{rank} ({rank_score:,} pts)" if isinstance(rank_score, int) else str(rank)

    embed = discord.Embed(title=title, colour=discord.Colour.green())
    embed.add_field(name="Rank", value=rank_value)
    embed.add_field(name="Fans", value=f"{data.get('fans', 0):,}")

    stats = data.get("stats") or {}
    if stats:
        stat_line = " / ".join(f"{k.title()} {v}" for k, v in stats.items())
        embed.add_field(name="Stats", value=stat_line, inline=False)

    skills = data.get("skills") or []
    if skills:
        lines = []
        for s in skills:
            info = decode.skill_display(s["skillId"])
            lines.append(f"{info['name']} (Lv. {s.get('level', '?')})")
        embed.add_field(name="Skills", value="\n".join(lines), inline=False)

    return embed


def _build_independent_embed(card_id: int, data: dict) -> discord.Embed:
    char = decode.character_display(card_id)
    title = char["name"] + (f" {char['outfit']}" if char.get("outfit") else "")

    embed = discord.Embed(title=title, colour=discord.Colour.purple())

    factors = data.get("factors") or []
    if factors:
        lines = []
        for f in factors:
            info = decode.factor_display(f["factorId"])
            lines.append(f"{info['name']} ★{info['level']}")
        embed.add_field(name="Factors", value="\n".join(lines), inline=False)

    return embed


# ---------------------------------------------------------------------------
# Duplicate-result handling
# ---------------------------------------------------------------------------

async def _post_result(user_id: int, channel_id: int, embed: discord.Embed) -> None:
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

    msg = await channel.send(embed=embed)
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _save_result_post(conn, user_id, msg.id, channel_id, now)
        await conn.commit()


# ---------------------------------------------------------------------------
# Ambiguous training_end — no persisted training_start state for this user.
# DM them to ask which mode this actually was, rather than trusting the
# payload's mode field with no corroborating context.
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

    await _post_result(user_id, channel_id, embed)

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

    mode = data["mode"]
    card_id = data["cardId"]
    scenario_id = data.get("scenarioId")
    started_at = datetime.fromtimestamp(data["timestamp"] / 1000, tz=timezone.utc)

    # Accepted and persisted for later use — not shown in any embed yet.
    # friendSupportCardId is omitted entirely (not null) on runs with no
    # friend support card, hence .get() rather than an index.
    support_card_ids = data.get("supportCardIds")
    friend_support_card_id = data.get("friendSupportCardId")

    ends_at = started_at + INDEPENDENT_DURATION if mode == "independent" else None

    dashboard_msg_id = await _update_dashboard(user_id, mode, card_id, ends_at=ends_at)

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
    """Returns "posted" or "pending_confirmation"."""
    async with aiosqlite.connect(LOCAL_DB) as conn:
        active = await _get_active(conn, user_id)

    if active is None:
        # No corroborating training_start — ask the user rather than
        # silently trusting the payload's own mode field.
        await _request_confirmation(user_id, data)
        return "pending_confirmation"

    # Prefer the stored mode (set at training_start time, consistent with the
    # timer/notification decision already made) over the payload's own mode.
    await _finish_training_end(user_id, active["mode"], data)
    return "posted"


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
