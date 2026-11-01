"""
Blacklist / timeout system.

Owners manage it from DMs:
  blacklist                       — list everyone currently blocked
  blacklist <user-id>             — block a user until lifted
  timeout <user-id> <seconds>     — block a user temporarily
  unblacklist <user-id>           — lift a blacklist or a timeout

While blocked, a user can't use anything: every slash command (and its
autocomplete), DM command (including the `auto` timer), reaction pick and the
guild hitlist command is answered with a short notice in Dia's voice instead.
Owners can never be blocked.

Blocks live in LOCAL_DB (`user_blocks`) and are mirrored in an in-memory dict, so
the per-message check is a dict lookup and never touches the database — the whole
point is to stay cheap while the bot is being spammed. DM/guild notices are
rate-limited per user (NOTICE_COOLDOWN_S) so the notice can't become spam itself;
slash commands are always answered (one ephemeral response) because an
unanswered interaction shows an error to the user.

This module deliberately doesn't import `bot` (bot.py imports ModerationTree
from here), so it logs through the shared "tonberries" logger directly.
"""

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass

import aiosqlite
import discord
from discord import app_commands

from global_config import LOCAL_DB, OWNER_USER_IDS

logger = logging.getLogger("tonberries")

# At most one notice per blocked user per this many seconds (DMs / guild messages).
NOTICE_COOLDOWN_S = 60

# Longest timeout accepted; anything longer should be a blacklist.
MAX_TIMEOUT_S = 365 * 24 * 3600


@dataclass(frozen=True)
class Block:
    user_id: int
    until: int | None   # unix timestamp the block ends; None = blacklisted until lifted
    set_by: int | None
    set_at: int

    @property
    def kind(self) -> str:
        return "blacklist" if self.until is None else "timeout"


_blocks: dict[int, Block] = {}
_last_notice: dict[int, float] = {}
_bg_tasks: set[asyncio.Task] = set()


def _now() -> float:
    """Wall clock; a function so tests can fake it."""
    return time.time()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create the table, drop expired timeouts and load the cache. Safe to re-run;
    the DB is the source of truth. A failure is logged and leaves the cache empty
    rather than aborting bot startup."""
    global _blocks
    try:
        os.makedirs(os.path.dirname(LOCAL_DB) or ".", exist_ok=True)
        async with aiosqlite.connect(LOCAL_DB) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_blocks (
                    user_id  INTEGER PRIMARY KEY,
                    until_ts INTEGER,
                    set_by   INTEGER,
                    set_at   INTEGER NOT NULL
                )
            """)
            await conn.execute(
                "DELETE FROM user_blocks WHERE until_ts IS NOT NULL AND until_ts <= ?",
                (int(_now()),),
            )
            await conn.commit()
            async with conn.execute(
                "SELECT user_id, until_ts, set_by, set_at FROM user_blocks"
            ) as cur:
                rows = await cur.fetchall()
        _blocks = {r[0]: Block(r[0], r[1], r[2], r[3]) for r in rows}
        logger.info(f"[Moderation] Loaded {len(_blocks)} block(s)")
    except Exception as exc:
        logger.error(f"[Moderation] Could not load blocks: {exc}")


async def _save(block: Block) -> None:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO user_blocks (user_id, until_ts, set_by, set_at) "
            "VALUES (?, ?, ?, ?)",
            (block.user_id, block.until, block.set_by, block.set_at),
        )
        await conn.commit()


async def _delete_expired_row(user_id: int) -> None:
    # Only removes the row if it is still an expired timeout, so a block the owner
    # re-applied in the meantime is never deleted by a stale cleanup task.
    try:
        async with aiosqlite.connect(LOCAL_DB) as conn:
            await conn.execute(
                "DELETE FROM user_blocks WHERE user_id=? AND until_ts IS NOT NULL AND until_ts <= ?",
                (user_id, int(_now())),
            )
            await conn.commit()
    except Exception as exc:
        logger.warning(f"[Moderation] Could not clean expired timeout for {user_id}: {exc}")


def _schedule_expired_cleanup(user_id: int) -> None:
    try:
        task = asyncio.get_running_loop().create_task(_delete_expired_row(user_id))
    except RuntimeError:
        return  # no running loop (e.g. called from a sync test); the next init_db cleans up
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_block(user_id: int) -> Block | None:
    """The active block for a user, or None. Pure in-memory; expired timeouts are
    dropped here. Owners are never blocked."""
    block = _blocks.get(user_id)
    if block is None or user_id in OWNER_USER_IDS:
        return None
    if block.until is not None and block.until <= _now():
        _blocks.pop(user_id, None)
        _schedule_expired_cleanup(user_id)
        return None
    return block


