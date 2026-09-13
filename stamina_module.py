"""
stamina_module.py

/stamina slash command — recommends the minimum Stamina stat needed to run a
full last spurt (no HP-conservation slowdown), per running style, for a given
Champions Meeting or free course selection. Also shows how much that changes
under a stacked debuff/recovery skill loadout.

Public API:
    load_stamina_data()                        → call once from on_ready
    handle_stamina_interaction(interaction, course, distance)

Methodology, in short (full derivation and validation notes live in
tests/test_stamina_calc.py, kept as the historical record of how this was
built and checked):
  - Formulas are from "Uma Musume Race Mechanics" (KuromiAK), except the
    last-spurt speed formula and the "no safety buffer" convention, which are
    calibrated to match github.com/TheCing/uma-tools's HP calculator exactly
    (verified: reproducing its formula chain in isolation gave its own
    published "Min Stamina for Full Spurt" figure to the exact stamina point
    for a known Speed/Guts/course/style combination).
  - Downhill accel mode is modeled as an expected-value adjustment (Markov
    steady-state), matching that same reference tool's own "w/ DH" figure.
  - Uphill Slope Modifier and the 3-segment (early/mid/late) phase split are
    kept from our own approach — more precise than the reference tool's
    shortcuts, and cheap to keep now that they're implemented.
  - Baseline Speed/Power/Guts/Wit are ASSUMED per distance category (not a
    specific character's real stats) — see DISTANCE_STATS below.
  - Ground condition is pulled from the CM's actual live timeline data when
    available (falls back to Firm/Good, modifier 1.0, otherwise).
  - Not modeled: skills' geometric/positional effects, PositionKeepCoef,
    ForceInModifier, MoveLaneModifier, Randomness Per Section, Compete Before
    Spurt, Secure Lead, Stamina Keep's own activation-chance formula — see
    tests/test_stamina_calc.py's docstring for why each of these is excluded.
"""
from __future__ import annotations

import json
import logging
import math
import os

import discord

import cm_module
from global_config import SKILL_DATA_JSON, SKILL_NAMES_JSON

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISPLAY_STYLES = ("front", "pace", "late", "end")

STYLE_NAME = {
    "front": "Front Runner",
    "pace":  "Pace Chaser",
    "late":  "Late Surger",
    "end":   "End Closer",
}
STYLE_COLOR = {
    "front": 0x3498DB,
    "pace":  0x2ECC71,
    "late":  0xF1C40F,
    "end":   0xE74C3C,
}

# Baseline stat assumptions, keyed by course_data.json's distanceType
# (1=Short/Sprint, 2=Mile, 3=Middle, 4=Long) — Sprint and Mile share a baseline.
DISTANCE_STATS = {
    1: {"speed": 1600, "power": 1201, "guts": 600, "wit": 1200},  # Sprint
    2: {"speed": 1600, "power": 1201, "guts": 600, "wit": 1200},  # Mile
    3: {"speed": 1500, "power": 1100, "guts": 600, "wit": 1100},  # Medium
    4: {"speed": 1400, "power": 1000, "guts": 600, "wit": 1000},  # Long
}
DISTANCE_PROFICIENCY = 1.0    # Grade A, assumed
STATUS_MODIFIER      = 1.0    # no rushed / pace-down state

# Ground Modifier ("Uma Musume Race Mechanics" p12): tiers 1-2 (Firm/Good) are
# always 1.0; tiers 3-4 differ by surface. Reuses cm_module._GROUND_COND_MAP
# (Firm/Good/Slightly Soft/Soft/Heavy -> 1-4), the same mapping already used
# elsewhere in this codebase for skill-condition ground checks.
_GROUND_MODIFIER_BY_TIER = {
    1: {1: 1.00, 2: 1.00},  # surface 1=turf, 2=dirt
    2: {1: 1.00, 2: 1.00},
    3: {1: 1.02, 2: 1.01},
    4: {1: 1.02, 2: 1.02},
}

# Strategy Phase Coefficient (Base Target Speed section)
STRATEGY_PHASE_COEF = {
    "front": {"early": 1.0,   "mid": 0.98,  "late": 0.962},
    "pace":  {"early": 0.978, "mid": 0.991, "late": 0.975},
    "late":  {"early": 0.938, "mid": 0.998, "late": 0.994},
    "end":   {"early": 0.931, "mid": 1.0,   "late": 1.0},
}

# Strategy Coefficient for MaxHP
STRATEGY_COEF_HP = {"front": 0.95, "pace": 0.89, "late": 1.0, "end": 0.995}

