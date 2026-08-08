import os

# Tonberries-Bot Configuration

OWNER_USER_IDS          = (680653908259110914, 320398342830030849)  # Discord user IDs allowed to DM-command the bot
MAIN_SERVER_ID          = 1415758478198439938  # Discord guild (server) ID for the Tonberries server

ONGOING_CHANNEL_ID      = 1504632800203247798  # Channel for currently active UMA events
UPCOMING_CHANNEL_ID     = 1504632621085229156  # Channel for upcoming UMA events
NOTIFICATION_CHANNEL_ID = 1504632023946625054  # Channel where notification messages are posted

GENERAL_CHANNEL_ID = 1464811661293654274  # uma-chat-v2 channel

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
CIRCLE_ID         = "532127760"          # uma.moe circle ID for Tonberries
CIRCLE_CHANNEL_ID = 1508622405357015190  # Channel where circle stats are posted
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
