"""
parent_module.py

/parent slash command — shows recommended inherited skills by running style
for a given Champions Meeting course.

Public API:
    load_parent_data()                        → call once from on_ready
    start_background_tasks()                  → call once from on_ready (async)
    handle_parent_interaction(interaction, cm_value)
    autocomplete_cm(interaction, current) -> list[Choice]
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import tasks

import cm_module
import skills_module
from global_config import (
    GT_GLOBAL_CHARS_JSON,
    SHARED_GAMETORA_DB,
    SKILL_DATA_JSON,
    SKILL_NAMES_JSON,
    SKILLS_DB,
)
from skill_chart import (
    _COND_RE,
    _RANDOM_FIELDS,
    accel_verdict,
    evaluate_trigger,
    format_effects,
    phase_start,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NHORSE = 9  # 9-horse Champions Meeting field

DISPLAY_STYLES = ("front", "pace", "late", "end", "all")

STYLE_NAME = {
    "front": "Front Runner",
    "pace":  "Pace Chaser",
    "late":  "Late Surger",
    "end":   "End Closer",
    "all":   "Generally Good",
}
STYLE_COLOR = {
    "front": 0x3498DB,
    "pace":  0x2ECC71,
    "late":  0xF1C40F,
    "end":   0xE74C3C,
    "all":   0x99AAB5,
}

STYLE_RANGES = {
    "front": (1, 2),
    "pace":  (2, 4),
    "late":  (5, 8),
    "end":   (6, 9),
}

ALL_STYLES = (*STYLE_RANGES,)

_BAD_LABELS      = ("Horrible", "Bad")
_MIN_VEL_MOD     = 0.15
_MIN_LSV_MOD     = 0.20
_ASSUMED_SPEED   = 20.0   # m/s assumed horse speed in midrace
_SC_WIN_END_PROX = 150.0  # window end must be within this many metres of p2 (or past it)
_SC_WIN_START_PROX = 200.0  # window start must be within this many metres of p2

_OVERTAKE_FIELDS = frozenset({
    "is_overtake",
    "change_order_up_end_after",
    "overtake_target_no_order_up_time",
    "order_rate_in20_continue",
})

_LATE_RACE_FIELDS = frozenset({
    "is_finalcorner",
    "is_last_straight",
    "is_lastspurt",
})

_EMPTY_EMOJI_URL  = "https://cdn.discordapp.com/emojis/1535215534730387506.webp?size=512&animated=true"
_MAX_EMBED_CHARS  = 4096
_MAX_TOTAL_CHARS  = 6000
_MAX_EMBEDS_PER_MSG = 10

_COND_FIELD_RE = re.compile(r'([a-z_][a-z0-9_]*)\s*(?:>=|<=|==|!=|>|<)')
_SKILL_DEP_RE  = re.compile(r'activate_count_\w+\s*>=\s*\d+')

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_skill_data:      dict      = {}
_skill_names:     dict      = {}
# sid (9XXXXX) → (character_name, icon_url)
_char_info:       dict[str, tuple[str, str]] = {}
# skills.db character_name → uma_jp_data.db character_id string
_char_name_to_id: dict[str, str] = {}
_base_char_ids:   set[str]  = set()
_data_loaded:     bool      = False

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_parent_data() -> None:
    """Load all static data needed by /parent.  Call once from on_ready."""
    global _skill_data, _skill_names, _char_info, _char_name_to_id, _base_char_ids, _data_loaded
    if _data_loaded:
        return

    # skill_data.json / skillnames.json
    for path, label in [(SKILL_DATA_JSON, "skill_data.json"), (SKILL_NAMES_JSON, "skillnames.json")]:
        if not os.path.exists(path):
            logger.warning(f"[Parent] {label} not found at {path} — /parent will be unavailable")
            return
    with open(SKILL_DATA_JSON, encoding="utf-8") as f:
        _skill_data = json.load(f)
    with open(SKILL_NAMES_JSON, encoding="utf-8") as f:
        _skill_names = json.load(f)

    # GT global character IDs
    _base_char_ids = _load_gt_global_char_ids(GT_GLOBAL_CHARS_JSON)
    if _base_char_ids:
        logger.info(f"[Parent] Loaded {len(_base_char_ids)} base eligible characters")
    else:
        logger.warning(f"[Parent] {GT_GLOBAL_CHARS_JSON} not found — run tests/scrape_global_chars.py or wait for weekly refresh")

    # Character info from skills.db (character_name + icon_url for each 9XXXXX skill)
    all_9sids = [sid for sid in _skill_data if sid.startswith("9")]
    _char_info = _load_character_info(all_9sids, SKILLS_DB)
    logger.info(f"[Parent] Loaded character info for {len(_char_info)} inherited skills")

    # character_name → character_id mapping via uma_jp_data.db
    unique_names = list({info[0] for info in _char_info.values() if info[0]})
    _char_name_to_id = _build_char_name_to_id(unique_names, SHARED_GAMETORA_DB)
    logger.info(f"[Parent] Resolved {len(_char_name_to_id)}/{len(unique_names)} character names to IDs")

    _data_loaded = True


def _load_gt_global_char_ids(json_path: str) -> set[str]:
    if not os.path.exists(json_path):
        return set()
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        return {c["id"] for c in data.get("characters", [])}
    except Exception as exc:
        logger.warning(f"[Parent] gt_global_chars.json load failed: {exc}")
        return set()


def _load_character_info(sids: list[str], db_path: str) -> dict[str, tuple[str, str]]:
    """
    Query skills.db for (character_name, icon_url) for each 9XXXXX sid.
    Converts 9XXXXX → 1XXXXX for the DB lookup; results are keyed by 9XXXXX.
    """
    if not sids or not os.path.exists(db_path):
        return {}
    conv: dict[int, str] = {}
    for sid in sids:
        if sid.startswith("9") and len(sid) >= 2:
            conv[int("1" + sid[1:])] = sid
    if not conv:
        return {}
    try:
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(conv))
            rows = db.execute(
                f"SELECT skill_id, character_name, icon_url FROM skills WHERE skill_id IN ({placeholders})",
                list(conv.keys()),
            ).fetchall()
        return {
            conv[row["skill_id"]]: (row["character_name"] or "", row["icon_url"] or "")
            for row in rows
            if row["skill_id"] in conv
        }
    except Exception as exc:
        logger.warning(f"[Parent] character info lookup failed: {exc}")
        return {}


def _build_char_name_to_id(char_names: list[str], jp_db_path: str) -> dict[str, str]:
    """
    Map skills.db character_name strings to uma_jp_data.db character_id strings.
    Handles the '(Original)' suffix mismatch for first-version characters.
    """
    if not char_names or not os.path.exists(jp_db_path):
        return {}
    result: dict[str, str] = {}
    unresolved: list[str] = []
    try:
        with sqlite3.connect(jp_db_path) as db:
            db.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(char_names))
            rows = db.execute(
                f"SELECT character_id, name FROM characters WHERE name IN ({placeholders})",
                char_names,
            ).fetchall()
        found = {row["name"]: row["character_id"] for row in rows}
        for name in char_names:
            if name in found:
                result[name] = found[name]
            elif name.endswith(" (Original)"):
                unresolved.append(name)
        if unresolved:
            stripped = [re.sub(r"\s*\(Original\)\s*$", "", n) for n in unresolved]
            with sqlite3.connect(jp_db_path) as db:
                db.row_factory = sqlite3.Row
                placeholders = ",".join("?" * len(stripped))
                rows = db.execute(
                    f"SELECT character_id, name FROM characters WHERE name IN ({placeholders})",
                    stripped,
                ).fetchall()
            strip_found = {row["name"]: row["character_id"] for row in rows}
            for orig_name, plain_name in zip(unresolved, stripped):
                if plain_name in strip_found:
                    result[orig_name] = strip_found[plain_name]
    except Exception as exc:
        logger.warning(f"[Parent] char_name_to_id lookup failed: {exc}")
    return result

# ---------------------------------------------------------------------------
# Weekly background task — refresh GT global character list
# ---------------------------------------------------------------------------

@tasks.loop(hours=72)
async def _refresh_chars_task() -> None:
    """Reload _base_char_ids from the JSON file written by the cron job."""
    global _base_char_ids
    new_ids = _load_gt_global_char_ids(GT_GLOBAL_CHARS_JSON)
    if new_ids:
        _base_char_ids = new_ids
        logger.info(f"[Parent] Reloaded {len(_base_char_ids)} GT global chars from file")


async def start_background_tasks() -> None:
    _refresh_chars_task.start()


async def refresh_char_ids() -> str:
    """
    Manually trigger a GT global character scrape and reload _base_char_ids.
    Returns a status string suitable for sending as a Discord message.
    """
    global _base_char_ids
    try:
        from scrape_global_chars import scrape_visible_characters
        chars = await scrape_visible_characters()
        payload = {"scraped_at": int(time.time()), "characters": chars}
        os.makedirs(os.path.dirname(GT_GLOBAL_CHARS_JSON) or ".", exist_ok=True)
        with open(GT_GLOBAL_CHARS_JSON, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        _base_char_ids = {c["id"] for c in chars}
        logger.info(f"[Parent] Manual GT chars refresh: {len(_base_char_ids)} characters")
        return f"GT global character list refreshed: {len(chars)} characters loaded."
    except Exception as exc:
        logger.error(f"[Parent] Manual GT chars refresh failed: {exc}")
        return f"GT global character refresh failed: {exc}"

# ---------------------------------------------------------------------------
# Running-style classifier
# ---------------------------------------------------------------------------

def _parse_pos_range(chunk: str) -> tuple[int | None, int | None]:
    ge_rate = le_rate = None
    for m in re.finditer(r"order_rate(>=|<=)(\d+(?:\.\d+)?)", chunk):
        op, val = m.group(1), float(m.group(2))
        if op == ">=":
            ge_rate = val
        elif op == "<=":
            le_rate = val
    if ge_rate is not None or le_rate is not None:
        lo = math.ceil(ge_rate / 100.0 * NHORSE) if ge_rate is not None else 1
        hi = math.floor(le_rate / 100.0 * NHORSE) if le_rate is not None else NHORSE
        return lo, hi

    ge_pos = le_pos = eq_pos = None
    for m in re.finditer(r"(?<!\w)order(>=|<=|==)(\d+)", chunk):
        op, val = m.group(1), int(m.group(2))
        if op == ">=":
            ge_pos = val
        elif op == "<=":
            le_pos = val
        elif op == "==":
            eq_pos = val
    if eq_pos is not None:
        return eq_pos, eq_pos
    if ge_pos is not None or le_pos is not None:
        lo = ge_pos if ge_pos is not None else 1
        hi = le_pos if le_pos is not None else NHORSE
        return lo, hi

    return None, None


def _get_styles(condition: str, precondition: str) -> list[str]:
    combined = (condition or "") + ("&" + precondition if precondition else "")
    m = re.search(r"running_style==(\d+)", combined)
    if m:
        s = {1: "front", 2: "pace", 3: "late", 4: "end"}.get(int(m.group(1)))
        return [s] if s else ["all"]
    chunk = combined.split("@")[0]
    lo, hi = _parse_pos_range(chunk)
    if lo is None:
        return ["all"]
    styles = [s for s, (s_lo, s_hi) in STYLE_RANGES.items() if lo <= s_hi and hi >= s_lo]
    return styles or ["all"]

# ---------------------------------------------------------------------------
# Effect helpers
# ---------------------------------------------------------------------------

def _has_effect(effects: list, types: set[int]) -> bool:
    return any(e.get("type") in types and e.get("target", 1) in (1, 2) for e in effects)


def _vel_modifier(effects: list) -> float | None:
    for e in effects:
        if e.get("type") in (21, 22, 27) and e.get("target", 1) in (1, 2):
            return e.get("modifier", 0) / 10000
    return None

# ---------------------------------------------------------------------------
# Reliability helpers
# ---------------------------------------------------------------------------

def _has_random_trigger(cond: str, precond: str) -> bool:
    combined = cond + ("&" + precond if precond else "")
    return any(m.group(1) in _RANDOM_FIELDS for m in _COND_RE.finditer(combined))


def _reliability_label(cond: str, precond: str, regions: list, p2: float) -> str | None:
    if not _has_random_trigger(cond, precond):
        return None
    if not regions:
        return None
    p2_in_range = any(r[0] <= p2 <= r[1] for r in regions)
    if not p2_in_range:
        return None
    total_range = sum(r[1] - r[0] for r in regions)
    if total_range <= 75:
        return "slightly unreliable"
    elif total_range <= 150:
        return "can be unreliable"
    else:
        return "very unreliable"

# ---------------------------------------------------------------------------
# Phase / timing helpers
# ---------------------------------------------------------------------------

def _fires_in_phase1(regions: list, p1: float, p2: float) -> bool:
    return (
        any(r[0] < p2 and r[1] > p1 for r in regions)
        and all(r[1] <= p2 for r in regions)
    )


def _starts_in_phase1(regions: list, p1: float, p2: float) -> bool:
    return any(p1 <= r[0] < p2 for r in regions)


def _has_late_race_condition(combined: str) -> bool:
    return any(m.group(1) in _LATE_RACE_FIELDS for m in _COND_FIELD_RE.finditer(combined))


def _has_skill_dependency(combined: str) -> bool:
    return bool(_SKILL_DEP_RE.search(combined))


def _requires_top5(condition: str, n: int = 5) -> bool:
    for chunk in condition.split("@"):
        _, hi = _parse_pos_range(chunk)
        if hi is not None and hi > n:
            return False
    return True


def _fires_as_speed_carryover(
    regions: list,
    p2: float,
    duration_frames: int,
) -> tuple[bool, bool]:
    """
    Returns (qualifies, inconsistent).

    qualifies:    True if at least one region fires right before p2
                  (within _SC_WIN_START_PROX) and the boost can still
                  be active when the horse reaches p2.
    inconsistent: True if EVERY qualifying region has an edge case —
                  either the window starts before carry_start (could
                  fire too early and expire before p2) or extends to/
                  past p2 (could fire in late race instead).
    """
    d = duration_frames / 10000.0
    if d <= 0:
        return False, False

    carry_start  = p2 - _ASSUMED_SPEED * d
    any_qualifies    = False
    all_inconsistent = True

    for r in regions:
        r0, r1 = r[0], r[1]
        if r0 >= p2:
            continue
        if r1 < carry_start:
            continue
        if r1 < p2 - _SC_WIN_END_PROX:
            continue
        if r0 < p2 - _SC_WIN_START_PROX:
            continue
        any_qualifies = True
        if r0 >= carry_start and r1 < p2:
            all_inconsistent = False

    if not any_qualifies:
        return False, False
    return True, all_inconsistent


def _has_overtake_condition(combined: str) -> bool:
    return any(m.group(1) in _OVERTAKE_FIELDS for m in _COND_FIELD_RE.finditer(combined))


def _misc_condition_notes(combined: str) -> str:
    notes: list[str] = []
    m = re.search(r'activate_count_heal\s*>=\s*(\d+)', combined)
    if m:
        notes.append(f"Requires activating recovery skills {m.group(1)} time(s)")
    m = re.search(r'temptation_count\s*(==|>=|>)\s*(\d+)', combined)
    if m:
        op, val = m.group(1), int(m.group(2))
        if op == '==' and val == 0:
            notes.append("Requires not being rushed")
        elif (op == '>=' and val >= 1) or (op == '>' and val >= 0):
            notes.append("Requires being rushed")
    m = re.search(r'is_badstart\s*(==|!=|>)\s*(\d+)', combined)
    if m:
        op, val = m.group(1), int(m.group(2))
        if op == '==' and val == 0:
            notes.append("Requires not having a late start")
        elif op in ('!=', '>') and val == 0:
            notes.append("Requires having a late start")
    return (" (" + ", ".join(notes) + ")") if notes else ""


def _fires_in_last_spurt(regions: list, p2: float, p3: float, distance: float, tail_margin: float = 50.0) -> bool:
    late_race_start = p2 + (p3 - p2) * 0.25
    cutoff = distance - tail_margin
    return any(r[1] > late_race_start and r[0] < cutoff for r in regions)

# ---------------------------------------------------------------------------
# Verdict string helper
# ---------------------------------------------------------------------------

def _strip_can_be_unreliable(verdict: str) -> str:
    v = re.sub(r", but can be unreliable, requires ", ", but requires ", verdict)
    v = re.sub(r", but can be unreliable$", "", v)
    return v.rstrip(", ") + " (unreliable)"

# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_parent_skills(
    skill_data: dict,
    skill_names: dict,
    course_entry: dict,
    char_names: dict[str, str] | None = None,
    eligible_char_ids: set[str] | None = None,
    char_name_to_id: dict[str, str] | None = None,
) -> dict[str, dict[str, list[dict]]]:
    """
    Return inherited skills grouped by running style and section.

    Return format:
        {style: {"accel": [...], "speed_carryover": [...], "velocity": [...], "last_spurt_velocity": [...]}}
    """
    SECTIONS = ("accel", "speed_carryover", "velocity", "last_spurt_velocity")
    out: dict[str, dict[str, list]] = {
        s: {sec: [] for sec in SECTIONS}
        for s in (*ALL_STYLES, "all")
    }
    seen_in_style: dict[str, set] = {s: set() for s in (*ALL_STYLES, "all")}

    d  = float(course_entry["distance"])
    p1 = phase_start(d, 1)
    p2 = phase_start(d, 2)
    p3 = phase_start(d, 3)

    for sid, entry in skill_data.items():
        if not sid.startswith("9"):
            continue

        raw  = skill_names.get(sid, "")
        name = (raw[0] if isinstance(raw, list) else str(raw)) or ""
        if not name:
            continue

        if eligible_char_ids:
            char_full = (char_names or {}).get(sid, "")
            if not char_full:
                continue
            char_id = (char_name_to_id or {}).get(char_full, "")
            if not char_id or char_id not in eligible_char_ids:
                continue

        alts = entry.get("alternatives", [])
        if not alts:
            continue

        best = None
        best_mod = -1
        for a in alts:
            if evaluate_trigger(a.get("condition", ""), a.get("precondition", ""), course_entry):
                mod = max((abs(e.get("modifier", 0)) for e in a.get("effects", [])), default=0)
                if mod > best_mod:
                    best_mod = mod
                    best = a
        if best is None:
            continue

        effects  = best.get("effects", [])
        cond     = best.get("condition", "")
        precond  = best.get("precondition", "")
        styles   = _get_styles(cond, precond)
        combined = cond + ("&" + precond if precond else "")

        is_accel = _has_effect(effects, {31})
        is_vel   = _has_effect(effects, {21, 22, 27})

        if is_accel:
            verdict = accel_verdict(cond, precond, effects, course_entry, is_inherited=True)
            if verdict and any(verdict.startswith(label) for label in _BAD_LABELS):
                continue
            if verdict and "very unreliable" in verdict:
                continue
            if verdict and "can be unreliable" in verdict:
                verdict = _strip_can_be_unreliable(verdict)
            efx     = format_effects(effects)
            display = verdict or efx
            for style in styles:
                if sid not in seen_in_style[style]:
                    out[style]["accel"].append({"sid": sid, "name": name, "verdict": display})
                    seen_in_style[style].add(sid)

        elif is_vel:
            vel_mod = _vel_modifier(effects)
            if vel_mod is None or vel_mod < _MIN_VEL_MOD:
                continue

            regions = evaluate_trigger(cond, precond, course_entry)
            if not regions:
                continue

            reliability = _reliability_label(cond, precond, regions, p2)
            efx    = format_effects(effects)
            suffix = " (unreliable)" if reliability == "can be unreliable" else ""

            bdur = int(best.get("baseDuration", 0))
            sc_qualifies, sc_inconsistent = _fires_as_speed_carryover(regions, p2, bdur)

            if reliability != "very unreliable" and sc_qualifies:
                sc_tags = []
                if sc_inconsistent:
                    sc_tags.append("inconsistent")
                if reliability == "can be unreliable":
                    sc_tags.append("unreliable")
                sc_suffix = (" (" + ", ".join(sc_tags) + ")") if sc_tags else ""
                sc_styles = ALL_STYLES if styles == ["all"] else styles
                for style in sc_styles:
                    if sid not in seen_in_style[style]:
                        out[style]["speed_carryover"].append(
                            {"sid": sid, "name": name, "verdict": efx + sc_suffix}
                        )
                        seen_in_style[style].add(sid)

            _p1_strict = _fires_in_phase1(regions, p1, p2)
            _p1_dep = (
                not _p1_strict
                and _starts_in_phase1(regions, p1, p2)
                and _has_skill_dependency(combined)
            )
            if ("front" in styles
                    and (_p1_strict or _p1_dep)
                    and not _has_late_race_condition(combined)
                    and not _has_overtake_condition(combined)):
                if sid not in seen_in_style["front"]:
                    note = _misc_condition_notes(combined)
                    out["front"]["velocity"].append(
                        {"sid": sid, "name": name, "verdict": efx + note}
                    )
                    seen_in_style["front"].add(sid)

            if (reliability != "very unreliable"
                    and vel_mod >= _MIN_LSV_MOD
                    and _fires_in_last_spurt(regions, p2, p3, d)
                    and _requires_top5(cond)
                    and not _has_overtake_condition(combined)):
                if sid not in seen_in_style["all"]:
                    note = _misc_condition_notes(combined)
                    out["all"]["last_spurt_velocity"].append(
                        {"sid": sid, "name": name, "verdict": efx + suffix + note}
                    )
                    seen_in_style["all"].add(sid)

    for style_data in out.values():
        for lst in style_data.values():
            lst.sort(key=lambda x: x["name"].lower())

    return out

# ---------------------------------------------------------------------------
# Embed building
# ---------------------------------------------------------------------------

def _skill_line(item: dict, underline: bool = False) -> str:
    """Format one skill line with icon emoji, character name and verdict."""
    info = _char_info.get(item["sid"], ("", ""))
    char_part = f" ({info[0]})" if info[0] else ""
    icon = skills_module.skill_icon_emoji(info[1]) if info[1] else "•"
    name = f"__**{item['name']}**__" if underline else f"**{item['name']}**"
    return f"{icon} {name}{char_part}: {item['verdict']}"


def _build_style_embed(style: str, sections: dict[str, list[dict]]) -> discord.Embed:
    accel    = sections["accel"]
    sc       = sections["speed_carryover"]
    velocity = sections["velocity"]
    lsv      = sections["last_spurt_velocity"]

    has_any = accel or sc or velocity or lsv

    if not has_any:
        embed = discord.Embed(title=STYLE_NAME[style], colour=STYLE_COLOR[style])
        embed.set_image(url=_EMPTY_EMOJI_URL)
        return embed
    else:
        parts: list[str] = []
        if accel:
            parts.append("**Accel**")
            parts.extend(_skill_line(sk, underline=True) for sk in accel)
        if sc:
            if parts:
                parts.append("")
            parts.append("**Speed Carryover**")
            parts.extend(_skill_line(sk) for sk in sc)
        if velocity:
            if parts:
                parts.append("")
            parts.append("**Mid-Race Velocity**")
            parts.extend(_skill_line(sk) for sk in velocity)
        if lsv:
            if parts:
                parts.append("")
            parts.append("**Last Spurt Velocity**")
            parts.extend(_skill_line(sk) for sk in lsv)
        desc = "\n".join(parts)

    if len(desc) > _MAX_EMBED_CHARS:
        desc = desc[:_MAX_EMBED_CHARS - 1] + "\u2026"

    return discord.Embed(
        title=STYLE_NAME[style],
        description=desc,
        colour=STYLE_COLOR[style],
    )

# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

async def autocomplete_cm(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Return CM number choices for the /parent cm parameter."""
    await cm_module.fetch_cm_events()
    now = time.time()
    events = cm_module._cm_events

    # Mirror cm_module.autocomplete_course classification so no events slip through:
    # upcoming = no end_ts (unknown) or end_ts still in the future
    # past     = has end_ts and it's in the past (sorted newest-first)
    upcoming = [e for e in events if not e["end_ts"] or e["end_ts"] > now]
    past     = sorted(
        [e for e in events if e["end_ts"] and e["end_ts"] <= now],
        key=lambda e: e["number"],
        reverse=True,
    )

    ordered = upcoming + past
    choices: list[app_commands.Choice[str]] = []
    seen: set[int] = set()

    def _label(cm: dict) -> str:
        return f"CM#{cm['number']} — {cm['venue']} {cm['distance']}m {cm['surface']}"

    for cm in ordered:
        num = cm["number"]
        if num in seen:
            continue
        label = _label(cm)
        if current and current.lower() not in label.lower() and current not in str(num):
            continue
        choices.append(app_commands.Choice(name=label, value=f"cm:{num}"))
        seen.add(num)
        if len(choices) >= 25:
            break

    return choices

