"""LangGraph 노드. 🟦 코드 / 🟨 LLM 구조화 출력 / 🟥 LLM + 도구 선택."""

import asyncio
import copy
import functools
import json
import re

import openai
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError
from langchain_openai import ChatOpenAI
from langgraph.types import interrupt
from sqlalchemy import select

from rework.agent import prompts as P
from rework.agent.errors import InputError, ToolCallError, TransientError
from rework.agent.mcp_client import RunContext, ToolHub
from rework.agent.schemas import AnswerInterpretation, CalcRuleSet, Judgement, ManualSpec, ProposalSet
from rework.config import get_settings
from rework.contracts.rules import ALLOWED_COLUMNS
from rework.db.models import Question
from rework.db.session import record_event, session_scope

ANALYSIS_TOOLS = ["analyze_trend", "analyze_baseline", "analyze_promo_lift", "analyze_dow", "rank_items"]
CRITERION_FIELDS = {
    "trend": {"consecutive_up", "consecutive_down", "change_pct", "last_mom_pct"},
    "baseline": {"deviation_pct", "target_value", "baseline_avg"},
    "promo_lift": {"lift_pct", "promo_day_avg", "normal_day_avg"},
    "dow": {"vs_overall_pct", "daily_avg"},
    "rank": {"vs_compare_pct", "daily_avg", "month_total"},
}
STAGE_LABELS = {
    "load_inputs": "파일 확인 중", "retrieve_context": "업무 규정 검색 중", "propose_calc_rules": "규칙 후보 생성 중",
    "verify_calc_rules": "계산·비교 중", "decompose_proposals": "제안 분해 중", "investigate": "판단 근거 조사 중",
    "call_tools": "분석 도구 실행 중", "judge_rationale": "근거 판정 중", "ask_user": "담당자 확인 필요",
    "apply_answers": "답변 반영 중", "save_results": "결과 저장 중", "build_artifacts": "결과물 생성 중",
}
TRANSIENT_OPENAI = (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)


def _hub(config) -> ToolHub:
    return config["configurable"]["hub"]


def _ctx(config) -> RunContext:
    return config["configurable"]["hub"].ctx


def tracked(fn):
    """노드 시작을 진행 이벤트로 남긴다."""
    name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(state, config):
        ctx = _ctx(config)
        record_event(ctx.job_id, ctx.run_id, "stage", name, STAGE_LABELS.get(name, name))
        return await fn(state, config)
    return wrapper


def _llm() -> ChatOpenAI:
    s = get_settings()
    return ChatOpenAI(model=s.openai_model, api_key=s.openai_api_key, temperature=0,
                      timeout=s.llm_timeout_seconds, max_retries=s.llm_max_retries)


async def _ainvoke(runnable, messages, config, node: str, what: str):
    ctx, cfg = _ctx(config), get_settings()
    # 라이브러리 timeout이 걸리지 않는 경우에 대비한 하드 제한 (재시도 포함)
    hard_limit = cfg.llm_timeout_seconds * (cfg.llm_max_retries + 1) + 10
    try:
        out = await asyncio.wait_for(runnable.ainvoke(messages), timeout=hard_limit)
    except TimeoutError as e:
        record_event(ctx.job_id, ctx.run_id, "error", node, f"모델 응답 {hard_limit}초 초과")
        raise TransientError(f"모델 응답 시간 초과 ({hard_limit}초)") from e
    except TRANSIENT_OPENAI as e:
        raise TransientError(f"모델 API 일시 오류: {e}") from e
    record_event(ctx.job_id, ctx.run_id, "llm", node, what)
    return out


async def _structured(schema, messages, config, node, attempts: int = 3):
    """구조화 출력. OpenAI strict json_schema로 형식을 강제하고, 그래도 검증에 실패하면 오류를 알려 주고 다시 요청한다."""
    model = _llm().with_structured_output(schema, method="json_schema", strict=True)
    msgs = list(messages)
    for i in range(attempts):
        try:
            return await _ainvoke(model, msgs, config, node, f"{schema.__name__} 생성")
        except (ValidationError, OutputParserException) as e:
            ctx = _ctx(config)
            record_event(ctx.job_id, ctx.run_id, "llm", node, f"{schema.__name__} 형식 오류로 재요청 ({i + 1}회)")
            if i == attempts - 1:
                raise InputError(f"모델 출력이 형식에 맞지 않습니다: {e}") from e
            msgs = list(messages) + [HumanMessage(f"직전 출력이 스키마 검증에 실패했습니다. 허용된 값만 써서 다시 출력하세요.\n오류: {str(e)[:800]}")]


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


# ---------------- A. 입력 준비 ----------------

