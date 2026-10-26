# Review.uz weekly giveaway bot

A Telegram bot for the weekly contest in [@reviewuznews](https://t.me/reviewuznews). Subscribers press «Иштирок этиш» once and are then in every weekly draw. Each draw picks one winner of a 1-year review.uz subscription, and anyone can verify the draw publicly.

- Product: [PRD](product-docs/PRD/PRD-weekly-giveaway-bot.md) · [JTBD](product-docs/JTBD/JTBD-weekly-giveaway-bot.md)
- Plan and status: [tasks/task-2026-10-26-weekly-giveaway-bot](tasks/task-2026-10-26-weekly-giveaway-bot/TASK.md)

## How a draw stays fair

1. An admin closes entries. The bot freezes the snapshot: the eligible participants' entry codes, sorted ascending.
2. The bot posts the commitment to the channel:
   - the participant count;
   - `snapshot_hash = SHA-256(codes joined by "\n", UTF-8, no trailing newline)`;
   - a [drand quicknet](https://drand.love) round that starts at least 60 s **after** the post.

   Nobody, including us, can know that round's randomness yet.
3. Once the round is out, the randomness is accepted only if **at least 2 of 4 public relays return the same value**, each matching `SHA-256(signature)`. Then:

   `index = int.from_bytes(SHA256(bytes.fromhex(randomness) + bytes.fromhex(snapshot_hash)), "big") mod N` (0-based)
4. If the candidate at that index is no longer subscribed, the bot moves to `(index + 1) mod N`, and so on. Each skip is recorded with its entry code and reason ("left the channel" or "removed by a channel admin").
5. An admin approves, and the results post shows every input and every skip. After that, `/verify N` sends the code list, and [`tools/verify_draw.py`](tools/verify_draw.py) recomputes everything using only the standard library.

What can't happen:

- **No cancelling.** A committed draw can't be cancelled; it ends with a public post even if nobody is left to win.
- **Late commitments are voided publicly.** If the commitment post lands too late for its round, that round is declared void in a new post with a later round.
- **API errors aren't "unsubscribed".** A transient Telegram error never counts as "unsubscribed". Only the explicit "user not found" or a left/kicked status does. If Telegram can't answer, the draw pauses and retries by itself; the pause alert doesn't name the candidate.
- **Redraw.** If the winner doesn't send their review.uz email or phone within 48 hours, an admin can redraw. The redraw uses the same snapshot minus that person, with a new round and a new commitment.

Limits:

- The snapshot commits to entry codes, not to identities. Each participant can confirm that their own code (`/status`) is in the list.
- The drand BLS signature itself isn't verified. The bot relies on 2-of-4 relay agreement instead.

## Setup

1. Create the bot with @BotFather and **add it as an administrator of the channel**, with permission to post messages. The bot refuses to start otherwise, because `getChatMember` is only reliable for admins.
2. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`, `CHANNEL_ID` and `ADMIN_IDS`.
3. Create the data directory for the container user: `mkdir -p data && sudo chown 10001 data`.
4. Run `docker compose up -d --build`. Migrations run on start, and SQLite lives in `./data/`. The bot refuses to start if the drand relays don't serve the pinned quicknet chain.

Run exactly one instance (SQLite + long polling). The bot only responds in private chats.

## Weekly runbook (admins)

Button labels follow your language: uz-Cyrl by default, and `/language` changes it.

| When | Action |
|------|--------|
| Draw time | `/admin` → «🔒 Иштирокни ёпиш ва танлов ўтказиш» (Close entries) → «Ҳа, тасдиқлайман» (Confirm). The commitment appears in the channel. |
| About a minute later | The bot DMs you the winner. Press «✅ Натижани каналга чиқариш» (Publish result) → confirm. |
| Winner sends their review.uz login | You get a DM with the contact. Activate the subscription on review.uz, then press «🎁 Обуна фаоллаштирилди» (Subscription activated). |
| No reply in 48h | You get a DM. Press «♻️ Қайта танлаш» (Redraw) → confirm. |
| "Paused", "drand", or "commitment not posted" alert | The bot retries by itself. «🔁 Қайта уриниш» (Retry) forces an immediate attempt. |

You can't close next week's entries until the previous draw is **activated**, **redrawn**, or ended with **no winner**.

Other commands: `/stats`, `/staff_add ID`, `/staff_del ID`. Bot admins (`ADMIN_IDS`), staff and channel admins can't win.

## Development

```sh
uv sync
uv run pytest            # unit + service + handler tests (live tests excluded)
uv run pytest -m live    # only the tests that hit the real drand relays
uv run ruff check . && uv run ruff format --check .
uv run alembic revision --autogenerate -m "..."   # after model changes (needs BOT_TOKEN set)
```

The texts live in `src/giveaway_bot/i18n/*.yaml` (uz-Cyrl is the source, and channel posts use it). The uz-Latn and ru catalogs are drafts until staff approve them. A test fails if a language is missing a key or a placeholder.
