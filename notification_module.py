import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone

import aiosqlite
import discord

from bot import bot, logger
from global_config import EMOJI_MAPPING_JSON, LOCAL_DB, SHARED_NOTIF_DB, NOTIFICATION_CHANNEL_ID

# ---------------------------------------------------------------------------
# Dia's notification wording
# ---------------------------------------------------------------------------
# Gacha-Timer-Bot rows may carry a `message_template` key (Champions Meeting /
# Legend Race) or a `custom_message`. We only ever READ those columns from
# Gacha's DB; the wording for the template keys lives here, in Dia's voice, with
# several variants per key. A `custom_message` set on the Gacha side still wins
# (see _resolve_message). Placeholders: {name} {time} {character}.

_DIA_TEMPLATES: dict[str, list[str]] = {
    "uma_champions_meeting_registration_start": [
        "Registration for **{name}** has started! Let's get our entries in.",
        "**{name}** registration is open! Shall we prepare our team? ♪",
        "Registration for **{name}** is now open, Trainer! Please check your lineup.",
    ],
    "uma_champions_meeting_round1_start": [
        "Round 1 of **{name}** has begun! Let's give it everything we have!",
        "**{name}** Round 1 is underway! I'm so excited! ♪",
        "The first round of **{name}** has started! Good luck, Trainer!",
    ],
    "uma_champions_meeting_round2_start": [
        "Round 2 of **{name}** has begun! Let's keep the momentum going!",
        "**{name}** Round 2 is underway! I'm cheering for you! ♪",
        "The second round of **{name}** has started! Do your best, Trainer!",
    ],
    "uma_champions_meeting_final_registration_start": [
        "Final Registration for **{name}** is open! Please double-check your lineup.",
        "**{name}** Final Registration has started! One more step before the Finals!",
        "Final Registration for **{name}** has begun, Trainer! Let's be ready.",
    ],
    "uma_champions_meeting_finals_start": [
        "The Finals of **{name}** have begun! Good luck, Trainer! ♪",
        "**{name}** Finals have started! Let's shine our brightest!",
        "It's time for the Finals of **{name}**! I'm cheering for you!",
    ],
    "uma_champions_meeting_end": [
        "**{name}** has ended! Thank you for your hard work. I hope you got a good placement! ♪",
        "That's the end of **{name}**! Well done, Trainer!",
        "**{name}** is over! I hope the results made you smile.",
    ],
    "uma_champions_meeting_reminder": [
        "**{name}** is starting {time}! Please get your team ready. ♪",
        "Almost time! **{name}** starts {time}. Shall we prepare?",
        "**{name}** is coming up {time}, Trainer! Let's be ready.",
    ],
    "uma_legend_race_character_start": [
        "**{character}**'s round of **{name}** has started! Let's give it our all!",
        "It's **{character}**'s turn in **{name}**! Let's watch it together. ♪",
        "**{name}**: **{character}**'s round has started!",
    ],
    "uma_legend_race_end": [
        "**{name}** has ended! Thank you for racing alongside me.",
        "**{name}** is over! Well run, everyone!",
        "That's the end of **{name}**. I hope you enjoyed it, Trainer!",
    ],
    "uma_legend_race_reminder": [
        "**{name}** is starting in 1 day! Please get ready, Trainer.",
        "Just one more day until **{name}**! I can't wait! ♪",
        "**{name}** starts {time}. Let's be ready!",
    ],
}

# Fallback wording for rows with no template (banners, offers, events, ...),
# keyed by (category group, timing type, urgency). Placeholders: {title} {t}
# (relative timestamp) {phase} {character}. Lookup tries the exact key, then
# widens: (group, timing, "*") -> ("*", timing, urgency) -> ("*", timing, "*").
# "early" = the ping fires 12h+ before the event (heads-up), "late" = shorter
# lead (last call).
_EARLY_LEAD_SECONDS = 12 * 3600

