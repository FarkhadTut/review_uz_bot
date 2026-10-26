# Plan: Weekly Giveaway Bot

Source requirements: [PRD](../../product-docs/PRD/PRD-weekly-giveaway-bot.md). The requirements table is cited below as *PRD: <requirement name>*.

## Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Runtime | Python 3.12, aiogram 3, managed with `uv` | User's choice. The 3.12 image is stable in Docker (local 3.14 is fine for dev) |
| Persistence | SQLite (WAL) via SQLAlchemy 2 async + aiosqlite; Alembic migrations from day one | One file on a Docker volume. Schema will change after launch |
| Transport | Long polling, single process | No domain or TLS needed. One process keeps SQLite writes simple |
| Background work | asyncio tasks started on boot, driven by persisted draw/claim state (no APScheduler) | Every wait (beacon round, 48h expiry, retention batch) is derived from DB state, so a restart resumes correctly |
| Randomness | drand **quicknet** (3s period) over HTTP. Fetch the round from 2 relays (`api.drand.sh`, `drand.cloudflare.com`) and require identical randomness | Public and pre-committable. Two relays guard against a single faulty relay. BLS signature verification is P1. Chain hash/genesis/period are read from `/info` at boot and checked against config |
| Snapshot hash | `SHA-256` of the participants' entry codes sorted ascending and joined with `\n` (UTF-8, no trailing newline), as hex | Simple to recompute in any language |
| Winner index | `int.from_bytes(SHA256(bytes.fromhex(randomness) ‖ bytes.fromhex(snapshot_hash)), "big") mod N` | Modulo bias is negligible at 256 bits. Deterministic |
| Skip rule | If the candidate at `i` is not subscribed, try `(i+1) mod N` and so on. `unknown` membership (API error after retries) **pauses** the draw and alerts admins; it is never a skip | Matches PRD: Subscription re-check at draw, and "never treat error as unsubscribed" |
| Entry code | 8 characters, Crockford base32, from `secrets`; unique index | Not derived from the Telegram ID, so the published list isn't reversible |
| Redraw | A new `Draw` row whose `parent_id` points to the original. Its snapshot = parent snapshot minus the no-show (new hash), with a new future round and a new commitment post | Every link in the proof chain is public |
| Excluded at close | Past winners, staff list, and channel admins from `getChatAdministrators` at close time | PRD: Staff exclusion, Past Winners excluded |
| i18n | One flat key→text YAML catalog per locale (`uz_cyrl`, `uz_latn`, `ru`), plus a test that all key sets match | No gettext build step. Missing keys fail CI |
| Admin auth | `ADMIN_IDS` from env. aiogram router filter on every admin handler and callback | PRD: Admin access |

## Changes (proposed layout)

```
pyproject.toml, uv.lock, alembic.ini, Dockerfile, compose.yaml, .env.example, README.md
src/giveaway_bot/
  main.py            # boot: config, DB, health check (bot is channel admin), routers, resume tasks, polling
  config.py          # pydantic-settings: BOT_TOKEN, CHANNEL_ID, ADMIN_IDS, DB_PATH, DRAND_*, CLAIM_WINDOW_H=48
  db/models.py       # User, Staff, Draw, DrawEntry, Claim, MembershipCheck, AuditLog
  db/repo.py
  domain/draw.py     # pure: snapshot_hash, winner_index, walk_candidates, mask_name
  domain/codes.py    # entry codes; contact validation (email / +998 phone)
  ports/membership.py  # is_subscribed -> YES|NO|UNKNOWN; TelegramMembership + FakeMembership
  ports/beacon.py      # round_at(t), randomness(round); DrandBeacon + FakeBeacon
  services/participation.py, draw_service.py, claims.py, stats.py, audit.py
  handlers/user.py, handlers/claim.py, handlers/admin.py
  i18n/{uz_cyrl,uz_latn,ru}.yaml, i18n/__init__.py
migrations/          # alembic
tools/verify_draw.py # standalone stdlib script: recomputes hash, round fetch, index, given the published list
tests/
```

**Data model (core fields)**
- `User`: tg_id PK, first_name, username, lang, first_start_at, member_at_first_start (bool|null), source, entry_code (unique, null until entered), entered_at, status `active|won|excluded`
- `Staff`: tg_id, added_by, added_at
- `Draw`: id, parent_id, status `committed|drawn|published|claimed|activated|expired|redrawn|paused`, closed_at, n, snapshot_hash, beacon_round, randomness, winner_index, skipped (JSON `[{index, reason}]`), winner_tg_id, commit_msg_id, result_msg_id, published_at
- `DrawEntry`: draw_id, position, entry_code, tg_id (immutable after insert)
- `Claim`: draw_id, contact, submitted_at, activated_at, activated_by
- `MembershipCheck`: tg_id, kind `retention30`, result, checked_at
- `AuditLog`: admin_id, action, payload JSON, at

## Steps