@tracked
async def load_inputs(state, config):
    """🟦 원천 데이터 구조와 보고서 PDF를 구조화한다."""
    hub, ctx = _hub(config), _ctx(config)
    try:
        prof = await hub.call("business", "inspect_dataset", {"dataset_id": state["dataset_id"]}, node="load_inputs")
        rep = await hub.call("business", "read_report",
                             {"report_id": state["report_id"], "dataset_id": state["dataset_id"]}, node="load_inputs")
    except ToolCallError as e:
        raise InputError(str(e)) from e
    months = {m["month"] for m in prof["months"]}
    for p in (rep["period"], rep["prev_period"]):
        if p not in months:
            raise InputError(f"보고서 기간 {p}의 데이터가 원천 CSV에 없습니다.")
    slots = rep.pop("slots")
    unresolved = []
    if prof["unknown_day_values"]:
        unresolved.append(f"처음 보는 요일 값: {prof['unknown_day_values']}")
    record_event(ctx.job_id, ctx.run_id, "status", "load_inputs",
                 f"데이터 {prof['row_count']:,}행 · 보고서 표 {len(rep['tables'])}개 · 검증 슬롯 {len(slots)}개 · 제안 {len(rep['proposals'])}개")
    return {"profile": prof, "report": rep, "slots": slots, "unresolved": unresolved, "stage": "load_inputs",
            "calc_iteration": 0, "calc_rules": {}, "calc_status": {}, "calc_history": {}, "calc_done": False,
            "tool_results": {}, "rationales": [], "questions": [], "proposal_index": 0, "proposal_tool_calls": 0,
            "messages": []}


@tracked
async def retrieve_context(state, config):
    """🟦 rag-mcp에서 업무 지침·데이터 사전을 검색한다. 없으면 없는 대로 진행한다."""
    hub, ctx = _hub(config), _ctx(config)
    labels = list(dict.fromkeys(s["metric_label"] for s in state["slots"]))
    queries = ["매출액 정의 할인 차감", "금액 표기 단위와 반올림", "거래건수 transaction_count 정의",
               "행사 판매수량 promotion 정의"] + labels[:2]
    docs: dict[str, dict] = {}
    for q in queries:
        try:
            res = await hub.call("rag", "search_work_rules", {"query": q, "top_k": 3}, node="retrieve_context")
        except ToolCallError as e:
            record_event(ctx.job_id, ctx.run_id, "status", "retrieve_context", f"규정 검색 실패, 근거 없이 진행: {e}")
            break
        for r in res["results"]:
            docs[r["chunk_id"]] = r
    record_event(ctx.job_id, ctx.run_id, "status", "retrieve_context",
                 f"관련 규정 {len(docs)}건" if docs else "관련 규정 없음 (검색 결과 없음)")
    return {"evidence_docs": list(docs.values()), "stage": "retrieve_context"}


# ---------------- B. 1층 계산 규칙 ----------------

def _pending(state) -> list[dict]:
    return [s for s in state["slots"] if state["calc_status"].get(s["slot_id"], {}).get("status") != "matched"]


@tracked
async def propose_calc_rules(state, config):
    """🟨 아직 맞지 않은 슬롯마다 계산 규칙 후보를 제안한다."""
    pending = _pending(state)
    view = [{"slot_id": s["slot_id"], "table": s["table_title"], "metric": s["metric_label"], "unit": s["unit"],
             "group_by": s["group_by"], "cells": [{k: c[k] for k in ("group", "period", "raw")} for c in s["cells"][:3]]}
            for s in pending]
    history = {s["slot_id"]: state["calc_history"].get(s["slot_id"], [])[-2:] for s in pending
               if state["calc_history"].get(s["slot_id"])}
    cols = [c for c in state["profile"]["columns"]]
    docs = "\n".join(f"- ({d['source_location']}) {d['excerpt'][:300]}" for d in state["evidence_docs"]) or "없음"
    msgs = [SystemMessage(P.CALC_SYSTEM.format(allowed=", ".join(ALLOWED_COLUMNS))),
            HumanMessage(P.CALC_USER.format(columns=_j(cols), notes="\n".join(state["report"]["notes"]) or "없음",
                                            docs=docs, slots=_j(view), history=_j(history) if history else "없음 (첫 시도)"))]
    res: CalcRuleSet = await _structured(CalcRuleSet, msgs, config, "propose_calc_rules")
    ids = {s["slot_id"] for s in pending}
    rules = dict(state["calc_rules"])
    rules.update({r.slot_id: r.model_dump() for r in res.rules if r.slot_id in ids})
    return {"calc_rules": rules, "stage": "propose_calc_rules"}


