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

A "fan role" is any guild role whose name is "<known character> Fan" — the
possessive "<Character>'s Fan" and up to two typos in the character part are
tolerated (as long as the closest known character is unambiguous).
Character names + colours come from the scraper output (`TRAINEES_DB`,
`characters` table); the cache reloads when that file changes (`trainee refresh`).

`sync_existing_fan_roles()` (run on startup) registers fan roles that already
existed on the server before the bot touched them: `bot_created=0` and a
synthetic `created_at` ordered alphabetically by role name, so the per-user
eviction order stays stable and legacy roles always count as older than any
role handed out through `/role`.
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

# Base for the synthetic `created_at` given to pre-existing (non-bot) fan roles.
# 2000-01-01 UTC + alphabetical index — always far below a real assignment time,
# so legacy roles sort as oldest, in name order.
_SYNTHETIC_BASE = 946_684_800

_FAN_SUFFIX = " Fan"
_APOS = "'’ʼ"  # straight ', right single quote ', modifier-letter apostrophe

# Trailing " Fan" on a full role name (requires whitespace before "fan").
_FAN_TAIL_RE = re.compile(r"\s+fan\s*$", re.IGNORECASE)
# Possessive tail ("'s" / "'"), tolerated so "Special Week's Fan" == "Special Week Fan".
_POSSESSIVE_RE = re.compile(rf"[{_APOS}]s?$", re.IGNORECASE)
# Lenient strips for partial autocomplete input.
_QUERY_FAN_RE = re.compile(r"\s+f(?:an?)?\s*$", re.IGNORECASE)   # trailing f / fa / fan
_QUERY_POSS_RE = re.compile(rf"\s*[{_APOS}]s?\s*$")              # trailing 's / '

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


_FUZZY_MAX_EDITS = 2
_FUZZY_MIN_LEN = 4


def _levenshtein(a: str, b: str, max_d: int) -> int:
    """Edit distance, capped: returns max_d + 1 as soon as it's known to exceed."""
    la, lb = len(a), len(b)
    if abs(la - lb) > max_d:
        return max_d + 1
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        row_best = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            row_best = min(row_best, cur[j])
        if row_best > max_d:
            return max_d + 1
        prev = cur
    return prev[lb]


def _fuzzy_character(text: str) -> str | None:
    """Closest known character to `text` within _FUZZY_MAX_EDITS — only if unambiguous."""
    key = text.strip().lower()
    if len(key) < _FUZZY_MIN_LEN:
        return None
    best_d = _FUZZY_MAX_EDITS + 1
    winners: list[str] = []
    for ckey, meta in _chars.items():
        d = _levenshtein(key, ckey, _FUZZY_MAX_EDITS)
        if d < best_d:
            best_d, winners = d, [meta[0]]
        elif d == best_d:
            winners.append(meta[0])
    if best_d <= _FUZZY_MAX_EDITS and len(winners) == 1:
        return winners[0]
    return None


def _fan_role_base(name: str) -> str | None:
    """A full fan-role name -> the canonical character name, or None.

    Accepts "<Character> Fan", the possessive "<Character>'s Fan" (straight or
    curly apostrophe), and up to _FUZZY_MAX_EDITS typos in the character part
    (as long as the closest match is unambiguous). Case-insensitive.
    """
    load_character_cache()
    m = _FAN_TAIL_RE.search(name)
    if not m:
        return None
    head = name[: m.start()].strip()
    depossessive = _POSSESSIVE_RE.sub("", head).strip()
    for cand in (head, depossessive):
        hit = _chars.get(cand.lower())
        if hit:
            return hit[0]
    return _fuzzy_character(depossessive)


def _normalize_query(text: str) -> str:
    """Lenient strip of a trailing 'fan'/'f'/possessive from partial user input."""
    t = _QUERY_FAN_RE.sub("", text.strip())
    t = _QUERY_POSS_RE.sub("", t)
    return t.strip().lower()


def _resolve_character(text: str) -> tuple[str, str, str | None] | None:
    """text (with or without a trailing 'Fan'/'s Fan') -> (canonical, slug, color)."""
    load_character_cache()
    base = _fan_role_base(text)
    if base:
        return _chars.get(base.lower())
    q = _normalize_query(text)
    hit = _chars.get(q) or _chars.get(text.strip().lower())
    if hit:
        return hit
    fuzzy = _fuzzy_character(q)
    return _chars.get(fuzzy.lower()) if fuzzy else None


