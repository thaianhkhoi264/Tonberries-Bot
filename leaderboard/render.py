"""
leaderboard/render.py

Renderer for the Monthly Fan Leaderboard image. `render(layout, texts, petits,
colors)` returns an RGBA image plus a {name: (x,y,w,h)} box map.

Element types (`type` in layout.json): `background` (paints first), `image`,
`dia_pair` (mirrored mascots), `text`, `order` (着 rank badge), `petit`
(character chibi). All positions/sizes live in `layout.json` so calibration
persists between sessions.

Per-member colour: `rank{N}_name` / `rank{N}_fans` are repainted from
`colors[N]` (the member's fan character's hex) via `member_paint()`.

Coordinates
-----------
`align`: "center" -> element centered on canvas_width/2 + x
         "left"   -> element left edge at x
         "right"  -> element right edge at canvas_width - x
`y` is always the element's top edge.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

REPO_ROOT = Path(__file__).resolve().parent.parent
LAYOUT_PATH = Path(__file__).resolve().parent / "layout.json"

Box = tuple[int, int, int, int]  # x, y, w, h
_RANK_ANY_RE = re.compile(r"^rank(\d+)_(name|fans|num|petit)$")

# "Eliminated" members (monthly fans below the requirement) — comedic treatment.
ELIM_FONT = "fonts/HelpMe.ttf"          # name + "ELIMINATED" only (number keeps its font)
ELIM_RED = "#DC2A2A"
ELIM_RED_DARK = "#5C0E0E"
ELIM_X = "#5E0C0C"                      # the big crossed-out X (no stroke)
ELIM_X_MIN_RATIO = 3.5                  # X width : height
ELIM_X_OVERSHOOT = 0.035                # extend past the number / petit (fraction of span)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_layout(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else LAYOUT_PATH
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_layout(data: dict, path: str | os.PathLike | None = None) -> None:
    p = Path(path) if path else LAYOUT_PATH
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# Colour / gradient helpers
# ---------------------------------------------------------------------------

def _hex_rgba(s: str) -> tuple[int, int, int, int]:
    s = s.lstrip("#")
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    a = int(s[6:8], 16) if len(s) >= 8 else 255
    return r, g, b, a


def _lerp(c0, c1, t: float):
    return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))


def _hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(v))) for v in rgb[:3]))


def lighten(color: str, amt: float) -> str:
    """Blend a colour toward white by `amt` (0..1)."""
    r, g, b, _ = _hex_rgba(color)
    return _hex((r + (255 - r) * amt, g + (255 - g) * amt, b + (255 - b) * amt))


def darken(color: str, amt: float) -> str:
    """Scale a colour toward black by `amt` (0..1)."""
    r, g, b, _ = _hex_rgba(color)
    return _hex((r * (1 - amt), g * (1 - amt), b * (1 - amt)))


# Stroke gradients for the podium (from = top, to = bottom).
_TIER_STROKE = {1: ("#894900", "#FFC521"),
                2: ("#5D527E", "#B2CBF1"),
                3: ("#7B4A39", "#EFAF6B")}


def member_paint(rank: int, color: str, stroke_width: int) -> dict:
    """fill + stroke specs for a member's name/fan-count text, from their fan
    character's colour.

    1-3 : slight top-lighter fill gradient; outline = a thin black ring inside a
          fixed tier stroke gradient (for contrast / readability).
    4+  : solid fill; a much darker solid outline.
    """
    if rank <= 3:
        a, b = _TIER_STROKE[rank]
        inner_w = max(2, min(5, round(stroke_width * 0.42)))
        return {
            "fill": {"from": lighten(color, 0.42), "to": color, "angle": 90},
            "stroke": [
                {"width": stroke_width, "from": a, "to": b, "angle": 90},
                {"width": inner_w, "color": "#000000"},
            ],
        }
    sw = stroke_width if rank <= 10 else max(2, round(stroke_width * 0.7))
    return {"fill": color,
            "stroke": {"width": sw, "color": darken(color, 0.72)}}


def linear_gradient(w: int, h: int, c_from: str, c_to: str, angle: float = 90) -> Image.Image:
    """RGBA gradient. angle 90 = top->bottom, 0 = left->right, else rotated."""
    c0, c1 = _hex_rgba(c_from), _hex_rgba(c_to)
    w, h = max(1, w), max(1, h)
    angle %= 360

    if angle == 90:
        col = Image.new("RGBA", (1, h))
        for y in range(h):
            col.putpixel((0, y), _lerp(c0, c1, y / max(h - 1, 1)))
        return col.resize((w, h))
    if angle == 0:
        row = Image.new("RGBA", (w, 1))
        for x in range(w):
            row.putpixel((x, 0), _lerp(c0, c1, x / max(w - 1, 1)))
        return row.resize((w, h))

    diag = int(math.ceil(math.hypot(w, h)))
    row = Image.new("RGBA", (diag, 1))
    for x in range(diag):
        row.putpixel((x, 0), _lerp(c0, c1, x / max(diag - 1, 1)))
    grad = row.resize((diag, diag)).rotate(-angle, resample=Image.BICUBIC)
    left, top = (diag - w) // 2, (diag - h) // 2
    return grad.crop((left, top, left + w, top + h))


# ---------------------------------------------------------------------------
# Text layer  (gradient fill + gradient stroke)
# ---------------------------------------------------------------------------

def _text_geometry(text: str, font: ImageFont.FreeTypeFont, max_sw: int, tracking: int):
    """Return (W, H, mask_fn) where mask_fn(stroke_width) -> an L-mode mask of
    the glyphs grown by that stroke width. Sized for `max_sw`."""
    scratch = ImageDraw.Draw(Image.new("L", (4, 4)))

    if tracking:
        widths = [scratch.textlength(ch, font=font) for ch in text]
        total = sum(widths) + tracking * (len(text) - 1)
        asc, desc = font.getmetrics()
        W = int(math.ceil(total)) + 2 * max_sw + 4
        H = asc + desc + 2 * max_sw + 4
        ox, oy = max_sw + 2, max_sw + 2

        def _draw(md, sw):
            x = ox
            for ch, cw in zip(text, widths):
                md.text((x, oy), ch, font=font, fill=255, stroke_width=sw, stroke_fill=255)
                x += cw + tracking
    else:
        bbox = scratch.textbbox((0, 0), text, font=font, stroke_width=max_sw)
        W = bbox[2] - bbox[0] + 4
        H = bbox[3] - bbox[1] + 4
        ox, oy = -bbox[0] + 2, -bbox[1] + 2

        def _draw(md, sw):
            md.text((ox, oy), text, font=font, fill=255, stroke_width=sw, stroke_fill=255)

    def mask_fn(sw: int) -> Image.Image:
        m = Image.new("L", (W, H), 0)
        _draw(ImageDraw.Draw(m), sw)
        return m

    return W, H, mask_fn


def _paint(spec_val, w: int, h: int) -> Image.Image:
    """A fill/stroke spec -> an opaque RGBA paint image of size (w, h).

    Accepts a solid colour (`"#RRGGBB"` or `{"color": "#RRGGBB"}`) or a gradient
    (`{"from": ..., "to": ..., "angle": ...}`).
    """
    if isinstance(spec_val, str):
        return Image.new("RGBA", (w, h), _hex_rgba(spec_val))
    if "color" in spec_val:
        return Image.new("RGBA", (w, h), _hex_rgba(spec_val["color"]))
    return linear_gradient(w, h, spec_val["from"], spec_val["to"], spec_val.get("angle", 90))


def render_text_layer(text: str, spec: dict) -> Image.Image:
    """Build the RGBA text layer for a text spec.

    `fill` / `stroke` accept a solid colour or a gradient (see `_paint`).
    `stroke` may also be a *list* of stroke specs — each `width` is measured from
    the glyph edge; they paint widest-first, so `[{outer}, {inner}]` makes rings.
    """
    font = ImageFont.truetype(str(REPO_ROOT / spec["font"]), int(spec["size"]))
    tracking = int(spec.get("tracking", 0))

    strokes = spec.get("stroke") or []
    if isinstance(strokes, dict):
        strokes = [strokes]
    strokes = [s for s in strokes if int(s.get("width", 0)) > 0]
    max_sw = max((int(s["width"]) for s in strokes), default=0)

    W, H, mask_fn = _text_geometry(text, font, max_sw, tracking)

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for s in sorted(strokes, key=lambda s: -int(s["width"])):
        paint = _paint(s, W, H)
        paint.putalpha(mask_fn(int(s["width"])))
        layer.alpha_composite(paint)

    f = _paint(spec["fill"], W, H)
    f.putalpha(mask_fn(0))
    layer.alpha_composite(f)
    return layer


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

def _x_for(align: str, canvas_w: int, elem_w: int, x: int) -> int:
    if align == "center":            # centred on the canvas; x = nudge
        return (canvas_w - elem_w) // 2 + x
    if align == "center_at":         # centred on x (a column / half centre)
        return round(x - elem_w / 2)
    if align == "right":             # right edge x px from the canvas right
        return canvas_w - elem_w - x
    return x                         # left edge at x


def place_layer(canvas: Image.Image, layer: Image.Image, spec: dict) -> Box:
    px = _x_for(spec.get("align", "left"), canvas.width, layer.width, int(spec.get("x", 0)))
    py = int(spec.get("y", 0))
    canvas.alpha_composite(layer, (px, py))
    return (px, py, layer.width, layer.height)


def _scaled(path: str, scale: float) -> Image.Image:
    img = Image.open(REPO_ROOT / path).convert("RGBA")
    if scale != 1.0:
        img = img.resize((max(1, round(img.width * scale)),
                          max(1, round(img.height * scale))), Image.LANCZOS)
    return img


def place_image(canvas: Image.Image, spec: dict) -> Box:
    return place_layer(canvas, _scaled(spec["path"], float(spec.get("scale", 1.0))), spec)


def place_dia_pair(canvas: Image.Image, spec: dict) -> dict[str, Box]:
    """Two mascot images that mirror each other about the canvas centre:
    `x` is the LEFT image's left edge; the RIGHT image's right edge is the same
    distance from the canvas right edge. Both share `y` (and `scale`)."""
    s = float(spec.get("scale", 1.0))
    x, y = int(spec.get("x", 0)), int(spec.get("y", 0))
    limg = _scaled(spec["left_src"], s)
    rimg = _scaled(spec["right_src"], s)
    canvas.alpha_composite(limg, (x, y))
    rx = canvas.width - x - rimg.width
    canvas.alpha_composite(rimg, (rx, y))
    return {"dia_pair": (x, y, limg.width, limg.height),
            "dia_pair_right": (rx, y, rimg.width, rimg.height)}


ORDER_DIR = "assets/images/order"
ORDER_FILE = "utx_txt_order_{:02d}.png"


def place_order(canvas: Image.Image, spec: dict, groups: dict) -> Box:
    """Rank badge from assets/images/order, aspect preserved.

    Rendered height = the element's own `height` if set, otherwise its group's
    `height` (so a group's members stay matched)."""
    path = REPO_ROOT / ORDER_DIR / ORDER_FILE.format(int(spec["index"]))
    img = Image.open(path).convert("RGBA")
    target_h = int(spec.get("height")
                   or groups.get(spec.get("group", ""), {}).get("height", img.height))
    if target_h and target_h != img.height:
        s = target_h / img.height
        img = img.resize((max(1, round(img.width * s)), max(1, target_h)), Image.LANCZOS)
    return place_layer(canvas, img, spec)


def place_petit(canvas: Image.Image, spec: dict, src: str | None,
                eliminated: bool = False) -> Box | None:
    """Character petit/chibi at the end of a row. `src` (repo-relative path)
    comes from the render call — the layout only stores position/size. Aspect
    is preserved; `height` (or `scale`) sets the size. Returns None if no src.
    Eliminated members' petits are grayscaled and flipped upside-down."""
    if not src:
        return None
    img = Image.open(REPO_ROOT / src).convert("RGBA")
    h = spec.get("height")
    if h:
        s = int(h) / img.height
        img = img.resize((max(1, round(img.width * s)), max(1, int(h))), Image.LANCZOS)
    elif float(spec.get("scale", 1.0)) != 1.0:
        s = float(spec["scale"])
        img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))),
                         Image.LANCZOS)
    if eliminated:
        gray = ImageOps.grayscale(img)
        img = Image.merge("RGBA", (gray, gray, gray, img.getchannel("A")))
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    return place_layer(canvas, img, spec)


