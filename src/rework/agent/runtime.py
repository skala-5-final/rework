"""Worker가 부르는 Agent 실행 진입점. 시작·재개·재실행을 처리한다."""

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy import select

from rework.agent.errors import InputError, ToolCallError
from rework.agent.graph import build_graph
from rework.agent.mcp_client import RunContext, ToolHub
from rework.config import get_settings
from rework.db.models import Answer, Job, Question, Run
from rework.db.session import record_event, session_scope


def _answers_for(run_id: str) -> dict:
    with session_scope() as s:
        rows = s.execute(select(Question, Answer).join(Answer, Answer.question_id == Question.id)
                         .where(Question.run_id == run_id)).all()
        return {q.key: {"question_id": q.id, "answer_id": a.id, "choice": a.choice, "text": a.text} for q, a in rows}


def _graph_config(ctx: RunContext, hub: ToolHub) -> dict:
    """노드·모델 호출 기록(LangSmith)에 실행 정보를 붙인다. 최상위 trace는 Worker의 trace_run이 연다."""
    return {"configurable": {"thread_id": ctx.run_id, "hub": hub}, "recursion_limit": get_settings().graph_recursion_limit,
            "run_name": "agent_graph",
            "metadata": {"job_id": ctx.job_id, "run_id": ctx.run_id, "tenant_id": ctx.tenant_id, "trace_id": ctx.trace_id}}


async def run_analysis(ctx: RunContext, action: str) -> str:
    """그래프를 실행하고 최종 상태를 돌려준다: COMPLETED | WAITING_FOR_INPUT."""
    with session_scope() as s:
        job = s.get(Job, ctx.job_id)
        dataset_id, report_id = job.source_file_id, job.report_file_id
    cfg = get_settings()
    async with AsyncPostgresSaver.from_conn_string(cfg.checkpoint_url) as saver:
        await saver.setup()
        graph = build_graph(saver)
        async with ToolHub(ctx) as hub:
            config = _graph_config(ctx, hub)
            snapshot = await graph.aget_state(config)
            pending_interrupt = any(t.interrupts for t in snapshot.tasks)
            if action == "resume" and pending_interrupt:
                answers = _answers_for(ctx.run_id)
                record_event(ctx.job_id, ctx.run_id, "status", "resume", f"답변 {len(answers)}개로 재개")
                result = await graph.ainvoke(Command(resume={"answers": answers}), config)
            elif snapshot.next:
                # 재시도: 마지막 체크포인트부터 이어서 실행
                record_event(ctx.job_id, ctx.run_id, "status", "resume", f"체크포인트에서 이어서 실행: {list(snapshot.next)}")
                result = await graph.ainvoke(None, config)
            else:
                result = await graph.ainvoke({"run_id": ctx.run_id, "job_id": ctx.job_id,
                                              "dataset_id": dataset_id, "report_id": report_id}, config)
            after = await graph.aget_state(config)
    # 다음 노드가 남아 있으면 ask_user의 interrupt에서 멈춘 것
    if after.next or result.get("__interrupt__"):
        return "WAITING_FOR_INPUT"
    return "COMPLETED"


async def run_refresh(ctx: RunContext) -> str:
    """완료 후 들어온 답변을 반영한다. 체크포인트의 질문 단계(ask_user) 뒤부터 모든 답변으로 다시 실행해
    판단 근거·규칙 버전·매뉴얼을 같은 실행 안에서 덮어쓴다. 조사·계산은 다시 하지 않는다."""
    cfg = get_settings()
    async with AsyncPostgresSaver.from_conn_string(cfg.checkpoint_url) as saver:
        await saver.setup()
        graph = build_graph(saver)
        async with ToolHub(ctx) as hub:
            config = _graph_config(ctx, hub)
            snapshot = await graph.aget_state(config)
            if snapshot.next:
                # 이전 갱신이 중간에 실패했으면 그 체크포인트부터 이어서 실행한다.
                await graph.ainvoke(None, config)
            else:
                answers = _answers_for(ctx.run_id)
                record_event(ctx.job_id, ctx.run_id, "status", "refresh", f"답변 {len(answers)}개로 매뉴얼 갱신")
                await graph.aupdate_state(config, {"answers": answers}, as_node="ask_user")
                await graph.ainvoke(None, config)
    return "COMPLETED"


async def run_rerun(ctx: RunContext) -> str:
    """확인된 규칙 버전을 새 기간 데이터에 적용한다. 모델을 쓰지 않는다."""
    with session_scope() as s:
        run = s.get(Run, ctx.run_id)
        params = dict(run.params)
    async with ToolHub(ctx) as hub:
        record_event(ctx.job_id, ctx.run_id, "stage", "check_schema", "입력 변경 사항 확인 중")
        try:
            chk = await hub.call("business", "check_schema", {"rule_version_id": params["rule_version_id"],
                                                               "dataset_id": params["dataset_id"]}, node="check_schema")
            if not chk["ok"]:
                raise InputError(f"새 데이터가 규칙과 맞지 않습니다. 빠진 열: {chk['missing_columns']}, "
                                 f"처음 보는 요일 값: {chk['new_day_values']}. 임의로 처리하지 않았습니다.")
            if chk["missing_groups"]:
                record_event(ctx.job_id, ctx.run_id, "status", "check_schema",
                             f"기존 보고서에 있던 값 중 새 데이터에 없는 항목: {chk['missing_groups']}")
            record_event(ctx.job_id, ctx.run_id, "stage", "generate_report_draft", "다음 기간 보고서 초안 생성 중")
            res = await hub.call("business", "generate_report_draft", {
                "rule_version_id": params["rule_version_id"], "dataset_id": params["dataset_id"],
                "month": params["month"]}, node="generate_report_draft")
        except ToolCallError as e:
            raise InputError(str(e)) from e
    record_event(ctx.job_id, ctx.run_id, "status", "generate_report_draft",
                 f"초안 {res['rows']}행 생성" + (" (부분월)" if res["partial_month"] else ""))
    return "COMPLETED"
