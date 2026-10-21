import json

import pytest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from rework.agent import budget as B
from rework.agent import policy
from rework.privacy import mask, mask_messages, mask_obj


# ---------- 변경 도구 승인 ----------

def test_read_tools_pass_anywhere():
    assert policy.check("business", "analyze_trend", {}, "investigate") is None


def test_write_tool_blocked_outside_its_node():
    # 모델이 고르는 조사 단계에서 저장 도구를 부르면 막는다.
    assert "save_results" in policy.check("business", "save_rule_version", {}, "investigate")


def test_write_tool_allowed_in_its_node():
    assert policy.check("business", "save_rule_version", {}, "save_results") is None


def test_confirmed_knowledge_needs_approval():
    assert "승인" in policy.check("rag", "register_confirmed_rule", {"title": "t", "content": "c"}, "save_results")
    assert "승인" in policy.check("rag", "register_confirmed_rule", {"answer_id": " "}, "save_results")
    assert policy.check("rag", "register_confirmed_rule", {"answer_id": "a_1"}, "save_results") is None


def test_unknown_and_human_only_tools_blocked():
    assert "허용 목록" in policy.check("business", "drop_table", {}, "save_results")
    assert "허용 목록" in policy.check("rag", "register_document", {}, "save_results")


# ---------- 개인정보 마스킹 ----------

def test_masks_contacts_and_names():
    text = "작성자: 김민수 / 문의 010-1234-5678, minsu@store.co.kr / 박지훈 과장 확인"
    out = mask(text)
    for leaked in ("김민수", "010-1234-5678", "minsu@store.co.kr", "박지훈"):
        assert leaked not in out
    assert "[전화번호]" in out and "[이메일]" in out and out.count("[이름]") == 2


def test_keeps_business_words_and_numbers():
    # 상품명·바코드·날짜·금액·일반 단어는 가리지 않는다.
    text = "2026-06-01 단팥빵 gtin 8801234567890 매출 1,234,000원, 대표 상품, 담당자 확인, 고객님 문의, 과장된 표현"
    assert mask(text) == text


def test_mask_obj_and_messages_keep_structure():
    assert mask_obj({"a": ["010-1111-2222", 3]}) == {"a": ["[전화번호]", 3]}
    msgs = [SystemMessage("규칙"), HumanMessage("결재: 이영희"),
            AIMessage(content="", tool_calls=[{"name": "rank_items", "args": {}, "id": "c1"}])]
    out, n = mask_messages(msgs)
    assert n == 1 and out[1].content == "결재: [이름]" and out[0] is msgs[0] and out[2].tool_calls == msgs[2].tool_calls


# ---------- 컨텍스트 예산 ----------

def _tool(rid: str, size: int) -> ToolMessage:
    return ToolMessage(content=json.dumps({"result_id": rid, "rows": "가" * size}, ensure_ascii=False), tool_call_id=rid)


def test_clip_long_tool_result():
    text = json.dumps({"result_id": "r1", "rows": "x" * 500})
    out, clipped = B.clip(text, 100)
    d = json.loads(out)
    assert clipped and d["result_id"] == "r1" and d["truncated"] and len(d["preview"]) == 100
    assert B.clip("short", 100) == ("short", False)


def test_fit_drops_oldest_tool_results_first():
    msgs = [SystemMessage("s"), HumanMessage("q"), _tool("r1", 3000), _tool("r2", 3000), _tool("r3", 3000), _tool("r4", 50)]
    before = B.count_tokens(msgs)
    out, b, a = B.fit(msgs, budget=before - 1000)
    assert b == before and a < b
    assert "omitted" in out[2].content and json.loads(out[2].content)["result_id"] == "r1"
    assert out[-1].content == msgs[-1].content and out[-2].content == msgs[-2].content  # 최근 2개는 그대로


def test_fit_under_budget_is_untouched():
    msgs = [HumanMessage("짧은 질문")]
    out, b, a = B.fit(msgs, budget=10_000)
    assert out is msgs and a == b


# ---------- 모델 응답 하드 제한 ----------

@pytest.mark.asyncio
async def test_hard_limit_returns_even_if_call_ignores_cancel():
    import asyncio
    import time

    from rework.agent.limits import within as _within

    async def stubborn():  # 취소를 무시하고 계속 기다리는 HTTP 호출 흉내
        for _ in range(50):
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                continue
        return "late"

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await _within(stubborn(), 0.2)
    assert time.monotonic() - started < 1
    assert await _within(asyncio.sleep(0, result="ok"), 1) == "ok"


def test_name_before_title_with_particle():
    assert mask("박서준 대리가 정리") == "[이름] 대리가 정리"
    assert mask("김민수 팀장님께 보고") == "[이름] 팀장님께 보고"
    assert mask("과장된 표현") == "과장된 표현"


def test_numbers_followed_by_korean_particle():
    # 품질 측정(Q-12)에서 찾은 결함: 조사가 붙으면 가려지지 않았다
    assert mask("문의는 010-1000-2000로 주세요") == "문의는 [전화번호]로 주세요"
    assert mask("카드 4111-1111-2222-3333으로 결제") == "카드 [카드번호]으로 결제"
    assert mask("900101-1234567은 주민번호") == "[주민번호]은 주민번호"


@pytest.mark.asyncio
async def test_close_error_does_not_hide_transient_error():
    # 품질 측정(Q-06)에서 찾은 결함: MCP 서버가 내려가면 연결 정리의 ExceptionGroup이 TransientError를 덮어써 재시도가 안 됐다
    from rework.agent.errors import TransientError
    from rework.agent.mcp_client import RunContext, ToolHub

    hub = ToolHub(RunContext(job_id="j", run_id="r", tenant_id="t"))

    async def broken_close():
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("Name or service not known")])
    hub._stack.aclose = broken_close
    original = TransientError("도구 호출 실패")
    assert await hub.__aexit__(TransientError, original, None) is False  # 원래 오류가 그대로 올라간다
    with pytest.raises(TransientError, match="연결 정리 실패"):
        await hub.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_cancel_inside_call_becomes_transient():
    # 품질 측정(Q-06)에서 찾은 결함: MCP 서버가 사라지면 라이브러리가 호출을 취소하고, CancelledError가 Worker를 종료시켰다
    import asyncio

    from rework.agent.errors import TransientError
    from rework.agent.limits import within

    async def cancelled_inside():
        raise asyncio.CancelledError("Cancelled via cancel scope")

    with pytest.raises(TransientError):
        await within(cancelled_inside(), 5)
