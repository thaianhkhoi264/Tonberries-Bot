# Dia voice — proposed message changes (REVIEW DRAFT, nothing applied)

Rules applied (from `.claude/skills/satono-diamond-voice`): voice only; every placeholder, backticked syntax, mention, timestamp, emoji ID and `{exc}` stays; at most one `♪` and one sparkle motif per message; no other characters mentioned; logs/slash descriptions/embed field labels untouched.

Two tiers, so you can say "player-facing only" or "everything":

- **A, player-facing** (autotrain, training dashboard, `/role`, `/skill`, `/parent`, `/stamina`, `/whenis`, event pings, fan channel): fuller voice.
- **B, owner tooling** (DM commands, sync/alert DMs): light touch, short lead-in + facts.

Line numbers are current as of this draft.

---

## main.py (tier B, owner DM commands, except where noted)

| Line | Now | Proposed |
|---|---|---|
| 254 | `Bot restarted.\nLatest commit: \`{commit}\`` | `I'm back! ♪\nLatest commit: \`{commit}\`` |
| 299 **(A)** | `Hello! Whatever you're typing is not supported, use \`auto\` to start an Independent Training reminder. <dianod>` | `Hello! I'm afraid I don't understand that one. Try \`auto\` to start an Independent Training reminder! <dianod>` |
| 354 | `**Dia's commands!**` | keep |
| 374-375 | `Restart Dia :(` / `Stop Dia in case she spammed...` | keep (already her own joke); other help rows stay literal |
| 381 / 384 / 387 | `Refreshing dashboards…` / `Dashboards refreshed.` / `Refresh failed: {exc}` | `On it! Refreshing the dashboards…` / `The dashboards are refreshed! ♪` / `Oh dear... the refresh failed: {exc}` |
| 402 | `Error fetching pending notifications: {exc}` | `Oh dear... I couldn't fetch the pending notifications: {exc}` |
| 406 | `Shutting down. Use \`sudo systemctl start tonberries-bot\` to bring me back.` | `Time for a rest... Use \`sudo systemctl start tonberries-bot\` to bring me back.` |
| 413 / 416 / 419 | `Building fan report…` / `Report sent.` / `Report failed: {exc}` | `Putting the fan report together…` / `The report is on its way! ♪` / `Oh dear... the report failed: {exc}` |
| 425 | `Public shaming turned **{state}**.` | `Understood! Public shaming is now **{state}**.` |
| 435 | `Monthly fan requirement: **{val}**` | keep (data readout) |
| 445 | `\nUsage: \`fancount edit <number>\`` | keep |
| 451 | `Usage: \`fancount\` or \`fancount edit <number>\`` | `Like this, please: \`fancount\` or \`fancount edit <number>\`` |
| 457 | `Invalid number: \`{x}\`` | `Hm, \`{x}\` doesn't look like a number to me...` |
| 461 | `Fan requirement must be a positive number.` | `The fan requirement has to be a positive number, please.` |
| 466 / 470 / 473 | `Monthly fan requirement updated to **{n}**. Refreshing club display…` / `Club display refreshed.` / `Refresh failed: {exc}` | `Got it! The monthly fan requirement is now **{n}**. Refreshing the club display…` / `The club display is refreshed! ♪` / `Oh dear... the refresh failed: {exc}` |
| 477 / 480 / 483 | `Fetching from uma.moe API…` / `Circle stats refreshed.` / `Circle refresh failed: {exc}` | `Fetching from uma.moe…` / `The circle stats are refreshed! ♪` / `Oh dear... the circle refresh failed: {exc}` |
| 500 | `Date must be \`YYYY-MM-DD\`.` | `The date should look like \`YYYY-MM-DD\`, please.` |
| 503 | `Firing the daily circle update for game-day **{d}**…` | `Starting the daily circle update for game-day **{d}**…` |
| 507 | `Done.` / `uma.moe was unreachable — nothing sent. Try again shortly.` | `All done! ♪` / `uma.moe couldn't be reached, so nothing was sent... Let's try again shortly.` |
| 511 | `Daily update failed: {exc}` | `Oh dear... the daily update failed: {exc}` |
| 515 / 521 | `Scraping GT global character list (this may take ~30 seconds)…` / `Parent refresh failed: {exc}` | `Scraping the GameTora character list... this may take about 30 seconds!` / `Oh dear... the parent refresh failed: {exc}` |
| 528-531 | `Refreshing trainee data — … This takes ~10 minutes; I'll post each stage.` | `Refreshing the trainee data — umamusu.wiki scrape → Fandom gap-fill → image normalize. This takes about 10 minutes; I'll post each stage!` |
| 552 / 556 | `Trainee refresh failed (exit {rc}):` / `Trainee refresh error: {exc}` | `Oh dear... the trainee refresh failed (exit {rc}):` / `Oh dear... the trainee refresh hit an error: {exc}` |
| 565 | `Tonberries server not in cache.` | `I can't find the Tonberries server in my cache...` |
| 569 | `Cleanup can't run — {skip}` | `I can't run the cleanup — {skip}` |
| 572 | `No empty fan roles right now.` | `There are no empty fan roles right now!` |
| 580 | `Deleted {n} empty fan role(s):` | `I deleted {n} empty fan role(s):` |
| 583-584 | `{n} empty fan role(s) would be deleted (auto-runs daily 17:30 UTC; \`role cleanup now\` to do it now):` | `{n} empty fan role(s) would be deleted (this runs daily at 17:30 UTC; use \`role cleanup now\` to do it now):` |
| 592 / 603 | `Rendering the Monthly Fan Leaderboard…` / `Render failed: {exc}` | `Rendering the Monthly Fan Leaderboard…` / `Oh dear... the render failed: {exc}` |
| 598 | `**Monthly Fan Leaderboard — {label}**` | keep (title) |
| 607 / 619 / 622 / 625 | `Running skills scraper (this takes several minutes)…` / `Skills scraper failed (exit {n}):` / `Skills database refreshed.` / `Skills scraper error: {exc}` | `Running the skills scraper... this takes several minutes!` / `Oh dear... the skills scraper failed (exit {n}):` / `The skills database is refreshed! ♪` / `Oh dear... the skills scraper hit an error: {exc}` |
| 629 / 635 | `Syncing uma-skill-tools data from GitHub…` / `Skill sync failed: {exc}` | `Syncing the uma-skill-tools data from GitHub…` / `Oh dear... the skill sync failed: {exc}` |
| 641 | `Usage: \`send [channel_id] [message]\`` | `Like this, please: \`send [channel_id] [message]\`` |
| 649 / 653 / 656 | `Invalid channel ID: \`{x}\`` / `Channel \`{id}\` not found.` / `Sent to <#{id}>.` | `Hm, \`{x}\` isn't a valid channel ID...` / `I couldn't find channel \`{id}\`...` / `Sent to <#{id}>!` |
| 661 / 668 / 671 | `Pulling latest changes…` / `…Restarting…` / `git pull failed: {exc}\nRestarting anyway…` | `Fetching the latest changes…` / `…Back in a moment!` / `The git pull failed: {exc}\nRestarting anyway...` |
| 495, 526, 561, 590, 640 | `No.` (owner-only refusals, only reachable by a non-owner who is already an owner-list user for lesser commands) | **Decision 1** below |
| 689 **(A)** | random of `No.` / `Nope.` / `Nuh Uh` / `Don't even think about it.` / `<diashake>` | **Decision 1** below |
| 710-713 **(A)** | `**{name}** have been added to the Hitlist` + ` for {reason}` + `!` | `**{name}** has been added to the Hitlist` + ` for {reason}` + `! Oh dear...` (also fixes "have" → "has") |

