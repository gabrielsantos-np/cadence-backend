"""Pull JSON out of a model response.

Not every model served through OpenRouter honours `response_format`, and some
wrap the payload in prose or a fenced block. Parsing defensively here is
cheaper than a repair round-trip.
"""

import json
import re
from typing import Any

_FENCED = re.compile(r"```(?:json)?\s*([\s\S]*?)```")
_FIRST_OPEN = re.compile(r"[\[{]")


def extract_json(raw: str) -> Any:
    fenced = _FENCED.search(raw)
    candidate = (fenced.group(1) if fenced else raw).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass  # Fall through to the widest-braces heuristic below.

    match = _FIRST_OPEN.search(candidate)
    first = match.start() if match else -1
    last = max(candidate.rfind("}"), candidate.rfind("]"))
    if first == -1 or last <= first:
        raise ValueError("Model returned no JSON.") from None
    return json.loads(candidate[first : last + 1])
