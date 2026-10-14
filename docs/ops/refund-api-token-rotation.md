# refund-api service token rotation

refund-api accepts two tokens at once, `REFUND_API_TOKEN_CURRENT` and
`REFUND_API_TOKEN_NEXT` (ADR-010 Part 6). The agent sends one, `REFUND_API_TOKEN`. Rotating
in the wrong order locks the agent out, and every refund then lands in reconciliation
(a 401 keeps the outcome `unknown`).

## Order

1. **Generate** a new token (at least 32 characters):
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
2. **Set `REFUND_API_TOKEN_NEXT`** on refund-api to the new token and restart it. It now
   accepts both the old and the new token.
3. **Switch the agent**: set `REFUND_API_TOKEN` to the new token and restart the agent.
4. **Promote**: on refund-api, set `REFUND_API_TOKEN_CURRENT` to the new token.
5. **Clear `REFUND_API_TOKEN_NEXT`** and restart refund-api. The old token stops working.

## Checks

- Before step 3: a request with the new token gets a non-401 response from refund-api.
- After step 5: a request with the old token gets 401.
- refund-api refuses to start if `NEXT` equals `CURRENT` or either is shorter than 32
  characters.

## Never

- Never put a token in a URL, a log line or a commit.
- Never clear `NEXT` before the agent has switched (step 3): that is the lockout.
