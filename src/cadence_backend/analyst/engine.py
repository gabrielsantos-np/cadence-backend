"""The two-pass analyst.

Pass one gathers evidence with tools; pass two composes it into typed blocks.
They are kept separate on purpose — merging them makes the gathering step worse
at deciding what to query next.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError

from cadence_backend.analyst.prompts import (
    COMPOSE_PROMPT,
    TITLE_PROMPT,
    build_system_prompt,
)
from cadence_backend.analyst.tools import build_tools
from cadence_backend.llm import complete, default_model, extract_json, llm
from cadence_backend.schemas.answer import AnswerBlock
from cadence_backend.schemas.trace import NoteStep, SearchStep, SqlStep, TraceStep
from cadence_backend.sources import find_document_source, find_sql_source

logger = logging.getLogger(__name__)

#: Hard ceiling on tool round-trips, so a confused model cannot loop forever.
MAX_TURNS = 14

_BLOCK_ADAPTER: TypeAdapter[AnswerBlock] = TypeAdapter(AnswerBlock)


@dataclass(frozen=True)
class StepEvent:
    step: TraceStep


@dataclass(frozen=True)
class AnswerEvent:
    blocks: list[AnswerBlock]


@dataclass(frozen=True)
class ErrorEvent:
    message: str


AnalystEvent = StepEvent | AnswerEvent | ErrorEvent

PriorTurn = dict[str, str]


def _validate_blocks(raw: Any) -> list[AnswerBlock]:
    """Keep the blocks that parse, drop the ones that do not.

    Deliberately lenient: throwing away a whole answer over one malformed
    block trades a useful result for a cosmetic problem.
    """
    if isinstance(raw, dict):
        raw = raw.get("blocks")
    if not isinstance(raw, list):
        raise ValueError("Model returned no answer blocks.")

    blocks: list[AnswerBlock] = []
    for item in raw:
        try:
            blocks.append(_BLOCK_ADAPTER.validate_python(item))
        except ValidationError as error:
            kind = item.get("type") if isinstance(item, dict) else type(item).__name__
            logger.warning(
                "dropped malformed answer block (type=%s): %s",
                kind,
                error.errors()[0].get("msg", "invalid"),
            )
    if not blocks:
        raise ValueError("Model returned no answer blocks.")
    return blocks


async def run_analyst(
    question: str,
    prior_turns: list[PriorTurn] | None = None,
    model_override: str | None = None,
) -> AsyncIterator[AnalystEvent]:
    """Run the analyst, yielding trace steps as they complete, then the answer."""
    client = llm()
    model = model_override or default_model()
    messages: list[Any] = [
        {"role": "system", "content": build_system_prompt()},
        *(prior_turns or []),
        {"role": "user", "content": question},
    ]

    sql_step_count = 0
    step_no = 0

    for _turn in range(MAX_TURNS):
        completion = await complete(
            client,
            model=model,
            messages=messages,
            tools=build_tools(),
            max_tokens=4096,
        )

        if not completion.choices:
            yield ErrorEvent("The model returned no response.")
            return

        message = completion.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        calls = message.tool_calls or []
        if not calls:
            break

        for call in calls:
            if call.type != "function":
                continue
            started = time.monotonic()

            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": "Could not parse your arguments as JSON. Try again.",
                    }
                )
                continue

            name = call.function.name

            if name == "run_sql":
                sql = (args.get("sql") or "").strip()
                purpose = args.get("purpose") or "Queried a source"
                try:
                    source = find_sql_source(args.get("source") or "")
                    outcome = await source.query(sql)
                    step_no += 1
                    sql_step_count += 1
                    yield StepEvent(
                        SqlStep(
                            id=f"s{step_no}",
                            label=purpose,
                            duration_ms=outcome.duration_ms,
                            source=source.trace_label,
                            sql=sql,
                            columns=outcome.columns,
                            rows=outcome.rows,
                            row_count=outcome.row_count,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(
                                {
                                    "columns": outcome.columns,
                                    "rows": outcome.rows,
                                    "row_count": outcome.row_count,
                                    "truncated": outcome.truncated,
                                }
                            ),
                        }
                    )
                except Exception as error:
                    # Errors go back to the model, not to the user — a failed
                    # query is a normal part of the loop and the model usually
                    # repairs it.
                    detail = str(error)
                    step_no += 1
                    yield StepEvent(
                        NoteStep(
                            id=f"s{step_no}",
                            label=f"{purpose} — query failed, retrying",
                            outcome="retry",
                            duration_ms=int((time.monotonic() - started) * 1000),
                            detail=detail[:400],
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"Query failed: {detail}. Fix the SQL and try again.",
                        }
                    )

            elif name == "search_documents":
                query = args.get("query") or ""
                try:
                    source = find_document_source(args.get("source") or "")
                    results = await source.search(query, question)
                    step_no += 1
                    yield StepEvent(
                        SearchStep(
                            id=f"s{step_no}",
                            label=f'Checking {source.name.lower()}: "{query}"',
                            source=source.name,
                            duration_ms=int((time.monotonic() - started) * 1000),
                            query=query,
                            results=results,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(
                                [r.model_dump(by_alias=True, exclude_none=True) for r in results]
                            ),
                        }
                    )
                except Exception as error:
                    # A bad source id is as retryable as bad SQL: hand it back
                    # to the model rather than killing the run.
                    detail = str(error)
                    step_no += 1
                    yield StepEvent(
                        NoteStep(
                            id=f"s{step_no}",
                            label=f'Searching for "{query}" failed, retrying',
                            outcome="retry",
                            duration_ms=int((time.monotonic() - started) * 1000),
                            detail=detail[:400],
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"Search failed: {detail}. Try again.",
                        }
                    )

            else:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": f"Unknown tool: {name}",
                    }
                )

    # Composition pass. Separate from the gathering loop so the model can focus
    # on shaping the answer without also deciding what to query next.
    compose_started = time.monotonic()
    try:
        composed = await complete(
            client,
            model=model,
            messages=[*messages, {"role": "user", "content": COMPOSE_PROMPT}],
            max_tokens=8192,
            response_format={"type": "json_object"},
        )
        raw = composed.choices[0].message.content or "" if composed.choices else ""
        blocks = _validate_blocks(extract_json(raw))

        step_no += 1
        yield StepEvent(
            NoteStep(
                id=f"s{step_no}",
                label="Composed the answer",
                outcome="composed",
                duration_ms=int((time.monotonic() - compose_started) * 1000),
                detail=(f"Turned {sql_step_count} query results into {len(blocks)} answer blocks."),
            )
        )
        yield AnswerEvent(blocks)
    except Exception as error:
        yield ErrorEvent(f"Could not compose an answer: {error}")


async def title_for(question: str) -> str:
    """Name a conversation from its opening question. Falls back to a truncation."""
    fallback = f"{question[:52].rstrip()}…" if len(question) > 52 else question
    try:
        completion = await complete(
            llm(),
            model=default_model(),
            max_tokens=32,
            messages=[
                {"role": "system", "content": TITLE_PROMPT},
                {"role": "user", "content": question},
            ],
        )
        title = (completion.choices[0].message.content or "").strip()
        return title.strip("\"'")[:80] if title else fallback
    except Exception:
        logger.warning("could not generate a title; using the question", exc_info=True)
        return fallback
