# Jobs-to-be-Done: Review.uz Weekly Giveaway Bot

**Created**: 2026-10-26
**Status**: Draft
**Version**: v0.1

---

## Job Statement

**When** I follow Uzbek economic news on Telegram and keep running into in-depth analysis I'd value but wouldn't pay 650 000 sum for on spec, **I want** a low-effort, honest chance to get full access, **so I can** read the paid analysis for a year without paying and without risking my data or time on a fake giveaway.

**Candidate Variants**

1. **When** I see a giveaway in a channel I'm curious about, **I want** to enter in one tap and not have to come back, **so I can** keep a chance of winning without it becoming a chore.
2. **When** a giveaway announces a winner, **I want** to see for myself that the pick wasn't rigged, **so I can** keep trusting the channel and not feel used for subscriber numbers.

Primary: the main statement above. Variant 1 drives the "one press, all weeks" decision; variant 2 drives the verifiable draw.

---

## Success Criteria

**Functional**
- Entry takes one tap after subscribing, and it carries over to all future weeks.
- The user always knows their status: participating, not subscribed, not eligible, or won.
- A winner can claim with one message (their review.uz email or phone) and gets a confirmation when access is activated.
- The draw's inputs and formula are published, so anyone can recompute the winner.

**Emotional Personal**
- Safe: the bot asks for nothing until you win, and then only a review.uz login.
- At ease: there's nothing to remember each week.

**Emotional Social**
- Smart, not gullible: a reader of a serious economics channel who joined a giveaway that can be proven fair.
- A recognizable winner: friends in the comments can see a real person won.

---

## Four Forces of Switching

**Promoting Forces**

| Force | Description |
|-------|-------------|
| **Push** | The paywall hides the analysis the reader actually wants. 650 000 sum is a lot to pay for an unknown. Free news sites give only headlines. |
| **Pull** | A year of paid analysis for free, at the cost of one subscription and one tap. A weekly winner keeps the chance alive. |

**Blocking Forces**

| Force | Description |
|-------|-------------|
| **Anxiety** | "Giveaways are rigged" or "it's just to inflate subscribers". Fear of spam or of handing personal data to an unknown bot. Doubt that the prize will actually be delivered. |
| **Habit** | Scrolling past giveaways. Reading free sources (other news channels and sites) is good enough. Reluctance to add yet another channel. |

**Current Alternatives**
- Free Uzbek news channels and sites: hired for speed and headlines, but they lack in-depth analysis and data.
- Reading the review.uz free tier and Telegram summaries: hired because it's free, but it stops at the paywall.
- Non-consumption: just skipping the in-depth analysis, because the price isn't justified without trying the product.

---

## User Context

- **Who**: A Telegram user who follows the economy, business or markets in Uzbekistan (entrepreneurs, analysts, students, investors in housing or cars) and hasn't paid for review.uz.
- **Trigger moments**: Seeing the contest post in @reviewuznews or a repost. Hitting the review.uz paywall on a shared article. Seeing a results post with a real winner.
- **Where/When**: On mobile Telegram, in short sessions, in Uzbek (Cyrillic or Latin) or Russian.
- **Constraints**: Low patience for multi-step bots, wary of sharing a phone number, and must keep the channel subscription until results.

---

## Research Findings

**Competitive Alternatives**
- Telegram native Giveaways are trusted because Telegram runs the draw and gift links act as proof, but the only prizes are Premium or Stars ([telegram.org/blog/giveaways](https://telegram.org/blog/giveaways)).
- Third-party giveaway bots check subscriptions but rarely prove fairness, which is a known trust problem ([GiveawayOK](https://giveawayok.com/en/blog/telegram-bots-giveaway-risks-reputation)).

**Industry Patterns & Best Practices**
- Requiring a subscription with a `getChatMember` check is standard. The bot must be a channel admin ([aiogram docs](https://docs.aiogram.dev/en/v3.14.0/api/methods/get_chat_member.html)).
- Public randomness beacons let anyone verify a draw ([drand.love](https://drand.love)).

**Market Signals**
- Telegram built giveaways into channels because growing a channel through prizes is a mainstream pattern ([telegram.org/blog/giveaways](https://telegram.org/blog/giveaways)).
- Review.uz has already committed to the contest and written its announcement (supplied evidence).

---

## Notes

- This JTBD covers the Reader's job. The channel's job (retained growth) is captured as the business impact in the PRD.
- The anxiety force is the one most likely to sink the contest, so the PRD answers it with a verifiable draw, minimal data collection and masked public results.