_EMBED_FOOTER = "Assumes Grade A distance aptitude; skills' own geometric effects not modeled"

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_skill_data:      dict = {}
_skill_names:     dict = {}
_skill_averages:  dict = {}
_data_loaded:     bool = False

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_stamina_data() -> None:
    """Load skill data and precompute recovery/debuff averages. Call once from on_ready."""
    global _skill_data, _skill_names, _skill_averages, _data_loaded
    if _data_loaded:
        return

    for path, label in [(SKILL_DATA_JSON, "skill_data.json"), (SKILL_NAMES_JSON, "skillnames.json")]:
        if not os.path.exists(path):
            logger.warning(f"[Stamina] {label} not found at {path} — /stamina will be unavailable")
            return
    with open(SKILL_DATA_JSON, encoding="utf-8") as f:
        _skill_data = json.load(f)
    with open(SKILL_NAMES_JSON, encoding="utf-8") as f:
        _skill_names = json.load(f)

    _skill_averages = _scan_recovery_debuff_averages(_skill_data, _skill_names)
    logger.info(f"[Stamina] Loaded skill data; recovery/debuff averages: {_skill_averages}")
    _data_loaded = True

# ---------------------------------------------------------------------------
# Formulas — "Uma Musume Race Mechanics" (KuromiAK), calibrated per the
# module docstring above
# ---------------------------------------------------------------------------

def _base_speed(course_distance: float) -> float:
    return 20.0 - (course_distance - 2000.0) / 1000.0


def _base_target_speed_late(bs: float, late_coef: float, speed_stat: float, dist_prof: float) -> float:
    return bs * late_coef + math.sqrt(500.0 * speed_stat) * dist_prof * 0.002


def _last_spurt_speed_max(base_target_speed_phase2: float, bs: float, speed_stat: float, dist_prof: float) -> float:
    """No Guts term — matches the validated reference's estimateSpurtSpeed(), not the raw PDF formula."""
    return ((base_target_speed_phase2 + 0.01 * bs) * 1.05
            + math.sqrt(500.0 * speed_stat) * dist_prof * 0.002)


def _hp_consumption_per_second(current_speed: float, bs: float,
                                status_modifier: float = 1.0, ground_modifier: float = 1.0) -> float:
    return 20.0 * (current_speed - bs + 12.0) ** 2 / 144.0 * status_modifier * ground_modifier


def _guts_modifier(guts_stat: float) -> float:
    return 1.0 + 200.0 / math.sqrt(600.0 * guts_stat)


def _slope_uphill_loss(raw_slope: float, power_stat: float) -> float:
    """SlopePer*200/PowerStat; SlopePer = raw_slope/10000 (course_data.json's per-mille scaling)."""
    slope_pct = raw_slope / 10000.0
    return slope_pct * 200.0 / power_stat


def _downhill_mode_rate(wit_stat: float) -> float:
    """Steady-state P(downhill accel mode) — Markov chain, entry WizStat*0.04%/s, exit 20%/s."""
    p_activate = wit_stat * 0.0004
    p_deactivate = 0.2
    return p_activate / (p_activate + p_deactivate)


def _ground_modifier_for(ground_str: str | None, surface: int) -> float:
    tier = cm_module._GROUND_COND_MAP.get(ground_str, 1) if ground_str else 1
    return _GROUND_MODIFIER_BY_TIER.get(tier, _GROUND_MODIFIER_BY_TIER[1]).get(surface, 1.00)


def _build_segments(d: float, p1: float, p2: float, slopes: list) -> list[tuple[float, float, str, float]]:
    """
    Partition [0, d] into (start, end, phase, raw_slope) pieces, splitting at
    the early/mid/late boundaries AND at every slope segment's edges.
    raw_slope > 0 = uphill (deterministic loss applied in-line), raw_slope < 0
    = downhill (no speed change — its HP is tallied separately for the
    Markov-chain downhill-savings adjustment), 0 = flat.
    """
    sloped = [(float(s["start"]), float(s["start"] + s["length"]), s["slope"])
              for s in slopes if s["slope"] != 0]
    bounds = {0.0, p1, p2, float(d)}
    for us, ue, _ in sloped:
        bounds.add(max(0.0, min(us, d)))
        bounds.add(max(0.0, min(ue, d)))
    ordered = sorted(b for b in bounds if 0.0 <= b <= d)

    segments = []
    for s0, s1 in zip(ordered, ordered[1:]):
        if s1 - s0 < 1e-9:
            continue
        mid = (s0 + s1) / 2.0
        phase = "early" if mid < p1 else ("mid" if mid < p2 else "late")
        raw_slope = 0.0
        for us, ue, sp in sloped:
            if s0 >= us - 1e-6 and s1 <= ue + 1e-6:
                raw_slope = sp
                break
        segments.append((s0, s1, phase, raw_slope))
    return segments

