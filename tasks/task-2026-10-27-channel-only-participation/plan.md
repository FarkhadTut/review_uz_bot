# Plan: one-tap participation from the channel

**Created**: 2026-10-27
**Source**: [discovery-channel-only-participation.md](discovery-channel-only-participation.md). Owner decisions are in [TASK.md](TASK.md).

## Changes

**Participation button**
- Every channel post the bot publishes carries one URL button, «🎁 Иштирок этиш» → `https://t.me/<bot>?start=channel`. These are:
  - the announcement post;
  - entries-closed (commit) posts, including redraw commitments;
  - results posts;
  - no-winner posts;
  - the redraw-empty post.
- The `channel` start parameter goes into the existing `users.source`, so button entries are recognisable.
- The bot already enters a subscriber when it shows the rules card (`a572f76`). The only new step is skipping the language prompt.

**No language prompt**
- `/start` for a user with no language sets it from the Telegram app's `language_code`: `ru` → `ru`, everything else → `uz_cyrl`. This reuses the `handlers/menu.py` `CLIENT_LANGS` mapping, moved behind a small `lang_for(code)` helper.
- The same `/start` also sets that chat's command menu.
- `/language` and 🌐 still offer the choice.

**Announcement**
- A new `settings` key-value table (`key`, `value`, `updated_at`, `updated_by`) holds the admin's announcement text as HTML. No row means the default text, `channel_announcement` (uz-Cyrl, channel-only key).
- A new service `services/announcements.py` provides:
  - `participate_keyboard(bot_username)`, also used by `DrawService`;
  - `Announcements.text()`, `set_text(html, admin_id)`, `publish(admin_id) -> msg_id`, `pin(msg_id)`.
  - Writes are audit-logged (`announce_text`, `announce_publish`, `announce_pin`).
- **Publisher port:**
  - `post_channel(html, keyboard=None)`;
  - a new `pin(msg_id)`.
  - `FakePublisher` records the keyboards of channel posts and pinned IDs.
- **`/admin`:** a new «📣 Эълон» row opens the announcement screen:
  - a preview of the current text;
  - «✏️ Матнни ўзгартириш» (a reply prompt, stateless, like the staff prompt: the reply-prompt filter is generalised to a prompt-text → kind map);
  - «📤 Каналга чиқариш» → confirm → publish → «📌 Закреплаш».
  - New text applies to future posts only. Published posts are never edited (owner decision).

**Text cleanup**
- `channel_commit` loses the «Улгурмадингизми? {bot_link}…» line; the button replaces it.
- The results post keeps the winner's "open the bot" line.

**Files**
- `db/models.py`
- new migration `migrations/versions/<rev>_settings.py`
- `ports/publisher.py`
- new `services/announcements.py`
- `services/draw_service.py`
- `handlers/user.py`
- `handlers/menu.py`
- `handlers/admin.py`
- `main.py` (wiring)
- the three `i18n/*.yaml` files
- `README.md`

