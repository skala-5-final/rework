import pytest

from rework.auth.mcp_scope import Scope, issue_token, resolve_scope
from rework.config import get_settings
from rework.observability import current_trace, setup_langsmith, summarize_usage, usage_record


@pytest.fixture
def http_mode(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_TOKEN_SECRET", "test-secret-0123456789abcdefghijklmnop")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cost_uses_longest_matching_price():
    # gpt-4o-mini-...는 gpt-4o가 아니라 gpt-4o-mini 가격을 써야 한다.
    r = usage_record({"gpt-4o-mini-2024-07-18": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}})
    assert r == {"model": "gpt-4o-mini-2024-07-18", "input_tokens": 1_000_000, "output_tokens": 1_000_000,
                 "cost_usd": 0.75}


def test_unknown_model_keeps_tokens_without_cost():
    r = usage_record({"some-new-model": {"input_tokens": 10, "output_tokens": 5}})
    assert (r["input_tokens"], r["output_tokens"], r["cost_usd"]) == (10, 5, None)


def test_summary_marks_incomplete_cost():
    s = summarize_usage([{"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.001},
                         {"input_tokens": 50, "output_tokens": 10, "cost_usd": None},
                         {"something": "else"}])
    assert s == {"llm_calls": 2, "input_tokens": 150, "output_tokens": 30, "cost_usd": 0.001, "cost_complete": False}


def test_trace_id_travels_in_mcp_token(http_mode):
    tok = issue_token("t1", "run1", "trace-123")
    assert resolve_scope({"authorization": f"Bearer {tok}"}) == Scope("t1", "run1", "trace-123")


def test_record_event_reads_current_trace():
    token = current_trace.set("trace-abc")
    try:
        assert current_trace.get() == "trace-abc"
    finally:
        current_trace.reset(token)
    assert current_trace.get() is None


def test_langsmith_stays_off_without_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    get_settings.cache_clear()
    try:
        assert setup_langsmith() is False
    finally:
        get_settings.cache_clear()