@tracked
async def verify_calc_rules(state, config):
    """🟦 규칙을 business-mcp로 실제 계산하고 보고서 값과 비교한다."""
    hub, ctx, cfg = _hub(config), _ctx(config), get_settings()
    pending = _pending(state)
    rules = [state["calc_rules"][s["slot_id"]] for s in pending if s["slot_id"] in state["calc_rules"]]
    calc = await hub.call("business", "execute_aggregation",
                          {"dataset_id": state["dataset_id"], "report_id": state["report_id"], "rules": rules},
                          node="verify_calc_rules")
    try:
        cmp = await hub.call("business", "compare_report", {"calculation_id": calc["calculation_id"]},
                             node="verify_calc_rules")
    except ToolCallError as e:
        # 계산 결과는 서버 메모리에만 있다. 그사이 서버가 재시작됐으면 이 노드를 처음부터 다시 실행한다.
        raise TransientError(f"계산 결과를 다시 찾지 못했습니다: {e}") from e
    status, history = dict(state["calc_status"]), copy.deepcopy(state["calc_history"])
    for c in cmp["comparisons"]:
        status[c["slot_id"]] = c
        r = c["rule"]
        history.setdefault(c["slot_id"], []).append({
            "rule": {k: r[k] for k in ("op", "column", "denominator_column", "scale", "as_percent", "decimals", "rounding")},
            "matched": f"{c['matched']}/{c['cells']}", "diffs": c["diffs"][:3]})
    for e in calc["errors"]:
        if e.get("slot_id"):
            history.setdefault(e["slot_id"], []).append({"error": e["error"]})
    iteration = state["calc_iteration"] + 1
    n_ok = sum(1 for s in state["slots"] if status.get(s["slot_id"], {}).get("status") == "matched")
    record_event(ctx.job_id, ctx.run_id, "status", "verify_calc_rules",
                 f"{iteration}회차 비교: {n_ok}/{len(state['slots'])} 슬롯 일치")
    done = n_ok == len(state["slots"]) or iteration >= cfg.calc_max_iterations
    out = {"calc_status": status, "calc_history": history, "calc_iteration": iteration, "calc_done": done,
           "stage": "verify_calc_rules"}
    if done:
        missing = [s for s in state["slots"] if status.get(s["slot_id"], {}).get("status") != "matched"]
        out["unresolved"] = state["unresolved"] + [
            f"계산 규칙 미확정: {s['table_title']} · {s['metric_label']} ({iteration}회 시도 후 불일치)" for s in missing]
        matched = [s for s in state["slots"] if s not in missing]
        out["questions"] = state["questions"] + [{
            "key": "calc_rules", "kind": "confirm",
            "text": f"보고서 수치를 재현한 계산 규칙 {len(matched)}개가 실제 작성 방식과 같습니까?",
            "reason": "숫자가 일치해도 실제 작성 방법을 유일하게 복원했다고 볼 수 없어 확인이 필요합니다.",
            "options": [{"value": "confirm", "label": "맞습니다"}, {"value": "reject", "label": "아닙니다"}],
            "context": {"rules": [{"metric": f"{s['table_title']} · {s['metric_label']}",
                                   "rule": state["calc_rules"][s["slot_id"]]} for s in matched]},
        }]
    return out


def route_calc(state) -> str:
    return "decompose_proposals" if state["calc_done"] else "propose_calc_rules"


# ---------------- C. 2층 판단 근거 ----------------

def _resolve_scope(scope: dict | None, cv: dict) -> dict | None:
    """모델이 쓴 이름을 데이터의 정확한 값으로 맞춘다. 못 찾은 값은 버린다."""
    if not scope or not scope.get("values"):
        return None
    cols = [scope["column"]] + [c for c in ("item_name", "category_l2", "category_l1") if c != scope["column"]]
    for col in cols:
        names, found = cv.get(col, []), []
        for v in scope["values"]:
            exact = [n for n in names if n == v]
            starts = [n for n in names if n.startswith(v)]
            contains = [n for n in names if v in n or n in v]
            pick = exact or starts[:1] or (contains if len(contains) == 1 else [])
            found += [p for p in pick if p not in found]
        if found and len(found) >= len(scope["values"]) * 0.5:
            return {"column": col, "values": found}
    return None


OPPOSITE = {"축소": {"유지", "확대"}, "유지": {"축소", "제외"}, "제외": {"포함", "유지"}, "포함": {"제외"}, "확대": {"축소"}}


