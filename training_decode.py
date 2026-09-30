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
    TRAINING_CHAIN_PROGRESS_JSON,
    SKILL_NAMES_JSON,
    UMA_TOOLS_CHARA_ICON_DIR,
    UMA_TOOLS_BUILD_PLANNER_CARDS_JSON,
    UMA_TOOLS_ICON_DIR,
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


def _chain_progress_table() -> dict:
    """{ storyId (str) -> { position, total, supportCardId, supportCharaId } } — see
    tests/extract_chain_progress.py. Only "chain" story_ids are present (a standalone
    side event has no position/total, see the plan doc's chain-event investigation)."""
    return _load(TRAINING_CHAIN_PROGRESS_JSON) or {}


def chain_progress(story_id: int) -> Optional[dict]:
    """{"position": N, "total": M} for a chain-event storyId, or None if it's not a
    chain event (a standalone side event) or the table hasn't caught up to it yet."""
    row = _chain_progress_table().get(str(story_id))
    if row is None:
        return None
    return {"position": row["position"], "total": row["total"]}


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


# training_progress's `statGains[].targetType` — confirmed via real per-turn capture
# (plan doc, Event 4), cross-checked against dozens of real turn transitions, not
# pattern-matched from one example. Distinct key space from the factor/spark `type`
# table above — same integers, unrelated meaning.
_TARGET_TYPE_STAT_NAMES = {
    1: "Speed", 2: "Stamina", 3: "Power", 4: "Guts", 5: "Wit",
    6: "SP Bonus", 10: "Vital", 30: "Skill Pts",
}


# training_progress's `performanceGains[].performanceType` (Live scenario only) —
# confirmed via direct player knowledge + partial data cross-check (plan doc, Event 4).
# "Composure" for stat 5 is now user-confirmed directly, resolving an earlier back-and-
# forth in this doc over "Compassion"/"Composture"/"Composure" guesses.
_PERFORMANCE_TYPE_NAMES = {1: "Dance", 2: "Passion", 3: "Vocal", 4: "Visual", 5: "Composure"}


def performance_type_display(performance_type: int) -> str:
    """Display name for a performanceGains `performanceType`, or the raw number
    (prefixed) if unmapped."""
    return _PERFORMANCE_TYPE_NAMES.get(performance_type, f"Unknown ({performance_type})")


# performanceType -> emoji_mapping.json's "performance_token" key. Note this uses "co"
# for type 5, not "me" — two icons exist for that stat (uma.guide's gl-tokens set
# labels them "Me" and "Co"), and "co"/"Composure" is the confirmed correct display
# term, see performance_type_display()'s note above.
_PERFORMANCE_TYPE_EMOJI_KEYS = {1: "da", 2: "pa", 3: "vo", 4: "vi", 5: "co"}


def _performance_token_table() -> dict:
    """{ "da"/"pa"/"vo"/"vi"/"me"/"co" -> Discord emoji markup } — see
    tests/upload_misc_icon_emojis.py."""
    return _load(EMOJI_MAPPING_JSON).get("performance_token", {})


def performance_type_emoji(performance_type: int) -> Optional[str]:
    """Discord emoji for a performanceGains `performanceType`. None if unmapped."""
    key = _PERFORMANCE_TYPE_EMOJI_KEYS.get(performance_type)
    return _performance_token_table().get(key) if key else None


def target_type_display(target_type: int) -> str:
    """Display name for a statGains/performanceGains `targetType`, or the raw number
    (prefixed) if it's not one of the confirmed values — e.g. an unmapped >=101 bond
    targetId shouldn't be confused with this table, that's a different ID space."""
    return _TARGET_TYPE_STAT_NAMES.get(target_type, f"Unknown ({target_type})")


def stat_emoji(stat_key: str) -> Optional[str]:
    """Discord emoji for a training_end `stats` key (e.g. "speed", "wiz"). None if unmapped."""
    return _stat_table().get(_STAT_KEY_ALIASES.get(stat_key, stat_key))


