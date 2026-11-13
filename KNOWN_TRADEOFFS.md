# Known Tradeoffs

Honest, current record of what's deferred and why — not a historical log. Build history
lives in `WEEKLY_LOG.md`.

## Architecture

- **Hand-rolled orchestration instead of a framework (ADR-001).** We own saving, resuming,
  review timeouts and tracing, which LangGraph or Temporal would provide. Revisit at more
  than about 8 states, or if parallel branches or sub-agents are needed.
- **The safety claim is scoped.** Injection can't auto-execute out-of-policy or cross-user
  actions, but it can still trigger **in-policy** actions and attempt **data leaks through
  read tools**. Both are measured (eval categories 7–8), not solved.

## Auth (ADR-003)

- **Vendor dependency on Clerk.** Sign-in UI and token issuance are Clerk's. The database
  stores only the Clerk user ID, so migrating means remapping IDs, not rewriting
  authorization. Organizations and advanced role management are paid plans; we use
  `publicMetadata` plus a custom claim instead, which doesn't scale to fine-grained
  permissions.
- **Revocation lag on open streams.** A token is verified when an SSE stream connects. A
  revoked user keeps an already-open stream until its maximum lifetime expires.

## Refund API (ADR-004)

- **Live demo may run agent and refund-api in one container.** This happens only if the
  host's free tier can't run two services. They keep separate processes, databases and
  credentials, and communicate over HTTP. The crash window can't be shown on the live demo
  because a restart kills both; the compose-based CI crash tests cover it.
- **Mock, not a real provider.** Stripe's real behavior (key pruning after ~24h, webhooks,
  partial failures) isn't exercised until the optional `StripeRefundClient` adapter run.
- **Idempotency keys are pruned only when touched again (ADR-010 Part 5).** A key nobody
  re-sends stays forever, so the table grows without bound in a long-running deployment.
  Harmless at demo volume; a periodic sweep is the fix if real volume appears.
- **The processing deadline starts at receipt, not at send (ADR-010).** Time between the
  agent sending a request and refund-api receiving it isn't counted, so the reconciler's
  quiet-period bound assumes that transit time is bounded by the load balancer's timeout.
  Closing it fully would mean comparing clocks across services.
- **A duplicate refund counts once against capacity (ADR-010 Amends 3).** If the reconciler
  finds two refunds for one action (after a pruned key, or a bug), the action is
  `confirmed` and its amount counts once, although money moved twice. The duplicate-refund
  alert is the control, and a person resolves it; capacity accounting doesn't try to.
- **refund-api's own HTTP tests are in-process (ADR-010).** They run the app through httpx's
  ASGI transport: real Postgres, real locks, but no sockets. They prove the server's logic,
  not how a real hung connection behaves; `timeout_after_commit` is only truly proven by
  the `tests/e2e` crash tests.
- **`action_id` is a flat request field, not `metadata.action_id` (ADR-004 step 2).** ADR-004
  only requires every refund request to carry the action's ID; the `metadata` name was
  Stripe's. Flat keeps `extra="forbid"` and exact formats on every field, with no untyped
  dict to drift into. A future Stripe adapter maps it into Stripe's `metadata`.
- **Token rotation is a manual runbook.** Two accepted tokens make rotation possible
  without downtime, but the order matters (`docs/ops/refund-api-token-rotation.md`); doing
  it wrong locks the agent out and sends refunds to reconciliation.
- **Rolling deploys of refund-api with mixed configs (ADR-013).** The worker reads
  `GET /config` just before each send, but during a rolling deploy two refund-api
  instances could briefly run different retention settings, so the read and the send could
  reach different instances. The demo runs one refund-api instance; with several, the fix is
  for each request to carry the key lifetime it needs (ADR-013 Option C).
- **One extra request per send.** Reading the provider's config before every send (ADR-013)
  costs one small authenticated GET per attempt. Trivial at this scale; a shorter cache
  interval would be cheaper but only shrinks the gap it exists to close.
- **The contract's numeric bounds are post-processed.** FastAPI's spec model rounds integer
  bounds to float (2^63 - 1 becomes 2^63), so refund-api copies the exact bounds back from
  the Pydantic schemas. A test fails if any bound in the contract is a float, so a new model
  can't silently reintroduce the rounding.
- **The fault-injection build must run as one worker (ADR-010 Part 3, ADR-011).** Fault
  plans live in process memory. The app can't detect its worker count, so it doesn't try;
  every fault test asserts its fault fired, which turns a multi-worker mistake into a loud
  failure rather than a silent pass.
- **The Docker image check runs in CI only.** The development machine has too little free
  disk for routine image builds, so `production_image_check.sh` is verified on GitHub's
  runners; run it locally only when there's space.