def _derive_comparators(claims: list[dict]) -> None:
    """🟦 같은 제안 안에서 반대 결정을 받은 대상을 비교 대상으로 붙인다.
    예: 단팥빵 '축소' ↔ 카스텔라·크림빵 '유지'. 근거가 이 둘을 가르지 못하면 설명으로 인정하지 않는다."""
    for c in claims:
        if c.get("comparators") or not c.get("targets"):
            continue
        others = [o for o in claims if o is not c and o.get("targets") and o["decision"] in OPPOSITE.get(c["decision"], set())
                  and o["targets"]["column"] == c["targets"]["column"]]
        values = [v for o in others for v in o["targets"]["values"] if v not in c["targets"]["values"]]
        if values:
            c["comparators"] = {"column": c["targets"]["column"], "values": list(dict.fromkeys(values))}


def _scope_from_text(text: str, cv: dict) -> dict | None:
    """🟦 모델이 대상을 못 잡았을 때, 문장에 나온 품목명(앞 단어 기준)을 찾는다."""
    found = [n for n in cv["item_name"] if n.split()[0] in text]
    return {"column": "item_name", "values": found} if found else None


def _merge_claims(claims: list[dict], key: str) -> list[dict]:
    """🟦 같은 결정을 받은 같은 종류의 대상은 하나의 결정으로 묶는다 (질문 수를 줄이고 기준을 일반화하기 위해)."""
    merged: list[dict] = []
    for c in claims:
        t = c.get("targets")
        same = next((m for m in merged if t and m.get("targets") and c["decision"] != "기타"
                     and m["decision"] == c["decision"] and m["targets"]["column"] == t["column"]), None)
        if same:
            same["statement"] += " / " + c["statement"]
            same["targets"]["values"] = list(dict.fromkeys(same["targets"]["values"] + t["values"]))
        else:
            merged.append(copy.deepcopy(c))
    for i, c in enumerate(merged, 1):
        c["claim_id"] = f"{key}-{i}"
    return merged


@tracked
async def decompose_proposals(state, config):
    """🟨 제안 문장을 결정(claim) 단위로 나누고 대상·비교 대상을 정한다."""
    rep, cv = state["report"], state["profile"]["categorical_values"]
    msgs = [SystemMessage(P.DECOMPOSE_SYSTEM), HumanMessage(P.DECOMPOSE_USER.format(
        title=rep["report_title"], period=rep["period"], prev_period=rep["prev_period"],
        months=_j(state["profile"]["months"]), items=", ".join(cv["item_name"]),
        cat1=", ".join(cv["category_l1"]), cat2=", ".join(cv["category_l2"]),
        proposals=_j([{"key": p["key"], "title": p["title"], "text": p["text"]} for p in rep["proposals"]])))]
    res: ProposalSet = await _structured(ProposalSet, msgs, config, "decompose_proposals")
    plans = {p.key: p for p in res.proposals}
    proposals = []
    for p in rep["proposals"]:
        claims = []
        for i, c in enumerate(plans[p["key"]].claims if p["key"] in plans else [], 1):
            d = c.model_dump()
            d["claim_id"] = f"{p['key']}-{i}"
            d["targets"] = _resolve_scope(d["targets"], cv)
            d["comparators"] = _resolve_scope(d["comparators"], cv)
            claims.append(d)
        if not claims:
            claims = [{"claim_id": f"{p['key']}-1", "statement": p["title"], "decision": "기타",
                       "stated_reason": p["text"], "targets": None, "comparators": None}]
        for c in claims:
            c["targets"] = c["targets"] or _scope_from_text(c["statement"] + " " + p["text"], cv)
        claims = _merge_claims(claims, p["key"])
        _derive_comparators(claims)
        proposals.append({**p, "claims": claims})
    return {"proposals": proposals, "proposal_index": 0, "messages": [], "proposal_tool_calls": 0,
            "stage": "decompose_proposals"}


@tracked
async def investigate(state, config):
    """🟥 모델이 분석 도구를 직접 골라 근거 가설을 검증한다."""
    hub, cfg = _hub(config), get_settings()
    p = state["proposals"][state["proposal_index"]]
    msgs = list(state.get("messages") or [])
    if not msgs:
        rep = state["report"]
        partial = [m["month"] for m in state["profile"]["months"] if m.get("partial")] or ["없음"]
        msgs = [SystemMessage(P.INVESTIGATE_SYSTEM.format(limit=cfg.investigate_max_tool_calls, period=rep["period"],
                                                          prev_period=rep["prev_period"], partial=", ".join(partial))),
                HumanMessage(P.INVESTIGATE_USER.format(key=p["key"], title=p["title"], text=p["text"],
                                                       claims=_j(p["claims"]), dataset_id=state["dataset_id"],
                                                       months=_j(state["profile"]["months"])))]
    model = _llm().bind_tools(hub.openai_tools("business", ANALYSIS_TOOLS))
    n_results = sum(isinstance(m, ToolMessage) for m in msgs)
    ai = await _ainvoke(model, msgs, config, "investigate",
                        f"{p['key']} 조사: " + (f"도구 결과 {n_results}개 검토" if n_results else "가설 수립·도구 선택"))
    return {"messages": msgs + [ai], "stage": "investigate"}


