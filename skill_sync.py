"""
skill_sync.py

Downloads uma-skill-tools and uma-tools JSON data files from GitHub to disk.
Used by skills_module.py for the /skill chart and autocomplete.

    sync_if_stale(bot)  — downloads if any file is older than MAX_AGE_H hours;
                          DMs main owner only when an update actually runs
    sync_now(bot)       — force re-download regardless of age;
                          always DMs main owner with the result
"""

import logging
import os
import time

import aiohttp
import discord

from global_config import (
    SKILL_DATA_JSON,
    COURSE_DATA_JSON,
    COURSE_LABELS_JSON,
    TRACK_NAMES_JSON,
    SKILL_NAMES_JSON,
)

logger = logging.getLogger("skill_sync")

MAX_AGE_H = 24  # re-download if oldest file exceeds this age

# (remote URL, local destination path)
_REMOTE_FILES = [
    (
        "https://raw.githubusercontent.com/alpha123/uma-skill-tools/master/data/skill_data.json",
        SKILL_DATA_JSON,
    ),
    (
        "https://raw.githubusercontent.com/alpha123/uma-skill-tools/master/data/course_data.json",
        COURSE_DATA_JSON,
    ),
    (
        "https://raw.githubusercontent.com/alpha123/uma-skill-tools/master/data/tracknames.json",
        TRACK_NAMES_JSON,
    ),
    (
        "https://raw.githubusercontent.com/alpha123/uma-tools/master/umalator-global/skillnames.json",
        SKILL_NAMES_JSON,
    ),
    (
        "https://raw.githubusercontent.com/alpha123/uma-tools/master/umalator-global/course_data.json",
        COURSE_LABELS_JSON,
    ),
]

# Same user who receives the restart DM (main.py:87)
_MAIN_OWNER_ID = 680653908259110914


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _oldest_mtime() -> float:
    """Return the oldest mtime across all target files, or 0.0 if any are missing."""
    times = []
    for _, dest in _REMOTE_FILES:
        if not os.path.exists(dest):
            return 0.0
        times.append(os.path.getmtime(dest))
    return min(times) if times else 0.0


async def _fetch_all() -> tuple[bool, str]:
    """Download every file atomically. Returns (success, status_message)."""
    try:
        async with aiohttp.ClientSession() as session:
            for url, dest in _REMOTE_FILES:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    r.raise_for_status()
                    data = await r.read()
                tmp = dest + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dest)   # atomic swap — never leaves a partial file
                logger.info(f"[SkillSync] Updated {dest} ({len(data):,} bytes)")
        return True, "uma-skill-tools data synced successfully."
    except Exception as exc:
        logger.error(f"[SkillSync] Download failed: {exc}")
        return False, f"uma-skill-tools sync failed: {exc}"


async def _dm_owner(bot: discord.Client, text: str) -> None:
    try:
        user = await bot.fetch_user(_MAIN_OWNER_ID)
        await user.send(text)
    except Exception as exc:
        logger.error(f"[SkillSync] Could not DM owner: {exc}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def sync_if_stale(bot: discord.Client) -> None:
    """
    Download all files if any are older than MAX_AGE_H hours (or missing).
    Silent when data is already fresh; DMs main owner when an update runs.
    """
    age_h = (time.time() - _oldest_mtime()) / 3600
    if age_h < MAX_AGE_H:
        logger.info(f"[SkillSync] Data is {age_h:.1f}h old — no sync needed")
        return
    logger.info(f"[SkillSync] Data is {age_h:.1f}h old — syncing")
    ok, msg = await _fetch_all()
    await _dm_owner(bot, msg)


async def sync_now(bot: discord.Client) -> str:
    """
    Force-download all files regardless of age.
    DMs main owner and returns the status message string.
    """
    logger.info("[SkillSync] Forced sync")
    ok, msg = await _fetch_all()
    await _dm_owner(bot, msg)
    return msg
