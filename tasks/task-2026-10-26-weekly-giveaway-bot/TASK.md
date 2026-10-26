# Weekly Giveaway Bot (@reviewuznews)

Objective: Ship the Telegram bot specified in the PRD. It handles one-tap entry with a subscription check, an admin-triggered weekly draw that anyone can verify (commit + drand), a 48h prize claim with redraw, an in-bot admin console, and growth/retention stats. Supports uz-Cyrl, uz-Latn and ru.
Stage / status: **Review fixes implemented, not yet committed or re-reviewed** (2026-10-26). `/sr` on `85f55cd` found 2 critical and 12 major; C1–C2 and M1–M9 are fixed with regression tests (see [review revision 2](code-review-85f55cd.md)). M10–M12 are partly addressed. Step 13, staging end-to-end, is still blocked on a test bot, test channel and test accounts.

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
- **Commitment round**: the round chosen at close is kept while it is still ≥ 15 s ahead. If it isn't (a failed post was retried late, or the post itself landed after the round), the round is **voided publicly**: it is stored in `Draw.void_rounds` and named in the replacing post.
- **Membership**: `RESTRICTED` with `is_member` counts as subscribed. Only the exact Bot API texts "user not found" / `PARTICIPANT_ID_INVALID` count as not subscribed. `KICKED` → `BANNED` (skipped, with a distinct public reason). Every other API error, including "chat not found", and rate limits over 2 s in a user request → `UNKNOWN`.
- **BLS signature verification (P1) is not implemented.** Randomness must be returned identically by ≥ 2 of 4 relays and equal SHA-256(signature).
- **Pauses retry automatically** with backoff (15 s → 10 min). The admin Retry button only forces an immediate attempt and is audited.
- **A committed draw always ends publicly**: a draw where everyone was skipped, and a redraw with nobody left, both get a channel post.
- **`/verify` shows results only once announced** (PUBLISHED or later); before that it says the result is pending.
- **Private chats only** for both user and admin handlers.
- **Winners are reminded**: `/start` and `/status` resend the claim instructions while a claim is open.

## Artifacts
- Active plan: [plan.md](plan.md)
- Code review: [code-review-85f55cd.md](code-review-85f55cd.md). Revision 1: NEEDS FIXES. Revision 2: fix log; re-review pending.
- Code: `src/giveaway_bot/` · tests: `tests/` · verifier: `tools/verify_draw.py` · ops: `Dockerfile`, `compose.yaml`, `.env.example`, `README.md`

## Verification evidence

### After review fixes (2026-10-26, working tree on top of `85f55cd`, uncommitted)
| Check | Command | Result |
|-------|---------|--------|
| Unit + service + handler tests | `uv run pytest` | **101 passed**, 1 live test deselected |
| Live drand, 4 default relays, quorum 2 | `uv run pytest -m live` | **1 passed** |
| Lint / format | `uv run ruff check . && uv run ruff format --check .` | clean |
| Migrations (initial + `void_rounds`) | `alembic upgrade head && alembic check` | both applied; no drift |
| Image build + boot | `docker compose build`; `docker run … BOT_TOKEN=123:fake` | built; both migrations ran; clean exit "BOT_TOKEN was rejected by Telegram" |
| Race reproduction (reviewer script `race.py`) | double approve | **1 results post** (was 2) |
| Tests catch the race | Removed the status guard from `_cas` | exactly the 5 double-press tests failed; code restored |
| **Staging end-to-end** | — | **NOT RUN** (same blocker as before) |

### Initial implementation (2026-10-26, commit `85f55cd`)
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
- 2026-10-26: Committed `85f55cd`. `/sr` full review: NEEDS FIXES. Critical findings:
  - C1: status transitions aren't atomic (double approve/redraw reproduced).
  - C2: redraw runs in two transactions.

  Majors include a stuck COMMITTED draw after a failed post, "chat not found" being treated as unsubscribed, a single drand relay accepted, admin-steerable skips, no post for no-winner draws, and crash windows with no resume.
- 2026-10-26: Fixed C1–C2 and M1–M9 with regression tests (101 tests). Also fixed: beacon timing pinned, the commit round is ≥ lead ahead, `/verify` gating and parsing, the private-chat filter, defensive admin callbacks with feedback, the verifier script's round check, the stale-admin-cache fallback, and the docs (README, .env.example, plan).
- Next:
  0. Commit the fixes, then run `/sr` again on the new commit.
  1. The user provides a test bot, test channel and accounts → run the Step 13 staging checklist.
  2. Staff approve the uz-Latn/ru texts.
  3. Legal check.
