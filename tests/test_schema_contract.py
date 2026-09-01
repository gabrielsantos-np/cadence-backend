"""The payload vocabulary the frontend already consumes.

The phase-1 stream cannot emit `conversation`, `step` or `answer` events
honestly, so their serialisation is pinned here instead. camelCase is the wire
format; a regression to snake_case would silently break every consumer.
"""

import json

from cadence_backend.core.sse import format_sse
from cadence_backend.schemas.answer import Finding, FindingBlock, LineChartBlock, LineChartSpec
from cadence_backend.schemas.chat import AnswerEvent, ConversationEvent, DoneEvent, StepEvent
from cadence_backend.schemas.trace import SqlStep


def dump(model) -> dict:
    return json.loads(model.model_dump_json(by_alias=True, exclude_none=True))


def test_sql_step_serialises_as_camel_case() -> None:
    step = SqlStep(
        id="s1",
        label="Revenue by service",
        duration_ms=12,
        source="Queried market dataset",
        sql="SELECT 1",
        columns=["service"],
        rows=[["Grovehouse"]],
        row_count=1,
    )

    assert dump(step) == {
        "id": "s1",
        "kind": "sql",
        "label": "Revenue by service",
        "durationMs": 12,
        "source": "Queried market dataset",
        "sql": "SELECT 1",
        "columns": ["service"],
        "rows": [["Grovehouse"]],
        "rowCount": 1,
    }


def test_step_event_wraps_a_trace_step() -> None:
    step = SqlStep(
        id="s1",
        label="x",
        duration_ms=1,
        source="Queried market dataset",
        sql="SELECT 1",
        columns=[],
        rows=[],
        row_count=0,
    )

    payload = dump(StepEvent(step=step, elapsed_ms=900))

    assert payload["elapsedMs"] == 900
    assert payload["step"]["kind"] == "sql"


def test_optional_finding_fields_are_omitted_not_null() -> None:
    """`soWhat` is absent on set-aside cards — it must not serialise as null."""
    finding = Finding(
        kicker="Price rise 1",
        context="Lumora+ · May 2024",
        headline="h",
        body="b",
        confidence_label="Strong evidence",
        confidence_score=8,
        accent="navy",
    )

    payload = dump(FindingBlock(finding=finding))

    assert payload["type"] == "finding"
    assert "soWhat" not in payload["finding"]
    assert "setAside" not in payload["finding"]
    assert payload["finding"]["confidenceScore"] == 8
    assert payload["finding"]["confidenceLabel"] == "Strong evidence"


def test_chart_points_keep_gaps_and_camel_case_keys() -> None:
    chart = LineChartSpec(
        title="Subscribers",
        x_key="month",
        x_label="Month",
        y_label="Subscribers",
        series=[{"key": "kestrel", "label": "Kestrel", "color": "#2a78d6"}],
        # The second point deliberately omits "kestrel": a missing key breaks
        # the line, which is how a documented gap is shown honestly.
        points=[{"month": "2024-01", "kestrel": 1_200_000}, {"month": "2024-02"}],
    )

    payload = dump(LineChartBlock(chart=chart))

    assert payload["type"] == "lineChart"
    assert payload["chart"]["xKey"] == "month"
    assert "subtitle" not in payload["chart"]
    assert "kestrel" not in payload["chart"]["points"][1]


def test_answer_event_carries_a_block_list() -> None:
    payload = dump(AnswerEvent(blocks=[], elapsed_ms=1200))

    assert payload == {"blocks": [], "elapsedMs": 1200}


def test_conversation_event_shape() -> None:
    payload = dump(ConversationEvent(id="abc", is_new=True))

    assert payload == {"id": "abc", "isNew": True}


def test_format_sse_frames_an_event() -> None:
    frame = format_sse("conversation", ConversationEvent(id="abc", is_new=True))

    assert frame == 'event: conversation\ndata: {"id":"abc","isNew":true}\n\n'


def test_format_sse_never_emits_a_raw_newline_in_data() -> None:
    """A literal newline in the data would split one frame into two."""
    frame = format_sse("error", {"message": "line one\nline two"})

    data_line = frame.split("\n")[1]
    assert data_line.startswith("data: ")
    assert frame.count("\n") == 3  # event line, data line, then the blank line


def test_done_event_is_an_empty_object() -> None:
    assert format_sse("done", DoneEvent()) == "event: done\ndata: {}\n\n"


def test_search_result_score_is_optional_and_omitted() -> None:
    """A source that does not score must stay byte-identical on the wire.

    The frontend has shipped against a four-field SearchResult. An optional
    score plus exclude_none keeps that contract additive — the same guarantee
    Finding.so_what relies on.
    """
    from cadence_backend.schemas.trace import SearchResult

    unscored = SearchResult(
        title="Coverage note",
        source="Cobalt Research Group",
        reference="CRG-COV-2026",
        snippet="Where a parent reports two services jointly...",
    )
    assert dump(unscored) == {
        "title": "Coverage note",
        "source": "Cobalt Research Group",
        "reference": "CRG-COV-2026",
        "snippet": "Where a parent reports two services jointly...",
    }

    scored = SearchResult(
        title="t",
        source="s",
        reference="r",
        snippet="x",
        score=0.8312,
    )
    assert dump(scored)["score"] == 0.8312


def test_cost_is_omitted_when_the_gateway_did_not_report_one() -> None:
    """A turn is either fully costed or says nothing — never a zero it invented.

    The frontend and the acceptance runner both treat an absent key as "not
    measured". Serialising 0.0 instead would read as "this was free".
    """
    payload = dump(AnswerEvent(blocks=[], elapsed_ms=1200))

    assert payload == {"blocks": [], "elapsedMs": 1200}
    assert "costUsd" not in payload
    assert "tokens" not in payload


def test_cost_rides_along_when_it_is_known() -> None:
    payload = dump(AnswerEvent(blocks=[], elapsed_ms=1200, cost_usd=1.2345, tokens=98_765))

    assert payload["costUsd"] == 1.2345
    assert payload["tokens"] == 98_765
