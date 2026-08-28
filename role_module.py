"""
role_module.py

`/role <Character> Fan` slash command — Tonberries server only.

The user picks a character (autocomplete appends " Fan"); the bot:
  1. reuses an existing guild role with that exact name if one exists,
     otherwise creates it, coloured with the character's scraped "Image Color";
  2. records the role in LOCAL_DB (`character_roles`);
  3. assigns the role to the invoking member.

Character names + colours come from the scraper output (`TRAINEES_DB`,
`characters` table). The cache reloads automatically when that file changes
(e.g. after `trainee refresh`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time

import aiosqlite
import discord
from discord import app_commands

from bot import bot
from global_config import LOCAL_DB, MAIN_SERVER_ID, TRAINEES_DB

logger = logging.getLogger("role_module")

_FAN_SUFFIX = " Fan"
_STRIP_FAN_RE = re.compile(r"\s*\bfan\b\s*$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Character cache  (name_lower -> (canonical_name, slug, color_hex|None))
# ---------------------------------------------------------------------------

_chars: dict[str, tuple[str, str, str | None]] = {}
_char_names_sorted: list[str] = []
_cache_mtime: float = 0.0

# One lock per role name so concurrent /role calls don't double-create.
_role_locks: dict[str, asyncio.Lock] = {}


def load_character_cache(force: bool = False) -> None:
    """(Re)load the character name/colour cache if TRAINEES_DB changed."""
    global _chars, _char_names_sorted, _cache_mtime

    if not os.path.exists(TRAINEES_DB):
        if force or _chars:
            logger.warning(f"[Role] {TRAINEES_DB} not found — character cache empty")
        _chars, _char_names_sorted, _cache_mtime = {}, [], 0.0
        return

    mtime = os.path.getmtime(TRAINEES_DB)
    if not force and mtime == _cache_mtime and _chars:
        return

    try:
        conn = sqlite3.connect(f"file:{TRAINEES_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT name, slug, image_color FROM characters WHERE name IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[Role] Failed to read {TRAINEES_DB}: {exc}")
        return

    new_map: dict[str, tuple[str, str, str | None]] = {}
    for name, slug, color in rows:
        key = name.strip().lower()
        if key and key not in new_map:
            new_map[key] = (name.strip(), slug, color)

    _chars = new_map
    _char_names_sorted = sorted(v[0] for v in new_map.values())
    _cache_mtime = mtime
    logger.info(f"[Role] Loaded {len(_chars)} characters from {TRAINEES_DB}")


def _resolve_character(text: str) -> tuple[str, str, str | None] | None:
    """text (with or without a trailing 'Fan') -> (canonical_name, slug, color)."""
    load_character_cache()
    base = _STRIP_FAN_RE.sub("", text.strip()).strip()
    return _chars.get(base.lower())


def _colour_from_hex(color: str | None) -> discord.Colour:
    if color:
        try:
            return discord.Colour(int(color.lstrip("#"), 16))
        except ValueError:
            pass
    return discord.Colour.default()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS character_roles (
                guild_id       INTEGER NOT NULL,
                role_id        INTEGER NOT NULL,
                role_name      TEXT    NOT NULL,
                character_slug TEXT,
                character_name TEXT,
                color          TEXT,
                bot_created    INTEGER NOT NULL DEFAULT 0,
                created_at     INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            )
            """
        )
        await conn.commit()
    logger.info("[Role] character_roles table ready")


async def _record_role(guild_id: int, role: discord.Role, slug: str,
                       char_name: str, color: str | None, bot_created: bool) -> None:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await conn.execute(
            """
            INSERT INTO character_roles
                (guild_id, role_id, role_name, character_slug, character_name,
                 color, bot_created, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, role_id) DO UPDATE SET
                role_name=excluded.role_name,
                character_slug=excluded.character_slug,
                character_name=excluded.character_name,
                color=excluded.color
            """,
            (guild_id, role.id, role.name, slug, char_name, color,
             int(bot_created), int(time.time())),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

async def autocomplete_role(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    load_character_cache()
    cur = _STRIP_FAN_RE.sub("", current.strip()).strip().lower()

    names = _char_names_sorted
    if cur:
        starts = [n for n in names if n.lower().startswith(cur)]
        contains = [n for n in names if cur in n.lower() and n not in starts]
        names = starts + contains

    return [
        app_commands.Choice(name=f"{n}{_FAN_SUFFIX}", value=f"{n}{_FAN_SUFFIX}")
        for n in names[:25]
    ]


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def handle_role(interaction: discord.Interaction, text: str) -> None:
    if interaction.guild_id != MAIN_SERVER_ID:
        await interaction.response.send_message(
            "This command only works in the Tonberries server.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.followup.send("Could not resolve your membership.", ephemeral=True)
        return

    resolved = _resolve_character(text)
    if resolved is None:
        await interaction.followup.send(
            f"**{text}** doesn't match a known trainee. Pick a name from the "
            f"autocomplete list (character data comes from the wiki scrape).",
            ephemeral=True,
        )
        return

    char_name, slug, color = resolved
    role_name = f"{char_name}{_FAN_SUFFIX}"

    if not guild.me.guild_permissions.manage_roles:
        await interaction.followup.send(
            "I don't have the **Manage Roles** permission here.", ephemeral=True
        )
        return

    lock = _role_locks.setdefault(f"{guild.id}:{role_name.lower()}", asyncio.Lock())
    async with lock:
        # Reuse an existing role by name — exact match first, then case-insensitive.
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            role = next(
                (r for r in guild.roles if r.name.lower() == role_name.lower()), None
            )

        bot_created = False
        if role is None:
            try:
                role = await guild.create_role(
                    name=role_name,
                    colour=_colour_from_hex(color),
                    reason=f"/role by {member} ({member.id})",
                )
                bot_created = True
            except discord.Forbidden:
                await interaction.followup.send(
                    "I couldn't create the role (missing permission).", ephemeral=True
                )
                return
            except discord.HTTPException as exc:
                logger.error(f"[Role] create_role failed for {role_name!r}: {exc}")
                await interaction.followup.send(
                    f"Failed to create the role: {exc}", ephemeral=True
                )
                return

        await _record_role(guild.id, role, slug, char_name, color, bot_created)

        if role in member.roles:
            await interaction.followup.send(
                f"You already have **{role.name}**.", ephemeral=True
            )
            return

        if role >= guild.me.top_role:
            await interaction.followup.send(
                f"**{role.name}** exists but sits above my highest role, so I "
                f"can't assign it. Ask an admin to move it down.",
                ephemeral=True,
            )
            return

        try:
            await member.add_roles(role, reason="/role command")
        except discord.Forbidden:
            await interaction.followup.send(
                f"I couldn't give you **{role.name}** (role hierarchy).", ephemeral=True
            )
            return

    verb = "Created and gave you" if bot_created else "Gave you"
    await interaction.followup.send(f"{verb} **{role.name}**.", ephemeral=True)
    logger.info(
        f"[Role] {member} ({member.id}) -> {role.name} "
        f"({'created' if bot_created else 'reused'} {role.id})"
    )
