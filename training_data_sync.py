"""
training_data_sync.py

Downloads uma.moe's global factor/character name tables to disk, for decoding
HorseACT training-event payloads (see training_decode.py). Same daily-refresh,
atomic-swap shape as skill_sync.py, but a separate script since the source set
is different (uma.moe here vs. GitHub raw files for skills — skill_sync.py's
existing SKILL_DATA_JSON / SKILL_NAMES_JSON are reused as-is, not re-fetched).

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
    TRAINING_FACTORS_JSON,
    TRAINING_CHARACTERS_JSON,
    TRAINING_CHARACTER_NAMES_JSON,
)

logger = logging.getLogger("training_data_sync")

MAX_AGE_H = 24  # re-download if oldest file exceeds this age

# (remote URL, local destination path)
# Despite the ".gz" extension, a plain GET without requesting gzip encoding
# returns decompressed JSON directly — no decompression step needed here.
_REMOTE_FILES = [
    ("https://uma.moe/resources/current/factors.json.gz", TRAINING_FACTORS_JSON),
    ("https://uma.moe/resources/current/character.json.gz", TRAINING_CHARACTERS_JSON),
    ("https://uma.moe/resources/current/character_names.json.gz", TRAINING_CHARACTER_NAMES_JSON),
]

# Same user who receives the restart DM (main.py) / skill_sync's owner DM
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
                logger.info(f"[TrainingDataSync] Updated {dest} ({len(data):,} bytes)")
        return True, "Training factor/character data synced successfully."
    except Exception as exc:
        logger.error(f"[TrainingDataSync] Download failed: {exc}")
        return False, f"Training data sync failed: {exc}"


async def _dm_owner(bot: discord.Client, text: str) -> None:
    try:
        user = await bot.fetch_user(_MAIN_OWNER_ID)
        await user.send(text)
    except Exception as exc:
        logger.error(f"[TrainingDataSync] Could not DM owner: {exc}")


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
        logger.info(f"[TrainingDataSync] Data is {age_h:.1f}h old — no sync needed")
        return
    logger.info(f"[TrainingDataSync] Data is {age_h:.1f}h old — syncing")
    ok, msg = await _fetch_all()
    await _dm_owner(bot, msg)


async def sync_now(bot: discord.Client) -> str:
    """
    Force-download all files regardless of age.
    DMs main owner and returns the status message string.
    """
    logger.info("[TrainingDataSync] Forced sync")
    ok, msg = await _fetch_all()
    await _dm_owner(bot, msg)
    return msg
