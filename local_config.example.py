# Template for local_config.py.
#
# Copy this file to local_config.py and fill in the real IDs. local_config.py
# is gitignored and never committed — global_config.py imports the names below
# from it. See CLAUDE.md for the split between this file and global_config.py.

OWNER_USER_IDS          = (0, 0)  # Discord user IDs allowed to DM-command the bot
MAIN_OWNER_ID           = 0       # Primary owner — receives restart DMs, exclusive hitlist access
MAIN_SERVER_ID          = 0       # Discord guild (server) ID for the Tonberries server

ONGOING_CHANNEL_ID      = 0  # Channel for currently active UMA events
UPCOMING_CHANNEL_ID     = 0  # Channel for upcoming UMA events
NOTIFICATION_CHANNEL_ID = 0  # Channel where notification messages are posted

GENERAL_CHANNEL_ID = 0  # uma-chat-v2 channel

# Circles — uma.moe API
CIRCLE_ID         = ""  # uma.moe circle ID for Tonberries
CIRCLE_CHANNEL_ID = 0   # Channel where circle stats are posted