# ---------------------------------------------------------------------------
# Full render
# ---------------------------------------------------------------------------

def render(layout: dict, texts: dict[str, str] | None = None,
           petits: dict[str, str] | None = None,
           colors: dict[int, str] | None = None,
           eliminated: set[int] | None = None, *,
           guides: bool | None = None) -> tuple[Image.Image, dict[str, Box]]:
    """Return (RGBA image, {element_name: box}).

    `texts` supplies the string for each text element by name (e.g.
    {"month_text": "August 2026", "rank1_name": "Cluuto"}); a text element with
    no entry falls back to its spec's `"text"` key, then its own name.
    `petits` supplies the image path for each `petit` element by name; a petit
    with no entry is skipped.
    `colors` maps a rank number -> hex; `rank{N}_name` / `rank{N}_fans` are then
    repainted with `member_paint(N, colour, existing_stroke_width)`.
    `eliminated` is a set of rank numbers (20-30) whose monthly fans fell below
    the requirement: red HelpMe-font name/number, fan count -> "ELIMINATED",
    grayscaled upside-down petit, and a fat strikethrough across the row.
    """
    texts = texts or {}
    petits = petits or {}
    colors = colors or {}
    eliminated = eliminated or set()
    cv = layout["canvas"]
    canvas = Image.new("RGBA", (int(cv["width"]), int(cv["height"])), (0, 0, 0, 0))
    bg = cv.get("background")
    if bg:
        canvas = Image.new("RGBA", canvas.size, _hex_rgba(bg))

    boxes: dict[str, Box] = {}
    els = layout["elements"]
    groups = layout.get("order_groups", {})

    # `background` elements always paint first, whatever their dict position.
    ordered = sorted(els.items(), key=lambda kv: kv[1].get("type") != "background")
    for name, spec in ordered:
        etype = spec.get("type", "image")
        mm = _RANK_ANY_RE.match(name)
        rank = int(mm.group(1)) if mm else None
        kind = mm.group(2) if mm else None
        elim = rank in eliminated

        if etype == "text":
            if name in texts:
                content = texts[name]
            elif kind == "num" and f"rank{rank}_name" in texts:
                content = str(rank)          # rank number for a row that has a member
            elif kind in ("name", "fans", "num"):
                continue                     # no member for this row → draw nothing
            else:
                content = spec.get("text") or name
            if elim:
                spec = dict(spec)
                spec["fill"] = ELIM_RED
                if kind == "num":               # keep its font + white outline
                    content = str(rank)
                else:                           # name / fans -> HelpMe font, red stroke
                    spec["font"] = ELIM_FONT
                    sw = (spec.get("stroke") or {}).get("width", 4)
                    spec["stroke"] = {"width": sw, "color": ELIM_RED_DARK}
                if kind == "fans":
                    content = "ELIMINATED"
            elif kind in ("name", "fans") and colors.get(rank):
                sw = (spec.get("stroke") or {}).get("width", 4)
                spec = {**spec, **member_paint(rank, colors[rank], sw)}
            layer = render_text_layer(str(content), spec)
            boxes[name] = place_layer(canvas, layer, spec)
        elif etype == "order":
            boxes[name] = place_order(canvas, spec, groups)
        elif etype == "petit":
            box = place_petit(canvas, spec, petits.get(name), eliminated=elim)
            if box:
                boxes[name] = box
        elif etype == "dia_pair":
            boxes.update(place_dia_pair(canvas, spec))
        else:  # "image" and "background"
            boxes[name] = place_image(canvas, spec)

    _strike_eliminated(canvas, boxes, eliminated)

    show_guides = layout.get("guides", {}).get("show", False) if guides is None else guides
    if show_guides:
        _draw_guides(canvas, layout.get("guides", {}), boxes)

    return canvas, boxes


