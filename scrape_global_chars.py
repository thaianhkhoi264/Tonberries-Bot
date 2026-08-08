"""
scrape_global_chars.py

Scrapes ONLY globally-released characters from https://gametora.com/umamusume/characters
BEFORE clicking "Show upcoming characters".

JP-only characters are present in the DOM but hidden via CSS; this script filters
them out by checking computed visibility on each character card.

Output: data/gt_global_chars.json
  {
    "scraped_at": <unix_timestamp>,
    "characters": [
      {"id": "100101", "name": "Special Week (Original)"},
      ...
    ]
  }

Run (from repo root):
    python scrape_global_chars.py
"""

import asyncio
import json
import os
import time

from playwright.async_api import async_playwright

from global_config import GT_GLOBAL_CHARS_JSON

CHARACTER_URL = "https://gametora.com/umamusume/characters"

# JS that extracts only VISIBLE character cards (before any "Show upcoming" click).
# Filters out elements that are hidden via CSS (display:none / visibility:hidden on
# the element or any ancestor up to <body>).
_EXTRACT_VISIBLE_JS = r"""() => {
    function isVisible(el) {
        let node = el;
        while (node && node !== document.body) {
            const cs = window.getComputedStyle(node);
            if (cs.display === 'none' || cs.visibility === 'hidden') return false;
            node = node.parentElement;
        }
        return true;
    }

    const links = Array.from(document.querySelectorAll('a[href*="/umamusume/characters/"]'));
    const results = [];
    const seenIds = new Set();

    for (const a of links) {
        const href = a.getAttribute('href') || '';
        if (href.includes('/profiles')) continue;
        const idMatch = href.match(/\/characters\/(\d+)/);
        if (!idMatch) continue;
        const charId = idMatch[1];
        if (seenIds.has(charId)) continue;

        // Only include characters whose card is visible (not hidden by CSS)
        if (!isVisible(a)) continue;

        seenIds.add(charId);

        const versionSpan = a.querySelector('span[class*="characters_list_char_version"]');
        const nameSpan    = a.querySelector('span[class*="sc-"]');
        if (!nameSpan) continue;

        // Normalise line-break display names (e.g. "Matikane-\nFukukitaru")
        let name = nameSpan.innerText.replace(/-\n/g, '').replace(/\n/g, ' ').trim();
        if (!name) continue;

        if (versionSpan) {
            const version = versionSpan.innerText.trim();
            if (version) name = name + ' (' + version + ')';
        }

        results.push({ id: charId, name });
    }
    return results;
}"""


async def scrape_visible_characters() -> list[dict]:
    """Navigate to the characters page and extract only visible character cards."""
    print(f"[GlobalChars] Navigating to {CHARACTER_URL}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(90_000)
        await page.goto(CHARACTER_URL, timeout=90_000, wait_until="domcontentloaded")

        # Wait for character card links to appear (excluding nav/profile links)
        try:
            await page.wait_for_selector(
                'a[href*="/umamusume/characters/"]:not([href*="/profiles"])',
                state="attached",
                timeout=30_000,
            )
        except Exception:
            pass

        # Let the card grid finish rendering — do NOT click "Show upcoming"
        await asyncio.sleep(4)

        raw = await page.evaluate(_EXTRACT_VISIBLE_JS)
        await browser.close()

    print(f"[GlobalChars] Found {len(raw)} visible characters")
    return raw


async def main() -> None:
    chars = await scrape_visible_characters()
    payload = {
        "scraped_at": int(time.time()),
        "characters": chars,
    }
    os.makedirs(os.path.dirname(GT_GLOBAL_CHARS_JSON) or ".", exist_ok=True)
    with open(GT_GLOBAL_CHARS_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[GlobalChars] Saved {len(chars)} characters to {GT_GLOBAL_CHARS_JSON}")


if __name__ == "__main__":
    asyncio.run(main())
