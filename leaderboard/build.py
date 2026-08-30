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
                        guides: bool = False) -> tuple[Image.Image, str]:
    """Render the leaderboard for a month (default: the current month).

    Returns (RGBA image, month label e.g. "August 2026").
    """
    layout = render.load_layout()
    label = data.month_label(year, month)
    members = data.top_members(year, month)

    texts: dict[str, str] = {"month_text": label}
    petits: dict[str, str] = {}
    colors: dict[int, str] = {}
    for i, (name, fans) in enumerate(members, 1):
        texts[f"rank{i}_name"] = name
        texts[f"rank{i}_fans"] = f"{fans:,}"
        petit, color = data.fan_character(name)
        if petit:
            petits[f"rank{i}_petit"] = petit
        colors[i] = color or data.NEUTRAL_COLOR

    img, _ = render.render(layout, texts, petits, colors, guides=guides)
    return img, label


def image_filename(label: str) -> str:
    return f"fan_leaderboard_{label.replace(' ', '_').lower()}.png"


if __name__ == "__main__":   # quick manual check: writes leaderboard_monthly.png
    now = datetime.now(timezone.utc)
    im, lbl = build_monthly_image()
    out = f"leaderboard_monthly_{now:%Y%m%d}.png"
    im.save(out)
    print(f"{lbl} -> {out}  ({im.size})")
