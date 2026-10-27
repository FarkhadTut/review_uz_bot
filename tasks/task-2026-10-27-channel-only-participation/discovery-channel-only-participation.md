# Discovery: One-tap participation from the channel

**Created**: 2026-10-27
**Status**: Draft
**Task context**: [TASK.md](TASK.md)

---

## Feature Overview
People join the weekly giveaway straight from the channel. Channel posts carry a **«🎁 Иштирок этиш»** button. Tapping it opens the bot. One tap on Telegram's Start button enters the person automatically: no language question and no join button.

Admins publish a giveaway announcement post with the button from `/admin`. The bot's own weekly posts (entries closed, results) carry the same button.

## Why This Exists
- **Bot friction costs entries.** The owner wants people to take part without "going through the bot". Today a newcomer must find the bot, press Start, pick a language, and only then see that they are in.
- **The ideal flow isn't possible.** Telegram has no "message visible to one person inside a channel" and no subscriber list (see Constraints). A button on the channel post that ends in one Start tap is the closest Telegram allows, and it keeps what only the bot can do:
  - a fair, known pool;
  - messaging the winner;
  - reminders;
  - collecting the contact.
- **Value:**
  - more entries from the same channel audience, which serves the PRD KPI of channel growth and retention;
  - the bot stays invisible apart from that one tap.

## Usage Context
- **Who:**
  - channel subscribers (mostly Telegram mobile, uz-Cyrl audience);
  - 1–3 admins.
- **When:**
  - a subscriber sees the pinned announcement or a weekly bot post and taps the button;
  - an admin publishes or re-words the announcement.
- **Context:**
  - The original announcement in the channel was posted by hand, so it has no button. It tells people to press «Иштирок этиш» in the bot, a button that no longer exists.
  - Auto-enrolment on the rules card is already live (commit `a572f76`).

## Chosen Direction
**One tap from the channel**, chosen by the owner on 2026-10-27. Rejected alternatives:
- **Per-person message inside the channel:** impossible in Telegram.
- **Admins pick from the subscriber list:** no list is available to bots or admins, and picking by hand isn't verifiable.
- **Comment to enter:** needs comments, and winners can't be messaged.
- **Automatically enter new subscribers only:** subscribers from before the bot are invisible to it, and winners can't be messaged.

## How It Works
- **Entry points:**
  - the button on the announcement post;
  - the button on each entries-closed and results post;
  - `/start` as before.
- **Core flow:**
  1. The admin opens `/admin` → «📣 Announcement» and sees the current text: the default, or the last saved version.
  2. Optionally the admin taps «✏️ Change text» and replies with the new text. It is saved and used for future announcement posts.
  3. The admin taps «📤 Publish». The bot posts the text plus the «🎁 Иштирок этиш» button to the channel, then offers «📌 Pin».
  4. A subscriber taps the button. The bot opens with the deep-link start parameter, and the subscriber taps Start.
  5. The bot sets the language from the Telegram app's language (ru → Russian, otherwise uz-Cyrl) without asking. It shows the rules card, which enters a subscriber and shows their code.
- **Key states:**
  - **New user:** language picked automatically, then entered.
  - **Returning user:** keeps their language, sees their entry state.
  - **Not subscribed:** «Subscribe» + «I subscribed» (existing).
  - **Past winner, staff or admin:** the reason only (existing).
  - **Telegram couldn't check:** retry button (existing).
- **Important edge cases:**
  - An Uzbek Telegram app can't tell Cyrillic from Latin, so it gets Cyrillic. The 🌐 menu button still changes the language.
  - An admin replying with an empty or over-long text: the bot refuses and asks again.
  - Publishing or pinning fails, for example because the bot lacks the right: the admin is told why.
  - Changing the text never touches posts already in the channel (owner decision).
  - A tap from a forwarded copy of the post in another chat works the same.

## In Scope
- [ ] A «🎁 Иштирок этиш» URL button (deep link into the bot) on:
  - the announcement post;
  - the entries-closed posts;
  - the results posts.
