# PRD: Review.uz Weekly Giveaway Bot

**Created**: 2026-10-26
**Status**: Draft
**JTBD Reference**: [JTBD-weekly-giveaway-bot.md](../JTBD/JTBD-weekly-giveaway-bot.md) · **Version**: v0.1

---

## Context

Review.uz runs the Telegram channel **«Иқтисодий шарҳ»** ([@reviewuznews](https://t.me/reviewuznews)) with analysis on Uzbekistan and world economy, investments, foreign trade, regional development, housing and car markets. In-depth articles on [review.uz](https://review.uz/) sit behind a paid subscription (1 year = **650 000 sum**).

The announced contest: **every week one channel subscriber wins a free 1-year review.uz subscription**. To take part, a user subscribes to @reviewuznews, opens the contest bot, presses **«Иштирок этиш»** and stays subscribed until results. The bot picks the winner at random among eligible participants; the result is announced in the channel. Participation is free.

This PRD specifies that bot.

---

## Problem & Evidence

**Problem**: The channel needs to grow its subscriber base, and the paywalled site needs more people who have actually experienced its paid analysis. Readers interested in the economy see headlines but rarely pay 650 000 sum for full access on spec. A giveaway gives them a low-effort reason to subscribe to the channel now, but only works if participants believe the draw is real and fair.

**Evidence**
- The contest has been designed and its announcement text written by Review.uz (source: announcement supplied to this PRD) — the business commitment exists; this bot is the missing piece.
- Telegram's native channel giveaways can only award Telegram Premium / Stars; custom prizes are just an "additional prize" text on top ([Telegram blog — Giveaways](https://telegram.org/blog/giveaways)). Using it would force buying Premium codes and give no lead capture → a custom bot is needed.
- Giveaway bots are a known trust problem: many don't publish participant lists or a selection report ([GiveawayOK — reputation risks](https://giveawayok.com/en/blog/telegram-bots-giveaway-risks-reputation)). Fairness proof is a differentiator, not decoration.
- [NEEDS CLARIFICATION: current @reviewuznews subscriber count and recent monthly net growth — needed to set the growth target]

**Business Impact**: Primary — net growth of @reviewuznews subscribers who stay after the draw. Secondary (not a KPI for v1) — one-year winners become habitual readers of paid content and potential renewals.

---

## Non-Goals

- **No automatic prize activation.** Staff activate the subscription on review.uz manually; the bot only collects the winner's account identifier and tracks the claim.
- **No referral / "invite friends for extra tickets"** mechanics. One person = one entry.
- **No multiple winners or prize tiers** per week.
- **No bot/fake-account heuristics** (no-photo, account-age filters) in v1 — they are unfair to real users and unreliable.
- **No payments, quizzes or tasks** to enter. Entry stays free and one-tap.
- **No fully automatic scheduled draw.** An admin starts each draw.
- **No web dashboard.** All admin actions happen inside the bot.

---

## Goals & Metrics

**Goals**
- Turn the giveaway into measurable, retained channel growth.
- Make every draw publicly verifiable so the contest builds trust in the brand instead of eroding it.
- Keep weekly operation to a few admin taps.

**Success Metrics**

| Metric | Target | Timeframe | How Measured |
|--------|--------|-----------|--------------|
| New-to-channel entrants (not a member at first `/start`, member after entering) | [NEEDS CLARIFICATION: target, set once baseline is known] | First 8 weeks | Bot records channel membership status at first `/start` and at entry |
| 30-day retention of new-to-channel entrants | ≥ 70% still subscribed | 30 days after each entrant's entry | Bot re-checks `getChatMember` on a daily batch of entrants who reached day 30 |
| Net channel subscribers | [NEEDS CLARIFICATION: target vs baseline] | 8 weeks from launch | Telegram channel statistics, start vs end |
| Prize claimed within 48h | ≥ 80% of draws | Rolling | Claim timestamps in bot |
| Draw completed with published proof | 100% of weeks | Rolling | Each weekly post contains count, snapshot hash, beacon round, winner index |

**Guardrails**
- Channel unsubscribe rate in the 7 days after each results post doesn't exceed the pre-launch weekly average [NEEDS CLARIFICATION: pre-launch average].
- Zero draws that need to be re-run for any reason other than the 48h no-claim rule.
- Bot reply to a user button press < 2 s p95.

---

## User Stories & Requirements

**Personas**: *Reader* — a Telegram user interested in Uzbek economy, may or may not already follow the channel. *Winner* — a Reader who was drawn. *Admin* — Review.uz staff running the contest.

**User Stories**
- *As a* Reader, *I need* to join with one tap after subscribing, *so I can* be entered into every weekly draw without coming back. **Done when** pressing «Иштирок этиш» while subscribed confirms "You are in all upcoming weekly draws" and pressing again doesn't create a second entry.
- *As a* Reader who isn't subscribed, *I need* the bot to tell me exactly what's missing, *so I can* fix it in one step. **Done when** the bot shows a "Subscribe to @reviewuznews" button and a "Check again" button, and re-checking after subscribing completes the entry.
- *As a* Reader, *I need* to verify the draw myself, *so I can* trust that nobody chose the winner. **Done when** the results post contains the participant count, snapshot hash, drand round and winner index, and the bot's `/verify` explains how to recompute it, with a link to the anonymised entrant list. *Changed 2026-10-27 (product owner): the channel posts carry no technical details; all of them (hash, round, randomness link, winning position, skipped codes, void rounds) are in `/verify N`.*
- *As a* Winner, *I need* a clear message telling me I won and what to send, *so I can* get my subscription. **Done when** the Winner receives a DM (if reachable) asking for the email or phone registered on review.uz, submits it, and gets "Accepted — access will be activated by staff".
- *As an* Admin, *I need* to run the weekly draw and handle claims from inside the bot, *so I can* operate the contest in minutes. **Done when** the Admin can close entries, see the drawn winner, approve the post, mark the prize activated, and trigger a redraw after 48h, all from admin buttons.

**Requirements**

| Priority | Requirement | Acceptance Criteria |
|----------|------------|---------------------|
| **P0** | **Entry**: «Иштирок этиш» registers a Participant once; entry persists across all future weeks until they win or are excluded. *Changed 2026-10-27 (product owner): no join button; a subscribed user is entered automatically when the bot shows the rules (after /start and language choice); unsubscribed users get «Subscribe» + «I subscribed»; past winners and excluded users get no way to join.* | Second press responds "already participating"; DB holds one row per Telegram user |
| **P0** | **Subscription check at entry** via `getChatMember` on @reviewuznews; statuses `member`, `administrator`, `creator` count as subscribed | Unsubscribed user can't enter; gets subscribe + re-check buttons |
| **P0** | **Bot is admin of @reviewuznews** (required for reliable `getChatMember`) | Startup health check fails loudly if the bot lacks admin rights |
| **P0** | **Entry code**: each Participant gets a random, non-guessable entry code at entry, shown in the bot | Code is unique, stable across weeks, not derived from the Telegram ID |
| **P0** | **Draw snapshot**: Admin "Close entries" freezes the eligible pool = all Participants who entered before that moment, minus Past Winners and Excluded Staff, ordered by entry code | Snapshot stored immutably; snapshot hash = SHA-256 of the ordered entry-code list |
| **P0** | **Committed draws must complete**: once a commitment is posted, the results post is mandatory; Admin can't cancel or re-commit that week | No admin action exists to discard a committed draw; only the 48h redraw path |
| **P0** | **Commitment post**: at close, bot publishes to the channel the participant count, snapshot SHA-256 and a drand beacon round number in the future (≥ 1 min ahead). *Changed 2026-10-27 (product owner): the post shows only the count; hash and round are stored at close and first published with the results.* | Post exists before the beacon round is published; values match the stored snapshot |
| **P0** | **Verifiable selection**: once the beacon round is out, winner index = `SHA-256(beacon_randomness ‖ snapshot_hash)` as integer mod N | Any third party re-running the published formula on the published inputs gets the same index |
| **P0** | **Subscription re-check at draw**: drawn candidate is checked with `getChatMember`; if not subscribed, the next index (i+1 mod N) is taken, and each skip is recorded in the proof | Results post lists skipped indexes and the reason "not subscribed" |
| **P0** | **Results post** in the channel: masked winner (e.g. first name + `@ab***`), count, hash, round, index, skip list. Admin approves the text before it's posted | Post appears only after admin confirmation |
| **P0** | **Winner notification & claim**: DM to the winner asking for review.uz email/phone; results post tells the winner to open the bot | Claim form validates an email or a +998 phone; stores it; notifies admins |
| **P0** | **48h claim window + redraw**: if no claim within 48h of the results post, Admin can redraw — same snapshot minus the no-show, next drand round, same formula, new post | Redraw is only enabled after 48h; proof chain links the original and redraw |
| **P0** | **Past Winners excluded permanently** | Once drawn and claimed (or marked won), the user never enters a later snapshot |
| **P0** | **Staff exclusion**: channel administrators (from `getChatAdministrators`) plus an admin-managed list of staff Telegram IDs | Excluded users can press the button but are never in a snapshot; told politely they're not eligible |
| **P0** | **Admin access** restricted to a configured list of Telegram IDs | Non-admins never see admin commands; admin commands reject non-admins |
| **P0** | **Languages**: uz-Cyrl, uz-Latn, ru; chosen at first `/start`, changeable via `/language` | All user-facing texts exist in 3 languages; no mixed-language screens |
| **P1** | **Prize tracking**: Admin marks claim "activated"; bot DMs winner confirmation | Status flow: drawn → claimed → activated (or → expired → redraw) |
| **P0** | **`/verify`** command + published entry-code list per draw (no Telegram IDs, names or phones) | Recomputing SHA-256 of the published list reproduces the posted snapshot hash; any Participant can find their own code in it |
| **P1** | **Growth stats for Admin**: entrants total, new-to-channel entrants, entrants this week, 30-day retention | `/stats` returns numbers matching DB queries |
| **P1** | **`/status`** for Readers: "You are participating since <date>", the next draw info | — |
| **P2** | Weekly reminder broadcast to participants when a new draw closes | Respects Telegram broadcast limits; opt-out button |
| **P2** | Deep links with source tags (`/start=post_<id>`) to see which channel posts drove entries | Source stored on first `/start` |

**Non-Functional Requirements**

| Category | Target |
|----------|--------|
| Performance | Button response < 2 s p95; snapshot + draw for 100k participants < 30 s |
| Reliability | No lost entries on restart (persistent DB, not in-memory); draw is idempotent — a retried draw step yields the same result |
| Security & privacy | Admin IDs and bot token in env, not code; winner contact data visible to admins only; public posts show only masked names; entrant list published as entry codes only (hashing Telegram IDs is reversible by enumeration) |
| Rate limits | Respect Telegram limits (~30 msg/s global); broadcasts are queued |
| Auditability | Every admin action (close, draw, approve, redraw, activate, exclusion change) logged with admin ID + timestamp |
| Accessibility | Plain text + inline buttons; works in all official Telegram clients |

---

## Solution Outline

**Core Flow — Reader**
1. Reader taps the bot link in a channel post → `/start` → choose language (first time only). Bot records membership status at first contact.
2. Bot shows contest rules + «Иштирок этиш».
3. Press → bot checks subscription. Not subscribed → "Subscribe" + "Check again". Subscribed → "You're in every weekly draw. Stay subscribed until results."
4. On a winning draw → DM "You won!" → Reader sends review.uz email/phone → "Accepted, staff will activate within N days" [NEEDS CLARIFICATION: activation SLA] → later "Activated".

**Core Flow — Weekly draw (Admin)**
1. Admin → "Close entries" → bot shows pool size → confirm.
2. Bot freezes snapshot, computes hash, picks drand round ≥ 1 min ahead, posts **commitment** to the channel.
3. When the round is published, bot computes the index, re-checks the candidate's subscription (skip forward if needed), shows the winner to Admin.
4. Admin approves → bot posts **results** in the channel and DMs the winner.
5. Claim within 48h → Admin activates on review.uz → marks activated. No claim → Admin "Redraw" → steps 2–4 with the same snapshot minus the no-show.

**Key States**
- Participant: `not_started` → `entered` → (`won` | `excluded`)
- Draw: `open` → `committed` → `drawn` → `published` → `claimed` → `activated` | `expired` → `redrawn`
- Error: bot not admin of channel; drand unreachable (draw waits, admin is informed, no fallback randomness); Telegram API error on `getChatMember` (retry; never treat an error as "unsubscribed")

**Rules & Constraints**
- One entry per Telegram account; one prize per person ever.
- Must be subscribed at entry **and** at draw. Unsubscribing and resubscribing between those is allowed — only the draw-time check matters.
- Entries after "Close entries" count for the next draw.
- Randomness comes only from the public drand beacon; no server-side RNG is used for winner selection.
- [NEEDS CLARIFICATION: legal review of promotional prize draws in Uzbekistan — whether free-entry draws with a 650 000 sum prize need registration, published rules or tax handling on the winner's side]

**Experience Notes**
- Rules text in the bot mirrors the announcement exactly, in the chosen language.
- Masking: show first name + partial username so the community can recognize a real person without exposing them.

---

## Modules & Interfaces

**Modules touched / introduced** (all new — greenfield)

| Module | New / Modified | Purpose | Why deep (what it hides) |
|--------|----------------|---------|--------------------------|
| Participation | New | Register entries, enforce one-entry/one-win rules | Subscription status semantics, exclusion rules, idempotency |
| Membership check | New | Answer "is user X subscribed now?" | Telegram status mapping, admin-rights requirement, retries, rate limits |
| Draw | New | Snapshot → commitment → selection → redraw | Hashing, ordering, beacon fetch, skip logic, proof chain |
| Randomness beacon | New | Fetch drand round randomness | drand HTTP API, round timing, verification |
| Prize claim | New | Collect winner contact, track 48h window and activation | State machine, validation, deadlines |
| Messaging / i18n | New | Render all texts in 3 languages; post to channel; DM | Message catalogs, masking, Telegram send limits |
| Admin console | New | Admin-only commands and confirmations | Authorization, audit log |

**Interface changes**
- **Participation** — `enter(user)` is idempotent; returns `entered | already_entered | not_subscribed | not_eligible`. Never creates duplicate entries.
- **Membership check** — `is_subscribed(user) -> yes | no | unknown`. `unknown` (API failure) is never treated as `no` by callers.
- **Draw** — `close_entries() -> Commitment{count, snapshot_hash, beacon_round}`; `select(commitment) -> Result{index, skipped[], winner}`. Deterministic: the same inputs always give the same Result. A snapshot is immutable once committed.
- **Randomness beacon** — `randomness(round)` returns verified randomness or waits; it never substitutes local randomness.
- **Prize claim** — `submit(winner, contact)` accepted only within 48h of the results post; `expire()` enables redraw.

**Seam choices**
- Randomness beacon is a seam with two adapters: drand HTTP (production) and a fixed-value fake (tests), so draw determinism is testable.
- Membership check is a seam: Telegram adapter (production) and an in-memory fake (tests).

---

## Research Findings

**How Similar Products Solve This**
- Telegram native Giveaways: random winners chosen by Telegram, eligibility by all/new subscribers, country, extra channels; prizes limited to Premium/Stars + text "additional prize"; winners get gift-code links as public proof ([telegram.org/blog/giveaways](https://telegram.org/blog/giveaways)). Strong trust; doesn't fit a custom prize without buying Premium.
- Third-party giveaway bots (e.g. TgDraw, BotMother template) offer subscription checks and a "fair randomizer" but publish no verification method ([BotMother template](https://botmother.com/templates/giveaway), [TgDraw](https://www.producthunt.com/products/tgdraw)).

**Competitive Landscape**
- Gap: none of the reviewed bots offer a draw that outsiders can independently recompute. Opaque bots are a known reputational risk ([GiveawayOK](https://giveawayok.com/en/blog/telegram-bots-giveaway-risks-reputation)). Commit + public beacon closes that gap cheaply.

**Technical Approach Research**
- `getChatMember` is only guaranteed for other users if the bot is an administrator of the chat; a user who left still returns an object, with status `left`/`kicked` ([aiogram docs — getChatMember](https://docs.aiogram.dev/en/v3.14.0/api/methods/get_chat_member.html)).
- drand (League of Entropy) is a public, verifiable randomness beacon with fixed-period rounds that can be pre-committed to ([drand.love](https://drand.love)).

---

## Shipping & Risks

| Item | Details |
|------|---------|
| **Dependencies** | Bot added as admin to @reviewuznews; list of admin + staff Telegram IDs; review.uz staff process for manual activation; translations into uz-Latn and ru; drand public API |
| **Risks** | Subscription skips aren't publicly verifiable (`getChatMember` results are private) → the skip rule is deterministic and every skip is published with its index; a skipped entrant can contest it via the bot; admins can't choose which index is skipped. · drand outage on draw day → draw waits and admin is told; post explains delay (no local fallback, by design). · Winner unreachable (blocked bot) → results post tells them to open the bot; 48h rule then applies. · Mass fake accounts → accepted for v1; revisit with data. · Legal non-compliance → legal check before the first draw. |
| **Assumptions** | Readers trust a recomputable proof even if few actually verify → validate by watching comments on first results posts. · Manual activation is enough at 1 winner/week → fine at this volume. · One-tap carry-over entry keeps people subscribed → validate via 30-day retention. |

**Open Questions**
- [ ] Current subscriber baseline and growth target
- [ ] Legal requirements for promotional draws in Uzbekistan
- [ ] Staff SLA for activating the prize after a claim
- [ ] Day/time of the weekly draw to announce in rules

---

### PRD Lite (single-page version)

1. **Problem**: @reviewuznews needs retained growth; a giveaway only works if it's trusted. Native Telegram giveaways can't award a review.uz subscription.
2. **Non-Goals**: auto-activation, referrals, fake-account heuristics.
3. **Success Metric**: new-to-channel entrants retained at 30 days ≥ 70% (bot `getChatMember` re-check).
4. **Key Story**: As a Reader, I tap «Иштирок этиш» once while subscribed and I'm in every weekly draw. Done when the second tap says "already participating".
5. **Launch Checklist**:
   - [ ] Bot is admin of @reviewuznews
   - [ ] Legal check done
   - [ ] Baseline recorded
   - [ ] Three-language texts approved

---

## Cross-AI Validation: SKIPPED

| Validator | Status | Reason |
|-----------|--------|--------|
| Codex CLI 0.130.0 | SKIPPED | `401 Unauthorized`: CLI not authenticated (request `req_bb311f53fc714a3eac686228c15da9c1`) |
| Cursor / Antigravity | SKIPPED | CLI not installed |

**Fallback self-review (adversarial, draw design only), applied in v0.1:**
- Hashing raw Telegram IDs for the public list was reversible by enumeration. Replaced with random per-participant entry codes.
- An admin could withhold the results after seeing the beacon. Committed draws are now mandatory, and no cancel path exists.
- Subscription skips can't be verified publicly. Recorded as a known risk with a deterministic rule and a contest path.

Rerun the validation once `codex login` is done: `/product` Step 6, or `/codex-cli` on this file.
