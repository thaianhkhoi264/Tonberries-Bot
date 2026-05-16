import os
import asyncio
import logging
from datetime import datetime, timezone

import aiosqlite
import discord

from bot import bot, logger
from global_config import (
    LOCAL_DB,
    SHARED_EVENTS_DB,
    SCRAPER_LAST_RUN_FILE,
    GACHA_BOT_DIR,
    ONGOING_CHANNEL_ID,
    UPCOMING_CHANNEL_ID,
)
import notification_module

_update_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Database initialisation
# Creates all tables in LOCAL_DB (event_messages, events_snapshot,
# pending_notifications).
# ---------------------------------------------------------------------------

async def init_local_db():
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS event_messages (
                event_id   TEXT,
                channel_id TEXT,
                message_id TEXT,
                PRIMARY KEY (event_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS pending_notifications (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id         TEXT,
                category         TEXT,
                title            TEXT,
                timing_type      TEXT,
                notify_unix      INTEGER,
                event_time_unix  INTEGER,
                sent             INTEGER DEFAULT 0,
                phase            TEXT,
                character_name   TEXT
            );
        """)
        await conn.commit()
    logger.info("[UMA] Local database initialised")


# ---------------------------------------------------------------------------
# Embed helpers (ported from Gacha-Timer-Bot's uma_module.py)
# ---------------------------------------------------------------------------

def get_event_color(event: dict) -> discord.Color:
    category = event.get("category", "").lower()
    title    = event.get("title",    "").lower()
    if category == "offer" or "paid banner" in title:
        return discord.Color.orange()
    if category == "banner":
        return discord.Color.green() if "support" in title else discord.Color.blue()
    if category == "champions meeting" or "champions meeting" in title:
        return discord.Color.purple()
    if category == "legend race" or "legend race" in title:
        return discord.Color.magenta()
    if category == "event" or "story" in title:
        return discord.Color.gold()
    return discord.Color.default()


def _resolve_image(image: str | None) -> str | None:
    """Turn a relative Gacha-Timer-Bot image path into an absolute one."""
    if not image:
        return None
    if image.startswith("http"):
        return image
    return os.path.join(GACHA_BOT_DIR, image)


def _build_embed(event: dict) -> discord.Embed:
    color = get_event_color(event)
    description = f"**Start:** <t:{event['start']}:F>\n**End:** <t:{event['end']}:F>"
    if event.get("description"):
        description += f"\n\n{event['description']}"
    embed = discord.Embed(title=event["title"], description=description, color=color)
    return embed


async def _send_embed(channel: discord.TextChannel, event: dict) -> discord.Message:
    embed = _build_embed(event)
    image = event.get("image")
    if image:
        if image.startswith("http"):
            embed.set_image(url=image)
            return await channel.send(embed=embed)
        else:
            basename = os.path.basename(image)
            file = discord.File(image, filename=basename)
            embed.set_image(url=f"attachment://{basename}")
            return await channel.send(embed=embed, file=file)
    return await channel.send(embed=embed)


async def _edit_message(msg: discord.Message, event: dict):
    embed = _build_embed(event)
    image = event.get("image")
    if image:
        if image.startswith("http"):
            embed.set_image(url=image)
            await msg.edit(embed=embed)
        else:
            basename = os.path.basename(image)
            file = discord.File(image, filename=basename)
            embed.set_image(url=f"attachment://{basename}")
            await msg.edit(embed=embed, attachments=[file])
    else:
        await msg.edit(embed=embed)


def _embed_changed(old_msg: discord.Message, event: dict) -> bool:
    """Return True if the embed needs to be re-sent."""
    if not old_msg.embeds:
        return True
    old = old_msg.embeds[0]
    new_embed = _build_embed(event)

    # Champions Meeting: ignore description diffs (phase detail lines can vary)
    is_cm = "champions meeting" in event.get("title", "").lower()

    title_changed = old.title != new_embed.title
    color_changed = old.color != new_embed.color
    desc_changed  = (not is_cm) and (old.description != new_embed.description)

    old_image = old.image.url if old.image else None
    image = event.get("image")
    if image and not image.startswith("http"):
        basename = os.path.basename(image)
        image_changed = not (old_image and old_image.endswith(basename))
    else:
        image_changed = old_image != image

    return title_changed or color_changed or desc_changed or image_changed


# ---------------------------------------------------------------------------
# upsert_event_message
# ---------------------------------------------------------------------------

async def upsert_event_message(channel: discord.TextChannel, event: dict,
                               event_id: str, force_update: bool = False):
    async with aiosqlite.connect(LOCAL_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT message_id FROM event_messages WHERE event_id=? AND channel_id=?",
            (event_id, str(channel.id)),
        ) as cursor:
            row = await cursor.fetchone()

        existing_msg = None
        if row and row["message_id"]:
            try:
                existing_msg = await channel.fetch_message(int(row["message_id"]))
            except (discord.NotFound, discord.HTTPException):
                existing_msg = None
                # Remove stale DB entry so we re-send below
                await conn.execute(
                    "DELETE FROM event_messages WHERE event_id=? AND channel_id=?",
                    (event_id, str(channel.id)),
                )
                await conn.commit()

        if existing_msg:
            if not force_update and not _embed_changed(existing_msg, event):
                return  # No change — skip edit
            try:
                await _edit_message(existing_msg, event)
            except discord.HTTPException as exc:
                logger.warning(f"[UMA] Failed to edit message {existing_msg.id}: {exc}")
            return

        # Send a new message and record its ID
        msg = await _send_embed(channel, event)
        await conn.execute(
            "INSERT OR REPLACE INTO event_messages (event_id, channel_id, message_id) VALUES (?, ?, ?)",
            (event_id, str(channel.id), str(msg.id)),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# clear_channel_messages
# Removes Discord messages that no longer correspond to active events.
# ---------------------------------------------------------------------------

async def clear_channel_messages(channel: discord.TextChannel, valid_ids: set):
    deleted = 0
    async with aiosqlite.connect(LOCAL_DB) as conn:
        try:
            async for message in channel.history(limit=100):
                if message.author.id != bot.user.id:
                    continue
                async with conn.execute(
                    "SELECT event_id FROM event_messages WHERE message_id=?",
                    (str(message.id),),
                ) as cursor:
                    db_row = await cursor.fetchone()

                should_delete = False
                if db_row:
                    if db_row[0] not in valid_ids:
                        should_delete = True
                elif message.embeds:
                    # Untracked bot embed — orphan from a previous run
                    should_delete = True

                if should_delete:
                    try:
                        await message.delete()
                        deleted += 1
                        if db_row:
                            await conn.execute(
                                "DELETE FROM event_messages WHERE message_id=?",
                                (str(message.id),),
                            )
                    except discord.NotFound:
                        pass
            await conn.commit()
        except discord.HTTPException as exc:
            logger.error(f"[UMA] clear_channel_messages error: {exc}")

    if deleted:
        logger.info(f"[UMA] Cleared {deleted} orphaned message(s) from #{channel.name}")


# ---------------------------------------------------------------------------
# ensure_channel_order
# Reposts messages from the first out-of-order position onward.
# ---------------------------------------------------------------------------

async def ensure_channel_order(channel: discord.TextChannel, events_for_channel: list):
    async with aiosqlite.connect(LOCAL_DB) as conn:
        async with conn.execute(
            "SELECT event_id, message_id FROM event_messages WHERE channel_id=? "
            "ORDER BY CAST(message_id AS INTEGER) ASC",
            (str(channel.id),),
        ) as cursor:
            actual = [(r[0], r[1]) async for r in cursor]

    if not actual:
        return

    actual_order  = [r[0] for r in actual]
    tracked_ids   = set(actual_order)
    desired_order = [e["id"] for e in events_for_channel if e["id"] in tracked_ids]

    if actual_order == desired_order:
        return

    # Only repost from the first mismatch onward
    prefix_len = sum(1 for a, d in zip(actual_order, desired_order) if a == d)
    to_delete  = actual[prefix_len:]
    to_repost  = desired_order[prefix_len:]

    logger.info(f"[UMA] #{channel.name} out of order — reposting {len(to_delete)} message(s)")

    async with aiosqlite.connect(LOCAL_DB) as conn:
        for ev_id, msg_id in to_delete:
            try:
                await channel.get_partial_message(int(msg_id)).delete()
            except discord.NotFound:
                pass
        if to_delete:
            ph = ",".join("?" * len(to_delete))
            await conn.execute(
                f"DELETE FROM event_messages WHERE channel_id=? AND event_id IN ({ph})",
                (str(channel.id),) + tuple(ev_id for ev_id, _ in to_delete),
            )
            await conn.commit()

    event_map = {e["id"]: e for e in events_for_channel}
    for ev_id in to_repost:
        if ev_id in event_map:
            await upsert_event_message(channel, event_map[ev_id], ev_id)


# ---------------------------------------------------------------------------
# uma_update_timers
# ---------------------------------------------------------------------------

async def uma_update_timers(force_update: bool = False):
    async with _update_lock:
        await _update_timers_impl(force_update)


async def _update_timers_impl(force_update: bool):
    if not bot.is_ready():
        return

    ongoing_channel  = bot.get_channel(ONGOING_CHANNEL_ID)
    upcoming_channel = bot.get_channel(UPCOMING_CHANNEL_ID)

    if not ongoing_channel or not upcoming_channel:
        logger.error("[UMA] Could not find ongoing/upcoming channel(s) — check IDs in global_config.py")
        return

    now               = int(datetime.now(timezone.utc).timestamp())
    one_month_later   = now + 2_592_000   # +30 days
    one_month_earlier = now - 2_592_000   # -30 days

    # Read events from shared (read-only) database
    try:
        async with aiosqlite.connect(SHARED_EVENTS_DB) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT id, title, start_date, end_date, image, category, description "
                "FROM events "
                "WHERE (end_date >= ? OR start_date >= ?) "
                "ORDER BY CAST(start_date AS INTEGER) ASC",
                (str(now), str(one_month_earlier)),
            ) as cursor:
                rows = await cursor.fetchall()
    except Exception as exc:
        logger.error(f"[UMA] Failed to read shared DB: {exc}")
        return

    events = []
    for row in rows:
        image = _resolve_image(row["image"])
        events.append({
            "id":          row["id"],
            "title":       row["title"],
            "start":       int(row["start_date"]),
            "end":         int(row["end_date"]),
            "image":       image,
            "category":    row["category"],
            "description": row["description"],
        })

    valid_ids = {e["id"] for e in events}

    # Remove orphaned Discord messages
    await clear_channel_messages(ongoing_channel,  valid_ids)
    await clear_channel_messages(upcoming_channel, valid_ids)

    ongoing_events  = []
    upcoming_events = []
    skipped = 0

    for event in events:
        start = event["start"]
        end   = event["end"]

        if end < now:
            # Expired — already cleaned up by clear_channel_messages
            continue

        if start > one_month_later and end > one_month_later:
            skipped += 1
            continue

        if start <= now < end:
            await upsert_event_message(ongoing_channel, event, event["id"], force_update)
            ongoing_events.append(event)
        elif start > now:
            await upsert_event_message(upcoming_channel, event, event["id"], force_update)
            upcoming_events.append(event)

    await ensure_channel_order(ongoing_channel,  ongoing_events)
    await ensure_channel_order(upcoming_channel, upcoming_events)

    logger.info(
        f"[UMA] Refresh done — {len(ongoing_events)} ongoing, "
        f"{len(upcoming_events)} upcoming, {skipped} skipped"
    )


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _delayed_notif_sync(delay: int = 60):
    """Wait for Gacha's notification DB to settle, then sync."""
    await asyncio.sleep(delay)
    await notification_module.sync_notifications_from_gacha()


async def scraper_file_watcher():
    """Polls SCRAPER_LAST_RUN_FILE every 60 s; refreshes when it changes."""
    last_seen = None
    while True:
        await asyncio.sleep(60)
        try:
            if not os.path.exists(SCRAPER_LAST_RUN_FILE):
                continue
            with open(SCRAPER_LAST_RUN_FILE, "r") as fh:
                timestamp = fh.read().strip()
            if timestamp != last_seen:
                last_seen = timestamp
                logger.info("[UMA] Scraper signal detected — refreshing")
                await uma_update_timers()
                asyncio.create_task(_delayed_notif_sync())
        except Exception as exc:
            logger.error(f"[UMA] File watcher error: {exc}")


async def start_background_tasks():
    """
    Called from on_ready.
    Runs the initial timer refresh, syncs notifications, then launches
    background loops.
    """
    await uma_update_timers()
    await notification_module.sync_notifications_from_gacha()
    asyncio.create_task(scraper_file_watcher())
    asyncio.create_task(notification_module.notification_loop())
    logger.info("[UMA] Background tasks started")