# ---------------------------------------------------------------------------
# Interaction handler
# ---------------------------------------------------------------------------

async def handle_parent_interaction(
    interaction: discord.Interaction,
    cm_value: str | None,
) -> None:
    await interaction.response.defer()

    if not _data_loaded:
        await interaction.followup.send(
            "Skill data is not yet loaded — please try again in a moment.",
            ephemeral=True,
        )
        return

    cm_module.load_local_data_if_needed()
    await cm_module.fetch_cm_events()

    # Resolve which CM to use
    cm_data: dict | None = None
    if cm_value:
        course_id, course_display = cm_module.resolve_course(cm_value, None)
        if cm_value.startswith("cm:"):
            try:
                num = int(cm_value[3:])
                cm_data = next((c for c in cm_module._cm_events if c["number"] == num), None)
            except ValueError:
                pass
    else:
        cm_data = cm_module.get_active_cm() or cm_module.get_next_cm()
        if cm_data:
            course_id, course_display = cm_module.resolve_course(f"cm:{cm_data['number']}", None)
        else:
            course_id, course_display = None, ""

    if not course_id:
        await interaction.followup.send(
            "Could not resolve a course for that CM. The CM may not be in the timeline yet.",
            ephemeral=True,
        )
        return

    course_entry = cm_module.get_course_entry(course_id)
    if not course_entry:
        await interaction.followup.send(
            f"No course geometry found for course ID {course_id}.",
            ephemeral=True,
        )
        return

    # Character eligibility
    cm_start_ts = cm_data["start_ts"] if cm_data and cm_data.get("start_ts") else 0
    eligible_char_ids: set[str] | None = None
    if _base_char_ids:
        timeline_ids = cm_module.get_eligible_card_ids(cm_start_ts) if cm_start_ts else set()
        eligible_char_ids = _base_char_ids | timeline_ids

    # char_names dict used by build_parent_skills (sid → character_name only)
    char_names = {sid: info[0] for sid, info in _char_info.items()}

    # Determine CM number for header display
    cm_num = cm_data["number"] if cm_data else "?"

    style_skills = build_parent_skills(
        _skill_data, _skill_names, course_entry,
        char_names, eligible_char_ids, _char_name_to_id,
    )

    # Build embeds
    embeds = [_build_style_embed(style, style_skills[style]) for style in DISPLAY_STYLES]

    # Send — first followup gets the header content + first batch of embeds
    header = f"## {course_display}\n-# *Good inherited skills by running style*\n-# *Prioritize Accel skills over velocity, unless you already have good gold accels*"

    # Batch respecting Discord limits (10 embeds / 6000 total chars per message)
    batches: list[list[discord.Embed]] = []
    batch: list[discord.Embed] = []
    batch_chars = 0
    for embed in embeds:
        chars = len(embed.title or "") + len(embed.description or "")
        if batch and (batch_chars + chars > _MAX_TOTAL_CHARS or len(batch) >= _MAX_EMBEDS_PER_MSG):
            batches.append(batch)
            batch = []
            batch_chars = 0
        batch.append(embed)
        batch_chars += chars
    if batch:
        batches.append(batch)

    if batches:
        await interaction.followup.send(content=header, embeds=batches[0])
        for extra_batch in batches[1:]:
            await interaction.followup.send(embeds=extra_batch)
    else:
        await interaction.followup.send(content=header)
