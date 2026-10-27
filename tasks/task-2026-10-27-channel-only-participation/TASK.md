---
status: implemented
created: 2026-10-27
---
# Task: participation without opening the bot

## Objective
People take part in the weekly giveaway from the channel alone, without having to open the bot. The bot becomes mainly a moderation and administration tool.

## Owner's idea (2026-10-27)
- **Ideal:** someone opens the channel and the bot shows them a participation message inside the channel, visible only to them.
- **Fallback:** admins pick winners themselves from the channel's subscriber list. The bot is then only for moderation and administration.

## Feasibility findings (Telegram Bot API, checked 2026-10-27)
- **No per-user messages in a channel.** Ephemeral "visible only to you" bot messages exist (Bot API 10.2), but only in **groups**, and they answer a user's action (a command or button press). Channels have nothing like it.
- **No messages to strangers.** A bot can't message a subscriber who never opened it. One exception: within 5 minutes of a **join request**, which only exists if the channel requires approval to join.
- **No subscriber list.**
  - The Bot API can only check a known user (`getChatMember`) or return the count (`getChatMemberCount`).
  - The Telegram app shows owners only about the 200 most recent subscribers.
  - Listing more needs a user account through MTProto (a "userbot"), which is partial and against the spirit of the Bot API.
- **The bot sees joins and leaves, but only going forward.** It receives `chat_member` updates from the moment it is an admin, so it can collect **new** subscribers automatically. People who subscribed earlier are invisible until they interact.
- **The linked discussion group works.** If the channel has comments, a bot in the discussion group sees every commenter's user ID, and can answer with an ephemeral message.

## Status
Discovery complete (2026-10-27): [discovery-channel-only-participation.md](discovery-channel-only-participation.md).

**Cross-AI validation:** skipped. The Codex CLI isn't authenticated, the same as in earlier tasks. This is not a pass.

**Owner decisions:**
- **Direction:** one tap from the channel. A «🎁 Иштирок этиш» URL button deep-links into the bot; Start enters a subscribed user with no language prompt.
- **Button placement:** the announcement post (published from `/admin`), the entries-closed posts and the results posts.
- **Announcement text:** a default text built into the bot, which admins can replace from `/admin`. A new text applies to future posts only; published posts are never edited.

**Rejected:**
- a per-user message in the channel (impossible);
- picking winners by hand from the subscriber list (no list exists);
- comment to enter;
- automatic entry for new subscribers.

**Open items:** none blocking. Assumptions recorded in the discovery: the no-winner post also gets the button; announcements are text only.

**Plan ready (2026-10-27):** [plan.md](plan.md), 9 steps with a verification table. No blocking decisions; risks are pin rights and the language guess.

**Implemented (2026-10-27):** all plan steps 1–9.
- **Button:** `services/announcements.py` defines `participate_keyboard`; every bot channel post carries it.
- **Announcement:** a `settings` table plus migration `c4f2a8d1e6b3`, the `Announcements` service, and the `/admin` «📣» screen (preview, change text by reply, publish after confirm, pin).
- **`/start`:** picks the language with no prompt.
- **Texts:** in all three languages; README updated.

**Verification** (repository root):
- `uv run pytest -q`: 176 passed, 1 deselected.
- `uv run pytest -q -m live`: 1 passed.
- ruff check and format: clean.
- **Mutation check:** all 8 caught.
  - commit-post button
  - results-post button
  - language prompt
  - ru detection
  - publish without confirm
  - text not persisted
  - empty text accepted
  - formatting dropped
- **Docker rebuild:** migration `b7e1c4d2a9f0 -> c4f2a8d1e6b3` applied, and the bot polls.

**Not verified:**
- publishing and pinning in the real test channel, and tapping the button from a second account (manual, by the owner);
- whether the bot has the pin right in the test channel.

## Next action
Owner: `/admin` → «📣» → publish and pin in the test channel, then tap the button from a second account. After that, `/sr` review.

## Links
- Parent: [../task-2026-10-26-weekly-giveaway-bot/TASK.md](../task-2026-10-26-weekly-giveaway-bot/TASK.md)
- PRD: [../../product-docs/PRD/PRD-weekly-giveaway-bot.md](../../product-docs/PRD/PRD-weekly-giveaway-bot.md)
