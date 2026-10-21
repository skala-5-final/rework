"""MCP 서버를 compose 내부 네트워크에서 직접 호출해 측정한다 (MCP는 밖에 포트를 열지 않는다).

    scripts/quality/run_in_container.sh mcp_checks.py <job_id>

- Q-11(MCP): 토큰 없음·서명 위조·대상(aud) 다름·만료·alg none·다른 회사 토큰 → 도구·리소스 실행 거절
- Q-14 MCP 계약: 도구마다 스키마(설명·필수 인자), 필수 인자 누락·타입 오류는 오류 결과로, 정상 입력은 구조화 결과로
- Q-17 승인(서버): register_confirmed_rule에 승인 없음·가짜 답변·다른 회사·다른 실행·반려 답변 → 거절, 정상 승인만 등록.
  그리고 DB의 모든 확정 지식이 실제 승인 답변(반려 아님)을 가리키는지 전수 확인.
"""

import asyncio
import json
import re
import sys
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta

import httpx2
import jwt
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from sqlalchemy import delete, select

from common import save, verdict
from rework.agent import policy
from rework.agent.graph import build_graph
from rework.auth.mcp_scope import AUDIENCE, issue_token
from rework.config import get_settings
from rework.db.models import Answer, Document, DocumentChunk, Job, Question, RuleVersion, Run, User
from rework.db.session import session_scope

URL = {"business": get_settings().business_mcp_url, "rag": get_settings().rag_mcp_url}
NODES = ["load_inputs", "retrieve_context", "propose_calc_rules", "verify_calc_rules", "decompose_proposals", "investigate",
         "call_tools", "judge_rationale", "ask_user", "apply_answers", "save_results", "build_artifacts",
         "check_schema", "generate_report_draft"]


async def session(stack: AsyncExitStack, server: str, token: str | None) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http = await stack.enter_async_context(create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(30.0, read=120)))
    return await stack.enter_async_context(Client(streamable_http_client(URL[server], http_client=http), read_timeout_seconds=120))


async def call(server: str, token: str | None, tool: str, args: dict) -> tuple[bool, str, object]:
    """(오류 여부, 메시지, 구조화 결과). 연결 단계 거절도 오류로 본다."""
    try:
        async with AsyncExitStack() as stack:
            c = await session(stack, server, token)
            res = await c.call_tool(tool, args)
            text = "".join(getattr(x, "text", "") for x in res.content)
            data = res.structured_content
            if data is None and not res.is_error:  # 구조화 필드가 없으면 Agent(ToolHub)처럼 JSON 본문을 결과로 본다
                try:
                    data = json.loads(text)
                except ValueError:
                    data = None
            return bool(res.is_error), text[:200], data
    except Exception as e:  # noqa: BLE001
        return True, f"{type(e).__name__}: {e}"[:200], None


async def read(server: str, token: str | None, uri: str) -> tuple[bool, str]:
    try:
        async with AsyncExitStack() as stack:
            c = await session(stack, server, token)
            res = await c.read_resource(uri)
            return False, "".join(getattr(x, "text", "") for x in res.contents)[:200]
    except Exception as e:  # noqa: BLE001
        return True, f"{type(e).__name__}: {e}"[:200]


def forged(tenant: str, run: str, **over) -> str:
    cfg = get_settings()
    body = {"tenant": tenant, "run": run, "trace": "", "aud": AUDIENCE, "exp": datetime.now(UTC) + timedelta(minutes=10)}
    body.update(over.get("claims", {}))
    return jwt.encode(body, over.get("secret", cfg.mcp_token_secret), algorithm=over.get("alg", "HS256"))


