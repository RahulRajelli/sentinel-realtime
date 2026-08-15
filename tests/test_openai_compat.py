"""Message translation for the OpenAI-compatible client. Offline, no SDK, no key.

This client is the path to a SECOND model, which is the open question that matters most here:
is "the agent reads detection order as causality" a property of agents, or of gemini-2.5-flash?
One adapter reaches GPT, Grok, OpenRouter and local servers, so the answer is a key away rather
than a rewrite away.

Translation is tested rather than trusted because the two sibling adapters each shipped with a
bug in exactly this layer: gemini.py failed to merge parallel tool results (400 on every batched
turn, 16 of 27 judgements degraded), and llm.py had to learn the same lesson first. The failure
mode is silent-ish and expensive, so it gets tests before it gets a live run.
"""

from __future__ import annotations

import json

from sentinel.judges.openai_compat import OpenAICompatClient as C


def test_system_and_user_turns_pass_through():
    out = C._to_openai_messages([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "summary"},
    ])
    assert out == [{"role": "system", "content": "sys"},
                   {"role": "user", "content": "summary"}]


def test_tool_calls_are_nested_under_function_with_json_arguments():
    out = C._to_openai_messages([
        {"role": "assistant", "tool_calls": [
            {"id": "a1", "name": "evidence_untimed",
             "arguments": {"incident_type": "compass_inconsistency"}}]},
    ])
    tc = out[0]["tool_calls"][0]
    assert tc["type"] == "function" and tc["id"] == "a1"
    assert tc["function"]["name"] == "evidence_untimed"
    # arguments must be a STRING here, unlike every other adapter in this package
    assert isinstance(tc["function"]["arguments"], str)
    assert json.loads(tc["function"]["arguments"]) == {"incident_type": "compass_inconsistency"}


def test_parallel_tool_results_stay_separate():
    """The opposite of gemini.py, and deliberately so.

    Gemini needs one Content carrying N function_response parts. This API keys each result by
    `tool_call_id`, so merging them would be wrong -- the ids are what pair result to call.
    """
    out = C._to_openai_messages([
        {"role": "assistant", "tool_calls": [
            {"id": "a1", "name": "one", "arguments": {}},
            {"id": "b2", "name": "two", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "a1", "name": "one", "content": "{}"},
        {"role": "tool", "tool_call_id": "b2", "name": "two", "content": "{}"},
    ])
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert [m["tool_call_id"] for m in tool_msgs] == ["a1", "b2"]


def test_malformed_tool_arguments_become_empty_rather_than_raising():
    """A bad JSON blob costs one turn the model can correct, not the whole judgement."""
    assert C._parse_arguments("{not json") == {}
    assert C._parse_arguments(None) == {}
    assert C._parse_arguments("[1,2]") == {}          # valid JSON, wrong shape
    assert C._parse_arguments('{"a": 1}') == {"a": 1}
    assert C._parse_arguments({"a": 1}) == {"a": 1}   # already parsed


def test_tool_spec_shape():
    spec = C._tool_spec({"name": "get_param", "description": "d",
                         "parameters": {"type": "object", "properties": {"name": {}}}})
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "get_param"
    assert spec["function"]["parameters"]["type"] == "object"


def test_parameterless_tool_gets_an_object_schema():
    """Unlike Gemini, which rejects an empty object schema and needs the key omitted."""
    spec = C._tool_spec({"name": "list_advisories", "description": "d", "parameters": {}})
    assert spec["function"]["parameters"] == {"type": "object", "properties": {}}


class _FakeFn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _FakeCall:
    def __init__(self, id, fn):
        self.id, self.function = id, fn


class _FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _FakeResp:
    def __init__(self, msg, usage=None):
        self.choices = [type("Ch", (), {"message": msg})()]
        self.usage = usage


class _Usage:
    def __init__(self, p, c):
        self.prompt_tokens, self.completion_tokens = p, c


def test_response_with_text_only():
    r = C._to_model_response(_FakeResp(_FakeMsg(content='{"root_cause": null}'), _Usage(10, 5)))
    assert r.text == '{"root_cause": null}'
    assert not r.wants_tools
    assert (r.tokens_in, r.tokens_out) == (10, 5)


def test_response_with_tool_calls():
    r = C._to_model_response(_FakeResp(_FakeMsg(tool_calls=[
        _FakeCall("c1", _FakeFn("evidence_untimed", '{"incident_type": "x"}'))]), _Usage(7, 2)))
    assert r.wants_tools
    assert r.tool_calls[0].name == "evidence_untimed"
    assert r.tool_calls[0].arguments == {"incident_type": "x"}


def test_missing_usage_does_not_crash():
    """Some OpenAI-compatible servers omit usage entirely; a sweep must not die on it."""
    r = C._to_model_response(_FakeResp(_FakeMsg(content="hi"), usage=None))
    assert (r.tokens_in, r.tokens_out) == (0, 0)


def test_reasoning_models_are_detected_for_parameter_suppression():
    """temperature/seed are rejected outright by several reasoning endpoints (see llm.py)."""
    from sentinel.judges.openai_compat import _NO_SAMPLING_PARAMS
    assert any(m in "o3-mini-2025-01-31" for m in _NO_SAMPLING_PARAMS)
    assert any(m in "grok-4.6-reasoning" for m in _NO_SAMPLING_PARAMS)
    assert not any(m in "gpt-5.6" for m in _NO_SAMPLING_PARAMS)