## autotrain_module.py (tier A)

| Line | Now | Proposed |
|---|---|---|
| 63 | `{prefix} training will end in <t:…:R>. <diaread>\nUse \`end\` to finish early, or \`renew\` to restart the timer.` | `{prefix} training will be done <t:…:R>. I'll be waiting! <diaread>\nUse \`end\` to finish early, or \`renew\` to restart the timer.` |
| 72 | `…is complete! I'm excited to see how well that went!\n\nYou can use \`auto [text]\` … <diapat>` | keep: already the reference tone |
| 233 | `No previous training found. Use \`auto\` to start one.` | `I don't have a previous training on record, Trainer. Use \`auto\` to start one!` |
| 272 | `No active timer to end.` | `There's no timer running right now, Trainer.` |

## notification_module.py (tier A, high-frequency, so lightest touch)

Lines 80-91 build `🐴 …` pings; admin-set custom templates from the Gacha DB override them anyway (`_resolve_message`). Proposal: add a short send-off to the **reminder** and **start** variants only, leave `has ended` / `is ending` / phase / character variants unchanged:

- `🐴 Reminder: **{title}** starts {t}!` → `🐴 Reminder: **{title}** starts {t}! Let's be ready!`
- `🐴 **{title}** is starting {t}!` → `🐴 **{title}** is starting {t}! Let's give it our all!`

