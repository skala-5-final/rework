"""rag-mcp: 업무 규정·데이터 사전·확인된 담당자 답변을 검색하고 출처를 반환한다."""

import os
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from rework.db.session import session_scope
from rework.rag_mcp.services import index

TENANT_ID = os.environ.get("REWORK_TENANT_ID", "")

server = MCPServer(
    "rag-mcp",
    instructions="회사별 업무 지식을 검색한다. 결과가 없으면 빈 목록을 돌려준다. 검색 점수는 업무 규칙의 확정 근거가 아니다.",
)


@server.tool()
def search_work_rules(query: Annotated[str, Field(description="찾을 내용 (예: 매출액 정의, 금액 표기 단위)")],
                      top_k: Annotated[int, Field(ge=1, le=8)] = 4) -> dict:
    """보고서 작성 지침·데이터 사전·확인된 질문 답변에서 관련 내용을 검색한다. 결과마다 document_id, document_version, chunk_id, title, excerpt, source_location을 준다. 관련 내용이 없으면 results가 빈 목록이다."""
    try:
        with session_scope() as s:
            results = index.search(s, tenant_id=TENANT_ID, query=query, top_k=top_k)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"검색 실패: {e}") from e
    return {"query": query, "results": results, "found": bool(results)}


@server.tool()
def get_source_excerpt(chunk_id: Annotated[str, Field(description="search_work_rules가 돌려준 chunk_id")]) -> dict:
    """문서 조각의 원문 전체와 문서 버전을 조회한다."""
    try:
        with session_scope() as s:
            return index.excerpt(s, tenant_id=TENANT_ID, chunk_id=chunk_id)
    except LookupError as e:
        raise ToolError(str(e)) from e


@server.tool()
def register_document(title: Annotated[str, Field(description="문서 제목")],
                      content: Annotated[str, Field(description="Markdown 본문")],
                      kind: Annotated[str, Field(description="guideline | dictionary")] = "guideline") -> dict:
    """업무 문서를 등록하고 제목 단위로 나눠 색인한다. 같은 제목이면 새 버전으로 저장한다."""
    with session_scope() as s:
        return index.register_document(s, tenant_id=TENANT_ID, title=title, content=content, kind=kind)


@server.tool()
def register_confirmed_rule(title: Annotated[str, Field(description="지식 제목 (예: 행사 품목 선정 기준)")],
                            content: Annotated[str, Field(description="확인된 규칙 내용과 확인 이력")],
                            answer_id: Annotated[str, Field(description="이 규칙을 확인한 답변 ID")]) -> dict:
    """담당자가 확인한 규칙을 다음 실행에서 검색할 수 있게 등록한다. 확인 답변 ID가 없으면 등록하지 않는다."""
    from rework.db.models import Answer, Question

    with session_scope() as s:
        a = s.get(Answer, answer_id)
        q = s.get(Question, a.question_id) if a else None
        if a is None or q is None or q.tenant_id != TENANT_ID:
            raise ToolError("확인 답변이 없어 등록하지 않았습니다. 확인되지 않은 추론은 업무 지식으로 등록할 수 없습니다.")
        return index.register_document(s, tenant_id=TENANT_ID, title=title,
                                       content=f"{content}\n\n(확인: 답변 {answer_id})", kind="confirmed")


if __name__ == "__main__":
    server.run()