# ---------------------------------------------------------------------------
# Recovery/debuff skill averages
# ---------------------------------------------------------------------------

def _scan_recovery_debuff_averages(skill_data: dict, skill_names: dict) -> dict:
    """
    Scan the regular (non-inherited) skill pool for type-9 (HP) effects and
    average the % magnitude within 4 categories: gold/white recovery (target=1,
    positive — restores your own HP) and gold/white debuff (negative, target
    NOT IN (1,2) — an opponent's skill draining HP off of us).

    target=1 with a NEGATIVE modifier is a different mechanic — a skill
    costing its OWN caster HP as a downside (e.g. "A Lifelong Dream, A
    Moment's Flight") — excluded, not a debuff. Only the first alternative
    per skill is checked; a skill with both a recovery AND a debuff effect on
    the same alt (e.g. Stamina Siphon/Stamina Eater) counts toward both.
    """
    def name_of(sid):
        n = skill_names.get(sid, "")
        return n[0] if isinstance(n, list) else str(n)

    cats = {"gold_recovery": [], "white_recovery": [], "gold_debuff": [], "white_debuff": []}
    for sid, entry in skill_data.items():
        if sid.startswith("9"):
            continue
        rarity = entry.get("rarity")
        if rarity not in (1, 2):
            continue
        alts = entry.get("alternatives", [])
        if not alts or not name_of(sid):
            continue
        for e in alts[0].get("effects", []):
            if e.get("type") != 9:
                continue
            target, mod = e.get("target"), e.get("modifier", 0)
            if target == 1 and mod > 0:
                cats["gold_recovery" if rarity == 2 else "white_recovery"].append(mod)
            elif target not in (1, 2) and mod < 0:
                cats["gold_debuff" if rarity == 2 else "white_debuff"].append(mod)

    return {
        cat: (sum(mods) / len(mods) / 10000.0 if mods else 0.0)
        for cat, mods in cats.items()
    }


def _effective_stamina_delta(pct: float, stamina_ref: float, course_distance: float, strategy_coef_hp: float) -> float:
    """How many Stamina points a pct-of-MaxHP swing (+recovery/-debuff) is worth, at a reference Stamina."""
    return pct * stamina_ref + pct * course_distance / (0.8 * strategy_coef_hp)

# ---------------------------------------------------------------------------
# Main calculation
# ---------------------------------------------------------------------------

def compute_stamina(style: str, course_entry: dict, skill_averages: dict, ground_modifier: float = 1.0) -> dict:
    d = float(course_entry["distance"])
    distance_type = course_entry.get("distanceType", 3)
    stats = DISTANCE_STATS.get(distance_type, DISTANCE_STATS[3])
    speed_stat, power_stat, guts_stat, wit_stat = stats["speed"], stats["power"], stats["guts"], stats["wit"]

    bs = _base_speed(d)
    coef = STRATEGY_PHASE_COEF[style]

    p1 = d / 6.0
    p2 = d * 2.0 / 3.0

    early_speed = bs * coef["early"]
    mid_speed = bs * coef["mid"]
    late_base_target = _base_target_speed_late(bs, coef["late"], speed_stat, DISTANCE_PROFICIENCY)
    lsv = _last_spurt_speed_max(late_base_target, bs, speed_stat, DISTANCE_PROFICIENCY)
    phase_speed = {"early": early_speed, "mid": mid_speed, "late": lsv}

    segments = _build_segments(d, p1, p2, course_entry.get("slopes", []))

    hp_used = 0.0
    downhill_normal_hp = 0.0
    for s0, s1, phase, raw_slope in segments:
        speed = phase_speed[phase]
        if raw_slope > 0:
            speed = max(0.1, speed - _slope_uphill_loss(raw_slope, power_stat))
        seg_dist = s1 - s0
        t = seg_dist / speed
        rate = _hp_consumption_per_second(speed, bs, STATUS_MODIFIER, ground_modifier)
        if phase == "late":
            rate *= _guts_modifier(guts_stat)
        seg_hp = rate * t
        hp_used += seg_hp
        if raw_slope < 0:
            downhill_normal_hp += seg_hp

    mode_rate = _downhill_mode_rate(wit_stat)
    downhill_savings = downhill_normal_hp * mode_rate * 0.6
    adjusted_hp_used = hp_used - downhill_savings

    strategy_coef_hp = STRATEGY_COEF_HP[style]
    # "Min Stamina for Full Spurt" — exact breakeven, no safety buffer.
    min_stamina = (hp_used - d) / (0.8 * strategy_coef_hp)
    min_stamina_dh = (adjusted_hp_used - d) / (0.8 * strategy_coef_hp)

    skill_effects = {
        cat: _effective_stamina_delta(pct, min_stamina_dh, d, strategy_coef_hp)
        for cat, pct in skill_averages.items()
    }
    gold_debuff_unit = abs(skill_effects["gold_debuff"])
    white_debuff_unit = abs(skill_effects["white_debuff"])
    gold_recovery_unit = skill_effects["gold_recovery"]

    def _scenario(n_gold_debuff: int, n_white_debuff: int, n_gold_recovery: int) -> float:
        return (min_stamina_dh
                + n_gold_debuff * gold_debuff_unit
                + n_white_debuff * white_debuff_unit
                - n_gold_recovery * gold_recovery_unit)

    scenarios = {
        "1g1w": {"base": _scenario(1, 1, 0), "r1": _scenario(1, 1, 1), "r2": _scenario(1, 1, 2)},
        "2g2w": {"base": _scenario(2, 2, 0), "r1": _scenario(2, 2, 1), "r2": _scenario(2, 2, 2)},
    }

    return {
        "style": style,
        "min_stamina": min_stamina,
        "min_stamina_dh": min_stamina_dh,
        "downhill_savings": downhill_savings,
        "downhill_mode_rate": mode_rate,
        "hp_used": hp_used,
        "stats_used": stats,
        "scenarios": scenarios,
    }

