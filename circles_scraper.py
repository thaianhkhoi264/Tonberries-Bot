"""
circles_scraper.py
Scrapes https://uma.moe/circles/<id> with Playwright and saves
club + member data to data/circles.db.

Run manually:
    venv/bin/python circles_scraper.py [circle_id]

Cron entry (every 3 hours):
    0 */3 * * * cd /home/piberry/Tonberries-Bot && /home/piberry/Tonberries-Bot/venv/bin/python circles_scraper.py >> logs/circles_scraper.log 2>&1
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone

import aiosqlite
from playwright.async_api import async_playwright

from global_config import CIRCLE_ID, CIRCLES_DB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("circles_scraper")

CIRCLES_URL = "https://uma.moe/circles/{circle_id}"

RANK_TIERS = {
    "01": "D",  "02": "D+", "03": "C",  "04": "C+",
    "05": "B",  "06": "B+", "07": "A",  "08": "A+",
    "09": "S",  "10": "S+", "11": "SS",
}

EXTRACT_JS = """
() => {
    const rankIconEl = document.querySelector('img.header-rank-icon');
    const rankIconSrc = rankIconEl ? rankIconEl.getAttribute('src') : null;

    const rankLabelEl = document.querySelector('.rank-label');
    const rankNumber = rankLabelEl
        ? (rankLabelEl.querySelector('app-animated-number') || rankLabelEl).innerText.trim()
        : null;

    const bufferEl = document.querySelector('.tier-gap-value.safe app-animated-number');
    const buffer = bufferEl ? bufferEl.innerText.trim() : null;

    const bufferDeltaEl = document.querySelector('.tier-side.lower .tier-delta app-animated-number');
    const bufferDelta = bufferDeltaEl ? bufferDeltaEl.innerText.trim() : null;

    const neededEl = document.querySelector('.tier-gap-value.needed app-animated-number');
    const needed = neededEl ? neededEl.innerText.trim() : null;

    const neededDeltaEl = document.querySelector('.tier-side.upper .tier-delta app-animated-number');
    const neededDelta = neededDeltaEl ? neededDeltaEl.innerText.trim() : null;

    const members = [];
    document.querySelectorAll('.member-card').forEach(card => {
        const nameEl = card.querySelector('.member-name-link');
        const nameNode = nameEl
            ? Array.from(nameEl.childNodes).find(n => n.nodeType === Node.TEXT_NODE)
            : null;
        const name = nameNode ? nameNode.textContent.trim() : null;
        if (!name) return;

        const isInactive = !!card.querySelector('.status-badge.inactive');

        let monthlyGain = null;
        let sevenDayAvg = null;

        card.querySelectorAll('.stat-row').forEach(row => {
            const label = row.querySelector('.label');
            const value = row.querySelector('app-animated-number');
            if (!label || !value) return;
            const labelText = label.innerText.trim();
            if (labelText === 'Monthly Gain') monthlyGain = value.innerText.trim();
            if (labelText === '7 Day Avg')    sevenDayAvg  = value.innerText.trim();
        });

        members.push({ name, monthlyGain, sevenDayAvg, isInactive });
    });

    return { rankIconSrc, rankNumber, buffer, bufferDelta, needed, neededDelta, members };
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_rank_tier(src: str | None) -> str:
    if not src:
        return "Unknown"
    m = re.search(r'circle_rank_(\d{2})\.webp', src)
    return RANK_TIERS.get(m.group(1), "Unknown") if m else "Unknown"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

async def init_db() -> None:
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(CIRCLES_DB) as conn:
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS circle_club (
                id            INTEGER PRIMARY KEY DEFAULT 1,
                scraped_at    INTEGER NOT NULL,
                rank_tier     TEXT,
                rank_icon_src TEXT,
                rank_number   TEXT,
                buffer        TEXT,
                buffer_delta  TEXT,
                needed        TEXT,
                needed_delta  TEXT
            );

            CREATE TABLE IF NOT EXISTS circle_members (
                name          TEXT PRIMARY KEY,
                monthly_gain  TEXT,
                seven_day_avg TEXT,
                is_inactive   INTEGER DEFAULT 0,
                scraped_at    INTEGER NOT NULL
            );
        """)
        await conn.commit()
    logger.info("[Circles] Database initialised")


async def save(data: dict) -> None:
    now     = int(datetime.now(timezone.utc).timestamp())
    members = data.get("members", [])

    async with aiosqlite.connect(CIRCLES_DB) as conn:
        await conn.execute(
            """
            INSERT OR REPLACE INTO circle_club
                (id, scraped_at, rank_tier, rank_icon_src, rank_number,
                 buffer, buffer_delta, needed, needed_delta)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                data.get("rankTier"),
                data.get("rankIconSrc"),
                data.get("rankNumber"),
                data.get("buffer"),
                data.get("bufferDelta"),
                data.get("needed"),
                data.get("neededDelta"),
            ),
        )

        # Full roster replacement so stale/left members don't linger
        await conn.execute("DELETE FROM circle_members")
        await conn.executemany(
            """
            INSERT INTO circle_members
                (name, monthly_gain, seven_day_avg, is_inactive, scraped_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    m["name"],
                    m.get("monthlyGain"),
                    m.get("sevenDayAvg"),
                    1 if m.get("isInactive") else 0,
                    now,
                )
                for m in members
            ],
        )
        await conn.commit()

    active = sum(1 for m in members if not m.get("isInactive"))
    logger.info(f"[Circles] Saved {active} active + {len(members) - active} inactive members")


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

async def scrape(circle_id: str) -> dict:
    url = CIRCLES_URL.format(circle_id=circle_id)
    logger.info(f"[Circles] Launching Chromium → {url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            bypass_csp=True,
        )
        await context.set_extra_http_headers({
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })

        page = await context.new_page()
        await page.goto(url, timeout=60_000)
        await page.wait_for_load_state("load")
        logger.info("[Circles] Waiting for Angular to render...")
        await asyncio.sleep(5)

        data = await page.evaluate(EXTRACT_JS)
        await context.close()
        await browser.close()

    data["rankTier"] = _parse_rank_tier(data.get("rankIconSrc"))
    logger.info(
        f"[Circles] Scraped — rank {data['rankTier']} (#{data.get('rankNumber')}), "
        f"{len(data['members'])} members total"
    )
    return data


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(circle_id: str) -> None:
    await init_db()
    data = await scrape(circle_id)
    await save(data)
    logger.info("[Circles] Done")


if __name__ == "__main__":
    circle_id = sys.argv[1] if len(sys.argv) > 1 else CIRCLE_ID
    asyncio.run(main(circle_id))
