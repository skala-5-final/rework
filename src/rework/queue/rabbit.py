"""RabbitMQ 큐 구성과 outbox 발행기.

rework.jobs ← (TTL 만료) ← rework.retry.10s / 30s / 60s
rework.failed: 재시도 한도를 넘긴 메시지
"""

import asyncio
import json
import logging

import aio_pika
from sqlalchemy import select

from rework.config import get_settings
from rework.db.models import Outbox, now
from rework.db.session import session_scope

log = logging.getLogger("rework.queue")
EXCHANGE = "rework"
JOBS_QUEUE = "rework.jobs"
FAILED_QUEUE = "rework.failed"


def retry_key(delay: int) -> str:
    return f"retry.{delay}s"


async def connect() -> aio_pika.abc.AbstractRobustConnection:
    return await aio_pika.connect_robust(get_settings().rabbitmq_url)


async def declare(channel: aio_pika.abc.AbstractChannel) -> aio_pika.abc.AbstractExchange:
    ex = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    jobs = await channel.declare_queue(JOBS_QUEUE, durable=True)
    await jobs.bind(ex, "jobs")
    for d in get_settings().retry_delays_seconds:
        q = await channel.declare_queue(f"rework.retry.{d}s", durable=True, arguments={
            "x-message-ttl": d * 1000, "x-dead-letter-exchange": EXCHANGE, "x-dead-letter-routing-key": "jobs"})
        await q.bind(ex, retry_key(d))
    failed = await channel.declare_queue(FAILED_QUEUE, durable=True)
    await failed.bind(ex, "failed")
    return ex


async def publish(ex: aio_pika.abc.AbstractExchange, routing_key: str, payload: dict) -> None:
    """persistent 메시지로 발행한다. 채널이 publisher confirm 모드라 브로커 확인까지 기다린다."""
    await ex.publish(aio_pika.Message(body=json.dumps(payload).encode(), content_type="application/json",
                                      message_id=payload["message_id"], delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                     routing_key=routing_key)


def _unpublished(limit: int = 50) -> list[tuple[str, str, dict]]:
    with session_scope() as s:
        rows = s.scalars(select(Outbox).where(Outbox.published_at.is_(None)).order_by(Outbox.created_at).limit(limit))
        return [(r.id, r.routing_key, r.payload) for r in rows]


def _mark_published(outbox_id: str) -> None:
    with session_scope() as s:
        s.get(Outbox, outbox_id).published_at = now()


async def outbox_relay(stop: asyncio.Event) -> None:
    """DB outbox에 쌓인 이벤트를 큐로 옮긴다. 큐가 내려가 있어도 이벤트는 DB에 남아 나중에 발행된다."""
    while not stop.is_set():
        try:
            conn = await connect()
            async with conn:
                channel = await conn.channel(publisher_confirms=True)
                ex = await declare(channel)
                while not stop.is_set():
                    for oid, key, payload in await asyncio.to_thread(_unpublished):
                        await publish(ex, key, payload)
                        await asyncio.to_thread(_mark_published, oid)  # 확인 후 표시. 그 사이 장애 시 재발행 → 소비자가 중복 처리
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1.0)
                    except TimeoutError:
                        pass
        except Exception as e:  # noqa: BLE001
            log.warning("outbox relay 재연결 대기: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=3.0)
            except TimeoutError:
                pass
