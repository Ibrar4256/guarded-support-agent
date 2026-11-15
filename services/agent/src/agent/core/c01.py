"""C01_LEGAL_OR_ABUSE: deterministic patterns over customer messages (ADR-009 conversation rules).

Runs on every customer message and never on KB articles or tool output, so injected content
can't trigger an escalation. Only the matched pattern IDs leave this module: no message text is
logged or stored (ADR-009 decision log). Being deterministic is what lets it run inside the turn's
start transaction (ADR-022 S1); a model classifier would have to run before it.

Known blind spots (ADR-009): negation ("I'm not going to sue" still matches, an over-escalation),
other languages, and deliberate obfuscation ("f*ck"). A miss is a customer-care miss, not a money
risk: every refund still passes R01-R19.
"""

import re
import unicodedata
from dataclasses import dataclass

RULE_ID = "C01_LEGAL_OR_ABUSE"
C01_VERSION = "c01-v1"

PATTERNS: dict[str, re.Pattern[str]] = {
    "C01_LEGAL_ACTION": re.compile(
        r"\b(?:sue|suing|sued|lawsuit|legal action|class action|small claims"
        r"|take (?:you|this|them|it) to court)\b"
    ),
    "C01_LEGAL_COUNSEL": re.compile(r"\b(?:lawyers?|attorneys?|solicitors?|legal counsel)\b"),
    "C01_CHARGEBACK": re.compile(
        r"\b(?:charge ?backs?|dispute (?:the|this|a|my) (?:charge|payment|transaction)"
        r"|(?:call|contact|tell) my bank|reverse the (?:charge|payment))\b"
    ),
    "C01_ABUSE_PROFANITY": re.compile(
        r"\b(?:fuck\w*|shit\w*|bitch\w*|bastard\w*|asshole\w*|cunt\w*|dickhead\w*)\b"
    ),
    "C01_ABUSE_INSULT": re.compile(
        r"\byou(?:'re| are)? (?:such )?(?:an? )?"
        r"(?:idiot|moron|stupid|useless|pathetic|incompetent)\b"
    ),
}

_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})


@dataclass(frozen=True)
class C01Result:
    patterns: tuple[str, ...]  # matched pattern IDs, in table order

    @property
    def flagged(self) -> bool:
        return bool(self.patterns)


def normalize(message: str) -> str:
    """NFKC (full-width and other look-alike forms), case-folded, one space between words."""
    folded = unicodedata.normalize("NFKC", message).translate(_APOSTROPHES).casefold()
    return " ".join(folded.split())


def classify_customer_message(message: str) -> C01Result:
    text = normalize(message)
    return C01Result(tuple(pid for pid, pattern in PATTERNS.items() if pattern.search(text)))