def _stat_rainbow_table() -> dict:
    """{ "speed"/"stamina"/"power"/"guts"/"wits" -> Discord emoji markup } — the
    rainbow-training badge per stat, see tests/upload_misc_icon_emojis.py."""
    return _load(EMOJI_MAPPING_JSON).get("stat_rainbow", {})


def stat_rainbow_emoji(stat_key: str) -> Optional[str]:
    """Discord emoji for a rainbow-training badge (e.g. "speed", "wiz"). None if unmapped."""
    return _stat_rainbow_table().get(_STAT_KEY_ALIASES.get(stat_key, stat_key))


def _mood_table() -> dict:
    """{ "0_left".."4_left"/"0_right".."4_right" -> Discord emoji markup } — each of the
    5 in-game motivation badges split left/right, see tests/upload_misc_icon_emojis.py."""
    return _load(EMOJI_MAPPING_JSON).get("mood", {})


def mood_emojis(mood_value: int) -> Optional[str]:
    """Combined left+right emoji pair for chara_info.motivation. The payload's mood
    value is 1-5; our icon set (and emoji_mapping.json's "mood" keys) is 0-4 — user-
    confirmed the icon index is simply `mood_value - 1`. None if unmapped (out of
    range, or the emoji table hasn't got that index for some reason)."""
    idx = mood_value - 1
    table = _mood_table()
    left = table.get(f"{idx}_left")
    right = table.get(f"{idx}_right")
    if left is None or right is None:
        return None
    return f"{left}{right}"


def _support_card_type_table() -> dict:
    """{ supportCardId (str) -> { type: 0-6, name: [jp, en], rarity, event, hints } } —
    uma-tools' own card-browser data (build-planner/cards.json), not something
    training_data_sync.py writes. `type` is 0-indexed in the same order as
    emoji_mapping.json's "stat" category (utx_ico_obtain_00..06): Speed, Stamina,
    Power, Guts, Wit, Pal, Group — confirmed against build-planner's own type-filter
    UI (7 buttons, same icon set) and cross-checked by name against well-known
    base-game SSRs of each type (e.g. Silence Suzuka=Speed, Gold Ship=Stamina,
    Special Week=Guts, Agnes Tachyon=Wit, Tazuna Hayakawa=Pal, "Team Sirius"=Group)."""
    return _load(UMA_TOOLS_BUILD_PLANNER_CARDS_JSON) or {}


_SUPPORT_CARD_TYPE_NAMES = ["Speed", "Stamina", "Power", "Guts", "Wit", "Pal", "Group"]

# emoji_mapping.json's "stat" keys don't spell these quite the same ("wits", not
# "Wit") — same aliasing idea as _STAT_KEY_ALIASES above, just the reverse direction.
_SUPPORT_CARD_TYPE_EMOJI_KEYS = {
    "Speed": "speed", "Stamina": "stamina", "Power": "power", "Guts": "guts",
    "Wit": "wits", "Pal": "pal", "Group": "group",
}


def support_card_type(support_card_id: int) -> Optional[str]:
    """"Speed"/"Stamina"/"Power"/"Guts"/"Wit"/"Pal"/"Group" for a supportCardId, or
    None if it's not in uma-tools' card list (new content the clone hasn't synced yet)."""
    row = _support_card_type_table().get(str(support_card_id))
    if row is None:
        return None
    try:
        return _SUPPORT_CARD_TYPE_NAMES[row["type"]]
    except (KeyError, IndexError, TypeError):
        return None


def support_card_name(support_card_id: int) -> Optional[str]:
    """English display name for a supportCardId, from the same build-planner table as
    support_card_type() above. None if unresolved (degrade gracefully, as elsewhere)."""
    row = _support_card_type_table().get(str(support_card_id))
    if row is None:
        return None
    name = row.get("name")
    return name[1] if isinstance(name, list) and len(name) > 1 else None


def support_card_type_emoji(support_card_id: int) -> Optional[str]:
    """Discord emoji for a supportCardId's training type. Reuses emoji_mapping.json's
    existing "stat" entries — the same utx_ico_obtain_NN icon set uma-tools itself
    uses for this exact type, already uploaded for the training-stats line."""
    name = support_card_type(support_card_id)
    if name is None:
        return None
    return _stat_table().get(_SUPPORT_CARD_TYPE_EMOJI_KEYS[name])


