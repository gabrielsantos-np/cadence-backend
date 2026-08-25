"""The defensive JSON parser.

Not every model served through OpenRouter honours `response_format`, and some
wrap the payload in prose or a fenced block.
"""

import pytest

from cadence_backend.llm import extract_json


def test_parses_plain_json() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('[{"a": 1}]') == [{"a": 1}]


def test_strips_a_code_fence() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json("```\n[1, 2]\n```") == [1, 2]


def test_recovers_json_wrapped_in_prose() -> None:
    assert extract_json('Sure! {"a": 1} hope that helps.') == {"a": 1}
    assert extract_json('Here:\n[{"b": 2}]\nDone.') == [{"b": 2}]


def test_raises_when_there_is_no_json() -> None:
    with pytest.raises(ValueError, match="no JSON"):
        extract_json("I could not answer that.")
