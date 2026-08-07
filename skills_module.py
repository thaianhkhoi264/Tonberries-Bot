"""
skills_module.py

Discord skill lookup.  Combines:
  - uma-skill-tools data (skill_data.json / skillnames.json) for chart generation
  - GameTora-scraped skills.db for in-game descriptions and icon thumbnails
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os

import aiosqlite
import discord
from discord import app_commands

import cm_module
from global_config import (
    SKILL_DATA_JSON,
    SKILL_NAMES_JSON,
    SKILLS_DB,
)
from skill_chart import build_skill_chart, format_effects, _format_cond_block, evaluate_trigger, accel_verdict

logger = logging.getLogger("skills_module")

MAX_RESULTS_LIST = 8

# ---------------------------------------------------------------------------
# Application emoji map  (icon URL stem → full Discord emoji string)
# Populated by upload_skill_emojis.py; used in embeds that show skill icons.
# ---------------------------------------------------------------------------
_SKILL_ICON_EMOJI: dict[str, str] = {
    "10011":   "<:sk_10011:1535150295435448392>",
    "10012":   "<:sk_10012:1535150299772096532>",
    "10014":   "<:sk_10014:1535150304440614982>",
    "10016":   "<:sk_10016:1535150309389631528>",
    "10021":   "<:sk_10021:1535150313714229298>",
    "10022":   "<:sk_10022:1535150318352859217>",
    "10024":   "<:sk_10024:1535150322920595526>",
    "10026":   "<:sk_10026:1535150327454638083>",
    "10031":   "<:sk_10031:1535150331854585947>",
    "10032":   "<:sk_10032:1535150336388497488>",
    "10034":   "<:sk_10034:1535150341535043607>",
    "10041":   "<:sk_10041:1535150345892798464>",
    "10044":   "<:sk_10044:1535150350133362688>",
    "10051":   "<:sk_10051:1535150355518853262>",
    "10054":   "<:sk_10054:1535150360593694790>",
    "10061":   "<:sk_10061:1535150364851183716>",
    "10062":   "<:sk_10062:1535150369720508469>",
    "10066":   "<:sk_10066:1535150374460203129>",
    "1010011": "<:sk_1010011:1535150379149295727>",
    "1010021": "<:sk_1010021:1535150383809175602>",
    "1010031": "<:sk_1010031:1535150388477562930>",
    "1010041": "<:sk_1010041:1535150403325272124>",
    "1010051": "<:sk_1010051:1535150408060768426>",
    "20011":   "<:sk_20011:1535150412511060028>",
    "20012":   "<:sk_20012:1535150417024122931>",
    "20013":   "<:sk_20013:1535150422052966440>",
    "20014":   "<:sk_20014:1535150427316944906>",
    "20015":   "<:sk_20015:1535150432228220998>",
    "20016":   "<:sk_20016:1535150436665786480>",
    "20021":   "<:sk_20021:1535150441304690778>",
    "20022":   "<:sk_20022:1535150446027477094>",
    "20023":   "<:sk_20023:1535150450536489020>",
    "20024":   "<:sk_20024:1535150455611723787>",
    "20026":   "<:sk_20026:1535150460451688509>",
    "20041":   "<:sk_20041:1535150464914685962>",
    "20042":   "<:sk_20042:1535150469973020812>",
    "20043":   "<:sk_20043:1535150474997801010>",
    "20044":   "<:sk_20044:1535150480362180698>",
    "20046":   "<:sk_20046:1535150484942233680>",
    "20051":   "<:sk_20051:1535150488889331774>",
    "20052":   "<:sk_20052:1535150494127755295>",
    "20056":   "<:sk_20056:1535150499395932261>",
    "20061":   "<:sk_20061:1535150504198541382>",
    "20062":   "<:sk_20062:1535150509185573025>",
    "20064":   "<:sk_20064:1535150513555767296>",
    "20066":   "<:sk_20066:1535150518299656192>",
    "20091":   "<:sk_20091:1535150522972110900>",
    "20092":   "<:sk_20092:1535150527468281927>",
    "20096":   "<:sk_20096:1535150532438663178>",
    "2010010": "<:sk_2010010:1535150537459368066>",
    "2010016": "<:sk_2010016:1535150542265917450>",
    "20101":   "<:sk_20101:1535150547219513414>",
    "20102":   "<:sk_20102:1535150552365666385>",
    "20111":   "<:sk_20111:1535150557566738452>",
    "20112":   "<:sk_20112:1535150562402631761>",
    "20121":   "<:sk_20121:1535150567398047846>",
    "20122":   "<:sk_20122:1535150572490068019>",
    "20131":   "<:sk_20131:1535150577598726247>",
    "20132":   "<:sk_20132:1535150582522707968>",
    "20141":   "<:sk_20141:1535150587698479134>",
    "20142":   "<:sk_20142:1535150592660348999>",
    "20151":   "<:sk_20151:1535150597362421811>",
    "20152":   "<:sk_20152:1535150602970071050>",
    "20161":   "<:sk_20161:1535150608086999111>",
    "20162":   "<:sk_20162:1535150613237604382>",
    "20171":   "<:sk_20171:1535150618052661248>",
    "20181":   "<:sk_20181:1535150622721048727>",
    "20191":   "<:sk_20191:1535150627577929808>",
    "20192":   "<:sk_20192:1535150633131450428>",
    "20201":   "<:sk_20201:1535150637942312961>",
    "20202":   "<:sk_20202:1535150642853576716>",
    "30011":   "<:sk_30011:1535150648218361898>",
    "30012":   "<:sk_30012:1535150653523894282>",
    "30016":   "<:sk_30016:1535150659974996018>",
    "30021":   "<:sk_30021:1535150664567488635>",
    "30022":   "<:sk_30022:1535150669646929960>",
    "30026":   "<:sk_30026:1535150674277437470>",
    "30041":   "<:sk_30041:1535150678664679434>",
    "30051":   "<:sk_30051:1535150683005653032>",
    "30052":   "<:sk_30052:1535150687678242836>",
    "30056":   "<:sk_30056:1535150692543500348>",
    "30071":   "<:sk_30071:1535150696838467614>",
    "30072":   "<:sk_30072:1535150702794645515>",
    "30076":   "<:sk_30076:1535150707836059648>",
    "40012":   "<:sk_40012:1535150712865165332>",
}


def skill_icon_emoji(icon_url: str | None) -> str:
    """Return the Discord emoji string for a skill icon URL, or '' if not mapped."""
    if not icon_url:
        return ""
    stem = icon_url.rsplit("/", 1)[-1].replace(".png", "")
    return _SKILL_ICON_EMOJI.get(stem, "")


# Rarity → embed colour (matches uma-skill-tools rarity field 1-5)
_RARITY_COLOUR = {
    1: 0x99aab5,  # grey   — Normal
    2: 0x3498db,  # blue   — Rare
    3: 0x2ecc71,  # green  — SR
    4: 0xf1c40f,  # gold   — SSR
    5: 0xe91e63,  # pink   — Unique
}

# ---------------------------------------------------------------------------
# uma-skill-tools data (loaded once from disk)
# ---------------------------------------------------------------------------
_skill_data:  dict = {}   # skill_id_str → {alternatives, rarity, ...}
_skill_names: dict = {}   # skill_id_str → name (str or [str, ...])

_uma_loaded = False


def load_uma_data() -> None:
    """Load skill_data.json and skillnames.json into memory. No-op after first call."""
    global _uma_loaded, _skill_data, _skill_names
    if _uma_loaded:
        return
    if os.path.exists(SKILL_DATA_JSON):
        with open(SKILL_DATA_JSON, encoding="utf-8") as f:
            _skill_data = json.load(f)
    if os.path.exists(SKILL_NAMES_JSON):
        with open(SKILL_NAMES_JSON, encoding="utf-8") as f:
            _skill_names = json.load(f)
    _uma_loaded = True
    logger.info(f"[Skills] Loaded {len(_skill_data)} skills, {len(_skill_names)} names")


def _en_name(skill_id: str) -> str:
    raw = _skill_names.get(skill_id, "")
    return (raw[0] if isinstance(raw, list) else str(raw)) or f"Skill {skill_id}"


def _find_skill_by_name(name: str) -> tuple[str, dict] | None:
    """
    Search _skill_names for an exact case-insensitive match.
    Returns (skill_id_str, skill_data_entry) or None.
    """
    name_lower = name.lower()
    for sid, raw in _skill_names.items():
        en = (raw[0] if isinstance(raw, list) else str(raw))
        if en.lower() == name_lower:
            entry = _skill_data.get(sid)
            if entry is not None:
                return sid, entry
    return None


# ---------------------------------------------------------------------------
# GameTora skills.db helpers (descriptions + icons)
# ---------------------------------------------------------------------------

async def _search_skills(query: str, limit: int = MAX_RESULTS_LIST + 1) -> list[dict]:
    """Return skills from skills.db whose name contains *query*."""
    if not os.path.exists(SKILLS_DB):
        return []
    async with aiosqlite.connect(SKILLS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM skills WHERE name LIKE ? ORDER BY name LIMIT ?",
            (f"%{query}%", limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _get_gametora_by_id(skill_id: str) -> dict | None:
    """Look up skills.db by numeric skill_id (same ID system as uma-skill-tools)."""
    if not os.path.exists(SKILLS_DB):
        return None
    try:
        async with aiosqlite.connect(SKILLS_DB) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM skills WHERE skill_id = ?", (int(skill_id),)
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Formatting helpers (GameTora path, kept for DM-command fallback)
# ---------------------------------------------------------------------------

def _fmt_conditions(cond: str | None) -> str:
    if not cond:
        return "\u2014"
    parts = [l.lstrip("&").strip() for l in cond.splitlines() if l.strip()]
    return " \u00b7 ".join(parts)


def _rarity_stars(rarity: str | None) -> str:
    if not rarity:
        return ""
    if "(" in rarity:
        label, stars = rarity.split("(", 1)
        return f"{stars.rstrip(')')} {label.strip()}"
    return rarity


def _build_gametora_embed(skill: dict) -> discord.Embed:
    """Legacy embed built entirely from skills.db (GameTora) data."""
    name        = skill.get("name") or "Unknown"
    rarity      = _rarity_stars(skill.get("rarity"))
    description = skill.get("description") or ""
    description_detailed = skill.get("description_detailed") or ""
    character   = skill.get("character_name") or ""
    activation  = skill.get("activation") or ""
    base_duration = skill.get("base_duration") or ""
    base_cost   = skill.get("base_cost") or ""
    effect      = skill.get("effect") or ""
    conditions  = _fmt_conditions(skill.get("conditions"))
    icon_url    = skill.get("icon_url") or ""
    inh_activation = skill.get("inherited_activation") or ""
    inh_duration   = skill.get("inherited_duration") or ""
    inh_cost       = skill.get("inherited_base_cost") or ""
    inh_effect     = skill.get("inherited_effect") or ""
    inh_conditions = _fmt_conditions(skill.get("inherited_conditions"))

    embed = discord.Embed(
        title=name,
        description=description or None,
        colour=discord.Colour.gold() if "Unique" in (skill.get("rarity") or "")
               else discord.Colour.greyple(),
    )
    if icon_url:
        embed.set_thumbnail(url=icon_url)
    meta_parts = [rarity] if rarity else []
    if character:
        meta_parts.append(f"From: {character}")
    if meta_parts:
        embed.add_field(name="Info", value=" \u00b7 ".join(meta_parts), inline=False)
    if description_detailed and description_detailed.rstrip(".") != description.rstrip("."):
        embed.add_field(name="Detail", value=description_detailed, inline=False)
    effect_line = effect
    if base_duration:
        effect_line += f" \u00b7 {base_duration}"
    if activation:
        effect_line += f" \u00b7 {activation}"
    if base_cost:
        effect_line += f" \u00b7 cost {base_cost}"
    if effect_line:
        embed.add_field(name="Effect", value=effect_line, inline=False)
    embed.add_field(name="Conditions", value=f"`{conditions}`", inline=False)
    if inh_effect:
        inh_line = inh_effect
        if inh_duration:
            inh_line += f" \u00b7 {inh_duration}"
        if inh_activation:
            inh_line += f" \u00b7 {inh_activation}"
        if inh_cost:
            inh_line += f" \u00b7 cost {inh_cost}"
        embed.add_field(name="When inherited", value=inh_line, inline=False)
        if inh_conditions and inh_conditions != conditions:
            embed.add_field(name="Inherited conditions", value=f"`{inh_conditions}`", inline=False)
    skill_id = skill.get("skill_id")
    if skill_id:
        embed.set_footer(text=f"ID: {skill_id}")
    return embed


# ---------------------------------------------------------------------------
# Chart-based embed builder
# ---------------------------------------------------------------------------

def _build_chart_embed(
    skill_id: str,
    en_name: str,
    entry: dict,
    gt: dict | None,
    course_id: int | None,
    course_display: str,
    all_same_geo: bool = False,
    alt_index: int = 0,
    course_entry: dict | None = None,
    is_inherited: bool = False,
) -> discord.Embed:
    """Build a rich embed with condition blocks, effect summary, and optional chart."""
    alts   = entry.get("alternatives", [])
    alt    = alts[alt_index] if alts and alt_index < len(alts) else (alts[0] if alts else {})
    cond   = alt.get("condition", "")
    precond = alt.get("precondition", "")
    bdur   = int(alt.get("baseDuration", 0))
    rarity = entry.get("rarity", 0)
    efx    = format_effects(alt.get("effects", []))
    dur_s  = bdur / 10000.0

    cond_block   = _format_cond_block(cond)
    precond_block = _format_cond_block(precond) if precond else ""

    desc_parts: list[str] = []

    # In-game description from GameTora (if available)
    if gt and gt.get("description"):
        desc_parts.append(gt["description"])
    if gt and gt.get("description_detailed"):
        desc_parts.append(f"*{gt['description_detailed']}*")
    if desc_parts:
        desc_parts.append("")   # blank separator

    # Condition blocks
    if precond_block:
        desc_parts.append(f"**Pre-condition:**\n{precond_block}")
        desc_parts.append(f"**Activation:**\n{cond_block}")
    elif cond_block:
        desc_parts.append(f"**Condition:**\n{cond_block}")

    # Effect + duration
    if all_same_geo and len(alts) > 1:
        effect_lines = []
        for i, a in enumerate(alts, 1):
            e = format_effects(a.get("effects", []))
            d = int(a.get("baseDuration", 0)) / 10000.0
            effect_lines.append(f"Alt {i}: {e}  |  {d:.1f}s")
        desc_parts.append("\n**Effects:**\n" + "\n".join(effect_lines))
    else:
        desc_parts.append(f"\n**Effect:** {efx}  |  **Duration:** {dur_s:.1f}s")
        if len(alts) > 1:
            desc_parts.append(f"*({len(alts)} alternatives \u2014 showing #{alt_index + 1})*")

    # Verdict (acceleration skills only)
    verdict = accel_verdict(cond, precond, alt.get("effects", []), course_entry, is_inherited)
    if verdict:
        desc_parts.append(f"\n**Verdict:** {verdict}")

    # External links (only when a course is resolved so visualizer link is meaningful)
    if course_id:
        viz_url = (
            f"https://alpha123.github.io/uma-tools/skill-visualizer-global/"
            f"#cid={course_id},sid={skill_id}"
        )
        gt_url = f"https://gametora.com/umamusume/skill-condition-viewer?skill={skill_id}"
        desc_parts.append(f"\n[Skill visualizer]({viz_url})  \u00b7  [Condition viewer]({gt_url})")

    description = "\n".join(desc_parts)
    # Discord embed description cap is 4096 chars
    if len(description) > 4096:
        description = description[:4090] + "\u2026"

    embed = discord.Embed(
        title=en_name,
        description=description or None,
        colour=_RARITY_COLOUR.get(rarity, 0x99aab5),
    )
    if gt and gt.get("icon_url"):
        embed.set_thumbnail(url=gt["icon_url"])
    if course_id:
        embed.set_image(url="attachment://skill_chart.png")
    if course_display:
        embed.set_footer(text=course_display)

    return embed


# ---------------------------------------------------------------------------
# Public slash command handlers
# ---------------------------------------------------------------------------

async def autocomplete_skills(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Autocomplete for the `name` parameter of /skill."""
    load_uma_data()
    cur = current.lower()

    choices: list[app_commands.Choice[str]] = []

    if _skill_names and cur:
        for sid, raw in _skill_names.items():
            if sid not in _skill_data:   # skip phantom entries not in sim data
                continue
            en = (raw[0] if isinstance(raw, list) else str(raw))
            if cur in en.lower():
                display = f"{en} (Inherited)" if sid.startswith("9") else en
                choices.append(app_commands.Choice(name=display, value=sid))
                if len(choices) >= 25:
                    break

    # Fallback to skills.db when uma data not yet synced or no matches
    if not choices and os.path.exists(SKILLS_DB):
        rows = await _search_skills(current, limit=25)
        choices = [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

    return choices[:25]


async def handle_skill_interaction(
    interaction: discord.Interaction,
    name: str,
    course: str | None = None,
    length: str | None = None,
) -> None:
    """Slash command entry point (/skill). Responds publicly with a skill chart."""
    await interaction.response.defer()
    load_uma_data()
    cm_module.load_local_data_if_needed()

    name = name.strip()
    if not name:
        await interaction.followup.send("Please provide a skill name.")
        return

    # --- Find skill in uma-skill-tools data ---
    # Autocomplete delivers skill_id as value; manual typing delivers a name.
    result = None
    if name.isdigit():
        entry = _skill_data.get(name)
        if entry:
            result = (name, entry)
    if result is None:
        result = _find_skill_by_name(name)

    if result is None:
        # Fall back: uma data missing or skill not found — try skills.db only
        if not os.path.exists(SKILLS_DB):
            await interaction.followup.send(
                f"No skill found for **{name}**.\n"
                "Skills database not synced yet \u2014 try `skill sync` first."
            )
            return
        rows = await _search_skills(name, limit=MAX_RESULTS_LIST + 1)
        if not rows:
            await interaction.followup.send(f"No skill found matching **{name}**.")
            return
        if len(rows) > MAX_RESULTS_LIST:
            await interaction.followup.send(
                f"Found more than {MAX_RESULTS_LIST} skills matching **{name}** \u2014 "
                "please be more specific."
            )
            return
        if len(rows) > 1:
            lines = [f"{i+1}. {r['name']}" for i, r in enumerate(rows)]
            await interaction.followup.send(
                f"Multiple skills match **{name}**:\n" + "\n".join(lines)
                + "\n\nUse the full name to get details."
            )
            return
        embed = _build_gametora_embed(rows[0])
        await interaction.followup.send(embed=embed)
        return

    skill_id, entry = result
    is_inherited = skill_id.startswith("9")
    en_name = _en_name(skill_id)
    if is_inherited:
        en_name = f"{en_name} (Inherited)"

    # --- Look up description / icon from GameTora DB (optional enrichment) ---
    gt = await _get_gametora_by_id(skill_id)
    # Inherited skills (9XXXXX) don't have their own GT row; fall back to the
    # corresponding unique version (1XXXXX) just to get the icon.
    if gt is None and is_inherited:
        gt = await _get_gametora_by_id("1" + skill_id[1:])

    # --- Resolve course ---
    # Pre-fetch CM events so resolve_course can work synchronously
    await cm_module.fetch_cm_events()

    # Default: use active CM, then next CM, if user didn't specify
    if not course:
        cm = cm_module.get_active_cm() or cm_module.get_next_cm()
        if cm:
            course = f"cm:{cm['number']}"

    course_id, course_display = cm_module.resolve_course(course or "", length)

    # --- Generate chart (CPU-bound; run in thread pool) ---
    chart_bytes: bytes | None = None
    course_entry_for_embed: dict | None = None
    if course_id is not None:
        course_entry = cm_module.get_course_entry(course_id)
        if course_entry:
            course_entry_for_embed = course_entry
            alts = entry.get("alternatives", [])
            alt_index = 0
            for i, a in enumerate(alts):
                if evaluate_trigger(a.get("condition", ""), a.get("precondition", ""), course_entry):
                    alt_index = i
                    break
            alt     = alts[alt_index] if alts else {}
            cond    = alt.get("condition", "")
            precond = alt.get("precondition", "")
            bdur    = int(alt.get("baseDuration", 0))
            loop = asyncio.get_event_loop()
            try:
                chart_bytes = await loop.run_in_executor(
                    None,
                    build_skill_chart,
                    cond, precond, bdur, course_entry, course_display,
                )
            except Exception as exc:
                logger.error(f"[Skills] Chart generation failed for {skill_id}: {exc}")

            # Show all alternative effects when they share the same geo trigger window
            ranges0 = evaluate_trigger(cond, precond, course_entry)
            all_same_geo = len(alts) > 1 and all(
                evaluate_trigger(a.get("condition", ""), a.get("precondition", ""), course_entry) == ranges0
                for a in alts if a is not alt
            )
        else:
            alt_index = 0
            all_same_geo = False
    else:
        alt_index = 0
        all_same_geo = False

    # --- Build and send embed ---
    embed = _build_chart_embed(
        skill_id, en_name, entry, gt,
        course_id, course_display,
        all_same_geo, alt_index,
        course_entry=course_entry_for_embed,
        is_inherited=is_inherited,
    )

    if chart_bytes:
        file = discord.File(io.BytesIO(chart_bytes), filename="skill_chart.png")
        await interaction.followup.send(embed=embed, file=file)
    else:
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# DM command handler (text-based; kept for owner DM use)
# ---------------------------------------------------------------------------

async def handle_skill_lookup(message: discord.Message, query: str) -> None:
    """Entry point called from main.py for DM `skill <name>` command."""
    query = query.strip()
    if not query:
        await message.channel.send("Usage: `skill <name>` \u2014 e.g. `skill Red Shift`")
        return

    if not os.path.exists(SKILLS_DB):
        await message.channel.send("Skills database not found. Run `skill refresh` first.")
        return

    results = await _search_skills(query)
    if not results:
        await message.channel.send(f"No skill found matching **{query}**.")
        return
    if len(results) > MAX_RESULTS_LIST:
        await message.channel.send(
            f"Found more than {MAX_RESULTS_LIST} skills matching **{query}** \u2014 "
            "please be more specific."
        )
        return
    if len(results) > 1:
        lines = [f"{i+1}. {r['name']}" for i, r in enumerate(results)]
        await message.channel.send(
            f"Multiple skills match **{query}**:\n" + "\n".join(lines)
            + "\n\nType the full name to get details."
        )
        return

    embed = _build_gametora_embed(results[0])
    await message.channel.send(embed=embed)
