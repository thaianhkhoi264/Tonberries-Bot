"""
cm_module.py

Provides Champions Meeting (CM) race data from uma.moe's timeline API,
plus course lookup helpers for the /skill command's autocomplete.

Public API
----------
load_local_data_if_needed()
    Load tracknames / course_data / course_labels JSON files from disk.
    Call once at bot startup (on_ready).

fetch_cm_events() -> list[dict]
    Async fetch of Champions Meeting events from uma.moe, cached 1 h.

autocomplete_course(interaction, current) -> list[Choice]
    For the `course` parameter of /skill.
    Order: active/upcoming CMs → venue names → past CMs.

autocomplete_length(interaction, current, course_value) -> list[Choice]
    For the `length` parameter of /skill.
    Returns course variants when course_value starts with "venue:".
    Returns empty list for CM choices (length is baked into the CM).

resolve_course(course_value, length_value) -> (course_id | None, display_str)
    Resolve user selections to a course_data entry ID and display name.

get_course_entry(course_id) -> dict | None
    Return the uma-skill-tools course_data dict for a given course ID.

get_active_cm() -> dict | None
get_next_cm() -> dict | None
    Return the currently running or next upcoming CM, from the cached list.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
from datetime import datetime

import aiohttp
import discord
from discord import app_commands

from global_config import (
    COURSE_DATA_JSON,
    COURSE_LABELS_JSON,
    SKILL_DATA_JSON,
    SKILL_NAMES_JSON,
    TRACK_NAMES_JSON,
    UMA_MOE_API_KEY,
)
from skill_chart import evaluate_trigger, _COND_RE, _GEO_FIELDS

logger = logging.getLogger("cm_module")

# ---------------------------------------------------------------------------
# Display-only label mappings (raw API values → user-facing strings)
# ---------------------------------------------------------------------------
_DISPLAY: dict[str, str] = {
    "Clockwise":        "Right",
    "Counterclockwise": "Left",
    "Autumn":           "Fall",
}


def display(v: str) -> str:
    """Map a raw API value to its display label (e.g. Clockwise → Right)."""
    return _DISPLAY.get(v, v)


def apply_display_fixes(text: str) -> str:
    """Replace all raw API values in *text* with their display equivalents."""
    for raw, mapped in _DISPLAY.items():
        text = text.replace(raw, mapped)
    return text


# Venue name variants across data sources
_VENUE_ALIASES: dict[str, str] = {
    "Oi":  "Ooi",
    "Ohi": "Ooi",
}

# ---------------------------------------------------------------------------
# Module-level local data (loaded from disk once at startup)
# ---------------------------------------------------------------------------
_track_name_to_id: dict[str, int] = {}   # English name → raceTrackId
_track_id_to_name: dict[int, str] = {}   # raceTrackId → English name
_course_data: dict[str, dict]     = {}   # courseId str → uma-skill-tools entry
_course_labels: dict[str, dict]   = {}   # courseId str → uma-tools entry (has 'course' field)
_skill_data:  dict[str, dict]     = {}   # skillId str → uma-skill-tools entry
_skill_names: dict[str, object]   = {}   # skillId str → [jp, en] or en string

_local_loaded = False


def load_local_data_if_needed() -> None:
    """Load JSON files from disk. Safe to call multiple times; no-op after first load."""
    global _local_loaded, _track_name_to_id, _track_id_to_name
    global _course_data, _course_labels, _skill_data, _skill_names
    if _local_loaded:
        return

    if os.path.exists(TRACK_NAMES_JSON):
        with open(TRACK_NAMES_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        for tid_str, names in raw.items():
            tid = int(tid_str)
            en = names[1] if isinstance(names, list) and len(names) > 1 else str(names)
            _track_name_to_id[en] = tid
            _track_id_to_name[tid] = en

    if os.path.exists(COURSE_DATA_JSON):
        with open(COURSE_DATA_JSON, encoding="utf-8") as f:
            _course_data = json.load(f)

    if os.path.exists(COURSE_LABELS_JSON):
        with open(COURSE_LABELS_JSON, encoding="utf-8") as f:
            _course_labels = json.load(f)

    if os.path.exists(SKILL_DATA_JSON):
        with open(SKILL_DATA_JSON, encoding="utf-8") as f:
            _skill_data = json.load(f)

    if os.path.exists(SKILL_NAMES_JSON):
        with open(SKILL_NAMES_JSON, encoding="utf-8") as f:
            _skill_names = json.load(f)

    _local_loaded = True
    logger.info(
        f"[CMModule] Local data: {len(_track_name_to_id)} tracks, "
        f"{len(_course_data)} courses, {len(_course_labels)} course-labels, "
        f"{len(_skill_data)} skills"
    )


def get_course_entry(course_id: int) -> dict | None:
    """Return the uma-skill-tools course_data dict for the given course ID."""
    return _course_data.get(str(course_id))


# ---------------------------------------------------------------------------
# uma.moe CM timeline cache
# ---------------------------------------------------------------------------
_UMA_MOE_TIMELINE = "https://uma.moe/resources/current/banner_timeline.json.gz"
_CM_CACHE_TTL     = 3600  # seconds

_cm_events:   list[dict] = []
_cm_cache_ts: float      = 0.0


def _parse_ts(s: str) -> int:
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _parse_cm_event(ev: dict) -> dict | None:
    """
    Parse a champions_meeting timeline event.

    description format:
        "Venue - Surface<br>Distance - DistType - Direction<br>Ground - Season - Weather"
    id format:
        "champions-meeting-N"  (0-indexed → CM number N+1)
    """
    m = re.search(r"champions-meeting-(\d+)", ev.get("id", ""))
    if not m:
        return None
    number = int(m.group(1)) + 1

    lines = [l.strip() for l in re.split(r"<br\s*/?>", ev.get("description", ""), flags=re.I)]

    venue = surface = dist_type = direction = ground = season = weather = ""
    distance = 0

    if lines:
        parts = [p.strip() for p in lines[0].split(" - ")]
        venue   = parts[0] if len(parts) > 0 else ""
        surface = parts[1] if len(parts) > 1 else ""

    if len(lines) >= 2:
        parts = [p.strip() for p in lines[1].split(" - ")]
        try:
            distance = int(parts[0].rstrip("mM"))
        except (ValueError, IndexError):
            pass
        dist_type = parts[1] if len(parts) > 1 else ""
        direction = parts[2] if len(parts) > 2 else ""

    if len(lines) >= 3:
        parts = [p.strip() for p in lines[2].split(" - ")]
        ground  = parts[0] if len(parts) > 0 else ""
        season  = parts[1] if len(parts) > 1 else ""
        weather = parts[2] if len(parts) > 2 else ""

    start_ts = _parse_ts(ev.get("global_release_date") or ev.get("jp_release_date") or "")
    end_ts   = _parse_ts(ev.get("estimated_end_date") or "")

    return {
        "number":       number,
        "venue":        venue,
        "surface":      surface,
        "distance":     distance,
        "dist_type":    dist_type,
        "direction":    direction,
        "ground":       ground,
        "season":       season,
        "weather":      weather,
        "start_ts":     start_ts,
        "end_ts":       end_ts,
        "is_confirmed": bool(ev.get("is_confirmed")),
    }


async def fetch_cm_events() -> list[dict]:
    """
    Fetch Champions Meeting events from uma.moe timeline.
    Returns a list of parsed CM dicts sorted by CM number.
    Result is cached for _CM_CACHE_TTL seconds.
    """
    global _cm_events, _cm_cache_ts

    now = time.time()
    if _cm_events and (now - _cm_cache_ts) < _CM_CACHE_TTL:
        return _cm_events

    try:
        headers = {"X-API-Key": UMA_MOE_API_KEY} if UMA_MOE_API_KEY else {}
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_UMA_MOE_TIMELINE, headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"[CMModule] Timeline returned {resp.status}")
                    return _cm_events
                raw = await resp.read()

        try:
            data = json.loads(gzip.decompress(raw).decode("utf-8"))
        except Exception:
            data = json.loads(raw.decode("utf-8"))

        parsed = []
        for ev in data.get("events", []):
            if ev.get("type") != "champions_meeting":
                continue
            cm = _parse_cm_event(ev)
            if cm:
                parsed.append(cm)

        parsed.sort(key=lambda c: c["number"])
        _cm_events = parsed
        _cm_cache_ts = now
        logger.info(f"[CMModule] Loaded {len(parsed)} CM events from uma.moe")

    except Exception as exc:
        logger.error(f"[CMModule] Failed to fetch CM timeline: {exc}")

    return _cm_events


# ---------------------------------------------------------------------------
# Course lookup helpers
# ---------------------------------------------------------------------------

def _normalize_venue(name: str) -> str:
    return _VENUE_ALIASES.get(name, name)


def _track_id_for(venue: str) -> int | None:
    return _track_name_to_id.get(_normalize_venue(venue))


_SURFACE_INT = {"turf": 1, "dirt": 2}


def _find_course_id(track_id: int, distance: int, surface_str: str) -> int | None:
    """Return the course ID matching (track_id, distance, surface) or None."""
    surf = _SURFACE_INT.get(surface_str.lower())
    if surf is None:
        return None
    for cid_str, c in _course_data.items():
        if (c.get("raceTrackId") == track_id
                and c.get("distance") == distance
                and c.get("surface") == surf):
            return int(cid_str)
    return None


def _inner_outer_label(cid: int) -> str:
    """Return ' (Inner)', ' (Outer)', or '' from uma-tools course field."""
    entry = _course_labels.get(str(cid))
    if not entry:
        return ""
    return {2: " (Inner)", 3: " (Outer)"}.get(entry.get("course", 1), "")


def get_venue_courses(venue: str) -> list[tuple[int, str]]:
    """
    Return all (course_id, display_label) pairs for a venue, sorted Turf first,
    then by ascending distance. Label format: "2000m Turf (Inner)".
    """
    track_id = _track_id_for(venue)
    if track_id is None:
        return []

    rows = []
    for cid_str, c in _course_data.items():
        if c.get("raceTrackId") != track_id:
            continue
        cid  = int(cid_str)
        dist = c.get("distance", 0)
        surf = "Turf" if c.get("surface") == 1 else "Dirt"
        label = f"{dist}m {surf}{_inner_outer_label(cid)}"
        rows.append((cid, label, 0 if surf == "Turf" else 1, dist))

    rows.sort(key=lambda x: (x[2], x[3]))
    return [(cid, label) for cid, label, _, _ in rows]


def _cm_course_id(cm: dict) -> int | None:
    track_id = _track_id_for(cm["venue"])
    if track_id is None:
        return None
    return _find_course_id(track_id, cm["distance"], cm["surface"])


def _cm_display(cm: dict) -> str:
    """Short label for autocomplete: 'CM#17 — Kyoto 2200m Turf'"""
    return f"CM#{cm['number']} \u2014 {cm['venue']} {cm['distance']}m {cm['surface']}"


def _cm_course_display(cm: dict) -> str:
    """
    Full course display for embed footer:
    'CM#17 — Kyoto 2200m Turf, Right, Good, Fall, Sunny'
    """
    parts = [
        f"CM#{cm['number']} \u2014 {cm['venue']} {cm['distance']}m {cm['surface']}",
        display(cm["direction"]),
        cm["ground"],
        display(cm["season"]),
        cm["weather"],
    ]
    return ", ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Public API: CM state helpers
# ---------------------------------------------------------------------------

def get_active_cm() -> dict | None:
    """Return the currently active CM, or None."""
    now = time.time()
    for cm in _cm_events:
        if cm["start_ts"] and cm["end_ts"] and cm["start_ts"] <= now <= cm["end_ts"]:
            return cm
    return None


def get_next_cm() -> dict | None:
    """Return the next upcoming CM (not yet started), or None."""
    now = time.time()
    upcoming = [c for c in _cm_events if c["start_ts"] and c["start_ts"] > now]
    return min(upcoming, key=lambda c: c["start_ts"]) if upcoming else None


# ---------------------------------------------------------------------------
# Public API: autocomplete
# ---------------------------------------------------------------------------

async def autocomplete_course(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """
    Autocomplete for the `course` parameter of /skill.
    Order: active/upcoming CMs → venue names → past CMs.
    """
    load_local_data_if_needed()
    events = await fetch_cm_events()
    now    = time.time()

    upcoming = [e for e in events if not e["end_ts"] or e["end_ts"] > now]
    past     = sorted(
        [e for e in events if e["end_ts"] and e["end_ts"] <= now],
        key=lambda c: c["number"], reverse=True,
    )

    # All venue names that have at least one course entry
    venue_track_ids = {c.get("raceTrackId") for c in _course_data.values()}
    venues = sorted(n for n, tid in _track_name_to_id.items() if tid in venue_track_ids)

    choices: list[app_commands.Choice[str]] = []
    cur = current.lower()

    def _add(label: str, value: str) -> None:
        if len(choices) < 25 and (not cur or cur in label.lower()):
            choices.append(app_commands.Choice(name=label, value=value))

    for cm in upcoming:
        _add(_cm_display(cm), f"cm:{cm['number']}")
    for v in venues:
        _add(v, f"venue:{v}")
    for cm in past:
        _add(_cm_display(cm), f"cm:{cm['number']}")

    return choices


async def autocomplete_length(
    interaction: discord.Interaction,
    current: str,
    course_value: str,
) -> list[app_commands.Choice[str]]:
    """
    Autocomplete for the `length` parameter of /skill.
    Only relevant when course_value is a venue ("venue:Nakayama").
    Returns empty list for CM choices (length is determined by the CM).
    """
    load_local_data_if_needed()
    if not course_value.startswith("venue:"):
        return []

    venue = course_value[len("venue:"):]
    cur   = current.lower()
    return [
        app_commands.Choice(name=label, value=f"course:{cid}")
        for cid, label in get_venue_courses(venue)
        if not cur or cur in label.lower()
    ][:25]


# ---------------------------------------------------------------------------
# Public API: resolve course for chart
# ---------------------------------------------------------------------------

def resolve_course(
    course_value: str,
    length_value: str | None,
) -> tuple[int | None, str]:
    """
    Resolve (course_value, length_value) → (course_id, display_name).

    course_value: "cm:17" | "venue:Nakayama" | ""
    length_value: "course:10503" | None

    Returns (None, "") if the combination is not resolvable.
    """
    load_local_data_if_needed()

    if course_value.startswith("cm:"):
        try:
            num = int(course_value[3:])
        except ValueError:
            return None, ""
        for cm in _cm_events:
            if cm["number"] == num:
                cid = _cm_course_id(cm)
                if cid is None:
                    return None, ""
                return cid, _cm_course_display(cm)
        return None, ""

    if course_value.startswith("venue:") and length_value and length_value.startswith("course:"):
        try:
            cid = int(length_value[len("course:"):])
        except ValueError:
            return None, ""
        entry = _course_data.get(str(cid))
        if not entry:
            return None, ""
        dist  = entry.get("distance", 0)
        surf  = "Turf" if entry.get("surface") == 1 else "Dirt"
        venue = course_value[len("venue:"):]
        name  = f"{venue} {dist}m {surf}{_inner_outer_label(cid)}"
        return cid, name

    return None, ""


# ---------------------------------------------------------------------------
# CM green-skill detection
# ---------------------------------------------------------------------------

# Effect types that are passive stat-boost "green" skills (Speed/Stamina/
# Power/Guts/Wisdom).  Excludes HP Recovery (9), speed bursts (21/22/27),
# and Acceleration (31).
_GREEN_EFFECT_TYPES = {1, 2, 3, 4, 5}

# Condition fields considered "CM-fixed" — they're pre-set by the CM schedule
# just like track ID or rotation, so they're valid for the geo-only filter.
_CM_CONDITION_FIELDS = _GEO_FIELDS | {"season", "ground_condition", "weather"}

# Mapping from uma.moe event strings → numeric values used in skill conditions
_SEASON_MAP      = {"Spring": 1, "Summer": 2, "Autumn": 3, "Fall": 3, "Winter": 4}
_WEATHER_MAP     = {"Sunny": 1, "Cloudy": 2, "Rainy": 3, "Snow": 4}
_GROUND_COND_MAP = {"Firm": 1, "Good": 2, "Slightly Soft": 3, "Soft": 3, "Heavy": 4}

_TIER_RE = re.compile(r"\s+[◎○×]\s*$")

# ---------------------------------------------------------------------------
# Icon-stem whitelist for green skills
# ---------------------------------------------------------------------------
# Valid stems: 100XY where X ∈ {1–6} and Y ∈ {1, 2, 6}.
# Stems ending in 4 are debuff skills and must be excluded.
_VALID_STEM_RE         = re.compile(r"^100[1-6][126]$")
_VALID_STEM_EXCEPTIONS = frozenset({"20181", "40012"})


def _icon_stem(icon_url: str) -> str:
    """Extract stem (filename without .png) from a Gametora icon URL."""
    if not icon_url:
        return ""
    return icon_url.rsplit("/", 1)[-1].replace(".png", "")


def _valid_green_icon(icon_url: str) -> bool:
    """Return True if this icon URL is a whitelisted, non-debuff green skill icon."""
    stem = _icon_stem(icon_url)
    if not stem:
        return False
    if stem in _VALID_STEM_EXCEPTIONS:
        return True
    return bool(_VALID_STEM_RE.match(stem))


def _icon_y_value(icon_url: str) -> int:
    """Return the Y digit of a 100XY stem for sort order (1 < 2 < 6)."""
    stem = _icon_stem(icon_url)
    if stem in _VALID_STEM_EXCEPTIONS:
        return 99
    try:
        return int(stem[-1])
    except (ValueError, IndexError):
        return 99


def _is_green_skill(effects: list) -> bool:
    relevant = [e for e in effects if e.get("target", 1) in (1, 2)]
    return bool(relevant) and all(e.get("type") in _GREEN_EFFECT_TYPES for e in relevant)


def _is_cm_geo_only(condition: str, precondition: str) -> bool:
    combined = (condition or "") + ("&" + precondition if precondition else "")
    tokens = [m.group(1) for m in _COND_RE.finditer(combined)]
    return bool(tokens) and all(t in _CM_CONDITION_FIELDS for t in tokens)


def _cmp_op(a: int, op: str, b: int) -> bool:
    return {"==": a == b, "!=": a != b, ">=": a >= b,
            "<=": a <= b, ">": a > b, "<": a < b}.get(op, False)


def _cm_fixed_val(field: str, cm: dict) -> int | None:
    if field == "season":           return _SEASON_MAP.get(cm.get("season", ""))
    if field == "ground_condition": return _GROUND_COND_MAP.get(cm.get("ground", ""))
    if field == "weather":          return _WEATHER_MAP.get(cm.get("weather", ""))
    return None


def _matches_cm_conditions(cond: str, precond: str, cm: dict) -> bool:
    combined = (cond or "") + ("&" + precond if precond else "")
    groups = combined.split("@") if combined else [""]
    for group in groups:
        ok = True
        for m in _COND_RE.finditer(group):
            field, op, raw = m.group(1), m.group(2), m.group(3)
            if field in _GEO_FIELDS:
                continue
            val = _cm_fixed_val(field, cm)
            if val is None:
                continue
            if not _cmp_op(val, op, int(float(raw))):
                ok = False
                break
        if ok:
            return True
    return False


def _skill_tier_rank(name: str) -> int:
    """○ = 0 (preferred 'first level'), × = 1, bare name = 2, ◎ = 3, enhanced = 4."""
    if name.endswith("○"): return 0
    if name.endswith("×"): return 1
    if not _TIER_RE.search(name): return 2
    if name.endswith("◎"): return 3
    return 4


def _en_skill_name(sid: str) -> str:
    raw = _skill_names.get(sid, "")
    return (raw[0] if isinstance(raw, list) else str(raw)) or f"Skill {sid}"


def get_cm_green_skills(
    cm: dict,
    icon_urls: dict | None = None,
) -> list[list[tuple[str, str]]]:
    """
    Return skill families (passive stat-boost / green) that activate on the
    given CM's course.

    Each item in the returned list is a **family** — up to two
    ``(skill_name, icon_url)`` tuples: the Y=1 base version (○) first,
    followed by the Y=2 gold/enhanced version if one exists.  Y=6 entries
    are omitted.  Families are sorted alphabetically by the first member's
    name.

    Only skills whose icon URL matches the whitelist (``100XY`` where
    X∈{1–6} and Y∈{1,2,6}, plus exceptions ``20181`` and ``40012``) are
    included.  Debuff skills (Y=4) and skills with unrecognised icons are
    excluded automatically.

    ``icon_urls`` should be ``{skill_id_str: icon_url}`` (from skills.db).
    """
    load_local_data_if_needed()

    course_id = _cm_course_id(cm)
    if not course_id:
        return []
    course_entry = _course_data.get(str(course_id))
    if not course_entry:
        return []

    icon_map = icon_urls or {}
    raw: list[tuple[str, str, str]] = []   # (name, icon_url, sid)

    for sid, entry in _skill_data.items():
        if not (sid.startswith("2") or sid.startswith("3")):
            continue
        alts = entry.get("alternatives", [])
        if not alts:
            continue
        alt     = alts[0]
        cond    = alt.get("condition",    "")
        precond = alt.get("precondition", "")
        effects = alt.get("effects", [])

        icon_url = icon_map.get(sid, "")
        if not _valid_green_icon(icon_url):
            continue
        if not _is_green_skill(effects):
            continue
        if not _is_cm_geo_only(cond, precond):
            continue
        if not _matches_cm_conditions(cond, precond, cm):
            continue
        if not evaluate_trigger(cond, precond, course_entry):
            continue

        name = _en_skill_name(sid)
        if name == f"Skill {sid}":
            continue

        raw.append((name, icon_url, sid))

    # Group by (family, Y value); pick best representative per Y slot.
    # Only Y=1 (base/○) and Y=2 (gold/enhanced) are shown; Y=6 is skipped.
    # Within each Y slot, prefer lower _skill_tier_rank (○ > ◎ > bare name).
    Slot = tuple[str, str]  # (name, icon_url)
    fam_y: dict[int, dict[int, Slot]] = {}   # fam_id → {y_val → best}

    for name, icon_url, sid in raw:
        y = _icon_y_value(icon_url)
        if y not in (1, 2):
            continue
        try:
            fam = int(sid) // 10
        except ValueError:
            fam = hash(sid)
        bucket = fam_y.setdefault(fam, {})
        existing = bucket.get(y)
        if existing is None or _skill_tier_rank(name) < _skill_tier_rank(existing[0]):
            bucket[y] = (name, icon_url)

    result: list[list[tuple[str, str]]] = []
    for bucket in fam_y.values():
        family: list[tuple[str, str]] = []
        for y in (1, 2):
            if y in bucket:
                family.append(bucket[y])
        if family:
            result.append(family)

    # Sort families alphabetically by the first member's name.
    result.sort(key=lambda fam: fam[0][0].lower())
    return result