# index -> uma-tools' own type-icon filename, same icon set as the emoji above but as
# a raw PNG (for compositing directly onto the support-card strip image, rather than
# rendering as embed text).
_SUPPORT_CARD_TYPE_ICON_FILENAMES = [f"utx_ico_obtain_{i:02d}.png" for i in range(7)]


def support_card_type_icon_path(support_card_id: int) -> Optional[str]:
    """Full path to the training-type badge PNG for a supportCardId, or None if the
    card is unresolved or the icon file is missing."""
    row = _support_card_type_table().get(str(support_card_id))
    if row is None:
        return None
    try:
        filename = _SUPPORT_CARD_TYPE_ICON_FILENAMES[row["type"]]
    except (KeyError, IndexError, TypeError):
        return None
    path = os.path.join(UMA_TOOLS_ICON_DIR, filename)
    return path if os.path.exists(path) else None


def spark_star_emojis(level: int) -> str:
    """Renders a spark's star level as its numeral plus a plain Unicode star.

    Went through two custom-Discord-emoji designs first (3 repeated stars,
    then 1 star + numeral) — both used emoji_mapping.json's "star" entries,
    45+ raw characters each, and a well-factored character can have 15-25+
    sparks, which blew past Discord's 1024-char field value limit in
    production. A plain "⭐" is a single character and carries the same
    information, so chunking (see _add_chunked_field) should now be rare
    rather than near-guaranteed for any well-factored character.
    """
    return f"{level}⭐"


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


# training_progress's `chara_effect_id_array` (statusEffects) — confirmed via
# master.mdb's text_data, category 142 (id 7 = "Fast Learner", user-confirmed against
# real gameplay). Hardcoded rather than loaded from a JSON file like the factor/skill/
# character tables above: unlike those, this has no existing sync source (it isn't
# published anywhere uma.moe/GitHub-hosted already), and it's a small, rarely-changing
# enum — new status effects come with new scenarios, not weekly content drops — so a
# static table is the pragmatic choice unless that turns out wrong.
_STATUS_EFFECT_NAMES = {
    1: "Night Owl", 2: "Slacker", 3: "Skin Outbreak", 4: "Slow Metabolism",
    5: "Migraine", 6: "Practice Poor", 7: "Fast Learner", 8: "Charming ○",
    9: "Hot Topic", 10: "Practice Perfect ○", 11: "Practice Perfect ◎",
    12: "Under the Weather", 13: "Shining Brightly", 14: "Fan Promise (Hokkaido)",
    15: "Fan Promise (Hokuto)", 16: "Fan Promise (Nakayama)", 17: "Fan Promise (Kansai)",
    18: "Fan Promise (Kokura)", 19: "Not Ready", 20: "Legs of Glass",
    21: "Ominous Portent", 22: "Idol's Promise (Kawasaki)", 23: "Hero's Brilliance",
    24: "Bud Longing for Spring", 100: "Pure Passion: Team Sirius",
    101: "Pure Passion: Heirs to the Throne", 102: "Pure Passion: Progenitors and Guides",
}


def status_effect_name(effect_id: int) -> Optional[str]:
    """Name for a chara_info.chara_effect_id_array entry, or None if unresolved
    (new content this table hasn't caught up to — degrade gracefully, as elsewhere)."""
    return _STATUS_EFFECT_NAMES.get(effect_id)


def base_character_name(base_id: int) -> Optional[str]:
    """Name for a *bare* base character id (no outfit digit) — e.g. training_progress's
    guest-partner ids (targetId >=101 that aren't the 101-106 unidentified range —
    confirmed to be the same base-id scheme as cardId's base half, just without an
    outfit, see plan doc's Event 4 "Run separation"/bonds notes). Not the same as
    character_display(), which expects a full cardId and also resolves an outfit."""
    row = _character_names_table().get(str(base_id))
    return row.get("name") if row else None


