# Code Review - Weekly Giveaway Bot @ 85f55cd

**Date**: 2026-10-26 | **Reviewer**: AI Code Reviewer (`/sr` full, 7 reviewers) | **Status**: NEEDS FIXES

<!-- SECTION:review-context -->
## Review Context

- **Mode**: task (`tasks/task-2026-10-26-weekly-giveaway-bot`)
- **Target**: commit `85f55cd` (root commit)
- **Base**: empty tree (whole repository reviewed)
- **Head**: `85f55cd`
- **Scope**: full (61 files, about 4.4k lines excluding `uv.lock`; migrations, infra and auth in scope)
- **Spec Available**: yes: [PRD](../../product-docs/PRD/PRD-weekly-giveaway-bot.md), [plan.md](plan.md)
- **Immutable Snapshot**: yes
- **Untracked Files**: none

<!-- /SECTION:review-context -->

---

<!-- SECTION:summary -->
## Reviewer Note

The design is sound and fits the problem well:
- the draw logic is pure, with the clock and sleep injected;
- the beacon and membership ports each have real fakes;
- an API error is never treated as "unsubscribed";
- resume is driven by persisted state.

The unit and service tests are good.

The blocking problems are in **state-transition safety**:
- every "check status, then write it" step runs outside a real transaction on pysqlite;
- a double press of Confirm double-executes approve and redraw (**reproduced**: two results posts, two child draws).

Several crash and failure windows leave a draw stuck or publish a second commitment. Some trust claims are also stronger than the code:
- a single drand relay is accepted;
- Telegram's "chat not found" counts as "unsubscribed";
- admins can influence the subscription skip after the randomness is known.

Fix the 2 critical and the transition-related majors before any public draw.

<!-- /SECTION:summary -->

---

<!-- SECTION:verdict -->
## Verdict

**NEEDS FIXES**: 2 critical, 12 major (after merging duplicates across reviewers).

**Next action**: fix C1–C2 and M1–M9, adding a regression test for each, then re-review with `/sr`. M10–M12 (performance, test gaps, structure) can go in the same pass or a follow-up before launch.

<!-- /SECTION:verdict -->

---

<!-- SECTION:key-findings -->
## Key Findings

### CRITICAL

- **C1 [high, reproduced] Status transitions aren't atomic, so double presses double-execute.**
  - Where: `services/draw_service.py` `approve` (~293), `redraw` (~358), `_create` close re-check (~141); `services/claims.py` `submit`, `activate`, `expire_due`.
  - Cause: pysqlite doesn't emit BEGIN before the first write, so the status SELECT isn't isolated. aiogram runs callbacks concurrently.
  - Observed (orchestrator re-ran the scratch script): two concurrent approves gave `[True, True]` and **2 channel results posts**. Two concurrent redraws gave **child draws 2 and 3**, both committed publicly.
  - Fix: route every status change through one conditional `UPDATE … WHERE id=? AND status IN (…)` and check `rowcount == 1`. Alternatively, use the SQLAlchemy pysqlite `BEGIN IMMEDIATE` recipe, plus an `asyncio.Lock` around close and redraw.
  - Add `asyncio.gather` double-press tests.
  - *(architecture C1, security M2)*

- **C2 [high] Redraw runs in two transactions and skips the unresolved-draw guard.**
  - Where: `draw_service.py:358-377`, `_create:143`.
  - The parent is committed as REDRAWN before the child exists. A crash, exception or failed commitment post in between causes one of:
    - a lost redraw that no button can recover;
    - a concurrent Close slipping in and creating a second unresolved draw;
    - a child stuck in COMMITTED with nothing resolving it.
  - Fix: write the parent CAS and the child insert in one transaction. Apply the guard to every creation. Always call `spawn_resolve`. *(architecture C2)*

### MAJOR

- **M1 [high] A failed commitment post leaves the draw stuck in COMMITTED and blocks every later Close until a restart.**
  - Where: `handlers/admin.py` `close!` and `redraw!`; `_draw_actions` has no action for COMMITTED.
  - Fix: catch the error and still call `runner.spawn_resolve` (resolve already calls `ensure_committed`). Add a Retry button for COMMITTED. *(orchestrator error-path pass, code-quality, architecture M1, spec F2)*

