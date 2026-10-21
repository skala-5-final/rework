"""rag-mcp: 업무 규정·데이터 사전·확인된 담당자 답변을 검색하고 출처를 반환한다."""

import logging
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from pydantic import Field

from rework.auth.mcp_scope import ScopeError, resolve_scope
from rework.config import get_settings
from rework.db.session import session_scope
from rework.rag_mcp.services import index

server = MCPServer(
    "rag-mcp",
    instructions="회사별 업무 지식을 검색한다. 결과가 없으면 빈 목록을 돌려준다. 검색 점수는 업무 규칙의 확정 근거가 아니다.",
)


def _tenant(ctx: Context, error: type[Exception] = ToolError) -> str:
    """요청마다 회사를 정한다 (stdio: 환경 변수, HTTP: 서명된 실행 토큰)."""
    try:
        return resolve_scope(ctx.headers).tenant_id
    except ScopeError as e:
        raise error(str(e)) from e


# ---------- Resource: 읽기 전용 자료. 모델이 고르는 도구와 달리 Agent(앱)가 골라 컨텍스트에 넣는다 ----------

@server.resource("doc://{chunk_id}", name="문서 조각", mime_type="application/json",
                 description="업무 문서 조각의 원문 전체, 문서 버전, 출처 위치. search_work_rules 결과의 chunk_id로 읽는다.")
def doc_chunk(chunk_id: str, ctx: Context) -> dict:
    tenant_id = _tenant(ctx, ResourceError)
    try:
        with session_scope() as s:
            return index.excerpt(s, tenant_id=tenant_id, chunk_id=chunk_id)
    except LookupError as e:
        raise ResourceError(str(e)) from e


@server.resource("knowledge://{kind}", name="업무 지식 목록", mime_type="application/json",
                 description="회사에 등록된 업무 지식 목록 (kind: guideline | dictionary | confirmed). 제목마다 최신 버전만.")
def knowledge_list(kind: str, ctx: Context) -> dict:
    tenant_id = _tenant(ctx, ResourceError)
    try:
        with session_scope() as s:
            return {"kind": kind, "documents": index.list_documents(s, tenant_id=tenant_id, kind=kind)}
    except LookupError as e:
        raise ResourceError(str(e)) from e


# ---------- Tool ----------


@server.tool()
def search_work_rules(ctx: Context, query: Annotated[str, Field(description="찾을 내용 (예: 매출액 정의, 금액 표기 단위)")],
                      top_k: Annotated[int, Field(ge=1, le=8)] = 4) -> dict:
    """보고서 작성 지침·데이터 사전·확인된 질문 답변에서 관련 내용을 검색한다. 결과마다 document_id, document_version, chunk_id, title, excerpt, source_location을 준다. 관련 내용이 없으면 results가 빈 목록이다."""
    tenant_id = _tenant(ctx)
    try:
        with session_scope() as s:
            results = index.search(s, tenant_id=tenant_id, query=query, top_k=top_k)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"검색 실패: {e}") from e
    return {"query": query, "results": results, "found": bool(results)}


@server.tool()
def get_source_excerpt(ctx: Context, chunk_id: Annotated[str, Field(description="search_work_rules가 돌려준 chunk_id")]) -> dict:
    """문서 조각의 원문 전체와 문서 버전을 조회한다."""
    tenant_id = _tenant(ctx)
    try:
        with session_scope() as s:
            return index.excerpt(s, tenant_id=tenant_id, chunk_id=chunk_id)
    except LookupError as e:
        raise ToolError(str(e)) from e


@server.tool()
def register_document(ctx: Context, title: Annotated[str, Field(description="문서 제목")],
                      content: Annotated[str, Field(description="Markdown 본문")],
                      kind: Annotated[str, Field(description="guideline | dictionary")] = "guideline") -> dict:
    """업무 문서를 등록하고 제목 단위로 나눠 색인한다. 같은 제목이면 새 버전으로 저장한다."""
    tenant_id = _tenant(ctx)
    with session_scope() as s:
        return index.register_document(s, tenant_id=tenant_id, title=title, content=content, kind=kind)


@server.tool()
def register_confirmed_rule(ctx: Context, title: Annotated[str, Field(description="지식 제목 (예: 행사 품목 선정 기준)")],
                            content: Annotated[str, Field(description="확인된 규칙 내용과 확인 이력")],
                            answer_id: Annotated[str, Field(description="이 규칙을 확인한 답변 ID")]) -> dict:
    """담당자가 확인한 규칙을 다음 실행에서 검색할 수 있게 등록한다. 확인 답변 ID가 없으면 등록하지 않는다."""
    from rework.db.models import Answer, Question

    try:
        sc = resolve_scope(ctx.headers)
    except ScopeError as e:
        raise ToolError(str(e)) from e
    with session_scope() as s:
        a = s.get(Answer, answer_id)
        q = s.get(Question, a.question_id) if a else None
        if a is None or q is None or q.tenant_id != sc.tenant_id:
            raise ToolError("확인 답변이 없어 등록하지 않았습니다. 확인되지 않은 추론은 업무 지식으로 등록할 수 없습니다.")
        # 승인은 이 실행에서 받은, 반려가 아닌 답변만 인정한다.
        if sc.run_id and q.run_id != sc.run_id:
            raise ToolError("다른 실행의 답변으로는 등록할 수 없습니다.")
        if a.choice == "reject":
            raise ToolError("담당자가 반려한 내용은 업무 지식으로 등록하지 않습니다.")
        tenant_id = sc.tenant_id
        return index.register_document(s, tenant_id=tenant_id, title=title,
                                       content=f"{content}\n\n(확인: 답변 {answer_id})", kind="confirmed")


if __name__ == "__main__":
    # 로그는 stderr로 (stdio에서는 stdout이 MCP 프로토콜 통로). 요청마다 trace_id가 남는다.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = get_settings()
    if cfg.mcp_transport == "http":
        server.run(transport="streamable-http", host="0.0.0.0", port=cfg.rag_mcp_port)
    else:
        server.run()
