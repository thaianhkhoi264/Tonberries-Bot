import asyncio
import logging
import re
from datetime import datetime, timezone

import aiosqlite
import discord

from bot import bot, logger
from global_config import LOCAL_DB, SHARED_NOTIF_DB, NOTIFICATION_CHANNEL_ID

# ---------------------------------------------------------------------------
# Gacha message templates (kept in sync with Gacha-Timer-Bot's MESSAGE_TEMPLATES)
# Used for template-based notifications when no custom_message is set.
# ---------------------------------------------------------------------------

_GACHA_TEMPLATES = {
    "default":                                        "{role}, The {name} is {action} {time}!",
    "uma_champions_meeting_registration_start":       "{role}, {name} Registration has started!",
    "uma_champions_meeting_round1_start":             "{role}, {name} Round 1 has started!",
    "uma_champions_meeting_round2_start":             "{role}, {name} Round 2 has started!",
    "uma_champions_meeting_final_registration_start": "{role}, {name} Final Registration has started!",
    "uma_champions_meeting_finals_start":             "{role}, {name} Finals has started! Good luck!",
    "uma_champions_meeting_end":                      "{role}, {name} has ended! Hope you got a good placement!",
    "uma_champions_meeting_reminder":                 "{role}, {name} is starting in {time}!",
    "uma_legend_race_character_start":                "{role}, {character}'s Legend Race has started!",
    "uma_legend_race_end":                            "{role}, {name} has ended!",
    "uma_legend_race_reminder":                       "{role}, {name} is starting in 1 day!",
}

# Lazy-initialised so the Lock is created after the event loop starts.
_notif_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _notif_lock
    if _notif_lock is None:
        _notif_lock = asyncio.Lock()
    return _notif_lock


# ---------------------------------------------------------------------------
# Role-mention stripping
# ---------------------------------------------------------------------------

def _strip_role_mentions(text: str) -> str:
    """
    Remove Discord role/everyone/here mentions and fix surrounding punctuation.
    Leading  '@role, text' → 'Text'
    Mid-text 'Hey @role, there' → 'Hey, there'
    """
    # Remove leading mention + optional trailing comma/whitespace
    text = re.sub(r'^(?:<@&\d+>|@everyone|@here)[,\s]*', '', text)
    # Remove any remaining mentions
    text = re.sub(r'(?:<@&\d+>|@everyone|@here)', '', text)
    # Clean up ' ,' → ',' and multiple spaces
    text = re.sub(r'\s+,', ',', text)
    text = re.sub(r' {2,}', ' ', text)
    # Strip any leading punctuation/spaces left by an empty {role} substitution
    text = text.lstrip(', ')
    if text:
        text = text[0].upper() + text[1:]
    return text.strip()


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
        return f"🐴 Reminder: **{title}** starts {t}!" # Reminder Notifications
    if timing_type == "start":
        return f"🐴 **{title}** is starting {t}!" # Event Start notifications
    if timing_type == "end":
        if event_time <= int(datetime.now(timezone.utc).timestamp()) + 60:
            return f"🐴 **{title}** has ended." # Event End Notifications
        return f"🐴 **{title}** is ending {t}!" # Event Ending soon Notifications
    if timing_type == "phase_start" and phase:
        return f"🐴 **{title}** — **{phase}** has started!" # CM notifications
    if timing_type == "character_start" and character_name:
        return f"🐴 **{title}** — **{character_name}**'s round has started!" # LR notifications
    return f"🐴 **{title}** — {timing_type} <t:{event_time}:F>"


async def _resolve_message(row) -> str:
    """
    Query Gacha's DB at fire time for the latest custom_message / message_template.
    Falls back to _build_message() if the row is gone or has no override.
    """
    gacha_id = row["event_id"]
    if not gacha_id:
        return _build_message(row)

    action = "ending" if row["timing_type"] == "end" else "starting"
    kwargs = {
        "role":      "",
        "name":      row["title"] or "",
        "category":  row["category"] or "",
        "action":    action,
        "time":      f"<t:{row['event_time_unix']}:R>",
        "phase":     row["phase"] or "",
        "character": row["character_name"] or "",
    }

    try:
        async with aiosqlite.connect(SHARED_NOTIF_DB) as gacha:
            gacha.row_factory = aiosqlite.Row
            async with gacha.execute(
                "SELECT custom_message, message_template "
                "FROM pending_notifications WHERE id=?",
                (gacha_id,),
            ) as cur:
                gacha_row = await cur.fetchone()
    except Exception as exc:
        logger.warning(f"[Notifications] Gacha DB lookup failed (id={gacha_id}): {exc}")
        return _build_message(row)

    if not gacha_row:
        return _build_message(row)

    custom = gacha_row["custom_message"]
    template_key = gacha_row["message_template"]

    if custom:
        try:
            msg = custom.format(**kwargs)
        except (KeyError, IndexError):
            msg = custom
        return _strip_role_mentions(msg)

    if template_key and template_key in _GACHA_TEMPLATES:
        try:
            msg = _GACHA_TEMPLATES[template_key].format(**kwargs)
        except (KeyError, IndexError):
            msg = _GACHA_TEMPLATES[template_key]
        return _strip_role_mentions(msg)

    return _build_message(row)


async def _send_notification(row):
    channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if not channel:
        logger.error("[Notifications] Notification channel not found")
        return
    try:
        await channel.send(await _resolve_message(row))
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
                "SELECT id, category, title, timing_type, notify_unix, "
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
                    "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    [
                        (r["id"], r["category"], r["title"], r["timing_type"],
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