- **M2 [medium] A second public commitment with a different round is possible.**
  - If `post_channel` delivers but raises (timeout), or the process dies before `commit_msg_id` is saved, `ensure_committed` re-picks the round and posts again. The channel then shows two rounds for one draw. The first round's value may already be public, which reads as re-rolling.
  - Fix: persist the round before posting. Keep it while it's still in the future. Label any re-commit as superseding the earlier one. Check that the post time is before `round_time`. *(spec F3, security, code-quality, architecture m3)*

- **M3 [medium] Admins can influence the subscription skip after the randomness is known.**
  - `admin_paused` sends the candidate's Telegram ID to admins. The pause has no time limit, and `retry` has no confirmation or audit entry. An insider can remove that person from the channel and then press Retry, and the next index wins. The public sees only skipped indexes.
  - Fix: drop the ID from the alert. Audit `retry`. Auto-retry paused draws with backoff. Publish skipped **entry codes** so people can dispute a skip. Consider fixing eligibility at close time. *(security M1)*

- **M4 [high] The membership adapter breaks the UNKNOWN contract.**
  - `"not found" in message` also matches Telegram's **"chat not found"**. A wrong `CHANNEL_ID` or lost access would make everyone NO, and the draw would end with no winner.
  - `TelegramServerError`, `TelegramForbiddenError` and similar errors aren't caught, so `join` gives no reply and `resolve` retries silently.
  - Fix: match user-not-found exactly. Fall back to `TelegramAPIError` → UNKNOWN. Add `tests/test_membership.py`. *(test-coverage, code-quality M3, docs)*

- **M5 [high] NO_WINNER draws get no channel post, and the redraw with nobody left shows the wrong message.**
  - A committed draw's outcome never reaches the public, which contradicts "results post is mandatory once committed".
  - `redraw()` returns `None` for both "too early" and "no candidates", so the admin sees "too early".
  - Fix: post a no-winner results message with every skip. Return an explicit redraw outcome. *(spec F1, code-quality M2, docs)*

- **M6 [high] Randomness trust is weaker than documented.**
  - A single reachable relay is accepted.
  - Only `SHA256(signature)` is checked; the BLS signature isn't verified.
  - Genesis and period come from the first relay and aren't pinned.
  - `BEACON_LEAD_SECONDS` has no lower bound.
  - `round_at(now+60)` is only 57–60 s ahead (PRD says ≥ 1 min).
  - TASK.md and plan.md claim "must agree across 2 relays".
  - Fix: require 2 agreeing relays, or a verified BLS signature. Pin genesis, period and public key. Validate lead ≥ 30 s. Use `round_at(now+lead)+1`. Correct the docs. *(spec F4/F6, security, docs)*

- **M7 [high] Background failures are only logged.**
  - On a drand outage, admins aren't alerted (PRD says "admin is informed").
  - `_resolve_with_retry` retries any exception forever, including a `None` draw from `adm:retry:<bad id>`, and each press spawns another task.
  - Fix: alert admins after N failures. Retry only transient errors. Dedupe tasks per draw. *(spec F5, architecture M6, code-quality, security)*

- **M8 [high] Crash windows between saving state and the side effect have no resume path.**
  - PUBLISHED with `result_msg_id` NULL: the 48h window runs on an unseen result, and the winner gets no DM.
  - EXPIRED where the admin alert was lost: `expiry_notified` exists but is never used.
  - Fix: treat these as pending and finish them from `Runner.start` and the periodic loop. *(architecture M2)*

- **M9 [medium] A winner who never got the DM can't find the claim instructions.**
  - The results post says "open the bot", but `/start` and `/status` don't resend `win_dm`.
  - Fix: show `win_dm` on `/start` and `/status` when there's an open claim. *(docs)*

- **M10 [high] Performance at 100k entrants.**
  - `DrawService.pool()` loads every user as an ORM object, on every `/admin`, Close and Stats. That's about 1–2 s on the event loop each time.
  - `_create` does `add_all` of N `DrawEntry` rows, which blocks the loop and holds the SQLite write lock. Concurrent entrants get "database is locked".
  - `/verify` rebuilds and re-uploads about 1 MB per request with no throttle.
  - Fix: use `COUNT(*)` and an `INSERT … SELECT` snapshot. Cache the `/verify` `file_id`. Add per-user throttling. *(performance ×3, security)*