- **The demo's two databases are consistent only if both seeds run** from
  `infra/seed/demo_manifest.json` (ADR-010, ADR-019); nothing checks one against the other at
  runtime, by design (no shared state between the services).
- **Demo dates are relative and refreshed by re-seeding** (ADR-019): a demo left without a re-run
  for longer than the refund window ages out of policy. A scheduled re-seed is the follow-up if the
  demo has to stay live between deploys.
- **A re-run of the agent's seed overwrites fulfillment state** (status, delivery time, chargeback
  flag) for the manifest's orders that have no unfinished run (those are left alone and reported,
  ADR-019 S5). Nothing else writes that state today; if something does, the seed must stop owning
  it.
- **The key-retention floor is one more deploy step** (ADR-021). It has its own command, run on
  every deployment, so the demo-only seed can be guarded by `DEMO_SEED_ENABLED=true` like the
  agent's. Skipping it stops refund-api at startup, with a message naming the command. A floor
  deleted after startup isn't seen until the next refund: refund-api answers 500, the agent's write
  breaker opens and alerts (and pages if it stays open).
- **refund-api no longer starts with its database down** (ADR-021 S8): the startup floor check
  needs the database, so an unreachable one fails the start (its own message, under a timeout)
  and the orchestrator has to restart it. Before, it started and failed requests until the
  database came back.
- **Demo identities are placeholders** (`user_demo_alice`): once Clerk is set up (ADR-003) the
  manifest's owner ids must be the demo accounts' Clerk user ids.
- **Row locks on immutable tables go through `SECURITY DEFINER` functions.** `FOR UPDATE`
  needs `UPDATE` privilege, which charges deliberately don't grant, so `lock_charge` takes
  the lock on the runtime role's behalf (migration 0003). Each such function is another
  privileged object to review.

## Database (ADR-005)

- **Two databases on one server.** Compose, CI and likely the demo give separate processes
  and credentials, not separate machines. A server outage takes down both.
- **Approved actions can be rejected at execution.** Proposals waiting for review don't
  reserve capacity, so capacity used in the meantime can make the execution-time check
  escalate an approved action. This is chosen deliberately, so pending proposals don't
  block small refunds.
- **Per-customer serialization.** Limit checks for one customer run one at a time. That's
  fine at support-ticket volumes, but a customer with a burst of runs queues on the lock.

## Repo layout (ADR-007)

- **Deliberate duplication between services.** Refund models, money helpers and
  canonical hashing exist in both services. They're kept in sync by the contract test and
  shared test vectors, not by shared code.

## Run lifecycle and schedule (ADR-008)

- **The reconciler never re-sends.** A refund that can't be found after the quiet period
  goes to a human instead of being retried automatically. That means more manual work, in
  exchange for no double-refund path.
- **A crash mid-turn loses that turn.** `AGENT_STEP` isn't resumed by a worker; the
  customer's message is saved, but the model's partial work is gone, and the next message
  starts a fresh turn. Nothing was sent.
- **One turn per conversation.** A second message while a reply is streaming gets 409
  instead of being queued.
- **No send after key retention.** A run resumed after a very long outage always goes to a
  lookup and possibly a human, even when re-sending would have been safe.
- **Schedule slip.** The backend core grew during design review (leases, fencing,
  reconciliation, contract and import checks). The plan went from 14 to 16 days, with
  explicit cut order in the brief.

## Worker (ADR-014)

- **A process freeze can still write late.** Each attempt's budget starts before the
  transaction that records it, and the send gets only what's left, but asyncio enforces
  deadlines at `await` points. A process frozen (GC, VM pause) between the deadline check and
  a socket write can write after the deadline. The margins in the quiet period (ADR-004) and
  in retention > window + recovery delay (ADR-012) cover it; it isn't measured.
- **"Pages" are log lines for now.** The `Alerter` writes `CRITICAL` structured log lines;
  nothing wakes a person until Project 5 or the deploy plugs in a real channel. A wrong
  token is less urgent than when ADR-012 made it a page: the send gate means it now **stops
  sends** (approved refunds wait) instead of escalating every refund.
- **Alerts are rate-limited by kind.** Repeats inside the interval are counted
  (`suppressed=<n>`), not sent, so a second, different problem of the same kind is only
  visible in the count until the next line, and if no later line of that kind comes, the
  count is never reported.
- **One run per worker process.** Throughput scales by adding processes, not concurrency
  inside one; fine at portfolio volume.
- **Polling, not push.** Pickup latency is up to one poll interval (seconds), plus one
  small query per interval per idle worker.
- **A run that may have sent money is never moved by the release streak.** A poison
  `EXECUTING` run pages and keeps cycling at its cooldown until a person looks; only the age
  check or the reconciler may move it.