def list_blocks() -> list[Block]:
    """All active blocks: blacklists first, then timeouts by soonest end."""
    active = [b for uid in list(_blocks) if (b := get_block(uid)) is not None]
    return sorted(active, key=lambda b: (b.until is not None, b.until or 0, b.user_id))


async def blacklist(user_id: int, set_by: int | None) -> Block:
    if user_id in OWNER_USER_IDS:
        raise ValueError("owner")
    block = Block(user_id, None, set_by, int(_now()))
    await _save(block)
    _blocks[user_id] = block
    return block


async def timeout(user_id: int, seconds: int, set_by: int | None) -> Block:
    if user_id in OWNER_USER_IDS:
        raise ValueError("owner")
    now = int(_now())
    block = Block(user_id, now + seconds, set_by, now)
    await _save(block)
    _blocks[user_id] = block
    return block


async def unblock(user_id: int) -> bool:
    """Lift a blacklist or a timeout. True if the user was blocked."""
    was_blocked = get_block(user_id) is not None
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await conn.execute("DELETE FROM user_blocks WHERE user_id=?", (user_id,))
        await conn.commit()
    _blocks.pop(user_id, None)
    _last_notice.pop(user_id, None)
    return was_blocked


# ---------------------------------------------------------------------------
# Notices (Dia's voice)
# ---------------------------------------------------------------------------

_BLACKLIST_NOTICES = [
    "I'm sorry, Trainer, but you've been blacklisted, so I can't help with that. "
    "If you think this is a mistake, please contact the bot owner.",
    "I'm afraid I can't help you right now... you're on my blacklist. "
    "If this is a mistake, please reach out to the bot owner.",
    "Hm, I can't take that request, Trainer. You've been blacklisted. "
    "If that doesn't seem right, please contact the bot owner.",
]

_TIMEOUT_NOTICES = [
    "Hm, you're on a timeout right now, Trainer. Please try again {t}!",
    "I'm afraid you're on a timeout at the moment. Let's try again {t}.",
    "You're on a little timeout, Trainer! I'll be happy to help again {t}.",
]


def notice_text(block: Block) -> str:
    if block.until is None:
        return random.choice(_BLACKLIST_NOTICES)
    return random.choice(_TIMEOUT_NOTICES).format(t=f"<t:{block.until}:R>")


def should_notify(user_id: int) -> bool:
    """True at most once per NOTICE_COOLDOWN_S per user (and records it)."""
    now = _now()
    if now - _last_notice.get(user_id, 0.0) < NOTICE_COOLDOWN_S:
        return False
    _last_notice[user_id] = now
    return True


async def notify_message(message: discord.Message, block: Block) -> None:
    """Reply to a blocked user's DM / guild command, rate-limited."""
    if not should_notify(message.author.id):
        return
    try:
        await message.channel.send(notice_text(block))
    except discord.HTTPException as exc:
        logger.warning(f"[Moderation] Could not send block notice to {message.author.id}: {exc}")


# ---------------------------------------------------------------------------
# Slash commands + autocomplete: one global check on the command tree
# ---------------------------------------------------------------------------

class ModerationTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        block = get_block(interaction.user.id)
        if block is None:
            return True
        try:
            if interaction.type is discord.InteractionType.autocomplete:
                await interaction.response.autocomplete([])
            else:
                await interaction.response.send_message(notice_text(block), ephemeral=True)
        except discord.HTTPException as exc:
            logger.warning(f"[Moderation] Could not answer blocked interaction from {interaction.user.id}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Owner DM commands (called from main.py's owner-only chain)
# ---------------------------------------------------------------------------

_USER_RE = re.compile(r"^(?:<@!?(\d{15,20})>|(\d{15,20}))$")
_NO_PINGS = discord.AllowedMentions.none()


def _parse_user_id(token: str) -> int | None:
    m = _USER_RE.match(token.strip())
    return int(m.group(1) or m.group(2)) if m else None


def _who(uid: int) -> str:
    return f"<@{uid}> (`{uid}`)"


def _describe(block: Block) -> str:
    if block.until is None:
        return f"{_who(block.user_id)} — blacklisted"
    return f"{_who(block.user_id)} — timeout, ends <t:{block.until}:R>"


async def handle_blacklist_command(message: discord.Message, args: str) -> None:
    """`blacklist` (list) / `blacklist <user-id>`."""
    args = args.strip()
    if not args:
        blocks = list_blocks()
        if not blocks:
            await message.channel.send(
                "Nobody is blocked right now! To block someone: `blacklist <user-id>` "
                "or `timeout <user-id> <seconds>`."
            )
            return
        lines = "\n".join(f"- {_describe(b)}" for b in blocks)
        await message.channel.send(
            f"**Blocked users:**\n{lines}\n\nLift one with `unblacklist <user-id>`.",
            allowed_mentions=_NO_PINGS,
        )
        return

    uid = _parse_user_id(args.split()[0])
    if uid is None:
        await message.channel.send(
            f"Hm, `{args.split()[0]}` doesn't look like a user ID. "
            "Like this, please: `blacklist <user-id>`"
        )
        return
    try:
        await blacklist(uid, message.author.id)
    except ValueError:
        await message.channel.send("I can't do that to one of my owners, Trainer!")
        return
    except Exception as exc:
        logger.error(f"[Moderation] blacklist failed for {uid}: {exc}")
        await message.channel.send(f"Sorry, I couldn't save that: {exc}")
        return
    logger.info(f"[Moderation] {message.author.id} blacklisted {uid}")
    await message.channel.send(
        f"Understood! {_who(uid)} is now blacklisted.", allowed_mentions=_NO_PINGS
    )


async def handle_unblacklist_command(message: discord.Message, args: str) -> None:
    """`unblacklist <user-id>` — lifts a blacklist or a timeout."""
    token = args.strip().split()[0] if args.strip() else ""
    uid = _parse_user_id(token) if token else None
    if uid is None:
        await message.channel.send("Like this, please: `unblacklist <user-id>`")
        return
    try:
        was_blocked = await unblock(uid)
    except Exception as exc:
        logger.error(f"[Moderation] unblacklist failed for {uid}: {exc}")
        await message.channel.send(f"Sorry, I couldn't save that: {exc}")
        return
    if was_blocked:
        logger.info(f"[Moderation] {message.author.id} lifted the block on {uid}")
        await message.channel.send(
            f"Done! {_who(uid)} can use me again. ♪", allowed_mentions=_NO_PINGS
        )
    else:
        await message.channel.send(
            f"`{uid}` isn't blacklisted or timed out.", allowed_mentions=_NO_PINGS
        )


async def handle_timeout_command(message: discord.Message, args: str) -> None:
    """`timeout <user-id> <seconds>`."""
    parts = args.split()
    if len(parts) != 2:
        await message.channel.send("Like this, please: `timeout <user-id> <seconds>`")
        return
    uid = _parse_user_id(parts[0])
    if uid is None:
        await message.channel.send(
            f"Hm, `{parts[0]}` doesn't look like a user ID. "
            "Like this, please: `timeout <user-id> <seconds>`"
        )
        return
    if not parts[1].isdigit() or int(parts[1]) <= 0:
        await message.channel.send(
            f"Hm, `{parts[1]}` isn't a positive number of seconds..."
        )
        return
    seconds = int(parts[1])
    if seconds > MAX_TIMEOUT_S:
        await message.channel.send(
            f"That's longer than I allow for a timeout ({MAX_TIMEOUT_S:,} seconds, about a year). "
            "Please use `blacklist` for something that long."
        )
        return

    existing = get_block(uid)
    if existing is not None and existing.until is None:
        await message.channel.send(
            f"{_who(uid)} is already blacklisted. Use `unblacklist` first if you'd rather "
            "give them a timeout.",
            allowed_mentions=_NO_PINGS,
        )
        return
    try:
        block = await timeout(uid, seconds, message.author.id)
    except ValueError:
        await message.channel.send("I can't do that to one of my owners, Trainer!")
        return
    except Exception as exc:
        logger.error(f"[Moderation] timeout failed for {uid}: {exc}")
        await message.channel.send(f"Sorry, I couldn't save that: {exc}")
        return
    logger.info(f"[Moderation] {message.author.id} timed out {uid} for {seconds}s")
    await message.channel.send(
        f"Understood! {_who(uid)} is on a timeout until <t:{block.until}:F> (<t:{block.until}:R>).",
        allowed_mentions=_NO_PINGS,
    )
