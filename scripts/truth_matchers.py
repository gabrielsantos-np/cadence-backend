"""Regexes that accept a correct answer and reject a wrong one.

Shared by the warehouse-derived case generators. This lives on its own because
getting it wrong is silent and expensive: the first version graded `4.24|4.2`
with an unescaped dot, so "3.3" matched inside "13.35", and a panel estimate of
3,456,019 was satisfied by a bare "3 million". Both would have inflated accuracy
rather than failing loudly, and a benchmark that scores wrong answers as correct
is worse than no benchmark.

`tests/test_truth_matchers.py` pins the behaviour these need to have.
"""

#: A figure must not be preceded or followed by another digit, and must not be
#: preceded by a decimal point — otherwise "3.3" matches inside "13.35" and
#: "86,000" matches inside "186,000".
GUARD_L = r"(?<![\d.])"
GUARD_R = r"(?![\d])"


def number_forms(value: int) -> str:
    """A regex accepting the ways a model legitimately writes one integer.

    Tolerates presentation without tolerating a wrong figure: 86000, 86,000 and
    +86,000 are the same answer; 86,001 is not.

    Rounded millions are offered only at a precision that actually round-trips,
    so 3,456,019 accepts "3.46 million" but never a bare "3 million" — the
    latter is a different number, and accepting it would pass an answer that is
    wrong by hundreds of thousands.
    """
    magnitude = abs(value)
    forms = [f"{magnitude:,}".replace(",", r",?"), str(magnitude)]
    if magnitude >= 1_000_000:
        millions = magnitude / 1_000_000
        for places in (1, 2):
            if abs(round(millions, places) * 1_000_000 - magnitude) < 0.5 * 10 ** (6 - places):
                forms.append(rf"{millions:.{places}f}".replace(".", r"\.") + r"\s*(?:m\b|million)")
    return GUARD_L + "(?:" + "|".join(forms) + ")" + GUARD_R


def decimal_forms(value: float, places: int = 2) -> str:
    """A regex for a rate, percentage or money figure.

    Accepts one fewer decimal place than given, because 4.24 may legitimately
    be written 4.2 — but not 4.3, and not 4.24 appearing inside 14.24.
    """
    exact = f"{value:.{places}f}".replace(".", r"\.")
    rounded = f"{value:.{places - 1}f}".replace(".", r"\.")
    return GUARD_L + f"(?:{exact}|{rounded})" + GUARD_R


def money_forms(millions: float) -> str:
    """A regex for a figure the warehouse stores in millions.

    The ledger stores 1436.52 meaning $1,436.52m, and a model may write it as
    "$1,436.52m", "1436.5" or "$1.44 billion". All three are the same answer;
    the billions form is offered only where it round-trips.
    """
    # Grouped and ungrouped, at both precisions: "$1,436.5m" is as legitimate a
    # way to write 1436.52 as "1436.52" is, and omitting the grouped one-decimal
    # form marked a correct answer wrong.
    forms = [
        f"{millions:,.2f}".replace(",", r",?").replace(".", r"\."),
        f"{millions:,.1f}".replace(",", r",?").replace(".", r"\."),
        f"{millions:.2f}".replace(".", r"\."),
        f"{millions:.1f}".replace(".", r"\."),
    ]
    if millions >= 1000:
        billions = millions / 1000
        if abs(round(billions, 2) * 1000 - millions) < 5:
            forms.append(rf"{billions:.2f}".replace(".", r"\.") + r"\s*(?:b\b|billion)")
    return GUARD_L + "(?:" + "|".join(forms) + ")" + GUARD_R