async def fixtures(job_id: str) -> dict:
    with session_scope() as s:
        job = s.get(Job, job_id)
        run = s.get(Run, job.current_run_id)
        rv = s.scalar(select(RuleVersion).where(RuleVersion.run_id == run.id))
        other_run = s.scalar(select(Run).where(Run.tenant_id == job.tenant_id, Run.id != run.id, Run.kind == "analyze"))
        user = s.scalar(select(User).where(User.tenant_id == job.tenant_id))
        f = {"tenant": job.tenant_id, "run": run.id, "other_run": other_run.id, "dataset": job.source_file_id,
             "report": job.report_file_id, "rule_version": rv.id, "job": job.id, "user": user.id}
    async with AsyncPostgresSaver.from_conn_string(get_settings().checkpoint_url) as saver:
        st = (await build_graph(saver).aget_state({"configurable": {"thread_id": f["run"]}})).values
    f["tool_args"] = {}
    for t in st["tool_results"].values():
        f["tool_args"].setdefault(t["tool"], {**t["args"], "dataset_id": f["dataset"]})
    f["chunk"] = next((d["chunk_id"] for d in st.get("evidence_docs", []) if d.get("chunk_id")), None)
    return f


# ---------------- Q-11 MCP ----------------

async def q11(f: dict) -> dict:
    ten, run = f["tenant"], f["run"]
    good = issue_token(ten, run)
    bad = {"토큰 없음": None, "형식 오류": "not-a-jwt",
           "서명 위조": forged(ten, run, secret="wrong-secret-0123456789abcdef0123"),
           "대상(aud) 다름": forged(ten, run, claims={"aud": "rework-api"}),
           "만료": forged(ten, run, claims={"exp": datetime.now(UTC) - timedelta(minutes=1)}),
           "alg none": jwt.encode({"tenant": ten, "run": run, "aud": AUDIENCE}, None, algorithm="none")}
    other = issue_token("other-co", "run_other")
    targets = [("business", "inspect_dataset", {"dataset_id": f["dataset"]}),
               ("business", "read_report", {"report_id": f["report"], "dataset_id": f["dataset"]}),
               ("business", "check_schema", {"rule_version_id": f["rule_version"], "dataset_id": f["dataset"]}),
               ("rag", "search_work_rules", {"query": "매출액 정의", "top_k": 2})]
    if "analyze_trend" in f["tool_args"]:
        targets.append(("business", "analyze_trend", f["tool_args"]["analyze_trend"]))
    if f["chunk"]:
        targets.append(("rag", "get_source_excerpt", {"chunk_id": f["chunk"]}))
    cases = []
    for name, tok in bad.items():
        for server, tool, args in targets:
            err, msg, _ = await call(server, tok, tool, args)
            cases.append({"kind": "인증 실패", "token": name, "req": f"{server}.{tool}", "blocked": err, "msg": msg})
        if f["chunk"]:
            err, msg = await read("rag", tok, f"doc://{f['chunk']}")
            cases.append({"kind": "인증 실패", "token": name, "req": "rag doc://", "blocked": err, "msg": msg})
    # 다른 회사 토큰: 데모 회사 자원을 가리키는 요청은 거절, 검색은 자기 회사 문서만
    for server, tool, args in targets:
        if tool == "search_work_rules":
            err, msg, data = await call(server, other, tool, args)
            with session_scope() as s:
                demo_chunks = set(s.scalars(select(DocumentChunk.id).where(DocumentChunk.tenant_id == ten)).all())
            results = (data or {}).get("results", []) if isinstance(data, dict) else []
            leaked = [r.get("chunk_id") for r in results if r.get("chunk_id") in demo_chunks]
            cases.append({"kind": "회사 격리", "req": "rag.search_work_rules (데모 회사 문서 노출)", "blocked": not err and not leaked,
                          "msg": f"결과 {len(results)}건, 데모 회사 조각 {len(leaked)}건"})
            continue
        err, msg, _ = await call(server, other, tool, args)
        cases.append({"kind": "회사 격리", "req": f"{server}.{tool}", "blocked": err, "msg": msg})
    if f["chunk"]:
        err, msg = await read("rag", other, f"doc://{f['chunk']}")
        cases.append({"kind": "회사 격리", "req": "rag doc://", "blocked": err, "msg": msg})
    controls = []
    for server, tool, args in targets:
        err, msg, _ = await call(server, good, tool, args)
        controls.append({"req": f"{server}.{tool}", "ok": not err, "msg": "" if not err else msg})
    blocked = sum(c["blocked"] for c in cases)
    return {"requests": len(cases), "blocked": blocked, "controls_ok": all(c["ok"] for c in controls),
            "controls": controls, "not_blocked": [c for c in cases if not c["blocked"]]}


# ---------------- Q-14 계약 ----------------

