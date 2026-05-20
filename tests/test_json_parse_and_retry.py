"""Regression tests for the LLM-response JSON parser and retry policy.

The previous greedy ``\\[.*\\]`` regex collapsed multiple JSON blocks into
one, breaking down-stream parsers. The new balanced-bracket walker should
extract only the first complete JSON value.

The retry policy now validates inputs in ``__post_init__`` instead of
relying on a post-loop assertion that was effectively dead code.
"""
from __future__ import annotations

import pytest


def test_parse_json_extracts_first_array():
    from backend.utils.json_parse import parse_json_array
    raw = 'Sure! Here is your data: [1, 2, 3] and a stray [4]'
    result = parse_json_array(raw)
    # Must be exactly the first array — not "[1, 2, 3] and a stray [4]" or [4].
    assert result == [1, 2, 3]


def test_parse_json_handles_fenced_blocks():
    from backend.utils.json_parse import parse_json_object
    raw = '```json\n{"k": 1, "list": [2, 3]}\n```'
    assert parse_json_object(raw) == {"k": 1, "list": [2, 3]}


def test_parse_json_array_with_nested_objects():
    from backend.utils.json_parse import parse_json_array
    raw = 'Result:\n[{"a": 1, "items": [10, 20]}, {"a": 2}]\nThanks.'
    result = parse_json_array(raw)
    assert isinstance(result, list)
    assert result[0]["items"] == [10, 20]
    assert result[1] == {"a": 2}


def test_parse_json_array_with_strings_containing_brackets():
    """Strings that contain brackets used to throw off naive depth counters."""
    from backend.utils.json_parse import parse_json_array
    raw = '[{"text": "arr[0]"}, {"text": "obj{1}"}]'
    result = parse_json_array(raw)
    assert result == [{"text": "arr[0]"}, {"text": "obj{1}"}]


def test_parse_json_object_finds_first_when_array_preceding():
    """`prefer='object'` should walk past a leading array."""
    from backend.utils.json_parse import parse_json_object
    raw = 'Note [a, b] then JSON {"k": "v"}'
    assert parse_json_object(raw) == {"k": "v"}


def test_parse_json_empty_input_returns_none():
    from backend.utils.json_parse import parse_json
    assert parse_json("") is None
    assert parse_json("   ") is None


def test_retry_policy_rejects_zero_attempts():
    from backend.utils.retry import RetryPolicy
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)


def test_retry_policy_rejects_negative_initial_delay():
    from backend.utils.retry import RetryPolicy
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=3, initial_delay=-1.0)


def test_retry_policy_rejects_backoff_below_one():
    from backend.utils.retry import RetryPolicy
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=3, backoff_factor=0.5)


def test_retry_policy_jitter_must_be_in_range():
    from backend.utils.retry import RetryPolicy
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=3, jitter=1.5)
