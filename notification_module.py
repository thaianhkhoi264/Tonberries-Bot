import re
import logging
from datetime import datetime, timezone

import aiosqlite
import discord

from bot import bot, logger
from global_config import (
    LOCAL_DB,
    SHARED_EVENTS_DB,
    NOTIFICATION_CHANNEL_ID,
)

# ---------------------------------------------------------------------------
# Notification timing constants (mirrors Gacha-Timer-Bot exactly)
# Values are minutes before the event time at which to fire the notification.
# ---------------------------------------------------------------------------

UMA_NOTIFICATION_TIMINGS = {
    "Character Banner": {"start": [1440],        "end": [1440, 1500]},
    "Support Banner":   {"start": [1440],        "end": [1440, 1500]},
    "Paid Banner":      {"start": [1440],        "end": [1440, 1500]},
    "Story Event":      {"start": [1440],        "end": [4320, 4380]},
    "Maintenance":      {"start": [60],          "end": [0]},
    "Champions Meeting":{"start": [],            "end": []},  # handled by schedule_cm_notifications
    "Legend Race":      {"start": [],            "end": []},  # handled by schedule_lr_notifications
}


# ---------------------------------------------------------------------------
# Category mapping
# DB stores: "Banner", "Event", "Offer", "Maintenance", "Champions Meeting",
#            "Legend Race"
# Timing dict uses more descriptive keys.
# ---------------------------------------------------------------------------

def _get_timing_category(event: dict) -> str:
    category = event.get("category", "")
    title = event.get("title", "").lower()
    if category == "Banner":
        return "Support Banner" if "support" in title else "Character Banner"
    if category == "Offer":
        return "Paid Banner"
    if category == "Event":
        return "Story Event"
    return category  # "Maintenance", "Champions Meeting", "Legend Race"


# ---------------------------------------------------------------------------
# Phase / character parsers (ported from Gacha-Timer-Bot's uma_handler.py)
# ---------------------------------------------------------------------------

def parse_champions_meeting_phases(description: str, event_start: int, event_end: int) -> list:
    """Derive Champions Meeting phase start times from the stored description."""
    phases = []
    phase_definitions = [
        ("Finals",             1),
        ("Final Registration", 1),
        ("Round 2",            2),
        ("Round 1",            2),
        ("League Selection",   None),
    ]
    duration_map = {}
    if description:
        for line in description.split("\n"):
            if "Round 1" in line:
                m = re.search(r"\((\d+)\s+days?\)", line)
                if m:
                    duration_map["Round 1"] = int(m.group(1))
            elif "Round 2" in line:
                m = re.search(r"\((\d+)\s+days?\)", line)
                if m:
                    duration_map["Round 2"] = int(m.group(1))

    current_end = event_end
    for phase_name, default_duration in phase_definitions:
        if phase_name == "League Selection":
            duration_seconds = current_end - event_start
            phase_start = event_start
        else:
            duration_days = duration_map.get(phase_name, default_duration)
            duration_seconds = duration_days * 86400
            phase_start = current_end - duration_seconds
        phases.insert(0, {
            "name":          phase_name,
            "start_time":    phase_start,
            "duration_days": duration_seconds // 86400,
        })
        current_end = phase_start
    return phases


def parse_legend_race_characters(description: str, event_start: int, event_end: int) -> list:
    """Derive Legend Race character rotation times from the stored description."""
    characters = []
    character_duration = 3 * 86400
    character_names = []

    lines = description.split("\n") if description else []

    for line in lines:
        line = line.strip()
        if line.startswith("-") and "(" in line:
            char_name = line[1:line.index("(")].strip()
            if char_name:
                character_names.append(char_name)

    if not character_names:
        in_character_section = False
        for line in lines:
            if "**Characters:**" in line or "**characters:**" in line.lower():
                in_character_section = True
                character_names.extend(re.findall(r"\[([^\]]+)\]\([^\)]+\)", line))
            elif in_character_section and "[" in line:
                character_names.extend(re.findall(r"\[([^\]]+)\]\([^\)]+\)", line))
            elif in_character_section and line and not line.startswith("**"):
                break

    if not character_names:
        for line in lines:
            if "characters:" in line.lower():
                parts = line.split(":", 1)
                if len(parts) > 1:
                    character_names.extend([n.strip() for n in parts[1].split(",") if n.strip()])

    current_start = event_start
    for char_name in character_names:
        if char_name:
            char_end = current_start + character_duration
            characters.append({
                "name":          char_name,
                "start_time":    current_start,
                "end_time":      char_end,
                "duration_days": 3,
            })
            current_start = char_end
    return characters


