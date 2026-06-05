"""
skills_scraper.py

Scrapes all skills from https://gametora.com/umamusume/skills using Playwright
and saves them to data/skills.db (SQLite).

Run manually:
    venv/bin/python skills_scraper.py

Cron entry (daily at 04:00):
    0 4 * * * cd /home/piberry/Tonberries-Bot && /home/piberry/Tonberries-Bot/venv/bin/python skills_scraper.py >> logs/skills_scraper.log 2>&1
"""

import asyncio
import logging
import os
import sys
import time

import aiosqlite
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

from global_config import SKILLS_DB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("skills_scraper")

SKILLS_URL = "https://gametora.com/umamusume/skills"

# ---------------------------------------------------------------------------
# JS extractor – runs inside the visible tippy popup
# ---------------------------------------------------------------------------
EXTRACT_MAIN_JS = """
() => {
    const popup = document.querySelector('.tippy-box[data-state="visible"] .tippy-content');
    if (!popup) return null;

    const result = {};

    // Skill name (inside the icon header row)
    const nameEl = popup.querySelector('.sc-3b566202-1');
    result.name = nameEl ? nameEl.innerText.trim() : null;

    // Icon URL
    const iconEl = popup.querySelector('img[src*="/skills/icon/"]');
    result.icon_url = iconEl ? iconEl.src : null;

    // Parse labeled tooltip lines – only first occurrence of each label
    const seen = {};
    popup.querySelectorAll('.tooltips_tooltip_line__OStyx').forEach(line => {
        const b = line.querySelector('b');
        if (!b) return;
        const label = b.textContent.trim();
        if (seen[label]) return;
        seen[label] = true;
        const value = line.innerText.replace(b.textContent, '').trim();
        switch (label) {
            case 'Description (in-game):':  result.description           = value; break;
            case 'Description (detailed):': result.description_detailed  = value; break;
            case 'Rarity:':                 result.rarity                = value; break;
            case 'Activation:':             result.activation            = value; break;
            case 'Base duration:':          result.base_duration         = value; break;
            case 'Effect:':                 result.effect                = value; break;
            case 'Base cost:':              result.base_cost             = value; break;
        }
    });

    // Characters – collect alt text from inline character thumbnails
    const charImgs = [...popup.querySelectorAll('.tooltips_inline_image__Zi70Y img')];
    const chars = charImgs.map(img => img.alt).filter(Boolean);
    result.character_name = chars.join(', ') || null;

    // Conditions (first pre-wrap div = main version)
    const condDiv = popup.querySelector('[style*="white-space: pre-wrap"]');
    result.conditions = condDiv ? condDiv.innerText.trim() : null;

    // Skill ID from the "Show conditions in the viewer" link
    const viewerLink = popup.querySelector('a[href*="skill-condition-viewer"]');
    if (viewerLink) {
        const m = viewerLink.href.match(/skill=(\\d+)/);
        result.skill_id = m ? parseInt(m[1]) : null;
    }

    // Has an inherited version toggle?
    // Identified by the text "When inherited" inside a .utils_linkcolor__rvv3k span.
    // Don't use span[aria-expanded] – character thumbnails carry the same attribute.
    result.has_inherited = popup.innerText.includes('When inherited');

    return result;
}
"""


# ---------------------------------------------------------------------------
# Parse inherited version from popup inner text after clicking "When inherited"
# ---------------------------------------------------------------------------
def _parse_inherited_text(popup_text: str) -> dict:
    """
    After clicking "When inherited", the popup inserts an 'Inherited Version'
    section above the original stats block.  Parse the section after the marker.
    """
    parts = popup_text.split("Inherited Version", 1)
    if len(parts) < 2:
        return {}

    section = parts[1]
    lines = [l.strip() for l in section.split("\n") if l.strip()]

    result: dict = {}
    cond_lines: list[str] = []
    in_conditions = False

    NEXT_FIELD_PREFIXES = (
        "Rarity:", "Activation:", "Base duration:", "Base cost:",
        "Effect:", "Show conditions", "Description", "Characters",
    )

    for line in lines:
        if in_conditions:
            if any(line.startswith(p) for p in NEXT_FIELD_PREFIXES):
                result["inherited_conditions"] = "\n".join(cond_lines)
                in_conditions = False
                cond_lines = []
            else:
                cond_lines.append(line)
                continue

        if line.startswith("Rarity:") and "inherited_rarity" not in result:
            result["inherited_rarity"] = line[len("Rarity:"):].strip()
        elif line.startswith("Activation:") and "inherited_activation" not in result:
            result["inherited_activation"] = line[len("Activation:"):].strip()
        elif line.startswith("Base cost:") and "inherited_base_cost" not in result:
            result["inherited_base_cost"] = line[len("Base cost:"):].strip()
        elif line == "Conditions:" and "inherited_conditions" not in result:
            in_conditions = True
        elif line.startswith("Base duration:") and "inherited_duration" not in result:
            result["inherited_duration"] = line[len("Base duration:"):].strip()
        elif line.startswith("Effect:") and "inherited_effect" not in result:
            result["inherited_effect"] = line[len("Effect:"):].strip()

    if in_conditions and cond_lines:
        result["inherited_conditions"] = "\n".join(cond_lines)

    return result


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