_FALLBACK_VARIANTS: dict[tuple[str, str, str], list[str]] = {
    # --- banners ---
    ("banner", "start", "early"): [
        "A new banner is coming {t}: **{title}**. Shall we see who's waiting for us? ♪",
        "**{title}** is coming {t}. I wonder who will join us!",
        "Heads-up, Trainer! **{title}** starts {t}.",
    ],
    ("banner", "start", "late"): [
        "**{title}** is starting {t}! Time to shine!",
        "Almost here! **{title}** starts {t}. Good luck, Trainer! ♪",
        "**{title}** begins {t}. Are you ready?",
    ],
    ("banner", "end", "early"): [
        "**{title}** is ending {t}. Please make your final decisions, Trainer!",
        "Just a reminder: **{title}** ends {t}.",
        "**{title}** will be leaving {t}. Please don't miss it!",
    ],
    ("banner", "end", "late"): [
        "Last chance for **{title}** — it ends {t}!",
        "**{title}** is ending {t}! This is your last call, Trainer!",
        "Hurry, Trainer! **{title}** ends {t}!",
    ],
    # --- offers ---
    ("offer", "start", "early"): [
        "**{title}** will be available {t}!",
        "Heads-up, Trainer! **{title}** is coming {t}.",
        "**{title}** is on its way — it starts {t}. ♪",
    ],
    ("offer", "start", "late"): [
        "**{title}** is starting {t}! Take a look when you can, Trainer!",
        "Almost time! **{title}** starts {t}.",
        "**{title}** becomes available {t}!",
    ],
    ("offer", "end", "*"): [
        "**{title}** is ending {t}. Please don't miss it, Trainer!",
        "Just a reminder: **{title}** ends {t}.",
        "**{title}** will be gone {t}. Take a look before it leaves!",
    ],
    # --- events ---
    ("event", "start", "*"): [
        "**{title}** is starting {t}! I can't wait to see what happens! ♪",
        "**{title}** begins {t}. Shall we get started, Trainer?",
        "Almost time for **{title}**! It starts {t}.",
    ],
    ("event", "end", "early"): [
        "**{title}** is ending {t}. Let's make the most of the time we have left!",
        "Just a reminder: **{title}** ends {t}. Have you finished everything, Trainer?",
        "**{title}** will be over {t}. Let's give it one more push!",
    ],
    ("event", "end", "late"): [
        "**{title}** is ending {t}! Please finish what you've started, Trainer!",
        "Last chance for **{title}** — it ends {t}!",
        "**{title}** ends {t}. Let's finish strong!",
    ],
    # --- anything else ---
    ("*", "start", "*"): [
        "**{title}** is starting {t}! Let's give it our all!",
        "Heads-up, Trainer! **{title}** starts {t}.",
        "**{title}** begins {t}. Shall we get started?",
    ],
    ("*", "end", "*"): [
        "**{title}** is ending {t}!",
        "Just a reminder: **{title}** ends {t}.",
        "**{title}** will be over {t}. Please don't miss it, Trainer!",
    ],
    ("*", "reminder", "*"): [
        "Reminder: **{title}** starts {t}! Let's be ready!",
        "**{title}** is coming up {t}, Trainer. Shall we prepare?",
        "Almost time! **{title}** starts {t}. ♪",
    ],
    ("*", "ended", "*"): [
        "**{title}** has ended. Thank you for your hard work!",
        "**{title}** is over. Well done, Trainer!",
        "That's the end of **{title}**. I hope it went well!",
    ],
    ("*", "phase_start", "*"): [
        "**{title}** — **{phase}** has started!",
        "**{phase}** of **{title}** has begun! Let's give it our all!",
        "**{title}**: **{phase}** is underway! Good luck, Trainer!",
    ],
    ("*", "character_start", "*"): [
        "**{title}** — **{character}**'s round has started!",
        "It's **{character}**'s turn in **{title}**! Let's watch together. ♪",
        "**{character}**'s round of **{title}** has begun!",
    ],
}

