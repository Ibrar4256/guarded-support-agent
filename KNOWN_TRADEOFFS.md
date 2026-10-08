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
