"""Message translation for the Gemini client, offline.

Covers one bug: Gemini requires the number of `function_response` parts to equal the number of
`function_call` parts in the turn being answered. `agent.py` emits one message per tool result,
so a model that called two tools in a single turn produced two separate one-part Contents and
the request failed with 400 INVALID_ARGUMENT. It degraded 16 of 27 B3 judgements while looking
exactly like a rate limit.

`llm.py` already merged consecutive tool results for Anthropic; only this client was missing it.
These tests exist so the two clients cannot drift apart on it again.

No network and no credentials: `_split` needs only the types module, so the client is built with
`object.__new__` rather than `__init__`, which would construct a real Vertex client.
"""

from __future__ import annotations

import pytest

pytest.importorskip("google.genai", reason="gemini client is optional")

from google.genai import types  # noqa: E402

from sentinel.judges.gemini import GeminiClient  # noqa: E402


def _client() -> GeminiClient:
    c = object.__new__(GeminiClient)
    c._types = types
    return c


def _tool_msg(name: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": f"{name}_0", "name": name, "content": content}


def test_parallel_tool_results_merge_into_one_content():
    """Two results for one assistant turn must arrive as ONE Content with two parts."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "summary"},
        {"role": "assistant", "tool_calls": [
            {"id": "a_0", "name": "list_advisories", "arguments": {}},
            {"id": "b_0", "name": "ordering", "arguments": {"type_a": "x", "type_b": "y"}},
        ]},
        _tool_msg("list_advisories", "[]"),
        _tool_msg("ordering", "{}"),
    ]
    system, contents = _client()._split(messages)

    assert system == "sys"
    model_turns = [c for c in contents if c.role == "model"]
    assert len(model_turns) == 1
    n_calls = sum(1 for p in model_turns[0].parts if p.function_call is not None)

    responses = [c for c in contents
                 if c.role == "user" and all(p.function_response is not None for p in c.parts)]
    assert len(responses) == 1, "two tool results must not become two Contents"
    assert len(responses[0].parts) == n_calls == 2


def test_single_tool_result_is_unchanged():
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "a_0", "name": "summarize", "arguments": {}}]},
        _tool_msg("summarize", "{}"),
    ]
    _, contents = _client()._split(messages)
    responses = [c for c in contents if c.role == "user"]
    assert len(responses) == 1 and len(responses[0].parts) == 1


def test_text_turns_are_never_merged_into_tool_results():
    """Merging a text turn in would reorder what the model sees."""
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "a_0", "name": "summarize", "arguments": {}}]},
        _tool_msg("summarize", "{}"),
        {"role": "user", "content": "That was not valid JSON."},
    ]
    _, contents = _client()._split(messages)
    user_turns = [c for c in contents if c.role == "user"]
    assert len(user_turns) == 2
    assert user_turns[1].parts[0].text == "That was not valid JSON."


def test_two_separate_tool_rounds_stay_separate():
    """Results answering DIFFERENT assistant turns must not collapse together."""
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "a_0", "name": "one", "arguments": {}}]},
        _tool_msg("one", "{}"),
        {"role": "assistant", "tool_calls": [{"id": "b_0", "name": "two", "arguments": {}}]},
        _tool_msg("two", "{}"),
    ]
    _, contents = _client()._split(messages)
    assert [c.role for c in contents] == ["model", "user", "model", "user"]
