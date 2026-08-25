"""Server-Sent Events serialisation.

SSE is the transport; the payload stays JSON. Deliberately small — the wire
format is four lines of rules, not a framework.
"""

import json
from typing import Any

from pydantic import BaseModel


def format_sse(event: str, payload: Any) -> str:
    """Serialise one SSE frame: an event name, its JSON data, and a blank line.

    Compact separators plus the default ``ensure_ascii`` guarantee the encoded
    data holds no literal newline, which would otherwise split one frame into
    two and corrupt the stream.
    """
    if isinstance(payload, BaseModel):
        data = payload.model_dump_json(by_alias=True, exclude_none=True)
    else:
        data = json.dumps(payload, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"