def _is_fan_role_name(name: str) -> bool:
    return _fan_role_base(name) is not None


def _member_fan_roles(member: discord.Member) -> list[discord.Role]:
    """Every role on the member named '<known character>['s] Fan'."""
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
    """The member's fan roles, oldest first (this is the /role eviction order).

    Age is the per-user `added_at` if the role was handed out through the bot;
    otherwise the guild-level `character_roles.created_at` (a synthetic
    alphabetical value for pre-existing roles, see `sync_existing_fan_roles`);
    otherwise 0. Ties break by role name, then id.
    """
    async with conn.execute(
        "SELECT role_id, added_at FROM user_character_roles WHERE guild_id=? AND user_id=?",
        (member.guild.id, member.id),
    ) as cur:
        added_at = {rid: ts for rid, ts in await cur.fetchall()}

    async with conn.execute(
        "SELECT role_id, created_at FROM character_roles WHERE guild_id=?",
        (member.guild.id,),
    ) as cur:
        role_created = {rid: ts for rid, ts in await cur.fetchall()}

    current = _member_fan_roles(member)
    current_ids = {r.id for r in current}

    # Prune tracking rows for fan roles the member no longer has.
    stale = [rid for rid in added_at if rid not in current_ids]
    for rid in stale:
        await _untrack_assignment(conn, member.guild.id, member.id, rid)

    def _age(r: discord.Role) -> int:
        return added_at.get(r.id) or role_created.get(r.id, 0)

    current.sort(key=lambda r: (_age(r), r.name.lower(), r.id))
    return current


async def sync_existing_fan_roles(guild: discord.Guild) -> int:
    """Register '<character> Fan' guild roles that the bot has never seen.

    Pre-existing roles get `bot_created=0` and a synthetic `created_at` of
    `_SYNTHETIC_BASE + <alphabetical index among all fan roles>`, giving a
    stable, name-ordered comparison point for `/role` eviction. Roles already
    in `character_roles` (bot-created, or synced on a previous run) are left
    untouched. Returns the number newly registered.
    """
    load_character_cache()
    fan_roles = sorted(
        (r for r in guild.roles if _is_fan_role_name(r.name)),
        key=lambda r: r.name.lower(),
    )
    if not fan_roles:
        return 0

    added = 0
    async with aiosqlite.connect(LOCAL_DB) as conn:
        async with conn.execute(
            "SELECT role_id FROM character_roles WHERE guild_id=?", (guild.id,)
        ) as cur:
            known = {rid for (rid,) in await cur.fetchall()}

        for idx, role in enumerate(fan_roles):
            if role.id in known:
                continue
            resolved = _resolve_character(role.name)
            slug = resolved[1] if resolved else None
            char_name = resolved[0] if resolved else (_fan_role_base(role.name) or role.name)
            color = resolved[2] if resolved else None
            await conn.execute(
                """
                INSERT INTO character_roles
                    (guild_id, role_id, role_name, character_slug, character_name,
                     color, bot_created, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(guild_id, role_id) DO NOTHING
                """,
                (guild.id, role.id, role.name, slug, char_name, color,
                 _SYNTHETIC_BASE + idx),
            )
            added += 1
        await conn.commit()

    if added:
        logger.info(
            f"[Role] Registered {added} pre-existing fan role(s) in "
            f"{guild.name} ({guild.id})"
        )
    return added


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

async def autocomplete_role(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    load_character_cache()
    cur = _normalize_query(current)

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
    cur = _normalize_query(current)
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

    lock = _role_locks.setdefault(f"{guild.id}:{char_name.lower()}", asyncio.Lock())
    async with lock:
        # Reuse an existing role: exact "<Character> Fan", then case-insensitive,
        # then any role that resolves to this character ("<Character>'s Fan" etc).
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            role = next(
                (r for r in guild.roles if r.name.lower() == role_name.lower()), None
            )
        if role is None:
            role = next(
                (r for r in guild.roles if _fan_role_base(r.name) == char_name), None
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
        # Any of the member's fan roles that resolve to this character
        # ("<Character> Fan" or "<Character>'s Fan").
        role = next(
            (r for r in _member_fan_roles(member) if _fan_role_base(r.name) == resolved[0]),
            None,
        )
    if role is None:
        # Last resort: the caller passed a role name verbatim.
        role = discord.utils.get(member.roles, name=text.strip())

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