1. **Scaffold.** Run `git init`, `uv init --package`, and add deps (aiogram 3, sqlalchemy[asyncio], aiosqlite, alembic, pydantic-settings, httpx, pyyaml) plus dev deps (pytest, pytest-asyncio, ruff). Add config, logging and `.env.example`. Add `.gitignore` covering `.env` and `*.db`.
2. **Domain core (TDD).** `domain/draw.py` and `domain/codes.py` with golden-vector tests: a fixed code list gives a known hash, and fixed randomness gives a known index. Cover the walk with skips, all-skipped → no winner, masking with and without a username, and contact validation. *(PRD: Verifiable selection, Entry code)*
3. **Persistence.** Models, Alembic initial migration, repo functions, and DB-level uniqueness on `entry_code`. *(PRD: Entry, Draw snapshot immutable)*
4. **i18n.** Three catalogs with all keys, a `t(lang, key, **kw)` helper and the key-parity test. The uz-Cyrl rules text mirrors the announcement; uz-Latn and ru are drafts pending approval.
5. **Ports.** `TelegramMembership` maps member/administrator/creator → YES and left/kicked → NO; errors are retried 3× and then return UNKNOWN. `DrandBeacon` covers `round_at(t)`, `randomness(round)` with two-relay agreement and wait-until-published, and an `/info` check at boot. Fakes are provided for tests. *(PRD: Subscription check, Randomness beacon)*
6. **Participation + user handlers.** `/start` (deep-link source, language picker the first time, `member_at_first_start` recorded), the rules screen, «Иштирок этиш» (idempotent enter, assigns the entry code, not-subscribed → Subscribe + Check again, staff/admin → not eligible), `/status` (shows the entry code) and `/language`. *(PRD: Entry, Subscription check at entry, Languages, /status)*
7. **Draw service.** `close_entries` builds the snapshot under one transaction, takes `round = round_at(now + 60s)` and posts the commitment. A resume task waits for the round, then `select` with re-checks; UNKNOWN → `paused` plus an admin alert with a Retry button. The admin sees a preview and approves; the bot posts the results, DMs the winner and marks them `won`. The bot refuses to close while a draw is unresolved. There is no cancel path after commit. *(PRD: Draw snapshot, Commitment post, Verifiable selection, Re-check at draw, Results post, Committed draws must complete)*
8. **Claims + redraw.** The winner's claim flow validates and stores the contact and notifies admins. The 48h window is measured from `published_at`. On expiry admins are notified and Redraw is enabled, following the redraw rule in Decisions. The admin can mark a claim Activated, and the bot DMs the winner. *(PRD: Winner notification & claim, 48h claim window + redraw, Prize tracking)*
9. **Admin console.** `/admin` menu with Close entries, Current draw, Approve results, Mark activated, Redraw, Staff add/remove/list, Stats. Every action is written to `AuditLog`, with confirmation buttons for Close, Approve and Redraw. *(PRD: Admin access, Staff exclusion, Auditability)*
10. **Stats + retention.** `/stats` shows totals, entrants this draw period, new-to-channel entrants, and 30-day retention. A daily asyncio job re-checks entrants at day ≥ 30 at ≤ 20 checks/s and stores `MembershipCheck`. *(PRD: Growth stats, metrics table)*
11. **Verify.** `/verify [draw#]` sends the entry-code list as a `.txt` file plus the formula and inputs. `tools/verify_draw.py` (stdlib only) recomputes everything from the file plus the round number. *(PRD: /verify)*
12. **Packaging.** Multi-stage `Dockerfile` (uv sync into a slim 3.12 image), and `compose.yaml` with the `./data` volume for SQLite, `restart: unless-stopped` and env_file. Running `alembic upgrade head` on start. README covers setup, making the bot a channel admin, the env vars and the operator runbook (weekly draw, claim, redraw).
13. **Staging E2E.** Run with a test bot, a private test channel and three test accounts, and work through the Verification checklist.

## Verification

Planned checks (none run yet):

| Behavior | Check |
|----------|-------|
| Hash/index/walk determinism, masking, contact validation | `uv run pytest tests/domain` with golden vectors; `tools/verify_draw.py` reproduces the same index from the same inputs (test) |
| Idempotent entry, eligibility, unique codes | `uv run pytest tests/services/test_participation.py` (FakeMembership, temp SQLite) |
| Close → commit → select → approve → publish, skips, UNKNOWN pauses, refusal to double-close, no cancel after commit | `uv run pytest tests/services/test_draw_service.py` (FakeBeacon + FakeMembership; the bot API is stubbed) |
| Restart resumes a committed draw and the 48h timers | Test: create a `committed` draw, rebuild the service, assert it completes |
| Claim validation, 48h expiry, redraw excludes the no-show and links the parent | `uv run pytest tests/services/test_claims.py` with an injectable clock |
| Admin-only handlers | Handler tests: a non-admin callback is rejected |
| i18n key parity | `uv run pytest tests/test_i18n.py` |
| drand adapter | Unit test with mocked HTTP (relay disagreement → error); one opt-in live test `-m live` fetching a real quicknet round |
| Lint | `uv run ruff check . && uv run ruff format --check .` |
| Image builds and boots | `docker compose build && docker compose up` against the test bot. The boot health check fails if the bot isn't a channel admin |
| End to end | Staging checklist from Step 13: enter while subscribed or not, staff blocked, close, commitment post, results post with proof, `/verify` + script match, claim, expiry → redraw, activation DM, all three languages |

## Risks

- **Legal check (PRD open question) gates the first public draw, not the code.**
- drand down at draw time → the draw stays `committed` and the resume loop retries with backoff. Admins are alerted. There is no local fallback, by design.
- Telegram rate limits on DMs and re-checks → re-checks happen per candidate only at draw time, and retention runs as a throttled batch.
- Prize text, SLA and the ru/uz-Latn translations need staff approval before launch. Placeholders stay clearly marked in the catalogs.
- Single-process SQLite: running two bot instances is unsupported (documented). The compose file runs exactly one.

## Deferred

- PRD P2 "weekly reminder broadcast" is not in this plan. It's a follow-up after launch data. Deep-link source tags (also P2) are included in Step 6 because they're nearly free.
