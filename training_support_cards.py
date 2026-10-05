"""
training_support_cards.py

Builds the horizontal support-card strip used as the `image` (bottom, full-width
slot) on both training_start (dashboard) and training_end (result) embeds.

training_end's `supportCards` array has the full picture:
    { "position": 1-6, "supportCardId": ..., "exp": ..., "limitBreakCount": 0-4 }
-> build_support_card_strip(), each card stamped with its limit-break count.

training_start only has bare IDs — `supportCardIds` (positions 1-5) and
`friendSupportCardId` (position 6) — limitBreakCount doesn't exist yet at that
point, so build_support_card_ids_strip() renders the same strip without any
count stamped.

Either way, position 6 is always the friend/borrow slot (see
api_contract_plan.md) — the last (rightmost) card after ordering, which always
gets a "Borrow" label regardless of whether a limit-break count is shown.
"""

import io
import os

from PIL import Image, ImageDraw, ImageFont

from global_config import UMA_TOOLS_SUPPORT_ICON_DIR
import training_decode as decode

_FONT_PATH = "fonts/FOT-UDKakugo C80 Pro.ttf"
_NATIVE_CARD_SIZE = 256          # native support_card_s_*.png size
_SCALE = 3                       # "increase the size by a lot" — upscaled with the stamped text
_TEXT_SCALE = 2                  # on top of _SCALE — text was still too small at _SCALE alone
_CARD_SIZE = _NATIVE_CARD_SIZE * _SCALE
_FONT_SIZE = 28 * _SCALE * _TEXT_SCALE
_PADDING = 8 * _SCALE
_STROKE_WIDTH = 2 * _SCALE * _TEXT_SCALE

# Type-icon badge (Speed/Stamina/.../Group, top-left corner) — native uma-tools icons
# are 70x70 against a 256x256 card, so this keeps roughly that same proportion at
# whatever _CARD_SIZE ends up being.
_TYPE_ICON_SIZE = round(_CARD_SIZE * 70 / _NATIVE_CARD_SIZE)


# Lowercased filename -> actual filename, cached lazily. Most of uma-tools'
# ~1000 support_card_s_*.png files are consistently lowercase, but a handful
# aren't (e.g. "Support_card_s_30024.png", capital S in "Support") — case
# insensitive lookup handles those (and any other latent ones) without
# hardcoding exceptions per ID.
_support_icon_index: dict[str, str] | None = None


def _support_icon_filename_index() -> dict[str, str]:
    global _support_icon_index
    if _support_icon_index is None:
        _support_icon_index = {}
        if os.path.isdir(UMA_TOOLS_SUPPORT_ICON_DIR):
            for fn in os.listdir(UMA_TOOLS_SUPPORT_ICON_DIR):
                _support_icon_index[fn.lower()] = fn
    return _support_icon_index


def _card_image(support_card_id: int) -> Image.Image:
    expected = f"support_card_s_{support_card_id}.png"
    actual = _support_icon_filename_index().get(expected.lower())
    if actual:
        path = os.path.join(UMA_TOOLS_SUPPORT_ICON_DIR, actual)
        img = Image.open(path).convert("RGBA")
        return img.resize((_CARD_SIZE, _CARD_SIZE), Image.LANCZOS)
    # Missing icon — a plain placeholder rather than erroring, same
    # degrade-gracefully convention as the rest of the training feature.
    placeholder = Image.new("RGBA", (_CARD_SIZE, _CARD_SIZE), (60, 60, 60, 255))
    draw = ImageDraw.Draw(placeholder)
    font = ImageFont.truetype(_FONT_PATH, _FONT_SIZE)
    draw.text((_CARD_SIZE // 2, _CARD_SIZE // 2), "?", font=font, fill="white", anchor="mm")
    return placeholder


def _draw_type_icon(img: Image.Image, support_card_id: int) -> None:
    """Pastes the card's training-type badge (Speed/Stamina/.../Group) in the top-left
    corner — same corner the game's own support-card art uses for this badge. Silently
    does nothing if the card or its type icon isn't resolvable (degrade gracefully,
    same convention as the rest of the training feature)."""
    icon_path = decode.support_card_type_icon_path(support_card_id)
    if not icon_path:
        return
    icon = Image.open(icon_path).convert("RGBA").resize((_TYPE_ICON_SIZE, _TYPE_ICON_SIZE), Image.LANCZOS)
    img.paste(icon, (_PADDING, _PADDING), icon)


def _draw_corner_text(img: Image.Image, text: str, corner: str) -> None:
    """corner: "bottom_right" or "top_right". White text, black stroke for contrast."""
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(_FONT_PATH, _FONT_SIZE)
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=_STROKE_WIDTH)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = img.width - w - _PADDING
    y = (img.height - h - _PADDING) if corner == "bottom_right" else _PADDING
    draw.text((x, y), text, font=font, fill="white", stroke_width=_STROKE_WIDTH, stroke_fill="black")


def _render_strip(cards: list[dict]) -> io.BytesIO | None:
    """cards: ordered list of {"supportCardId": ..., "limitBreakCount": ...(optional)}.
    The count is only stamped when the key is present — absent (not 0) means unknown."""
    if not cards:
        return None

    images = []
    for i, card in enumerate(cards):
        img = _card_image(card["supportCardId"]).copy()
        _draw_type_icon(img, card["supportCardId"])
        if "limitBreakCount" in card:
            _draw_corner_text(img, f"{card['limitBreakCount']}LB", "bottom_right")
        if i == len(cards) - 1:
            _draw_corner_text(img, "Borrow", "top_right")
        images.append(img)

    total_width = sum(im.width for im in images)
    height = max(im.height for im in images)
    strip = Image.new("RGBA", (total_width, height), (0, 0, 0, 0))
    x = 0
    for im in images:
        strip.paste(im, (x, 0), im)
        x += im.width

    buf = io.BytesIO()
    strip.save(buf, format="PNG")
    buf.seek(0)
    return buf


def build_support_card_strip(support_cards: list[dict]) -> io.BytesIO | None:
    """training_end version: full data (including limitBreakCount), sorted by position."""
    if not support_cards:
        return None
    cards = sorted(support_cards, key=lambda c: c.get("position", 0))
    return _render_strip(cards)


# Rendered PNG bytes for the bare-ID strip, keyed by the ordered card IDs (friend slot
# last, so the "Borrow" stamp position is part of the key). Rendering costs ~1.6s of
# blocking CPU and produces a ~3 MB PNG on the Pi, and a deck rarely changes — so a
# repeated deck (every training_progress turn, abandoned notices, the next run with the
# same deck) reuses the bytes. In-memory only: a bot restart just costs one re-render.
_ids_strip_cache: dict[tuple[int, ...], bytes] = {}
_IDS_STRIP_CACHE_MAX = 8


def build_support_card_ids_strip(support_card_ids: list[int] | None,
                                  friend_support_card_id: int | None) -> io.BytesIO | None:
    """training_start version: bare IDs only, friend slot always last — no LB stamps."""
    ids = list(support_card_ids or [])
    if friend_support_card_id is not None:
        ids.append(friend_support_card_id)
    if not ids:
        return None

    key = tuple(ids)
    data = _ids_strip_cache.get(key)
    if data is None:
        data = _render_strip([{"supportCardId": sid} for sid in ids]).getvalue()
        if len(_ids_strip_cache) >= _IDS_STRIP_CACHE_MAX:
            _ids_strip_cache.pop(next(iter(_ids_strip_cache)))  # oldest entry
        _ids_strip_cache[key] = data
    return io.BytesIO(data)  # fresh buffer each call — discord.File consumes it
