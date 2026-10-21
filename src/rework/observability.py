"""관측: 실행 한 번을 trace_id 하나로 묶고, 모델 호출마다 토큰과 비용을 센다.

- trace_id는 Worker가 메시지(시도)마다 만든다. 같은 값이 진행 이벤트, MCP 실행 토큰(→ MCP 서버 로그),
  LangSmith 최상위 trace ID에 들어가므로 어느 한 곳에서 찾으면 나머지를 따라갈 수 있다.
- LangSmith는 선택이다. 키가 없으면 trace_id·토큰·비용은 DB에만 남는다.
"""

import logging
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from rework.config import get_settings

log = logging.getLogger("rework.trace")

# record_event가 읽는다. 호출하는 곳마다 trace_id를 넘기지 않아도 같은 실행의 이벤트가 묶인다.
current_trace: ContextVar[str | None] = ContextVar("current_trace", default=None)


def new_trace_id() -> str:
    return str(uuid.uuid4())


def setup_langsmith() -> bool:
    """설정(.env)의 LangSmith 값을 환경 변수로 넘긴다. LangChain·LangSmith 라이브러리는 환경 변수만 읽는다."""
    cfg = get_settings()
    on = cfg.langsmith_tracing and bool(cfg.langsmith_api_key)
    os.environ["LANGSMITH_TRACING"] = "true" if on else "false"
    if on:
        os.environ["LANGSMITH_API_KEY"] = cfg.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = cfg.langsmith_project
    log.info("LangSmith 추적 %s", f"켜짐 (프로젝트 {cfg.langsmith_project})" if on else "꺼짐")
    return on


def tracing_enabled() -> bool:
    return os.environ.get("LANGSMITH_TRACING", "").lower() == "true"


@contextmanager
def trace_run(ctx, action: str):
    """LangSmith 최상위 trace를 연다. 안에서 실행되는 LangGraph 노드·모델 호출·MCP 호출이 모두 이 아래에 붙는다."""
    from rework.db.session import record_event

    if not tracing_enabled():
        yield
        return
    import langsmith as ls

    with ls.trace(f"rework.{action}", "chain", run_id=ctx.trace_id,
                  inputs={"job_id": ctx.job_id, "run_id": ctx.run_id, "action": action, "attempt": ctx.attempt},
                  metadata={"job_id": ctx.job_id, "run_id": ctx.run_id, "tenant_id": ctx.tenant_id,
                            "thread_id": ctx.run_id, "attempt": ctx.attempt},
                  tags=[action]) as rt:
        try:
            yield
        finally:
            try:
                url = rt.get_url()
            except Exception as e:  # noqa: BLE001 - 링크를 못 만들어도 실행은 계속한다
                log.warning("LangSmith 링크 생성 실패: %s", e)
                url = None
            record_event(ctx.job_id, ctx.run_id, "trace", "worker", "LangSmith trace",
                         {"trace_id": ctx.trace_id, "url": url})


def _price(model: str) -> tuple[float, float] | None:
    """응답의 모델 이름(예: gpt-4o-mini-2024-07-18)에 가장 길게 맞는 가격표 항목. 100만 토큰당 (입력, 출력) USD."""
    prices = get_settings().llm_prices_per_mtok
    keys = sorted((k for k in prices if model.startswith(k)), key=len, reverse=True)
    return tuple(prices[keys[0]]) if keys else None


def usage_record(usage_by_model: dict) -> dict:
    """get_usage_metadata_callback 결과({모델: 사용량})를 이벤트에 남길 형태로 바꾼다."""
    inp = sum(u.get("input_tokens", 0) for u in usage_by_model.values())
    out = sum(u.get("output_tokens", 0) for u in usage_by_model.values())
    cost = 0.0
    for model, u in usage_by_model.items():
        p = _price(model)
        if p is None:
            return {"model": model, "input_tokens": inp, "output_tokens": out, "cost_usd": None}
        cost += (u.get("input_tokens", 0) * p[0] + u.get("output_tokens", 0) * p[1]) / 1_000_000
    return {"model": ", ".join(usage_by_model), "input_tokens": inp, "output_tokens": out, "cost_usd": round(cost, 6)}


def summarize_usage(records: list[dict]) -> dict:
    """llm 이벤트 data 목록을 합친다. 가격을 모르는 모델이 섞이면 비용은 아는 것만 더하고 표시한다."""
    rs = [r for r in records if "input_tokens" in r]
    return {"llm_calls": len(rs),
            "input_tokens": sum(r["input_tokens"] for r in rs),
            "output_tokens": sum(r["output_tokens"] for r in rs),
            "cost_usd": round(sum(r["cost_usd"] or 0 for r in rs), 6),
            "cost_complete": all(r["cost_usd"] is not None for r in rs)}