@tracked
async def call_tools(state, config):
    """🟦 모델이 고른 도구를 실행한다. dataset_id는 신뢰된 값으로 덮어쓴다."""
    hub, cfg = _hub(config), get_settings()
    p = state["proposals"][state["proposal_index"]]
    msgs, results = list(state["messages"]), dict(state["tool_results"])
    count = state["proposal_tool_calls"]
    for tc in msgs[-1].tool_calls:
        if tc["name"] not in ANALYSIS_TOOLS:
            content = {"error": f"사용할 수 없는 도구: {tc['name']}"}
        elif count >= cfg.investigate_max_tool_calls:
            content = {"error": "제안당 도구 호출 한도에 도달했습니다. 지금까지의 결과로 요약하세요."}
        else:
            args = {**tc["args"], "dataset_id": state["dataset_id"]}
            count += 1
            try:
                out = await hub.call("business", tc["name"], args, node="investigate")
                rid = f"r{len(results) + 1}"
                results[rid] = {"proposal": p["key"], "tool": tc["name"],
                                "args": {k: v for k, v in args.items() if k != "dataset_id"}, "result": out}
                content = {"result_id": rid, **out}
            except ToolCallError as e:
                content = {"error": str(e)}
        msgs.append(ToolMessage(content=_j(content), tool_call_id=tc["id"]))
    return {"messages": msgs, "tool_results": results, "proposal_tool_calls": count}


def route_investigate(state) -> str:
    last = state["messages"][-1]
    limit = get_settings().investigate_max_tool_calls
    if isinstance(last, AIMessage) and last.tool_calls and state["proposal_tool_calls"] < limit \
            and state["remaining_steps"] > 15:
        return "call_tools"
    return "judge_rationale"


def route_after_tools(state) -> str:
    return "judge_rationale" if state["proposal_tool_calls"] >= get_settings().investigate_max_tool_calls else "investigate"


def _numbers(obj) -> list[float]:
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in _numbers(v)]
    if isinstance(obj, list):
        return [n for v in obj for n in _numbers(v)]
    if isinstance(obj, str):
        return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", obj)]
    return []


def _grounded(x: float, pool: list[float]) -> bool:
    """인용 숫자가 도구 결과에 있는지. '13.1% 감소'처럼 부호를 빼고 쓰는 경우도 인정한다."""
    return any(abs(abs(x) - abs(n)) <= max(0.06, abs(n) * 0.01) for n in pool)


def check_claim(claim: dict, cj, results: dict) -> dict:
    """🟦 모델 판정을 코드로 검사한다: 결과 ID 존재, 비교 대상 판별, 인용 숫자 근거, 기준 필드."""
    notes = []
    status = cj.status if cj else "unexplained"
    hyps = []
    for h in (cj.hypotheses if cj else []):
        d = h.model_dump()
        d["result_ids"] = [r for r in d["result_ids"] if r in results]
        hyps.append(d)
    supported = [h for h in hyps if h["verdict"] == "supported"]
    if status == "explained":
        problem = None
        if not supported:
            problem = "채택한 가설이 없음"
        elif not any(h["result_ids"] for h in supported):
            problem = "근거 도구 결과가 없음"
        elif claim.get("comparators") and all(h["holds_for_comparators"] is not False for h in supported):
            problem = "비교 대상에도 성립하는 가설뿐이라 결정을 가르지 못함"
        if problem:
            status = "ambiguous"
            notes.append(problem)
    ref_ids = {r for h in (supported or hyps) for r in h["result_ids"]} or set(results)
    pool = _numbers([results[r]["result"] for r in ref_ids if r in results])
    bad = [x for x in (cj.cited_numbers if cj else []) if not _grounded(x, pool)]
    if bad:
        notes.append(f"도구 결과에서 찾을 수 없는 숫자: {bad}")
        if status == "explained":
            status = "ambiguous"
    crit = cj.criterion.model_dump() if cj and cj.criterion else None
    if crit and crit["condition"]["field"] not in CRITERION_FIELDS[crit["type"]]:
        notes.append(f"기준 필드 {crit['condition']['field']}는 {crit['type']} 결과에 없음")
        crit = None
    return {**claim, "status": status, "criterion": crit, "hypotheses": hyps,
            "evidence": cj.evidence_summary if cj else [],
            "rejected_hypotheses": [h["statement"] for h in hyps if h["verdict"] == "refuted"],
            "check_notes": notes, "question": cj.question.model_dump() if cj and cj.question else None}


