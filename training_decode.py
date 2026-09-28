"""
training_decode.py

Decoding + name-lookup helpers for HorseACT training-event payloads
(training_start / training_end from horseact_network_probe).

ID schemes (see Z:\\Claude Projects\\HorseACT RE\\api_contract_plan.md for the full
verification writeup):

- factorId = base_id followed by a trailing star-level digit, e.g. 2003701 ->
  base 200370, level 1. Strip the last digit for base_id.
- cardId = base_character_id * 100 + outfit_index, e.g. 101601 -> base 1016,
  outfit "01".
- skillId is opaque — looked up directly, no decoding.

Every *_display() function degrades to showing the raw numeric ID instead of
erroring when a lookup misses (new content the daily sync hasn't caught up to
yet) — see "Missing name-table lookups degrade gracefully" in the plan doc.
"""

import json
import os
from typing import Optional

from global_config import (
    TRAINING_FACTORS_JSON,
    TRAINING_CHARACTER_NAMES_JSON,
    SKILL_NAMES_JSON,
    UMA_TOOLS_CHARA_ICON_DIR,
    EMOJI_MAPPING_JSON,
)

# ---------------------------------------------------------------------------
# ID decoding
# ---------------------------------------------------------------------------

def decode_factor_id(factor_id: int) -> tuple[int, int]:
    """Split a factorId into (base_id, star_level)."""
    s = str(factor_id)
    return int(s[:-1]), int(s[-1])


def decode_card_id(card_id: int) -> tuple[int, str]:
    """Split a cardId into (base_character_id, outfit_index) — outfit is a 2-digit string."""
    base_id = card_id // 100
    outfit_index = str(card_id % 100).zfill(2)
    return base_id, outfit_index


# ---------------------------------------------------------------------------
# Cached table loading (reloads if the underlying file changes on disk —
# training_data_sync.py / skill_sync.py write these with an atomic swap)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, dict]] = {}


def _load(path: str) -> dict:
    """Load and cache a JSON file's contents, keyed by mtime so a fresh sync is picked up."""
    if not os.path.exists(path):
        return {}
    mtime = os.path.getmtime(path)
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    _cache[path] = (mtime, data)
    return data


def _factor_table() -> dict[str, dict]:
    """{ baseId (str) -> { text, type } }"""
    raw = _load(TRAINING_FACTORS_JSON)
    if isinstance(raw, list):
        return {str(row["id"]): row for row in raw}
    return raw or {}


def _character_names_table() -> dict:
    """{ baseCharacterId (str) -> { name, skins: { outfitIndex: label } } }"""
    return _load(TRAINING_CHARACTER_NAMES_JSON) or {}


def _skill_names_table() -> dict:
    """{ skillId (str) -> [jp_name, en_name] | bare_name_string }"""
    return _load(SKILL_NAMES_JSON) or {}


def _status_rank_table() -> dict:
    """{ "00".."297" -> Discord emoji markup } — see tests/upload_misc_icon_emojis.py."""
    return _load(EMOJI_MAPPING_JSON).get("status_rank", {})


def status_rank_emoji(rank: int) -> Optional[str]:
    """Discord emoji for a 1-indexed training rank (the payload's `rank` field).

    The icon set is 0-indexed (key "00" = rank 1), hence rank - 1. Returns None
    (not a placeholder) if unmapped, so callers can fall back to the plain number.
    """
    return _status_rank_table().get(f"{rank - 1:02d}")


def _stat_table() -> dict:
    """{ "speed"/"stamina"/"power"/"guts"/"wits"/"pal"/"group" -> Discord emoji markup }."""
    return _load(EMOJI_MAPPING_JSON).get("stat", {})


# training_end's `stats` dict spells this "wiz"; emoji_mapping.json spells it "wits".
_STAT_KEY_ALIASES = {"wiz": "wits"}


def stat_emoji(stat_key: str) -> Optional[str]:
    """Discord emoji for a training_end `stats` key (e.g. "speed", "wiz"). None if unmapped."""
    return _stat_table().get(_STAT_KEY_ALIASES.get(stat_key, stat_key))


# ---------------------------------------------------------------------------
# Display helpers — each returns a dict that's always safe to render,
# falling back to the raw ID when the name table doesn't have an entry.
# ---------------------------------------------------------------------------

def factor_display(factor_id: int) -> dict:
    base_id, level = decode_factor_id(factor_id)
    row = _factor_table().get(str(base_id))
    if row is None:
        return {"name": f"Unknown factor ({factor_id})", "type": None, "level": level, "resolved": False}
    return {"name": row.get("text", str(base_id)), "type": row.get("type"), "level": level, "resolved": True}


def character_display(card_id: int) -> dict:
    base_id, outfit_index = decode_card_id(card_id)
    row = _character_names_table().get(str(base_id))
    if row is None:
        return {"name": f"Unknown character ({card_id})", "outfit": None, "resolved": False}
    name = row.get("name", str(base_id))
    outfit_label: Optional[str] = None
    if outfit_index != "01":  # "01" is always "Original" — omit from display
        skins = row.get("skins") or {}
        outfit_label = skins.get(outfit_index)
    return {"name": name, "outfit": outfit_label, "resolved": True}


def character_icon_path(card_id: int) -> Optional[str]:
    """Full path to the trained-character portrait PNG for a cardId, or None if missing."""
    base_id, _outfit_index = decode_card_id(card_id)
    path = os.path.join(UMA_TOOLS_CHARA_ICON_DIR, f"trained_chr_icon_{base_id}_{card_id}_02.png")
    return path if os.path.exists(path) else None


def skill_display(skill_id: int) -> dict:
    entry = _skill_names_table().get(str(skill_id))
    if entry is None:
        return {"name": f"Unknown skill ({skill_id})", "resolved": False}
    name = entry[1] if isinstance(entry, list) and len(entry) > 1 else str(entry)
    return {"name": name, "resolved": True}
