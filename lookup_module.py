"""
lookup_module.py

Implements the /whenis slash command — looks up when an Uma Musume support card or
character will next appear on a banner, using:
  • SHARED_GAMETORA_DB  (Gacha-Timer-Bot's uma_jp_data.db) for card name → ID
  • uma.moe banner timeline API for pickup_card_ids + banner dates
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import time
from datetime import datetime, timezone

import aiohttp
import aiosqlite
import discord
from discord import app_commands

from global_config import SHARED_GAMETORA_DB, UMA_MOE_API_KEY

logger = logging.getLogger("lookup_module")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UMA_TIMELINE_URL  = "https://uma.moe/resources/current/banner_timeline.json.gz"
SUPPORT_IMG_URL   = "https://media.gametora.com/umamusume/supports/full/small/{}.png"
CHARACTER_IMG_URL = "https://media.gametora.com/umamusume/characters/full/small/{}.png"
GAMETORA_BASE     = "https://gametora.com"

CACHE_TTL    = 3600   # seconds
PENDING_TTL  = 60     # seconds before disambiguation times out

DIGIT_EMOJIS = ["1️⃣", "2️⃣", "3️⃣"]

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_timeline: list[dict]        = []
_banner_card_ids: set[int]   = set()   # union of all pickup_card_ids across all events
_cache_ts: float             = 0.0

# {message_id: {user_id, cards, banners, channel, expires}}
_pending: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Timeline cache
# ---------------------------------------------------------------------------

async def _get_timeline() -> list[dict]:
    global _timeline, _banner_card_ids, _cache_ts
    if time.time() - _cache_ts < CACHE_TTL and _timeline:
        return _timeline

    if not UMA_MOE_API_KEY:
        logger.warning("[lookup] UMA_MOE_API_KEY not set — cannot fetch timeline")
        return []

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                UMA_TIMELINE_URL,
                headers={"X-API-Key": UMA_MOE_API_KEY},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                raw = await resp.read()

        try:
            raw = gzip.decompress(raw)
        except (gzip.BadGzipFile, OSError):
            pass  # already plain JSON

        data = __import__("json").loads(raw)
        events = data.get("events", []) if isinstance(data, dict) else data
        _timeline = events
        _banner_card_ids = {
            int(cid)
            for ev in events
            for cid in ev.get("pickup_card_ids", [])
            if str(cid).lstrip("-").isdigit()
        }
        _cache_ts = time.time()
        logger.info(f"[lookup] Cached {len(events)} events, {len(_banner_card_ids)} unique banner card IDs")
        return _timeline
    except Exception as exc:
        logger.error(f"[lookup] Failed to fetch timeline: {exc}")
        return _timeline  # return stale data if available


# ---------------------------------------------------------------------------
# Card search
# ---------------------------------------------------------------------------

async def _search_cards(query: str, limit: int = 25) -> list[dict]:
    """
    Search support_cards and characters in SHARED_GAMETORA_DB by name LIKE %query%.
    Returns list of dicts: {card_id, name, link, kind}.
    """
    if not os.path.exists(SHARED_GAMETORA_DB):
        logger.warning(f"[lookup] SHARED_GAMETORA_DB not found: {SHARED_GAMETORA_DB}")
        return []

    results: list[dict] = []
    pattern = f"%{query}%"

    async with aiosqlite.connect(SHARED_GAMETORA_DB) as conn:
        conn.row_factory = aiosqlite.Row
        # Support cards
        async with conn.execute(
            "SELECT card_id, name, link FROM support_cards WHERE name LIKE ? ORDER BY name LIMIT ?",
            (pattern, limit),
        ) as cur:
            for row in await cur.fetchall():
                results.append({
                    "card_id": str(row["card_id"]),
                    "name":    row["name"],
                    "link":    row["link"],
                    "kind":    "support",
                })

        # Characters (fill remaining quota)
        remaining = limit - len(results)
        if remaining > 0:
            async with conn.execute(
                "SELECT character_id, name, link FROM characters WHERE name LIKE ? ORDER BY name LIMIT ?",
                (pattern, remaining),
            ) as cur:
                for row in await cur.fetchall():
                    results.append({
                        "card_id": str(row["character_id"]),
                        "name":    row["name"],
                        "link":    row["link"],
                        "kind":    "character",
                    })

    return results


# ---------------------------------------------------------------------------
# Banner lookup
# ---------------------------------------------------------------------------

def _parse_date(date_str: str | None) -> float | None:
    """Parse an ISO date string to a UTC Unix timestamp.
    Handles: YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS, YYYY-MM-DDTHH:MM:SSZ, +00:00 suffix.
    """
    if not date_str:
        return None
    try:
        # Normalise: replace trailing Z with +00:00 so fromisoformat handles it
        normalised = date_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    return None


def _find_banners(card_id_int: int, timeline: list[dict]) -> list[dict]:
    """
    Find all timeline events where card_id_int appears in pickup_card_ids.
    Returns list sorted upcoming-first, then past (most-recent) as fallback.
    Each entry: {start_ts, end_ts, gametora_url}.
    """
    now = time.time()
    upcoming: list[dict] = []
    past: list[dict] = []

    for ev in timeline:
        ids = [int(x) for x in ev.get("pickup_card_ids", []) if str(x).isdigit()]
        if card_id_int not in ids:
            continue
        start_ts = _parse_date(ev.get("jp_release_date") or ev.get("start_date"))
        end_ts   = _parse_date(ev.get("estimated_end_date") or ev.get("end_date"))
        entry = {
            "start_ts":    start_ts,
            "end_ts":      end_ts,
            "gametora_url": ev.get("gametora_url") or "",
        }
        if start_ts and start_ts > now:
            upcoming.append(entry)
        else:
            past.append(entry)

    upcoming.sort(key=lambda x: x["start_ts"] or 0)
    past.sort(key=lambda x: x["start_ts"] or 0, reverse=True)
    return upcoming + past


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------

def _card_image_url(card: dict) -> str:
    if card["kind"] == "support":
        return SUPPORT_IMG_URL.format(card["card_id"])
    return CHARACTER_IMG_URL.format(card["card_id"])


def _build_result_embed(card: dict, banner: dict | None) -> discord.Embed:
    now = time.time()
    link = card["link"]
    if link and not link.startswith("http"):
        link = GAMETORA_BASE + link

    if banner:
        gt_url = banner.get("gametora_url") or link or ""
        start  = banner.get("start_ts")
        if start and start > now:
            desc = f"Next banner: <t:{int(start)}:R> (approximate)"
        elif start:
            desc = f"Last appeared <t:{int(start)}:R>. No upcoming banner found."
        else:
            desc = "Banner date unknown."
    else:
        gt_url = link or ""
        desc   = "No banner data found."

    embed = discord.Embed(
        title=card["name"],
        url=gt_url or None,
        description=desc,
        colour=discord.Colour.gold(),
    )
    embed.set_image(url=_card_image_url(card))
    embed.set_footer(text=f"ID: {card['card_id']}")
    return embed


# ---------------------------------------------------------------------------
# Pillow combined image
# ---------------------------------------------------------------------------

async def _build_combined_image(cards: list[dict]) -> bytes | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning("[lookup] Pillow not installed — skipping combined image")
        return None

    TARGET_H = 200
    GAP      = 8

    images: list[Image.Image] = []
    try:
        async with aiohttp.ClientSession() as session:
            for card in cards:
                url = _card_image_url(card)
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            img  = Image.open(io.BytesIO(data)).convert("RGBA")
                            # Resize to target height
                            w = int(img.width * TARGET_H / img.height)
                            images.append(img.resize((w, TARGET_H), Image.LANCZOS))
                        else:
                            logger.warning(f"[lookup] Image fetch {resp.status}: {url}")
                except Exception as exc:
                    logger.warning(f"[lookup] Failed to fetch {url}: {exc}")
    except Exception as exc:
        logger.error(f"[lookup] Image session error: {exc}")
        return None

    if not images:
        return None

    total_w = sum(img.width for img in images) + GAP * (len(images) - 1)
    canvas  = Image.new("RGBA", (total_w, TARGET_H), (0, 0, 0, 0))

    x = 0
    for i, img in enumerate(images):
        canvas.paste(img, (x, 0))
        # Draw number label
        draw = ImageDraw.Draw(canvas)
        label = str(i + 1)
        try:
            font = ImageFont.truetype("arial.ttf", 32)
        except Exception:
            font = ImageFont.load_default()
        # White text with black outline for visibility
        for dx, dy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
            draw.text((x + 6 + dx, 4 + dy), label, fill=(0, 0, 0, 200), font=font)
        draw.text((x + 6, 4), label, fill=(255, 255, 255, 255), font=font)
        x += img.width + GAP

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

async def autocomplete_whenis(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if not current:
        return []
    # Ensure timeline is cached so _banner_card_ids is populated
    await _get_timeline()
    results = await _search_cards(current, limit=100)
    # Only surface cards that actually appear in at least one banner
    filtered = [
        r for r in results
        if int(r["card_id"]) in _banner_card_ids
    ]
    return [
        app_commands.Choice(name=r["name"], value=r["name"])
        for r in filtered[:25]
    ]


# ---------------------------------------------------------------------------
# Main command handler
# ---------------------------------------------------------------------------

async def handle_whenis(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()  # non-ephemeral — shows "thinking..."

    query = query.strip()
    if not query:
        await interaction.followup.send("Please provide a card or character name.", ephemeral=True)
        return

    # Exact name match (autocomplete delivers full name as value; also handle free-text)
    cards = await _search_cards(query, limit=10)
    exact = [c for c in cards if c["name"].lower() == query.lower()]
    if not exact:
        # Fall back to first fuzzy match if user typed a partial name manually
        exact = cards[:1]
    if not exact:
        await interaction.followup.send(f"No card found matching **{query}**.", ephemeral=True)
        return

    # Deduplicate by card_id (keep first occurrence per ID)
    seen: set[str] = set()
    unique: list[dict] = []
    for c in exact:
        if c["card_id"] not in seen:
            seen.add(c["card_id"])
            unique.append(c)
    cards = unique

    # Banner lookup
    timeline     = await _get_timeline()
    card_banners = [(c, _find_banners(int(c["card_id"]), timeline)) for c in cards]
    has_banner   = [(c, b) for c, b in card_banners if b]

    if not has_banner:
        # No banner history at all — still show card with a no-banner note
        embed = _build_result_embed(cards[0], None)
        await interaction.followup.send(embed=embed)
        return

    # Single result — post publicly
    if len(has_banner) == 1:
        card, banners = has_banner[0]
        embed = _build_result_embed(card, banners[0])
        await interaction.followup.send(embed=embed)
        return

    # Disambiguation needed (multiple card IDs, each with banner history)
    try:
        await interaction.delete_original_response()
    except Exception:
        pass

    img_bytes = await _build_combined_image([c for c, _ in has_banner])

    lines = "\n".join(
        f"{DIGIT_EMOJIS[i]}  {c['name']}"
        for i, (c, _) in enumerate(has_banner)
        if i < len(DIGIT_EMOJIS)
    )
    content = (
        f"Multiple versions of **{query}** found in banners:\n"
        f"{lines}\n"
        f"React to pick one."
    )

    if img_bytes:
        file = discord.File(io.BytesIO(img_bytes), filename="cards.png")
        msg  = await interaction.channel.send(content=content, file=file)
    else:
        msg  = await interaction.channel.send(content=content)

    cap = min(len(has_banner), len(DIGIT_EMOJIS))
    for i in range(cap):
        await msg.add_reaction(DIGIT_EMOJIS[i])

    _pending[msg.id] = {
        "user_id": interaction.user.id,
        "cards":   [c for c, _ in has_banner[:cap]],
        "banners": [b for _, b in has_banner[:cap]],
        "channel": interaction.channel,
        "expires": time.time() + PENDING_TTL,
    }


# ---------------------------------------------------------------------------
# Reaction handler
# ---------------------------------------------------------------------------

async def handle_reaction(payload: discord.RawReactionActionEvent) -> None:
    # Clean up expired entries
    now = time.time()
    expired = [k for k, v in _pending.items() if v["expires"] < now]
    for mid in expired:
        del _pending[mid]

    entry = _pending.get(payload.message_id)
    if not entry:
        return
    if payload.user_id != entry["user_id"]:
        return

    emoji = str(payload.emoji)
    if emoji not in DIGIT_EMOJIS:
        return
    idx = DIGIT_EMOJIS.index(emoji)
    if idx >= len(entry["cards"]):
        return

    card    = entry["cards"][idx]
    banners = entry["banners"][idx]
    del _pending[payload.message_id]

    # Delete disambiguation message
    try:
        await entry["channel"].get_partial_message(payload.message_id).delete()
    except Exception:
        pass

    # Post result publicly
    embed = _build_result_embed(card, banners[0] if banners else None)
    await entry["channel"].send(embed=embed)