async def _self_consistency(c: dict, state, config) -> None:
    """🟦 기준을 당월 데이터에 실제로 적용해, 대상은 걸리고 비교 대상은 걸리지 않는지 확인한다."""
    crit, t = c.get("criterion"), c.get("targets")
    if c["status"] != "explained" or not crit or crit["type"] not in ("trend", "promo_lift") \
            or not t or t["column"] != "item_name":
        return
    comps = (c.get("comparators") or {}).get("values", [])
    probe = {**crit, "scope_column": "item_name", "scope_values": t["values"] + comps}
    try:
        ev = await _hub(config).call("business", "evaluate_criterion", {
            "dataset_id": state["dataset_id"], "criterion": probe, "month": state["report"]["period"]},
            node="judge_rationale")
    except ToolCallError as e:
        c["check_notes"].append(f"기준 재적용 실패: {e}")
        return
    hits = {h["name"] for h in ev["hits"]}
    missed, wrong = sorted(set(t["values"]) - hits), sorted(hits & set(comps))
    if missed or wrong:
        c["status"] = "ambiguous"
        c["check_notes"].append(f"기준을 당월에 다시 적용하니 대상 중 빠진 것 {missed}, 비교 대상 중 걸린 것 {wrong}")
    else:
        c["check_notes"].append("기준 재적용 검사 통과: 대상만 선택됨")


def _confirm_question(p: dict, claims: list[dict]) -> dict:
    """데이터로 설명된 결정들을 제안당 질문 하나로 확인받는다."""
    lines = [f"{c['statement']} → {c['criterion']['description'] if c['criterion'] else '; '.join(c['evidence'])}"
             for c in claims]
    return {"key": f"{p['key']}-confirm", "kind": "confirm",
            "text": f"[{p['title']}] 아래 판단 기준이 실제로 보신 근거와 같습니까?",
            "reason": "데이터로는 설명되지만, 담당자 확인 전까지는 추정입니다.",
            "options": [{"value": "confirm", "label": "맞습니다"}, {"value": "reject", "label": "아닙니다"}],
            "context": {"proposal_key": p["key"], "claims": [c["claim_id"] for c in claims], "criteria": lines,
                        "evidence": [e for c in claims for e in c["evidence"]],
                        "rejected": [h for c in claims for h in c["rejected_hypotheses"]]}}


def _question_for(p: dict, c: dict) -> dict:
    ctx = {"proposal_key": p["key"], "proposal_title": p["title"], "claim": c["statement"],
           "evidence": c["evidence"], "criterion": c["criterion"], "check_notes": c["check_notes"],
           "rejected": c["rejected_hypotheses"]}
    q = c.get("question") or {}
    if c["status"] == "ambiguous":
        return {"key": c["claim_id"], "kind": "choose" if q.get("options") else "free", "context": ctx,
                "text": q.get("text") or f"[{p['title']}] '{c['statement']}'는 어떤 기준으로 정하셨나요?",
                "reason": q.get("reason") or "여러 근거가 같은 결정을 설명하거나 기준값이 불명확합니다.",
                "options": [{"value": o, "label": o} for o in q.get("options", [])]}
    return {"key": c["claim_id"], "kind": "free", "context": ctx,
            "text": q.get("text") or f"[{p['title']}] '{c['statement']}'는 데이터로 설명되지 않았습니다. 어떤 근거로 결정하셨나요?",
            "reason": q.get("reason") or "원천 데이터에서 이 결정을 설명하는 신호를 찾지 못했습니다.", "options": []}


