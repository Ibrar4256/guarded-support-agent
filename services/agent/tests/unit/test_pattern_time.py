"""Every C01 and C02 pattern finishes in linear time on worst-case inputs at the message cap
(ADR-029 S1). Both run inside the turn's start transaction, so a pattern that backtracks
catastrophically would hold that transaction: a lock-holding denial of service.

CPython can't interrupt a running regex, so the patterns run in a child process with a timeout.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from agent_testing.env import example_turn_env

SRC = Path(__file__).parents[2] / "src"
PER_CALL_BOUND = 0.1  # seconds; linear patterns take well under a millisecond here
CHILD_BOUND = 60  # seconds for the whole battery; a catastrophic pattern never finishes

CHILD = r"""
import json, sys, time
from agent.core import c01, c02

cap = int(sys.argv[1])
units = [
    "a", " ", "<", "< / ", "[", "[/", "<|", "_", "-", "=", "A", "Aa1+", "Aa1-", "A" * 23 + " ",
    "ignore ", "ignore all ", "ignore all previous ", "ignore_", "stay in ", "system ",
    "you are now a ", "you're now an ", "you are now the ", "pre-", "pre-approv",
    "approved by your ", "policy ", "warrant ", "lookup_", '"function" ', '"tool_calls',
    "sue ", "you are such an ", "take you to ", "dispute the ", "charge ", "call my ", "fuck",
    "s\N{CYRILLIC SMALL LETTER U}", "\N{GREEK SMALL LETTER MU}m ", "k\N{OHM SIGN}",
]
inputs = []
for unit in units:
    body = (unit * (cap // len(unit) + 1))[:cap]
    inputs += [body, body[:-1] + "!"]

checks = {f"c01:{k}": (p.search, c01.normalize) for k, p in c01.PATTERNS.items()}
checks |= {f"c02:{k}": (p.search, c01.normalize) for k, p in c02.PATTERNS.items()}
checks["c02:encoded_payload"] = (c02.has_encoded_payload, lambda t: t)
checks["c02:mixed_script"] = (c02.has_mixed_script_word, c01.normalize)

worst = {}
for name, (check, prepare) in checks.items():
    slowest = 0.0
    for text in inputs:
        prepared = prepare(text)
        start = time.perf_counter()
        check(prepared)
        slowest = max(slowest, time.perf_counter() - start)
    worst[name] = slowest
lengths = sorted({len(t) for t in inputs})
print(json.dumps({"inputs": len(inputs), "lengths": lengths, "worst": worst}))
"""


def message_cap() -> int:
    return int(example_turn_env()["CUSTOMER_MESSAGE_MAX_CHARS"])


def test_every_c01_and_c02_pattern_is_linear_at_the_message_cap() -> None:
    try:
        done = subprocess.run(
            [sys.executable, "-c", CHILD, str(message_cap())],
            capture_output=True,
            text=True,
            timeout=CHILD_BOUND,
            env={"PYTHONPATH": str(SRC)},
            check=True,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"a pattern didn't finish within {CHILD_BOUND} s: catastrophic backtracking")
    report = json.loads(done.stdout)

    assert report["lengths"] == [message_cap()]
    assert {name.split(":")[0] for name in report["worst"]} == {"c01", "c02"}
    slow = {name: t for name, t in report["worst"].items() if t > PER_CALL_BOUND}
    assert slow == {}, f"patterns over {PER_CALL_BOUND} s on a {message_cap()}-char input"
