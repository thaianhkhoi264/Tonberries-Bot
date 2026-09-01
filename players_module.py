"""
players_module.py

Links an in-game trainer name to a Discord user, so other features (e.g. the
monthly role leaderboard) can map circle data to Discord members.

Links are keyed on `trainer_name` (case-insensitive). One Discord user maps to
one trainer name.

Owner DM commands (either owner):
  link                       — show link count + usage
  link list                  — list every link
  link <@user|id> <name...>  — link a trainer name to a Discord user
  link import                — bulk-load from data/player_links.csv
                               (or from a .csv attached to the message)
  link export                — (re)write data/player_links.csv from the current
                               month's circle roster, keeping IDs already set
  unlink <name...>           — remove a link

CSV format (data/player_links.csv):
    trainer_name,discord_user_id
    Parker,123456789012345678
    Kaga,
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
from datetime import datetime, timezone

import aiosqlite
import discord

from global_config import LOCAL_DB, game_now

logger = logging.getLogger("players_module")

PLAYER_LINKS_CSV = "data/player_links.csv"

_USER_TOKEN_RE = re.compile(r"^<@!?(\d+)>$|^(\d{15,25})$")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_links (
                trainer_name    TEXT COLLATE NOCASE PRIMARY KEY,
                discord_user_id INTEGER NOT NULL UNIQUE,
                linked_at       INTEGER NOT NULL,
                linked_by       INTEGER
            )
            """
        )
        await conn.commit()
    logger.info("[Players] player_links table ready")


async def set_link(trainer_name: str, discord_user_id: int,
                   linked_by: int | None = None) -> None:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        # A Discord id can only map to one trainer — clear any prior owner.
        await conn.execute(
            "DELETE FROM player_links WHERE discord_user_id=? AND trainer_name<>?",
            (discord_user_id, trainer_name),
        )
        await conn.execute(
            """
            INSERT INTO player_links (trainer_name, discord_user_id, linked_at, linked_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(trainer_name) DO UPDATE SET
                discord_user_id=excluded.discord_user_id,
                linked_at=excluded.linked_at,
                linked_by=excluded.linked_by
            """,
            (trainer_name.strip(), discord_user_id,
             int(datetime.now(timezone.utc).timestamp()), linked_by),
        )
        await conn.commit()


async def remove_link(trainer_name: str) -> bool:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        cur = await conn.execute(
            "DELETE FROM player_links WHERE trainer_name=?", (trainer_name.strip(),)
        )
        await conn.commit()
        return cur.rowcount > 0


async def get_link(trainer_name: str) -> int | None:
    async with aiosqlite.connect(LOCAL_DB) as conn:
        async with conn.execute(
            "SELECT discord_user_id FROM player_links WHERE trainer_name=?",
            (trainer_name.strip(),),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def get_all_links() -> dict[str, int]:
    """{trainer_name: discord_user_id} for all links."""
    async with aiosqlite.connect(LOCAL_DB) as conn:
        async with conn.execute(
            "SELECT trainer_name, discord_user_id FROM player_links ORDER BY trainer_name COLLATE NOCASE"
        ) as cur:
            return {name: uid for name, uid in await cur.fetchall()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_user_id(token: str) -> int | None:
    m = _USER_TOKEN_RE.match(token.strip())
    if not m:
        return None
    return int(m.group(1) or m.group(2))


async def _current_month_roster() -> list[str]:
    """Distinct trainer names from this month's circle_member_snapshots."""
    now = game_now()
    try:
        async with aiosqlite.connect(LOCAL_DB) as conn:
            async with conn.execute(
                "SELECT DISTINCT trainer_name FROM circle_member_snapshots "
                "WHERE year=? AND month=?",
                (now.year, now.month),
            ) as cur:
                names = [r[0] for r in await cur.fetchall() if r[0] and r[0].strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[Players] roster read failed: {exc}")
        return []
    return sorted({n.strip() for n in names}, key=str.lower)


# ---------------------------------------------------------------------------
# CSV import / export
# ---------------------------------------------------------------------------

def _read_csv_rows(text: str) -> list[tuple[str, str]]:
    """Return [(trainer_name, discord_id_raw)] from CSV text, header skipped."""
    rows: list[tuple[str, str]] = []
    reader = csv.reader(io.StringIO(text))
    for i, row in enumerate(reader):
        if not row or not row[0].strip():
            continue
        name = row[0].strip()
        if i == 0 and name.lower() in ("trainer_name", "trainer", "name"):
            continue
        did = row[1].strip() if len(row) > 1 else ""
        rows.append((name, did))
    return rows


async def import_csv(text: str, linked_by: int | None = None) -> dict:
    """Apply a player_links CSV. Returns a summary dict."""
    rows = _read_csv_rows(text)
    linked = 0
    skipped_blank = 0
    errors: list[str] = []
    seen_ids: dict[int, str] = {}

    for name, did_raw in rows:
        if not did_raw:
            skipped_blank += 1
            continue
        uid = _parse_user_id(did_raw)
        if uid is None:
            errors.append(f"{name}: bad Discord id {did_raw!r}")
            continue
        if uid in seen_ids and seen_ids[uid].lower() != name.lower():
            errors.append(f"{name}: Discord id {uid} already used by {seen_ids[uid]!r}")
            continue
        seen_ids[uid] = name
        await set_link(name, uid, linked_by)
        linked += 1

    return {
        "linked": linked,
        "skipped_blank": skipped_blank,
        "errors": errors,
        "total_rows": len(rows),
    }


async def export_csv(path: str | None = None) -> tuple[int, int]:
    """Write `path` from this month's roster, keeping already-linked ids.
    Returns (rows_written, already_linked)."""
    path = path or PLAYER_LINKS_CSV
    roster = await _current_month_roster()
    links = await get_all_links()
    # Case-insensitive lookup of existing links.
    links_ci = {k.lower(): v for k, v in links.items()}

    # Union: roster names + any linked names not in this month's roster.
    names = list(roster)
    lower_set = {n.lower() for n in names}
    for linked_name in links:
        if linked_name.lower() not in lower_set:
            names.append(linked_name)
    names.sort(key=str.lower)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    filled = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["trainer_name", "discord_user_id"])
        for n in names:
            uid = links_ci.get(n.lower(), "")
            if uid:
                filled += 1
            w.writerow([n, uid])
    return len(names), filled


# ---------------------------------------------------------------------------
# Command handlers  (called from main.py on_message, owner-gated)
# ---------------------------------------------------------------------------

async def handle_link_command(message: discord.Message, args: str) -> None:
    parts = args.split()
    sub = parts[0].lower() if parts else ""

    if not parts:
        links = await get_all_links()
        await message.channel.send(
            f"{len(links)} player link(s). Usage:\n"
            "`link list` · `link <@user|id> <trainer name>` · "
            "`link import` · `link export` · `unlink <trainer name>`"
        )
        return

    if sub == "list":
        links = await get_all_links()
        if not links:
            await message.channel.send("No player links yet. `link import` or `link <@user> <name>`.")
            return
        lines = [f"- **{name}** → <@{uid}>" for name, uid in links.items()]
        text = "\n".join(lines)
        for chunk in (text[i:i + 1900] for i in range(0, len(text), 1900)):
            await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        return

    if sub == "export":
        try:
            n, filled = await export_csv()
            await message.channel.send(
                f"Wrote `{PLAYER_LINKS_CSV}` — {n} player(s), {filled} already linked, "
                f"{n - filled} to fill in."
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[Players] export failed: {exc}")
            await message.channel.send(f"Export failed: {exc}")
        return

    if sub == "import":
        text: str | None = None
        src = PLAYER_LINKS_CSV
        for att in message.attachments:
            if att.filename.lower().endswith(".csv"):
                try:
                    data = await att.read()
                    text = data.decode("utf-8-sig")
                    os.makedirs(os.path.dirname(PLAYER_LINKS_CSV) or ".", exist_ok=True)
                    with open(PLAYER_LINKS_CSV, "w", encoding="utf-8", newline="") as f:
                        f.write(text)
                    src = f"attachment `{att.filename}` (saved to {PLAYER_LINKS_CSV})"
                except Exception as exc:  # noqa: BLE001
                    await message.channel.send(f"Couldn't read attachment: {exc}")
                    return
                break
        if text is None:
            if not os.path.exists(PLAYER_LINKS_CSV):
                await message.channel.send(
                    f"No `{PLAYER_LINKS_CSV}` and no CSV attached. "
                    f"Run `link export` first, fill it in, then `link import`."
                )
                return
            with open(PLAYER_LINKS_CSV, encoding="utf-8-sig") as f:
                text = f.read()

        summary = await import_csv(text, linked_by=message.author.id)
        msg = (
            f"Imported from {src}:\n"
            f"- linked/updated: **{summary['linked']}**\n"
            f"- rows with no Discord id (skipped): {summary['skipped_blank']}"
        )
        if summary["errors"]:
            errs = "\n".join(f"  • {e}" for e in summary["errors"][:20])
            more = f"\n  …and {len(summary['errors']) - 20} more" if len(summary["errors"]) > 20 else ""
            msg += f"\n- errors ({len(summary['errors'])}):\n{errs}{more}"
        await message.channel.send(msg)
        return

    # link <@user|id> <trainer name...>
    uid = _parse_user_id(parts[0])
    if uid is None:
        # maybe: link <trainer name...>  -> show that player's link
        name = args.strip()
        linked = await get_link(name)
        if linked:
            await message.channel.send(
                f"**{name}** → <@{linked}>",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await message.channel.send(
                f"**{name}** isn't linked. `link <@user|id> {name}` to link them."
            )
        return

    name = " ".join(parts[1:]).strip()
    if not name:
        await message.channel.send("Usage: `link <@user|id> <trainer name>`")
        return

    await set_link(name, uid, linked_by=message.author.id)
    await message.channel.send(
        f"Linked **{name}** → <@{uid}>.",
        allowed_mentions=discord.AllowedMentions.none(),
    )
    logger.info(f"[Players] {message.author.id} linked {name!r} -> {uid}")


async def handle_unlink_command(message: discord.Message, args: str) -> None:
    name = args.strip()
    if not name:
        await message.channel.send("Usage: `unlink <trainer name>`")
        return
    if await remove_link(name):
        await message.channel.send(f"Unlinked **{name}**.")
        logger.info(f"[Players] {message.author.id} unlinked {name!r}")
    else:
        await message.channel.send(f"**{name}** wasn't linked.")
