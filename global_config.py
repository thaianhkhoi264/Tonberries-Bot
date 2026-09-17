import os
from datetime import datetime, timedelta, timezone

from local_config import (
    OWNER_USER_IDS,
    MAIN_OWNER_ID,
    MAIN_SERVER_ID,
    ONGOING_CHANNEL_ID,
    UPCOMING_CHANNEL_ID,
    NOTIFICATION_CHANNEL_ID,
    GENERAL_CHANNEL_ID,
    CIRCLE_ID,
    CIRCLE_CHANNEL_ID,
)

# Tonberries-Bot Configuration
#
# Discord/uma.moe IDs (owner IDs, server ID, channel IDs, circle ID) live in
# local_config.py, which is gitignored and never committed. Copy
# local_config.example.py to local_config.py and fill in real values to run
# the bot. Everything else (paths, non-sensitive settings) stays here.

# The Umamusume global server's daily reset is 15:00 UTC. Fan counts (daily and
# monthly) roll over then, not at the UTC calendar midnight — so every "what day
# / month is it" decision in the circle code is keyed to this shifted clock.
GAME_RESET_UTC_HOUR = 15


def game_now() -> datetime:
    """`datetime.now(UTC)` shifted back into the current in-game day.

    Between 00:00 and 15:00 UTC this still returns "yesterday"; at/after 15:00
    UTC it flips to the new game day. Use `.year` / `.month` / `.day` / `.date()`
    off this for buckets; keep real `datetime.now(timezone.utc)` for timestamps.
    """
    return datetime.now(timezone.utc) - timedelta(hours=GAME_RESET_UTC_HOUR)

# Absolute path to Gacha-Timer-Bot on the same Pi
GACHA_BOT_DIR = "/home/piberry/Gacha-Timer-Bot"

# Read-only access to Gacha-Timer-Bot's databases
SHARED_EVENTS_DB      = f"{GACHA_BOT_DIR}/data/uma_musume_data.db"
SHARED_NOTIF_DB       = f"{GACHA_BOT_DIR}/data/notification_data.db"
SHARED_GAMETORA_DB    = f"{GACHA_BOT_DIR}/data/JP_Data/uma_jp_data.db"  # support_cards + characters
# File written by Gacha-Timer-Bot's scraper; used as a refresh signal
SCRAPER_LAST_RUN_FILE = f"{GACHA_BOT_DIR}/data/scraper_last_run.txt"

# Tonberries-Bot's own database (event message IDs + notification schedule)
LOCAL_DB = "data/tonberries.db"

# Circles — uma.moe API
UMA_MOE_API_KEY   = os.getenv("UMA_MOE_API_KEY", "")  # set in .env

# Skills scraper (GameTora)
SKILLS_DB = "data/skills.db"  # written by skills_scraper.py

# uma-skill-tools / uma-tools cached JSON files (written by skill_sync.py)
UMA_TOOLS_DIR    = "data/uma_tools"
SKILL_DATA_JSON    = f"{UMA_TOOLS_DIR}/skill_data.json"
COURSE_DATA_JSON   = f"{UMA_TOOLS_DIR}/course_data.json"    # uma-skill-tools (geometry)
COURSE_LABELS_JSON = f"{UMA_TOOLS_DIR}/course_labels.json"  # uma-tools (inner/outer labels)
TRACK_NAMES_JSON   = f"{UMA_TOOLS_DIR}/tracknames.json"
SKILL_NAMES_JSON   = f"{UMA_TOOLS_DIR}/skillnames.json"
GT_GLOBAL_CHARS_JSON = "data/gt_global_chars.json"  # GameTora visible-only character list (refreshed weekly)

# Trainee / petit-image scraper (umamusu.wiki + Fandom fallback).
# Separate DB from LOCAL_DB; written by build_trainee_data.py (the `trainee refresh` command).
TRAINEES_DB               = "data/trainees.db"
PETIT_IMAGE_DIR           = "data/petit_images"             # umamusu.wiki _0011 chibis
PETIT_IMAGE_FANDOM_DIR    = "data/petit_images_fandom"      # Fandom gap-fills
PETIT_IMAGE_NORMALIZED_DIR = "data/petit_images_normalized" # alpha-trimmed, both sources
