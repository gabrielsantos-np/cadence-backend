"""Answer composition — the typed blocks an answer is made of.

Answers are typed blocks, never prose.
"""

from typing import Annotated, Literal

from pydantic import Field

from cadence_backend.schemas.base import CamelModel

CalloutTone = Literal["assumption", "warning"]

#: Accent ramp used for a finding card's rail and its chart series.
Accent = Literal["navy", "green", "violet", "blue", "amber", "gray"]

#: A chart datum. Missing keys break the line, which is how gaps are shown
#: honestly — so absent keys must never be filled in with a default.
ChartPoint = dict[str, str | int | float | None]


class Metric(CamelModel):
    label: str
    value: str
    note: str
    #: Optional direction hint for the value colour.
    trend: Literal["up", "down", "flat"] | None = None


class Finding(CamelModel):
    #: e.g. "Price rise 1"
    kicker: str
    #: e.g. "Lumora+ · May 2024"
    context: str
    headline: str
    body: str
    #: What this changes about the decision — omitted on set-aside cards.
    so_what: str | None = None
    confidence_label: str
    #: Out of 10.
    confidence_score: float
    accent: Accent
    #: Considered but excluded; renders muted with a "Set aside" chip.
    set_aside: bool | None = None


class ChartSeries(CamelModel):
    key: str
    label: str
    color: str
    #: Renders dashed — used for projected rather than observed series.
    dashed: bool | None = None


class ChartMarker(CamelModel):
    x: str | int | float
    label: str


class LineChartSpec(CamelModel):
    title: str
    subtitle: str | None = None
    x_key: str
    x_label: str
    y_label: str
    #: Appended to tooltip values, e.g. "%" or "M".
    unit: str | None = None
    series: list[ChartSeries]
    points: list[ChartPoint]
    marker: ChartMarker | None = None


class BarChartSpec(CamelModel):
    title: str
    subtitle: str | None = None
    x_key: str
    y_label: str
    unit: str | None = None
    series: list[ChartSeries]
    points: list[ChartPoint]


class TextBlock(CamelModel):
    type: Literal["text"] = "text"
    text: str


class MetricsBlock(CamelModel):
    type: Literal["metrics"] = "metrics"
    items: list[Metric]


class CalloutBlock(CamelModel):
    type: Literal["callout"] = "callout"
    tone: CalloutTone
    eyebrow: str
    text: str


class FindingBlock(CamelModel):
    type: Literal["finding"] = "finding"
    finding: Finding


class LineChartBlock(CamelModel):
    type: Literal["lineChart"] = "lineChart"
    chart: LineChartSpec


class BarChartBlock(CamelModel):
    type: Literal["barChart"] = "barChart"
    chart: BarChartSpec


class BottomLineBlock(CamelModel):
    type: Literal["bottomLine"] = "bottomLine"
    text: str


AnswerBlock = Annotated[
    TextBlock
    | MetricsBlock
    | CalloutBlock
    | FindingBlock
    | LineChartBlock
    | BarChartBlock
    | BottomLineBlock,
    Field(discriminator="type"),
]
