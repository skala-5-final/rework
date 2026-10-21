"""runs.py로 실행한 작업들에서 Q-04 · H-01 · H-02 · H-03 · H-05를 계산한다.

    uv run python scripts/quality/run_metrics.py [var/quality/runs.json]

- Q-04 근거 없는 수치 주장: 판정 근거 문장의 숫자를 그 제안의 도구 결과·보고서 수치와 대조 (반올림 허용).
- H-01 Trace 수집률: 시도(trace_id)마다 진행 이벤트 · MCP 서버 로그 · LangSmith 세 곳이 같은 ID로 이어지는지.
- H-02 토큰·비용 관측률: 모델 호출 이벤트마다 입력·출력 토큰과 비용이 있는지.
- H-03 실행 제어: 허용 목록 밖 도구 0건, 제안당·실행당 호출 한도, 시도 실행시간 한도 준수.
- H-05 불확실한 판단 처리율: 데이터로 확정하지 못한 결정이 질문 또는 미해결로 분류되고, 답변 없이 '확인됨'이 되지 않는지.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import httpx
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from common import OUT, ROOT, save, verdict
from rework.agent.graph import build_graph
from rework.agent.policy import TOOL_POLICY
from rework.config import get_settings
from rework.db.models import Event, Job, Rationale
from rework.db.session import session_scope

API_STAGES = {"queued", "answers", "rerun"}  # API가 직접 남기는 이벤트. Worker 시도 밖이다
NUM = re.compile(r"(?<![A-Za-z0-9_.])[-+−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![A-Za-z0-9_.])[-+−]?\d+(?:\.\d+)?")


async def _state(run_id: str) -> dict:
    async with AsyncPostgresSaver.from_conn_string(get_settings().checkpoint_url) as saver:
        return (await build_graph(saver).aget_state({"configurable": {"thread_id": run_id}})).values


def _leaves(x, out: list[float]) -> list[float]:
    if isinstance(x, bool):
        return out
    if isinstance(x, (int, float)):
        out.append(float(x))
    elif isinstance(x, dict):
        for v in x.values():
            _leaves(v, out)
    elif isinstance(x, list):
        for v in x:
            _leaves(v, out)
    elif isinstance(x, str):
        out += [_f(m) for m in NUM.findall(x)]
    return out


def _f(s: str) -> float:
    return float(s.replace(",", "").replace("−", "-").lstrip("+"))


def _numbers(text: str) -> list[tuple[str, float, int]]:
    """주장 속 수치. 날짜·월·연도·회차처럼 기간을 나타내는 숫자는 뺀다."""
    out = []
    for m in NUM.finditer(text):
        tail = text[m.end():m.end() + 2]
        if tail[:1] in ("월", "년") or tail.startswith("개월") or re.match(r"\d{4}-\d{2}", text[m.start():m.start() + 7]):
            continue
        raw = m.group()
        dec = len(raw.split(".")[1]) if "." in raw else 0
        out.append((raw, _f(raw), dec))
    return out


def _grounded(x: float, dec: int, pool: list[float]) -> bool:
    tol = 0.5 * 10 ** -dec + 1e-9
    for v in pool:
        for cand in (v, v * 100, abs(v), abs(v) * 100, v / 1000):   # 비율→%, 부호 없는 표현, 원→천원
            if abs(abs(x) - abs(cand)) <= tol:
                return True
    return False


def q04(states: dict[str, dict]) -> dict:
    total, bad = 0, []
    for job_id, st in states.items():
        report_pool = _leaves(st.get("report", {}), [])
        for r in st.get("rationales", []):
            pool = report_pool + _leaves([t["result"] for t in st["tool_results"].values() if t["proposal"] == r["key"]], [])
            pool += _leaves(r.get("proposal_text", ""), [])
            for c in r["claims"]:
                texts = list(c.get("evidence") or []) + [(c.get("criterion") or {}).get("description") or ""]
                for t in texts:
                    for raw, x, dec in _numbers(t):
                        total += 1
                        if not _grounded(x, dec, pool):
                            bad.append({"job_id": job_id, "claim": c["claim_id"], "number": raw, "text": t[:160]})
    rate = len(bad) / total * 100 if total else 0.0
    return {"verdict": verdict(total > 0 and not bad), "summary": f"수치 주장 {total}개 중 근거 없음 {len(bad)}개 ({rate:.2f}%)",
            "target": "0%", "numeric_claims": total, "ungrounded": len(bad), "ungrounded_rate_pct": round(rate, 2),
            "examples": bad[:20]}


def _events(job_ids: list[str]) -> dict[str, list[Event]]:
    with session_scope() as s:
        rows = s.scalars(select(Event).where(Event.job_id.in_(job_ids)).order_by(Event.id)).all()
    by = defaultdict(list)
    for e in rows:
        by[e.job_id].append(e)
    return by


def _langsmith_key() -> str:
    key = os.environ.get("LANGSMITH_API_KEY") or get_settings().langsmith_api_key
    if not key and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("LANGSMITH_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
    return key


def h01(events: dict[str, list[Event]]) -> dict:
    logs = subprocess.run(["docker", "compose", "logs", "--no-log-prefix", "business-mcp", "rag-mcp"],
                          cwd=ROOT, capture_output=True, text=True).stdout
    archived = OUT / "mcp_server_logs.txt"  # 컨테이너를 다시 만들기 전에 보관한 로그
    if archived.exists():
        logs += archived.read_text()
    key = _langsmith_key()
    http = httpx.Client(base_url="https://api.smith.langchain.com", headers={"x-api-key": key}, timeout=60)
    attempts, failures, queue_only = [], [], 0
    for job_id, evs in events.items():
        worker = [e for e in evs if e.stage not in API_STAGES]
        for tid in sorted({e.trace_id for e in worker if e.trace_id}):
            mine = [e for e in worker if e.trace_id == tid]
            kinds = Counter(e.type for e in mine)
            if not any(e.message.startswith("작업 시작") for e in mine):
                queue_only += 1  # 중복 무시·lease 대기처럼 실행 없이 메시지만 처리한 기록. 노드·MCP·모델 호출이 없다
                continue
            in_mcp = f"trace={tid}" in logs
            ls_types = Counter()
            if key:
                for wait in (0, 2, 5, 10):  # 조회 API의 일시 거절(요청 제한)과 수집 지연을 기다린다
                    time.sleep(wait)
                    try:
                        r = http.post("/api/v1/runs/query", json={"trace": tid, "select": ["run_type"], "limit": 100})
                    except httpx.HTTPError:
                        continue
                    if r.status_code == 200 and r.json().get("runs"):
                        ls_types = Counter(x["run_type"] for x in r.json()["runs"])
                        break
            need_llm, need_tool = kinds["llm"] > 0, kinds["tool_call"] > 0
            # MCP 서버 로그는 도구를 부른 시도에만 남는다 (연결·도구 목록 조회는 요청별 인증을 거치지 않는다)
            ok = (in_mcp or not need_tool) and bool(ls_types) and (not need_llm or ls_types["llm"] > 0) \
                and (not need_tool or ls_types["tool"] > 0)
            attempts.append({"job_id": job_id, "trace_id": tid, "events": dict(kinds), "mcp_log": in_mcp,
                             "langsmith": dict(ls_types), "ok": ok})
            if not ok:
                failures.append(attempts[-1])
        # trace_id 없는 Worker 이벤트 (API가 남기는 작업 등록·답변 제출은 Worker 밖이라 제외)
        for e in worker:
            if not e.trace_id:
                failures.append({"job_id": job_id, "event_without_trace": f"{e.type}:{e.message[:60]}"})
    n_ok = sum(a["ok"] for a in attempts)
    untraced = [f for f in failures if "event_without_trace" in f]
    rate = n_ok / len(attempts) * 100 if attempts else 0
    return {"verdict": verdict(bool(attempts) and n_ok == len(attempts) and not untraced),
            "summary": f"시도 {len(attempts)}개 중 세 곳 연결 {n_ok}개 ({rate:.1f}%), trace_id 없는 Worker 이벤트 {len(untraced)}개",
            "target": "100%", "attempts": len(attempts), "connected": n_ok, "rate_pct": round(rate, 1),
            "untraced_worker_events": len(untraced), "queue_only_traces": queue_only, "failures": failures[:40], "langsmith_checked": bool(key)}


def h02(events: dict[str, list[Event]]) -> dict:
    calls = [e for evs in events.values() for e in evs if e.type == "llm"]
    # 수정 전 코드는 형식 오류로 재요청한 호출을 llm 이벤트(토큰 없음)로만 남겼다 → 관측 실패로 센다.
    ok = [e for e in calls if e.data.get("input_tokens") is not None and e.data.get("cost_usd") is not None
          and not e.data.get("usage_unknown")]
    missing = [{"job_id": e.job_id, "stage": e.stage, "message": e.message[:80]} for e in calls if e not in ok]
    rate = len(ok) / len(calls) * 100 if calls else 0
    return {"verdict": verdict(bool(calls) and len(ok) == len(calls)),
            "summary": f"모델 호출 {len(calls)}회 중 토큰·비용 기록 {len(ok)}회 ({rate:.1f}%)",
            "target": "100%", "llm_calls": len(calls), "observed": len(ok), "rate_pct": round(rate, 1),
            "input_tokens": sum(e.data.get("input_tokens", 0) for e in ok),
            "output_tokens": sum(e.data.get("output_tokens", 0) for e in ok),
            "cost_usd": round(sum(e.data.get("cost_usd") or 0 for e in ok), 4), "missing": missing[:20]}


def h03(events: dict[str, list[Event]], states: dict[str, dict]) -> dict:
    cfg = get_settings()
    allowed = {f"{s}.{t}" for s, t in TOOL_POLICY} | {"rag.read_resource"}
    violations = []
    for job_id, evs in events.items():
        tools = [e for e in evs if e.type == "tool_call"]
        bad = [e.message for e in tools if e.message not in allowed]
        if bad:
            violations.append({"job_id": job_id, "not_allowed": bad})
        for tid in {e.trace_id for e in evs if e.trace_id}:
            n = sum(1 for e in tools if e.trace_id == tid)
            if n > cfg.run_max_tool_calls:
                violations.append({"job_id": job_id, "trace_id": tid, "tool_calls": n})
            ts = [e.ts for e in evs if e.trace_id == tid]
            dur = (max(ts) - min(ts)).total_seconds()
            if dur > cfg.run_max_seconds:
                violations.append({"job_id": job_id, "trace_id": tid, "seconds": dur})
        per_prop = Counter(t["proposal"] for t in states.get(job_id, {}).get("tool_results", {}).values())
        over = {k: v for k, v in per_prop.items() if v > cfg.investigate_max_tool_calls}
        if over:
            violations.append({"job_id": job_id, "per_proposal": over})
    return {"verdict": verdict(not violations),
            "summary": f"작업 {len(events)}개에서 허용 목록·호출 한도·실행시간 한도 위반 {len(violations)}건",
            "limits": {"investigate_max_tool_calls": cfg.investigate_max_tool_calls, "run_max_tool_calls": cfg.run_max_tool_calls,
                       "run_soft_seconds": cfg.run_soft_seconds, "run_max_seconds": cfg.run_max_seconds},
            "violations": violations[:20]}


def h05(states: dict[str, dict], job_ids: list[str]) -> dict:
    with session_scope() as s:
        saved = s.execute(select(Job.id, Rationale.data).join(Rationale, Rationale.run_id == Job.current_run_id)
                          .where(Job.id.in_(job_ids))).all()
    confirmed_wo_answer = [(jid, c["claim_id"]) for jid, d in saved for c in d["claims"]
                           if c.get("confirmation", {}).get("status") == "confirmed" and not c.get("confirmation_ref")]
    uncertain, handled, misses = 0, 0, []
    for job_id, st in states.items():
        qkeys = {q["key"] for q in st.get("questions", [])}
        unresolved = " ".join(st.get("unresolved", []))
        for r in st.get("rationales", []):
            for c in r["claims"]:
                if c["status"] == "explained" and not c.get("unresolved_details"):
                    continue
                uncertain += 1
                if c["claim_id"] in qkeys or r["key"] in unresolved:
                    handled += 1
                else:
                    misses.append({"job_id": job_id, "claim": c["claim_id"], "status": c["status"]})
    rate = handled / uncertain * 100 if uncertain else 0
    return {"verdict": verdict(uncertain > 0 and rate >= 90 and not confirmed_wo_answer),
            "summary": f"불확실한 결정 {uncertain}개 중 질문·미해결 분류 {handled}개 ({rate:.1f}%), 답변 없이 확인됨 {len(confirmed_wo_answer)}건",
            "target": "90% 이상", "uncertain": uncertain, "handled": handled, "rate_pct": round(rate, 1),
            "confirmed_without_answer": confirmed_wo_answer[:20], "misses": misses[:20],
            "note": "전문가 정답 라벨(결정별 '확정 가능/불가')이 없어, 하네스가 불확실로 판정한 결정의 처리 여부와 무단 확정 0건을 측정했다."}


def main(path: Path, only: set[str] | None = None) -> None:
    runs = json.loads(path.read_text())
    job_ids = [r["job_id"] for r in runs if r["job_id"]]
    with session_scope() as s:
        run_of = {j.id: j.current_run_id for j in s.scalars(select(Job).where(Job.id.in_(job_ids)))}
    states = {jid: asyncio.run(_state(run_of[jid])) for jid in job_ids}
    events = _events(job_ids)
    pool = {"pool": path.name, "jobs": len(job_ids)}
    metrics = {"Q-04": lambda: q04(states), "H-01": lambda: h01(events), "H-02": lambda: h02(events),
               "H-03-runs": lambda: h03(events, states), "H-05": lambda: h05(states, job_ids)}
    for name, fn in metrics.items():
        if not only or name in only:
            save(name, {**fn(), **pool})


if __name__ == "__main__":
    # run_metrics.py [runs 파일] [지표,지표]  예) run_metrics.py var/quality/fault_runs.json H-01
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT / "runs.json",
         set(sys.argv[2].split(",")) if len(sys.argv) > 2 else None)
