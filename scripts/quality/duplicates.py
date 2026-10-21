"""Q-08 중복 처리: 완료된 작업의 큐 메시지를 그대로 50번 다시 보내도 규칙·결과물이 늘지 않는지.

    uv run python scripts/quality/duplicates.py [job_id]

RabbitMQ의 재전달(같은 message_id)을 흉내 낸다. 작업의 outbox에 남은 원래 메시지(start, resume)를 번갈아 보낸다.
"""

import asyncio
import json
import sys
import time

import aio_pika
from sqlalchemy import func, select

from common import OUT, Client, save, verdict
from rework.config import get_settings
from rework.db.models import Artifact, Document, Job, Outbox, Question, Rationale, RuleVersion
from rework.db.session import session_scope
from rework.queue import rabbit

N = 50


def snapshot(job_id: str) -> dict:
    with session_scope() as s:
        run_id = s.get(Job, job_id).current_run_id
        count = lambda m, cond: s.scalar(select(func.count()).select_from(m).where(cond))  # noqa: E731
        return {"rule_versions": count(RuleVersion, RuleVersion.job_id == job_id) if hasattr(RuleVersion, "job_id")
                else count(RuleVersion, RuleVersion.run_id == run_id),
                "rationales": count(Rationale, Rationale.run_id == run_id),
                "artifacts": count(Artifact, Artifact.job_id == job_id),
                "questions": count(Question, Question.run_id == run_id),
                "confirmed_docs": count(Document, Document.kind == "confirmed")}


async def send(payloads: list[dict]) -> None:
    conn = await aio_pika.connect_robust(get_settings().rabbitmq_url)
    async with conn:
        ch = await conn.channel(publisher_confirms=True)
        ex = await rabbit.declare(ch)
        for p in payloads:
            await rabbit.publish(ex, "jobs", p)


def main(job_id: str | None) -> None:
    c = Client()
    if not job_id:
        job_id = next(r["job_id"] for r in json.loads((OUT / "runs.json").read_text()) if r["final_status"] == "COMPLETED")
    with session_scope() as s:
        originals = [o.payload for o in s.scalars(select(Outbox)) if o.payload.get("job_id") == job_id]
    before = snapshot(job_id)
    ignored_before = sum(1 for e in c.events(job_id) if "중복 메시지 무시" in e["message"])
    payloads = [originals[i % len(originals)] for i in range(N)]
    asyncio.run(send(payloads))
    for _ in range(60):
        ignored = sum(1 for e in c.events(job_id) if "중복 메시지 무시" in e["message"]) - ignored_before
        if ignored >= N:
            break
        time.sleep(2)
    time.sleep(5)
    after = snapshot(job_id)
    reran = [e for e in c.events(job_id) if e["stage"] == "worker" and e["message"].startswith("작업 시작")
             and e["id"] > 0][-N:]
    side_effects = {k: after[k] - before[k] for k in before if after[k] != before[k]}
    save("Q-08", {"verdict": verdict(ignored >= N and not side_effects),
                  "summary": f"중복 메시지 {N}건 중 무시 {ignored}건, 규칙·결과물 변화 {len(side_effects)}종",
                  "target": "부작용 0건", "job_id": job_id, "sent": N,
                  "message_kinds": sorted({p['action'] for p in payloads}), "ignored": ignored,
                  "before": before, "after": after, "side_effects": side_effects})


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