- **M11 [high] Test gaps on P0 paths.**
  - `TelegramMembership` is untested.
  - Untested handlers: free-text claim; admin approve, redraw, activate and retry; `/status`; `/language`.
  - The results-post skip list is never rendered in a test.
  - The past-winner exclusion test never runs a second draw.
  - `/verify` is only checked with N=1.
  - Runner backoff is untested (`sleep` isn't injected).
  - Migration vs models is not checked in CI.
  - *(test-coverage, 7 majors merged)*

- **M12 [high] Structural: the draw/claim state machine is spread out and the eligibility rule is defined three times.**
  - `claims.py` mutates `Draw.status` with no transition table.
  - `DrawService` is a ~400-line hub, and `Claims` depends on it only for `alert_admins`.
  - Eligibility lives in `Participation.is_excluded`, `DrawService.pool` and `Stats.snapshot`, and the Stats version omits channel admins.
  - Fix: one transition map (pairs with C1). Add `Participation.eligible_pool()`. Extract an `AdminNotifier`. *(architecture M3–M5, code-quality, test-coverage)*

### MINOR (high confidence)

- `/verify` reveals randomness, winner index and position for DRAWN/PAUSED draws **before admin approval**. It also crashes on `"²"` or very long digit strings. Fix: show results only from PUBLISHED, and parse with `[0-9]{1,9}`. *(security, spec F11, code-quality)*
- The admin approves the winner, not the rendered post text. PRD and plan say "preview / approves the text". *(spec F9, security)*
- The winner's `first_name` goes into the official post unfiltered: auto-linked `@mentions` or `t.me/` URLs become promo links. Strip them and cap the length. *(security)*
- No private-chat filter: claims and `/admin` work in groups and echo contacts or staff IDs. *(security)*
- Staff who entered before being excluded are told "already participating" instead of "not eligible". The channel-admin list is cached for 5 minutes at close time. Redraw doesn't re-filter exclusions. *(spec F7/F10, security, architecture m4)*
- `approve!`, `activate` and `retry` give no feedback on failure or a repeat press. Callback parsing isn't defensive. `cancel` → `delete` fails on messages older than 48h. *(code-quality, architecture m6)*
- `match` blocks have no `assert_never` default (`handlers/user.py`). *(code-quality)*
- `register_start` does network I/O inside an open session, and a concurrent first `/start` raises IntegrityError. *(code-quality)*
- The resolve lock is global and held through beacon waits. Make it per-draw and dedupe tasks. *(architecture m1)*
- A crash mid-walk forgets earlier skips. Persist each skip. *(architecture m2)*
- Contact input has no length cap, and the email regex can backtrack quadratically. *(security)*
- Membership calls `asyncio.sleep(retry_after)` inline in the user's handler. Join does a duplicate user lookup and sends a new message per press. *(performance)*
- The Publisher port imports aiogram, so services depend on it transitively. *(architecture m7)*
- `Draw.expiry_notified` is a dead column. The full-name and skip-list formatting is duplicated. *(code-quality, docs)*
- P1 gaps: no "entrants this draw period" in `/stats`, and no next-draw info in `/status`. *(spec F8)*
- Docs:
  - README button names are English, but the labels exist only in uz/ru.
  - `pytest -m live` runs **only** the live tests.
  - The formula text doesn't mention hex→bytes or big-endian.
  - Wrap-around and the final position aren't public.
  - Bot admins are missing from the "can't win" list.
  - The runbook doesn't say Close is blocked until the previous draw is activated or redrawn.
  - `.env.example` is missing `DRAND_CHAIN_HASH` and `RETENTION_CHECKS_PER_SECOND`.
  - `./data` ownership needs uid 10001.
  - TASK.md says "nothing committed" (stale).
  - plan.md layout and data model drift is undeclared.
  - *(docs)*
- `tools/verify_draw.py` doesn't check the returned round, divides by zero on an empty file, and doesn't apply skips. *(code-quality, security)*

### Other low-confidence notes

- Second instance not prevented (add a file lock).
- `DrawBlocked(None)` possible.
- The `/verify` text points to a repo path, not a URL.
- Operator could add fake codes undetectably (inherent; document it).
- Channel admins can edit the commitment post.
- `httpx` client is never closed.
- Retention batch throughput is ~2k/day.
- Stats does 5 table scans.
- Tests load a developer's `.env` (`_env_file=None`).
- The router-detach teardown uses a private attribute.

### Pattern propagation (C1)

I scanned every status writer: `draw_service.approve`, `redraw`, `_create`, `ensure_committed`, `_set`, and `claims.submit`, `activate`, `expire_due`. The same unguarded read-then-write appears in all **6 status-changing call sites**. `participation._assign_code` is protected by the unique index.

<!-- /SECTION:key-findings -->

---

<!-- SECTION:coverage -->
## Review Coverage

- **Diff Reviewed**: whole tree at `85f55cd` except `uv.lock`
- **Spec Used**: PRD (P0/P1/P2 table, Solution Outline, Key States) and plan.md Decisions/Verification; TASK.md deviations taken as declared but re-checked against the code
- **Verification Run**: yes (see Verification). The test-coverage reviewer had no shell, so the orchestrator ran the suite.
- **Review Limits**:
  - No staging run against real Telegram (plan Step 13 is still blocked).
  - TDD chronology can't be checked from a single root commit.
  - The performance figures are estimates, not measurements.
  - Cross-AI validation was not run (optional; Codex isn't authenticated).

<!-- /SECTION:coverage -->

---

## Domain Findings

<!-- SECTION:quality-gate -->
### Verification Gate

See Verification. Tests, lint and format pass at HEAD.

<!-- /SECTION:quality-gate -->

---

<!-- SECTION:spec-compliance -->
### Spec Compliance

**Agent**: `spec-compliance-reviewer`: **NON_COMPLIANT**.

- PRD P0/P1: 13 of 17 fully met, 4 partial (committed draw must complete, results-post text approval, staff exclusion messaging, P1 stats/status).
- Plan decisions: 11 of 13 met; randomness is partial and the deviation is undeclared.
- Findings F1–F5 → M5, M1, M2, M6, M7. F6–F10 → minors. Details in Key Findings.

<!-- /SECTION:spec-compliance -->

---

<!-- SECTION:approach-review -->
### Architecture

**Agent**: `senior-architecture-reviewer`: **NEEDS_REWORK** (2 critical, 6 major, 7 minor).

- Scores: approach 7/10, fit 7/10, practices 6/10.
- All three seams are real (production + fake adapters).
- TDD compliance is unverifiable from a single commit.
- C1/C2 reproduced. M1–M6 → C1, C2, M1, M7, M8, M12.

<!-- /SECTION:approach-review -->

---

<!-- SECTION:security -->
### Security

**Agent**: `security-code-reviewer`: 0 critical, 2 major (→ M3, C1), 9 minor.

- No auth bypass, injection or participant-side steering found.
- HTML escaping, bound SQL parameters, `secrets` entry codes and the router-level admin filter are all correct.

<!-- /SECTION:security -->

---

<!-- SECTION:code-quality -->
### Code Quality

**Agent**: `code-quality-reviewer`: 0 critical, 3 major (→ M1, M5, M4), 13 minor.

- Confirmed correct:
  - candidate walk and skip continuation across the wrap;
  - `join` match is exhaustive today;
  - `expire_on_commit=False` makes detached use safe;
  - naive UTC datetimes are used consistently.

<!-- /SECTION:code-quality -->

---

<!-- SECTION:test-coverage -->
### Tests

**Agent**: `test-coverage-reviewer`: 0 critical, 7 major (merged → M11, plus M4's adapter bug), 11 minor.

- Did not run tests (no shell); the orchestrator ran them.
- Service-level coverage of draw, claim and participation is good. The handler and adapter layers are thin.

<!-- /SECTION:test-coverage -->

---

<!-- SECTION:documentation -->
### Documentation

**Agent**: `documentation-accuracy-reviewer`: 0 critical, 3 major (→ M6, M4, M9), 12 minor.

- The draw formula is consistent across `domain/draw.py`, `tools/verify_draw.py`, the `/verify` file and all three catalogs.

<!-- /SECTION:documentation -->

---

<!-- SECTION:performance -->
### Performance

**Agent**: `performance-reviewer`: 0 critical, 3 major (→ M10), 4 minor.

- Estimated close plus resolve at 100k is about 4–8 s, so the PRD's 30 s target is met. But it blocks the event loop and the SQLite writer, which breaks the 2 s p95 button target during a draw.

<!-- /SECTION:performance -->

---

<!-- SECTION:verification -->
## Verification

Run by the orchestrator at HEAD `85f55cd`, from the repo root, with a clean tree:

| Command | Result |
|---------|--------|
| `uv run pytest -q` | 58 passed, 1 deselected (live) |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 48 files already formatted |
| Reviewer race scripts (`scratchpad/race/race.py`, `race2.py`; temp DB + fakes) | double approve → 2 results posts; double redraw → 2 child draws (**C1 confirmed**) |

From the implementation session (same code, not re-run in this review): `pytest -m live` passed, `alembic check` was clean, and the Docker build and boot smoke test passed. **Not run:** the staging end-to-end with real Telegram.

<!-- /SECTION:verification -->

---

<!-- SECTION:metadata -->
## Metadata

**Changed Files**: 61 (whole repo; `uv.lock` excluded from review)
**Diff Source**: `git show 85f55cd` (root commit vs empty tree)
**Reviewers Invoked**:
- spec-compliance-reviewer
- senior-architecture-reviewer
- security-code-reviewer
- code-quality-reviewer
- test-coverage-reviewer
- documentation-accuracy-reviewer
- performance-reviewer
- orchestrator error-path and integration-seams passes

**Not invoked**: structural-quality-reviewer (deep scope only; not requested).

<!-- /SECTION:metadata -->

---

<!-- SECTION:revision-2 -->
## Revision 2: fix log (2026-10-26, working tree on top of `85f55cd`, uncommitted)

**Status**: fixes implemented; **re-review pending**. This revision records what changed. It is not a new verdict.

| Finding | Fix | Regression test(s) |
|---------|-----|--------------------|
| **C1** non-atomic transitions | Every status change goes through `DrawService._cas`: `UPDATE … WHERE id=? AND status IN (…)` plus a `rowcount == 1` check. Close and redraw are serialized by a lifecycle lock. Claims use the same helper. | `test_double_approve_posts_once`, `test_double_close_creates_one_draw`, `test_double_redraw_creates_one_child`, `test_double_submit_and_double_activate_happen_once`, `test_double_confirm_press_publishes_once` (handler). Removing the guard fails exactly these 5. |
| **C2** redraw split | The parent CAS (EXPIRED→REDRAWN or NO_WINNER), the no-show reset and the child insert run in one transaction. The commit happens afterwards, and failures go through the resolve retry. | `test_redraw_with_failed_commitment_still_leaves_a_resolvable_child`, `test_redraw_with_nobody_left_is_announced`, `test_redraw_before_expiry_is_not_allowed` |
| **M1** stuck COMMITTED | `close_entries`/`redraw` never raise on a failed post. The handler always calls `spawn_resolve`, and resolve publishes the commitment first. COMMITTED drafts get a Retry button and an admin "commitment not posted" alert. | `test_failed_commitment_post_keeps_the_round_while_it_is_still_ahead`, redraw test above |
| **M2** second commitment | The round is persisted at close and kept while it is ≥ 15 s ahead. Otherwise, or if the post lands after its round, the round goes into `void_rounds` and is named in the replacing post. A round is never replaced after its randomness was fetched. | `test_commitment_retried_too_late_voids_the_old_round_publicly`, `test_commitment_that_lands_after_its_round_is_voided_and_recommitted` |
| **M3** admin-steerable skip | The pause alert no longer names the candidate. Pauses retry automatically with backoff. Retry is validated and audited. Skips are published with the **entry code** and the reason; a *kicked* candidate is shown as "removed by a channel admin", so a skip can be checked and disputed. | `test_api_failure_pauses…` (alert text exact), `test_banned_candidate_is_skipped_with_a_distinct_public_reason`, `test_paused_draw_is_retried_automatically` |
| **M4** membership contract | Exact "user not found" / `PARTICIPANT_ID_INVALID` → NO. `KICKED` → BANNED. Every other `TelegramAPIError`, including "chat not found", → UNKNOWN. Rate limits over 2 s → UNKNOWN instead of sleeping in the handler. The admin-list cache falls back to stale data on error and is refreshed fresh at close. | `tests/test_membership.py` (15 cases) |
| **M5** no public post for NO_WINNER | A NO_WINNER draw posts `channel_no_winner` with all skips. A redraw with nobody left posts `channel_redraw_empty`. `redraw()` returns an explicit `RedrawResult`. | `test_everyone_unsubscribed_means_no_winner`, `test_redraw_with_nobody_left_is_announced` |
| **M6** randomness trust | 4 default relays, ≥ 2 must agree. Genesis, period and chain hash are pinned (boot fails on mismatch). `BEACON_LEAD_SECONDS ≥ 30`. Committed round = `round_at(now+lead)+1`. | `test_a_single_reachable_relay_is_never_trusted_alone`, `test_info_with_unexpected_timing_is_rejected[*]`, close test asserts `round_time ≥ now+60`; live test against all 4 relays |
| **M7** silent background failures | The runner alerts admins once per draw on a beacon outage, retries transient errors only, alerts and stops on anything else, and deduplicates tasks per draw. A missing draw is a no-op. | `test_beacon_outage_alerts_admins_once_and_retries_with_backoff`, `test_unexpected_error_alerts_admins_and_stops_retrying`, `test_spawning_twice_runs_one_resolve`, `test_resolving_a_missing_draw_is_a_quiet_no_op` |
| **M8** crash windows | `published_at` is set only after the post. A PUBLISHED or NO_WINNER draw without a `result_msg_id` is finished by `finish_publishing` from the runner's housekeeping loop. Expiry alerts are tracked in `expiry_notified` and re-sent until delivered. | `test_results_post_interrupted_by_a_crash_is_finished_on_restart`, `test_interrupted_results_post_and_paused_draws_resume_on_start`, `test_expiry_alert_lost_in_a_crash_is_sent_on_the_next_pass` |
| **M9** missed winner DM | `/start` and `/status` resend `win_dm` while the claim is open. | `test_winner_who_missed_the_dm_gets_claim_instructions_on_start`, `test_pending_claim_is_offered_again_until_the_deadline` |

**Also fixed (from the minors)**:
- `/verify` reveals results only from PUBLISHED onward, and arguments are parsed with `[0-9]{1,9}` (`test_verify_rejects_garbage_arguments`; the verify test now uses 3 entrants and checks the file's SHA-256).
- Private-chat-only routers (`test_group_chats_are_ignored`).
- Defensive admin callback parsing with feedback on every outcome (`test_malformed_admin_callbacks_are_ignored`).
- `query.answer()` is always sent.
- `/cancel` tolerates old messages.
- The verifier checks the returned round and handles an empty file.
- The httpx client is closed on shutdown.
- Tests no longer read a developer's `.env`.
- `pool()` is replaced by `pool_count()` (COUNT) and column-only snapshot reads.
- The snapshot uses a bulk `INSERT`.
- Admin texts no longer duplicate the full-name and skip formatting.
- Docs: README, `.env.example` and the plan's randomness decision are corrected.

**Not addressed in this pass**:
- **M10 (partly)**: `/verify` still uploads the list per request, with no `file_id` cache or throttle.
- **M11 (partly)**: there are still no handler tests for activate/redraw/staff/`/language`, and no migration-vs-models test in CI.
- **M12**: no `AdminNotifier` or `ResultsPublisher` extraction; the state map is enforced by `_cas`, not a declared table.
- **Minors**: winner-name sanitising in the public post, `register_start` concurrency, `assert_never`, contact length cap/regex, a second-instance file lock, P1 stats/status gaps, and approval of the post *text* (the admin approves the winner).

**Verification (observed)**:
- `uv run pytest`: 101 passed.
- `uv run pytest -m live`: 1 passed.
- ruff check and format: clean.
- `alembic upgrade head && alembic check`: no drift.
- `docker compose build` and boot smoke: OK.
- The reviewer's `race.py`: double approve → 1 post.

<!-- /SECTION:revision-2 -->