- **A `FAIL_CLOSED` decision row on every failed pre-send try.** ADR-009 is ambiguous about
  whether only the try that escalates gets a row; logging each one makes the decision log
  longer, but every failure is replayable.
- **Policy settings come in as a parameter for now.** The worker's pre-send takes a
  `PolicyConfig`; reading it from the environment (shared with the chat API) arrives with the
  entry points.
- **A reconciliation alert can precede a failed commit.** The worker raises the alert inside
  the transaction that moves a run to `NEEDS_RECONCILIATION`; if that commit then fails, the
  alert describes a move that didn't happen. The run is still `EXECUTING` and retried, so it's
  a false alarm, never a missed one.
- **Stopping waits for the next step boundary.** A worker told to stop finishes the step it's
  in, including a backoff sleep of up to the backoff cap, before releasing; the shutdown grace
  covers it, but a stop isn't instant.
- **The agent has no Docker image yet.** ADR-007's check that the production image can't
  import `agent_testing` exists for refund-api; the agent's version arrives with its
  Dockerfile at deployment. Until then the boundary rests on the import contract (production
  never imports test code) and on `testing/` sitting outside `src/`.
- **Migration 0002 refuses databases with sent runs.** Their window snapshot can't be known,
  so they must be resolved first; fine before launch, a real migration step after it.

## Reconciler (ADR-016)

- **"Not found" assumes refund-api's lookups are strongly consistent.** A lookup served by a
  lagging replica would make "not found" unreliable, and "not found" frees capacity. Revisit
  before any lookup moves to a replica.
- **A budget raised around a send and lowered again before reconciling** escapes both bounds
  (the recorded one and the fresh read). It needs two config changes inside one run's life; with
  rolling deploys of refund-api, requests should carry their own bound (ADR-013 Option C).
- **Transit time beyond the margin is an assumption** (as in ADR-010): the quiet period's margin
  covers the gap between refund-api's deadline check and its commit, and network transit.
- **A run that never resolves cycles and pages.** Only a lookup or a person can settle an
  `unknown` outcome; the run keeps its status, cools down between tries, and pages once per
  interval.
- **A refund-api bug returning mismatched rows for every action pages per run** (rate-limited),
  because a mismatch looks run-specific.
- **The "failed before" mark for repeated provider-type failures lives in memory.** A worker
  restart costs one extra cycle before such a run counts toward its streak. Marks for runs that
  another process resolves are never cleared; the dict grows by one entry per such run until a
  restart (small, but unbounded in principle).
- **A run whose lookups fail alone never pages through its streak** (ADR-017): with no other
  run's lookup answered in between, its refund-api-type failures don't count. Its route to a
  person is the stale-unknown page (ADR-017 S3), so `RECONCILER_STALE_PAGE_SECONDS` is its paging
  delay. It is scanned at the start of each pass, so a run that stops being held by the kill
  switch pages from the next pass, one poll interval later.
- **Which runs the kill switch held is kept in memory** (ADR-017 S4). A restart forgets it, so
  held runs page one by one until their next pass: noise in the safe direction.
- **A degraded (not down) refund-api pages per failing run** (ADR-017): when some lookups fail and
  others are answered, the evidence counter moves, so each failing run builds a streak and pages,
  although the cause is one dependency. Bounded by the streak limit and per-run rate limiting.
- **An idle reconciler reads `GET /config` once per poll interval** (a fresh read each pass,
  ADR-016 Part 2), and scans for stale runs. Both are cheap now; neither has a dedicated index for
  `NEEDS_RECONCILIATION` runs (the partial index covers APPROVED/EXECUTING only). Add one if
  uncertain runs ever number more than a handful.
- **The kill switch is read, not locked, in the deciding transaction.** The runtime role can't
  `SELECT … FOR SHARE` the controls row (it needs UPDATE), so a switch flipped in the same
  instant as a reject can lose the race by one transaction.
- **e2e `slow_commit`'s negative control is a lookup, not a planted reconciler** (as ADR-015's
  stale-worker test): at the moment the run needs reconciliation the test asserts refund-api holds
  no refund, which is what a reconciler without a quiet period would act on. The "not found →
  rejected" mapping and the quiet-period plants are proven in-process; a manual plant that drops
  the quiet condition makes the e2e test end `rejected`. The e2e profile's refund-api budget
  (5 s) is longer than the agent's three attempts so a commit can land after they end.

## Provider breakers (ADR-018)

- **Per-process learning.** Each worker process has its own breakers; with several workers each
  spends a threshold's worth of failures before it pauses. Shared state (ADR-018 Option C) is the
  fix once more than one worker runs.
