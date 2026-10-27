---
status: discovery
created: 2026-10-27
---
# Task: bot polish and winner notifications

## Objective
Make the bot's messages, menus and commands look polished, and close the gaps in winner/participant notifications.

## Current state (observed 2026-10-27)
- Winner DM exists: `win_dm` is sent when the admin publishes the result (`services/draw_service.py`, `finish_publishing`), and re-shown on `/start` and `/status` while the claim is pending (`handlers/user.py::_remind_claim`). The admin is told whether delivery worked.
- After activation the winner gets `prize_activated`.
- Gaps: no reminder before the 48 h deadline; no message to the winner when the deadline passes or a redraw happens; entrants get nothing when a draw closes or results are out.
- Commands: `set_my_commands` registers one menu with all three languages mashed into each description (`main.py`). No bot description / short description. `/help` is a bare list. No persistent menu buttons.

## Status
Discovery complete (2026-10-27): [discovery-bot-polish-notifications.md](discovery-bot-polish-notifications.md). Cross-AI validation skipped (no authenticated validator).

User decisions:
- **UI:** menu buttons + cards, with a persistent 4-button keyboard.
- **Admin:** light polish.
- **Notifications:** a deadline reminder 12 h before the deadline, a missed-prize notice, and a weekly "you're in this draw" notice. Every such message has an inline mute/unmute toggle; winner messages are always sent.

Plan: [plan.md](plan.md) (2026-10-27).

**Implemented (2026-10-27):** all plan steps 1–9.
- Migration `b7e1c4d2a9f0`; `services/notices.py`.
- `Claims.remind_due` and the missed-prize notice.
- Runner wiring; `handlers/menu.py`; the reply menu, mute toggle and status card in `handlers/user.py`.
- Admin status words.
- All 3 catalogs rewritten as cards.
- README "What users see" and `.env.example`.

**Decisions made in planning:**
- Notices are claimed before sending (at most once).
- Redraws don't repeat the weekly notice.
- The bottom menu travels on its own short `welcome` message, because a message can't carry both a reply keyboard and inline buttons.

**Verification:**
- `uv run pytest`: 154 passed, 1 deselected. `uv run pytest -m live`: 1 passed. ruff check and format: clean.
- Mutation check (break each feature, run its tests). Caught:
  - menu routing (3 fail)
  - mute ignored (1)
  - reminder flag (1)
  - missed-prize on redrawn (1)
  - redraw child notified (1)
  - toggle button (1)
  - Two tests were added after the first pass missed the reminder-flag and redraw cases.
  - "No per-entry claim" makes the notice loop spin forever; it was dropped from the automated run.
- Docker rebuild on the test bot: migration `3454fbb593df -> b7e1c4d2a9f0` applied, the bot polls, and setting the menus and description logged no errors.

**Not verified:** how it looks in a real Telegram client (menu, cards, toggle), and a full weekly-notice run on the test channel. The uz-Latn and ru wording still needs staff approval.

## Next action
Manual check in Telegram on the test bot, then `/sr` review of the commit.

## Links
- Parent bot task: [../task-2026-10-26-weekly-giveaway-bot/TASK.md](../task-2026-10-26-weekly-giveaway-bot/TASK.md)