(Optional: skip this file entirely if you'd rather the pings stay neutral.)

## circles_module.py (tier A, public fan channel)

| Line | Now | Proposed |
|---|---|---|
| 474 | `Monthly goal reached — {n} ahead of target` | `Monthly goal reached — {n} ahead of target! Wonderful!` |
| 483 | `On track — {n} more fans to finish the month` | `On track! {n} more fans to finish the month.` |
| 498 | `Very behind — {details}` | `Oh dear... very behind — {details}` |
| 504 | `Behind — {details}` | `A little behind — {details}. Let's catch up!` |
| 511 | `Month ended — {n} short of the monthly goal` | `The month has ended, and we're {n} short of the goal... A lesson for next time.` |
| 677 | `No members.` | keep |
| 696 / 702 / 706 | field labels `Goal Reached!` / `Behind Goal` / `Did nothing` | keep (labels) |
| 713 | `_Daily totals reset — gains will show in the next report._` | `_The daily totals have reset — gains will show in the next report!_` |
| 743 / 758 | `*Nobody here — great work!*` | `*Nobody here — wonderful work, everyone!* ♪` |
| 739 / 749 | `Dia's Watchlist` / `Dia's Hitlist` | keep (titles, already Dia) |
| 721 | `~~**{name}**~~ - Eliminated` | keep |
| 869-875 | `**{n}** added to Hitlist` / `➖ **{n}** removed from Hitlist` / `… added to Watchlist` / `➖ … removed from Watchlist` | keep: change-log lines, already tersely emoji-coded |
| 942 | `# {label} — Fan Leaderboard` | keep (title) |

## training_module.py (tier A)

| Line | Now | Proposed |
|---|---|---|
| 304-309 | pinned header text ("This channel shows live training status. …") | Same content, in voice: `This channel shows live training status. When a run starts, a card appears here with the character and current progress. Once the run finishes, the card is removed and the result is posted to the manual or independent results channel!\n\nFor Independent Training, I'll also ping you here once the 50 minute timer ends. ♪` (note: `ensure_dashboard_header` only checks the old message still exists, so the already-pinned message will **not** update on its own; delete it in Discord for the bot to repost the new text) |
| 443 | `Ready to collect! Log in to finish the run. <diapat>` | keep |
| 446 | `Independent Training ongoing — ready <t:…:R>` | `Independent Training is underway — ready <t:…:R>!` |
| 449 | `Manual Training ongoing — waiting for it to end` | `Manual Training is underway — I'll wait for it to end!` |
| 547 | `<@{id}> Independent Training has ended — log in to collect the result!` | `<@{id}> Your Independent Training has ended! Please log in to collect the result! ♪` |
| 801-803 | `I got a training_end for **{name}** with no matching training_start on record — I can't tell if this was a manual or independent training.\nReply \`manual\`, \`independent\`, or \`cancel\` here.` | `Hm... I got a training_end for **{name}** with no matching training_start on record, so I can't tell if this was a manual or independent training.\nCould you reply \`manual\`, \`independent\`, or \`cancel\` here, please?` |
| 1046 | `Cancelled — that training_end will be discarded.` | `Understood — I'll discard that training_end.` |
| 1051 | `Got it — posted as a {cmd} training result.` | `Got it! I posted it as a {cmd} training result. ♪` |
| 1054 | `Reply \`manual\`, \`independent\`, or \`cancel\` for the training_end I asked about.` | `Please reply \`manual\`, \`independent\`, or \`cancel\` for the training_end I asked about.` |
| 932 / 938 | `A training run was glued` / `The Training for {name} was glued` | keep (embed titles) |

## role_module.py (tier A, ephemeral replies in the Tonberries server)

| Line | Now | Proposed |
|---|---|---|
| 533, 666 | `This command only works in the Tonberries server.` | `I'm afraid this command only works in the Tonberries server.` |
| 543, 676 | `Could not resolve your membership.` | `Hm, I couldn't work out your membership...` |
| 549 | `**{text}** doesn't match a known trainee. Pick a name from the autocomplete list (character data comes from the wiki scrape).` | `Hm, **{text}** doesn't match any trainee I know. Please pick a name from the autocomplete list!` (drops the parenthetical) |
| 560 | `I don't have the **Manage Roles** permission here.` | `I'm afraid I don't have the **Manage Roles** permission here...` |
| 589 | `I couldn't create the role (missing permission).` | `Oh dear... I couldn't create the role (missing permission).` |
| 595 | `Failed to create the role: {exc}` | `Oh dear... I couldn't create the role: {exc}` |
| 606 | `You already have **{role}**.` | `You already have **{role}**, Trainer!` |
| 613-614 | `**{role}** exists but sits above my highest role, so I can't assign it. Ask an admin to move it down.` | `**{role}** sits above my highest role, so I can't assign it... Could you ask an admin to move it down, please?` |
| 630-632 | ` You were at the {n}-role limit, so I removed **{old}**.` | ` You were at the {n}-role limit, so I removed **{old}** to make room.` |
| 635-636 | ` You're at the {n}-role limit and I couldn't remove **{old}** (role hierarchy) — ask an admin.` | keep |
| 644 | `I couldn't give you **{role}** (role hierarchy).` | `Oh dear... I couldn't give you **{role}** (role hierarchy).` |
| 652-653 | `Created and gave you **{role}**.` / `Gave you **{role}**.` | `I created **{role}** and gave it to you! ♪` / `Here you go — **{role}** is yours! ♪` (+ evicted note unchanged) |
| 694 | `You are not a **{text}**.` | `Hm, you don't have **{text}**.` |
| 702 | `I couldn't remove **{role}** (role hierarchy).` | `Oh dear... I couldn't remove **{role}** (role hierarchy).` |
| 710 | `Removed **{role}**.` | `Done! I removed **{role}**.` |

## skills_module.py, parent_module.py, stamina_module.py, lookup_module.py (tier A)

Embed bodies (skill descriptions, conditions, stamina/parent tables) and headers stay plain. Only the conversational replies change:

| File:line | Now | Proposed |
|---|---|---|
| skills 393 | `Please provide a skill name.` | `Which skill would you like to look up, Trainer?` |
| skills 410-411 | `No skill found for **{n}**.\nSkills database not synced yet — try \`skill sync\` first.` | `I couldn't find a skill for **{n}**...\nThe skills database hasn't been synced yet — please try \`skill sync\` first.` |
| skills 416, 535 | `No skill found matching **{n}**.` | `I couldn't find a skill matching **{n}**... Might it be spelled a little differently?` |
| skills 420-421, 539-540 | `Found more than {max} skills matching **{n}** — please be more specific.` | `I found more than {max} skills matching **{n}** — could you be a little more specific?` |
| skills 427-428 / 546-547 | `Multiple skills match **{n}**:` … `Use the full name to get details.` / `Type the full name…` | `Several skills match **{n}**:` … `Please use the full name to see the details!` / `Please type the full name…` |
| skills 526 | `Usage: \`skill <name>\` — e.g. \`skill Red Shift\`` | `Like this, please: \`skill <name>\` — e.g. \`skill Red Shift\`` |
| skills 530 | `Skills database not found. Run \`skill refresh\` first.` | `I can't find the skills database... Please run \`skill refresh\` first.` |
| parent 827, stamina 376 | `Skill data is not yet loaded — please try again in a moment.` | `I'm still loading the skill data — please try again in a moment!` |
| parent 854, stamina 393 | `Could not resolve a course for that CM/selection. The CM may not be in the timeline yet.` | `Hm, I couldn't work out a course for that. The CM may not be in the timeline yet...` |
| parent 862, stamina 401 | `No course geometry found for course ID {id}.` | `I couldn't find any course geometry for course ID {id}...` |
| parent 889, stamina 428-429 | `## {course}` + `-# *Good inherited skills by running style*` … | keep (informational headers) |
| lookup 341 | `Please provide a card or character name.` | `Which card or character would you like to look up, Trainer?` |
| lookup 351 | `No card found matching **{q}**.` | `I couldn't find a card matching **{q}**... Might it be spelled a little differently?` |
| lookup 395-397 | `Multiple versions of **{q}** found in banners:\n{lines}\nReact to pick one.` | `There are several versions of **{q}** in the banners:\n{lines}\nPlease react to pick one!` |

## players_module.py (tier B, owner `link` tooling), minimal

Mostly data readouts and usage lines. Proposed: usage lines get `Like this, please:` and failures get `Oh dear...`; everything else unchanged.

| Line | Now | Proposed |
|---|---|---|
| 246 | `No player links yet. \`link import\` or \`link <@user> <name>\`.` | `There are no player links yet! Try \`link import\` or \`link <@user> <name>\`.` |
| 263 | `Export failed: {exc}` | `Oh dear... the export failed: {exc}` |
| 279 | `Couldn't read attachment: {exc}` | `Oh dear... I couldn't read the attachment: {exc}` |
| 285-286 | `No \`{path}\` and no CSV attached. Run \`link export\` first, fill it in, then \`link import\`.` | `I don't see \`{path}\` or an attached CSV... Please run \`link export\` first, fill it in, then \`link import\`.` |
| 324 / 338 | `Usage: \`link <@user\|id> <trainer name>\`` / `Usage: \`unlink <trainer name>\`` | `Like this, please: …` |
| 329 | `Linked **{name}** → <@{uid}>.` | `Linked **{name}** → <@{uid}>! ♪` |
| 341 / 344 | `Unlinked **{name}**.` / `**{name}** wasn't linked.` | `Unlinked **{name}**.` / `**{name}** wasn't linked, so there's nothing to remove.` |
| 237, 258, 294-302, 313, 318 | counts, summaries, link readouts | keep |

## Leave plain (tier C, and why)

- `cm_module.py:367-373` schema-drift DM and `uma_module.py:370, 441` ⚠️ channel-fix DMs: urgent diagnostics for the owner. A lead-in would be the only change (`Oh dear...`); recommend leaving as-is.
- `skill_sync.py:88/91` and `training_data_sync.py:74/77` status strings: they double as the reply to `skill sync` and are logged; recommend a light `Oh dear... ` prefix on the two failure strings only, or leave.
- All `logger.*` calls, slash-command names/descriptions/option help, embed field labels, skill/stamina/parent embed content, `Generated` footer, `ID:` footers.

## Decisions for you

1. **Refusals.** The curt `No.` family (owner-only commands, and the guild hitlist reply to non-owners) reads like a deliberate joke. Options: (a) keep as is; (b) voice the guild pool only (`I'm afraid not.` / `Nope!` / `Nuh uh!` / `Don't even think about it.` / `<diashake>`) and leave owner-only `No.` alone; (c) voice everything (`I'm afraid I can't do that for you.`). **Recommend (b).**
2. **Failure prefix.** `Oh dear...` on every failure gets repetitive across ~20 sites. Options: (a) always `Oh dear...`; (b) rotate `Oh dear...` / `Oh no...` / `Hm, that didn't work...` via a tiny shared helper (adds a small `dia_voice.py`); (c) `Oh dear...` only on public/player-facing failures, plain on owner tooling. **Recommend (c): simplest, no new module.**
3. **Scope.** All tiers, or tier A only (owner DM tooling untouched)? **Recommend A + the light tier-B edits above, since you read those most.**
4. **Pings.** Do the reminder/start suffixes (`Let's be ready!`) belong on event notifications, or should those stay neutral?
5. **Pinned dashboard header.** The existing pinned message won't change by itself. Do you want to delete it in Discord so the bot reposts the voiced version on next restart?

## Next step (once you approve)

Apply per file, `py_compile` each, and hand you the diff to review and stage by name. I'd keep CRLF files (`cm_module.py`, `parent_module.py`) byte-safe. Suggested commit split: one commit per tier, or one for everything — your call.
