# Review.uz weekly giveaway bot

A Telegram bot for the weekly contest in [@reviewuznews](https://t.me/reviewuznews). Subscribers press «Иштирок этиш» once and are then in every weekly draw. Each draw picks one winner of a 1-year review.uz subscription, and anyone can verify the draw publicly.

- Product: [PRD](product-docs/PRD/PRD-weekly-giveaway-bot.md) · [JTBD](product-docs/JTBD/JTBD-weekly-giveaway-bot.md)
- Plan and status: [tasks/task-2026-10-26-weekly-giveaway-bot](tasks/task-2026-10-26-weekly-giveaway-bot/TASK.md)

## How a draw stays fair

1. An admin presses **Close entries**. The bot freezes the snapshot: the eligible participants' entry codes, sorted.
2. The bot posts to the channel: the participant count, `SHA-256(codes joined by "\n")`, and a **future** [drand quicknet](https://drand.love) round. Nobody, including us, can know that round's randomness yet.
3. Once the round is published: `index = int(SHA256(randomness ‖ snapshot_hash)) mod N`. If that candidate has left the channel, the bot takes the next index. Each skip is published.
4. An admin approves, and the bot posts the results with every input. `/verify N` sends the code list; [`tools/verify_draw.py`](tools/verify_draw.py) recomputes everything using only the standard library.

A committed draw can't be cancelled. If Telegram can't confirm a candidate's subscription, the draw **pauses**; an API error never counts as "unsubscribed". If the winner doesn't send their review.uz email or phone within 48 hours, an admin can redraw. The redraw uses the same snapshot minus that person, with a new round and a new commitment post.

## Setup

1. Create the bot with @BotFather and **add it as an administrator of the channel**, with permission to post messages. The bot refuses to start otherwise, because `getChatMember` is only reliable for admins.
2. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`, `CHANNEL_ID` and `ADMIN_IDS`.
3. Run `docker compose up -d --build`. Migrations run on start, and SQLite lives in `./data/`.

Run exactly one instance (SQLite + long polling).

## Weekly runbook (admins)

| When | Action |
|------|--------|
| Draw time | `/admin` → **Close entries** → confirm. The commitment post appears in the channel. |
| About a minute later | The bot DMs you the winner. Press **Publish result** → confirm. |
| Winner sends their review.uz login | You get a DM with the contact. Activate the subscription on review.uz, then press **Subscription activated**. |
| No reply in 48h | You get a DM. Press **Redraw** → confirm. |
| Paused draw | Press **Retry** once Telegram is reachable. |

Other commands: `/stats`, `/staff_add ID`, `/staff_del ID`. Staff and channel admins can't win.

## Development

```sh
uv sync
uv run pytest            # unit + service + handler tests
uv run pytest -m live    # also hits the real drand relays
uv run ruff check . && uv run ruff format --check .
uv run alembic revision --autogenerate -m "..."   # after model changes (needs BOT_TOKEN set)
```

The texts live in `src/giveaway_bot/i18n/*.yaml` (uz-Cyrl is the source, and channel posts use it). The uz-Latn and ru catalogs are drafts until staff approve them. A test fails if a language is missing a key or a placeholder.
