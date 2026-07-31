"""
skill_chart.py

Generates a Pillow PNG chart visualising where a skill activates on a course.
Faithful port of RaceTrack.tsx from uma-skill-tools.

Public API
----------
build_skill_chart(condition, precondition, base_duration, course,
                  course_name="", dark=False) -> bytes
    Returns raw PNG bytes.

evaluate_trigger(condition, precondition, course) -> list[tuple[float, float]]
    Returns list of (start_m, end_m) ranges where the skill activates.

is_immediate_policy(condition) -> bool
    True if trigger renders as a single line (not a filled box).

state_text(condition) -> str
    Human-readable summary of runtime state conditions.

_format_cond_block(cond) -> str
    Discord-markdown formatted condition string.

format_effects(effects) -> str
    Human-readable effect summary from uma-skill-tools effect list.
"""
from __future__ import annotations

import io
import math
import re

from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Race sizes for order_rate → position conversion
# ---------------------------------------------------------------------------
CM_HORSES  = 9   # Champion's Meeting
LOH_HORSES = 12  # League of Heroes


# ---------------------------------------------------------------------------
# Phase helpers — exact formulas from uma-skill-tools CourseData.ts
# ---------------------------------------------------------------------------

def phase_start(distance: int, phase: int) -> float:
    return {0: 0.0, 1: distance/6, 2: distance*2/3, 3: distance*5/6}.get(phase, 0.0)

def phase_end(distance: int, phase: int) -> float:
    return {0: distance/6, 1: distance*2/3, 2: distance*5/6, 3: float(distance)}.get(phase, float(distance))


# ---------------------------------------------------------------------------
# Condition → geometry range evaluator
# ---------------------------------------------------------------------------

_COND_RE   = re.compile(r"([a-z_]+)(>=|<=|==|!=|>|<)(-?\d+(?:\.\d+)?)")
_GEO_FIELDS = {
    # Phase position
    "phase",
    "phase_random",
    "phase_firsthalf",        "phase_firsthalf_random",
    "phase_laterhalf",        "phase_laterhalf_random",
    "phase_firstquarter",     "phase_firstquarter_random",
    # Distance / progress
    "distance_rate",          "distance_rate_after_random",
    "remain_distance",
    "furlong",
    # Corners
    "corner",
    "corner_random",          "all_corner_random",
    "is_finalcorner",         "is_finalcorner_random",
    "is_finalcorner_laterhalf",
    "phase_corner_random",
    # Straights
    "is_last_straight",       "is_last_straight_onetime",
    "last_straight_random",
    "straight_front_type",    "straight_random",
    "phase_straight_random",
    "phase_first_half_straight_random",
    "phase_latter_half_straight_random",
    # Slopes
    "slope",
    "up_slope_random",        "down_slope_random",
    # Last spurt
    "is_lastspurt",
    # Course properties (filters that can eliminate trigger)
    "distance_type", "ground_type", "course_distance",
    "track_id", "rotation", "corner_count",
    "is_basis_distance", "is_dirtgrade", "is_tight_track",
}

# Conditions that use a non-Immediate policy (random/erlang/etc.) → show as region box
# Everything else uses ImmediatePolicy → show as a single vertical line
_RANDOM_FIELDS = {
    # Geo conditions that pick a random point (render as filled box, not line)
    "corner_random", "all_corner_random",
    "phase_random",
    "phase_firsthalf_random", "phase_firstquarter_random", "phase_laterhalf_random",
    "phase_corner_random",
    "phase_straight_random", "straight_random", "last_straight_random",
    "phase_first_half_straight_random", "phase_latter_half_straight_random",
    "is_finalcorner_random",
    "distance_rate_after_random",
    "down_slope_random", "up_slope_random",
    "run_at_full_speed_random",
    # Runtime-state random fields (not chartable geometrically)
    "bashin_diff_behind", "bashin_diff_infront",
    "behind_near_lane_time", "behind_near_lane_time_set1", "blocked_all_continuetime",
    "blocked_front", "blocked_front_continuetime", "blocked_side_continuetime",
    "change_order_onetime", "change_order_up_end_after", "change_order_up_finalcorner_after",
    "change_order_up_middle", "compete_fight_count", "infront_near_lane_time",
    "is_move_lane", "is_overtake", "is_surrounded", "near_count",
    "overtake_target_no_order_up_time", "overtake_target_time",
}

def is_immediate_policy(condition: str) -> bool:
    """Return True if all conditions use ImmediatePolicy (renders as line, not filled box)."""
    for m in _COND_RE.finditer(condition):
        if m.group(1) in _RANDOM_FIELDS:
            return False
    return True


