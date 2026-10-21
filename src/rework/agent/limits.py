"""시간 한도와 검증용 장애 주입.

- within: 시간 안에 끝나지 않으면 기다리지 않고 TimeoutError (모델 호출 하드 제한, 시도 전체 실행시간 한도).
- 장애 주입: 품질 검증(Q-06 장애 복구)에서 실제 예외 클래스를 정해진 지점에 던진다. 작업을 만들 때 지정한 경우에만 동작한다.
"""

import asyncio

import httpx2
import openai

from rework.agent.errors import TransientError

FAULT_KINDS = ("llm_429", "llm_503", "mcp_error")


async def within(coro, seconds: float):
    """asyncio.wait_for는 취소가 늦게 처리되는 작업이면 그만큼 같이 기다리므로, 남은 작업은 취소 요청만 하고 버린다."""
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if not done:
        task.cancel()
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # 버린 작업의 예외 경고를 막는다
        raise TimeoutError
    if task.cancelled():
        # 바깥(Worker 종료)에서 취소됐다면 위의 await에서 이미 CancelledError가 났다. 여기 왔다면 작업 안에서 취소된 것:
        # MCP 서버가 사라지면 클라이언트 라이브러리가 진행 중인 호출을 취소한다. Worker를 죽이지 말고 일시 장애로 재시도한다.
        raise TransientError("작업 안에서 호출이 취소되었습니다 (연결 끊김)")
    return task.result()


def llm_fault(kind: str) -> Exception:
    """OpenAI SDK가 실제로 던지는 예외와 같은 클래스. 하네스의 오류 분류(일시 장애 → 재시도)를 그대로 거친다."""
    status = {"llm_429": 429, "llm_503": 503}[kind]
    resp = httpx2.Response(status, request=httpx2.Request("POST", "https://api.openai.com/v1/chat/completions"))
    cls = openai.RateLimitError if status == 429 else openai.InternalServerError
    return cls(f"[장애 주입] HTTP {status}", response=resp, body=None)
