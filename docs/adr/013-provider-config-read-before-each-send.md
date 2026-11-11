# ADR-013: Read the Provider's Config Before Each Send

**Status:** Accepted (2026-11-12). Revised the same day after outside review: the scenario
rewritten without exotic timing, the config response validated (strict parsing, internal
consistency, an agent-side cap, integer milliseconds), the precedence pointed at Part 3 as
well, and a deterministic race test added.

**Locked 2026-11-12.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Precedence:** supersedes **the source of the window in ADR-012 Part 3's age check** (it's
the value read just before each send, not the cached one) and the matching refresh rule in
Part 4. It also replaces `GET /config`'s field names: durations are reported as integer
milliseconds. Everything else in ADR-012 stands, including the cache, which still gates
claiming and alerting.

## Context
ADR-012 keeps every send inside the provider's key lifetime with a per-send age check:

`now() − first_send_started_at + client_timeout ≤ min(send_window_seconds, current window)`

where "current window" is refund-api's `max_retry_window_seconds` from `GET /config`, cached
and refreshed on an interval. refund-api stamps each key's `expires_at` when it **commits**
the key, using the retention in force **at that moment** (ADR-012 Part 5).

A self-review after locking ADR-012 found the gap between those two facts:

1. The agent's cache says the window is **600 s**.
2. refund-api is redeployed with a smaller retention, **100 s** (window 50 s), which is
   valid on its side because 100 > 50 + recovery delay.
3. Before the agent's next refresh, a first send reaches the new instance and **commits**,
   stamped with the new, shorter life (expires at commit + 100 s), but the **response is
   lost**.
4. The retry is still bounded by the cached, larger window: `min(600 snapshot, 600 cached)`
   allows a retry at 200 s, with a key refund-api has already pruned. That's a second refund.

No unusual timing is needed: any retention decrease plus one lost response inside a refresh
interval is enough. Per-key expiry protects a key from config changes made *after* it was
stamped; it can't protect a key stamped *under* a new config while the agent still holds the
old one. It's the same class of failure ADR-012 Parts 3 and 5 exist to prevent.

Constraints: only durations cross the boundary between the two services, never timestamps
(the age check runs on the agent's database clock, the expiry on refund-api's); the decision
mustn't couple chat to refund-api (ADR-012 Part 4); deterministic tests.

## Options Considered

