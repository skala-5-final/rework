"""Q-09 작업 생성 API 응답시간: POST /jobs 100회의 P95 ≤ 1초.

    docker compose stop worker      # 만든 작업이 실제 분석으로 이어지지 않게 Worker를 멈춘다
    uv run python scripts/quality/api_perf.py
    docker compose up -d --scale worker=3

측정이 끝나면 만든 작업과 큐 메시지를 지운다 (분석 비용이 들지 않게).
"""

import time

import httpx
from sqlalchemy import delete, select

from common import Client, compose, p95, save, verdict
from rework.db.models import Event, Job, Outbox, Run
from rework.db.session import session_scope

N = 100
NAME = "[품질] API 응답시간"


def cleanup() -> int:
    with session_scope() as s:
        ids = s.scalars(select(Job.id).where(Job.name == NAME)).all()
        if ids:
            s.execute(delete(Event).where(Event.job_id.in_(ids)))
            for o in s.scalars(select(Outbox)):
                if o.payload.get("job_id") in ids:
                    s.delete(o)
            s.execute(Job.__table__.update().where(Job.id.in_(ids)).values(current_run_id=None))
            s.execute(delete(Run).where(Run.job_id.in_(ids)))
            s.execute(delete(Job).where(Job.id.in_(ids)))
    return len(ids)


def purge_queue() -> None:
    httpx.delete("http://localhost:15672/api/queues/%2F/rework.jobs/contents", auth=("guest", "guest"), timeout=10)


def main() -> None:
    running = "worker" in compose("ps", "--status", "running", "--services")
    if running:
        raise SystemExit("Worker가 실행 중입니다. docker compose stop worker 후 다시 실행하세요.")
    c = Client()
    src, rep = c.inputs()
    times = []
    for _ in range(N):
        t = time.perf_counter()
        r = c.http.post("/jobs", json={"name": NAME, "source_file_id": src, "report_file_id": rep})
        times.append(time.perf_counter() - t)
        r.raise_for_status()
    time.sleep(3)  # outbox 발행기가 큐로 옮길 시간
    purge_queue()
    removed = cleanup()
    v = p95(times)
    save("Q-09", {"verdict": verdict(v <= 1.0), "summary": f"POST /jobs {N}회, P95 {v * 1000:.0f}ms (목표 ≤ 1,000ms)",
                  "target": "P95 ≤ 1초", "n": N, "p95_ms": round(v * 1000, 1), "p50_ms": round(sorted(times)[N // 2] * 1000, 1),
                  "max_ms": round(max(times) * 1000, 1), "cleanup_jobs": removed,
                  "env": "로컬 Docker Desktop (같은 머신에서 호출)"})


if __name__ == "__main__":
    main()
