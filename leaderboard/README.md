# Monthly Fan Leaderboard image

`layout.json` holds every hand-tuned position/size and **is the source of truth**
— commit it so calibration survives between sessions.

## Files

**Committed (used by the bot):**

| file | |
|---|---|
| `layout.json` | the hand-tuned layout |
| `render.py` | `render(layout, texts, petits, colors) -> (RGBA image, boxes)` |
| `data.py` | live data: `top_members()`, `fan_character()`, `character_pool()` from `LOCAL_DB` + `TRAINEES_DB` |
| `build.py` | `build_monthly_image()` — ties data + render together |

**Calibration (in `tests/`, gitignored):**

| file | |
|---|---|
| `tests/leaderboard_calibrate.py` | entry point: GUI with no args, headless with flags |
| `tests/leaderboard_editor.py` | the visual (Tkinter) editor |
| `tests/leaderboard_sampledata.py` | sample text/petit/colour, from `tests/tonberries.db` (random fan character when link tables are absent) |
| `tests/leaderboard_calibration.png` / `_final.png` | generated previews |

## Bot command

`render monthly` (owner DM) → `build_monthly_image()` → PNG DM'd back. Renders
the **current month** from `circle_member_snapshots`. Needs `data/trainees.db`
populated (`trainee refresh`) for petits/colours; unlinked members / members with
no fan role render in a neutral colour with no petit.

## Visual editor

```bash
python tests/leaderboard_calibrate.py
```

- **Pick** an element from the dropdown, or **click** one on the canvas; **drag** to move.
- **Arrow keys** nudge 1px (**Shift** = 10px).
- Dragging **snaps** (green line) to the canvas centre *and* to other elements'
  left / centre / right and top / middle / bottom edges — that's how the rank
  badges line up with each other.
- **Center H / Center V / Center both** snap the selected element to the canvas centre.
- `align` dropdown switches left/center/right while keeping the element in place.
- Per-type fields: images have `scale`; text has `size`, `tracking`, `stroke_width`
  (+ `fill_angle` / `stroke_angle` when that paint is a gradient); order badges
  have either their own `height` (badges 00–02, sized independently) or a
  `group_height` (badges 03–09 share `order_groups.rest.height`).
- Text/number strings for calibration come from `tests/leaderboard_sampledata.py`
  (top 3 by monthly fans, read from `tests/tonberries.db`). Edit `month` to
  preview a different label; toggle `guides`.
- **Save** (or Ctrl+S) writes `layout.json`.

## Headless

```bash
python tests/leaderboard_calibrate.py --render            # calibration.png (guides)
python tests/leaderboard_calibrate.py --final             # transparent, no guides -> final.png
python tests/leaderboard_calibrate.py --month "September 2026"
python tests/leaderboard_calibrate.py --set month_text.y=560 --set header_image.scale=1.3
```

`--set KEY=VALUE` writes back to `layout.json` (values coerced to int/float/bool/null
when they look like one). Dotted keys index into `elements`, e.g.
`month_text.fill.from`, `month_text.stroke.width`. You can also just edit the JSON.

## Layout schema

```jsonc
{
  "canvas": { "width": 1800, "height": 2400, "background": null },  // null = transparent
  "elements": {
    "header_image": {
      "type": "image",
      "path": "assets/images/Fan Leaderboard.png",
      "align": "center",   // center | left | right
      "x": 0,              // center: nudge from centre;  left: left edge;  right: gap from right edge
      "y": 60,             // top edge
      "scale": 1.0
    },
    "month_text": {
      "type": "text",
      "font": "fonts/FOT-UDKakugo C80 Pro.ttf",
      "size": 220,
      "align": "center", "x": 0, "y": 540,
      "tracking": 0,       // extra px between letters
      "fill":   { "from": "#FFD6D1", "to": "#EF3D40", "angle": 90 },  // 90 = top->bottom
      "stroke": { "width": 7, "from": "#FFE803", "to": "#7C3816", "angle": 90 }
    },

    "order_00": { "type": "order", "index": 0, "height": 150, "align": "left", "x": 90, "y": 900 },
    // 00-02 each carry their own "height";  03-09 use "group": "rest"
    "order_09": { "type": "order", "index": 9, "group": "rest", "align": "left", "x": 90, "y": 2250 },

    "rank1_name": { "type": "text", "font": "...", "size": 72, "align": "left",  "x": 330, "y": 930,
                    "text": "Player 1", "fill": "#FFFFFF", "stroke": { "width": 6, "color": "#000000" } },
    "rank1_fans": { "type": "text", "font": "...", "size": 64, "align": "right", "x": 120, "y": 940,
                    "text": "000,000,000", "fill": "#FFFFFF", "stroke": { "width": 6, "color": "#000000" } }
    // ...rank2_*, rank3_*
  },

  "order_groups": { "rest": { "height": 120 } },   // shared height for badges 03-09

  "guides": { "show": true, "center_line": true, "thirds": true, "element_boxes": true }
}
```

