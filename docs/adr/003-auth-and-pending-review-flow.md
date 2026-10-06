# ADR-003: Authentication, Roles and the Pending-Review Flow

**Status:** Accepted (2026-10-01). Vendor: Clerk. Revised 2026-10-05: the execution-time
policy re-check happens once, before the first send (ADR-004 protocol step 6). Revised
2026-10-06: review expiry uses compare-and-set and races safely with approval; escalations
append an `escalated` event.

**Locked 2026-10-06.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

## Context
Two coupled decisions:

1. **Auth:** customer and reviewer roles. Identity must be enforced in FastAPI, because
   ADR-001 requires `user_id` to come from the session, never from model-filled tool
   arguments. The Next.js frontend is a client of the API, not a security boundary.
2. **Pending review:** when a run reaches `AWAIT_APPROVAL`, the customer's SSE stream
   can't stay open for the hours a review might take. The customer needs a clear
   "pending review" state and a later update.

Constraints: one developer, about 4 days for auth, streaming, the chat UI and the
approval queue together (the main schedule risk); a live demo with synthetic data and
demo accounts; free tiers only.

---

## Part 1 — Authentication

### Option A: Managed auth provider (Clerk) with JWT verified in FastAPI — CHOSEN
- How it works: The provider handles sign-in, sessions and demo accounts. The Next.js app
  sends the provider's JWT to FastAPI, which verifies it against the provider's public
  signing keys (JWKS) and reads the role from a custom claim. All role checks happen in
  FastAPI.
- Pros: Hours instead of days. Password storage, resets and session security are handled
  by the provider. Demo accounts are easy to set up. Verifying a JWT in FastAPI is small
  and well understood.
- Cons: A vendor dependency, plus free-tier limits (check current limits before
  committing). Role claims need provider-specific configuration, and local dev or CI need
  a test-token path.
- Cost/latency/complexity profile: $0 at demo scale. Signature checks are local once the
  keys are cached.

### Option B: Auth.js (NextAuth) in Next.js, sharing sessions with FastAPI — rejected
- How it works: Auth.js runs sessions in the Next.js app and issues a JWT that FastAPI
  verifies using a shared secret.
- Pros: Free, self-hosted, idiomatic in Next.js.
- Cons: Auth is owned by the frontend, so FastAPI trusts a token minted by Next.js. You
  have to configure the token format and share secrets across two services. The
  credentials (password) provider is discouraged and leaves password storage to us.
- Why we didn't use it here: It makes the frontend the issuer of the identity the safety
  model relies on, and the cross-service JWT setup eats into a tight schedule.
- When it WOULD be the better choice: Next.js-only apps, or when you need OAuth sign-in
  without a vendor and the backend is in the same Next.js codebase.

### Option C: Hand-rolled JWT auth in FastAPI (or fastapi-users) — rejected
- How it works: FastAPI owns users, password hashing, token issuing and refresh.
- Pros: No vendor, full control, and FastAPI is the single source of identity.
- Cons: We own password hashing, refresh-token rotation, revocation and reset flows,
  where security bugs are easy to make and add nothing to this project's story.
- Why we didn't use it here: Days of security-sensitive work that is off topic for an
  agent-safety project.
- When it WOULD be the better choice: Strict data-residency or on-prem requirements, or
  where a vendor is forbidden.

### Option D: Server-side sessions in FastAPI with Next.js as a backend-for-frontend proxy — rejected
- How it works: An httpOnly session cookie is issued by FastAPI. Next.js server routes
  proxy every API call, and the browser never holds a token.
- Pros: The strongest browser security posture (no tokens in JS), and simple
  revocation.
- Cons: Every request, including SSE streams, goes through the Next.js proxy, which adds
  latency and serverless streaming timeouts. You still build user management yourself.
- Why we didn't use it here: Proxying long-lived SSE through Next.js conflicts with the
  streaming design, and user management is the same cost as C.
- When it WOULD be the better choice: Enterprise apps with strict browser-security
  requirements and no long-lived streams through the proxy.

---

## Part 2 — Pending-review flow

### Option A: End the stream at `AWAIT_APPROVAL`; deliver the outcome through a resumable per-conversation event stream — CHOSEN
- How it works: When a run enters `AWAIT_APPROVAL`, the server emits a `pending_review`
  SSE event and closes the turn stream. Every conversation has an append-only `events`
  table. The client subscribes to `GET /conversations/{id}/events` with `Last-Event-ID`,
  so it reconnects and catches up after closing the tab. When a reviewer acts, one
  transaction updates the run's status with compare-and-set (so two reviewers can't both
  approve) and enqueues execution. Execution **re-runs `POLICY_CHECK` once, before the
  first attempt** (never on crash-resume; see ADR-004 protocol step 6), then calls the
  refund API with the action's idempotency key and appends the outcome message as an
  event. Unreviewed actions expire after a configurable timeout and escalate:
  - **Expiry is a compare-and-set:** `UPDATE runs SET status='ESCALATE', escalated_at=now()
    WHERE id=:run AND status='AWAIT_APPROVAL' AND review_deadline < now()`. If it affects
    0 rows, a reviewer got there first and expiry does nothing.
  - ★ **Approval is the mirror image:** `… WHERE id=:run AND status='AWAIT_APPROVAL' AND
    review_deadline >= now()`. A reviewer can't approve a run whose deadline has passed
    but hasn't been swept by the expiry job yet.
  - Both use the database clock, and whichever wins appends its event (`approved` or
    `escalated`) in the same transaction, so the customer always sees the outcome.
