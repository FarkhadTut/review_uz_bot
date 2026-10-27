---
status: ready
created: 2026-10-27
discovery: discovery-bot-polish-notifications.md
---
# Plan: bot polish and notifications

## Changes

**Data (migration `polish_notifications`)**

| Table | Field | Default | Purpose |
|---|---|---|---|
| `users` | `notify` | `true` | Weekly-notice opt-in (mute toggle). |
| `draws` | `reminder_sent` | `false` | Deadline reminder sent. |
| `draws` | `missed_notified` | `false` | Missed-prize notice sent. |
| `draws` | `notice_done` | `false` | Weekly notice finished, or not applicable. |
| `draw_entries` | `notified` | `false` | Weekly notice handled for this entry; makes a crashed run resumable. |

Each flag is claimed with a conditional UPDATE *before* sending, so a notice is sent **at most once**. A crash in between loses one message rather than sending it twice. Duplicates are worse than a rare miss for these texts.

**`services/notices.py` (new): `Notices`**
- `pending()`: draws with a commitment post, `parent_id IS NULL` and `notice_done = false`. Redraw children are created with `notice_done = true`, because their entrants were already told for the parent draw.
- `send_weekly(draw_id)`:
  - goes through the entries with `notified = false` in batches;
  - claims each entry, then sends `weekly_notice` with the mute toggle to users with `notify = true`;
  - throttles to `rate` sends per second;
  - sets `notice_done` at the end.
  - A per-draw lock means concurrent calls don't double-send.
- **Publisher:**
  - `TelegramPublisher.send` waits out `TelegramRetryAfter` once before giving up;
  - `post_link(msg_id)` builds the channel post URL (`t.me/<name>/<id>`, or `t.me/c/<id>/<msg>` for private channels).

**`services/claims.py`**
- `remind_due()`:
  - picks PUBLISHED, announced draws where `published_at + window − reminder_hours ≤ now` and `reminder_sent = false`;
  - claims the flag, then sends `claim_reminder` (always, ignoring mute).
- `expire_due()` also sends `prize_missed` to the winner once (claims `missed_notified`, always, ignoring mute).
- `Settings.claim_reminder_hours`: default 12, must be less than `claim_window_hours`.

**`services/runner.py`**
- Housekeeping also calls `claims.remind_due()`.
- It spawns `notices.send_weekly(id)` for each pending draw, at most one task per draw, so delivery never blocks close, commit or resolve.

**UI (`handlers/user.py`, new `handlers/menu.py`)**
- **Persistent reply keyboard** (`menu_contest`, `menu_status`, `menu_verify`, `menu_language`):
  - sent with a short `welcome` message on `/start`, and after a language change;
  - a router matches these labels in all 3 languages **before** `free_text`, so a winner pressing a menu button is never treated as a claim.
- **Card-style texts** in 3 catalogs:
  - `rules` (inline join + channel buttons), `joined`, `already_joined`, `status_*`, `help`;
  - `win_dm`, the `claim_*` texts, `prize_activated`;
  - new keys: `welcome`, `weekly_notice`, `claim_reminder`, `prize_missed`, `notify_on` / `notify_off` buttons, `notify_muted` / `notify_unmuted` toasts.
- **`notify:on` / `notify:off` callbacks:** set `users.notify`, edit the message's button in place, and answer with a toast. `/status` shows the toggle.
- **Command menus (`handlers/menu.py`):**
  - at startup: `setMyCommands` default (uz-Cyrl) + `language_code=ru`; `setMyDescription` / `setMyShortDescription` likewise;
  - per-chat menus for admins (user + admin commands, in the admin's language);
  - after `/language`, a per-chat menu in the chosen language (admins also get the admin commands);
  - API failures are logged, never fatal.
- **Admin:**
  - `admin_menu` as a card;
  - status names via `status_<state>` keys, also in `admin_current` and `admin_already_done`.

**Unchanged:** channel posts and all draw and claim state transitions.

## Steps
1. Models + Alembic migration (+ `notice_done = true` for redraw children in `redraw`; existing draws are backfilled `notice_done = true`).
2. Publisher: `RetryAfter` handling, `post_link` (Telegram and fake).
3. `Notices` service + tests.
4. Claims `remind_due` and the missed-prize notice + settings + tests.
5. Runner wiring + test.
6. i18n: new and rewritten texts in uz-Cyrl, uz-Latn, ru; description length test.
7. Handlers: reply keyboard + menu router, toggle callback, `/status` toggle, language → per-chat commands, admin polish + tests.
8. `main.py`: startup menus/descriptions, `Notices` wiring.
9. README runbook update; full verification; Docker rebuild and restart of the running test bot.

## Verification
- `uv run pytest`; `uv run ruff check . && uv run ruff format --check .`
- `tests/test_migrations.py` (upgrade, check, downgrade) covers the schema.
- **New tests:**
  - weekly notice: once per entrant, muted users skipped, resume after a partial run, redraw not notified, unreachable users don't stop the run;
  - deadline reminder: not early, once, not after a claim;
  - missed-prize notice: once, even when muted;
  - handlers: a pending winner pressing a menu button is not a claim; the toggle flips the flag and edits the button; `/start` sends the reply keyboard; a language change sets the per-chat commands;
  - catalogs: every key in all 3 languages; bot descriptions within Telegram limits.
- **Manual:** restart the test bot and check the menu, `/start` and `/status` in Telegram.

## Risks and decisions
- **At most once** (claim the flag, then send) was chosen over at least once: a duplicate "you won" or "you missed it" is worse than a rare lost reminder. The win DM is still re-shown on `/start` and `/status`.
- **Redraws** don't re-send the weekly notice (decided in planning; see above).
- **Language code:** Telegram's `uz` code can't tell Cyrillic from Latin. Clients get the Cyrillic default unless they're on `ru`; after `/language` the per-chat menu follows the user's choice.
