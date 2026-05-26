# Tonberries-Bot Configuration
# Fill in OWNER_USER_ID, MAIN_SERVER_ID, and all channel IDs before first run.

OWNER_USER_IDS          = (680653908259110914, 0)  # Discord user IDs allowed to DM-command the bot
MAIN_SERVER_ID          = 0  # Discord guild (server) ID for the Tonberries server

ONGOING_CHANNEL_ID      = 0  # Channel for currently active UMA events
UPCOMING_CHANNEL_ID     = 0  # Channel for upcoming UMA events
NOTIFICATION_CHANNEL_ID = 0  # Channel where notification messages are posted

# Absolute path to Gacha-Timer-Bot on the same Pi
GACHA_BOT_DIR = "/home/piberry/Gacha-Timer-Bot"

# Read-only access to Gacha-Timer-Bot's databases
SHARED_EVENTS_DB      = f"{GACHA_BOT_DIR}/data/uma_musume_data.db"
SHARED_NOTIF_DB       = f"{GACHA_BOT_DIR}/data/notification_data.db"
# File written by Gacha-Timer-Bot's scraper; used as a refresh signal
SCRAPER_LAST_RUN_FILE = f"{GACHA_BOT_DIR}/data/scraper_last_run.txt"

# Tonberries-Bot's own database (event message IDs + notification schedule)
LOCAL_DB = "data/tonberries.db"

# Circles scraper
CIRCLE_ID  = "0"         # uma.moe circle ID for Tonberries
CIRCLES_DB = "data/circles.db"  # written by circles_scraper.py
