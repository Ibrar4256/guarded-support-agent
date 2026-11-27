"""Record and replay model responses for evals (ADR-025 S6, ADR-026 S2).

``EvalModel`` wraps a case's model. Every call gets a key: the SHA-256 of the canonical JSON of
the provider, model, the purpose's prompt fingerprint, the messages, the tool schemas, the
temperature, the output cap and the run index (1..k, so each of k runs keeps its own sample).
Nothing is normalized out of the messages: a changed prompt must miss.

- ``replay``: the recording for the key, or ``MissingRecording`` naming it; the wrapped model is
  never called. A recording made with another anchor is refused (``AnchorMismatch``): the anchor
  reaches the key only through the dates in messages, so nothing else would notice.
- ``record``: calls the model and writes one file per key.
- ``live``: calls the model and stores nothing (the drift check).
- ``record-missing`` (ADR-029 S4): replays every key it has; calls live and records only a
  missing key of a purpose it was told to record (never ``turn``), so a changed turn prompt
  fails as in replay instead of being recorded silently.

Each purpose may have its own ``Route`` (provider, model, the model to call): the warrant check
can run on another model than the turn's, and its keys name that model. Live calls are tallied
per provider (tokens, and calls the provider didn't fully count): a replayed call writes an
``llm_calls`` row too, but costs nothing, so the budget charges this tally.

A case's ``script`` (ADR-028 S7) answers its first turn calls in every mode: a fixed response,
for example a malformed one, that no model wrote, so it has no key and is never recorded; the
model's own calls start after it, with the scripted response in their messages.

In every mode an outgoing message containing the real current date fails the call
(``RealDateLeak``): every date a prompt shows must come from the eval clock (ADR-026 S2). A
call that raises is never recorded: a rate limit or an outage isn't the model's answer.

Every recording error raised is also kept in ``errors``: a best-effort caller (the background
summary) catches and logs it, and the run must still fail on it.
"""

import dataclasses
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent.llm.types import (
    ChatModel,
    FinishReason,
    Message,
    ModelResponse,
    OnText,
    Purpose,
    ToolCall,
    ToolSchema,
    Usage,
    UsageCompleteness,
)
from agent_testing.evals.clock import ANCHOR


class Mode(StrEnum):
    REPLAY = "replay"
    RECORD = "record"
    LIVE = "live"
    RECORD_MISSING = "record-missing"


class RecordingError(Exception):
    pass


class MissingRecording(RecordingError):
    def __init__(self, key: str) -> None:
        super().__init__(f"no recording for key {key}; record the case first")
        self.key = key


class AnchorMismatch(RecordingError):
    pass


class RealDateLeak(RecordingError):
    pass


class EvalRefused(RecordingError):
    pass


def refuse_production(environ: Mapping[str, str]) -> None:
    """Evals never run in production, and the service never reads recordings (ADR-025 S6)."""
    if environ.get("APP_ENV") == "production":
        raise EvalRefused("evals and recordings are refused with APP_ENV=production")


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def recording_key(
    *,
    provider: str,
    model: str,
    fingerprint: str,
    messages: Sequence[Message],
    tools: Sequence[ToolSchema],
    temperature: float,
    max_output_tokens: int,
    run_index: int,
) -> str:
    inputs = {
        "provider": provider,
        "model": model,
        "fingerprint": fingerprint,
        "messages": list(messages),
        "tools": list(tools),
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
        "run_index": run_index,
    }
    return hashlib.sha256(canonical(inputs).encode("utf-8")).hexdigest()


def response_to_json(response: ModelResponse) -> dict[str, Any]:
    return dataclasses.asdict(response)


def response_from_json(data: Mapping[str, Any]) -> ModelResponse:
    usage = data["usage"]
    return ModelResponse(
        text=data["text"],
        tool_calls=tuple(ToolCall(**call) for call in data["tool_calls"]),
        finish_reason=FinishReason(data["finish_reason"]),
        usage=None if usage is None else Usage(**usage),
        completeness=UsageCompleteness(data["completeness"]),
        provider=data["provider"],
        model=data["model"],
        attempts=data["attempts"],
        prompt_chars=data["prompt_chars"],
        output_chars=data["output_chars"],
        first_token_seconds=data["first_token_seconds"],
        latency_seconds=data["latency_seconds"],
    )


@dataclass(frozen=True)
class Recording:
    key: str
    anchor: str
    provider: str
    model: str
    purpose: str
    fingerprint: str
    run_index: int
    case_id: str
    recording_run: str
    recorded_on: str
    response: dict[str, Any]


class RecordingStore:
    """One JSON file per key under ``root`` (ADR-026 Part 3)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> Recording | None:
        path = self.path(key)
        if not path.is_file():
            return None
        return Recording(**json.loads(path.read_text(encoding="utf-8")))

    def put(self, recording: Recording) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        text = json.dumps(dataclasses.asdict(recording), indent=2, ensure_ascii=False)
        self.path(recording.key).write_text(text + "\n", encoding="utf-8")


def real_dates() -> set[str]:
    """Today's date in the forms a prompt renders it (ISO), in UTC and local time."""
    return {datetime.now(UTC).date().isoformat(), date.today().isoformat()}