def _merge(ranges):
    if not ranges: return []
    out = [list(sorted(ranges)[0])]
    for s, e in sorted(ranges)[1:]:
        if s <= out[-1][1]: out[-1][1] = max(out[-1][1], e)
        else: out.append([s, e])
    return [(a, b) for a, b in out]

def _intersect(a, b):
    return _merge([(max(as_, bs), min(ae, be)) for as_, ae in a for bs, be in b if max(as_, bs) < min(ae, be)])

def _subtract(base, excl):
    result = list(base)
    for es, ee in excl:
        new = []
        for bs, be in result:
            if be <= es or bs >= ee: new.append((bs, be))
            else:
                if bs < es: new.append((bs, es))
                if be > ee: new.append((ee, be))
        result = new
    return result


_DIRTGRADE_TRACKS = {10101, 10103, 10104, 10105}   # Ooi, Kawasaki, Funabashi, Morioka
_TIGHT_TRACKS     = {10001, 10002, 10004, 10010,   # Sapporo, Hakodate, Fukushima, Kokura
                     10103, 10104}                  # Kawasaki, Funabashi
FURLONG_M = 201.168   # 1 furlong in metres


def _phase_range(d, ph):
    return (phase_start(d, ph), phase_end(d, ph))

def _phase_first_half(d, ph):
    ps, pe = phase_start(d, ph), phase_end(d, ph)
    return (ps, (ps + pe) / 2)

def _phase_latter_half(d, ph):
    ps, pe = phase_start(d, ph), phase_end(d, ph)
    return ((ps + pe) / 2, pe)

def _phase_first_quarter(d, ph):
    ps, pe = phase_start(d, ph), phase_end(d, ph)
    return (ps, ps + (pe - ps) / 4)


def _course_prop_match(op, course_val, ival):
    """Return True if course_val satisfies op ival."""
    return {
        "==": course_val == ival, "!=": course_val != ival,
        ">=": course_val >= ival, "<=": course_val <= ival,
        ">":  course_val >  ival, "<":  course_val <  ival,
    }.get(op, False)


def _eval_and_group(group: str, course: dict) -> list:
    d         = course["distance"]
    corners   = course.get("corners", [])
    straights = course.get("straights", [])
    slopes    = course.get("slopes", [])
    c_ranges  = [(c["start"], c["start"] + c["length"]) for c in corners]
    s_ranges  = [(s["start"], s["end"]) for s in straights]
    last_home = next(((s["start"], s["end"]) for s in reversed(straights) if s.get("frontType") == 1),
                     (phase_start(d, 3), float(d)))
    last_corn = (corners[-1]["start"], corners[-1]["start"] + corners[-1]["length"]) if corners else None

    result = [(0.0, float(d))]
    for m in _COND_RE.finditer(group):
        field, op, raw = m.group(1), m.group(2), m.group(3)
        if field not in _GEO_FIELDS:
            continue
        val  = float(raw)
        ival = int(val)
        con  = []

        # ── Phase ────────────────────────────────────────────────────────────
        if field == "phase":
            if op in (">=", ">"):
                ph = ival if op == ">=" else ival + 1
                con = [(phase_start(d, ph), float(d))]
            elif op in ("<=", "<"):
                ph = ival if op == "<=" else ival - 1
                con = [(0.0, phase_end(d, ph))]
            elif op == "==":
                con = [_phase_range(d, ival)]

        elif field == "phase_random":
            con = [_phase_range(d, ival)]

        elif field in ("phase_firsthalf", "phase_firsthalf_random"):
            con = [_phase_first_half(d, ival)]

        elif field in ("phase_laterhalf", "phase_laterhalf_random"):
            con = [_phase_latter_half(d, ival)]

        elif field in ("phase_firstquarter", "phase_firstquarter_random"):
            con = [_phase_first_quarter(d, ival)]

        # ── Distance / progress ──────────────────────────────────────────────
        elif field == "distance_rate":
            pct = val / 100.0
            if op in (">=", ">"):   con = [(pct * d, float(d))]
            elif op in ("<=", "<"): con = [(0.0, pct * d)]

        elif field == "distance_rate_after_random":
            pct = val / 100.0
            con = [(pct * d, float(d))]

        elif field == "remain_distance":
            m_pos = d - val
            if op in ("<=", "<"):   con = [(max(0.0, m_pos), float(d))]
            elif op in (">=", ">"): con = [(0.0, max(0.0, m_pos))]

        elif field == "furlong":
            con = [((ival - 1) * FURLONG_M, min(ival * FURLONG_M, float(d)))]

        # ── Corners ──────────────────────────────────────────────────────────
        elif field == "corner":
            if op == "!=" and ival == 0:
                con = c_ranges[:]
            elif op == "==" and ival == 0:
                con = _subtract([(0.0, float(d))], c_ranges)
            elif op == "==" and 0 < ival <= len(corners):
                c = corners[ival - 1]
                con = [(c["start"], c["start"] + c["length"])]

        elif field == "corner_random":
            if op == "==" and 0 < ival <= len(corners):
                c = corners[ival - 1]
                con = [(c["start"], c["start"] + c["length"])]

        elif field == "all_corner_random":
            if ival == 1:
                if c_ranges:  con = c_ranges[:]
                else:
                    result = []; break

        elif field == "is_finalcorner":
            if last_corn:
                if ival == 1: con = [last_corn]
                else:         con = _subtract([(0.0, float(d))], [last_corn])
            elif ival == 1:
                result = []; break

        elif field == "is_finalcorner_random":
            if last_corn and ival == 1:
                con = [last_corn]
            elif ival == 1:
                result = []; break

        elif field == "is_finalcorner_laterhalf":
            if last_corn and ival == 1:
                mid = (last_corn[0] + last_corn[1]) / 2
                con = [(mid, last_corn[1])]
            elif ival == 1:
                result = []; break

        elif field == "phase_corner_random":
            ph_rng = [_phase_range(d, ival)]
            inter  = _intersect(ph_rng, _merge(c_ranges))
            if inter:  con = inter
            else:      result = []; break

        # ── Straights ────────────────────────────────────────────────────────
        elif field in ("is_last_straight", "is_last_straight_onetime"):
            if ival == 1:   con = [last_home]
            else:           con = _subtract([(0.0, float(d))], [last_home])

        elif field == "last_straight_random":
            if ival == 1:
                con = [last_home]

        elif field == "straight_front_type":
            typed = [(s["start"], s["end"]) for s in straights if s.get("frontType") == ival]
            con   = typed if op == "==" else _subtract([(0.0, float(d))], typed)

        elif field == "straight_random":
            if ival == 1:
                if s_ranges:  con = s_ranges[:]
                else:         result = []; break

        elif field == "phase_straight_random":
            ph_rng = [_phase_range(d, ival)]
            inter  = _intersect(ph_rng, _merge(s_ranges))
            if inter:  con = inter
            else:      result = []; break

        elif field == "phase_first_half_straight_random":
            ph_rng = [_phase_first_half(d, ival)]
            inter  = _intersect([ph_rng], _merge(s_ranges))
            if inter:  con = inter
            else:      result = []; break

        elif field == "phase_latter_half_straight_random":
            ph_rng = [_phase_latter_half(d, ival)]
            inter  = _intersect([ph_rng], _merge(s_ranges))
            if inter:  con = inter
            else:      result = []; break

        # ── Slopes ───────────────────────────────────────────────────────────
        elif field == "slope":
            if ival == 0:
                all_sl = [(s["start"], s["start"] + s["length"]) for s in slopes]
                con    = _subtract([(0.0, float(d))], all_sl)
            elif ival == 1:
                con = [(s["start"], s["start"] + s["length"]) for s in slopes if s["slope"] > 0]
                if not con:
                    result = []; break
            elif ival == 2:
                con = [(s["start"], s["start"] + s["length"]) for s in slopes if s["slope"] < 0]
                if not con:
                    result = []; break

        elif field in ("up_slope_random", "down_slope_random"):
            want_up = field == "up_slope_random"
            segs = [(s["start"], s["start"] + s["length"])
                    for s in slopes if (s["slope"] > 0) == want_up]
            if op == "==" and ival == 1:
                if segs:  con = segs
                else:     result = []; break

        # ── Last spurt ───────────────────────────────────────────────────────
        elif field == "is_lastspurt":
            ph3 = (phase_start(d, 3), float(d))
            if op == "==" and ival == 1:
                con = [ph3]
            elif op == "==" and ival == 0:
                con = _subtract([(0.0, float(d))], [ph3])

        # ── Course property filters ───────────────────────────────────────────
        elif field == "distance_type":
            if not _course_prop_match(op, course.get("distanceType", 0), ival):
                result = []; break

        elif field == "ground_type":
            if not _course_prop_match(op, course.get("surface", 1), ival):
                result = []; break

        elif field == "course_distance":
            if not _course_prop_match(op, course["distance"], ival):
                result = []; break

        elif field == "track_id":
            if not _course_prop_match(op, course.get("raceTrackId", 0), ival):
                result = []; break

        elif field == "rotation":
            if not _course_prop_match(op, course.get("turn", 0), ival):
                result = []; break

        elif field == "corner_count":
            if not _course_prop_match(op, len(corners), ival):
                result = []; break

        elif field == "is_basis_distance":
            is_core = 1 if d % 400 == 0 else 0
            if not _course_prop_match(op, is_core, ival):
                result = []; break

        elif field == "is_dirtgrade":
            is_dg = 1 if course.get("raceTrackId", 0) in _DIRTGRADE_TRACKS else 0
            if not _course_prop_match(op, is_dg, ival):
                result = []; break

        elif field == "is_tight_track":
            is_tt = 1 if course.get("raceTrackId", 0) in _TIGHT_TRACKS else 0
            if not _course_prop_match(op, is_tt, ival):
                result = []; break

        if con:
            result = _intersect(result, _merge(con))
        if not result:
            break
    return result