# ---------------------------------------------------------------------------
# Notification scheduling
# ---------------------------------------------------------------------------

async def _insert_notifications(rows: list):
    """Bulk-insert notification rows into LOCAL_DB, ignoring duplicates."""
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await conn.executemany(
            """INSERT OR IGNORE INTO pending_notifications
               (event_id, category, title, timing_type, notify_unix, event_time_unix, sent, phase, character_name)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            rows,
        )
        await conn.commit()


async def _schedule_standard_notifications(event: dict, now: int):
    timing_cat = _get_timing_category(event)
    timings = UMA_NOTIFICATION_TIMINGS.get(timing_cat, {})
    event_id = event["id"]
    start    = int(event["start_date"])
    end      = int(event["end_date"])

    rows = []
    for minutes in timings.get("start", []):
        notify_unix = start - minutes * 60
        if notify_unix > now:
            rows.append((event_id, event["category"], event["title"],
                         "start", notify_unix, start, None, None))

    for minutes in timings.get("end", []):
        notify_unix = end - minutes * 60
        if notify_unix > now:
            rows.append((event_id, event["category"], event["title"],
                         "end", notify_unix, end, None, None))

    if rows:
        await _insert_notifications(rows)


async def _schedule_cm_notifications(event: dict, now: int):
    description = event.get("description", "") or ""
    start = int(event["start_date"])
    end   = int(event["end_date"])
    event_id = event["id"]

    phases = parse_champions_meeting_phases(description, start, end)
    rows = []

    reminder_time = start - 86400
    if reminder_time > now:
        rows.append((event_id, event["category"], event["title"],
                     "reminder", reminder_time, start, None, None))

    for phase in phases:
        if phase["start_time"] > now:
            rows.append((event_id, event["category"], event["title"],
                         "phase_start", phase["start_time"], phase["start_time"],
                         phase["name"], None))

    if end > now:
        rows.append((event_id, event["category"], event["title"],
                     "end", end, end, None, None))

    if rows:
        await _insert_notifications(rows)


async def _schedule_lr_notifications(event: dict, now: int):
    description = event.get("description", "") or ""
    start = int(event["start_date"])
    end   = int(event["end_date"])
    event_id = event["id"]

    characters = parse_legend_race_characters(description, start, end)
    rows = []

    reminder_time = start - 86400
    if reminder_time > now:
        rows.append((event_id, event["category"], event["title"],
                     "reminder", reminder_time, start, None, None))

    for char in characters:
        if char["start_time"] > now:
            rows.append((event_id, event["category"], event["title"],
                         "character_start", char["start_time"], char["start_time"],
                         None, char["name"]))

    if end > now:
        rows.append((event_id, event["category"], event["title"],
                     "end", end, end, None, None))

    if rows:
        await _insert_notifications(rows)


async def schedule_notifications_for_event(event: dict):
    """
    (Re-)schedule notifications for one event.
    Deletes all unsent notifications for the event first so rescheduling
    after a time change is safe.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    event_id = event["id"]
    category = event.get("category", "")

    async with aiosqlite.connect(LOCAL_DB) as conn:
        await conn.execute(
            "DELETE FROM pending_notifications WHERE event_id=? AND sent=0",
            (event_id,),
        )
        await conn.commit()

    if category == "Champions Meeting":
        await _schedule_cm_notifications(event, now)
    elif category == "Legend Race":
        await _schedule_lr_notifications(event, now)
    else:
        await _schedule_standard_notifications(event, now)

    logger.info(f"[Notifications] Scheduled notifications for: {event.get('title')}")


# ---------------------------------------------------------------------------
# Sync logic (startup + incremental)
# ---------------------------------------------------------------------------

async def _read_shared_events() -> list[dict]:
    async with aiosqlite.connect(SHARED_EVENTS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id, title, start_date, end_date, category, description, profile "
            "FROM events WHERE profile='UMA'"
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def sync_notifications_on_startup():
    """
    Called once on startup.  Schedules notifications for any event not yet
    in the snapshot, and reschedules events whose times have changed.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    shared_events = await _read_shared_events()

    async with aiosqlite.connect(LOCAL_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT event_id, start_date, end_date FROM events_snapshot"
        ) as cursor:
            snapshot = {r["event_id"]: (r["start_date"], r["end_date"])
                        for r in await cursor.fetchall()}

    shared_ids = set()
    for event in shared_events:
        eid   = event["id"]
        start = event["start_date"]
        end   = event["end_date"]
        shared_ids.add(eid)

        if int(end) < now:
            continue  # already expired

        if eid not in snapshot:
            await schedule_notifications_for_event(event)
        elif snapshot[eid] != (start, end):
            await schedule_notifications_for_event(event)

    async with aiosqlite.connect(LOCAL_DB) as conn:
        stale = set(snapshot) - shared_ids
        if stale:
            ph = ",".join("?" * len(stale))
            await conn.execute(
                f"DELETE FROM events_snapshot WHERE event_id IN ({ph})",
                tuple(stale),
            )
        for event in shared_events:
            await conn.execute(
                "INSERT OR REPLACE INTO events_snapshot (event_id, start_date, end_date) VALUES (?, ?, ?)",
                (event["id"], event["start_date"], event["end_date"]),
            )
        await conn.commit()

    logger.info(f"[Notifications] Startup sync complete ({len(shared_events)} events)")


async def sync_notifications():
    """
    Called by the file watcher after each scraper run.
    Only reschedules events whose start_date or end_date changed.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    shared_events = await _read_shared_events()

    async with aiosqlite.connect(LOCAL_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT event_id, start_date, end_date FROM events_snapshot"
        ) as cursor:
            snapshot = {r["event_id"]: (r["start_date"], r["end_date"])
                        for r in await cursor.fetchall()}

    shared_ids = set()
    rescheduled = 0
    for event in shared_events:
        eid   = event["id"]
        start = event["start_date"]
        end   = event["end_date"]
        shared_ids.add(eid)

        if int(end) < now:
            continue

        if eid not in snapshot:
            await schedule_notifications_for_event(event)
            rescheduled += 1
        elif snapshot[eid] != (start, end):
            # Time changed — reschedule
            await schedule_notifications_for_event(event)
            rescheduled += 1

    async with aiosqlite.connect(LOCAL_DB) as conn:
        stale = set(snapshot) - shared_ids
        if stale:
            ph = ",".join("?" * len(stale))
            await conn.execute(
                f"DELETE FROM events_snapshot WHERE event_id IN ({ph})",
                tuple(stale),
            )
        for event in shared_events:
            await conn.execute(
                "INSERT OR REPLACE INTO events_snapshot (event_id, start_date, end_date) VALUES (?, ?, ?)",
                (event["id"], event["start_date"], event["end_date"]),
            )
        await conn.commit()

    if rescheduled:
        logger.info(f"[Notifications] Sync: rescheduled {rescheduled} event(s)")


# ---------------------------------------------------------------------------
# Notification sender
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
# Background loop
# ---------------------------------------------------------------------------

async def notification_loop():
    """Polls LOCAL_DB every 30 s and fires due notifications."""
    import asyncio
    while True:
        await asyncio.sleep(30)
        now = int(datetime.now(timezone.utc).timestamp())
        try:
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
    window_end = now + 3 * 86400  # 3 days

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