- [ ] The no-winner post too, for consistency (assumption; cheap to drop).
- [ ] `/admin` → announcement screen:
  - preview of the current text;
  - «✏️ Change text» (reply prompt, survives restarts like the staff prompt);
  - «📤 Publish»;
  - «📌 Pin» after publishing.
- [ ] A default announcement text (owner's original contest text, rewritten for one tap), used until an admin saves their own.
- [ ] No language prompt on `/start`: the language comes from the Telegram app, and 🌐 still changes it.
- [ ] Entries from the button are recorded with a distinct source in the existing `source` field, so stats can tell button entries apart.
- [ ] The "didn't make it? open @bot" line in channel posts is replaced by the button.

## Out of Scope
- [ ] Per-user messages inside the channel (impossible).
- [ ] Reading the channel's subscriber list, or picking winners by hand.
- [ ] Comment-to-enter, and automatically entering new subscribers from join events.
- [ ] Announcement posts with a photo or video (text only for now).
- [ ] Editing announcements already published (owner decision: new text applies to future posts only).
- [ ] Separate announcement texts per language (the channel is uz-Cyrl).

## Key Requirements
- [ ] One tap after opening the bot (Start) is enough to be entered, for a subscribed user.
- [ ] Only bot-published channel posts get the button. The announcement is published by the bot from `/admin`.
- [ ] Admins can replace the announcement text from `/admin` without a code change. The saved text survives restarts. Formatting (bold, links) is kept.
- [ ] The bot never publishes, pins or changes channel posts without an explicit admin confirmation.
- [ ] Existing behaviours stay:
  - auto-enrolment;
  - the subscribe nudge;
  - the past-winner and excluded cards;
  - the winner flow (DM, contact sharing, reminders).

## Constraints
- **No per-user channel messages.**
  - Ephemeral "visible to you only" messages (Bot API 10.2, `ephemeral_message_parameters`) exist only in groups, as answers to a user's action. Channels have no equivalent.
  - Bots can't message users who never started them. The only exception is the 5-minute window after a join request, which needs approval-to-join.
- **No subscriber list.** Bots can only check a known user (`getChatMember`) or get the count. The Telegram app shows owners about the 200 newest subscribers.
- **Buttons only on bot posts.** Inline buttons can only be attached to messages the bot itself sends, so the existing manual announcement can't get one. Admins should publish the bot version and unpin or delete the old one.
- **Pin rights.** Pinning in a channel needs the bot's channel admin right to edit messages (to be checked at planning; failure is reported, not fatal).
- **Language guess.** Language detection can only tell Russian from "everything else". Cyrillic is the default, matching the channel.
- **Load.** The entry check stays one `getChatMember` per entry. A burst of taps after a post is bounded by Telegram's limits, to be confirmed at planning.

## Notes
- **Sources for the platform facts:**
  - [Bot API changelog (10.2 ephemeral group messages)](https://core.telegram.org/bots/api-changelog.md)
  - [sendMessage reference](https://core.telegram.org/bots/api/methods/sendmessage.md)
  - [aiogram ChatJoinRequest docs (5-minute window)](https://docs.aiogram.dev/en/dev-3.x/api/types/chat_join_request.html)
  - [Telegram blog: ephemeral bot messages in groups](https://telegram.org/blog/communities-editor-invisible-messages)
  - [subscriber export limits](https://okhlopkov.com/how-to-get-a-telegram-channel-subscribers-list-in-python/)
  - [BotMother on membership checks](https://docs.botmother.com/article/46900)
- **Grill summary:**
  - The default text must stop saying "press «Иштирок этиш» in the bot".
  - The no-winner post gets the button too, as an assumption.
  - Language auto-pick replaces the prompt for every `/start`, not only deep links.
  - The edit-text prompt must survive restarts, so it uses the same reply-prompt approach as the staff screen.
- **Proposed glossary terms** (no glossary file exists yet; not created):
  - **Announcement post:** the bot-published giveaway post with the button.
  - **Participation button:** «🎁 Иштирок этиш», a deep link to the bot.
  - **One-tap entry:** Start from the button enters a subscribed user.