# Avoid sending the same variant twice in a row for a given pool.
_last_variant: dict[str, str] = {}


def _pick(pool_key: str, variants: list[str]) -> str:
    choices = [v for v in variants if v != _last_variant.get(pool_key)] or variants
    choice = random.choice(choices)
    _last_variant[pool_key] = choice
    return choice


# Dia's wave emoji opens every notification. It lives in emoji_mapping.json
# ("dia" -> "wave"), read once; if the file/key is missing we just omit it.
_wave_emoji: str | None = None


def _wave() -> str:
    global _wave_emoji
    if _wave_emoji is None:
        try:
            with open(EMOJI_MAPPING_JSON, encoding="utf-8") as f:
                _wave_emoji = json.load(f).get("dia", {}).get("wave", "")
        except Exception as exc:
            logger.warning(f"[Notifications] Could not load wave emoji from {EMOJI_MAPPING_JSON}: {exc}")
            _wave_emoji = ""
    return _wave_emoji


def _with_wave(text: str) -> str:
    wave = _wave()
    return f"{wave} {text}" if wave else text


def _esc(text: str | None) -> str:
    """Escape Discord markdown in event titles etc. (some titles contain a literal
    '*', which would otherwise break the **bold** wrapped around them)."""
    return discord.utils.escape_markdown(text or "")

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

def _category_group(category: str | None) -> str:
    c = (category or "").lower()
    return c if c in ("banner", "offer", "event") else "*"


def _fallback_pool(group: str, timing: str, urgency: str) -> tuple[str, list[str]] | None:
    for key in ((group, timing, urgency), (group, timing, "*"), ("*", timing, urgency), ("*", timing, "*")):
        if key in _FALLBACK_VARIANTS:
            return "/".join(key), _FALLBACK_VARIANTS[key]
    return None


def _build_message(row) -> str:
    title          = row["title"]
    event_time     = row["event_time_unix"]
    timing_type    = row["timing_type"]
    phase          = row["phase"]
    character_name = row["character_name"]

    t = f"<t:{event_time}:R>"
    title = _esc(title)
    group = _category_group(row["category"])
    lead = (event_time or 0) - (row["notify_unix"] or 0)
    urgency = "early" if lead >= _EARLY_LEAD_SECONDS else "late"

    if timing_type == "phase_start" and phase:
        timing = "phase_start"
    elif timing_type == "character_start" and character_name:
        timing = "character_start"
    elif timing_type == "end" and event_time <= int(datetime.now(timezone.utc).timestamp()) + 60:
        timing = "ended"
    elif timing_type in ("start", "end", "reminder"):
        timing = timing_type
    else:
        return _with_wave(f"**{title}** — {timing_type} <t:{event_time}:F>")

    found = _fallback_pool(group, timing, urgency)
    if found is None:
        return _with_wave(f"**{title}** — {timing_type} <t:{event_time}:F>")
    pool_key, variants = found
    variant = _pick(pool_key, variants)
    return _with_wave(variant.format(
        title=title, t=t, phase=_esc(phase), character=_esc(character_name),
    ))


async def _resolve_message(row) -> str:
    """
    Query Gacha's DB at fire time (read-only) for the latest custom_message /
    message_template. A custom_message is used verbatim; a known template key
    gets one of Dia's variants; otherwise _build_message() picks fallback wording.
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

    if template_key and template_key in _DIA_TEMPLATES:
        variant = _pick(template_key, _DIA_TEMPLATES[template_key])
        safe = {**kwargs, "name": _esc(kwargs["name"]), "character": _esc(kwargs["character"]),
                "phase": _esc(kwargs["phase"])}
        try:
            msg = variant.format(**safe)
        except (KeyError, IndexError):
            msg = variant
        return _with_wave(_strip_role_mentions(msg))

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
