"""
skill_describe.py

Numeric, course-specific description of a skill for the /skill embed, derived
from its condition / precondition alone (no GameTora text):

  Effect        effect text + duration scaled by course distance
  Activates     where (metres) + what you need (CM rank band, rushed, ...)
  Precondition  the precondition, if any
  Timing        (acceleration + velocity skills only) how far from the start of
                late-race (p2) the skill first activates
  Rating        (acceleration skills only) accel_verdict's overall rating

Field meanings come from the uma-skill-tools condition reference (every field
used in skill_data.json is covered; anything unmapped is shown as the raw
`field>=value` token rather than dropped).

Public API
----------
describe_skill(sid, entry, name, course, is_cm=True) -> dict
embed_fields(info) -> list[(name, value, inline)]
"""
from __future__ import annotations

import re

import cm_module
from skill_chart import (
    CM_HORSES, _COND_RE, _ETYPE, _RANDOM_FIELDS, _STAT_T,
    _order_rate_to_order, accel_verdict, evaluate_trigger, phase_start,
)

_VELOCITY_TYPES = {21, 22, 27}
_ACCEL_TYPE = 31

_PHASE_NAME = {0: "the opening stretch", 1: "mid-race", 2: "late-race", 3: "the last spurt"}

# Fields consumed by hand-written logic below (everything else goes through _REG).
_HANDLED = {
    "order", "order_rate", "phase", "corner", "is_finalcorner", "is_finalcorner_laterhalf",
    "is_last_straight", "is_last_straight_onetime", "temptation_count",
    "blocked_side_continuetime", "blocked_front_continuetime", "distance_rate",
    "running_style", "distance_type", "is_lastspurt", "phase_random", "phase_firsthalf_random",
    "phase_laterhalf_random", "straight_random", "all_corner_random", "down_slope_random",
    "up_slope_random", "change_order_onetime", "is_behind_in", "always",
    "remain_distance", "slope", "is_finalcorner_random", "distance_rate_after_random",
    "last_straight_random", "phase_corner_random", "phase_straight_random",
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_groups(cond: str) -> list[tuple[str, dict]]:
    """Split on '@' (OR); each group -> (raw string, {field: [(op, value), ...]})."""
    groups = []
    for raw in (cond or "").split("@"):
        atoms: dict[str, list] = {}
        for m in _COND_RE.finditer(raw):
            atoms.setdefault(m.group(1), []).append((m.group(2), float(m.group(3))))
        if atoms:
            groups.append((raw, atoms))
    return groups


def has(atoms: dict, field: str, op: str, val: float) -> bool:
    return (op, val) in atoms.get(field, [])


def ordinal(n: int) -> str:
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _is_continue(field: str) -> bool:
    return bool(re.fullmatch(r"order_rate_(in|out)\d+_continue", field))


# ---------------------------------------------------------------------------
# Rank / phase phrases
# ---------------------------------------------------------------------------

def rank_band(atoms: dict) -> tuple[int, int] | None:
    lo, hi, seen = 1, CM_HORSES, False
    for op, v in atoms.get("order_rate", []):
        n = _order_rate_to_order(v, CM_HORSES)
        if op == ">=":
            lo = max(lo, n)
        elif op == ">":
            lo = max(lo, n + 1)        # order_rate>50 in a 9-horse race = 6th or worse
        elif op == "<=":
            hi = min(hi, n)
        elif op == "<":
            hi = min(hi, n - 1)
        elif op == "==":
            lo = hi = n
        seen = True
    for op, v in atoms.get("order", []):
        n = int(v)
        if op == ">=":
            lo = max(lo, n)
        elif op == ">":
            lo = max(lo, n + 1)
        elif op == "<=":
            hi = min(hi, n)
        elif op == "<":
            hi = min(hi, n - 1)
        elif op == "==":
            lo = hi = n
        seen = True
    for field in atoms:
        m = re.fullmatch(r"order_rate_(in|out)(\d+)_continue", field)
        if m and has(atoms, field, "==", 1.0):
            n = _order_rate_to_order(int(m.group(2)), CM_HORSES)
            if m.group(1) == "out":
                lo = max(lo, n + 1)
            else:
                hi = min(hi, n)
            seen = True
    return (lo, hi) if seen else None


def rank_text(band: tuple[int, int] | None) -> str | None:
    if band is None:
        return None
    lo, hi = band
    if lo == 1 and hi == CM_HORSES:
        return None
    if lo == hi:
        return ordinal(lo)
    if hi == CM_HORSES:
        return f"{ordinal(lo)} or worse"
    return f"{ordinal(lo)}–{ordinal(hi)}"


def phase_text(atoms: dict) -> str | None:
    for op, v in atoms.get("phase", []):
        v = int(v)
        if op == "==":
            return {0: "in the opening stretch", 1: "during mid-race",
                    2: "during late-race", 3: "in the last spurt"}.get(v)
        if op == ">=":
            return {1: "from mid-race onward", 2: "once late-race has begun",
                    3: "in the last spurt"}.get(v)
        if op == "<=":
            return {0: "in the opening stretch", 1: "before late-race",
                    2: "before the last spurt"}.get(v)
    return None


# ---------------------------------------------------------------------------
# Field registry (meanings: references/condition-fields.txt)
# ---------------------------------------------------------------------------

def _n(v: float) -> str:
    return f"{v:g}"


def _s(v: float) -> str:
    return "" if v == 1 else "s"


def _is_are(v: float) -> str:
    return "is" if v == 1 else "are"


_SEASON = {1: "spring", 2: "summer", 3: "fall", 4: "winter", 5: "the cherry-blossom season"}
_WEATHER = {1: "sunny", 2: "cloudy", 3: "rainy", 4: "snowy"}
_GROUND = {1: "firm", 2: "good", 3: "soft", 4: "heavy"}      # global names (cm_module._GROUND_COND_MAP)
_MOOD = {1: "Terrible", 2: "Bad", 3: "Normal", 4: "Good", 5: "Perfect"}
_TIME = {1: "in the morning", 2: "during the day", 3: "in the evening", 4: "at night"}
_GRADE = {100: "G1", 200: "G2", 300: "G3", 400: "OP", 700: "Pre-OP", 800: "maiden", 900: "debut", 999: "daily"}
_STYLE_PL = {"nige": "Front Runner", "senko": "Pace Chaser", "sashi": "Late Surger", "oikomi": "End Closer"}


def _lo_hi(op, v, ge, le, eq=None):
    """Pick a phrase by operator; strict ops on whole numbers shift by one."""
    if op == ">" and float(v).is_integer():
        op, v = ">=", v + 1
    if op == "<" and float(v).is_integer():
        op, v = "<=", v - 1
    if op in (">=", ">"):
        return ge(v)
    if op in ("<=", "<"):
        return le(v)
    if op == "==" and eq:
        return eq(v)
    return None


def _track_name(v):
    name = cm_module._track_id_to_name.get(int(v), f"track {int(v)}")
    return {"Chuukyo": "Chukyo"}.get(name, name)


def _ge_only(fmt):
    """Clause that only makes sense for a lower bound ("N+ ...")."""
    return lambda op, v: _lo_hi(op, v, fmt, lambda v: None)


def _flag(on, off=None):
    """Clause for a 0/1 flag field."""
    return lambda op, v: {1: on, 0: off}.get(int(v)) if op == "==" else None


# field -> (kind, fn(op, value) -> phrase | None)
#   "you":    adjective phrase that reads after "you're"
#   "clause": a full standalone clause
_REG = {
    "hp_per": ("clause", lambda op, v: _lo_hi(op, v,
        lambda v: "you have stamina left" if v <= 1 else f"you have {_n(v)}%+ stamina",
        lambda v: f"you have {_n(v)}% stamina or less")),
    "bashin_diff_infront": ("clause", lambda op, v: _lo_hi(op, v,
        lambda v: f"the runner ahead is {_n(v)}+ horse lengths away",
        lambda v: f"the runner ahead is within {_n(v)} horse length{_s(v)}")),
    "bashin_diff_behind": ("clause", lambda op, v: _lo_hi(op, v,
        lambda v: f"the runner behind you is {_n(v)}+ lengths back",
        lambda v: f"a runner is within {_n(v)} horse length{_s(v)} behind you")),
    "distance_diff_top": ("you", lambda op, v: _lo_hi(op, v,
        lambda v: f"{_n(v)}+ m behind the leader", lambda v: f"within {_n(v)} m of the leader")),
    "distance_diff_rate": ("you", lambda op, v: _lo_hi(op, v,
        lambda v: f"at least {_n(v)}% of the way back through the field",
        lambda v: f"within the front {_n(v)}% of the field")),
    "is_overtake": ("clause", _flag("you're chasing a runner within reach", "you have no one to catch")),
    "overtake_target_time": ("clause", _ge_only(lambda v: f"another runner has been chasing you for {_n(v)}+ s")),
    "overtake_target_no_order_up_time": ("clause", _ge_only(lambda v: f"you've had a runner to catch for {_n(v)}+ s")),
    "infront_near_lane_time": ("clause", _ge_only(lambda v: f"a runner has been right in front of you for {_n(v)}+ s")),
    "behind_near_lane_time": ("clause", _ge_only(lambda v: f"a runner has been right behind you for {_n(v)}+ s")),
    "behind_near_lane_time_set1": ("clause", _ge_only(lambda v: f"a runner has been behind you for {_n(v)}+ s")),
    "near_count": ("clause", lambda op, v: _lo_hi(op, v,
        lambda v: f"{_n(v)}+ runners are close by",
        lambda v: f"at most {_n(v)} runner{_s(v)} {_is_are(v)} close by",
        lambda v: f"exactly {_n(v)} runner{_s(v)} {_is_are(v)} close by")),
    "visiblehorse": ("clause", _ge_only(lambda v: f"{_n(v)}+ runners are in view")),
    "is_surrounded": ("clause", _flag("you're surrounded", "you're not surrounded")),
    "blocked_front": ("clause", _flag("someone is right in front of you", "no one is blocking you in front")),
    "is_temptation": ("clause", _flag("you're rushing", "you're not rushing")),
    "temptation_opponent_count_behind": ("clause", _ge_only(
        lambda v: f"{_n(v)}+ runner{_s(v)} behind you {_is_are(v)} rushing")),
    "temptation_opponent_count_infront": ("clause", _ge_only(
        lambda v: f"{_n(v)}+ runner{_s(v)} ahead of you {_is_are(v)} rushing")),
    "is_badstart": ("clause", _flag("you had a late start", "you didn't have a late start")),
    "accumulatetime": ("clause", lambda op, v: _lo_hi(op, v,
        lambda v: f"{_n(v)}+ s into the race", lambda v: f"within the first {_n(v)} s")),
    "activate_count_start": ("clause", _ge_only(lambda v: f"you've activated {_n(v)}+ skills in the opening")),
    "activate_count_middle": ("clause", _ge_only(lambda v: f"you've activated {_n(v)}+ skills in mid-race")),
    "activate_count_end_after": ("clause", _ge_only(lambda v: f"you've activated {_n(v)}+ skills since late-race began")),
    "activate_count_later_half": ("clause", _ge_only(lambda v: f"you've activated {_n(v)}+ skills in the second half")),
    "activate_count_all": ("clause", _ge_only(lambda v: f"you've activated {_n(v)}+ skills so far")),
    "activate_count_heal": ("clause", _ge_only(lambda v: f"you've activated {_n(v)}+ recovery skill{_s(v)}")),
    "popularity": ("you", lambda op, v: "the most popular runner" if (op, v) == ("==", 1) else _lo_hi(op, v,
        lambda v: f"ranked {ordinal(int(v))} or lower in popularity",
        lambda v: f"among the {_n(v)} most popular runners")),
    "post_number": ("clause", lambda op, v: _lo_hi(op, v,
        lambda v: f"you started from gate block {_n(v)} or higher",
        lambda v: f"you started from gate block {_n(v)} or lower",
        lambda v: f"you started from gate block {_n(v)}")),
    "is_move_lane": ("clause", lambda op, v: {1: "you've just moved toward the inner fence",
                                              2: "you've just moved away from the fence"}.get(int(v)) if op == "==" else None),
    "compete_fight_count": ("clause", _ge_only(lambda v: f"you've been in {_n(v)}+ Showdown{_s(v)}")),
    "change_order_up_end_after": ("clause", _ge_only(lambda v: f"you've overtaken {_n(v)}+ time{_s(v)} since late-race began")),
    "change_order_up_middle": ("clause", _ge_only(lambda v: f"you've overtaken {_n(v)}+ time{_s(v)} during mid-race")),
    "change_order_up_finalcorner_after": ("clause", _ge_only(
        lambda v: f"you've overtaken {_n(v)}+ time{_s(v)} since the final corner began")),
    "motivation": ("clause", lambda op, v: _lo_hi(op, v,
        lambda v: f"your mood is {_MOOD.get(int(v), '?')} or better",
        lambda v: f"your mood is {_MOOD.get(int(v), '?')} or worse",
        lambda v: f"your mood is {_MOOD.get(int(v), '?')}")),
    "running_style_count_same": ("clause", lambda op, v: _lo_hi(op, v,
        lambda v: f"{_n(v)}+ runners (you included) share your running style",
        lambda v: f"at most {_n(v)} runner{_s(v)} (you included) share{'s' if v == 1 else ''} your running style",
        lambda v: "no one else shares your running style" if v == 1
        else f"exactly {_n(v)} runners (you included) share your running style")),
    "running_style_count_same_rate": ("clause", lambda op, v: _lo_hi(op, v,
        lambda v: f"at least {_n(v)}% of the field shares your running style",
        lambda v: f"at most {_n(v)}% of the field shares your running style")),
    "running_style_equal_popularity_one": ("clause", _flag("you share a running style with the most popular runner")),
    "same_skill_horse_count": ("clause", lambda op, v: "no one else in the race has this skill" if (op, v) == ("==", 1)
                               else _lo_hi(op, v, lambda v: f"{_n(v)}+ runners (you included) have this skill",
                                           lambda v: f"at most {_n(v)} runners (you included) have this skill")),
    "rotation": ("clause", lambda op, v: {1: "on right-handed (clockwise) tracks",
                                          2: "on left-handed (counterclockwise) tracks"}.get(int(v)) if op == "==" else None),
    "track_id": ("clause", lambda op, v: f"at {_track_name(v)}" if op == "==" else None),
    "ground_condition": ("clause", lambda op, v: _lo_hi(op, v,
        lambda v: f"on {_GROUND.get(int(v), '?')} ground or worse",
        lambda v: f"on {_GROUND.get(int(v), '?')} ground or better",
        lambda v: f"on {_GROUND.get(int(v), '?')} ground")),
    "season": ("clause", lambda op, v: f"in {_SEASON.get(int(v), '?')}" if op == "==" else None),
    "weather": ("clause", lambda op, v: f"in {_WEATHER.get(int(v), '?')} weather" if op == "==" else None),
    "time": ("clause", lambda op, v: _TIME.get(int(v)) if op == "==" else None),
    "grade": ("clause", lambda op, v: f"in a {_GRADE.get(int(v), '?')} race" if op == "==" else None),
    "ground_type": ("clause", lambda op, v: {1: "on turf", 2: "on dirt"}.get(int(v)) if op == "==" else None),
    "is_basis_distance": ("clause", lambda op, v: {1: "at standard distances (divisible by 400)",
                                                   0: "at non-standard distances"}.get(int(v)) if op == "==" else None),
    "random_lot": ("clause", lambda op, v: f"{_n(v)}% chance to proc" if op == "==" else None),
}
for _key, _name in _STYLE_PL.items():
    _REG[f"running_style_count_{_key}_otherself"] = ("clause", (lambda nm: lambda op, v: _lo_hi(
        op, v, lambda v: f"at least {_n(v)} other {nm}{_s(v)} in the race",
        lambda v: f"at most {_n(v)} other {nm}{_s(v)} in the race"))(_name))


# ---------------------------------------------------------------------------
# Location, requirements
# ---------------------------------------------------------------------------

def location_text(atoms: dict, d: float | None = None) -> str | None:
    corner_eq = [int(v) for op, v in atoms.get("corner", []) if op == "==" and v >= 1]
    if corner_eq:
        loc = f"on the track's {ordinal(corner_eq[0])} corner"
    elif has(atoms, "is_last_straight", "==", 1.0):
        loc = "on the last straight"
    elif has(atoms, "is_last_straight_onetime", "==", 1.0):
        loc = "as you enter the last straight"
    elif has(atoms, "is_finalcorner_laterhalf", "==", 1.0):
        loc = "in the second half of the final corner"
    elif has(atoms, "is_finalcorner", "==", 1.0) and has(atoms, "corner", "==", 0.0):
        loc = "on the final straight"
    elif has(atoms, "is_finalcorner", "==", 1.0) and has(atoms, "corner", "!=", 0.0):
        loc = "in the final corner"
    elif has(atoms, "corner", "!=", 0.0) and has(atoms, "is_finalcorner", "==", 0.0):
        loc = "on a corner before the final corner"
    elif has(atoms, "corner", "!=", 0.0):
        loc = "on a corner"
    elif has(atoms, "corner", "==", 0.0):
        loc = "on a straight"
    elif has(atoms, "is_finalcorner", "==", 1.0):
        loc = "from the final corner onward"
    else:
        loc = None

    if loc is None:
        for field, tmpl in (("phase_random", "at a random point in {p}"),
                            ("phase_firsthalf_random", "at a random point in the first half of {p}"),
                            ("phase_laterhalf_random", "at a random point in the second half of {p}"),
                            ("phase_corner_random", "at a random corner point in {p}"),
                            ("phase_straight_random", "at a random straight point in {p}")):
            for op, v in atoms.get(field, []):
                if op == "==" and int(v) in _PHASE_NAME:
                    return tmpl.format(p=_PHASE_NAME[int(v)])
        for field, txt in (("straight_random", "on a random straight"),
                           ("all_corner_random", "on a random corner"),
                           ("down_slope_random", "on a random downhill"),
                           ("up_slope_random", "on a random uphill"),
                           ("is_finalcorner_random", "at a random point in the final corner"),
                           ("last_straight_random", "at a random point on the last straight")):
            if has(atoms, field, "==", 1.0):
                loc = txt
                break
    if loc is None:
        for op, v in atoms.get("distance_rate_after_random", []):
            if op == "==" and d:
                loc = f"at a random point after {_n(v)}% of the race"
        for op, v in atoms.get("slope", []):
            if op == "==":
                loc = {0: "on flat ground", 1: "on an uphill", 2: "on a downhill"}.get(int(v))
        rem = atoms.get("remain_distance", [])
        if rem and loc is None:
            hi = [v for op, v in rem if op in ("<=", "<")]
            lo = [v for op, v in rem if op in (">=", ">")]
            eq = [v for op, v in rem if op == "=="]
            if eq:
                loc = f"with {_n(eq[0])} m to go"
            elif hi and lo and max(hi) - min(lo) <= 3:
                loc = f"with {_n((max(hi) + min(lo)) / 2)} m to go"
            elif hi and lo:
                loc = f"with {_n(min(lo))}–{_n(max(hi))} m to go"
            elif hi:
                loc = f"in the last {_n(max(hi))} m"
            elif lo:
                loc = f"with {_n(min(lo))}+ m to go"
    if loc is None and d:
        dr = atoms.get("distance_rate", [])
        lo = [v for op, v in dr if op in (">=", ">")]
        hi = [v for op, v in dr if op in ("<=", "<")]
        if lo and hi:
            loc = (f"between {_n(min(lo))}% and {_n(max(hi))}% of the race "
                   f"({min(lo) * d / 100:,.0f}–{max(hi) * d / 100:,.0f} m)")
        elif lo:
            loc = f"past {_n(min(lo))}% of the race ({min(lo) * d / 100:,.0f} m)"
        elif hi:
            loc = f"before {_n(max(hi))}% of the race ({max(hi) * d / 100:,.0f} m)"

    ph = phase_text(atoms)
    return " ".join(x for x in (loc, ph) if x) or None


def need_bits(atoms: dict, d: float, loc: str | None = None) -> tuple[list[str], list[str], list[str]]:
    """(phrases that read after "you're", standalone clauses, unmapped raw tokens)."""
    bits: list[str] = []
    clauses: list[str] = []
    for op, v in atoms.get("running_style", []):
        if op == "==":
            bits.append({1: "a Front Runner", 2: "a Pace Chaser", 3: "a Late Surger", 4: "an End Closer"}.get(int(v), "?"))
    for op, v in atoms.get("distance_type", []):
        if op == "==":
            bits.append("in " + {1: "a Sprint", 2: "a Mile", 3: "a Medium", 4: "a Long"}.get(int(v), "?") + " race")
    if atoms.get("distance_rate") and not (loc and "of the race" in loc):
        for op, v in atoms["distance_rate"]:
            if op in (">=", ">"):
                bits.append(f"past {v:g}% of the race ({v * d / 100:,.0f} m)")
            elif op in ("<=", "<"):
                bits.append(f"before {v:g}% of the race ({v * d / 100:,.0f} m)")
    rt = rank_text(rank_band(atoms))
    if rt:
        bits.append(rt)
    if has(atoms, "temptation_count", "==", 0.0):
        bits.append("not rushed")
    for op, v in atoms.get("blocked_side_continuetime", []):
        if op in (">=", ">"):
            bits.append(f"blocked from the side for {v:g}+ s")
    for op, v in atoms.get("blocked_front_continuetime", []):
        if op in (">=", ">"):
            bits.append(f"blocked in front for {v:g}+ s")
    if has(atoms, "is_lastspurt", "==", 1.0) and not (loc and "last spurt" in loc):
        bits.append("in the last spurt")
    for op, v in atoms.get("change_order_onetime", []):
        if op in ("<", "<="):
            bits.append("passing another runner")
        elif op in (">", ">="):
            bits.append("being passed by another runner")
    if any(_is_continue(f) for f in atoms):
        bits = [b if b != rt else f"{rt} the whole way" for b in bits]
    if has(atoms, "is_behind_in", "==", 1.0):
        clauses.append("the runner behind you is closer to the inner fence")

    raw: list[str] = []
    for field, vals in atoms.items():
        if field in _HANDLED or _is_continue(field):
            continue
        for op, v in vals:
            entry = _REG.get(field)
            text = entry[1](op, v) if entry else None
            if text is None:
                raw.append(f"`{field}{op}{v:g}`")
            elif entry[0] == "you":
                bits.append(text)
            else:
                clauses.append(text)
    return bits, clauses, raw


def join_you(bits: list[str]) -> str:
    if len(bits) <= 1:
        return "".join(bits)
    return ", ".join(bits[:-1]) + " and " + bits[-1]


def need_elements(atoms: dict, d: float, loc: str | None = None) -> list[str]:
    you, clauses, raw = need_bits(atoms, d, loc)
    out = ([f"if you're {join_you(you)}"] if you else []) + clauses
    if raw:
        out.append(f"also {', '.join(raw)}")
    return out


# ---------------------------------------------------------------------------
# Windows, timing
# ---------------------------------------------------------------------------

def merge(wins):
    out = []
    for a, b in sorted((float(w[0]), float(w[1])) for w in wins):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def fmt_windows(wins, d: float) -> str:
    parts = []
    for a, b in merge(wins):
        a, b = round(a), round(b)
        parts.append(f"{a:,}–{b:,} m" + (" to the finish" if b >= d else ""))
    return ", ".join(parts)


def win_paren(wins, d: float) -> str:
    """" (windows)" -- empty when the window is the whole race."""
    m = merge(wins)
    if len(m) == 1 and m[0][0] <= 1 and m[0][1] >= d - 1:
        return ""
    return f" ({fmt_windows(wins, d)})"


def timing_text(wins, p2: float, is_random: bool) -> str:
    merged = merge(wins)
    if not is_random:
        # Deterministic: checked every frame, so it fires at the first window's start.
        off = merged[0][0] - p2
        if abs(off) <= 1:
            return f"Activates right at the start of late-race ({p2:,.0f} m)"
        if off < 0:
            return f"Activates {abs(off):,.0f} m before late-race ({p2:,.0f} m)"
        return f"Activates {off:,.0f} m after late-race starts ({p2:,.0f} m)"

    # Random: the window is where it *can* fire, so use the one closest to p2.
    if any(a <= p2 <= b for a, b in merged):
        return f"Window covers the start of late-race ({p2:,.0f} m)"
    a, b = min(merged, key=lambda w: min(abs(w[0] - p2), abs(w[1] - p2)))
    if b < p2:
        return f"Closest window ends {p2 - b:,.0f} m before late-race ({p2:,.0f} m)"
    return f"Closest window starts {a - p2:,.0f} m after late-race starts ({p2:,.0f} m)"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------

# Extra effect types from uma-skill-tools' SkillType enum that skill_chart._ETYPE lacks.
_XTYPE = {10: "Start delay (multiplier)", 13: "Rushed duration", 14: "Start delay", 29: "Rushed chance",
          37: "Activates a random gold skill", 42: "Evolved skill duration",
          # 8 and 28 are not in that enum; labelled from GameTora's skill text
          # ("narrow the field of view", "increase navigation").
          8: "Field of view", 28: "Lane movement speed"}


def effect_text(effects: list) -> str:
    """Every effect, own (target 1/2) first; opponent-aimed ones labelled, duplicates merged."""
    own: list[str] = []
    other: dict[str, int] = {}
    for e in effects:
        typ, tgt, val = e.get("type"), e.get("target", 1), e.get("modifier", 0) / 10000
        label = _ETYPE.get(typ) or _XTYPE.get(typ) or f"Effect type {typ}"
        txt = f"{label} {int(val):+}" if typ in _STAT_T else f"{label} {val:+g}"
        if tgt in (1, 2):
            own.append(txt)
        else:
            other[txt] = other.get(txt, 0) + 1
    others = [f"{txt} on other runners" + (f" ({n} target groups)" if n > 1 else "") for txt, n in other.items()]
    return ", ".join(own + others) or "—"


# ---------------------------------------------------------------------------
# Description -> structured embed fields
# ---------------------------------------------------------------------------

def describe_skill(sid: str, entry: dict, name: str, course: dict, is_cm: bool = True) -> dict:
    """
    Structured description of one skill on one course.

    entry: the skill's skill_data.json entry. is_cm: the course came from a CM
    selection (a 9-horse field); otherwise rank notes carry a caveat.
    """
    d = float(course["distance"])
    p2 = phase_start(int(d), 2)
    info = {"sid": sid, "name": name, "rarity": entry.get("rarity", 0),
            "effects": [], "effect": None, "activates": [], "arms": None, "timing": None, "rating": None}

    live_alts = []
    for alt in entry["alternatives"]:
        cond, precond = alt.get("condition", ""), alt.get("precondition", "")
        live = []
        for raw, atoms in parse_groups(cond):
            wins = evaluate_trigger(raw, precond, course)
            if wins:
                live.append((atoms, wins))
        if not live:
            continue

        dur = alt["baseDuration"] / 10000.0 * d / 1000.0
        live_alts.append((alt, effect_text(alt["effects"]) + (f" for {dur:.1f} s" if dur > 0 else "")))
        if len(live_alts) > 1:
            continue          # later alternatives only contribute an effect line
        info["effect"] = live_alts[0][1]

        types = {e.get("type") for e in alt["effects"] if e.get("target", 1) in (1, 2)}
        passive = bool(types) and types <= _STAT_T

        # Clauses with identical requirements collapse into one line.
        by_need: dict[str, list] = {}
        for atoms, wins in live:
            elems = need_elements(atoms, d, location_text(atoms, d))
            need = (" — " + ", ".join(elems)) if elems else ""
            by_need.setdefault(need, []).append((atoms, wins))
        for need, members in by_need.items():
            all_wins = [w for _, ws in members for w in ws]
            kinds = {("laterhalf" if has(a, "is_finalcorner_laterhalf", "==", 1.0)
                      else "straight" if has(a, "is_finalcorner", "==", 1.0) and has(a, "corner", "==", 0.0)
                      else None) for a, _ in members}
            if passive:
                fallback = "passive for the whole race"
            elif min(float(w[0]) for w in all_wins) <= 1 and re.fullmatch(
                    r"( — if you're (a Front Runner|a Pace Chaser|a Late Surger|an End Closer))?", need):
                fallback = "from the start of the race"
            else:
                fallback = "whenever its conditions are met"
            if kinds == {"laterhalf", "straight"}:
                loc = "from halfway through the final corner onward"
            else:
                loc = " or ".join(dict.fromkeys(location_text(a, d) or fallback for a, _ in members))
            paren = "" if "m)" in loc else win_paren(all_wins, d)
            info["activates"].append(f"{_cap(loc)}{paren}{need}")

        pre_lines = []
        for raw, atoms in parse_groups(precond):
            wins = evaluate_trigger(raw, "", course)
            loc = location_text(atoms, d)
            you, clauses, raw_tokens = need_bits(atoms, d, loc)
            loc_txt = (loc + ("" if "m)" in loc or not wins else win_paren(wins, d))) if loc else None
            parts = ([loc_txt] if loc_txt else []) + ([f"you're {join_you(you)}"] if you else []) + clauses + raw_tokens
            if parts:
                pre_lines.append(", ".join(parts))
        info["arms"] = "\n".join(_cap(p) for p in pre_lines) or None

        if types & ({_ACCEL_TYPE} | _VELOCITY_TYPES):
            all_wins = [w for _, ws in live for w in ws]
            info["timing"] = timing_text(all_wins, p2, _has_random_trigger(cond))
        if _ACCEL_TYPE in types:
            verdict = accel_verdict(cond, precond, alt["effects"], course, sid.startswith("9"))
            if verdict:
                info["rating"] = verdict.split(", ")[0]

    if len(live_alts) > 1:
        same_cond = len({a.get("condition") for a, _ in live_alts}) == 1
        for i, (a, txt) in enumerate(live_alts):
            if same_cond and a.get("precondition") and not live_alts[-1][0].get("precondition"):
                label = "when armed"
            elif same_cond and not a.get("precondition"):
                label = "otherwise"
            else:
                label = f"alt {i + 1}"
            info["effects"].append(f"{txt} — {label}")
    elif live_alts:
        info["effects"].append(live_alts[0][1])
    texts = info["activates"] + ([info["arms"]] if info["arms"] else [])
    info["rank_note"] = (not is_cm) and any(_RANK_RE.search(t) for t in texts)
    return info


def _has_random_trigger(cond: str) -> bool:
    # Only the activation condition counts; the precondition is a pre-load gate.
    return any(m.group(1) in _RANDOM_FIELDS for m in _COND_RE.finditer(cond))


_RANK_RE = re.compile(r"\b\d+(st|nd|rd|th)\b(?! corner)")


def embed_fields(info: dict) -> list[tuple[str, str, bool]]:
    """(name, value, inline) rows for the /skill embed; values are capped to Discord's 1024."""
    if not info["effect"]:
        return [("Effect", "Can't activate on this course.", False)]
    fields = [
        ("Effect", "\n".join(info["effects"]), False),
        ("Activates", "\n".join(info["activates"])
         + ("\n*Ranks assume a 9-horse CM field.*" if info.get("rank_note") else ""), False),
    ]
    if info["arms"]:
        fields.append(("Precondition", info["arms"], False))
    if info["timing"]:
        fields.append(("Timing", info["timing"], info["rating"] is not None))
    if info["rating"]:
        fields.append(("Rating", info["rating"], True))
    return [(n, v[:1024], inline) for n, v, inline in fields]
