import asyncio
import logging
from datetime import datetime, timezone

import aiosqlite
import discord

from bot import bot, logger
from global_config import LOCAL_DB, SHARED_NOTIF_DB, NOTIFICATION_CHANNEL_ID

# Lazy-initialised so the Lock is created after the event loop starts.
_notif_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _notif_lock
    if _notif_lock is None:
        _notif_lock = asyncio.Lock()
    return _notif_lock


# ---------------------------------------------------------------------------
# Message formatter
# ---------------------------------------------------------------------------

def _build_message(row) -> str:
    title          = row["title"]
    event_time     = row["event_time_unix"]
    timing_type    = row["timing_type"]
    phase          = row["phase"]
    character_name = row["character_name"]

    t = f"<t:{event_time}:R>"

    if timing_type == "reminder":
        return f"🐴 Reminder: **{title}** starts {t}!"
    if timing_type == "start":
        return f"🐴 **{title}** is starting {t}!"
    if timing_type == "end":
        if event_time <= int(datetime.now(timezone.utc).timestamp()) + 60:
            return f"🐴 **{title}** has ended."
        return f"🐴 **{title}** is ending {t}!"
    if timing_type == "phase_start" and phase:
        return f"🐴 **{title}** — **{phase}** has started!"
    if timing_type == "character_start" and character_name:
        return f"🐴 **{title}** — **{character_name}**'s round has started!"
    return f"🐴 **{title}** — {timing_type} <t:{event_time}:F>"


async def _send_notification(row):
    channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if not channel:
        logger.error("[Notifications] Notification channel not found")
        return
    try:
        await channel.send(_build_message(row))
    except discord.DiscordException as exc:
        logger.error(f"[Notifications] Failed to send notification: {exc}")


# ---------------------------------------------------------------------------
# Sync from Gacha-Timer-Bot's notification_data.db
# ---------------------------------------------------------------------------

async def sync_notifications_from_gacha():
    """
    Full replacement: delete all unsent notifications, then copy all future
    UMA notifications from Gacha-Timer-Bot's notification_data.db.
    Held under _notif_lock to avoid races with the notification loop.
    """
    now = int(datetime.now(timezone.utc).timestamp())

    try:
        async with aiosqlite.connect(SHARED_NOTIF_DB) as gacha:
            gacha.row_factory = aiosqlite.Row
            async with gacha.execute(
                "SELECT category, title, timing_type, notify_unix, "
                "event_time_unix, phase, character_name "
                "FROM pending_notifications "
                "WHERE profile='UMA' AND notify_unix > ? "
                "ORDER BY notify_unix ASC",
                (now,),
            ) as cursor:
                gacha_rows = [dict(r) for r in await cursor.fetchall()]
    except Exception as exc:
        logger.error(f"[Notifications] Failed to read Gacha's DB: {exc}")
        return

    async with _get_lock():
        async with aiosqlite.connect(LOCAL_DB) as conn:
            await conn.execute("DELETE FROM pending_notifications WHERE sent=0")
            if gacha_rows:
                await conn.executemany(
                    "INSERT INTO pending_notifications "
                    "(event_id, category, title, timing_type, notify_unix, "
                    "event_time_unix, sent, phase, character_name) "
                    "VALUES (NULL, ?, ?, ?, ?, ?, 0, ?, ?)",
                    [
                        (r["category"], r["title"], r["timing_type"],
                         r["notify_unix"], r["event_time_unix"],
                         r["phase"], r["character_name"])
                        for r in gacha_rows
                    ],
                )
            await conn.commit()

    logger.info(f"[Notifications] Synced {len(gacha_rows)} notification(s) from Gacha")


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def notification_loop():
    """Polls LOCAL_DB every 30 s and fires due notifications."""
    while True:
        await asyncio.sleep(30)
        now = int(datetime.now(timezone.utc).timestamp())
        try:
            async with _get_lock():
                async with aiosqlite.connect(LOCAL_DB) as conn:
                    conn.row_factory = aiosqlite.Row
                    async with conn.execute(
                        "SELECT * FROM pending_notifications "
                        "WHERE sent=0 AND notify_unix <= ? "
                        "ORDER BY notify_unix ASC",
                        (now + 30,),
                    ) as cursor:
                        due = await cursor.fetchall()

                    for row in due:
                        await _send_notification(row)
                        await conn.execute(
                            "UPDATE pending_notifications SET sent=1 WHERE id=?",
                            (row["id"],),
                        )
                    await conn.commit()
        except Exception as exc:
            logger.error(f"[Notifications] Loop error: {exc}")


# ---------------------------------------------------------------------------
# pending command helper
# ---------------------------------------------------------------------------

async def get_pending_text() -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    window_end = now + 3 * 86400

    async with aiosqlite.connect(LOCAL_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT title, timing_type, notify_unix, phase, character_name "
            "FROM pending_notifications "
            "WHERE sent=0 AND notify_unix > ? AND notify_unix <= ? "
            "ORDER BY notify_unix ASC",
            (now, window_end),
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return "No notifications scheduled in the next 3 days."

    lines = ["**Upcoming notifications (next 3 days):**\n"]
    for row in rows:
        label = row["timing_type"]
        if row["phase"]:
            label += f": {row['phase']}"
        elif row["character_name"]:
            label += f": {row['character_name']}"
        lines.append(f"• <t:{row['notify_unix']}:F> — [{label}] {row['title']}")

    return "\n".join(lines)
