"""Worker: rework.jobs 큐를 소비해 Agent를 실행한다.

- 메시지는 재전달될 수 있다고 가정한다: processed_messages로 중복을 막는다.
- 같은 실행을 두 Worker가 동시에 처리하지 않도록 runs.lease_until로 임대한다.
- 결과·체크포인트를 저장한 뒤 ACK한다. 일시적 장애는 지연 큐로 재시도한다.
"""

import asyncio
import json
import logging
import traceback
import uuid
from datetime import timedelta

import aio_pika
from sqlalchemy import or_, update

from rework.agent.errors import InputError, TransientError
from rework.agent.mcp_client import RunContext
from rework.agent.runtime import run_analysis, run_rerun
from rework.config import get_settings
from rework.db.models import Job, ProcessedMessage, Run, now
from rework.db.session import init_db, record_event, session_scope
from rework.queue import rabbit

log = logging.getLogger("rework.worker")


def _already_processed(message_id: str) -> bool:
    with session_scope() as s:
        return s.get(ProcessedMessage, message_id) is not None


def _acquire_lease(run_id: str) -> bool:
    with session_scope() as s:
        res = s.execute(update(Run).where(Run.id == run_id, or_(Run.lease_until.is_(None), Run.lease_until < now()))
                        .values(lease_until=now() + timedelta(seconds=get_settings().run_lease_seconds), status="RUNNING"))
        return res.rowcount == 1


def _finish(payload: dict, *, status: str, error: str | None = None) -> None:
    with session_scope() as s:
        run = s.get(Run, payload["run_id"])
        run.status, run.lease_until = status, None
        job = s.get(Job, payload["job_id"])
        job.status, job.error, job.attempts = status, error, payload["attempt"]
        if s.get(ProcessedMessage, payload["message_id"]) is None:
            s.add(ProcessedMessage(message_id=payload["message_id"]))


def _context(payload: dict) -> RunContext:
    with session_scope() as s:
        job = s.get(Job, payload["job_id"])
        job.status, job.attempts = "RUNNING", payload["attempt"]
        return RunContext(job_id=job.id, run_id=payload["run_id"], tenant_id=job.tenant_id,
                          attempt=payload["attempt"], inject_failure=job.inject_failure)


async def handle(message: aio_pika.abc.AbstractIncomingMessage, ex) -> None:
    payload = json.loads(message.body)
    job_id, run_id, attempt = payload["job_id"], payload["run_id"], payload["attempt"]
    delays = get_settings().retry_delays_seconds

    if _already_processed(payload["message_id"]):
        record_event(job_id, run_id, "status", "worker", f"중복 메시지 무시: {payload['message_id']}")
        await message.ack()
        return
    if not _acquire_lease(run_id):
        record_event(job_id, run_id, "status", "worker", "다른 Worker가 같은 실행을 처리 중이라 건너뜀")
        await message.ack()
        return

    ctx = _context(payload)
    record_event(job_id, run_id, "status", "worker", f"작업 시작 (action={payload['action']}, 시도 {attempt}회차)")
    try:
        if payload["action"] == "rerun":
            status = await run_rerun(ctx)
        else:
            status = await run_analysis(ctx, payload["action"])
        _finish(payload, status=status)
        record_event(job_id, run_id, "status", "worker",
                     "담당자 답변 대기 (체크포인트 저장 후 큐 메시지 반납)" if status == "WAITING_FOR_INPUT" else "완료")
    except TransientError as e:
        if attempt <= len(delays):
            delay = delays[attempt - 1]
            retry = {**payload, "message_id": f"msg_{uuid.uuid4().hex[:12]}", "attempt": attempt + 1}
            await rabbit.publish(ex, rabbit.retry_key(delay), retry)  # 재시도 발행 확인 후 기존 메시지 ACK
            _finish(payload, status="RETRY_SCHEDULED", error=str(e))
            record_event(job_id, run_id, "retry", "worker", f"일시적 장애로 {delay}초 후 재시도 ({attempt + 1}회차): {e}")
        else:
            await rabbit.publish(ex, "failed", {**payload, "error": str(e)})
            _finish(payload, status="FAILED", error=f"재시도 한도 초과: {e}")
            record_event(job_id, run_id, "error", "worker", f"재시도 한도 초과로 실패 큐로 이동: {e}")
    except InputError as e:
        _finish(payload, status="FAILED", error=str(e))
        record_event(job_id, run_id, "error", "worker", f"입력 수정 필요: {e}")
    except Exception as e:  # noqa: BLE001
        log.error("예상하지 못한 오류\n%s", traceback.format_exc())
        await rabbit.publish(ex, "failed", {**payload, "error": repr(e)})
        _finish(payload, status="FAILED", error=f"{type(e).__name__}: {e}")
        record_event(job_id, run_id, "error", "worker", f"처리 실패: {type(e).__name__}: {e}")
    await message.ack()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    init_db()
    conn = await rabbit.connect()
    async with conn:
        channel = await conn.channel(publisher_confirms=True)
        await channel.set_qos(prefetch_count=1)
        ex = await rabbit.declare(channel)
        queue = await channel.get_queue(rabbit.JOBS_QUEUE)
        log.info("Worker 대기 중: %s", rabbit.JOBS_QUEUE)
        async with queue.iterator() as it:
            async for message in it:
                await handle(message, ex)


if __name__ == "__main__":
    asyncio.run(main())
