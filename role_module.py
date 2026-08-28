"""
role_module.py

`/role <Character> Fan` and `/removerole <Character> Fan` — Tonberries server only.

`/role`:
  1. reuses an existing guild role named "<Character> Fan" if one exists,
     otherwise creates it, coloured with the character's scraped "Image Color";
  2. records the role in LOCAL_DB (`character_roles`) and the assignment in
     `user_character_roles`;
  3. assigns the role to the invoking member.
  4. Enforces a per-user cap of MAX_FAN_ROLES: adding one past the cap removes
     the member's oldest fan role (and says so in the reply).

`/removerole`: takes one of the member's fan roles back off them (the guild role
itself is left alone — other members may still have it).

A "fan role" is any guild role whose name is "<known character> Fan".
Character names + colours come from the scraper output (`TRAINEES_DB`,
`characters` table); the cache reloads when that file changes (`trainee refresh`).
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

from global_config import LOCAL_DB, MAIN_SERVER_ID, TRAINEES_DB

logger = logging.getLogger("role_module")

MAX_FAN_ROLES = 5

_FAN_SUFFIX = " Fan"
_STRIP_FAN_RE = re.compile(r"\s*\bfan\b\s*$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Character cache  (name_lower -> (canonical_name, slug, color_hex|None))
# ---------------------------------------------------------------------------

_chars: dict[str, tuple[str, str, str | None]] = {}
_char_names_sorted: list[str] = []
_cache_mtime: float = 0.0

# One lock per (guild, role name) so concurrent /role calls don't double-create.
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


def _is_fan_role_name(name: str) -> bool:
    if not name.endswith(_FAN_SUFFIX):
        return False
    return name[: -len(_FAN_SUFFIX)].strip().lower() in _chars


def _member_fan_roles(member: discord.Member) -> list[discord.Role]:
    """Every role on the member whose name is '<known character> Fan'."""
    load_character_cache()
    return [r for r in member.roles if _is_fan_role_name(r.name)]


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
        await conn.executescript(
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
            );

            CREATE TABLE IF NOT EXISTS user_character_roles (
                guild_id  INTEGER NOT NULL,
                user_id   INTEGER NOT NULL,
                role_id   INTEGER NOT NULL,
                added_at  INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, role_id)
            );
            """
        )
        await conn.commit()
    logger.info("[Role] role tables ready")


async def _record_role(conn: aiosqlite.Connection, guild_id: int, role: discord.Role,
                       slug: str, char_name: str, color: str | None,
                       bot_created: bool) -> None:
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


async def _track_assignment(conn: aiosqlite.Connection, guild_id: int,
                            user_id: int, role_id: int) -> None:
    await conn.execute(
        """
        INSERT INTO user_character_roles (guild_id, user_id, role_id, added_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id, role_id) DO UPDATE SET added_at=excluded.added_at
        """,
        (guild_id, user_id, role_id, int(time.time())),
    )


async def _untrack_assignment(conn: aiosqlite.Connection, guild_id: int,
                              user_id: int, role_id: int) -> None:
    await conn.execute(
        "DELETE FROM user_character_roles WHERE guild_id=? AND user_id=? AND role_id=?",
        (guild_id, user_id, role_id),
    )


async def _fan_roles_by_age(conn: aiosqlite.Connection, member: discord.Member
                            ) -> list[discord.Role]:
    """The member's fan roles, oldest first.

    Ordered by the tracked `added_at`; roles the member holds but that were
    never assigned through the bot sort first (treated as age 0) and are
    pruned/absent from the tracking table.
    """
    async with conn.execute(
        "SELECT role_id, added_at FROM user_character_roles WHERE guild_id=? AND user_id=?",
        (member.guild.id, member.id),
    ) as cur:
        added_at = {rid: ts for rid, ts in await cur.fetchall()}

    current = _member_fan_roles(member)
    current_ids = {r.id for r in current}

    # Prune tracking rows for fan roles the member no longer has.
    stale = [rid for rid in added_at if rid not in current_ids]
    for rid in stale:
        await _untrack_assignment(conn, member.guild.id, member.id, rid)

    current.sort(key=lambda r: (added_at.get(r.id, 0), r.id))
    return current


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


