"""Provider methodology and coverage notes.

Small enough to keep in memory, and drawn from the same fiction as the
dataset so the two never disagree.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchNote:
    title: str
    source: str
    reference: str
    body: str


RESEARCH_NOTES: list[ResearchNote] = [
    ResearchNote(
        title="Panel note: churn response to standard-tier repricing",
        source="Meridian Panel Analytics",
        reference="MPA-2026-Q1-014",
        body="Across the panel, cancellations peak one month after a price rise takes effect and decay over roughly two further months. Peak-to-baseline ratios cluster between 1.6x and 2.0x. The multiplier is broadly constant across services; what varies is the baseline it multiplies.",
    ),
    ResearchNote(
        title="Methodology: cohort retention versus base churn",
        source="Meridian Panel Analytics",
        reference="MPA-METH-07",
        body="Cohort retention tracks subscribers who joined during the quarter. Because new joiners churn faster than tenured ones, cohort figures run below what base cancellation rates imply. The two are not inverses and should not be reconciled against each other.",
    ),
    ResearchNote(
        title="Coverage note: sub-scale service reporting threshold",
        source="Meridian Panel Analytics",
        reference="MPA-COV-2024",
        body="Panel coverage of sub-scale services began July 2024, once their panel cells cleared the reporting threshold. Genre totals before that month understate the market and must not be trended across the break.",
    ),
    ResearchNote(
        title="Coverage note: reporting entity and bundled disclosures",
        source="Cobalt Research Group",
        reference="CRG-COV-2026",
        body="Where a parent reports two services jointly, the combined figure is repeated on each service row and flagged with is_bundled. It cannot be decomposed into standalone service revenue by any method, including apportioning by subscriber share, because the services carry different prices and different discounting.",
    ),
    ResearchNote(
        title="Taxonomy note: single-genre attribution",
        source="Streamscope Telemetry Co-op",
        reference="STC-TAX-03",
        body="The co-op taxonomy assigns each title exactly one genre. A title that could plausibly sit in two categories contributes to only one, so genre viewing hours are a partition of total hours rather than an overlapping tally.",
    ),
    ResearchNote(
        title="Scope note: subscriber overlap is out of scope",
        source="Cobalt Research Group",
        reference="CRG-SCOPE-11",
        body="Cross-service subscriber overlap is not measured. No household, account or panel-person identifier links a subscription at one service to a subscription at another, so de-duplicated reach and overlap questions are outside what this dataset supports.",
    ),
]