# bonds[]/facilities[]' targetId 101-106 — previously flagged "we don't know what this
# is" in the plan doc, now confirmed: targetId - 100 + 9000 = chara_id (e.g. 102 -> 9002),
# the six trainer/assistant "Pal"-type characters. Verified against master.mdb text_data
# directly (102 -> "Yayoi Akikawa", 103 -> "Etsuko Otonashi", user-confirmed against real
# gameplay) plus the remaining four resolved the same way.
_BOND_TRAINER_NAMES = {
    101: "Tazuna Hayakawa", 102: "Yayoi Akikawa", 103: "Etsuko Otonashi",
    104: "Aoi Kiryuin", 105: "Sasami Anshinzawa", 106: "Riko Kashimoto",
}


def bond_partner_name(target_id: int, deck_positions: Optional[list] = None) -> Optional[str]:
    """Resolves a bonds[]/facilities[] targetId to a display name.
    - 1-6: deck position — needs `deck_positions` (the 6 supportCardIds in order,
      position 6 being friendSupportCardId); None (not 0) if omitted, so callers that
      don't have the deck handy get an honest "can't resolve" rather than a wrong guess.
    - 101-106: the six trainer/assistant staff (see _BOND_TRAINER_NAMES above).
    - anything else: a guest character's bare base id (see base_character_name)."""
    if 1 <= target_id <= 6:
        if not deck_positions:
            return None
        cid = deck_positions[target_id - 1]
        return support_card_name(cid) or f"Card {cid}"
    if target_id in _BOND_TRAINER_NAMES:
        return _BOND_TRAINER_NAMES[target_id]
    return base_character_name(target_id)


# ---------------------------------------------------------------------------
# Turn -> in-game calendar date (training_progress's `turn` field)
#
# Confirmed turn-by-turn against the real in-game calendar (not derived/guessed):
#   - Turns 13-24: Junior Year, a half year (career starts mid-year) — Jul-E through
#     Dec-L, 12 turns / 6 months.
#   - Turns 25-48: Classic Year, a full year — Jan-E through Dec-L, 24 turns.
#   - Turns 49-72: Senior Year, a full year — Jan-E through Dec-L, 24 turns.
# Concert checkpoints (see the plan doc's Event 4 / single_mode_live_live_data) land
# on turns 24/36/48/60/72 — one at Junior Year's end (it's only a half year), then
# one mid-year + one year-end for each of Classic and Senior, with 72 doubling as
# the finale.
#
# Turns 1-12 (pre-debut) and anything past 72 have no confirmed date — turn_to_date
# returns None for those rather than guessing, same degrade-gracefully convention as
# the rest of this module.
# ---------------------------------------------------------------------------

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_TURN_YEAR_BLOCKS = [
    (13, 24, 6, "Junior Year"),   # half year, starts July (index 6)
    (25, 48, 0, "Classic Year"),  # full year, starts January
    (49, 72, 0, "Senior Year"),   # full year, starts January
]


def turn_to_date(turn: int) -> Optional[dict]:
    """{"date": "Late Dec", "year": "Junior Year", "label": "Late Dec, Junior Year"}
    for a training_progress `turn` number, or None if it's outside the confirmed
    13-72 range (pre-debut or uncharted post-finale)."""
    for start, end, start_month_idx, year_name in _TURN_YEAR_BLOCKS:
        if start <= turn <= end:
            offset = turn - start
            month_idx = (start_month_idx + offset // 2) % 12
            half = "Early" if offset % 2 == 0 else "Late"
            date = f"{half} {_MONTHS[month_idx]}"
            return {"date": date, "year": year_name, "label": f"{date}, {year_name}"}
    return None


def skill_display(skill_id: int) -> dict:
    entry = _skill_names_table().get(str(skill_id))
    if entry is None:
        return {"name": f"Unknown skill ({skill_id})", "resolved": False}
    if isinstance(entry, list):
        # Normally [japanese_name, english_name], but a fair number of entries
        # (e.g. "100161": ["Shadow Break"]) only carry one element — take it
        # rather than falling through to str(entry), which stringified the
        # whole list literally (e.g. "['Shadow Break']").
        name = entry[1] if len(entry) > 1 else (entry[0] if entry else str(skill_id))
    else:
        name = str(entry)
    return {"name": name, "resolved": True}