async def autocomplete_removerole(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return []
    cur = _STRIP_FAN_RE.sub("", current.strip()).strip().lower()
    names = sorted(r.name for r in _member_fan_roles(member))
    if cur:
        names = [n for n in names if cur in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


# ---------------------------------------------------------------------------
# /role
# ---------------------------------------------------------------------------

def _guild_ok(interaction: discord.Interaction) -> bool:
    return interaction.guild_id == MAIN_SERVER_ID


async def handle_role(interaction: discord.Interaction, text: str) -> None:
    if not _guild_ok(interaction):
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

        async with aiosqlite.connect(LOCAL_DB) as conn:
            await _record_role(conn, guild.id, role, slug, char_name, color, bot_created)

            if role in member.roles:
                await _track_assignment(conn, guild.id, member.id, role.id)
                await conn.commit()
                await interaction.followup.send(
                    f"You already have **{role.name}**.", ephemeral=True
                )
                return

            if role >= guild.me.top_role:
                await conn.commit()
                await interaction.followup.send(
                    f"**{role.name}** exists but sits above my highest role, so I "
                    f"can't assign it. Ask an admin to move it down.",
                    ephemeral=True,
                )
                return

            # Enforce the per-user cap: evict the oldest fan role if needed.
            evicted_note = ""
            existing = await _fan_roles_by_age(conn, member)
            if len(existing) >= MAX_FAN_ROLES:
                oldest = existing[0]
                try:
                    await member.remove_roles(
                        oldest, reason=f"/role {MAX_FAN_ROLES}-role limit"
                    )
                    await _untrack_assignment(conn, guild.id, member.id, oldest.id)
                    evicted_note = (
                        f" You were at the {MAX_FAN_ROLES}-role limit, so I removed "
                        f"**{oldest.name}**."
                    )
                except discord.Forbidden:
                    evicted_note = (
                        f" You're at the {MAX_FAN_ROLES}-role limit and I couldn't "
                        f"remove **{oldest.name}** (role hierarchy) — ask an admin."
                    )

            try:
                await member.add_roles(role, reason="/role command")
            except discord.Forbidden:
                await conn.commit()
                await interaction.followup.send(
                    f"I couldn't give you **{role.name}** (role hierarchy).",
                    ephemeral=True,
                )
                return

            await _track_assignment(conn, guild.id, member.id, role.id)
            await conn.commit()

    verb = "Created and gave you" if bot_created else "Gave you"
    await interaction.followup.send(f"{verb} **{role.name}**.{evicted_note}", ephemeral=True)
    logger.info(
        f"[Role] {member} ({member.id}) -> {role.name} "
        f"({'created' if bot_created else 'reused'} {role.id}){evicted_note}"
    )


# ---------------------------------------------------------------------------
# /removerole
# ---------------------------------------------------------------------------

async def handle_removerole(interaction: discord.Interaction, text: str) -> None:
    if not _guild_ok(interaction):
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
    role: discord.Role | None = None
    if resolved is not None:
        role_name = f"{resolved[0]}{_FAN_SUFFIX}"
        role = discord.utils.get(member.roles, name=role_name) or next(
            (r for r in member.roles if r.name.lower() == role_name.lower()), None
        )
    if role is None:
        # Fall back to a direct name match against the member's fan roles.
        want = _STRIP_FAN_RE.sub("", text.strip()).strip().lower()
        role = next(
            (r for r in _member_fan_roles(member)
             if r.name[: -len(_FAN_SUFFIX)].strip().lower() == want),
            None,
        )

    if role is None:
        await interaction.followup.send(
            f"You are not a **{text}**.", ephemeral=True
        )
        return

    try:
        await member.remove_roles(role, reason="/removerole command")
    except discord.Forbidden:
        await interaction.followup.send(
            f"I couldn't remove **{role.name}** (role hierarchy).", ephemeral=True
        )
        return

    async with aiosqlite.connect(LOCAL_DB) as conn:
        await _untrack_assignment(conn, guild.id, member.id, role.id)
        await conn.commit()

    await interaction.followup.send(f"Removed **{role.name}**.", ephemeral=True)
    logger.info(f"[Role] {member} ({member.id}) removed {role.name} ({role.id})")
