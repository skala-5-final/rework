from datetime import UTC, datetime, timedelta

import jwt
import pytest
from mcp import Client

from rework.auth.mcp_scope import AUDIENCE, Scope, ScopeError, issue_token, resolve_scope
from rework.business_mcp import server as business
from rework.config import get_settings
from rework.storage import resolve, to_ref


@pytest.fixture
def http_mode(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_TOKEN_SECRET", "test-secret-0123456789abcdefghijklmnop")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stdio_mode(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


def test_signed_token_gives_scope(http_mode):
    assert resolve_scope(_bearer(issue_token("t1", "run1"))) == Scope("t1", "run1")


@pytest.mark.parametrize("headers", [None, {}, {"authorization": "t1"}, {"x-tenant-id": "t1"}])
def test_http_without_token_is_rejected(http_mode, headers):
    with pytest.raises(ScopeError):
        resolve_scope(headers)


def test_http_ignores_environment(http_mode, monkeypatch):
    monkeypatch.setenv("REWORK_TENANT_ID", "t1")
    with pytest.raises(ScopeError):
        resolve_scope(None)


@pytest.mark.parametrize("secret, aud, exp", [
    ("wrong-secret-0123456789abcdefghijklmnop", AUDIENCE, timedelta(minutes=5)),
    ("test-secret-0123456789abcdefghijklmnop", "other-service", timedelta(minutes=5)),
    ("test-secret-0123456789abcdefghijklmnop", AUDIENCE, timedelta(minutes=-1)),
])
def test_forged_or_expired_token_is_rejected(http_mode, secret, aud, exp):
    token = jwt.encode({"tenant": "t1", "run": "r", "aud": aud, "exp": datetime.now(UTC) + exp}, secret, algorithm="HS256")
    with pytest.raises(ScopeError):
        resolve_scope(_bearer(token))


def test_stdio_uses_environment(stdio_mode, monkeypatch):
    monkeypatch.setenv("REWORK_TENANT_ID", "t1")
    monkeypatch.setenv("REWORK_RUN_ID", "run1")
    assert resolve_scope(None) == Scope("t1", "run1")


def test_stdio_without_tenant_is_rejected(stdio_mode, monkeypatch):
    monkeypatch.delenv("REWORK_TENANT_ID", raising=False)
    with pytest.raises(ScopeError):
        resolve_scope(None)


def test_calculation_is_not_visible_to_other_run():
    calc_id = business._put_calc(Scope("t1", "run1"), {"slots": {}, "results": []})
    assert business._get_calc(Scope("t1", "run1"), calc_id) is not None
    assert business._get_calc(Scope("t2", "run1"), calc_id) is None
    assert business._get_calc(Scope("t1", "run2"), calc_id) is None


def test_old_calculations_are_dropped(monkeypatch):
    old = business._put_calc(Scope("t1", "run1"), {"slots": {}, "results": []})
    business._calculations[old]["at"] -= business.CALC_TTL_SECONDS + 1
    business._put_calc(Scope("t1", "run1"), {"slots": {}, "results": []})
    assert old not in business._calculations


@pytest.mark.asyncio
async def test_tool_fails_closed_without_token(http_mode):
    async with Client(business.server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "ctx" not in tools["analyze_trend"].input_schema["properties"]
        res = await client.call_tool("inspect_dataset", {"dataset_id": "file_x"})
    assert res.is_error
    assert "실행 토큰" in "".join(getattr(c, "text", "") for c in res.content)


def test_storage_ref_is_relative_and_legacy_absolute_paths_resolve():
    base = get_settings().storage_dir
    assert to_ref(base / "t1" / "f.csv") == "t1/f.csv"
    assert resolve("t1/f.csv") == base / "t1" / "f.csv"
    assert resolve("/Users/someone/project/var/storage/t1/f.csv") == base / "t1" / "f.csv"
