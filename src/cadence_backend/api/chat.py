"""POST /api/chat — the streaming analyst endpoint."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from cadence_backend import analyst
from cadence_backend.conversations import append_message, create_conversation, prior_turns
from cadence_backend.core.sse import format_sse
from cadence_backend.llm import track_usage
from cadence_backend.schemas import chat as wire
from cadence_backend.schemas.answer import CalloutBlock

logger = logging.getLogger(__name__)

#: Detached writes are held here so the event loop cannot garbage-collect a
#: task that is still finishing after the client has gone.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _detach(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


router = APIRouter(prefix="/api", tags=["chat"])

#: Proxies buffer text/event-stream by default, which defeats the point of
#: streaming. These two headers are load-bearing, not decoration.
STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}

STREAM_DESCRIPTION = """\
Streams Server-Sent Events. SSE is the transport; every payload is JSON.

```
event: conversation
data: {"id":"...","isNew":true}

event: step
data: {"step":{"id":"s1","kind":"sql","label":"...","durationMs":12,
       "source":"Queried market dataset","sql":"...","columns":[],"rows":[],
       "rowCount":0},"elapsedMs":900}

event: answer
data: {"blocks":[],"elapsedMs":1200}

event: done
data: {}
```

`step.kind` is one of `sql`, `search` or `note`. Ordering is fixed:
`conversation` is always first and `done` always last. `answer` is emitted even
when the run failed, and an `error` event accompanies it rather than replacing
it.
"""


async def analyst_stream(request: wire.ChatRequest) -> AsyncIterator[str]:
    """Yield SSE frames for one question.

    Whatever goes wrong, the engine turn still has to be persisted — including
    when the client walks away mid-run. Letting either case escape leaves a
    conversation holding a user message and no reply.
    """
    run_started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - run_started) * 1000)

    try:
        # A conversation is created on the first question, not when "New chat"
        # is pressed — an empty conversation would only clutter the sidebar.
        conversation_id = request.conversation_id
        is_new = conversation_id is None
        if conversation_id is None:
            conversation_id = await create_conversation(await analyst.title_for(request.question))

        logger.info(
            "chat request: conversation_id=%s is_new=%s question_length=%d model=%s",
            conversation_id,
            is_new,
            len(request.question),
            request.model or "default",
        )
        yield format_sse("conversation", wire.ConversationEvent(id=conversation_id, is_new=is_new))

        await append_message(conversation_id, "user", {"text": request.question})
        history = [] if is_new else await prior_turns(conversation_id)

        steps: list[dict] = []
        blocks: list = []
        failure: str | None = None
        persisted = False

        def failure_blocks(message: str) -> list:
            # Persist the failure alongside the trace: the queries that did run
            # are still worth keeping, and the conversation stays coherent.
            return [
                CalloutBlock(
                    tone="warning",
                    eyebrow="The analyst could not complete this answer",
                    text=message,
                )
            ]

        async def persist(final_blocks: list, total_ms: int) -> None:
            nonlocal persisted
            persisted = True
            await append_message(
                conversation_id,
                "engine",
                {
                    "steps": steps,
                    "blocks": [
                        b.model_dump(by_alias=True, exclude_none=True) for b in final_blocks
                    ],
                    "elapsedMs": total_ms,
                },
            )

        try:
            # Scoped to this turn: concurrent requests share an event loop, and
            # a module-level total would bill one turn for another's tokens.
            with track_usage() as usage:
                try:
                    async for event in analyst.run_analyst(
                        request.question, history, request.model
                    ):
                        if isinstance(event, analyst.StepEvent):
                            steps.append(event.step.model_dump(by_alias=True, exclude_none=True))
                            yield format_sse(
                                "step", wire.StepEvent(step=event.step, elapsed_ms=elapsed())
                            )
                        elif isinstance(event, analyst.AnswerEvent):
                            blocks = event.blocks
                        elif isinstance(event, analyst.ErrorEvent):
                            failure = event.message
                except Exception as error:
                    logger.exception("analyst run failed")
                    failure = str(error)

            if failure and not blocks:
                blocks = failure_blocks(failure)

            total_ms = elapsed()
            await persist(blocks, total_ms)
            logger.info(
                "turn complete in %.1fs · %d model calls · %d tokens · %s",
                total_ms / 1000,
                usage.calls,
                usage.prompt_tokens + usage.completion_tokens,
                f"${usage.cost_usd:.4f}" if usage.cost_usd is not None else "cost unreported",
            )
            yield format_sse(
                "answer",
                wire.AnswerEvent(
                    blocks=blocks,
                    elapsed_ms=total_ms,
                    cost_usd=usage.cost_usd,
                    tokens=(usage.prompt_tokens + usage.completion_tokens) or None,
                ),
            )
            if failure:
                yield format_sse("error", wire.ErrorEvent(message=failure))
            yield format_sse("done", wire.DoneEvent())

        finally:
            if not persisted:
                # The client disconnected, so this generator is being closed
                # part-way through. Awaiting here would be cancelled too, so
                # the write is handed to a detached task that outlives us.
                logger.warning(
                    "client disconnected mid-run; persisting a partial turn for %s",
                    conversation_id,
                )
                message = failure or "The run was interrupted before it finished."
                _detach(persist(blocks or failure_blocks(message), elapsed()))

    except Exception as error:
        # Nothing could be persisted — report and terminate cleanly rather than
        # leaving the client waiting on a stream that will never close.
        logger.exception("chat stream failed before the analyst could run")
        yield format_sse("error", wire.ErrorEvent(message=str(error)))
        yield format_sse("done", wire.DoneEvent())


@router.post(
    "/chat",
    summary="Ask the analyst a question",
    description=STREAM_DESCRIPTION,
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "An SSE stream of `conversation`, `step`, `answer`, `error` and `done` events."
            ),
        }
    },
)
async def chat(request: wire.ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        analyst_stream(request),
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )
