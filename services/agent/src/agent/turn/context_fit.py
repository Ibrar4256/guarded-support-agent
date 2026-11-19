"""Each purpose's largest prompt plus output, in tokens (ADR-025 S12).

Characters convert at a conservative 3 per token until tokens are measured; parts the provider
caps in tokens (every model output, including the stored summary) are counted in tokens.

- **Turn:** the system prompt with the largest digest and the summary's fence, the summary,
  the window budget, the tool schemas, and the turn's earlier steps: each of the call budget's
  earlier calls adds the model's own response and the largest read-tool result. Plus the output.
- **Summary:** its prompt, the previous summary, the messages to add, plus the output. The
  messages to add are bounded nominally, as when summaries keep up: the trigger count plus one
  (two messages arrive between turn starts), each the larger of a customer message and a reply.

Not bounded here (KNOWN_TRADEOFFS): several read calls in one response (counted as one per
step), corrections echoing the model's own tool names, and the summary's input growing while
summaries keep failing.

Largest values are computed by running the real formatting code on the largest inputs it can
get: ids are at most ``MAX_ID_CHARS`` (the id pattern every looked-up order passed), amounts at
most a BIGINT, every currency in the table plus one outside it.
"""

import json
import math
import uuid
from dataclasses import dataclass, field

from agent.core.currency import MINOR_UNIT_EXPONENTS
from agent.core.lifecycle import RefundOutcome
from agent.core.policy import FulfillmentStatus
from agent.db.memory import PastRun
from agent.db.order_lookup import OwnedOrder
from agent.llm.settings import LLMSettings
from agent.llm.types import Purpose
from agent.tools.order_lookup import NOT_FOUND, UNAVAILABLE, found_result
from agent.turn.fingerprint import FIXED_NONCE, SAMPLE_TIME
from agent.turn.loop import turn_tools
from agent.turn.memory import OUTCOME_LABELS, SummaryJob, digest_line, summary_messages
from agent.turn.prompt import MessageEvent, build_messages
from agent.turn.settings import TurnSettings

CHARS_PER_TOKEN = 3
MAX_ID_CHARS = 128
MAX_AMOUNT = 2**63 - 1
UNLISTED_CURRENCY = "ZZZ"
# "assistant: " and the newline that joins the summary's transcript lines.
TRANSCRIPT_LINE_CHARS = len("assistant: \n")


def tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class PromptBound:
    purpose: Purpose
    parts: dict[str, int] = field(default_factory=dict)  # tokens per part

    @property
    def total(self) -> int:
        return sum(self.parts.values())

    def describe(self) -> str:
        return ", ".join(f"{name} {count}" for name, count in self.parts.items())


def bound(purpose: Purpose, turn: TurnSettings, llm: LLMSettings) -> PromptBound:
    return turn_bound(turn, llm) if purpose is Purpose.TURN else summary_bound(turn, llm)


def turn_bound(turn: TurnSettings, llm: LLMSettings) -> PromptBound:
    largest_digest = "\n".join([largest_digest_line()] * turn.digest_tickets)
    system = system_chars(largest_digest, None)
    summary_frame = system_chars(largest_digest, "") - system
    earlier_steps = turn.max_model_calls - 1
    return PromptBound(
        Purpose.TURN,
        {
            "system prompt and digest": tokens(system + summary_frame),
            "summary": llm.max_output_tokens,
            "window": tokens(turn.window_max_chars),
            "tool schemas": tokens(len(canonical_json(turn_tools()))),
            "earlier responses": earlier_steps * llm.max_output_tokens,
            "tool results": earlier_steps * tokens(largest_tool_result()),
            "output": llm.max_output_tokens,
        },
    )


def summary_bound(turn: TurnSettings, llm: LLMSettings) -> PromptBound:
    frame = summary_chars(previous=None)
    previous_frame = summary_chars(previous="") - frame
    per_message = max(tokens(turn.message_max_chars), llm.max_output_tokens)
    messages = turn.summary_trigger_messages + 1
    return PromptBound(
        Purpose.SUMMARY,
        {
            "summary prompt": tokens(frame + previous_frame),
            "previous summary": llm.max_output_tokens,
            "messages to add": messages * (per_message + tokens(TRANSCRIPT_LINE_CHARS)),
            "output": llm.max_output_tokens,
        },
    )


def system_chars(digest: str, summary: str | None) -> int:
    """The system message's length; an empty ``summary`` still counts its label and fence."""
    messages = build_messages(
        [], nonce=FIXED_NONCE, digest=digest, summary=None if summary is None else summary or " "
    )
    return len(messages[0]["content"]) - (0 if summary is None else 1)


def summary_chars(*, previous: str | None) -> int:
    job = SummaryJob(
        conversation_id=uuid.UUID(int=0),
        run_id=uuid.UUID(int=0),
        turn_id=uuid.UUID(int=0),
        previous=None if previous is None else previous or " ",
        events=(MessageEvent("customer_message", {"text": ""}, 1),),
        through=1,
    )
    chars = sum(len(m["content"]) for m in summary_messages(job, FIXED_NONCE))
    return chars - (0 if previous is None else 1)


def largest_digest_line() -> str:
    candidates = [
        digest_line(PastRun(uuid.UUID(int=0), SAMPLE_TIME, status, outcome, order, amount, cur))
        for status in OUTCOME_LABELS
        for outcome in RefundOutcome
        for cur in (*MINOR_UNIT_EXPONENTS, UNLISTED_CURRENCY)
        for order, amount in (("x" * MAX_ID_CHARS, MAX_AMOUNT), (None, None))
    ]
    return max(candidates, key=len)


def largest_tool_result() -> int:
    """The longest text a read tool can return (lookup_order is the only one)."""
    found = (
        found_result(OwnedOrder("x" * MAX_ID_CHARS, status, MAX_AMOUNT, currency, SAMPLE_TIME))
        for status in FulfillmentStatus
        for currency in MINOR_UNIT_EXPONENTS
    )
    return max(len(text) for text in (NOT_FOUND, UNAVAILABLE, *found))


def canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))
