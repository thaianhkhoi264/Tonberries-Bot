"""
Auto-train reminder module.

Any Discord user can DM the bot:
  auto [text]      — start a 49m 30s countdown timer (text is optional)
  autotrain [text] — same as above
  renew            — repeat the last timer with the same text
  end              — finish the running timer early

On completion the timer message is deleted and a completion DM is sent.
Timer state persists across bot restarts; if the bot comes back after a
timer has already expired it immediately delivers the completion message.
Supports any number of concurrent users.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord

from bot import bot, logger
from global_config import LOCAL_DB

TIMER_DURATION = timedelta(minutes=49, seconds=30)

# Active asyncio tasks keyed by user_id — lets us cancel on re-trigger
_active_tasks: dict[int, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------

def _timer_text(text: str, end_ts: int) -> str:
    prefix = f"**{text}** Independent" if text else "Independent"
    return (
        f"{prefix} training will end in <t:{end_ts}:R>. <:diaread:1529673666370207884>\n"
        "Use `end` to finish early, or `renew` to restart the timer."
    )


def _done_text(text: str) -> str:
    subject = f"Your **{text}** Independent" if text else "Your Independent"
    return (
        f"{subject} Training is complete! I'm excited to see how well that went!\n\n"
        "You can use `auto [text]` to start a new reminder, or `renew` to start another reminder. <a:diapat:1508665594013286400>"
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS autotrain_timers (
                user_id      INTEGER PRIMARY KEY,
                text         TEXT    NOT NULL,
                ends_at      TEXT    NOT NULL,
                timer_msg_id INTEGER,
                done_msg_id  INTEGER,
                state        TEXT    NOT NULL DEFAULT 'pending'
            )
        """)
        await conn.commit()


async def _get_timer(conn, user_id: int) -> dict | None:
    async with conn.execute(
        "SELECT text, ends_at, timer_msg_id, done_msg_id, state "
        "FROM autotrain_timers WHERE user_id=?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "text":         row[0],
        "ends_at":      datetime.fromisoformat(row[1]),
        "timer_msg_id": row[2],
        "done_msg_id":  row[3],
        "state":        row[4],
    }


async def _save_pending(conn, user_id: int, text: str, ends_at: datetime, timer_msg_id: int) -> None:
    await conn.execute(
        """INSERT OR REPLACE INTO autotrain_timers
           (user_id, text, ends_at, timer_msg_id, done_msg_id, state)
           VALUES (?, ?, ?, ?, NULL, 'pending')""",
        (user_id, text, ends_at.isoformat(), timer_msg_id),
    )


async def _save_done(conn, user_id: int, done_msg_id: int) -> None:
    await conn.execute(
        "UPDATE autotrain_timers SET state='done', timer_msg_id=NULL, done_msg_id=? WHERE user_id=?",
        (done_msg_id, user_id),
    )


# ---------------------------------------------------------------------------
# Timer task
# ---------------------------------------------------------------------------

async def _run_timer(user_id: int, text: str, ends_at: datetime, timer_msg_id: int | None) -> None:
    """Sleep until ends_at, then deliver the completion message."""
    now  = datetime.now(timezone.utc)
    wait = (ends_at - now).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)

    try:
        user = await bot.fetch_user(user_id)
    except Exception as exc:
        logger.error(f"[AutoTrain] Could not fetch user {user_id}: {exc}")
        _active_tasks.pop(user_id, None)
        return

    dm = await user.create_dm()

    # Delete the countdown message
    if timer_msg_id is not None:
        try:
            msg = await dm.fetch_message(timer_msg_id)
            await msg.delete()
        except Exception:
            pass  # Already deleted or unavailable — carry on

    # Send the completion message
    try:
        done_msg = await dm.send(_done_text(text))
    except Exception as exc:
        logger.error(f"[AutoTrain] Could not send completion to {user_id}: {exc}")
        _active_tasks.pop(user_id, None)
        return

    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _save_done(conn, user_id, done_msg.id)
        await conn.commit()

    _active_tasks.pop(user_id, None)
    logger.info(f"[AutoTrain] Timer complete for user {user_id} (text={text!r})")


def _schedule(user_id: int, text: str, ends_at: datetime, timer_msg_id: int | None) -> None:
    """Cancel any existing task for the user and schedule a new one."""
    old = _active_tasks.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    _active_tasks[user_id] = asyncio.create_task(
        _run_timer(user_id, text, ends_at, timer_msg_id)
    )


