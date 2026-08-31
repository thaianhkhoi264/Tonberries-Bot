import aiohttp

from bot import logger
from global_config import CIRCLE_ID, UMA_MOE_API_KEY

_BASE = "https://uma.moe/api/v4"


async def fetch_circle(
    circle_id: str | int = CIRCLE_ID,
    year: int | None = None,
    month: int | None = None,
) -> dict:
    """Fetch circle details and member fan data from the uma.moe API.

    Returns the full API response dict with keys: circle, members,
    club_rank, fans_to_next_tier, fans_to_lower_tier,
    yesterday_fans_to_next_tier, yesterday_fans_to_lower_tier.
    """
    params: dict = {"circle_id": int(circle_id)}
    if year is not None:
        params["year"] = year
    if month is not None:
        params["month"] = month

    headers = {"X-API-Key": UMA_MOE_API_KEY} if UMA_MOE_API_KEY else {}
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            f"{_BASE}/circles", params=params, headers=headers
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(
                    f"[UmaMoeAPI] GET /circles returned {resp.status}: {text[:300]}"
                )
                resp.raise_for_status()
            return await resp.json()


async def fetch_rank_thresholds() -> list[dict]:
    """Fetch the fan cutoff for each circle rank tier (D through SS).

    Returns the ``thresholds`` list from the API response; each entry has
    ``name`` (e.g. "A+"), ``ranking_from``/``ranking_to`` and
    ``current_min_fans`` (the cutoff to sit in that tier).
    """
    headers = {"X-API-Key": UMA_MOE_API_KEY} if UMA_MOE_API_KEY else {}
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            f"{_BASE}/circles/rank-thresholds", headers=headers
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(
                    f"[UmaMoeAPI] GET /circles/rank-thresholds returned "
                    f"{resp.status}: {text[:300]}"
                )
                resp.raise_for_status()
            data = await resp.json()
            return data.get("thresholds", [])