@tracked
async def judge_rationale(state, config):
    """🟨+🟦 결정마다 근거를 판정하고, 코드가 판정을 검사한 뒤 질문을 만든다."""
    p = state["proposals"][state["proposal_index"]]
    results = {rid: r for rid, r in state["tool_results"].items() if r["proposal"] == p["key"]}
    notes = next((m.content for m in reversed(state["messages"]) if isinstance(m, AIMessage) and m.content), "")
    msgs = [SystemMessage(P.JUDGE_SYSTEM), HumanMessage(P.JUDGE_USER.format(
        key=p["key"], title=p["title"], text=p["text"], claims=_j(p["claims"]),
        results=_j([{"result_id": rid, "tool": r["tool"], "args": r["args"], "result": r["result"]}
                    for rid, r in results.items()]) if results else "없음", notes=notes or "없음"))]
    j: Judgement = await _structured(Judgement, msgs, config, "judge_rationale")
    by_id = {c.claim_id: c for c in j.claims}
    claims = [check_claim(c, by_id.get(c["claim_id"]), results) for c in p["claims"]]
    for c in claims:
        await _self_consistency(c, state, config)
    order = {"unexplained": 0, "ambiguous": 1, "explained": 2}
    rationale = {"key": p["key"], "title": p["title"], "proposal_text": p["text"], "claims": claims,
                 "status": min((c["status"] for c in claims), key=order.get)}
    ctx = _ctx(config)
    record_event(ctx.job_id, ctx.run_id, "status", "judge_rationale",
                 f"{p['key']} {p['title']}: " + ", ".join(f"{c['claim_id']}={c['status']}" for c in claims))
    return {"rationales": state["rationales"] + [rationale],
            "questions": state["questions"] + _questions(p, claims),
            "proposal_index": state["proposal_index"] + 1, "messages": [], "proposal_tool_calls": 0,
            "stage": "judge_rationale"}


def _questions(p: dict, claims: list[dict]) -> list[dict]:
    explained = [c for c in claims if c["status"] == "explained"]
    qs = [_confirm_question(p, explained)] if explained else []
    return qs + [_question_for(p, c) for c in claims if c["status"] != "explained"]


def route_rationale(state) -> str:
    if state["proposal_index"] < len(state["proposals"]):
        return "investigate"
    return "ask_user" if state["questions"] else "save_results"


# ---------------- D. 담당자 확인 ----------------

@tracked
async def ask_user(state, config):
    """🟦 질문을 DB에 저장하고 실행을 중단한다. 답변이 오면 같은 run_id로 여기서 재개한다."""
    ctx = _ctx(config)
    ids = {}
    with session_scope() as s:
        for q in state["questions"]:
            row = s.scalar(select(Question).where(Question.run_id == ctx.run_id, Question.key == q["key"]))
            if row is None:
                row = Question(run_id=ctx.run_id, job_id=ctx.job_id, tenant_id=ctx.tenant_id, key=q["key"],
                               kind=q["kind"], text=q["text"], reason=q["reason"], options=q["options"],
                               context=q["context"])
                s.add(row)
                s.flush()
            ids[q["key"]] = row.id
    answers = interrupt({"question_ids": list(ids.values())})
    return {"question_ids": ids, "answers": answers, "stage": "ask_user"}


@tracked
async def apply_answers(state, config):
    """🟨+🟦 답변을 근거에 반영한다. 확인 상태 자체는 저장 시 DB 답변 기록으로 정해진다."""
    answers = state.get("answers") or {}
    rationales = copy.deepcopy(state["rationales"])
    for rat in rationales:
        for c in rat["claims"]:
            a = answers.get(c["claim_id"]) or (answers.get(f"{rat['key']}-confirm") if c["status"] == "explained" else None)
            if not a:
                continue
            c["confirmation_ref"] = a["question_id"]
            c["answer"] = {"choice": a["choice"], "text": a["text"]}
            if a["choice"] == "reject" or (a["choice"] == "confirm" and not a["text"].strip()):
                continue
            said = a["text"].strip() or a["choice"]
            interp: AnswerInterpretation = await _structured(AnswerInterpretation, [
                SystemMessage(P.ANSWER_SYSTEM),
                HumanMessage(f"[결정] {c['statement']}\n[보고서 근거 표현] {c['stated_reason']}\n"
                             f"[데이터 근거] {_j(c['evidence'])}\n[담당자 답변] {said}")], config, "apply_answers")
            c["criterion_user"] = interp.criterion_description
            if interp.criterion and interp.criterion.condition.field in CRITERION_FIELDS[interp.criterion.type]:
                c["criterion"] = interp.criterion.model_dump()
            if c["status"] != "explained":
                c["status"] = "user_explained"
        order = {"unexplained": 0, "ambiguous": 1, "user_explained": 2, "explained": 3}
        rat["status"] = min((c["status"] for c in rat["claims"]), key=order.get)
    return {"rationales": rationales, "stage": "apply_answers"}


# ---------------- E. 결과물 ----------------

