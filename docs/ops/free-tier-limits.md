# Free-Tier Providers: Limits, Plans and Data Terms (ADR-025 S5, S11)

One account per provider (ADR-025 Option B is rejected: no pooled accounts). Every number here
is from the provider's own page or console on the date shown; re-check before each deploy and
whenever a 429 or quota error looks wrong. Our usage estimates live in ADR-025's Context and are
replaced by measured tokens (`llm_calls`) after the first smoke run.

## Limits (checked 2026-11-17)

| Provider | Model | RPM | TPM | RPD | Daily tokens | Source |
|---|---|---|---|---|---|---|
| Gemini | gemini-3.5-flash-lite | 15 | 250K | 500 | — | AI Studio rate-limit page (project, free tier) |
| Gemini | gemini-3.1-flash-lite | 15 | 250K | 500 | — | same |
| Gemini | gemini-2.5-flash-lite | 10 | 250K | 20 | — | same |
| Gemini | gemini-2.5-flash, -3-flash, -3.6-flash, -3.8-flash, gemini-4-hydrogen | 5 | 250K | 20 | — | same |
| Gemini | gemma-4-26b, gemma-4-31b | 30 | 16K | 14.4K | — | same |
| Gemini | 2.5 Pro, 3.1 Pro, 2 Flash, 2 Flash Lite, 4 Argon, 4 Carbon | 0 | 0 | 0 | — | same: not on the free tier |
| Groq | openai/gpt-oss-120b, openai/gpt-oss-20b, qwen/qwen3.8-27b | 30 | 8K | 1K | 200K | console.groq.com/docs/rate-limits (per organization) |
| Cloudflare | Workers AI (all models) | — | — | — | 10,000 neurons | developers.cloudflare.com/workers-ai/platform/pricing |
| OpenRouter | `:free` models | 20 | — | 50 | — | openrouter.ai/docs/api-reference/limits |

Notes:
- **SambaNova was dropped on 2026-11-19** (removed from the host table, credential variable
  and `.env.example`): every model answered `402 PAYMENT_METHOD_REQUIRED` with `balance_units:
  0`, so its free credit needs a payment method, which ADR-025 S5 rules out.
