# ADR-024: The Customer Lock's Mode — `FOR NO KEY UPDATE`, So Foreign-Key Checks Don't Wait

**Status:** Accepted (2026-11-16), as drafted.

**Locked 2026-11-16.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Supersedes:** ADR-005's lock clause only: step 2 of *Concurrency control* (`SELECT … FROM
customers WHERE id = :user_id FOR UPDATE`) becomes `FOR NO KEY UPDATE`, and its claim that
"unrelated writes never contend with limit checks" is corrected below. Everything else in
ADR-005, the lock order (`customers` first), the lock timeout and the "never across HTTP" rule,
is unchanged. ADR-009's lock order (`customers → conversations → runs → policy_controls`) is
unchanged.

## Context
ADR-005 serializes every per-customer limit check by locking the customer's row: the worker's
pre-send check (ADR-004 step 6), the proposal path (ADR-009, built in turn slice T4) and the demo
seed (ADR-019) all take it. ADR-005 says the row is "only a lock target: no other code path
updates it … so unrelated writes never contend with limit checks".

That last claim is wrong. Three tables reference `customers.id` by foreign key: `orders`,
`conversations` and `runs`. Postgres checks a foreign key on insert by taking `FOR KEY SHARE` on
the referenced row, and `FOR UPDATE` conflicts with `FOR KEY SHARE`. So while a limit check holds
a customer, **inserting a run, a conversation or an order for that customer waits.**

Found by a T4 test: holding the customer lock from the turn's start made `start_turn` fail
before any model call, with `LockNotAvailable` on `INSERT INTO runs` (the FK check). Measured
2026-11-16 on our Postgres, holder in one connection, second connection with a 300 ms lock
timeout:

| Holder's lock | Insert referencing the customer | Second limit check on the same customer |
|---|---|---|
| `FOR UPDATE` (today) | blocked (`LockNotAvailable`) | blocked |
| `FOR NO KEY UPDATE` | **proceeds** | blocked |

What it costs today: the lock is held for one short transaction, so normally a customer's new
message in another conversation waits milliseconds. If a holder runs long (a slow statement,
up to the statement timeout), the new turn's start exceeds `TURN_LOCK_TIMEOUT_SECONDS` and fails
with a database error instead of a reply. No money risk: correctness is untouched, only
liveness of a second conversation.

Constraints: the policy checks must stay serialized per customer (ADR-005 write skew, ADR-009
R05/R06/R11/R12/R16/R17); the lock order must not change (deadlock freedom); no new
infrastructure.

## Options Considered

### Option A: `FOR NO KEY UPDATE` on the customer row — CHOSEN
- How it works: every taker of the customer lock (`presend.lock_customer`, used by the pre-send
  check and the proposal path, and the seed) asks for `FOR NO KEY UPDATE` instead of `FOR
  UPDATE` (SQLAlchemy `with_for_update(key_share=True)`). It conflicts with itself, `FOR SHARE`
  and `FOR UPDATE`, so two limit checks still serialize; it doesn't conflict with `FOR KEY
  SHARE`, so foreign-key checks pass.
- Pros: one word per taker; same row, same lock order, same timeout; visible in `pg_locks` like
  today; the guarantee we need is exactly "limit checks exclude each other", which it keeps.
- Cons: it only promises the row's key won't change while held. That's fine: nothing updates or
  deletes a customer row (ADR-005), and the key is the Clerk user id.
- Cost/latency/complexity profile: none added; removes waits.

### Option B: A transaction-scoped advisory lock keyed on the customer — rejected
- How it works: `pg_advisory_xact_lock(hashtextextended(:user_id, 0))` instead of a row lock.
- Pros: no row lock at all, so nothing else can ever contend with it.
- Cons: a hash, so two customers can share a key and serialize each other (rare, invisible);
  advisory locks show in `pg_locks` without the customer's id; every taker changes, and the
  ADR-005/009 lock order now mixes a non-row lock with row locks, which makes it harder to reason
  about.
- Why we didn't use it here: Option A removes the contention with a one-word change and keeps
  every existing property.
- When it WOULD be the better choice: when there's no row to lock (customers not stored in this
  database), or when the locked row itself is updated by unrelated writes.

### Option C: Keep `FOR UPDATE`; answer "try again" when a turn's start hits a lock timeout — rejected as the fix
- How it works: `start_turn` catches `55P03` and the chat API replies "please try again", like
  ADR-009's proposal-time lock timeout.
- Pros: no change to the lock; also covers turn-start waits with other causes (KNOWN_TRADEOFFS:
  flagging a pending run takes its row lock in the start transaction).
- Cons: treats the symptom: the second conversation still waits, and still fails past the
  timeout; it just fails politely.
- Why we didn't use it here: the wait has no purpose; removing it beats handling it.
- When it WOULD be the better choice: as defense in depth for the other turn-start lock waits.
  That mapping belongs to the chat API (it decides the HTTP status), so it's recorded there, not
  built here.

### Option D: Drop the foreign keys to `customers` — rejected
- How it works: `orders`, `conversations` and `runs` keep `user_id` as plain text.
- Pros: no FK check, so no contention under any lock mode.
- Cons: loses referential integrity on the column every ownership rule (R03, the order-lookup
  `WHERE`) depends on.
- Why we didn't use it here: integrity on ownership columns is worth more than a lock mode.
- When it WOULD be the better choice: a high-write system where the parent row is hot and
  integrity is enforced elsewhere (application or batch checks).

### Option E: A separate lock table nothing references — rejected
- How it works: `customer_locks(customer_id PK)`, one row per customer, locked `FOR UPDATE` by
  limit checks; no foreign key points at it.
- Pros: makes "this row is only a lock target" true by construction.
- Cons: a migration, a row to create on first sight (another `ON CONFLICT DO NOTHING` path), and
  a second table that must stay in step with `customers`, for the same effect as Option A.
- Why we didn't use it here: Option A gets there with no schema change.
- When it WOULD be the better choice: if the customer row ever starts getting real updates
  (profile fields), so even `FOR NO KEY UPDATE` would contend with them.

## Decision
We choose Option A: the customer lock is `FOR NO KEY UPDATE` everywhere it's taken (the
pre-send check, the proposal path and the seed, through one shared helper so they can't drift).
It keeps the one property we need, limit checks on one customer exclude each other, and
removes the measured contention with foreign-key checks, with no schema change and no change to
the lock order or timeouts.

The seed's guarantee still holds: it relies on excluding limit checks, so "has an unfinished run
on this order" can't change under it. A run gets an `order_id` only through a proposal, which
holds the customer lock; a run created concurrently has none.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| While the customer lock is held (the production helper, in another transaction), inserting a conversation, a run and an order for that customer completes within a short lock timeout | `FOR UPDATE` planted back → `LockNotAvailable` |
| A turn in a new conversation starts and answers while a limit check holds the same customer | `FOR UPDATE` → the start fails on `INSERT INTO runs` |
| A second limit check on the same customer still waits (`pg_locks` shows it blocked) | The lock removed → it doesn't wait |
| Existing, re-run unchanged: the pre-send race (ADR-005), concurrent proposals on one order (T4), the seed waiting for a pre-send check | Their existing no-lock controls still fail |
| The seed takes the lock through the same helper as the limit checks | The seed with its own `with_for_update()` fails the identity check |

## Consequences
- A customer's other conversations no longer wait on that customer's limit checks; the
  lock-timeout failure at turn start from this cause is gone.
- Limit checks are serialized exactly as before; nothing about R05–R18's per-customer exactness
  changes because of this ADR.
- We rely on the customer row never being updated or deleted. ADR-005 already says so; if
  profile data ever moves into `customers`, revisit (Option E).
- Turn-start lock waits from other causes (the C01 flag on a pending run) still surface as a
  database error until the chat API maps `55P03` to "try again" (Option C, recorded for that
  slice).
- **Noticed while writing this, not changed here:** ADR-009 calls R18 exact per customer, but
  an escalation at turn start (C01, a spent budget, the `escalate` tool) doesn't take the
  customer lock, so a proposal in another conversation can miss one that commits at the same
  moment. R18 only raises a proposal to review, never moves money, so this goes to
  KNOWN_TRADEOFFS as an approximation rather than a lock change (taking the customer lock at
  turn start would also break the lock order: the start already holds the conversation).
- **Revisit if:** the customer row gains updatable columns, or a new table needs a foreign key to
  `customers` written inside a limit check of another customer (it can't: one lock per check).

## Interview-ready summary
"I serialize refund limit checks per customer by locking the customer's row. A test showed the
lock was blocking more than it should: inserting any row with a foreign key to that customer
takes a key-share lock on it, and FOR UPDATE conflicts with that, so a proposal in one
conversation could stall the start of another conversation for the same customer. I measured it,
then switched to FOR NO KEY UPDATE, which still makes two limit checks wait for each other but
lets foreign-key checks through. The alternatives were an advisory lock, which works but hashes
customers into shared keys and hides who holds it; dropping the foreign keys, which gives up
integrity on the ownership columns; a separate lock table, which is the right move once the
customer row gets real updates; and just catching the timeout, which treats the symptom. The
lesson I keep is that 'nobody updates this row' isn't the same as 'nothing locks this row'."
