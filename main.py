import sys
import asyncio
import random
import re

import discord
from discord import app_commands

from bot import bot, token, logger
from global_config import OWNER_USER_IDS, MAIN_OWNER_ID, MAIN_SERVER_ID
import uma_module
import notification_module
import circles_module
import skills_module
import autotrain_module
import lookup_module
import skill_sync
import cm_module
import parent_module
import role_module
import players_module


# ---------------------------------------------------------------------------
# Manual hitlist state
# ---------------------------------------------------------------------------

# name → active removal Task (cancelled and replaced if the same name is re-added)
_manual_hitlist: dict[str, asyncio.Task] = {}

_HITLIST_RE = re.compile(
    r"^dia add (.+?) to the hitlist(?: for (.+))?$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="skill", description="Look up an Uma Musume skill")
@app_commands.describe(
    name="Skill name to search for",
    course="CM or racecourse to visualise on (defaults to current/next CM)",
    length="Course variant / distance (only for venue selections)",
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def _slash_skill(
    interaction: discord.Interaction,
    name: str,
    course: str | None = None,
    length: str | None = None,
):
    await skills_module.handle_skill_interaction(interaction, name, course, length)


@_slash_skill.autocomplete("name")
async def _slash_skill_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await skills_module.autocomplete_skills(interaction, current)


@_slash_skill.autocomplete("course")
async def _slash_skill_course_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await cm_module.autocomplete_course(interaction, current)


@_slash_skill.autocomplete("length")
async def _slash_skill_length_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    course_value = interaction.namespace.course or ""
    return await cm_module.autocomplete_length(interaction, current, course_value)


@bot.tree.command(name="parent", description="Show recommended inherited skills for a CM")
@app_commands.describe(cm="CM number (defaults to active/next CM)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def _slash_parent(interaction: discord.Interaction, cm: str | None = None):
    await parent_module.handle_parent_interaction(interaction, cm)


@_slash_parent.autocomplete("cm")
async def _slash_parent_cm_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await parent_module.autocomplete_cm(interaction, current)


@bot.tree.command(name="whenis", description="Look up when a support card or character will appear on a banner")
@app_commands.describe(name="Character or support card name")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def _slash_whenis(interaction: discord.Interaction, name: str):
    await lookup_module.handle_whenis(interaction, name)


@_slash_whenis.autocomplete("name")
async def _slash_whenis_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await lookup_module.autocomplete_whenis(interaction, current)


@bot.tree.command(name="role", description="Get a character fan role (Tonberries server only)")
@app_commands.describe(character="Character name — 'Fan' is added automatically")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def _slash_role(interaction: discord.Interaction, character: str):
    await role_module.handle_role(interaction, character)


@_slash_role.autocomplete("character")
async def _slash_role_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await role_module.autocomplete_role(interaction, current)


@bot.tree.command(name="removerole", description="Remove one of your character fan roles (Tonberries server only)")
@app_commands.describe(character="Fan role to remove")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def _slash_removerole(interaction: discord.Interaction, character: str):
    await role_module.handle_removerole(interaction, character)


@_slash_removerole.autocomplete("character")
async def _slash_removerole_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await role_module.autocomplete_removerole(interaction, current)


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    await lookup_module.handle_reaction(payload)


@bot.event
async def on_ready():
    logger.info(f"[Bot] Logged in as {bot.user} (ID: {bot.user.id})")
    await uma_module.init_local_db()
    await uma_module.start_background_tasks()
    await circles_module.start_background_task()
    await autotrain_module.restore_timers()
    await bot.tree.sync()
    logger.info("[Bot] Slash commands synced")
    await skill_sync.sync_if_stale(bot)
    cm_module.load_local_data_if_needed()
    skills_module.load_uma_data()
    parent_module.load_parent_data()
    await parent_module.start_background_tasks()
    await role_module.init_db()
    await players_module.init_db()
    role_module.load_character_cache(force=True)
    _tonberries = bot.get_guild(MAIN_SERVER_ID)
    if _tonberries:
        await role_module.sync_existing_fan_roles(_tonberries)
    await role_module.start_background_tasks()
    await _send_restart_dm()


async def _send_restart_dm() -> None:
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h — %s (%cr)"],
            capture_output=True, text=True,
        )
        commit = result.stdout.strip() or "unknown"
    except Exception:
        commit = "unavailable"
    try:
        user = await bot.fetch_user(MAIN_OWNER_ID)
        await user.send(f"Bot restarted.\nLatest commit: `{commit}`")
    except Exception as exc:
        logger.error(f"[Bot] Failed to send restart DM: {exc}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.guild is not None:
        await _handle_guild_message(message)
        return

    cmd       = message.content.strip()
    cmd_lower = cmd.lower()

    # Public commands — available to any user in DMs
    if cmd_lower.startswith("autotrain "):
        await autotrain_module.handle_auto(message, cmd[len("autotrain "):])
        return
    if cmd_lower == "autotrain":
        await autotrain_module.handle_auto(message, "")
        return
    if cmd_lower.startswith("auto "):
        await autotrain_module.handle_auto(message, cmd[len("auto "):])
        return
    if cmd_lower == "auto":
        await autotrain_module.handle_auto(message, "")
        return
    if cmd_lower == "renew":
        await autotrain_module.handle_renew(message)
        return
    if cmd_lower == "end":
        await autotrain_module.handle_end(message)
        return

    # Everything below is owner-only — send a helpful reply to everyone else
    if message.author.id not in OWNER_USER_IDS:
        await message.channel.send(
            "Hello! Whatever you're typing is not supported, use `auto` to start an Independent Training reminder. <a:dianod:1508662343322697839>"
        )
        return

    if cmd_lower == "help":
        await _cmd_help(message)
    elif cmd_lower == "refresh":
        await _cmd_refresh(message)
    elif cmd_lower == "pending":
        await _cmd_pending(message)
    elif cmd_lower == "restart":
        await _cmd_restart(message)
    elif cmd_lower == "shutdown":
        await _cmd_shutdown(message)
    elif cmd_lower == "circles":
        await _cmd_circles(message)
    elif cmd_lower == "circle run":
        await _cmd_circle_run(message)
    elif cmd_lower == "report":
        await _cmd_report(message)
    elif cmd_lower in ("shaming on", "shaming off"):
        await _cmd_shaming(message, cmd_lower == "shaming on")
    elif cmd_lower == "fancount":
        await _cmd_fancount(message)
    elif cmd_lower.startswith("fancount "):
        await _cmd_fancount(message, cmd[len("fancount "):])
    elif cmd_lower == "parent refresh":
        await _cmd_parent_refresh(message)
    elif cmd_lower == "trainee refresh":
        await _cmd_trainee_refresh(message)
    elif cmd_lower in ("role cleanup", "role cleanup now"):
        await _cmd_role_cleanup(message, force=cmd_lower.endswith(" now"))
    elif cmd_lower == "unlink" or cmd_lower.startswith("unlink "):
        await players_module.handle_unlink_command(message, cmd[len("unlink"):].strip())
    elif cmd_lower == "link" or cmd_lower.startswith("link "):
        await players_module.handle_link_command(message, cmd[len("link"):].strip())
    elif cmd_lower == "render monthly":
        await _cmd_render_monthly(message)
    elif cmd_lower == "skill sync":
        await _cmd_skill_sync(message)
    elif cmd_lower == "skill refresh":
        await _cmd_skill_refresh(message)
    elif cmd_lower.startswith("skill "):
        await skills_module.handle_skill_lookup(message, cmd[len("skill "):])
    elif cmd_lower.startswith("send "):
        await _cmd_send(message, cmd[len("send "):])
    # Unknown commands are silently ignored


# ---------------------------------------------------------------------------
# DM command handlers
# ---------------------------------------------------------------------------

async def _cmd_help(message: discord.Message):
    text = (
        "**Dia's commands!**\n\n"
        "`help` — Show this message\n"
        "`refresh` — Force-refresh the ongoing/upcoming event channels\n"
        "`pending` — List all notifications scheduled in the next 3 days\n"
        "`circles` — Force-refresh the club circle stats\n"
        "`circle run` — Run the full daily routine now (snapshot + report + monthly leaderboard)\n"
        "`report` — Force-send the daily fan report to you\n"
        "`shaming on/off` — Toggle posting the daily report to uma-chat-v2\n"
        "`fancount` — Show current monthly fan requirement\n"
        "`fancount edit <number>` — Change the monthly fan requirement and refresh the channel\n"
        "`skill <name>` — Look up a skill (e.g. `skill Red Shift`)\n"
        "`parent refresh` — Re-scrape the GT global character list\n"
        "`trainee refresh` — Re-scrape trainee data (umamusu + Fandom gaps) and normalize images\n"
        "`role cleanup` — Preview empty fan roles (add `now` to delete them)\n"
        "`link` — Link in-game trainers to Discord users (`link list`/`import`/`export`, `link <@user> <name>`, `unlink <name>`)\n"
        "`render monthly` — Render + DM the Fan Leaderboard image "
        "(current in-game month, or the last finished month if it has no data yet)\n"
        "`skill sync` — Force-download uma-skill-tools data from GitHub\n"
        "`skill refresh` — Re-scrape all skills from GameTora\n"
        "`restart` — Restart Dia :(\n"
        "`shutdown` — Stop Dia in case she spammed..."
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


async def _cmd_shutdown(message: discord.Message):
    await message.channel.send("Shutting down. Use `sudo systemctl start tonberries-bot` to bring me back.")
    logger.info("[Bot] Shutdown requested by owner")
    await bot.close()
    sys.exit(0)  # Exit code 0 — systemd Restart=on-failure will NOT restart


async def _cmd_report(message: discord.Message):
    await message.channel.send("Building fan report…")
    try:
        await circles_module.send_daily_report([message.author.id])
        await message.channel.send("Report sent.")
    except Exception as exc:
        logger.error(f"[Bot] Report failed: {exc}")
        await message.channel.send(f"Report failed: {exc}")


async def _cmd_shaming(message: discord.Message, enable: bool) -> None:
    await circles_module.set_shaming_enabled(enable)
    state = "on" if enable else "off"
    await message.channel.send(f"Public shaming turned **{state}**.")


async def _cmd_fancount(message: discord.Message, args: str = "") -> None:
    args = args.strip()
    if not args:
        req = await circles_module.get_monthly_requirement()
        val = req["value"]
        changed_at = req.get("changed_at")
        changed_by = req.get("changed_by")
        lines = [f"Monthly fan requirement: **{val:,}**"]
        if changed_at:
            try:
                from datetime import datetime
                ts = int(datetime.fromisoformat(changed_at).timestamp())
                lines.append(f"Last changed: <t:{ts}:F>")
            except Exception:
                pass
        if changed_by:
            lines.append(f"Changed by: <@{changed_by}>")
        lines.append("\nUsage: `fancount edit <number>`")
        await message.channel.send("\n".join(lines))
        return

    parts = args.split()
    if parts[0].lower() != "edit" or len(parts) < 2:
        await message.channel.send("Usage: `fancount` or `fancount edit <number>`")
        return

    try:
        new_val = int(parts[1].replace(",", "").replace("_", ""))
    except ValueError:
        await message.channel.send(f"Invalid number: `{parts[1]}`")
        return

    if new_val <= 0:
        await message.channel.send("Fan requirement must be a positive number.")
        return

    await circles_module.set_monthly_requirement(new_val, message.author.id)
    await message.channel.send(
        f"Monthly fan requirement updated to **{new_val:,}**. Refreshing club display…"
    )
    try:
        await circles_module.post_or_edit(force=True)
        await message.channel.send("Club display refreshed.")
    except Exception as exc:
        logger.error(f"[Bot] Fancount refresh failed: {exc}")
        await message.channel.send(f"Refresh failed: {exc}")


async def _cmd_circles(message: discord.Message):
    await message.channel.send("Fetching from uma.moe API…")
    try:
        await circles_module.post_or_edit(force=True)
        await message.channel.send("Circle stats refreshed.")
    except Exception as exc:
        logger.error(f"[Bot] Circles refresh failed: {exc}")
        await message.channel.send(f"Circle refresh failed: {exc}")


async def _cmd_circle_run(message: discord.Message):
    """Run the full daily routine now (snapshot + daily report + monthly
    leaderboard if a month just wrapped), ignoring the once-per-day guard."""
    if message.author.id != MAIN_OWNER_ID:
        await message.channel.send("No.")
        return
    await message.channel.send("Running the daily circle update now…")
    try:
        ok = await circles_module.run_daily_update(force=True)
        await message.channel.send(
            "Done." if ok else "uma.moe was unreachable — nothing sent. Try again shortly."
        )
    except Exception as exc:
        logger.error(f"[Bot] circle run failed: {exc}")
        await message.channel.send(f"Daily update failed: {exc}")


async def _cmd_parent_refresh(message: discord.Message):
    await message.channel.send("Scraping GT global character list (this may take ~30 seconds)…")
    try:
        result = await parent_module.refresh_char_ids()
        await message.channel.send(result)
    except Exception as exc:
        logger.error(f"[Bot] Parent refresh failed: {exc}")
        await message.channel.send(f"Parent refresh failed: {exc}")


async def _cmd_trainee_refresh(message: discord.Message):
    if message.author.id != MAIN_OWNER_ID:
        await message.channel.send("No.")
        return
    await message.channel.send(
        "Refreshing trainee data — umamusu.wiki scrape → Fandom gap-fill → image "
        "normalize. This takes ~10 minutes; I'll post each stage."
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "build_trainee_data.py",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        tail: list[str] = []
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            if line.startswith("== "):
                await message.channel.send(line[3:])
        rc = await proc.wait()
        if rc != 0:
            err = "\n".join(tail[-25:])
            await message.channel.send(
                f"Trainee refresh failed (exit {rc}):\n```\n{err}\n```"
            )
    except Exception as exc:
        logger.error(f"[Bot] Trainee refresh error: {exc}")
        await message.channel.send(f"Trainee refresh error: {exc}")


async def _cmd_role_cleanup(message: discord.Message, force: bool = False):
    if message.author.id != MAIN_OWNER_ID:
        await message.channel.send("No.")
        return
    guild = bot.get_guild(MAIN_SERVER_ID)
    if guild is None:
        await message.channel.send("Tonberries server not in cache.")
        return
    names, skip = await role_module.cleanup_empty_fan_roles(guild, dry_run=not force)
    if skip:
        await message.channel.send(f"Cleanup can't run — {skip}")
        return
    if not names:
        await message.channel.send("No empty fan roles right now.")
        return

    shown = names[:40]
    listing = "\n".join(f"- {n}" for n in shown)
    if len(names) > len(shown):
        listing += f"\n…and {len(names) - len(shown)} more"
    if force:
        await message.channel.send(f"Deleted {len(names)} empty fan role(s):\n{listing}")
    else:
        await message.channel.send(
            f"{len(names)} empty fan role(s) would be deleted (auto-runs daily "
            f"17:30 UTC; `role cleanup now` to do it now):\n{listing}"
        )


async def _cmd_render_monthly(message: discord.Message):
    if message.author.id != MAIN_OWNER_ID:
        await message.channel.send("No.")
        return
    await message.channel.send("Rendering the Monthly Fan Leaderboard…")
    try:
        from leaderboard.build import render_monthly_png

        buf, filename, label = await render_monthly_png(bot.get_guild(MAIN_SERVER_ID))
        await message.channel.send(
            f"**Monthly Fan Leaderboard — {label}**",
            file=discord.File(buf, filename=filename),
        )
    except Exception as exc:
        logger.error(f"[Bot] render monthly failed: {exc}")
        await message.channel.send(f"Render failed: {exc}")


async def _cmd_skill_refresh(message: discord.Message):
    await message.channel.send("Running skills scraper (this takes several minutes)…")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "skills_scraper.py",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[-1500:]
            logger.error(f"[Bot] Skills scraper failed: {err}")
            await message.channel.send(
                f"Skills scraper failed (exit {proc.returncode}):\n```\n{err}\n```"
            )
        else:
            await message.channel.send("Skills database refreshed.")
    except Exception as exc:
        logger.error(f"[Bot] Skills scraper error: {exc}")
        await message.channel.send(f"Skills scraper error: {exc}")


async def _cmd_skill_sync(message: discord.Message):
    await message.channel.send("Syncing uma-skill-tools data from GitHub…")
    try:
        result = await skill_sync.sync_now(bot)
        await message.channel.send(result)
    except Exception as exc:
        logger.error(f"[Bot] Skill sync failed: {exc}")
        await message.channel.send(f"Skill sync failed: {exc}")


async def _cmd_send(message: discord.Message, args: str) -> None:
    if message.author.id != MAIN_OWNER_ID:
        await message.channel.send("No.")
        return
    parts = args.split(" ", 1)
    if len(parts) < 2:
        await message.channel.send("Usage: `send [channel_id] [message]`")
        return
    try:
        channel_id = int(parts[0])
    except ValueError:
        await message.channel.send(f"Invalid channel ID: `{parts[0]}`")
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        await message.channel.send(f"Channel `{channel_id}` not found.")
        return
    await channel.send(parts[1])
    await message.channel.send(f"Sent to <#{channel_id}>.")


async def _cmd_restart(message: discord.Message):
    import subprocess
    await message.channel.send("Pulling latest changes…")
    try:
        result = subprocess.run(
            ["git", "-C", "/home/piberry/Tonberries-Bot", "pull"],
            capture_output=True, text=True
        )
        out = (result.stdout + result.stderr).strip()
        await message.channel.send(f"```\n{out}\n```\nRestarting…")
        logger.info(f"[Bot] git pull: {out}")
    except Exception as exc:
        await message.channel.send(f"git pull failed: {exc}\nRestarting anyway…")
        logger.error(f"[Bot] git pull error: {exc}")
    subprocess.Popen(["sudo", "systemctl", "restart", "tonberries-bot"])


# ---------------------------------------------------------------------------
# Guild message handler
# ---------------------------------------------------------------------------

async def _handle_guild_message(message: discord.Message) -> None:
    if message.guild.id != MAIN_SERVER_ID:
        return

    m = _HITLIST_RE.match(message.content.strip())
    if m is None:
        return

    if message.author.id != MAIN_OWNER_ID:
        await message.channel.send(random.choice(["No.", "Nope.", "Nuh Uh", "Don't even think about it.", "<a:diashake:1508662342060081253>"]))
        return

    name   = m.group(1).strip()
    reason = m.group(2).strip() if m.group(2) else None

    # Cancel existing removal task if the same name is re-added
    existing = _manual_hitlist.get(name)
    if existing and not existing.done():
        existing.cancel()
    _manual_hitlist[name] = asyncio.create_task(_remove_from_hitlist_after(name))

    # Queue line for the next daily report
    reason_display = reason if reason else "Treasoning"
    circles_module.add_manual_hitlist_line(
        f"{circles_module.STATUS_EMOJIS['hitlist']} **{name}** added to Hitlist for **{reason_display}**"
    )

    # Add to the live hitlist embed and refresh the channel display
    circles_module.add_manual_hitlist_entry(name, reason)

    reply = f"**{name}** have been added to the Hitlist"
    if reason:
        reply += f" for {reason}"
    reply += "!"
    await message.channel.send(reply)

    await circles_module.refresh_hitlist_embed()


async def _remove_from_hitlist_after(name: str) -> None:
    await asyncio.sleep(86400)  # 24 hours
    _manual_hitlist.pop(name, None)
    circles_module.remove_manual_hitlist_entry(name)
    await circles_module.refresh_hitlist_embed()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not token:
        logger.critical("DISCORD_TOKEN not set in .env — cannot start")
        sys.exit(1)
    bot.run(token)
