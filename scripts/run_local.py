"""큐·API 없이 Agent를 바로 실행한다 (개발·평가용).

    uv run python scripts/run_local.py                 # 분석 → 질문에서 멈춤
    uv run python scripts/run_local.py --auto-answer   # 모든 확인 질문에 '맞습니다'로 답하고 끝까지
"""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select

from rework.agent.mcp_client import RunContext
from rework.agent.runtime import run_analysis
from rework.db.models import Answer, Event, File, Job, Question, Run, User
from rework.db.session import init_db, session_scope
from rework.storage import save_bytes

ROOT = Path(__file__).resolve().parents[1]


def register(tenant: str, kind: str, path: Path) -> str:
    with session_scope() as s:
        f = File(tenant_id=tenant, kind=kind, original_name=path.name, path="", sha256="", size=0)
        s.add(f)
        s.flush()
        stored, digest = save_bytes(tenant, f.id, path.name, path.read_bytes())
        f.path, f.sha256, f.size = stored, digest, path.stat().st_size
        return f.id


def print_events(job_id: str, after: int) -> int:
    with session_scope() as s:
        for e in s.scalars(select(Event).where(Event.job_id == job_id, Event.id > after).order_by(Event.id)):
            print(f"  [{e.type:9}] {e.stage:20} {e.message}")
            after = e.id
    return after


async def main(args) -> None:
    init_db()
    with session_scope() as s:
        user = s.scalar(select(User).where(User.username == "demo"))
        tenant, uid = user.tenant_id, user.id
    src = register(tenant, "source", ROOT / args.source)
    rep = register(tenant, "report", ROOT / args.report)
    with session_scope() as s:
        job = Job(tenant_id=tenant, created_by=uid, name="로컬 실행", source_file_id=src, report_file_id=rep)
        s.add(job)
        s.flush()
        run = Run(job_id=job.id, tenant_id=tenant)
        s.add(run)
        s.flush()
        job.current_run_id = run.id
        job_id, run_id = job.id, run.id
    print("job", job_id, "run", run_id)
    ctx = RunContext(job_id=job_id, run_id=run_id, tenant_id=tenant)
    try:
        status = await run_analysis(ctx, "start")
    finally:
        last = print_events(job_id, 0)
    print("→", status)
    if status == "WAITING_FOR_INPUT" and args.auto_answer:
        with session_scope() as s:
            for q in s.scalars(select(Question).where(Question.run_id == run_id)):
                choice = "confirm" if q.kind == "confirm" else ""
                text = "" if choice else "(자동 응답) 담당자 경험상 판단"
                s.add(Answer(question_id=q.id, run_id=run_id, user_id=uid, choice=choice, text=text))
                q.status = "answered"
        status = await run_analysis(ctx, "resume")
        print_events(job_id, last)
        print("→", status)
    print("job_id:", job_id)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="data/판매분석가_일별상세.csv")
    ap.add_argument("--report", default="data/2026-06_ST001_월간판매실적보고서.pdf")
    ap.add_argument("--auto-answer", action="store_true")
    asyncio.run(main(ap.parse_args()))