- **A failed write probe can cost one run its send window.** A probe on a fresh approval starts
  its window; the probe order (a doomed run first, then an approval, then fewest attempts) bounds
  the loss at one run per failed probe, logarithmic in the outage's length up to
  `PROVIDER_BREAKER_MAX_OPEN_SECONDS`. That maximum trades recovery speed against this cost; keep
  it below refund-api's retry window minus the client timeout.
- **A probe is a real, counted request**, and may commit.
- **S1 protects only runs with a full budget**: a run resumed with most attempts used can reach
  reconciliation before the write breaker opens.
- **An unrecognized status still reconciles up to a threshold's worth of runs** (a 404 for
  everything): ADR-012 keeps classifying it as `RECONCILE`, and the breaker opens only after the
  threshold.
- **A degraded refund-api can pause every refund on a path**, including those that would have gone
  through.
- **An outage longer than a run's window** still ends in reconciliation and escalation for runs
  already `EXECUTING`.
- **A fresh worker's first send is a probe** (breakers start half-open): one blip there pauses
  sending for the claim cooldown. Two e2e tests warm the worker with one refund first, because
  the paths they test (a timeout retried; attempts spent, then reconciliation) are a running,
  closed-breaker worker's.
- **`REFUND_CLIENT_MAX_ATTEMPTS` must now be at least 2** (the threshold must be at least 1 and
  below it).
- **The send-loop and reconciler integration tests run with breakers that never open**
  (`worker_harness.never_opening`), so they test those loops' own rules; the breaker interactions
  are tested with production-built breakers in `test_provider_breakers.py`.
- **refund-api's `fail_all` lookup fault is a test-build middleware**, installed innermost (after
  auth) through Starlette's `user_middleware` list, because production has no hook in the lookup
  path and gets none.

## End-to-end crash tests (ADR-015)

- **Processes, not containers.** The crash tests run both services from their own venvs, so
  they don't exercise the images; the image checks are separate (refund-api's in CI, the
  agent's when its Dockerfile lands).
- **They use the Postgres superuser** to create a database per test. Test-only, and it's what
  makes "a worker can only see its own test's runs" true by construction.
- **`timeout_after_commit` has no duplicate-producing control of its own.** refund-api holds
  one fault plan per action, so it can't also be `dedup_disabled`; its control is that the
  timeout fired and two sends went out for one refund, and the after-response test carries the
  `dedup_disabled` control.
- **Linux only.** The stall check reads `/proc`, like CI and the development machine.

## Refund policy (ADR-009)

- **Single currency in v1.** Other currencies are refused (R15); capacity sums assume one
  currency.
- **R13 (breaker) and R19 (global review-queue cap) are approximate** across customers;
  overshoot is bounded by concurrent proposals × the auto-approve maximum.
- **"Item never arrived" always reaches a human** (R08/R09 need a delivered order).
- **Self-approval is only caught for the same account;** a reviewer with a separate
  customer account isn't detected.
- **No customer cancellation, and reviewer-originated refunds happen outside the system**
  in v1.
- **The kill switch doesn't stop `EXECUTING` retries;** a chargeback opened after sending
  can't be stopped.
- **More reviewer load** by design (R11, R12, R18, a tripped breaker).
- **R02 forces a fresh order lookup in every run** (one extra tool call).

## Local database (step 2)

- **Postgres 15 locally**, reusing an image already on disk (disk is nearly full). Neon
  supports 15–17; upgrading later is a config change, verified by the migration tests.
- **Dev credentials** are generated into a gitignored `.env`; `.env.example` documents the
  variables.

## Code reuse

- **Project 1's LLM layer is extracted and ported, not shared (corrected by ADR-005).**
  Project 1's layer is sync (`openai.OpenAI`) and mixed in with research-specific code.
  Only the provider, retry and telemetry pieces are extracted and ported to `AsyncOpenAI`;
  they aren't shared as a package. The cost: the two versions now differ (sync vs.
  async), fixes must be ported by hand, and they will drift. Revisit at Project 5, where a shared tracing SDK is the natural
  place to consolidate.

## Evaluation

- **Full LLM evals don't gate every PR.** Only deterministic suites plus a small LLM smoke
  set run per PR; the full adversarial suite runs nightly or on a label. A regression can
  be merged and caught up to a day later.
- **Small adversarial sample.** 20–30 cases × 3 runs; results are directional, not
  statistically strong.
- **Mock backends.** The order DB and refund API are mocks. The refund mock implements
  idempotency deduplication, but real payment-provider failure modes (partial failures,
  webhooks) are not modeled.
- **Free-tier models.** Results reflect open models on Groq/Gemini free tiers; tool-calling
  reliability differs from frontier models.
