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
## Revision 2: fix log (2026-10-26, commit `d4630e8`)

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

---

<!-- SECTION:revision-3 -->
## Revision 3: re-review of `85f55cd..d4630e8` (2026-10-26)

**Reviewer**: `/sr` full, 7 reviewers plus orchestrator passes. **Verdict: NEEDS FIXES** (0 critical, 8 major).

### Review context
- **Mode**: task, explicit range `85f55cd..d4630e8` (29 files, +2060 −397 excluding `uv.lock`; includes a migration). Scope: full.
- **Snapshot**: clean git worktree at `d4630e8`, removed after the review.
- **Immutable snapshot**: yes.
- **Spec**: PRD, plan.md (revised Randomness and Skip rows), TASK.md deviations.

### Reviewer note
**Both criticals are fixed.**
- The architecture reviewer re-ran every race (double close, approve, redraw, activate, submit; close vs redraw; submit vs expiry at the deadline ±1 s; approve vs housekeeping). Each one now executes exactly once.
- Crash windows for PUBLISHED, NO_WINNER, EXPIRED and COMMITTED resume automatically.
- M1, M4, M5, M8 and M9 are fixed. M2, M3, M6 and M7 are fixed with residual gaps.

**What blocks approval now:**
- **Regression tests that prove nothing (verified):** two regression tests pass even on the pre-fix code.
- **One bad relay halts the draw:** a single misbehaving drand relay can stop resolution.
- **Retry does nothing:** the Retry button is a silent no-op while auto-retry is backing off.
- **Staff told "already participating":** staff who entered earlier get that instead of "not eligible".
- **Veto by ban+unban:** an insider can still veto a drawn candidate after the randomness is public, and ban+unban hides the "removed" label.
- **Two still-open performance majors:** an unthrottled `/verify` upload, and `register_start` holding a pool connection during a network call.

### Revision-1 findings: status at `d4630e8`

| Finding | Status | Evidence / residual |
|---------|--------|---------------------|
| C1 non-atomic transitions | **Fixed** | All status writers use `_cas` or a conditional bulk UPDATE (orchestrator grep). Reproductions are single-execution (architecture). Removing the guard fails exactly 5 tests (implementation session). Residual: only one process is safe (R8). |
| C2 redraw split | **Fixed** | One transaction under `_lifecycle`; close vs redraw in both orders gives 1 unresolved draw. |
| M1 stuck COMMITTED | **Fixed** | Architecture crash reproduction d resumes and resolves. |
| M2 second commitment | **Fixed, with residual** | Same round reused while still ahead; void rounds are published. Residuals: never-announced rounds are voided on every retry during a posting outage, and void rounds are not shown in the results post, `/verify` or the verifier (n1, n9). |
| M3 admin-steerable skip | **Partial** | Anonymous pause alert, audited Retry, skips published with entry codes. Remaining: veto by ban+unban after the randomness is out (R4). |
| M4 membership contract | **Fixed** | 15 adapter cases. Residual: `channel_admin_ids` raises when nothing is cached (n6). |
| M5 NO_WINNER post | **Fixed, with residual** | Housekeeping retries it. The redraw-empty post is sent only once (n3). |
| M6 randomness trust | **Partial** | 2-of-4 quorum plus pinned timing work, but one bad relay halts the draw (R1). Three of the four relays share one operator (n4). Lead minimum is 30 s vs the PRD's 60 s, and the round is chosen before the snapshot insert (n7). |
| M7 background failures | **Partial** | Retry is a no-op during backoff (R2). Alert spam and permanent errors treated as transient (n1). An exception inside the except-handler ends the task silently (n5). |
| M8 crash windows | **Fixed, with residual** | A lost DRAWN/CLAIMED alert isn't resumed (n5). "Expiry re-sent until delivered" is overstated: delivery failure still sets the flag (n8). |
| M9 missed winner DM | **Fixed** | Handler test for `/start`. |
| M10 performance | **Partial** | `pool_count` and column reads fixed. `/verify` upload still open (R6); `register_start` pool hold (R7). |
| M11 test gaps | **Partial** | Membership adapter, `/verify` with N>1 and skip-list rendering are now covered. See R5. |
| M12 structure | **Open (deferred)** | `Claims` calls the private `DrawService._cas`; there is no transition table. |

