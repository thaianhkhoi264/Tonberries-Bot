import sys
import asyncio

import discord

from bot import bot, token, logger
from global_config import OWNER_USER_IDS
import uma_module
import notification_module


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    logger.info(f"[Bot] Logged in as {bot.user} (ID: {bot.user.id})")
    await uma_module.init_local_db()
    await uma_module.start_background_tasks()


@bot.event
async def on_message(message: discord.Message):
    # Only handle DMs from the owner
    if message.guild is not None:
        return
    if message.author.id not in OWNER_USER_IDS:
        return
    if message.author.bot:
        return

    cmd = message.content.strip().lower()

    if cmd == "help":
        await _cmd_help(message)
    elif cmd == "refresh":
        await _cmd_refresh(message)
    elif cmd == "pending":
        await _cmd_pending(message)
    elif cmd == "restart":
        await _cmd_restart(message)
    # Unknown commands are silently ignored


# ---------------------------------------------------------------------------
# DM command handlers
# ---------------------------------------------------------------------------

async def _cmd_help(message: discord.Message):
    text = (
        "**Tonberries-Bot commands** (DM only)\n\n"
        "`help` — Show this message\n"
        "`refresh` — Force-refresh the ongoing/upcoming event channels\n"
        "`pending` — List all notifications scheduled in the next 3 days\n"
        "`restart` — Restart the bot (handled by systemd)"
    )
    await message.channel.send(text)


async def _cmd_refresh(message: discord.Message):
    await message.channel.send("Refreshing dashboards…")
    try:
        await uma_module.uma_update_timers(force_update=True)
        await message.channel.send("Dashboards refreshed.")
    except Exception as exc:
        logger.error(f"[Bot] Refresh failed: {exc}")
        await message.channel.send(f"Refresh failed: {exc}")


async def _cmd_pending(message: discord.Message):
    try:
        text = await notification_module.get_pending_text()
        # Discord messages cap at 2000 chars; split if needed
        if len(text) <= 2000:
            await message.channel.send(text)
        else:
            chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
            for chunk in chunks:
                await message.channel.send(chunk)
    except Exception as exc:
        logger.error(f"[Bot] Pending command failed: {exc}")
        await message.channel.send(f"Error fetching pending notifications: {exc}")


async def _cmd_restart(message: discord.Message):
    await message.channel.send("Restarting…")
    logger.info("[Bot] Restart requested by owner")
    await bot.close()
    sys.exit(1)  # Non-zero exit triggers systemd Restart=on-failure


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not token:
        logger.critical("DISCORD_TOKEN not set in .env — cannot start")
        sys.exit(1)
    bot.run(token)