@dataclass(frozen=True)
class Route:
    """What answers one purpose: the provider and model its keys name, and the model to call."""

    provider: str
    model: str
    inner: ChatModel


@dataclass(frozen=True)
class CaseRun:
    """What identifies one run of one case in a recording."""

    case_id: str
    run_index: int
    recording_run: str


class EvalModel:
    def __init__(
        self,
        inner: ChatModel,
        *,
        mode: Mode,
        store: RecordingStore,
        provider: str,
        model: str,
        fingerprints: Mapping[Purpose, str],
        temperature: float,
        max_output_tokens: int,
        run: CaseRun,
        anchor: datetime = ANCHOR,
        today: Callable[[], set[str]] = real_dates,
        script: Sequence[ModelResponse] = (),
        routes: Mapping[Purpose, Route] | None = None,
        record_purposes: frozenset[Purpose] = frozenset(),
    ) -> None:
        if Purpose.TURN in record_purposes:
            raise ValueError("record-missing never records a turn call: a changed prompt must miss")
        self._default = Route(provider, model, inner)
        self._routes = dict(routes or {})
        self._record_purposes = record_purposes
        self._mode = mode
        self._store = store
        self._fingerprints = fingerprints
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._run = run
        self._anchor = anchor.isoformat()
        self._today = today
        self._script = list(script)
        self.scripted = 0  # scripted responses used so far
        self.keys: list[str] = []  # every key this run asked for, in order
        self.spent: dict[str, int] = {}  # provider -> tokens of live calls
        self.uncounted = 0  # live calls whose usage the provider didn't fully report
        self.recorded: dict[str, int] = {}  # purpose -> keys recorded
        self.replayed: dict[str, int] = {}  # purpose -> keys answered from recordings
        self.errors: list[RecordingError] = []  # every one raised, even if a caller caught it

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        *,
        deadline: float,
        purpose: Purpose,
        on_text: OnText | None = None,
    ) -> ModelResponse:
        try:
            return await self._complete(messages, tools, deadline, purpose, on_text)
        except RecordingError as error:
            self.errors.append(error)
            raise

    async def _complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        deadline: float,
        purpose: Purpose,
        on_text: OnText | None,
    ) -> ModelResponse:
        self._refuse_real_dates(messages)
        if purpose is Purpose.TURN and self.scripted < len(self._script):
            self.scripted += 1
            return self._script[self.scripted - 1]
        route = self._routes.get(purpose, self._default)
        fingerprint = self._fingerprints[purpose]
        key = recording_key(
            provider=route.provider,
            model=route.model,
            fingerprint=fingerprint,
            messages=messages,
            tools=tools,
            temperature=self._temperature,
            max_output_tokens=self._max_output_tokens,
            run_index=self._run.run_index,
        )
        self.keys.append(key)
        if self._mode is Mode.REPLAY or (
            self._mode is Mode.RECORD_MISSING
            and (purpose not in self._record_purposes or self._store.get(key) is not None)
        ):
            return self._replay(key, purpose)
        response = await route.inner.complete(
            messages, tools, deadline=deadline, purpose=purpose, on_text=on_text
        )
        self._charge(route.provider, response)
        if self._mode in (Mode.RECORD, Mode.RECORD_MISSING):
            _count(self.recorded, purpose)
            self._store.put(
                Recording(
                    key=key,
                    anchor=self._anchor,
                    provider=route.provider,
                    model=route.model,
                    purpose=purpose.value,
                    fingerprint=fingerprint,
                    run_index=self._run.run_index,
                    case_id=self._run.case_id,
                    recording_run=self._run.recording_run,
                    recorded_on=datetime.now(UTC).date().isoformat(),
                    response=response_to_json(response),
                )
            )
        return response

    def _charge(self, provider: str, response: ModelResponse) -> None:
        usage = response.usage
        spent = 0 if usage is None else usage.input_tokens + usage.output_tokens
        self.spent[provider] = self.spent.get(provider, 0) + spent
        if response.completeness is not UsageCompleteness.FULL:
            self.uncounted += 1

    def _replay(self, key: str, purpose: Purpose) -> ModelResponse:
        recording = self._store.get(key)
        if recording is None:
            raise MissingRecording(key)
        if recording.anchor != self._anchor:
            raise AnchorMismatch(
                f"recording {key} was made with anchor {recording.anchor}, "
                f"this run uses {self._anchor}: re-record the case"
            )
        _count(self.replayed, purpose)
        return response_from_json(recording.response)

    def _refuse_real_dates(self, messages: Sequence[Message]) -> None:
        text = canonical(list(messages))
        leaked = sorted(day for day in self._today() if day in text)
        if leaked:
            raise RealDateLeak(
                f"an eval prompt contains the real date {', '.join(leaked)}: every date a "
                "prompt shows must come from the eval clock (ADR-026 S2)"
            )


def _count(counts: dict[str, int], purpose: Purpose) -> None:
    counts[purpose.value] = counts.get(purpose.value, 0) + 1
