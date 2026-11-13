# ADR-019: The Agent's Demo Seed — What a Re-run Changes, and Dates That Don't Age Out

**Status:** Accepted (2026-11-13). Both options chosen by the user in chat when the agent's half
of the seed was started; not yet externally reviewed.

**Precedence:** fills in ADR-010 Part 2 ("the agent's seed command writes orders") for the agent
side. refund-api's seed is unchanged: it reads only the charge fields and ignores the rest.

## Context
ADR-010 settled that one deploy-time manifest feeds two owner-role seed commands, one per
service, so orders and charges can't drift. Two things it left open, both on the agent side:

1. **Re-runs.** refund-api refuses any difference in an existing charge, because charges are
   immutable. An agent order mixes **facts** that mirror refund-api's charge (owner, charged
   amount, currency) with **fulfillment state** that really changes (status, delivery time,
   chargeback flag).
2. **Dates age out.** The manifest held absolute `delivered_at` timestamps. ADR-009's R08 allows a
   refund only within the refund window (30 days in the example config) of delivery, so every demo
   order would fall outside policy a month after the manifest was written: the live demo would stop
   issuing refunds with no error, just `OUT_OF_POLICY`. A portfolio demo may be opened months later.

Constraints: the seed runs as the owner role at deploy time (ADR-005, ADR-010); no shared code with
refund-api (ADR-007); the manifest stays the single input for both services.

## Options Considered

### Part 1 — An existing order that differs from the manifest

#### Option A: Facts must match (refuse, write nothing); fulfillment state is updated — CHOSEN
- How it works: in one owner transaction, every existing order's owner, charged amount and
  currency are compared with the manifest; any difference refuses the whole seed. Otherwise new
  customers and orders are inserted and existing orders' status, delivery time and chargeback flag
  are set from the manifest.
- Pros: the facts stay pinned to refund-api's immutable charges; re-running the seed can move demo
  orders along (and keep their dates fresh, Part 2); all-or-nothing.
- Cons: the seed becomes a writer of mutable order state, so a re-run overwrites state changed by
  any other path (none exists today; ADR-009 decisions keep their own input snapshots, so history
  stays auditable).
- Cost/latency/complexity profile: one comparison and one update per existing order.

#### Option B: Refuse any difference (mirror refund-api) — rejected
- Pros: simplest; the same rule in both services.
- Cons: a demo order can't change state, and with Part 2's relative dates every re-run would be
  refused.
- Why we didn't use it here: orders aren't immutable the way charges are.
- When it WOULD be the better choice: an agent whose orders are written once by an upstream system.

#### Option C: Insert-only, ignore differences — rejected
- Pros: never fails on a re-run.
- Cons: drift between manifest and database is silent, including the facts refund-api relies on.
- Why we didn't use it here: a silent mismatch on a charged amount is exactly what ADR-010's single
  manifest exists to prevent.
- When it WOULD be the better choice: never for facts shared with another service.

### Part 2 — Delivery dates

#### Option A: Relative (`delivered_days_ago`), resolved against the database clock at seed time — CHOSEN
- How it works: the manifest's agent field is `delivered_days_ago` (a whole number, null unless
  delivered); the seed stores `now() - interval` on the database clock. Re-running the seed (each
  deploy, or a scheduled job later) keeps demo orders inside the window.
- Pros: the demo keeps working without editing the manifest; one clock (the database's, ADR-005).
- Cons: delivery times move on every run; a demo left without a re-run for longer than the window
  still ages out.
- Cost/latency/complexity profile: none.

#### Option B: Absolute dates, documented — rejected
- Pros: deterministic; nothing moves.
- Cons: the demo breaks silently a window after the manifest was written, until someone edits it.
- Why we didn't use it here: a portfolio demo must work when it's opened, not when it was written.
- When it WOULD be the better choice: fixtures for tests that pin exact boundaries.

#### Option C: A longer refund window for the demo — rejected
- Pros: no seed change.
- Cons: changes the policy under demonstration to hide a data problem; R08's behavior is part of
  what the demo shows.
- Why we didn't use it here: it demonstrates a different policy than the one built.
- When it WOULD be the better choice: never as a fix for stale data.

### Smaller decisions
- **S1. Strict manifest for the agent:** the agent's order model forbids unknown fields (a stale
  `delivered_at` is refused, not ignored); refund-api keeps ignoring the agent's fields.
- **S2. Consistency rules checked before writing:** `delivered` exactly when `delivered_days_ago`
  is set, days non-negative, duplicate order ids refused, the same ID and currency patterns as the
  request model.
- **S3. Customers are inserted if missing and never changed** (a lock target only, ADR-005).
- **S4. Exit codes as refund-api's seed:** 0 done, 1 refused (nothing written), 2 usage.

## Decision
Part 1 A, Part 2 A, S1–S4.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| A first run inserts customers and orders; delivery times are `now() - days` on the database clock | — |
| A re-run with a changed status and chargeback flag updates them and refreshes delivery times | A seed that skips updates leaves them stale |
| A changed charged amount (or owner, or currency) refuses the seed and writes nothing, not even new orders | Comparing only the amount lets a changed owner through |
| An unknown field (`delivered_at`), `delivered` without days, days on an undelivered order, negative days, a duplicate id: refused | — |
| The demo manifest is valid for the agent's seed (and refund-api's own test keeps it valid for refund-api) | — |
| Exit codes and messages | — |

## Consequences
- `python -m agent.seed <manifest>` as `AGENT_DB_OWNER_URL`; the README's deploy steps run both
  seeds.
- `infra/seed/demo_manifest.json` uses `delivered_days_ago`.
- `KNOWN_TRADEOFFS.md`: a demo not re-seeded for longer than the refund window ages out; a re-run
  overwrites fulfillment state.
- **Revisit if:** orders get another writer (then the seed must not overwrite state it doesn't
  own); demo identities move to Clerk (ADR-003), when the manifest's owner ids must be the demo
  accounts' Clerk user ids.

## Interview-ready summary
"Both services seed from one manifest, so the agent's orders and the provider's charges can't
drift. Two details mattered for a demo that has to work months later. A re-run must refuse if an
order's money facts changed, because they mirror the provider's immutable charge, but it may move
the order's fulfillment state along. And dates in a fixture rot: the refund window is 30 days, so
absolute delivery dates would have silently put every demo order out of policy a month later.
The manifest says 'delivered 3 days ago' instead, resolved on the database clock each time it
runs."