- `type: "background"` (e.g. `confetti`) always paints first, whatever its
  position in `elements`.
- `type: "dia_pair"` renders two mascot images that mirror about the canvas
  centre: `x` = the **left** image's left edge; the right image's right edge is
  the same distance from the canvas right edge; both share `y` / `scale`
  (`left_src` / `right_src` are the two files). Dragging either one in the editor
  moves the pair — horizontal mirrored, vertical synced.
- `order` elements load `assets/images/order/utx_txt_order_{index:02d}.png`,
  scaled (aspect kept) to `height` or the group's `height`.
- `fill` / `stroke` accept a solid colour (`"#RRGGBB"` or `{"color": "#RRGGBB"}`)
  or a gradient (`{"from": ..., "to": ..., "angle": ...}`).
- text content: `render(layout, texts={name: string})`; missing entries fall back
  to the spec's `"text"` key.

## Layout (30 members, 2400×3150)

- **1st** — badge+name+fans unit centred on the canvas (x 1200).
- **2nd / 3rd** — each unit centred on its half (x 600 / 1800).
- **4th–30th** — three columns, content centred on the column centre
  (x 400 / 1200 / 2000):
  - col 1 = ranks 4–10 (`rest` group 着 badge + name + fans, larger)
  - col 2 = ranks 11–20, col 3 = ranks 21–30 — a big brown-gradient rank number
    (`rank{N}_num`, brown→light-brown fill + white outline, echoing the 6th–10th
    badges) with the name + fan count **left-aligned** beside it
- Every row ends with `rank{N}_petit` — the character petit/chibi of the member's
  fan. Layout stores position + `height` only (aspect kept); the image path is
  passed to `render(layout, texts, petits)`.
- **Colour** — `rank{N}_name` / `rank{N}_fans` are repainted from `colors[N]`
  (the member's fan character's hex, `member_paint()`):
  - ranks 1–3: slight top-lighter fill gradient; outline = a thin **black ring**
    inside a fixed tier stroke gradient (gold `#894900→#FFC521` /
    silver `#5D527E→#B2CBF1` / bronze `#7B4A39→#EFAF6B`)
  - ranks 4–10: solid fill + a much darker solid stroke (`darken(color, 0.72)`)
  - ranks 11–30: same, but the stroke width is scaled to `0.7×`
  - no linked Discord account **or no fan role** → neutral silvery colour, no petit
- Petit selection skips the generic default costume (`costume_id == '000101'`).
- `stroke` in a text spec may be a **list** of stroke specs — each `width` is
  measured from the glyph edge and they paint widest-first, so `[{outer},{inner}]`
  makes concentric rings.

`align: "center_at"` centres an element on its `x` (used for the column text).
The editor snaps drags to x = ¼, ⅓, ½, ⅔, ¾ of the canvas (600 / 800 / 1200 /
1600 / 1800), so column-centring is a drag away.

`tests/leaderboard_sampledata.py`:
- `sample_texts` — 30 name/fans from `circle_member_snapshots`
- `sample_petits` / `sample_colors` — each member's fan character drives **both**
  their petit image and their colour. Live: trainer → linked Discord user →
  **most recent** fan role → character (no link / no role → neutral colour, no
  petit). Testing (link tables absent): a random-but-stable character each.
- `fan_character(trainer)` is what the bot uses live

## Status

- [x] canvas 2400×3150, transparent
- [x] `confetti` background layer + `dia_pair` mirrored mascots
- [x] `header_image`, `month_text`
- [x] podium 1–3 — balanced, consistent name/fans/badge ratios
- [x] left column 4–10 — badge + name + fans
- [x] ranks 11–30 — brown rank number + left-aligned name/fans (cols 2 & 3)
- [x] per-row fan petit image (`rank{N}_petit`); random for now, `fan_petit` live
- [ ] background
- [ ] wire into the bot (end-of-month, before daily reset) — build `texts`/`petits`
      from live circle + link data, call `render(..., guides=False)`
