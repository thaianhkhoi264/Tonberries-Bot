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
    SKILLS_DB,
    MAIN_OWNER_ID,
)
import notification_module
import cm_module
import skills_module

_update_lock = asyncio.Lock()


_FIX_LOG = "logs/channel_fixes.log"


def _write_fix_log(lines: list[str]) -> None:
    """Append a timestamped block to the channel-fix log file."""
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    block = f"\n{'=' * 60}\n{ts}\n" + "\n".join(lines) + "\n"
    try:
        with open(_FIX_LOG, "a", encoding="utf-8") as f:
            f.write(block)
    except Exception as exc:
        logger.error(f"[UMA] Failed to write fix log: {exc}")


async def _dm_owner(text: str) -> None:
    try:
        user = await bot.fetch_user(MAIN_OWNER_ID)
        await user.send(text)
    except Exception as exc:
        logger.error(f"[UMA] Failed to DM owner: {exc}")

# Cache of {skill_id_str: icon_url} loaded from skills.db (populated once).
_icon_urls: dict[str, str] = {}
_icon_urls_loaded = False


async def _ensure_icon_urls() -> None:
    """Load skill icon URLs from skills.db into the module cache (once)."""
    global _icon_urls, _icon_urls_loaded
    if _icon_urls_loaded:
        return
    _icon_urls_loaded = True
    try:
        async with aiosqlite.connect(SKILLS_DB) as conn:
            async with conn.execute(
                "SELECT skill_id, icon_url FROM skills WHERE icon_url IS NOT NULL"
            ) as cursor:
                rows = await cursor.fetchall()
        _icon_urls = {str(r[0]): r[1] for r in rows}
        logger.info(f"[UMA] Loaded {len(_icon_urls)} skill icon URLs")
    except Exception as exc:
        logger.warning(f"[UMA] Could not load skill icon URLs from skills.db: {exc}")


def _find_cm_for_event(event: dict, cm_events: list[dict]) -> dict | None:
    """
    Match an event from the shared DB to a CM dict from uma.moe by
    comparing start timestamps (closest match within 3 days wins).
    """
    event_start = event.get("start", 0)
    best: dict | None = None
    best_diff = float("inf")
    for cm in cm_events:
        diff = abs(cm.get("start_ts", 0) - event_start)
        if diff < best_diff and diff < 86_400 * 3:
            best_diff = diff
            best = cm
    return best


async def _build_embed_with_skills(event: dict) -> discord.Embed:
    """
    Build the event embed.  For Champions Meeting events, appends a green
    skills section (passive stat-boost skills that activate on that CM course).
    """
    embed = _build_embed(event)

    if "champions meeting" not in event.get("title", "").lower():
        return embed

    try:
        cm_events = await cm_module.fetch_cm_events()
        cm = _find_cm_for_event(event, cm_events)
        if not cm:
            return embed

        await _ensure_icon_urls()
        green = cm_module.get_cm_green_skills(cm, _icon_urls)
        if not green:
            return embed

        lines = []
        for family in green:
            parts = []
            for name, icon_url in family:
                emoji = skills_module.skill_icon_emoji(icon_url) if icon_url else ""
                parts.append(f"{emoji} {name}" if emoji else f"\u2022 {name}")
            lines.append(" / ".join(parts))

        section = "\u2500" * 18 + "\n**Green Skills:**\n" + "\n".join(lines)
        if embed.description:
            embed.description += f"\n\n{section}"
        else:
            embed.description = section

        # Discord embed descriptions cap at 4096 characters
        if len(embed.description) > 4096:
            embed.description = embed.description[:4090] + "\u2026"

    except Exception as exc:
        logger.warning(f"[UMA] Failed to build green skills section: {exc}")

    return embed


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
        desc_text = event["description"]
        if "champions meeting" in event.get("title", "").lower():
            desc_text = cm_module.apply_display_fixes(desc_text)
        description += f"\n\n{desc_text}"
    embed = discord.Embed(title=event["title"], description=description, color=color)
    return embed


