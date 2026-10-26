# Weekly Giveaway Bot (@reviewuznews)

Objective: Ship the Telegram bot specified in the PRD. It handles one-tap entry with a subscription check, an admin-triggered weekly draw that anyone can verify (commit + drand), a 48h prize claim with redraw, an in-bot admin console, and growth/retention stats. Supports uz-Cyrl, uz-Latn and ru.
Stage / status: **Implementation complete locally (plan Steps 1–12). Step 13, staging end-to-end, is blocked on a test bot, test channel and test accounts.** Nothing is committed yet; the git repo was initialised with no commits.

## Context and decisions
- Product source: [PRD](../../product-docs/PRD/PRD-weekly-giveaway-bot.md) · [JTBD](../../product-docs/JTBD/JTBD-weekly-giveaway-bot.md)
- Product decisions (user, 2026-10-26): one press enters every week; past winners are excluded forever; staff and channel admins are excluded; the admin closes entries, which triggers the commit + drand beacon; the snapshot is taken at the moment of closing; the winner has 48h to send their review.uz email/phone, otherwise a redraw; staff activate the prize manually; the contest is open-ended; three languages.
- Tech decisions (user, 2026-10-26): Python + aiogram 3 + SQLite, run as Docker on our Linux server with long polling.
- Planning decisions: see [plan.md § Decisions](plan.md#decisions).
- Open product questions (**not blocking code**): subscriber baseline/target (recorded at launch), Uzbekistan legal check (**blocks the first public draw**), activation SLA wording, draw day/time.

### Implementation deviations (2026-10-26)
- **A no-show winner goes back to `active`** after a redraw, so they can still win a later week. They never received a prize, so "one prize per person" still holds. *Product call: confirm or reverse.*
- **Bot admins (`ADMIN_IDS`) are also excluded from winning**, along with the staff list and channel admins.
- **Results-post failure rolls back** (draw → `drawn`, winner → `active`) so the 48h window never runs on an unannounced result, and the admin can publish again.
- **No separate `db/repo.py`.** Services own their queries, which removes one layer of indirection.
- **Channel posts are in uz-Cyrl only**, and `channel_*` keys exist only in that catalog. Other messages follow the user's language.
- **The commitment round is re-picked when the commitment is posted** (`ensure_committed`), so a failed post can never leave a round that has already been published.
- **Membership**: `RESTRICTED` with `is_member` counts as subscribed. A "user not found" error counts as not subscribed. Any other error → `UNKNOWN`.
- **BLS signature verification (P1) is not implemented.** Randomness is checked as `SHA-256(signature)` and must agree across 2 relays.

## Artifacts
- Active plan: [plan.md](plan.md)
- Code: `src/giveaway_bot/` · tests: `tests/` · verifier: `tools/verify_draw.py` · ops: `Dockerfile`, `compose.yaml`, `.env.example`, `README.md`

## Verification evidence (2026-10-26, working dir repo root, uncommitted tree)
| Check | Command | Result |
|-------|---------|--------|
| Unit + service + handler tests | `uv run pytest` | **58 passed**, 1 live test deselected |
| Live drand (both relays, round 1000) | `uv run pytest -m live` | **1 passed** |
| Lint / format | `uv run ruff check . && uv run ruff format --check .` | clean |
| Migration matches models | `alembic upgrade head && alembic check` | upgraded; "No new upgrade operations detected" |
| Image build | `docker compose build` | built (286MB) |
| Container boot | `docker run … BOT_TOKEN=123:fake` | migrations ran; clean exit "BOT_TOKEN was rejected by Telegram" |
| Tests catch real breakage | Removed the skip-continuation, the claim-deadline boundary and the admin callback filter one at a time | Each made exactly one test fail; code restored |
| **Staging end-to-end** (plan Step 13) | — | **NOT RUN: needs a test bot token, a private test channel with the bot as admin, and 2–3 test accounts** |

Coverage by PRD requirement: entry, subscription checks, entry codes, snapshot/commitment/selection/skip/pause, mandatory completion, results post, claim/48h/redraw, past-winner and staff exclusion, admin auth, i18n parity, `/verify` + script, stats/retention and restart-resume all have automated tests (see `tests/`). The bot-is-admin boot check and the Telegram adapters are covered only by the staging run.

## Progress and next action
- 2026-10-26: Plan written.
- 2026-10-26: Steps 1–12 implemented with the verification above.
- Next:
  1. The user provides a test bot, test channel and accounts → run the Step 13 staging checklist.
  2. Staff approve the uz-Latn/ru texts.
  3. Legal check.
  4. Commit, then review (`/sr`).
