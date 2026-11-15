"""C01_LEGAL_OR_ABUSE's deterministic patterns (ADR-009 conversation rules)."""

import pytest

from agent.core.c01 import PATTERNS, classify_customer_message


@pytest.mark.parametrize(
    ("message", "pattern"),
    [
        ("I will sue you over this", "C01_LEGAL_ACTION"),
        ("I'm filing a lawsuit tomorrow", "C01_LEGAL_ACTION"),
        ("I'll take you to court", "C01_LEGAL_ACTION"),
        ("My lawyer will be in touch", "C01_LEGAL_COUNSEL"),
        ("I'm going to do a chargeback", "C01_CHARGEBACK"),
        ("I will dispute the charge with my card", "C01_CHARGEBACK"),
        ("Fine, I'll call my bank", "C01_CHARGEBACK"),
        ("this is fucking ridiculous", "C01_ABUSE_PROFANITY"),
        ("you're useless", "C01_ABUSE_INSULT"),
        ("You are an idiot", "C01_ABUSE_INSULT"),
    ],
)
def test_each_pattern_flags_its_case(message: str, pattern: str) -> None:
    result = classify_customer_message(message)

    assert result.flagged
    assert pattern in result.patterns


@pytest.mark.parametrize(
    "message",
    [
        "Where is my order ord_demo_1001?",
        "Can I get a refund for the broken blender?",
        "Thanks, that was really useful!",
        "Is the payment pending or charged already?",
        "I'll shop with you again",
        "Assume nothing; I just want my order",
    ],
)
def test_ordinary_messages_pass(message: str) -> None:
    assert not classify_customer_message(message).flagged


@pytest.mark.parametrize(
    "message",
    [
        "I WILL SUE",  # case
        "\uff33\uff35\uff25 you",  # full-width letters (NFKC)
        "you\u2019re useless",  # a curly apostrophe
        "my   lawyer",  # extra spaces
    ],
)
def test_normalization_catches_look_alike_forms(message: str) -> None:
    assert classify_customer_message(message).flagged


def test_negation_is_a_known_blind_spot_and_over_escalates() -> None:
    """ADR-009: deterministic rules can't read negation; this over-escalates by design."""
    assert classify_customer_message("I'm not going to sue anyone").flagged


def test_obfuscation_is_a_known_blind_spot() -> None:
    assert not classify_customer_message("f*ck this").flagged


def test_the_result_carries_pattern_ids_only() -> None:
    result = classify_customer_message("my lawyer says I should sue")

    assert result.patterns == ("C01_LEGAL_ACTION", "C01_LEGAL_COUNSEL")
    assert set(result.patterns) <= set(PATTERNS)
    assert "lawyer" not in repr(result) and "sue" not in repr(result).replace("C01_", "")
