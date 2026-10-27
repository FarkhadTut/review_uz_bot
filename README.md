# Review.uz weekly giveaway bot

A Telegram bot for the weekly contest in [@reviewuznews](https://t.me/reviewuznews). Subscribers press «Иштирок этиш» once and are then in every weekly draw. Each draw picks one winner of a 1-year review.uz subscription, and anyone can verify the draw publicly.

- Product: [PRD](product-docs/PRD/PRD-weekly-giveaway-bot.md) · [JTBD](product-docs/JTBD/JTBD-weekly-giveaway-bot.md)
- Plan and status: [tasks/task-2026-10-26-weekly-giveaway-bot](tasks/task-2026-10-26-weekly-giveaway-bot/TASK.md)

## How a draw stays fair

1. An admin closes entries. The bot freezes the snapshot: the eligible participants' entry codes, sorted ascending.
2. The bot posts the commitment to the channel:
   - the participant count;
   - `snapshot_hash = SHA-256(codes joined by "\n", UTF-8, no trailing newline)`;
   - a [drand quicknet](https://drand.love) round that starts at least `BEACON_LEAD_SECONDS` (60 s minimum) after entries close. The commitment is posted only while that round is still at least 15 s away; a round whose post fails or lands late is voided and replaced, publicly.

   Nobody, including us, can know that round's randomness yet.
3. Once the round is out, the randomness is accepted only if **at least 2 of 4 public relays return the same value**, each matching `SHA-256(signature)`, and no other value has as many votes. A relay that is down, broken or lying is outvoted. Then:

   `index = int.from_bytes(SHA256(bytes.fromhex(randomness) + bytes.fromhex(snapshot_hash)), "big") mod N` (0-based)
4. If the candidate at that index is no longer subscribed, the bot moves to `(index + 1) mod N`, and so on. Each skip is recorded with its entry code and reason ("left the channel" or "removed by a channel admin").
5. An admin approves, and the results post shows every input, the skips and any void rounds. Once that post is out, `/verify N` sends the code list (at most once per 30 s per user). [`tools/verify_draw.py`](tools/verify_draw.py) recomputes the hash, checks the randomness from one relay and prints the starting index, using only the standard library; apply the published skips to it by hand.

What can't happen:

- **No cancelling.** A committed draw can't be cancelled; it ends with a public post even if nobody is left to win.
- **Late commitments are voided publicly.** If the commitment post lands too late for its round, that round is declared void in a new post with a later round.
- **API errors aren't "unsubscribed".** A transient Telegram error never counts as "unsubscribed". Only the explicit "user not found" or a left/kicked status does. If Telegram can't answer, the draw pauses and retries by itself; the pause alert doesn't name the candidate.
- **Redraw.** If the winner doesn't send their review.uz email or phone within 48 hours, an admin can redraw. The redraw uses the same snapshot minus that person, with a new round and a new commitment.

Limits:

- The snapshot commits to entry codes, not to identities. Each participant can confirm that their own code (`/status`) is in the list.
- The drand BLS signature itself isn't verified. The bot relies on 2-of-4 relay agreement instead.
- **Known limit: an insider can veto a candidate after the randomness is public.** Someone who is a channel admin *and* can read the bot's database can compute the drawn candidate once the round is out, then remove them from the channel before the bot checks. The skip is published with the candidate's entry code, but a ban followed by an unban shows up as "left the channel". Closing this would need a membership check of every entrant before the round is committed (about one Telegram call per entrant per draw), which was not built; restrict database access and channel-admin rights accordingly.

## Setup

1. Create the bot with @BotFather and **add it as an administrator of the channel**, with permission to post messages. The bot refuses to start otherwise, because `getChatMember` is only reliable for admins.
2. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`, `CHANNEL_ID` and `ADMIN_IDS`.
3. Create the data directory for the container user: `mkdir -p data && sudo chown 10001 data`.
4. Run `docker compose up -d --build`. Migrations run on start, and SQLite lives in `./data/`. The bot refuses to start if the drand relays don't serve the pinned quicknet chain.

Run exactly one instance (SQLite + long polling). The bot enforces this with a lock file next to the database and refuses to start if another process holds it. It only responds in private chats.

## Weekly runbook (admins)

Button labels follow your language: uz-Cyrl by default, and `/language` changes it.

| When | Action |
|------|--------|
| Draw time | `/admin` → «🔒 Иштирокни ёпиш ва танлов ўтказиш» (Close entries) → «Ҳа, тасдиқлайман» (Confirm). The commitment appears in the channel. |
| About a minute later | The bot DMs you the winner. Press «✅ Натижани каналга чиқариш» (Publish result) → confirm. |
| Winner sends their review.uz login | You get a DM with the contact. Activate the subscription on review.uz, then press «🎁 Обуна фаоллаштирилди» (Subscription activated). |
| No reply in 48h | You get a DM. Press «♻️ Қайта танлаш» (Redraw) → confirm. |
| "Paused", "drand", or "commitment not posted" alert | The bot retries by itself (15 s, doubling to 10 min). «🔁 Қайта уриниш» (Retry, under `/admin` → «📋 Жорий танлов») wakes it for an immediate attempt. |
| «❌ … кутилмаган хато» (unexpected error) alert | Automatic retries stopped, for example because the bot lost its rights in the channel. Fix the cause, then press «🔁 Қайта уриниш». |

You can't close next week's entries until the previous draw is **activated**, **redrawn**, or ended with **no winner**.

Other commands: `/stats`, `/staff_add ID`, `/staff_del ID`. Bot admins (`ADMIN_IDS`), staff and channel admins can't win.

## What users see

- **Menu.** A bottom keyboard (🎁 Contest · 📋 My status · 🔎 Verify · 🌐 Language) in the user's language, and a ☰ command menu.
  - The bot sets the ☰ menu and its description on every start: Uzbek Cyrillic by default, Russian for Russian-language Telegram clients.
  - After `/language`, each user's menu follows their choice. Admins also see the admin commands.
  - The profile picture is set by hand in @BotFather.
- **Notifications.**

  | When | Who | Message | Can be muted |
  |------|-----|---------|--------------|
  | Entries close and the commitment is posted | Every entrant in the snapshot | "You're in draw №N", their code, a link to the post. Sent in the background (`NOTICE_PER_SECOND`); redraws don't repeat it. | Yes: 🔕/🔔 button under the message and in `/status` |
  | Result published | Winner | Congratulations + how to claim | No |
  | `CLAIM_REMINDER_HOURS` (12) before the deadline, if unclaimed | Winner | Reminder with hours left | No |
  | Claim window expired | Winner | The prize goes to a redraw | No |
  | Subscription activated | Winner | Prize is active | No |

  Each notice goes out at most once per draw, even across restarts.

## Development

```sh
uv sync
uv run pytest            # unit + service + handler tests (live tests excluded)
uv run pytest -m live    # only the tests that hit the real drand relays
uv run ruff check . && uv run ruff format --check .
uv run alembic revision --autogenerate -m "..."   # after model changes (needs BOT_TOKEN set)
```

The texts live in `src/giveaway_bot/i18n/*.yaml` (uz-Cyrl is the source, and channel posts use it). The uz-Latn and ru catalogs are drafts until staff approve them. A test fails if a language is missing a key or a placeholder.