### Key findings

#### MAJOR
- **R1 [medium; orchestrator confirmed by reading `ports/beacon.py`] One misbehaving drand relay halts resolve.**
  - A relay returning 200 with non-JSON or missing keys raises `JSONDecodeError`/`KeyError`/`ValueError` out of `asyncio.gather`. That isn't transient, so the runner alerts and stops.
  - A relay with a bad signature or the wrong round raises `BeaconError` for the whole poll, even when 3 others agree. The draw then retries forever.
  - Fix: per-relay errors → "invalid", ignored. Decide among valid answers only. Ideally verify BLS. *(code-quality, security)*
- **R2 [high] Retry is a silent no-op while auto-retry is backing off** (up to 600 s).
  - `spawn_resolve` dedupe returns early.
  - README, TASK and `admin_paused` all promise "forces an immediate attempt".
  - Fix: a per-draw `asyncio.Event` that wakes the backoff sleep, or correct the texts. *(documentation, spec N5, architecture m-R4)*
- **R3 [high] Staff or channel admins who entered before exclusion are told "already participating"** (`participation.py:91-94`). This breaks PRD P0 "told politely they're not eligible". Fix: check exclusion first, and make `/status` say not eligible. *(spec F7, carried over from revision 1 minor)*
- **R4 [medium] Insider veto after the randomness is public.**
  - Setup: a channel admin who also has DB access.
  - Steps: compute the candidate from the public round, then ban and unban them. Telegram then reports `left`, so the skip shows as "left the channel", not "removed".
  - A forced pause (revoking the bot's rights) widens the window.
  - Fix: decide eligibility before the randomness exists (membership pass at close, or timestamped `chat_member` updates), or document it as a known limit. *(security)*
- **R5 [high; orchestrator confirmed by reverting the fixes] Regression tests that don't protect the fix, plus open P0 test gaps.**
  - **M6 round lead:** reverting `+1` still passes. The test clock is aligned to the 3 s period.
  - **M5 redraw-empty:** removing the post still passes. `№N` also appears in the parent's results post.
  - Still untested:
    - admin `redraw!`/`activate`/`retry` handlers;
    - past-winner exclusion with a second draw (`ALREADY_WON`);
    - `ensure_committed` exhaustion and the "never replace after randomness" guard;
    - migration-vs-models check in CI;
    - the double-submit test is decided by `IntegrityError`, not the CAS.
  - *(test-coverage)*
- **R6 [high] `/verify` rebuilds and uploads the full code list on every request, with no throttle.** The results post now sends readers there; about 90 MB of upload per 100 requests at 100k. Fix: cache the `file_id` per draw and add a per-user cooldown. *(performance; was "not addressed" in revision 2)*
- **R7 [medium] `register_start` holds a pooled DB connection across `getChatMember`.** A launch burst exhausts the pool (5+10). Fix: check membership before opening the session; insert with `ON CONFLICT DO NOTHING`. *(performance; revision-1 minor)*
- **R8 [low] Exactly-once side effects rely on in-process locks.** A second instance (deploy overlap, a local run against prod) double-posts commitments or results. Fix: an exclusive file lock at boot. *(security, architecture nit)*

#### MINOR (high confidence unless noted)
- **n1** Permanent Telegram errors (BadRequest, Forbidden) are treated as transient. `admin_commit_pending` is resent on every retry (reproduced: 10 alerts in 10 iterations). Never-announced rounds are voided each retry, so the void list grows without bound.
- **n2** Long void or skip lists can push a post over Telegram's 4096-character limit, after which it permanently fails to publish. Cap the inline list.
- **n3** The redraw-with-nobody-left post is sent once: not persisted, not retried, no alert.
- **n4** (low) 3 of the 4 default relays are run by drand.sh. The first two answers are accepted before other relays publish.
- **n5** A lost DRAWN/CLAIMED admin alert isn't resumed. An exception inside the runner's TRANSIENT handler ends the task silently. A failed housekeeping publish reverts to DRAWN without an alert. A failed NO_WINNER post never reaches admins.
- **n6** `channel_admin_ids` raises when nothing is cached (no warm-up at boot). The stale fallback at close is undeclared. The bot can't boot while fewer than 2 relays are reachable (undeclared; contradicts "draw waits, admin is informed").
- **n7** `BEACON_LEAD_SECONDS ≥ 30` vs PRD ≥ 1 min (undeclared). The round is chosen before the 100k insert. The beacon-outage alert comes only after 400 polls (about 10–77 min).
- **n8** "Expiry alert re-sent until delivered" is overstated, because `alert_admins` swallows delivery failures.
- **n9** Void rounds are absent from the results post, `/verify` and `verify_draw.py`. The host clock is trusted for "future" (no `/public/latest` check).
- **n10** (medium) `/verify` shows PUBLISHED/NO_WINNER results before `result_msg_id` exists. Approve racing housekeeping can tell the admin "already done". A duplicate claim message replies "help".
- **n11** Docs:
  - README says the round is "at least 60 s after the post" (not guaranteed).
  - Channel posts and `verify_text` leave out hex→bytes and big-endian.
  - `verify_draw.py` is said to "recompute everything" (it doesn't apply skips and uses one relay).
  - Stale `models.py` comments (PAUSED, skipped shape).
  - plan.md layout, data model and Steps 5/7 drift.
  - Runbook leaves out the `admin_resolve_failed` row.
  - `channel_void_note` assumes an earlier post existed.
  - `channel_skipped_line` lost its trailing newline.
- **n12** Tests:
  - runner tests rely on wall-clock time and `runner.stop()` is not in a `finally`;
  - a test named "paused draws resume" has no paused draw;
  - weak assertions (banned reason text, retry_after sleep);
  - tests reach into privates (`_positions`);
  - `/verify <id>` before approval is untested;
  - callback private-chat filter untested.

#### Other low-confidence notes
- Callbacks on inaccessible messages (aiogram version dependent).
- `_replace_round` isn't a CAS.
- `OperationalError` is classed as transient for permanent DB errors.
- Unbounded lock dicts (negligible).
- Membership retry sleeps after the last attempt and retries deterministic BadRequests.

### Coverage
- All 7 reviewers ran on the snapshot worktree.
- The test-coverage, code-quality and security reviewers had no shell, so their findings are static. The orchestrator ran the verification and two sensitivity reverts. The architecture reviewer ran the race and crash reproductions.
- **Deep structural review recommended: >15 files / >600 lines. Run `/sr deep`.**

### Verification (observed)
| Command / check | Where | Result |
|---|---|---|
| `uv run pytest -q` | worktree @ `d4630e8` | 101 passed, 1 deselected |
| `uv run ruff check .` / `ruff format --check .` | worktree @ `d4630e8` | clean / 52 files formatted |
| Revert `_future_round` `+1` → `pytest -k close_commits` | worktree, reverted after | **still passes** (R5) |
| Remove redraw-empty post → `pytest -k nobody_left` | worktree, reverted after | **still passes** (R5) |
| Status-writer grep | worktree | every Draw status write goes through `_cas` or a conditional UPDATE, or follows a CAS in the same transaction |
| Race and crash reproductions (`scratchpad/race3/race3.py`, `crash.py`) | architecture reviewer | all races single-execution; crash windows a–e2 recover; g = alert spam plus void inflation |
| From the fix session (not re-run here) | `d4630e8` tree | `pytest -m live` passed; `alembic check` clean; Docker build and boot OK |

### Metadata
- **Diff source**: `git diff 85f55cd d4630e8 -- . ':!uv.lock'`
- **Reviewers**:
  - spec-compliance
  - senior-architecture
  - security
  - code-quality
  - test-coverage
  - documentation-accuracy
  - performance
  - orchestrator status-writer and sensitivity passes
- **Not run**: structural-quality (deep only).

**Next action**: fix R1–R3 and R5 (plus R6 and R7 before launch traffic). Decide R4: implement an eligibility pass at close, or document the limit. Add the boot file lock (R8). Then re-review.
<!-- /SECTION:revision-3 -->

---

<!-- SECTION:revision-4 -->
## Revision 4: fix log for revision 3 (2026-10-26)

**Status**: fixes implemented and committed; **re-review pending**. This revision records what changed and how each fix was verified. It is not a new verdict.

| Finding | Fix | Regression test(s) | Fails with the fix reverted? |
|---------|-----|--------------------|-------------------------------|
| **R1** one bad relay halts resolve | Per-relay malformed, wrong-round or self-inconsistent answers become "invalid" and are ignored. A value needs ≥ `min_agreeing` votes **and** more votes than any other value; a split vote keeps polling and then errors. Beacon poll cap is about 1 min (40 polls), and the runner retries and alerts. | `test_one_garbage_relay_does_not_block_two_agreeing_ones`, `test_one_forged_relay_is_outvoted_by_the_others`, `test_wrong_round_or_bad_signature_from_one_relay_is_ignored`, `test_split_vote_without_a_majority_is_never_resolved_by_picking_one` | yes, 2 fail |
| **R2** Retry is a no-op during backoff | Backoff waits on the sleep **or** a per-draw wake event. `spawn_resolve` on a running draw sets the event. | `test_admin_retry_wakes_a_draw_sleeping_in_backoff` | yes |
| **R3** staff told "already participating" | `enter()` checks exclusion before the entry code. `/status` says not eligible for excluded users. `not_eligible` text now names bot admins too. | `test_entrant_who_becomes_staff_or_channel_admin_is_told_not_eligible[staff, channel_admin]`, `test_status_tells_an_entrant_who_became_staff_they_are_not_eligible` | yes, 2 fail |
| **R4** insider veto after randomness | **Documented as a known limit** (README, TASK decision). The user hadn't chosen, so the default was taken; the eligibility-pass-at-close alternative is described. | (none; documentation) | n/a |
| **R5** ineffective or missing tests | M6 test is now parametrized over clock offsets 0/1/2. M5 test asserts the exact `channel_redraw_empty` post and the post count. New tests: past winner `ALREADY_WON` plus excluded from a 2nd snapshot; `ensure_committed` exhaustion (`CommitmentError`, 3 void rounds); round never replaced after randomness was fetched; migration-vs-models (`alembic upgrade`, `check`, `downgrade`); double-submit checks one admin alert carrying the accepted contact; handler tests for redraw!/activate/retry (with audit)/repeat presses. | `test_committed_round_starts_at_least_the_lead_after_close_at_any_time[0-2]`, `test_redraw_with_nobody_left_is_announced`, `test_past_winner_cannot_enter_again_and_is_left_out_of_the_next_draw`, `test_commitment_that_keeps_landing_late_gives_up_without_a_commitment`, `test_a_round_is_never_replaced_once_its_randomness_was_fetched`, `tests/test_migrations.py`, `test_admin_redraw_activate_and_retry_buttons` | M6: yes (offsets 1 and 2 fail); M5: yes |
| **R6** `/verify` re-uploads per request | `VerifyLimiter`: the code list is uploaded once per draw and Telegram's `file_id` reused; one request per user per 30 s. | `test_verify_reuses_the_uploaded_file_and_rate_limits_each_user` | n/a (new behaviour) |
| **R7** `register_start` holds a connection across `getChatMember` | Membership is checked before any session opens. Insert uses `INSERT … ON CONFLICT DO NOTHING`. | `test_membership_is_checked_outside_any_open_db_session`, `test_concurrent_first_start_creates_one_user_without_errors` | yes |
| **R8** no second-instance guard | `singleton.single_instance` takes an exclusive lock (`fcntl.flock` / `msvcrt.locking`) on `<db>.lock` at boot. A second process exits with a clear message. | `tests/test_singleton.py`; container check: lock file created in `/app/data` | n/a |

**Also fixed (from the revision-3 minors)**:
- **n1**: permanent Telegram errors (BadRequest, Forbidden, NotFound, Unauthorized) stop and alert instead of looping. `admin_commit_pending` is sent once per outage, and the beacon alert once (`test_permanent_telegram_error_stops_and_alerts_instead_of_looping`, `test_commitment_pending_is_alerted_once_per_outage`).
- **n2**: inline skip and void lists are capped at 25 (plus "+N more, full list in /verify"). An 80-skip, 200-void no-winner post stays under 4096 characters (`test_long_skip_and_void_lists_keep_the_post_within_telegram_limits`).
- **n3**: a failed redraw-empty post alerts admins (`test_failed_redraw_empty_post_is_reported_to_admins`).
- **n5**: alerting can't kill the retry loop, and housekeeping re-spawns COMMITTED/PAUSED draws that have no live task (`test_housekeeping_respawns_an_orphaned_committed_draw`).
- **n6**:
  - the channel-admin cache is warmed at boot;
  - close requires a fresh list (`fresh=True` raises) and the handler replies "try again" (`test_admin_list_is_cached_and_falls_back_only_for_entry_checks`, `test_close_reports_when_telegram_cannot_list_channel_admins`).
- **n7**: `BEACON_LEAD_SECONDS` minimum is now 60, as in the PRD.
- **n9 (partial)**: void rounds appear in the results and no-winner posts.
- **n10**:
  - `/verify` also requires `result_msg_id` (`test_verify_by_id_before_announcement_reveals_nothing`);
  - a duplicate claim message gets "already received" (`test_second_claim_message_is_acknowledged_not_answered_with_help`).
- **n11 docs**:
  - README covers the lead time, majority vote, Retry, the runbook "unexpected error" row, verifier scope, the lock and the R4 limit;
  - posts and `verify_text` spell out hex→bytes and big-endian, plus the skip rule;
  - `channel_skipped_line` has its trailing newline back;
  - `channel_void_note` no longer assumes an earlier post;
  - `models.py` comments are updated;
  - plan drift is declared in TASK.md.

**Still open (minor or deferred)**:
- The bot can't boot while fewer than 2 relays are reachable (n6, undeclared in the PRD sense).
- The host clock is trusted for "future" (n9).
- Void rounds aren't in `/verify` text or the verifier tool.
- A DRAWN/CLAIMED alert lost in a crash isn't resumed (n5 partial).
- "Expiry alert re-sent until delivered" is still overstated: delivery failures are swallowed (n8).
- Results-post *text* preview for admins.
- P1 stats and status gaps.
- M12 structure (`_cas` used by `Claims`, no transition table).
- Winner-name sanitising in the public post.
- A deep structural review is recommended.

**Verification (observed, working tree before commit)**:
- `uv run pytest`: 129 passed, 1 deselected.
- `uv run pytest -m live`: 1 passed.
- ruff check: clean; ruff format: 55 files formatted.
- Sensitivity (revert the fix, run its tests): M6 2 fail, M5 1, R3/R7 3, R1 2, runner (R2/n1/n5) 4. Code restored afterwards and the full suite re-passed.
- `docker compose build` plus boot: both migrations ran; clean exit "BOT_TOKEN was rejected by Telegram"; `giveaway.lock` created in `/app/data` (0600, owner `bot`).
- Staging end-to-end with real Telegram: **not run** (needs a test bot, channel and accounts).
<!-- /SECTION:revision-4 -->
