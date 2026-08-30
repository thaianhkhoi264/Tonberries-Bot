"""
leaderboard/build.py

Ties `data` + `render` together: build the Monthly Fan Leaderboard PNG from the
bot's live databases. Called by the `render monthly` owner command.
"""

from __future__ import annotations

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
    """
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


if __name__ == "__main__":   # quick manual check: writes leaderboard_monthly.png
    now = datetime.now(timezone.utc)
    im, lbl = build_monthly_image()
    out = f"leaderboard_monthly_{now:%Y%m%d}.png"
    im.save(out)
    print(f"{lbl} -> {out}  ({im.size})")
