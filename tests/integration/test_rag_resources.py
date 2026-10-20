"""rag-mcp Resource. 실행 중인 PostgreSQL과 시드 데이터가 필요하다. 임베딩(OpenAI)은 부르지 않는다."""

import json

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from sqlalchemy import select, text

from rework.config import get_settings
from rework.db.models import DocumentChunk
from rework.db.session import get_engine, session_scope

try:
    with get_engine().connect() as conn:
        conn.execute(text("select 1 from document_chunks where tenant_id='demo-store' limit 1")).one()
except Exception:  # noqa: BLE001
    pytest.skip("DB가 없거나 시드되지 않았습니다.", allow_module_level=True)

from rework.rag_mcp import server as rag  # noqa: E402


@pytest.fixture
def tenant(monkeypatch):
    """stdio 방식처럼 환경 변수로 회사를 정한다."""
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    get_settings.cache_clear()

    def use(tenant_id: str):
        monkeypatch.setenv("REWORK_TENANT_ID", tenant_id)
    yield use
    get_settings.cache_clear()


def _chunk_id() -> str:
    with session_scope() as s:
        return s.scalar(select(DocumentChunk.id).where(DocumentChunk.tenant_id == "demo-store").limit(1))


async def _read(uri: str) -> dict:
    async with Client(rag.server) as client:
        res = await client.read_resource(uri)
    return json.loads(res.contents[0].text)


async def _rejected(uri: str) -> bool:
    """거절 오류는 연결 안에서 확인한다 (연결을 닫으며 다른 예외로 감싸지기 전에)."""
    async with Client(rag.server) as client:
        try:
            await client.read_resource(uri)
        except MCPError:
            return True
    return False


@pytest.mark.asyncio
async def test_templates_are_listed(tenant):
    tenant("demo-store")
    async with Client(rag.server) as client:
        uris = {t.uri_template for t in (await client.list_resource_templates()).resource_templates}
    assert uris == {"doc://{chunk_id}", "knowledge://{kind}"}


@pytest.mark.asyncio
async def test_doc_returns_full_chunk(tenant):
    tenant("demo-store")
    cid = _chunk_id()
    doc = await _read(f"doc://{cid}")
    with session_scope() as s:
        assert doc["excerpt"] == s.get(DocumentChunk, cid).content
    assert doc["chunk_id"] == cid and doc["document_version"] >= 1


@pytest.mark.asyncio
async def test_doc_of_other_tenant_is_hidden(tenant):
    tenant("other-co")
    assert await _rejected(f"doc://{_chunk_id()}")


@pytest.mark.asyncio
async def test_knowledge_lists_latest_versions(tenant):
    tenant("demo-store")
    out = await _read("knowledge://guideline")
    titles = [d["title"] for d in out["documents"]]
    assert "점포 월간 판매 실적 보고서 작성 지침" in titles
    assert len(titles) == len(set(titles))


@pytest.mark.asyncio
async def test_unknown_kind_and_missing_tenant_are_rejected(tenant):
    tenant("demo-store")
    assert await _rejected("knowledge://secret")
    tenant("")
    assert await _rejected("knowledge://guideline")