# ---------------------------------------------------------------------------
# Command handlers (called from main.py's on_message)
# ---------------------------------------------------------------------------

async def handle_auto(message: discord.Message, text: str) -> None:
    """Handle `auto [text]` / `autotrain [text]`."""
    text = text.strip()
    user_id = message.author.id

    # Cancel any existing task
    old = _active_tasks.pop(user_id, None)
    if old and not old.done():
        old.cancel()

    # Delete any lingering message from a previous session
    async with aiosqlite.connect(LOCAL_DB) as conn:
        existing = await _get_timer(conn, user_id)
    if existing:
        old_id = existing["timer_msg_id"] or existing["done_msg_id"]
        if old_id:
            try:
                old_msg = await message.channel.fetch_message(old_id)
                await old_msg.delete()
            except Exception:
                pass

    ends_at   = datetime.now(timezone.utc) + TIMER_DURATION
    end_ts    = int(ends_at.timestamp())
    timer_msg = await message.channel.send(_timer_text(text, end_ts))

    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _save_pending(conn, user_id, text, ends_at, timer_msg.id)
        await conn.commit()

    _schedule(user_id, text, ends_at, timer_msg.id)
    logger.info(f"[AutoTrain] Timer started for user {user_id} (text={text!r})")


async def handle_renew(message: discord.Message) -> None:
    """Handle `renew` — restart the timer with the same text as last time."""
    user_id = message.author.id

    async with aiosqlite.connect(LOCAL_DB) as conn:
        existing = await _get_timer(conn, user_id)

    if existing is None:
        await message.channel.send(
            "No previous training found. Use `auto` to start one."
        )
        return

    # Cancel any in-flight task
    old = _active_tasks.pop(user_id, None)
    if old and not old.done():
        old.cancel()

    # Delete the current message (completion or countdown)
    old_id = existing["done_msg_id"] or existing["timer_msg_id"]
    if old_id:
        try:
            old_msg = await message.channel.fetch_message(old_id)
            await old_msg.delete()
        except Exception:
            pass

    text      = existing["text"]
    ends_at   = datetime.now(timezone.utc) + TIMER_DURATION
    end_ts    = int(ends_at.timestamp())
    timer_msg = await message.channel.send(_timer_text(text, end_ts))

    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _save_pending(conn, user_id, text, ends_at, timer_msg.id)
        await conn.commit()

    _schedule(user_id, text, ends_at, timer_msg.id)
    logger.info(f"[AutoTrain] Timer renewed for user {user_id} (text={text!r})")


async def handle_end(message: discord.Message) -> None:
    """Handle `end` — finish the running timer early and deliver completion."""
    user_id = message.author.id

    async with aiosqlite.connect(LOCAL_DB) as conn:
        existing = await _get_timer(conn, user_id)

    if existing is None or existing["state"] != "pending":
        await message.channel.send("No active timer to end.")
        return

    # Cancel the in-flight task
    old = _active_tasks.pop(user_id, None)
    if old and not old.done():
        old.cancel()

    # Delete the countdown message
    timer_msg_id = existing["timer_msg_id"]
    if timer_msg_id:
        try:
            old_msg = await message.channel.fetch_message(timer_msg_id)
            await old_msg.delete()
        except Exception:
            pass

    # Send completion message
    text = existing["text"]
    try:
        done_msg = await message.channel.send(_done_text(text))
    except Exception as exc:
        logger.error(f"[AutoTrain] Could not send completion to {user_id}: {exc}")
        return

    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _save_done(conn, user_id, done_msg.id)
        await conn.commit()

    logger.info(f"[AutoTrain] Timer ended early for user {user_id} (text={text!r})")


# ---------------------------------------------------------------------------
# Startup — restore pending timers from DB
# ---------------------------------------------------------------------------

async def restore_timers() -> None:
    """Reschedule all pending timers on bot startup.

    If a timer already expired while the bot was down, _run_timer will skip
    the sleep and immediately send the completion message.
    """
    await init_db()

    async with aiosqlite.connect(LOCAL_DB) as conn:
        async with conn.execute(
            "SELECT user_id, text, ends_at, timer_msg_id "
            "FROM autotrain_timers WHERE state='pending'"
        ) as cur:
            rows = await cur.fetchall()

    count = 0
    for user_id, text, ends_at_str, timer_msg_id in rows:
        ends_at = datetime.fromisoformat(ends_at_str)
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        _schedule(user_id, text, ends_at, timer_msg_id)
        count += 1

    logger.info(f"[AutoTrain] Restored {count} pending timer(s)")