# ---------------------------------------------------------------------------
# Embed building
# ---------------------------------------------------------------------------

def _build_style_embed(style: str, result: dict) -> discord.Embed:
    stats = result["stats_used"]
    sc = result["scenarios"]
    desc = (
        f"**Min Stamina for Full Spurt: {result['min_stamina']:.0f}**\n"
        f"With Downhill: **{result['min_stamina_dh']:.0f}** "
        f"(DH rate {result['downhill_mode_rate']*100:.1f}%, saves {result['downhill_savings']:.0f} HP)\n\n"
        f"HP used: {result['hp_used']:.0f}  |  "
        f"Stats: Spd {stats['speed']}, Pwr {stats['power']}, Gts {stats['guts']}, Wit {stats['wit']}\n\n"
        f"*Debuff scenarios (stam w/ Downhill; gold+white debuffs applied at once):*\n"
        f"1 Gold + 1 White debuff: **{sc['1g1w']['base']:.0f}** "
        f"→ +1 Gold Rec: **{sc['1g1w']['r1']:.0f}** → +2 Gold Rec: **{sc['1g1w']['r2']:.0f}**\n"
        f"2 Gold + 2 White debuff: **{sc['2g2w']['base']:.0f}** "
        f"→ +1 Gold Rec: **{sc['2g2w']['r1']:.0f}** → +2 Gold Rec: **{sc['2g2w']['r2']:.0f}**"
    )
    embed = discord.Embed(title=STYLE_NAME[style], description=desc, colour=STYLE_COLOR[style])
    return embed

# ---------------------------------------------------------------------------
# Interaction handler
# ---------------------------------------------------------------------------

async def handle_stamina_interaction(
    interaction: discord.Interaction,
    course: str | None,
    distance: str | None,
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

    # Default: use active CM, then next CM, if user didn't specify
    if not course:
        cm = cm_module.get_active_cm() or cm_module.get_next_cm()
        if cm:
            course = f"cm:{cm['number']}"

    course_id, course_display = cm_module.resolve_course(course or "", distance)
    if not course_id:
        await interaction.followup.send(
            "Could not resolve a course for that selection. The CM may not be in the timeline yet.",
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

    # Pull the CM's actual ground condition when we have live timeline data
    # for it; fall back to the default (Firm/Good, modifier 1.0) for past CMs
    # that have dropped out of the timeline, or free course selections with
    # no CM tied to them at all.
    cm_data: dict | None = None
    if course and course.startswith("cm:"):
        try:
            num = int(course[3:])
            cm_data = next((c for c in cm_module._cm_events if c["number"] == num), None)
        except ValueError:
            pass

    surface = course_entry.get("surface", 1)
    ground_str = cm_data.get("ground") if cm_data else None
    ground_mod = _ground_modifier_for(ground_str, surface)

    embeds = [
        _build_style_embed(style, compute_stamina(style, course_entry, _skill_averages, ground_mod))
        for style in DISPLAY_STYLES
    ]

    header = (
        f"## {course_display}\n"
        f"-# *Min Stamina for Full Spurt by running style — {_EMBED_FOOTER}*"
    )
    await interaction.followup.send(content=header, embeds=embeds)