## Steps
1. **Model and migration.** Add the `Setting` model and an Alembic revision after `b7e1c4d2a9f0` that creates `settings`. `tests/test_migrations.py` must stay green (it runs `alembic check`).
2. **Publisher.** Add `post_channel(html, keyboard)` and `pin(msg_id)` to the Protocol, `TelegramPublisher` (`send_message(..., reply_markup=to_markup(kb))`, `pin_chat_message(channel, msg_id, disable_notification=True)`) and `FakePublisher`.
3. **Announcements service.** Create `services/announcements.py`.
   - `set_text` rejects empty text and text over 4000 characters (Telegram's limit is 4096 after parsing).
   - Publish and pin let `TelegramAPIError` propagate to the caller.
4. **DrawService.** Pass `participate_keyboard(self.bot_username)` to all five `post_channel` calls, and drop `bot_link` from `channel_commit`.
5. **`/start` language.** New users get a language without the prompt, plus their chat's command menu (`set_chat_commands`). `start` needs `bot` and `admin_ids`, which the dispatcher already injects.
6. **Admin.**
   - Add the panel row and the actions `announce`, `announcetext`, `publish`, `publish!` and `pin:<msg_id>`.
   - Generalise the reply-prompt handler: the staff ID prompt and the announcement text prompt. The announcement text is read from `message.html_text`, so formatting survives.
   - Failures to publish or pin are reported to the admin with the Telegram error, never raised.
7. **Wiring.** Create `Announcements` in `main.py`, inject it into the dispatcher and the test `app` fixture.
8. **Texts in three languages.** New strings:
   - `btn_announce`, `admin_announce` (preview header);
   - `btn_announce_text`, `admin_announce_ask`, `admin_announce_bad`, `admin_announce_saved`;
   - `btn_publish`, `admin_announce_published`, `btn_pin`, `admin_pinned`, `admin_publish_error`;
   - channel-only, uz-Cyrl: `channel_btn_participate`, `channel_announcement` (the owner's original announcement, step 2 rewritten as «пастдаги «🎁 Иштирок этиш» тугмасини босинг ва Start'ни босинг — тамом»).
9. **Docs.** Update the README runbook: publish the announcement from `/admin`, then unpin or delete the old manual post. Note the pin right needed.

## Verification
Planned: TDD, red first, using the project commands from the repo root: `uv run pytest -q`, `uv run ruff check src tests`, `uv run ruff format --check src tests`.

| Requirement | Check |
|---|---|
| Button on every bot channel post | `test_draw_service`: commit, results and no-winner posts recorded by `FakePublisher` carry one URL button to `t.me/<bot>?start=channel`. `test_draw_safety`: the redraw-empty post does too. |
| One tap enters a subscribed user | `test_handlers`: `/start channel` from a subscribed user with no language returns the rules card with `rules_in` and the code, never `lang_prompt`. `source == "channel"`. Language is `ru` for `language_code="ru"` and `uz_cyrl` otherwise. |
| Admin replaces the text, persisted | `test_announcements`: `set_text` stores HTML, `text()` returns it, a fresh service instance (same DB) still returns it, and the default is used when unset. Empty or too-long text is rejected. |
| Publish only on explicit confirm, then pin | `test_handlers`: `adm:announce` shows a preview, `adm:publish` only asks to confirm (no channel post yet), `adm:publish!` posts once with the button and offers `adm:pin:<id>`. Pressing pin records it. A failing `FakePublisher` produces the error text. |
| Text edit via reply, formatting kept | `test_handlers`: reply to the prompt with a message that has bold entities → stored with `<b>`. A non-admin reply is ignored. |
| New text only for future posts | `test_handlers`: after publishing, changing the text produces no edit request on the published post. |
| Existing behaviour unchanged | The full suite stays green, in particular the auto-enrolment, subscribe-nudge, winner-flow and i18n parity tests. |
| Schema | `tests/test_migrations.py` (upgrade to head plus `alembic check`). |

**Manual check on the test bot after `docker compose up -d --build`:**
- publish and pin the announcement in the test channel;
- tap the button from a second account: Start, then entered;
- run a draw and check the buttons on its posts.

## Risks and decisions
- **Pin right.** Pinning needs the bot's channel right to edit or pin messages. Without it, pin fails and the admin is told; publishing still works.
- **Language guess.** Uzbek-language apps can't be told apart (Latin or Cyrillic), so they get Cyrillic (matching the channel). 🌐 changes it.
- **Bursts.** A tap burst after a post means one `getChatMember` per new entrant, as today. No new limits are needed; flood waits on sends are already handled in `TelegramPublisher.send`.
- **Old manual post.** The existing manual announcement can't get a button (Telegram allows buttons only on the bot's own posts). Admins replace it; the runbook says so.
- **Decision: a key-value `settings` table, not a file.** It survives container rebuilds via the existing `data/` volume, is audit-logged, and doesn't need a second persistence mechanism.
- **Decision: reuse the stateless reply-prompt pattern.** Nothing is lost on restart, and it matches the staff screen.