def _strike_eliminated(canvas: Image.Image, boxes: dict[str, Box],
                       eliminated: set[int]) -> None:
    """A big flat "X" (HelpMe-font slashes, stretched, no stroke) crossed over
    each eliminated member's row, from the rank number across to the petit.
    Kept wide (>= ELIM_X_MIN_RATIO : 1) and vertically centred so the red name
    stays readable above/below it."""
    x_glyph = render_text_layer("X", {"font": ELIM_FONT, "size": 200,
                                      "tracking": 0, "fill": ELIM_X})
    for rank in eliminated:
        row = [b for k in ("num", "name", "fans")
               if (b := boxes.get(f"rank{rank}_{k}"))]
        if not row:
            continue
        petit = boxes.get(f"rank{rank}_petit")
        x0 = min(b[0] for b in row)
        x1 = (petit[0] + petit[2]) if petit else max(b[0] + b[2] for b in row)
        y0 = min(b[1] for b in row)
        y1 = max(b[1] + b[3] for b in row)
        # overshoot the number / petit a little — the X glyph has edge padding.
        ov = round((x1 - x0) * ELIM_X_OVERSHOOT)
        x0 = max(2, x0 - ov)
        x1 = min(canvas.width - 2, x1 + ov)
        w = max(1, round(x1 - x0))
        h = max(1, min(round(y1 - y0) + 2 * round(ov * 0.4),
                       round(w / ELIM_X_MIN_RATIO)))
        y = round(y0 + ((y1 - y0) - h) / 2)
        canvas.alpha_composite(x_glyph.resize((w, h), Image.LANCZOS), (round(x0), y))


