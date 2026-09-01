"""
leaderboard/build.py

Ties `data` + `render` together: build the Monthly Fan Leaderboard PNG from the
bot's live databases. Called by the `render monthly` owner command.
"""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone

from PIL import Image

from leaderboard import data, render


def build_monthly_image(*, year: int | None = None, month: int | None = None,
                        guides: bool = False,
                        member_roles: dict[int, set[int]] | None = None
                        ) -> tuple[Image.Image, str]:
    """Render the leaderboard for a month (default: the current month).

    `member_roles` maps discord_user_id -> set of that member's role ids (built
    by the command from the live guild). Fan characters are resolved from these
    actual roles matched against `character_roles`; without it, only members who
    used `/role` resolve.

    Returns (RGBA image, month label e.g. "August 2026").

    With no month given, uses the current in-game month — or, if that month has
    no data yet (e.g. just after a reset), the most recent finished month.
    """
    if year is None and month is None:
        members = data.top_members()
        if not members:
            fin = data.latest_finalized_month()
            if fin:
                year, month = fin

    layout = render.load_layout()
    label = data.month_label(year, month)
    members = data.top_members(year, month)
    eliminated = data.eliminated_ranks(members, data.monthly_requirement())

    links = data.linked_discord_ids()
    role_slugs = data.fan_role_slugs()
    role_times = data.user_fan_role_times()
    member_roles = member_roles or {}

    texts: dict[str, str] = {"month_text": label}
    petits: dict[str, str] = {}
    colors: dict[int, str] = {}
    for i, (name, fans) in enumerate(members, 1):
        texts[f"rank{i}_name"] = name
        texts[f"rank{i}_fans"] = f"{fans:,}"

        uid = links.get(name.strip().lower())
        slug = data.resolve_fan_slug(uid, member_roles.get(uid, set()),
                                     role_slugs, role_times) if uid else None
        petit, color = data.slug_assets(slug) if slug else (None, None)
        if petit:
            petits[f"rank{i}_petit"] = petit
        colors[i] = color or data.NEUTRAL_COLOR

    img, _ = render.render(layout, texts, petits, colors, eliminated, guides=guides)
    return img, label


def image_filename(label: str) -> str:
    return f"fan_leaderboard_{label.replace(' ', '_').lower()}.png"


def has_data(year: int | None = None, month: int | None = None) -> bool:
    """Whether any member snapshots exist for the given month."""
    return bool(data.top_members(year, month))


async def render_monthly_png(guild=None, *, year: int | None = None,
                             month: int | None = None
                             ) -> tuple[io.BytesIO, str, str]:
    """Build the leaderboard PNG for a month, off the event loop.

    Resolves each member's current fan roles from the live `guild` (so
    manually-assigned fan roles count, not just ones set via `/role`).
    Returns (PNG bytes, filename, month label).
    """
    member_roles: dict[int, set[int]] = {}
    if guild is not None:
        if not guild.chunked:
            await guild.chunk()
        member_roles = {m.id: {r.id for r in m.roles} for m in guild.members}

    img, label = await asyncio.to_thread(
        build_monthly_image, year=year, month=month, member_roles=member_roles
    )
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf, image_filename(label), label


if __name__ == "__main__":   # quick manual check: writes leaderboard_monthly.png
    now = datetime.now(timezone.utc)
    im, lbl = build_monthly_image()
    out = f"leaderboard_monthly_{now:%Y%m%d}.png"
    im.save(out)
    print(f"{lbl} -> {out}  ({im.size})")