def _wrong_type(schema: dict) -> dict | None:
    for name in schema.get("required", []):
        t = schema["properties"][name].get("type")
        if t == "string":
            return {name: 12345}
        if t in ("array", "object", "integer", "number"):
            return {name: "잘못된 타입"}
    return None


async def q14(f: dict) -> dict:
    tok = issue_token(f["tenant"], f["run"])
    valid = {"inspect_dataset": {"dataset_id": f["dataset"]},
             "read_report": {"report_id": f["report"], "dataset_id": f["dataset"]},
             "check_schema": {"rule_version_id": f["rule_version"], "dataset_id": f["dataset"]},
             "search_work_rules": {"query": "매출액 정의", "top_k": 2},
             **({"get_source_excerpt": {"chunk_id": f["chunk"]}} if f["chunk"] else {}),
             **{k: v for k, v in f["tool_args"].items()}}
    cases = []
    for server in ("business", "rag"):
        async with AsyncExitStack() as stack:
            tools = (await (await session(stack, server, tok)).list_tools()).tools
        for t in tools:
            s = t.input_schema
            props = s.get("properties", {})
            ok = (s.get("type") == "object" and bool(t.description) and set(s.get("required", [])) <= set(props)
                  and all(p.get("description") for p in props.values()))
            cases.append({"tool": t.name, "case": "스키마: 설명·필수 인자·인자 설명", "pass": ok})
            if s.get("required"):
                err, msg, _ = await call(server, tok, t.name, {})
                cases.append({"tool": t.name, "case": "필수 인자 누락 → 오류 결과", "pass": err and bool(msg), "msg": msg})
            wrong = _wrong_type(s)
            if wrong:
                base = {k: v for k, v in valid.get(t.name, {}).items()}
                err, msg, _ = await call(server, tok, t.name, {**base, **wrong})
                cases.append({"tool": t.name, "case": "타입 오류 → 오류 결과", "pass": err and bool(msg), "msg": msg})
            if t.name in valid:
                err, msg, data = await call(server, tok, t.name, valid[t.name])
                cases.append({"tool": t.name, "case": "정상 입력 → JSON 객체 결과", "pass": (not err) and isinstance(data, dict), "msg": "" if not err else msg})
    for uri, expect_ok in ([(f"doc://{f['chunk']}", True)] if f["chunk"] else []) + [
            ("doc://chk_없는조각", False), ("knowledge://confirmed", True), ("knowledge://guideline", True)]:
        err, msg = await read("rag", tok, uri)
        cases.append({"tool": uri, "case": "리소스 읽기" + (" (정상)" if expect_ok else " (없는 자료 → 오류)"),
                      "pass": (not err) == expect_ok, "msg": msg if err else ""})
    return {"cases": len(cases), "passed": sum(c["pass"] for c in cases), "failed": [c for c in cases if not c["pass"]]}


# ---------------- Q-17 승인 ----------------

def _policy_matrix() -> list[dict]:
    out = []
    for (server, tool), rule in policy.TOOL_POLICY.items():
        if rule.kind != "write":
            continue
        for node in NODES:
            args = {rule.approval_arg: "ans_x"} if rule.approval_arg else {}
            allowed = policy.check(server, tool, args, node) is None
            out.append({"case": f"{tool} @ {node}", "expected": node in rule.nodes, "allowed": allowed,
                        "pass": allowed == (node in rule.nodes)})
        if rule.approval_arg:
            node = next(iter(rule.nodes))
            for v in (None, "", "  "):
                allowed = policy.check(server, tool, {rule.approval_arg: v} if v is not None else {}, node) is None
                out.append({"case": f"{tool} 승인값 {v!r}", "expected": False, "allowed": allowed, "pass": not allowed})
    for server, tool in (("rag", "register_document"), ("business", "delete_rule"), ("rag", "update_document")):
        allowed = policy.check(server, tool, {}, "save_results") is None
        out.append({"case": f"{server}.{tool} (Agent 미허용)", "expected": False, "allowed": allowed, "pass": not allowed})
    return out