### Option A: Read `GET /config` immediately before each send — CHOSEN
- How it works: Before **every** send, including the first, the worker reads `GET /config`
  and uses the value it just read in the age check:
  `now() − first_send_started_at + client_timeout ≤ min(send_window_seconds, window just read)`.
  The order of each round is fixed:
  1. **Read** `GET /config`.
  2. **Age check** on the agent's database clock (and, for the first send, set
     `send_window_seconds` from the value just read, in the transaction that moves the run
     to `EXECUTING`).
  3. **Fenced update:** attempt count + 1 and `last_attempt_at` (ADR-012 Part 3).
  4. **Send**, inside the total per-attempt timeout.
  - **If the read fails** (unreachable, timeout, 5xx): nothing is sent and **no attempt is
    counted**, because nothing reached refund-api. The worker backs off on the normal
    schedule; the run keeps aging, so a long failure ends in the age check sending it to
    reconciliation (ADR-012's long-outage rule).
  - **If the read returns 401/403:** nothing is sent, the worker releases its claim, the gate
    closes and it pages, exactly as when the interval refresh sees it.
  - **If the window just read is below the worker's worst case:** no new claims, and an
    alert (ADR-012). The run in hand still goes through the age check with the smaller
    value, which can only make it stop sooner.
  - **The cache stays**, refreshed on its interval, for one job only: deciding whether the
    worker *claims* runs at all (the gate), and alerting. The age check never uses it.
  - **The read is validated, because every send now trusts it.** A buggy
    `max_retry_window_ms: 1000000000` would otherwise set a huge snapshot on the first read
    and make every later `min()` equally huge. Each read must pass all of these, or it
    counts as a **failed read** (nothing sent, no attempt counted) plus an alert:
    - parsed with the same **strict parser** as ADR-012 Part 1: no duplicate keys, no
      `NaN`/`Infinity`, **exact integer types** (durations are integer milliseconds, so
      nothing is rounded);
    - **internally consistent:** `max_retry_window_ms > 0`, and
      `key_retention_ms > max_retry_window_ms + client_timeout`. Both values come from the
      same response, so the agent checks this without knowing anything else;
    - **capped by the agent:** a maximum window from the agent's own config
      (`REFUND_CLIENT_MAX_PROVIDER_WINDOW_SECONDS`), so even a consistent but absurd
      response can't stretch the bound.
- **Why this closes the gap:** after a retention decrease, every later send reads the new
  window *W_new*, and refund-api's own startup guard guarantees its retention *R_new* >
  *W_new* + recovery delay. A key stamped under the new config lives at least *R_new* from a
  commit that happened after the first send, so any send the age check allows (elapsed +
  timeout ≤ *W_new*) reaches a live key. Only the attempt already on the wire was decided
  under the old value, and it has already been sent.
- Pros: Closes the gap structurally: no procedure to follow, no timing to tune. Uses an
  endpoint that already exists. Each send's bound is the window in force when it's sent.
- Cons: One extra authenticated request per send. refund-api being unreachable now also
  stops sends that were already claimed (they wait, then reconcile), which is the right
  outcome but a little earlier than before.
- Cost/latency/complexity profile: One small GET per send, a few milliseconds; trivial at
  this project's scale.

### Option B: A shorter refresh interval — rejected
- How it works: Refresh the cache every few seconds instead of every minute.
- Pros: No change to the send path.
- Cons: Narrows the gap but never closes it; whatever the interval, a decrease inside it
  still leaves a retry allowed under the old window.
- Why we didn't use it here: A smaller hole is still a hole.
- When it WOULD be the better choice: When a per-send request is genuinely expensive and the
  residual risk is accepted and written down.

### Option C: The client declares the lifetime it needs; the provider stamps the longer one — rejected
- How it works: Each send carries the remaining window the agent still needs, as a duration
  (for example a header). refund-api stamps `expires_at = now() + max(retention, requested)`,
  capped at a maximum.
- Pros: Correct even across a rolling deploy with mixed configs, because the lifetime travels
  with the request that creates the key.
- Cons: A contract change, and a client that can extend server-side state needs a cap and a
  policy for abuse. More moving parts than the problem needs with one provider we control.
- Why we didn't use it here: Option A closes the gap without changing what refund-api
  promises.
- When it WOULD be the better choice: Multiple provider instances deployed with rolling,
  mixed configs, or a provider that can't serve a cheap config read.

### Option D: A deploy rule for retention decreases — rejected
- How it works: Lower the window first, wait at least the old window plus one refresh
  interval, then lower the retention.
- Pros: No code.
- Cons: A procedure nobody checks; one hurried deploy reopens the gap.
- Why we didn't use it here: The same reason ADR-012 rejected deploy rules twice: an
  unchecked procedure isn't an invariant.
- When it WOULD be the better choice: Never as the only protection; acceptable as extra
  guidance next to a check.

### Option E: refund-api refuses retention decreases until old windows have drained — rejected
- How it works: refund-api records the largest window it has announced and when; at startup
  it refuses a retention below that window plus the recovery delay until enough time has
  passed.
- Pros: Enforced by the provider itself, independent of any client.
- Cons: Persistent announcement history, a startup rule that can block urgent deploys, and
  logic that has to be right across restarts. Heavy for one provider and one client.
- Why we didn't use it here: Option A gets the same safety with one read and no new state.
- When it WOULD be the better choice: A provider with many independent clients it can't
  coordinate.

## Decision
Option A. Before every send the worker reads `GET /config` and bounds the send by
`min(send_window_seconds, window just read)`. The interval-refreshed cache only gates claiming
and alerting.

**Remaining limit, recorded in `KNOWN_TRADEOFFS.md`:** during a *rolling* deploy of
refund-api, two instances can briefly run different configs, so the config read and the send
could reach different instances. The demo runs a single refund-api instance. If that ever
changes, Option C becomes the right design.

### Tests this ADR adds (each with a negative control)
| Test | Negative control |
|---|---|
| A first attempt commits after retention is decreased; the next retry reads the new window and the run goes to `NEEDS_RECONCILIATION` without sending | With the cached window, the retry is sent |
| The per-send read fails → nothing is sent and the attempt count is unchanged | Counting the failed read as an attempt reaches the attempt limit early |
| The per-send read returns 401 → nothing is sent, the claim is released, and it pages | A valid token sends |
| The first send sets `send_window_seconds` from the value read just before it | A snapshot taken from the cache is caught by a decrease-before-first-send test |
| An invalid config read (retention ≤ window + client timeout, a window above the agent's cap, a non-integer or duplicated field) → nothing sent, no attempt counted, an alert | Trusting the value as read sends |
| A deterministic race through a seam: the config changes between the per-send read and the send, so the key is stamped under the new, smaller retention; the next retry reads the new value and stops | Reusing the earlier read for the retry sends it |

## Consequences
- One authenticated `GET /config` per send, in addition to the interval refresh.
- `GET /config` reports `max_retry_window_ms`, `key_retention_ms` and
  `processing_budget_ms` as integers (replacing ADR-012's `*_seconds` names).
- The agent gains `REFUND_CLIENT_MAX_PROVIDER_WINDOW_SECONDS`, a hard cap on any window it
  accepts.
- The worker's send loop gets a fixed order: read → age check → fenced update → send.
- `KNOWN_TRADEOFFS.md` gains the rolling-deploy limit.
- Pointer lines go into ADR-012 (Part 3's age check and Part 4's refresh) when this ADR is
  locked.
- **Revisit if:** refund-api runs multiple instances with rolling deploys (then Option C), or
  per-send reads become measurably expensive (then Option B, with the residual risk written
  down).

## Interview-ready summary
"After locking the retry design, I reviewed it adversarially myself and found a mirror of the
bug the reviews had closed. Keys were stamped with the retention in force when they commit,
but my agent bounded retries with a cached copy of the provider's window. If retention dropped
between two refreshes and a lost first attempt committed after the drop, my agent could retry
with a key the provider had already deleted. The fix is to read the provider's config
immediately before every send and bound that send by the window just read. Because every send
now trusts that read, it's strictly parsed, checked for internal consistency and capped by my
own config; a read that fails any of that sends nothing and doesn't count as an attempt. I rejected a shorter refresh interval,
because it only shrinks the hole, and a deploy rule, because nobody checks procedures. The
one thing it doesn't cover is a rolling deploy with mixed configs; there the right design is
for the request to carry the lifetime it needs."
