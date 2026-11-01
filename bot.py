import os
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# Imported after load_dotenv(): it pulls in global_config, which reads .env values.
from moderation_module import ModerationTree  # noqa: E402

os.makedirs("logs", exist_ok=True)

_file_handler = logging.FileHandler("logs/tonberries.log", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)

logger = logging.getLogger("tonberries")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # required for fan-role cleanup (empty-role detection)

# ModerationTree answers blacklisted / timed-out users on every slash command and
# autocomplete (see moderation_module.py).
bot = commands.Bot(command_prefix="\x00", intents=intents, help_command=None, tree_cls=ModerationTree)

token = os.getenv("DISCORD_TOKEN")