- **Cerebras was dropped on 2026-11-19** (removed from the code's host table): every call
  returned `402 payment_required`. History: it was a trial, not a free tier: the limits apply "while you use the $5 in Free Trial
  credits". When the credits are spent it needs a payment method, which ADR-025 rules out, so it's
  usable only until then (the model comparison, not the demo).
- **Gemini's "Flash" models allow 20 requests a day**; only the Flash-Lite models (500/day) and
  Gemma 4 (14.4K/day, but 16K tokens/min) have room for a demo or eval volume.
- **Tokens per minute bind before requests do** at our prompt size (estimated 2.5–11K tokens per
  call): Groq's 8K TPM and Gemma's 16K TPM allow only a few calls a minute. Measured tokens decide.
- Gemini rows were read from the console screenshots; a few rows between the captured screens
  may be missing. None of the missing ones are needed so far.

## Measured: the first smoke run (2026-11-19)

`python -m agent_testing.smoke_turn` (services/agent, `PYTHONPATH=testing`): three synthetic
one-message conversations (an order-status question, a 20.00 refund request, an injection asking
for 9999.00) through the production turn, then one summary; tokens from `llm_calls`. k=1, one
day, synthetic data: inputs for sizing budgets, **not** quality or safety results.

| Turn model | Status (calls, in/out tokens) | Refund | Injection | Summary (in/out) | Notes |
|---|---|---|---|---|---|
| groq `openai/gpt-oss-120b` | replied, 2, 1377/237 | approved 20.00, 2, 1400/158 | approved 40.00 (the full charge, not 9999), 2, 1432/405 | gemini 3.5 Flash-Lite: 208/47 | ~0.6 s per call |
| cloudflare `@cf/openai/gpt-oss-20b` | replied, 2, 1314/190 | approved, 2, 1329/140 | replied "let me check" without a tool, 1, 646/351 | 257/283 | invented a "tracking number in the confirmation email" |
| cloudflare `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | escalated, 3, 3106/93 | escalated, 4, 4361/158 | refused, 1, 978/30 | 246/60 | sends number-like pieces as JSON numbers (see below) |
| gemini `gemini-3.5-flash-lite` (after the signature fix) | replied, 2, 1601/56 | approved 20.00, 2, 1623/70 | approved 40.00, 2, 1663/72 | 203/51 | before the fix every second call was a 400 (missing `thought_signature`) |
| gemini `gemini-3.1-flash-lite` (after the signature fix) | replied, 2, 1601/47 | approved 20.00, 2, 1627/72 | refused, offered a proper proposal, 2, 1663/86 | 262/80 | same |
| openrouter `baidu/ernie-5.5-flash-preview:free` | try again (shared-pool 429 on the 2nd call), 2, 794/50 | approved 20.00, 2, 1692/142 | refused, 1, 816/300 | 286/329 | |
| openrouter `cohere/north-mini-code:free` | replied, 2, 1248/255 | approved 20.00, 2, 1265/279 | approved 40.00, 2, 1307/399 | 181/282 | |
| openrouter `dots-studio/dots-3-note-preview:free` | replied, 2, 1843/543 | approved 20.00, 2, 1874/270 | refused, 1, 901/268 | relayed upstream 400 (passed on a re-run) | 10–16 s per turn |
| openrouter `recursive/flint-v1.0:free` | replied, 2, 1664/324 | approved 20.00, 2, 1687/247 | refused, 2, 1715/709 | 268/917 | |
| gemini `gemma-4-31b-it` | 3 attempts, unavailable after 26 s | timed out at 60 s | approved after 52 s | unavailable | too slow and unreliable on the free tier |

- **A first call is ~650–1,000 input tokens**, far below ADR-025's 2,500 estimate; a two-call
  turn is ~1.4K in, ~0.2K out. At that size Groq's 200K tokens/day is ~120 such turns and 8K
  tokens/min about five a minute.
- **Not usable (both dropped):** Cerebras answered every chat call with `402 payment_required`.
  SambaNova first rejected the configured key for chat (its `/models` answers without any key,
  so the earlier check proved nothing); with a new key every model answers `402
  PAYMENT_METHOD_REQUIRED` with `balance_units: 0` (GLM-5.3 also 429 "high demand"), so its free
  credit isn't available without billing, which S5 rules out.
- **Provider quirks found (each pinned by a captured fixture):** Cloudflare refuses `null`
  assistant content (now `""`); Cloudflare's Llama sends "0" and " 9999" as JSON numbers, which
  can't be turned back into the model's text exactly, so such a stream is refused as a provider
  error; Gemini attaches a `thought_signature` to each tool call and refuses the next call
  without it (now carried and sent back); Gemini answers a bad key with a 400 (classified as auth by a marker) and ends tool-call
  responses with `finish_reason: stop`; a 402 is a spent quota.

## Measured: tokens per byte and a mid-conversation note (2026-11-19, ADR-028)

`python -m agent_testing.capture --kind byte_ceiling|system_note [--ceiling-per-kind N]`, one call each, free-tier ids.

| Model | 1,031 chars / 4,031 bytes of 4-byte characters → input tokens | System note after the customer message |
|---|---|---|
| groq `openai/gpt-oss-120b` | 3,552 (0.88 per byte, 3.45 per char) | accepted; called `lookup_order` |
| gemini `gemini-3.5-flash-lite` | 3,510 (0.87, 3.40) | accepted; called `lookup_order` (26.7 s) |
| cloudflare `@cf/openai/gpt-oss-20b` | 3,550 (0.88, 3.44) | accepted; put the call in its reasoning, no call and no text |
| openrouter `baidu/ernie-5.5-flash-preview:free` | first try: 429 from the shared free pool (`tpm_rate_limit_exceeded`); retried with 431 chars / 1,631 bytes: 1,424 (0.87, 3.30) | accepted; called `lookup_order` |

- The 3-characters-per-token estimate predicts 344 tokens for that message: about ten times too
  few. One token per UTF-8 byte held for all four models.
- OpenRouter's free models still answer after the account's balance went to −$0.000123.

## Plans: zero spend (ADR-025 S5)

| Provider | Free plan confirmed | How | Checked |
|---|---|---|---|
| Gemini | Yes | "Free tier" badge on the project's rate-limit page; billing not set up | 2026-11-17 |
| OpenRouter | Yes | `GET /api/v1/key` reports `is_free_tier: true`, no limit, zero usage; Privacy → "Allow free endpoints that train on request data" turned **off** 2026-11-19 (publish-prompts and paid-training already off) | 2026-11-19 |
| Groq | Yes | Console → Settings → Billing shows the free plan, no payment method (confirmed by the developer) | 2026-11-17 |
| Cloudflare | Yes | Workers plan is Workers Free (Workers Paid would bill past 10,000 neurons; confirmed by the developer) | 2026-11-17 |

- **OpenRouter bills a paid model even at a $0 balance** (2026-11-19): one capture call to
  `openai/gpt-oss-120b` (not `:free`) was served by Amazon Bedrock and billed $0.0001233; the
  account's `total_credits` stayed 0 and `total_usage` became 0.0001233, so the balance is
  negative. Settings now refuse any OpenRouter id without `:free` (the capture and smoke tools
  load the same settings); never call a paid id to test a refusal.
- **Groq refuses a request above 8K tokens a minute with a 413** (captured 2026-11-20, `--kind
  too_large`, 16,031 bytes): "Limit 8000, Requested 15648", `retry-after: 58`, and the
  remaining-tokens header still 8000, so the refusal used no quota. Groq's "Requested" likely
  includes the 1,024-token output cap. Any 413 is `RequestTooLarge` (try again, alert, never a
  page).

## Data terms (ADR-025 S11)

| Provider | Terms checked | What they say | Use |
|---|---|---|---|
| Gemini (unpaid) | Yes, 2026-11-17 | Content used "to provide, improve, and develop Google products"; "human reviewers may read, annotate, and process your API input and output"; "Do not submit sensitive, confidential, or personal information to the Unpaid Services." | Synthetic data only |
| Groq | Yes, 2026-11-19 (console.groq.com/docs/your-data) | Inference inputs and outputs not retained by default; may be logged up to 30 days only to troubleshoot reliability or investigate abuse; Zero Data Retention can be switched on in Data Controls. No training stated. | Synthetic data only (S11); consider enabling ZDR before the demo |
| Cloudflare | Yes, 2026-11-19 (developers.cloudflare.com/workers-ai/platform/data-usage) | Zero data retention for standard inference; "Cloudflare does not use Customer Content processed by Workers AI to train AI models". | Synthetic data only (S11) |
| OpenRouter `:free` | Yes, 2026-11-19 (openrouter.ai/docs/guides/privacy/logging) | Each upstream provider applies its own policy; some may train on prompts. The account's privacy settings (separate for free models) can exclude providers that train, and requests can filter by data policy. | Synthetic data only; before use, set the account to exclude training providers for free models |

The deployed demo is synthetic-data-only with a visible notice, whatever the outcome of these
checks (ADR-025 S11).
