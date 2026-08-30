"""Monthly Fan Leaderboard image.

Committed (used by the bot):
    render.py    — the renderer
    data.py      — live data (circle snapshots + fan-role links + petit images)
    build.py     — build_monthly_image() ties data + render together
    layout.json  — the hand-tuned layout; single source of truth

Calibration tools live in `tests/` (leaderboard_calibrate.py / _editor.py /
_sampledata.py) and edit `layout.json` in place.

The bot renders via the `render monthly` owner command (see main.py).
"""