async def _send_embed(channel: discord.TextChannel, event: dict) -> discord.Message:
    embed = await _build_embed_with_skills(event)
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
    embed = await _build_embed_with_skills(event)
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
    # Pass 1: identify messages to delete and capture their content before deleting
    to_delete: list[tuple[discord.Message, str | None]] = []  # (message, event_id_or_None)
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

                if db_row and db_row[0] not in valid_ids:
                    to_delete.append((message, db_row[0]))
                elif not db_row and message.embeds:
                    to_delete.append((message, None))
        except discord.HTTPException as exc:
            logger.error(f"[UMA] clear_channel_messages error: {exc}")
            return

    if not to_delete:
        return

    # Capture embed details before deleting
    log_lines = [f"ORPHAN CLEANUP — #{channel.name} (id={channel.id})"]
    for msg, ev_id in to_delete:
        reason = f"event_id={ev_id} not in valid_ids" if ev_id else "untracked bot embed (no DB row)"
        embed_title = msg.embeds[0].title if msg.embeds else "(no embed)"
        embed_desc  = (msg.embeds[0].description or "")[:300] if msg.embeds else ""
        log_lines.append(
            f"  msg_id={msg.id}  reason={reason}\n"
            f"    embed title: {embed_title}\n"
            f"    embed desc:  {embed_desc!r}"
        )

    # Pass 2: delete and clean DB
    deleted = 0
    async with aiosqlite.connect(LOCAL_DB) as conn:
        for msg, ev_id in to_delete:
            try:
                await msg.delete()
                deleted += 1
                if ev_id:
                    await conn.execute(
                        "DELETE FROM event_messages WHERE message_id=?",
                        (str(msg.id),),
                    )
            except discord.NotFound:
                pass
        await conn.commit()

    logger.info(f"[UMA] Cleared {deleted} orphaned message(s) from #{channel.name}")
    _write_fix_log(log_lines)
    await _dm_owner(
        f"⚠️ **[UMA]** Deleted {deleted} orphaned message(s) from <#{channel.id}>. "
        f"Details in `{_FIX_LOG}`."
    )


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
    event_map     = {e["id"]: e for e in events_for_channel}
    desired_order = [e["id"] for e in events_for_channel if e["id"] in tracked_ids]

    if actual_order == desired_order:
        return

    # Only repost from the first mismatch onward
    prefix_len = sum(1 for a, d in zip(actual_order, desired_order) if a == d)
    to_delete  = actual[prefix_len:]
    to_repost  = desired_order[prefix_len:]

    logger.info(f"[UMA] #{channel.name} out of order — reposting {len(to_delete)} message(s)")

    def _name(ev_id: str) -> str:
        e = event_map.get(ev_id)
        return e["name"] if e else ev_id

    def _snowflake_ts(msg_id: str) -> str:
        """Approximate UTC time a Discord message was created from its snowflake ID."""
        try:
            ts = ((int(msg_id) >> 22) + 1420070400000) / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return "?"

    log_lines = [f"ORDER FIX — #{channel.name} (id={channel.id})  |  mismatch starts at index {prefix_len}"]
    log_lines.append("  DB rows (actual order by message_id):")
    for ev_id, msg_id in actual:
        marker = "  ← MISMATCH" if ev_id in [r[0] for r in to_delete] else ""
        log_lines.append(f"    event_id={ev_id}  msg_id={msg_id}  posted={_snowflake_ts(msg_id)}  name={_name(ev_id)}{marker}")
    log_lines.append("  Desired order (from events list):")
    for i, ev_id in enumerate(desired_order):
        log_lines.append(f"    [{i}] event_id={ev_id}  name={_name(ev_id)}")
    log_lines.append(f"  Will delete+repost: {[_name(r[0]) for r in to_delete]}")
    log_lines.append("  Event start times (desired sort key):")
    for e in events_for_channel:
        if e["id"] in tracked_ids:
            start = e.get("start") or e.get("start_time") or "?"
            log_lines.append(f"    event_id={e['id']}  start={start}  name={e['name']}")

    _write_fix_log(log_lines)
    await _dm_owner(
        f"⚠️ **[UMA]** <#{channel.id}> was out of order — reposting {len(to_delete)} message(s). "
        f"Details in `{_FIX_LOG}`."
    )

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
    await uma_update_timers(force_update=True)
    await notification_module.sync_notifications_from_gacha()
    asyncio.create_task(scraper_file_watcher())
    asyncio.create_task(notification_module.notification_loop())
    logger.info("[UMA] Background tasks started")