- Pros: No connection held for hours. Survives reloads and server restarts. One
  mechanism for live updates and catch-up. Re-checking policy once, before the first send,
  closes the gap between approval and execution (the order changed while it waited). Each piece is
  testable.
- Cons: An events table and a catch-up protocol to build. Push latency depends on how the
  SSE endpoint learns about new events: polling the table every 1–2 s (simple) or Postgres
  `LISTEN/NOTIFY` (lower latency, more moving parts).
- Cost/latency/complexity profile: Medium. The events table also feeds the trace viewer.

### Option B: Keep the SSE connection open until the review completes — rejected
- How it works: The turn's stream blocks until approve or reject.
- Pros: The simplest client.
- Cons: Reviews take minutes to hours. Proxies, load balancers and hosting platforms drop
  idle long-lived connections. The result is lost if the tab closes.
- Why we didn't use it here: It breaks under normal reviewer delays.
- When it WOULD be the better choice: Sub-minute automated checks, not human review.

### Option C: WebSockets — rejected
- How it works: A two-way socket per client, with server push for review outcomes.
- Pros: Real-time in both directions.
- Cons: Connection state, reconnect logic and auth on upgrade all need building, and some
  hosts support WebSockets less well than SSE. The client doesn't need to send anything
  over the push channel.
- Why we didn't use it here: SSE plus `Last-Event-ID` already gives one-way push with
  built-in resume.
- When it WOULD be the better choice: Interactive features like typing indicators, live
  agent takeover or collaborative reviewer tools.

### Option D: Out-of-band notification only (email) — rejected
- How it works: The chat says "we'll email you"; the outcome arrives by email.
- Pros: Matches how many real support desks work.
- Cons: Needs an email service. The demo can't show the full loop in one screen recording.
- Why we didn't use it here: Out of scope; a later add-on if needed.
- When it WOULD be the better choice: Reviews that take longer than a session, in a real
  product.

---

## Decision
**Part 1, Option A, with Clerk.** Checked 2026-10-01: the free (Hobby) plan allows 50,000
monthly retained users per app and includes custom session-token claims. Organizations
and advanced role management are paid, and we don't need them. Two roles fit the free
tier as follows:
- The role (`customer` | `reviewer`) is stored in the user's Clerk `publicMetadata`.
  Users can't edit `publicMetadata` from the frontend.
- The role is copied into the session token as a custom claim.
- FastAPI verifies every token itself: RS256 signature against the instance's JWKS
  (cached), `exp` / `nbf`, and `azp` against an allowlist of our frontend origins. Then it
  reads `sub` (the user ID) and the role claim. Role checks live only in FastAPI.

**Part 2, Option A:** end the stream, then deliver the outcome through a resumable event
stream, with compare-and-set approval, a policy re-check once before the first send
(never on resume), and review expiry.

## Consequences
- Authorization tests are part of the deterministic suite: a customer can't call reviewer
  endpoints, and a reviewer approval on a run that's already decided **or expired** is rejected.
- **Approval/expiry race test:** approval and expiry run at the same time on one run (with
  controlled timing, as in ADR-005) → exactly one wins, exactly one event is appended,
  and the loser affects 0 rows.
- The `events` table is the shared spine for chat updates, the reviewer console and the
  trace viewer.
- Polling vs. `LISTEN/NOTIFY` for the SSE endpoint is left open: start with polling and
  measure.
- **The browser's `EventSource` API can't send an `Authorization` header.** The frontend
  consumes SSE with `fetch` plus a streamed response body, sending the Clerk token as a
  Bearer header. We don't use a token in the query string, which would leak into logs.
- **Clerk session tokens are short-lived, but a stream can stay open longer.** The token
  is verified when the stream connects. The server closes streams after a maximum lifetime,
  and the client reconnects with a fresh token and `Last-Event-ID`, so a revoked user loses
  access within one lifetime window, not never.
- **`azp` must be checked.** Without it, a valid Clerk token minted for a different origin
  on the same instance would be accepted.
- Local dev and CI don't call Clerk: tests sign tokens with a local test key pair, and the
  verifier takes its JWKS source from configuration.
- Vendor lock-in is limited to sign-in UI and token issuance. Our database stores only the
  Clerk user ID (`sub`), so migrating means remapping IDs, not rewriting authorization.
- **Revisit if:** the vendor's free tier becomes limiting, or we need real-time
  bidirectional features (then Part 2, Option C).

## Interview-ready summary
"I used Clerk for sign-in, but FastAPI verifies every token itself: signature against the
JWKS, expiry and authorized party. All role checks live in the backend, because my agent's
safety model depends on user ID coming from the session, never from the model, and a
frontend is not a security boundary. I rejected rolling my own JWT auth because password
storage and refresh rotation are security work that adds nothing to an agent-safety
project. That would be the right call under data-residency rules that forbid a vendor.
For human review, I don't hold the chat stream open for hours. The stream ends with a
pending-review event, the outcome arrives later through a resumable event stream keyed by
Last-Event-ID, approvals use compare-and-set so two reviewers can't both approve, and
policy is re-checked once, before the first send, because the order may have changed while
it waited. It's never re-checked on resume, since that can only block a refund that already
happened. WebSockets would be the better fit if I needed two-way features like live agent
takeover."
