"""C02_INJECTION_SIGNAL: deterministic signals of known injection forms in customer messages
(ADR-029 S1, an ADR-009 conversation rule).

Each customer message is classified once, when it arrives, and only the matched ids are stored
with it. At proposal a match in the conversation's window sends the refund to review: C02 can
only raise severity. Being deterministic and linear-time is what lets it run inside the turn's
start transaction, like C01. No pattern has nested or overlapping quantifiers.

Known blind spots (ADR-029): paraphrase, other languages, base64 split by spaces, a word spelled
entirely in look-alike letters of one script, and claimed approvals or invented policies worded
like ordinary requests. Those are W01's job; C02 catches the mechanical forms.
"""

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass

from agent.core.c01 import normalize

C02_VERSION = "c02-v1"

_SEP = r"[ _-]"
_L = r"(?<![a-z0-9])"
_R = r"(?![a-z0-9])"

PATTERNS: dict[str, re.Pattern[str]] = {
    "C02_ROLE_MARKER": re.compile(
        rf"< ?/? ?(?:system|assistant|developer|untrusted){_R}"
        r"|\[/?(?:system|assistant|developer|inst)\]"
        r"|<\|[a-z_]{1,20}\|>"
    ),
    "C02_INSTRUCTION_OVERRIDE": re.compile(
        rf"{_L}(?:ignore|disregard|forget){_SEP}(?:(?:all|any){_SEP})?"
        rf"(?:(?:previous|prior|above|earlier|your){_SEP})?"
        rf"(?:instructions|rules|directions|guidelines|prompts?){_R}"
        rf"|{_L}(?:system{_SEP}override|stay{_SEP}in{_SEP}character){_R}"
    ),
    "C02_ROLE_ASSIGNMENT": re.compile(
        rf"{_L}you(?:'re| are) now (?:a|an|the) [a-z0-9 -]{{0,30}}?"
        rf"{_L}(?:bot|assistant|ai|agent|model){_R}"
    ),
    "C02_AUTHORITY_CLAIM": re.compile(
        rf"{_L}pre{_SEP}?approv(?:ed|al){_R}"
        rf"|{_L}policy{_SEP}override{_R}"
        rf"|{_L}approved{_SEP}by{_SEP}(?:your|the|a){_SEP}"
        rf"(?:manager|supervisor|admin|administrator){_R}"
    ),
    "C02_INTERNAL_NAME": re.compile(
        rf"{_L}(?:propose_refund|lookup_order|warrant_verdict){_R}"
        rf"|{_L}warrant{_SEP}checker{_R}"
        r'|"tool_calls"|"function" ?:'
    ),
}
ENCODED_PAYLOAD = "C02_ENCODED_PAYLOAD"
MIXED_SCRIPT = "C02_MIXED_SCRIPT"

_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")
_PRINTABLE = frozenset(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}
PRINTABLE_SHARE = 0.8
# NFKC turns the micro and ohm signs into Greek letters, so "5µm" and "10kΩ" would read as
# mixed-script. A short word whose only non-Latin letter is one of them is a unit, not a tag.
_UNIT_LETTERS = frozenset({"\N{GREEK SMALL LETTER MU}", "\N{GREEK SMALL LETTER OMEGA}"})
_UNIT_MAX_LETTERS = 4
_LOOK_ALIKE_SCRIPTS = frozenset({"CYRILLIC", "GREEK"})


@dataclass(frozen=True)
class C02Result:
    patterns: tuple[str, ...]  # matched ids: table order, then the two detectors
    version: str = C02_VERSION

    @property
    def flagged(self) -> bool:
        return bool(self.patterns)


def _decodes_to_text(token: str) -> bool:
    body = token.rstrip("=")
    urlsafe = "-" in body or "_" in body
    if (urlsafe and ("+" in body or "/" in body)) or len(body) % 4 == 1:
        return False
    padded = body + "=" * (-len(body) % 4)
    try:
        raw = base64.b64decode(padded, altchars=b"-_" if urlsafe else None, validate=True)
    except (binascii.Error, ValueError):
        return False
    return bool(raw) and sum(b in _PRINTABLE for b in raw) >= PRINTABLE_SHARE * len(raw)


def has_encoded_payload(text: str) -> bool:
    """A run of 24+ base64 characters (either alphabet) that decodes to mostly printable text.
    Reads the NFKC text before case folding, which would corrupt the encoding."""
    return any(_decodes_to_text(m.group()) for m in _TOKEN.finditer(text))


def _script(letter: str) -> str:
    return unicodedata.name(letter, "").split(" ", 1)[0]


def _is_mixed(word: str) -> bool:
    scripts = {_script(c) for c in word}
    if "LATIN" not in scripts or not scripts & _LOOK_ALIKE_SCRIPTS:
        return False
    foreign = [c for c in word if _script(c) != "LATIN"]
    is_unit = len(word) <= _UNIT_MAX_LETTERS and len(foreign) == 1 and foreign[0] in _UNIT_LETTERS
    return not is_unit


def has_mixed_script_word(text: str) -> bool:
    """One word (a run of letters) mixing Latin with Cyrillic or Greek: the shape of a tag
    like ``<system>`` with one letter swapped for a look-alike."""
    word: list[str] = []
    for ch in text + " ":
        if ch.isalpha():
            word.append(ch)
            continue
        if word and _is_mixed("".join(word)):
            return True
        word = []
    return False


def classify_customer_message(message: str) -> C02Result:
    folded = normalize(message)
    found = [pid for pid, pattern in PATTERNS.items() if pattern.search(folded)]
    if has_encoded_payload(unicodedata.normalize("NFKC", message)):
        found.append(ENCODED_PAYLOAD)
    if has_mixed_script_word(folded):
        found.append(MIXED_SCRIPT)
    return C02Result(tuple(found))
