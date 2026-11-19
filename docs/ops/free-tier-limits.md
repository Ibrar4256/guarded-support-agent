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
| SambaNova | gpt-oss-120b, Meta-Llama-3.3-70B-Instruct, gemma-4-31B-it | 20 | — | 20 | 200K | docs.sambanova.ai/docs/en/models/rate-limits (free tier = no payment method) |
| Cerebras | gpt-oss-120b | 5 | 30K uncached | — | 1M | inference-docs.cerebras.ai/support/rate-limits |
| Cerebras | qwen-3.8-27b | 300 | 150K uncached | — | no cap | same |
| OpenRouter | `:free` models | 20 | — | 50 | — | openrouter.ai/docs/api-reference/limits |

Notes:
- **Cerebras is a trial, not a free tier:** the limits apply "while you use the $5 in Free Trial
  credits". When the credits are spent it needs a payment method, which ADR-025 rules out, so it's
  usable only until then (the model comparison, not the demo).
- **Gemini's "Flash" models allow 20 requests a day**; only the Flash-Lite models (500/day) and
  Gemma 4 (14.4K/day, but 16K tokens/min) have room for a demo or eval volume.
- **Tokens per minute bind before requests do** at our prompt size (estimated 2.5–11K tokens per
  call): Groq's 8K TPM and Gemma's 16K TPM allow only a few calls a minute. Measured tokens decide.
- Gemini rows were read from the console screenshots; a few rows between the captured screens
  may be missing. None of the missing ones are needed so far.

## Plans: zero spend (ADR-025 S5)

| Provider | Free plan confirmed | How | Checked |
|---|---|---|---|
| Gemini | Yes | "Free tier" badge on the project's rate-limit page; billing not set up | 2026-11-17 |
| OpenRouter | Yes | `GET /api/v1/key` reports `is_free_tier: true`, no limit, zero usage | 2026-11-17 |
| Groq | Yes | Console → Settings → Billing shows the free plan, no payment method (confirmed by the developer) | 2026-11-17 |
| Cloudflare | Yes | Workers plan is Workers Free (Workers Paid would bill past 10,000 neurons; confirmed by the developer) | 2026-11-17 |
| SambaNova | Yes | No payment method linked, which is what defines its free tier (confirmed by the developer) | 2026-11-17 |
| Cerebras | Trial credits | Never add a payment method; stop using it when the $5 credit is spent | — |

## Data terms (ADR-025 S11)

| Provider | Terms checked | What they say | Use |
|---|---|---|---|
| Gemini (unpaid) | Yes, 2026-11-17 | Content used "to provide, improve, and develop Google products"; "human reviewers may read, annotate, and process your API input and output"; "Do not submit sensitive, confidential, or personal information to the Unpaid Services." | Synthetic data only |
| Groq | To check | — | Not before checked |
| Cloudflare | To check | — | Not before checked |
| SambaNova | To check | — | Not before checked |
| Cerebras | To check | — | Not before checked |
| OpenRouter `:free` | To check | Served by upstream providers with their own logging and training policies | Not before checked; synthetic data only |

The deployed demo is synthetic-data-only with a visible notice, whatever the outcome of these
checks (ADR-025 S11).