@tracked
async def save_results(state, config):
    """🟦 규칙 버전과 근거를 저장하고, 확인된 지식만 rag-mcp에 등록한다."""
    hub, ctx = _hub(config), _ctx(config)
    answers = state.get("answers") or {}
    calc_ref = answers.get("calc_rules", {}).get("question_id")
    calc_rules, cols = [], {"business_date", "item_name", "category_l1", "category_l2", "day_of_week", "weekend_yn"}
    for s in state["slots"]:
        rule = state["calc_rules"].get(s["slot_id"])
        if not rule:
            continue
        st = state["calc_status"].get(s["slot_id"], {}).get("status", "untested")
        cols |= {rule["column"]} | ({rule["denominator_column"]} if rule.get("denominator_column") else set())
        calc_rules.append({"slot": {k: s[k] for k in ("slot_id", "table_id", "table_title", "metric_label", "unit", "group_by", "cells")},
                           "rule": rule, "validation_status": st,
                           "confirmation_ref": calc_ref if st == "matched" else None})
    rep, prof = state["report"], state["profile"]
    rv = await hub.call("business", "save_rule_version", {
        "calc_rules": calc_rules, "dataset_columns": sorted(cols), "unresolved": state["unresolved"],
        "report": {"title": rep["report_title"], "period": rep["period"], "prev_period": rep["prev_period"]},
        "scope": {"day_values": prof["categorical_values"]["day_of_week"], "months": [m["month"] for m in prof["months"]],
                  "store_ids": prof["categorical_values"]["store_id"]}}, node="save_results")
    for rat in state["rationales"]:
        await hub.call("business", "save_rationale", {"proposal_key": rat["key"], "rationale": rat}, node="save_results")

    # 확인된 지식만 다음 실행에서 검색되도록 등록한다.
    registered = 0
    calc_ans = answers.get("calc_rules")
    if calc_ans and calc_ans["choice"] == "confirm":
        lines = [f"- {r['slot']['table_title']} · {r['slot']['metric_label']}: {r['rule']['op']}({r['rule']['column']}), "
                 f"scale={r['rule']['scale']}, 소수 {r['rule']['decimals']}자리" for r in calc_rules if r["validation_status"] == "matched"]
        registered += await _register(hub, ctx, "월간 판매 실적 보고서 계산 규칙", "\n".join(lines), calc_ans["answer_id"])
    for rat in state["rationales"]:
        for c in rat["claims"]:
            a = answers.get(c["claim_id"]) or (answers.get(f"{rat['key']}-confirm") if c["status"] == "explained" else None)
            if not a or a["choice"] == "reject":
                continue
            desc = c.get("criterion_user") or (c["criterion"] or {}).get("description") or ""
            body = (f"제안: {rat['title']} — {c['statement']}\n판단 기준: {desc}\n근거 데이터: {'; '.join(c['evidence'])}\n"
                    f"담당자 답변: {a['text'] or a['choice']}")
            registered += await _register(hub, ctx, f"판단 기준: {c['statement']}"[:200], body, a["answer_id"])
    record_event(ctx.job_id, ctx.run_id, "status", "save_results",
                 f"규칙 버전 v{rv['version']} 저장 · 확인된 지식 {registered}건 등록")
    return {"rule_version_id": rv["rule_version_id"], "stage": "save_results"}


async def _register(hub, ctx, title, content, answer_id) -> int:
    try:
        await hub.call("rag", "register_confirmed_rule", {"title": title, "content": content, "answer_id": answer_id},
                       node="save_results")
        return 1
    except ToolCallError as e:
        record_event(ctx.job_id, ctx.run_id, "status", "save_results", f"지식 등록 건너뜀: {e}")
        return 0


@tracked
async def build_artifacts(state, config):
    """🟨+🟦 매뉴얼 서술은 모델이, 표·숫자는 business-mcp가 저장된 규칙에서 채운다."""
    hub = _hub(config)
    summary = {
        "report": state["report"]["report_title"],
        "calc_rules": [{"metric": f"{s['table_title']} · {s['metric_label']}",
                        "rule": state["calc_rules"].get(s["slot_id"]),
                        "status": state["calc_status"].get(s["slot_id"], {}).get("status")} for s in state["slots"]],
        "decisions": [{"proposal": r["title"], "claims": [{"statement": c["statement"], "status": c["status"],
                                                           "criterion": (c["criterion"] or {}).get("description") or c.get("criterion_user")}
                                                          for c in r["claims"]]} for r in state["rationales"]],
        "unresolved": state["unresolved"],
    }
    spec: ManualSpec = await _structured(ManualSpec, [SystemMessage(P.MANUAL_SYSTEM), HumanMessage(_j(summary))],
                                         config, "build_artifacts")
    res = await hub.call("business", "generate_artifacts", {
        "rule_version_id": state["rule_version_id"], "manual_spec": spec.model_dump(),
        "dataset_id": state["dataset_id"], "report_id": state["report_id"]}, node="build_artifacts")
    return {"artifact_ids": res["artifact_ids"], "stage": "done"}