async def init_db() -> None:
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(SKILLS_DB) as conn:
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS skills (
                name                 TEXT PRIMARY KEY,
                skill_id             INTEGER UNIQUE,
                icon_url             TEXT,
                description          TEXT,
                description_detailed TEXT,
                character_name       TEXT,
                rarity               TEXT,
                activation           TEXT,
                conditions           TEXT,
                base_duration        TEXT,
                base_cost            TEXT,
                effect               TEXT,
                inherited_rarity     TEXT,
                inherited_activation TEXT,
                inherited_base_cost  TEXT,
                inherited_conditions TEXT,
                inherited_duration   TEXT,
                inherited_effect     TEXT,
                scraped_at           INTEGER NOT NULL
            );
        """)
        await conn.commit()
    logger.info("[Skills] Database initialised")


async def save_skill(conn: aiosqlite.Connection, skill: dict, scraped_at: int) -> None:
    await conn.execute(
        """
        INSERT OR REPLACE INTO skills (
            name, skill_id, icon_url, description, description_detailed,
            character_name, rarity, activation, conditions, base_duration,
            base_cost, effect,
            inherited_rarity, inherited_activation, inherited_base_cost,
            inherited_conditions, inherited_duration, inherited_effect,
            scraped_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            skill.get("name"),
            skill.get("skill_id"),
            skill.get("icon_url"),
            skill.get("description"),
            skill.get("description_detailed"),
            skill.get("character_name"),
            skill.get("rarity"),
            skill.get("activation"),
            skill.get("conditions"),
            skill.get("base_duration"),
            skill.get("base_cost"),
            skill.get("effect"),
            skill.get("inherited_rarity"),
            skill.get("inherited_activation"),
            skill.get("inherited_base_cost"),
            skill.get("inherited_conditions"),
            skill.get("inherited_duration"),
            skill.get("inherited_effect"),
            scraped_at,
        ),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

async def scrape() -> int:
    """Scrape all skills from GameTora. Returns number of skills saved."""
    scraped_at = int(time.time())
    saved = 0

    async with aiosqlite.connect(SKILLS_DB) as conn:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1600, "height": 1000})

            logger.info(f"[Skills] Loading {SKILLS_URL}")
            await page.goto(SKILLS_URL, wait_until="load", timeout=60_000)
            await page.wait_for_timeout(6000)

            # Expand the full skills list
            show_all = page.locator("text=Show all")
            if await show_all.count():
                await show_all.first.click()
                logger.info("[Skills] Clicked 'Show all' – waiting for list to render…")
                await page.wait_for_timeout(3000)

            total = await page.locator("text=More").count()
            logger.info(f"[Skills] Found {total} skills to scrape")

            for i in range(total):
                try:
                    btn = page.locator("text=More").nth(i)
                    await btn.scroll_into_view_if_needed()
                    await btn.click()

                    # Wait for the tippy popup
                    popup_box = page.locator(".tippy-box[data-state='visible']").first
                    await popup_box.wait_for(state="visible", timeout=8000)
                    popup_content = popup_box.locator(".tippy-content").first
                    await page.wait_for_timeout(300)

                    # Extract main data
                    data = await page.evaluate(EXTRACT_MAIN_JS)
                    if not data or not data.get("name"):
                        logger.warning(f"[Skills] [{i+1}/{total}] Empty data – skipping")
                        continue

                    # Extract inherited version if available
                    if data.get("has_inherited"):
                        try:
                            wi = popup_content.locator("text=When inherited").first
                            if await wi.is_visible():
                                await wi.click()
                                await page.wait_for_timeout(500)
                                popup_text = await popup_content.first.inner_text()
                                inherited = _parse_inherited_text(popup_text)
                                data.update(inherited)
                        except Exception as exc:
                            logger.debug(
                                f"[Skills] [{i+1}/{total}] Inherited extraction failed: {exc}"
                            )

                    await save_skill(conn, data, scraped_at)
                    saved += 1

                    if saved % 50 == 0 or i + 1 == total:
                        logger.info(
                            f"[Skills] Progress: {i+1}/{total} scraped, {saved} saved"
                        )

                except PWTimeoutError:
                    logger.warning(f"[Skills] [{i+1}/{total}] Popup timeout – skipping")
                except Exception as exc:
                    logger.error(f"[Skills] [{i+1}/{total}] Error: {exc}")
                finally:
                    # Close any open popup by clicking outside it (search input is safe)
                    try:
                        await page.locator(
                            "input[placeholder*='skill name']"
                        ).click(timeout=3000)
                        await page.wait_for_timeout(200)
                    except Exception:
                        try:
                            await page.keyboard.press("Escape")
                            await page.wait_for_timeout(200)
                        except Exception:
                            pass

            await browser.close()

    return saved


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    await init_db()
    logger.info("[Skills] Starting full scrape…")
    saved = await scrape()
    logger.info(f"[Skills] Done – {saved} skills saved to {SKILLS_DB}")


if __name__ == "__main__":
    asyncio.run(main())
