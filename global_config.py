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
    TRAINING_SERVER_ID,
    TRAINING_DASHBOARD_CHANNEL_ID,
    TRAINING_MANUAL_CHANNEL_ID,
    TRAINING_INDEPENDENT_CHANNEL_ID,
    TRAINING_ABANDONED_CHANNEL_ID,
    TRAINING_USER_DESCRIPTION_TO_ID,
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

# Skill icon emojis (built by tests/build_emoji_mapping.py, tracked in git — unlike the
# Pi-only data/ files above). {"skill": {skillId: "<:utx_ico_skill_NNNNN:realId>", ...}}
EMOJI_MAPPING_JSON = "emoji_mapping.json"

# Trainee / petit-image scraper (umamusu.wiki + Fandom fallback).
# Separate DB from LOCAL_DB; written by build_trainee_data.py (the `trainee refresh` command).
TRAINEES_DB               = "data/trainees.db"
PETIT_IMAGE_DIR           = "data/petit_images"             # umamusu.wiki _0011 chibis
PETIT_IMAGE_FANDOM_DIR    = "data/petit_images_fandom"      # Fandom gap-fills
PETIT_IMAGE_NORMALIZED_DIR = "data/petit_images_normalized" # alpha-trimmed, both sources

# --- HorseACT training-event API (horseact_network_probe integration) ---

# REST API server (api_server.py), mirroring Gacha-Timer-Bot's setup on the same Pi
# but on a different port (Gacha-Timer-Bot already owns 8080).
API_ENABLED = os.getenv("API_ENABLED", "true").lower() == "true"
API_HOST    = os.getenv("API_HOST", "0.0.0.0")
API_PORT    = int(os.getenv("API_PORT", "8081"))

# factors.json / character.json / character_names.json from uma.moe (global game data),
# refreshed daily by training_data_sync.py. Skill name data reuses skill_sync.py's
# existing SKILL_DATA_JSON / SKILL_NAMES_JSON above rather than duplicating that fetch.
TRAINING_DATA_DIR             = "data/training"
TRAINING_FACTORS_JSON         = f"{TRAINING_DATA_DIR}/factors.json"
TRAINING_CHARACTERS_JSON      = f"{TRAINING_DATA_DIR}/character.json"
TRAINING_CHARACTER_NAMES_JSON = f"{TRAINING_DATA_DIR}/character_names.json"

# Support-card chain-event progress (story_id -> position/total in its chain), from
# tests/extract_chain_progress.py run locally against a real game client's master.mdb.
# No public sync source (unlike the three above) — refresh by hand when needed.
TRAINING_CHAIN_PROGRESS_JSON  = f"{TRAINING_DATA_DIR}/chain_progress.json"

# All 269 Live-scenario "squares" (title / effect / performance-point cost per square),
# extracted from master_global.mdb — copied from the HorseACT RE plan folder's
# live_squares.json. Lives at the repo root, tracked in git like emoji_mapping.json
# (unlike the gitignored data/ files above), so a normal push/pull deploys it. Used for
# the Live Show embed's "Shop" field (the 3 squares currently on offer).
TRAINING_LIVE_SQUARES_JSON    = "live_squares.json"

# cardId -> dressId for every card (from tests/extract_card_dress_ids.py). uma-tools names
# its trained-character icons by the outfit's dressId, which differs from the cardId the
# API sends for ~46% of cards (e.g. 103802 -> 103826). Tracked at the repo root like
# live_squares.json, so a normal push/pull deploys it.
TRAINING_CARD_DRESS_IDS_JSON  = "card_dress_ids.json"

# Trained-character portrait PNGs from the local uma-tools clone (kept fresh by its
# own daily cron `git pull`, same source as the emoji icons above) — used as the
# training embeds' thumbnail. Referenced directly, not copied, so it stays in sync
# for free. Filename convention: trained_chr_icon_<baseId>_<dressId>_02.png (dressId via
# TRAINING_CARD_DRESS_IDS_JSON, not always the cardId).
UMA_TOOLS_CHARA_ICON_DIR = "/home/piberry/uma-tools/icons/chara"

# Support-card art (256x256 PNGs), same uma-tools clone — used to build the
# training_end result embeds' `image` (the 6-card horizontal strip), from the
# payload's supportCards array. Filename convention: support_card_s_<supportCardId>.png.
UMA_TOOLS_SUPPORT_ICON_DIR = "/home/piberry/uma-tools/icons/support"

# uma-tools' own card-browser data (its build-planner tool) — the only source found so
# far for a supportCardId -> training type (Speed/Stamina/Power/Guts/Wit/Pal/Group)
# mapping; not training-event-specific, just uma-tools' full card list.
UMA_TOOLS_BUILD_PLANNER_CARDS_JSON = "/home/piberry/uma-tools/build-planner/cards.json"

# Same clone's top-level icon set (not icons/support or icons/chara) — holds the
# training-type badges (utx_ico_obtain_00..06.png, Speed..Group) stamped onto each
# support card in the training embeds' support-card strip.
UMA_TOOLS_ICON_DIR = "/home/piberry/uma-tools/icons"