async def q17(f: dict) -> dict:
    with session_scope() as s:
        qa = {}
        for key, choice in (("quality-approve", "other"), ("quality-reject", "reject")):
            q = Question(run_id=f["run"], job_id=f["job"], tenant_id=f["tenant"], key=key, kind="choose",
                         text="[품질 검증] 승인 시험 질문", reason="", options=[], context={}, status="answered")
            s.add(q)
            s.flush()
            a = Answer(question_id=q.id, run_id=f["run"], user_id=f["user"], choice=choice, text="[품질 검증] 승인 시험 답변")
            s.add(a)
            s.flush()
            qa[key] = (q.id, a.id)
    approve, reject = qa["quality-approve"][1], qa["quality-reject"][1]
    tok, other_run, other_ten = (issue_token(f["tenant"], f["run"]), issue_token(f["tenant"], f["other_run"]),
                                 issue_token("other-co", "run_other"))
    title = "[품질 검증] 승인 시험 지식"
    scenarios = [("가짜 답변 ID", tok, "ans_does_not_exist", False), ("빈 답변 ID", tok, "", False),
                 ("다른 회사 토큰", other_ten, approve, False), ("다른 실행 토큰", other_run, approve, False),
                 ("반려 답변", tok, reject, False), ("정상 승인", tok, approve, True)]
    server_cases = []
    try:
        for name, t, ans, expect in scenarios:
            err, msg, _ = await call("rag", t, "register_confirmed_rule", {"title": title, "content": "품질 검증", "answer_id": ans})
            server_cases.append({"case": name, "expected_registered": expect, "registered": not err, "pass": (not err) == expect, "msg": msg})
    finally:
        with session_scope() as s:
            ids = s.scalars(select(Document.id).where(Document.title == title)).all()
            s.execute(delete(DocumentChunk).where(DocumentChunk.document_id.in_(ids)))
            s.execute(delete(Document).where(Document.id.in_(ids)))
            for qid, aid in qa.values():
                s.execute(delete(Answer).where(Answer.id == aid))
                s.execute(delete(Question).where(Question.id == qid))
    # DB 전수 검사: 확정 지식마다 실제 승인 답변(같은 회사, 반려 아님)이 있는지
    audit = []
    with session_scope() as s:
        for d in s.scalars(select(Document).where(Document.kind == "confirmed")):
            m = re.search(r"\(확인: 답변 (ans_\w+)\)", d.content)
            a = s.get(Answer, m.group(1)) if m else None
            q = s.get(Question, a.question_id) if a else None
            ok = bool(a and q and q.tenant_id == d.tenant_id and a.choice != "reject")
            audit.append({"document_id": d.id, "answer_id": m.group(1) if m else None, "pass": ok})
    matrix = _policy_matrix()
    allc = matrix + server_cases
    return {"scenarios": len(allc), "passed": sum(c["pass"] for c in allc), "policy_matrix": len(matrix),
            "server_cases": server_cases, "failed": [c for c in allc if not c["pass"]],
            "confirmed_docs": len(audit), "unapproved_confirmed_docs": [a for a in audit if not a["pass"]]}


async def main(job_id: str) -> None:
    f = await fixtures(job_id)
    r11 = await q11(f)
    save("Q-11-mcp", {"verdict": verdict(r11["blocked"] == r11["requests"] and r11["controls_ok"]),
                      "summary": f"비인가 MCP 요청 {r11['requests']}건 중 차단 {r11['blocked']}건, 정상 토큰 대조군 {'통과' if r11['controls_ok'] else '실패'}",
                      "target": "100%", **r11})
    r14 = await q14(f)
    save("Q-14", {"verdict": verdict(r14["cases"] >= 60 and r14["passed"] == r14["cases"]),
                  "summary": f"MCP 계약 {r14['cases']}건 중 통과 {r14['passed']}건", "target": "100% (60건 이상)", **r14})
    r17 = await q17(f)
    ok = r17["passed"] == r17["scenarios"] and not r17["unapproved_confirmed_docs"] and r17["scenarios"] >= 30
    save("Q-17", {"verdict": verdict(ok),
                  "summary": f"승인 시나리오 {r17['scenarios']}건 중 통과 {r17['passed']}건, DB 확정 지식 {r17['confirmed_docs']}건 중 미승인 {len(r17['unapproved_confirmed_docs'])}건",
                  "target": "미승인 변경 0건 (시나리오 30건 이상)", **r17})


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
