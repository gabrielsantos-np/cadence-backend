"""The graders that decide whether an answer was correct.

These are tested properly rather than eyeballed because a loose matcher fails
in the worst direction: it scores a wrong answer as right, inflating accuracy
instead of failing loudly. The first version of the generator had exactly that
bug in two places, and would have reported a falsely perfect result.
"""

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from truth_matchers import decimal_forms, money_forms, number_forms  # noqa: E402


def matches(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.I))


# --------------------------------------------------------------------------
# Integers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "a net gain of 86,000",
        "86000 subscribers",
        "+86,000 net",
        "the figure was 86,000.",
    ],
)
def test_integer_accepts_legitimate_formatting(text: str) -> None:
    assert matches(number_forms(86_000), text)


@pytest.mark.parametrize(
    "text",
    [
        "186,000 subscribers",  # our number is a suffix of a bigger one
        "860,000 subscribers",  # a different number entirely
        "86,001 subscribers",  # off by one
        "8,600 subscribers",
    ],
)
def test_integer_rejects_wrong_or_embedded_figures(text: str) -> None:
    assert not matches(number_forms(86_000), text)


def test_millions_shorthand_only_at_a_precision_that_round_trips() -> None:
    pattern = number_forms(3_456_019)

    assert matches(pattern, "roughly 3.46 million cancellations")
    assert matches(pattern, "3,456,019 cancellations")
    # "3 million" is a different number — wrong by 456,019.
    assert not matches(pattern, "roughly 3 million cancellations")
    assert not matches(pattern, "roughly 8.9 million cancellations")


def test_a_value_below_a_million_offers_no_shorthand() -> None:
    assert "million" not in number_forms(240_000)


# --------------------------------------------------------------------------
# Rates and percentages
# --------------------------------------------------------------------------


def test_decimal_accepts_the_exact_value_and_one_place_less() -> None:
    pattern = decimal_forms(4.24)

    assert matches(pattern, "churn was 4.24%")
    assert matches(pattern, "churn was 4.2%")


@pytest.mark.parametrize("text", ["churn was 14.24%", "churn was 4.29%", "churn was 3.24%"])
def test_decimal_rejects_embedded_and_wrong_values(text: str) -> None:
    assert not matches(decimal_forms(4.24), text)


def test_decimal_dot_is_escaped_not_a_wildcard() -> None:
    """An unescaped dot made '3.3' match '13.35'. It must not."""
    assert not matches(decimal_forms(3.28), "the rate was 13.35%")
    assert not matches(decimal_forms(3.28), "the rate was 3x28")


# --------------------------------------------------------------------------
# Money held in millions
# --------------------------------------------------------------------------


def test_money_accepts_the_forms_a_model_actually_writes() -> None:
    pattern = money_forms(1436.52)

    assert matches(pattern, "$1,436.52m in advertising")
    assert matches(pattern, "1436.52")
    assert matches(pattern, "$1,436.5m")
    assert matches(pattern, "$1.44 billion")


def test_money_rejects_a_different_figure() -> None:
    pattern = money_forms(1436.52)

    assert not matches(pattern, "$1,436.99m")
    assert not matches(pattern, "$436.52m")
    assert not matches(pattern, "$2.10 billion")