def evaluate_trigger(condition: str, precondition: str, course: dict):
    if not condition:
        return [(0.0, float(course["distance"]))]
    groups = [r for g in condition.split("@") for r in _eval_and_group(g, course)]
    result = _merge(groups)
    if precondition:
        pre = _merge([r for g in precondition.split("@") for r in _eval_and_group(g, course)])
        if pre:
            result = _intersect(result, pre)
    return result


def state_text(condition: str) -> str:
    _ST = {"order", "order_rate", "running_style", "is_overtake",
           "change_order_onetime", "bashin_diff_behind", "bashin_diff_infront",
           "hp_per", "motivation", "accumulatetime", "blocked_front_continuetime",
           "blocked_front", "corner_random", "is_finalcorner_random", "phase_random",
           "is_activate_any_skill", "is_activate_heal_skill"}
    parts = []
    for m in _COND_RE.finditer(condition):
        f, op, raw = m.group(1), m.group(2), m.group(3)
        if f not in _ST: continue
        iv = int(float(raw))
        if f == "order_rate":
            parts.append(f"Top {iv}%" if op == "<=" else f">={iv}% back")
        elif f == "order":
            parts.append(f"Position {op} {iv}")
        elif f == "running_style":
            _s = {1:'Leader',2:'Front',3:'Mid',4:'Closer'}.get(iv, str(iv))
            parts.append(f"Style: {_s}")
        elif f == "distance_type":
            _d = {1:'Sprint',2:'Mile',3:'Medium',4:'Long'}.get(iv, '?')
            parts.append(f"Track: {_d}")
        elif f == "hp_per":
            parts.append(f"HP {op} {iv}%")
        elif f == "is_overtake" and iv == 1:
            parts.append("While overtaking")
        elif f not in ("corner_random", "is_finalcorner_random", "phase_random",
                       "is_activate_any_skill", "is_activate_heal_skill"):
            parts.append(f"{f}{op}{raw}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Condition / effect formatters (used in Discord embed text)
# ---------------------------------------------------------------------------

def _order_rate_note(op: str, val_pct: float) -> str:
    """Convert an order_rate condition value to per-race-size position labels."""
    pct = val_pct / 100.0
    if op in (">=", ">"):
        cm  = math.ceil(pct * CM_HORSES)
        loh = math.ceil(pct * LOH_HORSES)
        return f"(CM >={cm} | LoH >={loh})"
    elif op in ("<=", "<"):
        cm  = max(math.floor(pct * CM_HORSES),  1)
        loh = max(math.floor(pct * LOH_HORSES), 1)
        return f"(CM <={cm} | LoH <={loh})"
    return ""


_ORDER_RATE_RE = re.compile(r"^order_rate(>=|<=|==|!=|>|<)(-?\d+(?:\.\d+)?)$")

def _format_cond_block(cond: str) -> str:
    """
    Format a raw uma-tools condition string into a readable multi-line block.

    - OR groups (split by @) are separated with a blank line and '(OR)' label
    - AND conditions within a group use '&' prefix on all but the first
    - order_rate lines get an appended position note like (CM ≥5 | LoH ≥6)
    """
    if not cond:
        return ""
    groups = cond.split("@")
    lines  = []
    for gi, group in enumerate(groups):
        if gi > 0:
            lines.append("*(OR)*")
        tokens = [t.strip() for t in group.split("&") if t.strip()]
        for ti, token in enumerate(tokens):
            prefix = "&" if ti > 0 else ""
            note   = ""
            m = _ORDER_RATE_RE.match(token)
            if m:
                note = "  " + _order_rate_note(m.group(1), float(m.group(2)))
            lines.append(f"`{prefix}{token}`{note}")
    return "\n".join(lines)


_ETYPE = {
    1:"Speed", 2:"Stamina", 3:"Power", 4:"Guts", 5:"Wisdom",
    9:"HP Recovery", 21:"Instant speed", 22:"Speed (w/decel)",
    27:"Target speed", 31:"Acceleration",
}
_STAT_T = {1, 2, 3, 4, 5}

def format_effects(effects) -> str:
    parts = []
    for e in effects:
        n = _ETYPE.get(e.get("type", 0))
        if not n: continue
        val = e.get("modifier", 0) / 10000
        if e["type"] in _STAT_T:
            parts.append(f"{n} {int(val):+}")
        else:
            parts.append(f"{n} {val:+g}")
    return ", ".join(parts) or "—"


# ---------------------------------------------------------------------------
# Chart — faithful port of RaceTrack.tsx
# ---------------------------------------------------------------------------

# ---- Colors copied verbatim from RaceTrack.tsx ----
_C = lambda r, g, b: (r, g, b, 255)

# terrain
BG_TERRAIN       = _C(211, 243,  68)
BG_DIVIDER       = _C(140, 170,  10)
TERR_STROKE      = _C( 90, 120,   6)

# slope row
SLOPE_BG         = _C(239, 229, 241)
SLOPE_BG_ACC     = _C(163, 106, 175)
UPHILL_M  = [_C(234, 207, 147), _C(229, 196, 120)]
UPHILL_A  = [_C(191, 143,  37), _C(175, 132,  33)]
DOWNHILL_M= [_C( 82, 195, 184), _C(116, 206, 198)]
DOWNHILL_A= [_C( 42, 123, 115), _C( 50, 142, 134)]

# section row
SECT_BG          = _C(232, 232, 232)
SECT_BG_ACC      = _C(139, 139, 139)
STR_M  = [_C(209, 235, 255), _C(185, 224, 255)]
STR_A  = [_C( 23, 154, 255), _C(  9, 146, 254)]
COR_M  = [_C(255, 216, 185), _C(254, 228, 209)]
COR_A  = [_C(254, 117,   9), _C(250, 121,  27)]

# phase row
PH_M = {0: _C(  0, 154, 111), 1: _C(242, 233, 103),
        2: _C(209, 134, 175), 3: _C(199, 109, 159)}
PH_A = {0: _C(  0,  92,  66), 1: _C(190, 179,  16),
        2: _C(149,  56, 107), 3: _C(133,  51,  96)}
PH_LBL_GLOBAL = {0: "Early-race", 1: "Mid-race", 2: "Late-race", 3: "Last spurt"}

# ruler
RULER_BG         = _C(228, 235, 240)
RULER_TICK       = _C(107, 145, 173)

# text
TEXT_BROWN       = (121,  64,  22, 255)
TEXT_DARK        = ( 80,  80,  80, 255)
TEXT_STROKE      = (255, 255, 255, 200)   # white outline behind row labels

# trigger (from app.tsx colors[0])
TRIG_STROKE      = (205,  11,  11, 230)
TRIG_FILL        = (247, 115, 115,  77)

# ---- Image dimensions ----
W, H = 960, 240

# ---- Row y positions (matching SVG percentages) ----
TERR_TOP  = 0
TERR_H    = round(0.262 * H)           # 62
DIV_TOP   = TERR_H
DIV_H     = round(0.018 * H)           # 4
SLOPE_TOP = DIV_TOP + DIV_H            # 66
ROW_H     = round(0.18  * H)           # 43
ROW_M     = round(0.9   * ROW_H)       # 38  (main area)
ROW_A     = ROW_H - ROW_M              # 5   (accent bar)
SECT_TOP  = SLOPE_TOP + ROW_H          # 109
PHASE_TOP = SECT_TOP  + ROW_H          # 152
RULER_TOP = PHASE_TOP + ROW_H          # 195
RULER_H   = H - RULER_TOP              # 45


def _load_fonts():
    bold_paths = [
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    reg_paths = [
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    font_md = None
    for p in bold_paths:
        try:
            font_md = ImageFont.truetype(p, 16)
            break
        except Exception:
            pass
    for p in reg_paths:
        try:
            font_sm = ImageFont.truetype(p, 13)
            font_xs = ImageFont.truetype(p, 11)
            return (font_md or ImageFont.truetype(p, 16), font_sm, font_xs)
        except Exception:
            pass
    d = ImageFont.load_default()
    return d, d, d


def _tw(draw, txt, font):
    try:
        bb = draw.textbbox((0, 0), txt, font=font)
        return bb[2] - bb[0]
    except Exception:
        return len(txt) * 7


def _center(draw, x1, x2, y_top, h, txt, font, fill):
    """Draw text centred in [x1..x2] × [y_top..y_top+h] if it fits."""
    try:
        bb     = draw.textbbox((0, 0), txt,         font=font)  # actual (for width)
        bb_cap = draw.textbbox((0, 0), txt.upper(), font=font)  # caps (for height, no descenders)
        tw     = bb[2]     - bb[0]
        th_cap = bb_cap[3] - bb_cap[1]
    except Exception:
        tw, th_cap = len(txt) * 7, 10
        bb = bb_cap = (0, 0, tw, 10)
    if tw + 4 > (x2 - x1):
        return
    cx = (x1 + x2) // 2
    cy = y_top + h // 2
    draw.text((cx - tw // 2 - bb[0], cy - th_cap // 2 - bb_cap[1]),
              txt, fill=fill, font=font, stroke_width=1, stroke_fill=TEXT_STROKE)


def _dist_marker(draw, meters, dist_total, y_row_top, font, text_color, up=False, drawn_xs=None):
    """
    Tick + 'Xm' label at a section boundary.
    The tick is always drawn.  The label is skipped when it would overlap a
    previously drawn label in the same row (tracked via drawn_xs set).
    """
    px_x = int(meters / dist_total * W)
    tick_top = y_row_top - 4
    tick_bot = y_row_top + 4
    draw.line([px_x, tick_top, px_x, tick_bot], fill=text_color, width=1)

    lbl = f"{int(meters)}m"
    tw  = _tw(draw, lbl, font)
    if drawn_xs is not None:
        if any(abs(px_x - x) < max(tw + 8, 140) for x in drawn_xs):
            return
        drawn_xs.add(px_x)

    lx = px_x - tw // 2
    ly = tick_top - 12 if up else tick_bot + 1
    draw.text((lx, ly), lbl, fill=text_color, font=font)


def build_skill_chart(
    condition: str,
    precondition: str,
    base_duration,
    course: dict,
    course_name: str = "",
    dark: bool = False,
) -> bytes:
    """
    Generate a skill activation chart PNG.

    Parameters
    ----------
    condition : str
        Raw uma-skill-tools trigger condition string.
    precondition : str
        Raw uma-skill-tools precondition string (may be empty).
    base_duration :
        Unused currently; reserved for future duration overlay.
    course : dict
        Course data dict from course_data.json (keyed by course ID).
    course_name : str
        Optional display name (currently unused in image).
    dark : bool
        If True, render dark-mode variant.

    Returns
    -------
    bytes
        Raw PNG image bytes.
    """
    distance  = course["distance"]
    corners   = course.get("corners", [])
    straights = course.get("straights", [])
    slopes    = course.get("slopes", [])

    font_md, font_sm, font_xs = _load_fonts()

    if dark:
        bg_color   = (0,   0,   0,   0)
        terrain_bg = (0,   0,   0,   0)
        text_color = (230, 230, 230, 255)
        _f = lambda c: c
    else:
        bg_color   = (100, 100, 100, 128)
        terrain_bg = (0,   0,   0,   0)
        text_color = TEXT_BROWN
        _f = lambda c: (c[0], c[1], c[2], 192)

    def px(m): return int(m / distance * W)

    img  = Image.new("RGBA", (W, H), bg_color)
    draw = ImageDraw.Draw(img)

    # ---- 1.  Terrain elevation silhouette (0 – TERR_H) ----
    full_sl = []
    prev = 0
    for s in sorted(slopes, key=lambda s: s["start"]):
        if s["start"] > prev:
            full_sl.append({"start": prev, "length": s["start"] - prev, "slope": 0})
        full_sl.append(dict(s))
        prev = s["start"] + s["length"]
    if prev < distance:
        full_sl.append({"start": prev, "length": distance - prev, "slope": 0})

    running, highest, lowest = 0.0, 1.0, 0.0
    for s in slopes:
        running += s["slope"] / 10000 * s["length"]
        highest = max(highest, running)
        lowest  = min(lowest,  running)
    hr = highest - (0.0 if lowest + highest > -30 else lowest)
    if hr == 0: hr = 1.0

    heights = [50.0]
    for s in full_sl:
        heights.append(heights[-1] - (s["slope"] / 10000 * s["length"]) / hr * 40)

    draw.rectangle([0, TERR_TOP, W - 1, TERR_TOP + TERR_H - 1], fill=terrain_bg)
    for i, s in enumerate(full_sl):
        x1  = px(s["start"])
        x2  = px(s["start"] + s["length"])
        h0  = heights[i]
        h1  = heights[i + 1]
        py0 = TERR_TOP + int(h0 / 100 * TERR_H)
        py1 = TERR_TOP + int(h1 / 100 * TERR_H)
        bot = TERR_TOP + TERR_H
        if s["slope"] == 0:
            draw.rectangle([x1, py0, x2, bot], fill=_f(BG_TERRAIN))
        else:
            draw.polygon([(x1, py0), (x1, bot), (x2, bot), (x2, py1)], fill=_f(BG_TERRAIN))

    # Top contour stroke
    top_pts = []
    for i, s in enumerate(full_sl):
        x1  = px(s["start"])
        x2  = px(s["start"] + s["length"])
        py0 = TERR_TOP + int(heights[i]     / 100 * TERR_H)
        py1 = TERR_TOP + int(heights[i + 1] / 100 * TERR_H)
        if not top_pts:
            top_pts.append((x1, py0))
        top_pts.append((x2, py1))
    if len(top_pts) >= 2:
        draw.line(top_pts, fill=TERR_STROKE, width=2)

    # ---- 2.  Divider ----
    draw.rectangle([0, DIV_TOP, W - 1, DIV_TOP + DIV_H - 1], fill=_f(BG_DIVIDER))

    # ---- 3.  Slope row ----
    draw.rectangle([0, SLOPE_TOP,       W - 1, SLOPE_TOP + ROW_H - 1], fill=_f(SLOPE_BG))
    draw.rectangle([0, SLOPE_TOP + ROW_M, W - 1, SLOPE_TOP + ROW_H - 1], fill=SLOPE_BG_ACC)

    upi, dni = 0, 0
    for s in slopes:
        x1, x2 = px(s["start"]), px(s["start"] + s["length"])
        w_frac  = s["length"] / distance
        if s["slope"] > 0:
            draw.rectangle([x1, SLOPE_TOP,        x2, SLOPE_TOP + ROW_M - 1], fill=_f(UPHILL_M[upi % 2]))
            draw.rectangle([x1, SLOPE_TOP + ROW_M, x2, SLOPE_TOP + ROW_H - 1], fill=UPHILL_A[upi % 2])
            lbl = "Uphill" if w_frac >= 0.085 else "Up"
            _center(draw, x1, x2, SLOPE_TOP, ROW_M, lbl, font_md, text_color)
            upi += 1
        else:
            draw.rectangle([x1, SLOPE_TOP,        x2, SLOPE_TOP + ROW_M - 1], fill=_f(DOWNHILL_M[dni % 2]))
            draw.rectangle([x1, SLOPE_TOP + ROW_M, x2, SLOPE_TOP + ROW_H - 1], fill=DOWNHILL_A[dni % 2])
            lbl = "Downhill" if w_frac >= 0.085 else "Dn"
            _center(draw, x1, x2, SLOPE_TOP, ROW_M, lbl, font_md, text_color)
            dni += 1

    slope_xs = set()
    marked = set()
    for i, s in enumerate(slopes):
        for m in (s["start"], s["start"] + s["length"]):
            if m not in marked and m != 0 and m != distance:
                up = (i > 0 and slopes[i - 1]["start"] + slopes[i - 1]["length"] == m and
                      s["length"] < distance * 0.05)
                _dist_marker(draw, m, distance, SLOPE_TOP, font_sm, (0, 0, 0, 255), up=up, drawn_xs=slope_xs)
                marked.add(m)

    # ---- 4.  Section row (corners + straights) ----
    draw.rectangle([0, SECT_TOP,        W - 1, SECT_TOP + ROW_H - 1], fill=_f(SECT_BG))
    draw.rectangle([0, SECT_TOP + ROW_M, W - 1, SECT_TOP + ROW_H - 1], fill=SECT_BG_ACC)

    for i, s in enumerate(straights):
        x1, x2 = px(s["start"]), px(s["end"])
        draw.rectangle([x1, SECT_TOP,        x2, SECT_TOP + ROW_M - 1], fill=_f(STR_M[i % 2]))
        draw.rectangle([x1, SECT_TOP + ROW_M, x2, SECT_TOP + ROW_H - 1], fill=STR_A[i % 2])
        w_frac = (s["end"] - s["start"]) / distance
        lbl = "Straight" if w_frac >= 0.085 else "Str"
        _center(draw, x1, x2, SECT_TOP, ROW_M, lbl, font_md, text_color)

    n_corners = len(corners)
    for i, c in enumerate(corners):
        x1, x2 = px(c["start"]), px(c["start"] + c["length"])
        draw.rectangle([x1, SECT_TOP,        x2, SECT_TOP + ROW_M - 1], fill=_f(COR_M[i % 2]))
        draw.rectangle([x1, SECT_TOP + ROW_M, x2, SECT_TOP + ROW_H - 1], fill=COR_A[i % 2])
        cn     = 4 - (n_corners - i - 1) % 4
        w_frac = c["length"] / distance
        lbl    = f"Corner {cn}" if w_frac >= 0.085 else f"C{cn}"
        _center(draw, x1, x2, SECT_TOP, ROW_M, lbl, font_md, text_color)

    sections = sorted(
        [(s["start"], s["end"]) for s in straights] +
        [(c["start"], c["start"] + c["length"]) for c in corners],
        key=lambda s: s[0],
    )
    sect_xs = set()
    marked = set()
    for i, (ss, se) in enumerate(sections):
        for m in (ss, se):
            if m not in marked and m != 0 and m != distance:
                up = (i > 0 and sections[i - 1][1] == m and
                      (se - ss) < distance * 0.05)
                _dist_marker(draw, m, distance, SECT_TOP, font_sm, (0, 0, 0, 255), up=up, drawn_xs=sect_xs)
                marked.add(m)

    # ---- 5.  Phase row ----
    ph_fracs = [(0.0, 1/6), (1/6, 2/3), (2/3, 5/6), (5/6, 1.0)]
    for ph, (frac_s, frac_e) in enumerate(ph_fracs):
        x1, x2 = int(frac_s * W), int(frac_e * W)
        draw.rectangle([x1, PHASE_TOP,        x2, PHASE_TOP + ROW_M - 1], fill=_f(PH_M[ph]))
        draw.rectangle([x1, PHASE_TOP + ROW_M, x2, PHASE_TOP + ROW_H - 1], fill=PH_A[ph])
        _center(draw, x1, x2, PHASE_TOP, ROW_M, PH_LBL_GLOBAL[ph], font_md, text_color)

    for ph in (1, 2, 3):
        m = phase_start(distance, ph)
        _dist_marker(draw, m, distance, PHASE_TOP, font_sm, (0, 0, 0, 255))

    # ---- 6.  Ruler (24 equal segments, numbers 1-24) ----
    draw.rectangle([0, RULER_TOP, W - 1, H - 1], fill=_f(RULER_BG))
    for i in range(25):
        tx = int(i / 24 * W)
        sw = 2 if (i == 0 or i == 24) else 1
        ty = RULER_TOP + int(0.86 * RULER_H)
        draw.line([tx, ty, tx, H - 1], fill=RULER_TICK, width=sw)
    for i in range(1, 25):
        cx  = int((i - 0.5) / 24 * W)
        lbl = str(i)
        tw  = _tw(draw, lbl, font_xs)
        ty  = RULER_TOP + int(0.34 * RULER_H)
        draw.text((cx - tw // 2, ty), lbl, fill=(0, 0, 0, 255), font=font_xs)
    draw.rectangle([0, RULER_TOP + int(0.982 * RULER_H), W - 1, H - 1], fill=RULER_TICK)

    # ---- 7.  Trigger overlay (alpha-composited) ----
    trigger_ranges = evaluate_trigger(condition, precondition, course)
    is_imm = is_immediate_policy(condition + "&" + precondition)

    ov  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ovd = ImageDraw.Draw(ov)

    if trigger_ranges:
        if is_imm:
            lx = px(trigger_ranges[0][0])
            ovd.line([lx, 0, lx, H - 1], fill=TRIG_STROKE, width=2)
        else:
            for ts, te in trigger_ranges:
                x1, x2 = px(ts), px(te)
                ovd.rectangle([x1, 0, x2, H - 1], fill=TRIG_FILL)
                ovd.line([x1, 0, x1, H - 1], fill=TRIG_STROKE, width=2)
                if x2 > x1:
                    ovd.line([x2, 0, x2, H - 1], fill=TRIG_STROKE, width=2)

    img = Image.alpha_composite(img, ov)

    # ---- 8.  "DOES NOT ACTIVATE" overlay when no trigger regions ----
    if not trigger_ranges:
        dna_lbl = "DOES NOT ACTIVATE"
        font_dna = None
        for p in [
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]:
            try:
                font_dna = ImageFont.truetype(p, 58)
                break
            except Exception:
                pass
        if font_dna is None:
            font_dna = ImageFont.load_default()

        draw3 = ImageDraw.Draw(img)
        try:
            bb  = draw3.textbbox((0, 0), dna_lbl, font=font_dna)
            tw2 = bb[2] - bb[0]
            th2 = bb[3] - bb[1]
        except Exception:
            tw2, th2 = len(dna_lbl) * 22, 38

        cx2 = (W - tw2) // 2
        cy2 = (H - th2) // 2
        shadow = (0, 0, 0, 180)
        for dx, dy in ((-2, -2), (2, -2), (-2, 2), (2, 2), (0, -2), (0, 2), (-2, 0), (2, 0)):
            draw3.text((cx2 + dx, cy2 + dy), dna_lbl, fill=shadow, font=font_dna)
        draw3.text((cx2, cy2), dna_lbl, fill=(220, 40, 40, 255), font=font_dna)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
