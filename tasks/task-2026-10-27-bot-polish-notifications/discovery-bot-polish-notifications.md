---
status: discovery-complete
created: 2026-10-27
task: TASK.md
related: ../task-2026-10-26-weekly-giveaway-bot/TASK.md, ../../product-docs/PRD/PRD-weekly-giveaway-bot.md
---
# Discovery: Bot polish and winner/participant notifications

## Feature Overview
Two things:
1. **Polish:** make the bot look like a finished product. That means a persistent menu keyboard, card-style messages, a proper per-language command menu and bot description, and a lightly tidied admin panel.
2. **Notifications:** add three notifications that are missing today:
   - a deadline reminder for winners;
   - a "you missed the prize" notice;
   - a weekly "you're in this week's draw" notice to every entrant, with an inline mute/unmute toggle.

## Why This Exists
- The bot is the first thing a new channel subscriber touches. Today it reads like a prototype:
  - the command menu mixes all three languages in each line;
  - there is no bot description;
  - `/help` is a bare list.
  A polished bot supports the PRD KPI, channel growth: people trust the giveaway and stay.
- Winners already get a DM at publish (`win_dm`), and the bot shows it again on `/start` and `/status`. But a winner who misses that DM loses the prize silently after 48 h, and nobody is told they lost it. A reminder means fewer redraws and less staff work; the notice is basic fairness.
- Entrants hear nothing between joining and the results post. A weekly "you're in" message keeps them engaged and reminds them to stay subscribed, since an unsubscribed winner is skipped.

## Usage Context
- **Subscribers:** Telegram mobile, mostly uz-Cyrl, some uz-Latn and ru. They open the bot from the channel post, press join once, then mostly hear from the bot weekly.
- **Winner:** gets the win DM, then may forget it; the reminder fires 12 h before the deadline.
- **Admins (1–3 people):** run the weekly draw from the `/admin` panel in a private chat.

## Chosen Direction
**Menu buttons and cards** (user's choice).

**Persistent bottom keyboard** with four buttons, in the user's language:

| uz-Cyrl | English |
|---|---|
| 🎁 Танлов | Contest |
| 📋 Ҳолатим | My status |
| 🔎 Текшириш | Verify |
| 🌐 Тил | Language |

**Card-style texts:**
- emoji header, a thin divider, short labelled lines;
- inline buttons under messages where there is an action: join, open channel, mute toggle.

**Admin side:** light polish only. Status names in words, a clearer panel card, and admin commands shown in the ☰ menu for admins only.

## How It Works

**Polish**
1. **On startup** the bot sets its profile per language:
   - `setMyCommands`: the default menu in uz-Cyrl and a `ru` menu for Russian-language clients. Telegram's `uz` code can't tell Cyrillic from Latin, so the default is Cyrillic.
   - `setMyDescription` and `setMyShortDescription`: what users see before pressing Start.
2. **After `/language`** the bot also sets a per-chat command menu in the chosen language. For admins, that per-chat menu = user commands + admin commands (`admin`, `stats`, `staff_add`, `staff_del`).
3. **On `/start`:**
   - the rules show as a card with inline «✅ Иштирок этиш» and «📢 Каналга ўтиш» buttons;
   - the bottom menu keyboard is attached.
4. **Menu keyboard buttons** route to the same handlers as `/start`, `/status`, `/verify` and `/language`, recognised in all 3 languages.

**Notifications**
5. **"You're in this week's draw"**
   - Sent when entries close and the commitment is posted.
   - Goes to every entrant in the snapshot who hasn't muted notices.
   - Shows the draw number, their entry code, a reminder to stay subscribed, and a link to the commitment post.
   - Carries an inline **🔕 Хабарларни ўчириш / 🔔 Ёқиш** toggle. Pressing it flips the user's setting and edits the button in place.
   - Delivery runs in the background, rate-limited, and never delays the draw.
6. **Deadline reminder**
   - Sent to a winner who hasn't sent a contact, at **36 h after publish** (12 h before the 48 h deadline).
   - Sent once, and **always**, whatever the mute setting.
7. **Missed-prize notice**
   - Sent when the claim window expires, at the moment the admins get `admin_expired`.
   - Tells the winner politely that the deadline passed and the prize goes to a redraw.
   - Sent **always**, and once.
8. **Mute setting.** Every non-essential notice carries the toggle, so today that's only the weekly notice. Winner messages (win DM, reminder, missed-prize, activation) ignore the setting. `/status` shows the current setting and the toggle.

**Admin polish**
9. **`/admin` panel card:**
   - pool size, current draw and its status in words (e.g. «⏳ Ғолиб аниқланмоқда», "Winner being drawn");
   - the existing buttons.
10. **Admin alerts** keep their content but use the same card style.

## In Scope
- Persistent reply keyboard (4 buttons, 3 languages), with routing for those texts.
- Card-style rewrite of the user-facing texts in all 3 catalogs:
  - `rules`, `joined`, `status_*`, `help`;
  - the claim texts;
  - `win_dm` and `prize_activated`.
- Per-language command menus and bot description/short description, set at startup. Per-chat menu after `/language`, with admin commands for admins.
- "You're in this draw" broadcast with the inline mute/unmute toggle. The toggle also appears in `/status`.
- Winner deadline reminder (12 h before), sent once and persisted, so a restart doesn't re-send or skip it.
- Missed-prize notice at expiry, sent once and persisted.
- Admin panel and status-name polish; admin commands in the admins-only ☰ menu.
- Tests for each new notification:
  - fires once;
  - respects mute where it applies;
  - survives a restart;
  - doesn't block the draw;
  - routes keyboard button texts correctly.

## Out of Scope
- **Channel post format:** the commitment, results and no-winner posts keep their current structure and every verification field. At most, wording tweaks if they're needed to match the new style. They are public fairness records, so they change only deliberately.
- **Results DM to every entrant** after publish (the user didn't choose it).
- **The bot's avatar picture:** set manually in @BotFather. The bot API can't set it.
- **Telegram Mini App / web UI.**
- **New languages,** and final approval of the uz-Latn and ru wording (staff review, as before).
- **Changing the 48 h window** or the redraw rules.

## Key Requirements
1. **Free text vs. claims:** a menu-button press is never treated as a claim submission.
   - Today, a pending winner's free text goes to `claims.submit`.
   - Keyboard texts in all 3 languages must be matched first.
2. **Weekly notice delivery:**
   - It must not delay or block close, commit or resolve.
   - It respects Telegram limits: under ~25 msg/s, honouring `RetryAfter`.
   - A blocked bot (Forbidden) skips that user silently.
3. **Exactly once:**
   - The weekly notice is sent at most once per user per draw, including after a crash or restart. A persisted marker or a resume point is needed.
   - The deadline reminder and the missed-prize notice are each sent at most once per draw, also across restarts. The housekeeping loop drives them, like `expire_due`.
4. **Mute:**
   - The toggle flips a per-user setting that is stored durably.
   - The toggled message's button updates in place.
   - Mute never suppresses winner messages.
5. **Weekly notice content:** only to snapshot entrants (excluded users are already out of the snapshot). It never reveals anything about the draw outcome.
6. **Missed-prize notice content:** it does not name or hint at the next winner.
7. **Language:**
   - Every new text exists in uz-Cyrl, uz-Latn and ru, with matching placeholders; the existing i18n test enforces this.
   - Command menus and descriptions use the user's language when known, otherwise the client language, with uz-Cyrl as the default.
8. **Admin menu:** admin commands appear in the menu only for admin chats.
9. **Telegram limits:** descriptions fit Telegram's limits (description ≤ 512 chars, short description ≤ 120 chars).

## Constraints
- The existing stack and patterns: aiogram 3, SQLite, Alembic, `Publisher` port, `i18n/*.yaml`. New user fields (mute flag) and per-draw markers (reminder and missed sent, weekly notice progress) need an Alembic migration.
- Single instance, long polling. The broadcast runs inside the bot process.
- Channel posts stay in uz-Cyrl only.
- Verification data and the draw safety guarantees from the review rounds must not regress: CAS transitions, no cancelling, and persisted commitments.

## Notes
- **Existing behaviour found during discovery:**
  - The winner DM already exists: `services/draw_service.py`, `finish_publishing`.
  - It is re-shown on `/start` and `/status`: `handlers/user.py`, `_remind_claim`.
  - Commands are registered in `main.py` with mixed-language descriptions.
- **Grill findings, folded into the requirements above:**
  - keyboard texts colliding with claim free text;
  - a broadcast at scale blocking the draw;
  - double sends after a restart;
  - mute accidentally silencing winner messages;
  - Telegram's `uz` language code not separating the two scripts;
  - per-chat admin menus overriding the user menu.
- **Glossary terms proposed** (not yet written to the glossary):
  - *weekly notice*: "you're in this week's draw";
  - *mute toggle*;
  - *deadline reminder*;
  - *missed-prize notice*.
- **Cross-AI validation:** SKIPPED. The Codex CLI is not authenticated (401 earlier in the project), and no other validator is installed.
