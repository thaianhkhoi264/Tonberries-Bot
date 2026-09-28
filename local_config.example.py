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

# HorseACT training-event API (horseact_network_probe integration)
# Lives in its own separate Discord server, not MAIN_SERVER_ID.
TRAINING_SERVER_ID               = 0  # Guild ID for the HorseACT server
TRAINING_DASHBOARD_CHANNEL_ID    = 0  # Live per-user training status (ongoing / waiting for it to end)
TRAINING_MANUAL_CHANNEL_ID       = 0  # Finished manual-training results (name, rating, stats, skills)
TRAINING_INDEPENDENT_CHANNEL_ID  = 0  # Finished independent-training results (name, factors)

# Maps api_keys.json descriptions -> Discord user IDs, same pattern as Gacha-Timer-Bot's
# USER_DESCRIPTION_TO_ID. horseact_network_probe issues its own per-user API key; add an
# entry here (and a matching key in api_keys.json) for each person running the plugin.
TRAINING_USER_DESCRIPTION_TO_ID = {
    # "SomeUser": 0,
}