def _draw_guides(canvas: Image.Image, gcfg: dict, boxes: dict[str, Box]) -> None:
    d = ImageDraw.Draw(canvas)
    W, H = canvas.size
    if gcfg.get("center_line", True):
        d.line([(W // 2, 0), (W // 2, H)], fill=(0, 200, 255, 160), width=2)
    if gcfg.get("thirds", False):
        for fx in (1 / 3, 2 / 3):
            d.line([(int(W * fx), 0), (int(W * fx), H)], fill=(255, 255, 255, 60), width=1)
            d.line([(0, int(H * fx)), (W, int(H * fx))], fill=(255, 255, 255, 60), width=1)
    if gcfg.get("element_boxes", True):
        for name, (x, y, w, h) in boxes.items():
            d.rectangle([x, y, x + w - 1, y + h - 1], outline=(255, 0, 128, 220), width=2)
            label = f"{name}  x={x} y={y}  {w}x{h}"
            d.text((x + 4, max(0, y - 22)), label, fill=(255, 0, 128, 255))


# ---------------------------------------------------------------------------
# Preview compositing (transparent canvas -> checkerboard so it's visible)
# ---------------------------------------------------------------------------

def on_checkerboard(img: Image.Image, cell: int = 24) -> Image.Image:
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    d = ImageDraw.Draw(bg)
    for y in range(0, img.height, cell):
        for x in range(0, img.width, cell):
            if (x // cell + y // cell) % 2:
                d.rectangle([x, y, x + cell, y + cell], fill=(204, 204, 204, 255))
    bg.alpha_composite(img)
    return bg
