"""
skills_module.py

Discord skill lookup from the locally cached skills.db (populated by skills_scraper.py).
"""

import logging
import os

import aiosqlite
import discord
from discord import app_commands

from global_config import SKILLS_DB

logger = logging.getLogger("skills_module")

# Maximum number of search results to list before asking for a narrower query
MAX_RESULTS_LIST = 8


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def _search_skills(query: str, limit: int = MAX_RESULTS_LIST + 1) -> list[dict]:
    """Return skills whose name contains *query* (case-insensitive)."""
    if not os.path.exists(SKILLS_DB):
        return []
    rows = []
    async with aiosqlite.connect(SKILLS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM skills WHERE name LIKE ? ORDER BY name LIMIT ?",
            (f"%{query}%", limit),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return rows


async def _get_skill_by_id(skill_id: int) -> dict | None:
    if not os.path.exists(SKILLS_DB):
        return None
    async with aiosqlite.connect(SKILLS_DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM skills WHERE skill_id = ?", (skill_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_conditions(cond: str | None) -> str:
    """Join multiline conditions into a compact inline string."""
    if not cond:
        return "—"
    # Each line may start with '&' (continuation marker) – strip it for readability
    parts = [l.lstrip("&").strip() for l in cond.splitlines() if l.strip()]
    return " · ".join(parts)


def _rarity_stars(rarity: str | None) -> str:
    """Map 'Unique (⭐⭐⭐)' → '⭐⭐⭐ Unique', 'Normal' → 'Normal', etc."""
    if not rarity:
        return ""
    # Rarity strings from GameTora look like "Unique (⭐⭐⭐)" or just "Normal"
    if "(" in rarity:
        label, stars = rarity.split("(", 1)
        return f"{stars.rstrip(')')} {label.strip()}"
    return rarity


def _build_skill_embed(skill: dict) -> discord.Embed:
    """Build a Discord embed for a single skill."""
    name = skill.get("name") or "Unknown"
    rarity = _rarity_stars(skill.get("rarity"))
    description = skill.get("description") or ""
    description_detailed = skill.get("description_detailed") or ""
    character = skill.get("character_name") or ""
    activation = skill.get("activation") or ""
    base_duration = skill.get("base_duration") or ""
    base_cost = skill.get("base_cost") or ""
    effect = skill.get("effect") or ""
    conditions = _fmt_conditions(skill.get("conditions"))
    icon_url = skill.get("icon_url") or ""

    # Inherited fields
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

    # Rarity / source character
    meta_parts = [rarity] if rarity else []
    if character:
        meta_parts.append(f"From: {character}")
    if meta_parts:
        embed.add_field(name="Info", value=" · ".join(meta_parts), inline=False)

    # Detailed description (if different from short one)
    if description_detailed and description_detailed.rstrip(".") != description.rstrip("."):
        embed.add_field(name="Detail", value=description_detailed, inline=False)

    # Main version stats
    effect_line = effect
    if base_duration:
        effect_line += f" · {base_duration}"
    if activation:
        effect_line += f" · {activation}"
    if base_cost:
        effect_line += f" · cost {base_cost}"
    if effect_line:
        embed.add_field(name="Effect", value=effect_line, inline=False)

    embed.add_field(name="Conditions", value=f"`{conditions}`", inline=False)

    # Inherited version
    if inh_effect:
        inh_line = inh_effect
        if inh_duration:
            inh_line += f" · {inh_duration}"
        if inh_activation:
            inh_line += f" · {inh_activation}"
        if inh_cost:
            inh_line += f" · cost {inh_cost}"
        embed.add_field(name="When inherited", value=inh_line, inline=False)
        if inh_conditions and inh_conditions != conditions:
            embed.add_field(
                name="Inherited conditions", value=f"`{inh_conditions}`", inline=False
            )

    skill_id = skill.get("skill_id")
    if skill_id:
        embed.set_footer(text=f"ID: {skill_id}")

    return embed


# ---------------------------------------------------------------------------
# Public command handler
# ---------------------------------------------------------------------------

async def handle_skill_lookup(message: discord.Message, query: str) -> None:
    """
    Entry point called from main.py.
    Searches for *query* in skills.db and responds with an embed (or error).
    """
    query = query.strip()
    if not query:
        await message.channel.send("Usage: `skill <name>` — e.g. `skill Red Shift`")
        return

    if not os.path.exists(SKILLS_DB):
        await message.channel.send(
            "Skills database not found. Run `skill refresh` first."
        )
        return

    results = await _search_skills(query)

    if not results:
        await message.channel.send(f"No skill found matching **{query}**.")
        return

    if len(results) > MAX_RESULTS_LIST:
        await message.channel.send(
            f"Found more than {MAX_RESULTS_LIST} skills matching **{query}** — "
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

    skill = results[0]
    embed = _build_skill_embed(skill)
    await message.channel.send(embed=embed)


# ---------------------------------------------------------------------------
# Slash command handlers (interaction-based)
# ---------------------------------------------------------------------------

async def autocomplete_skills(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Autocomplete handler for the /skill slash command."""
    if not current or not os.path.exists(SKILLS_DB):
        return []
    results = await _search_skills(current, limit=25)
    return [
        app_commands.Choice(name=r["name"], value=r["name"])
        for r in results
    ]


async def handle_skill_interaction(
    interaction: discord.Interaction, query: str
) -> None:
    """
    Slash command entry point — responds ephemerally so only the invoking
    user sees the result.
    """
    await interaction.response.defer(ephemeral=True)

    query = query.strip()
    if not query:
        await interaction.followup.send(
            "Please provide a skill name.", ephemeral=True
        )
        return

    if not os.path.exists(SKILLS_DB):
        await interaction.followup.send(
            "Skills database not found. Ask an admin to run `skill refresh`.",
            ephemeral=True,
        )
        return

    results = await _search_skills(query)

    if not results:
        await interaction.followup.send(
            f"No skill found matching **{query}**.", ephemeral=True
        )
        return

    if len(results) > MAX_RESULTS_LIST:
        await interaction.followup.send(
            f"Found more than {MAX_RESULTS_LIST} skills matching **{query}** — "
            "please be more specific.",
            ephemeral=True,
        )
        return

    if len(results) > 1:
        lines = [f"{i+1}. {r['name']}" for i, r in enumerate(results)]
        await interaction.followup.send(
            f"Multiple skills match **{query}**:\n" + "\n".join(lines)
            + "\n\nUse the full name to get details.",
            ephemeral=True,
        )
        return

    embed = _build_skill_embed(results[0])
    await interaction.followup.send(embed=embed, ephemeral=True)
