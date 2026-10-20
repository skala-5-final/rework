"""Agent 쪽 MCP 연결. 두 서버를 stdio로 띄우고, 모든 도구 호출을 세고 기록한다."""

import json
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from rework.agent.errors import ToolCallError, TransientError
from rework.config import get_settings
from rework.db.session import record_event

SERVERS = {"business": "rework.business_mcp.server", "rag": "rework.rag_mcp.server"}


@dataclass
class RunContext:
    """신뢰된 실행 문맥. Worker가 DB에서 읽어 만든다. 모델 출력으로 바꾸지 않는다."""
    job_id: str
    run_id: str
    tenant_id: str
    attempt: int = 1
    inject_failure: bool = False


class ToolHub:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.clients: dict[str, Client] = {}
        self.calls = 0
        self._stack = AsyncExitStack()
        self._schemas: dict[str, list] = {}

    async def __aenter__(self):
        env = {"REWORK_TENANT_ID": self.ctx.tenant_id, "REWORK_RUN_ID": self.ctx.run_id}
        try:
            for name, module in SERVERS.items():
                params = StdioServerParameters(command=sys.executable, args=["-m", module], env=env)
                client = Client(params, read_timeout_seconds=120)
                self.clients[name] = await self._stack.enter_async_context(client)
                self._schemas[name] = (await self.clients[name].list_tools()).tools
        except Exception as e:
            await self._stack.aclose()
            raise TransientError(f"MCP 서버 연결 실패: {e}") from e
        record_event(self.ctx.job_id, self.ctx.run_id, "status", "mcp",
                     "MCP 서버 연결: " + ", ".join(f"{n}({len(t)}개 도구)" for n, t in self._schemas.items()))
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    def openai_tools(self, server: str, names: list[str]) -> list[dict]:
        """MCP 도구 설명을 그대로 모델용 함수 정의로 바꾼다 (도구 발견)."""
        return [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                  "parameters": t.input_schema}}
                for t in self._schemas[server] if t.name in names]

    async def call(self, server: str, tool: str, args: dict, *, node: str) -> dict:
        limit = get_settings().run_max_tool_calls
        if self.calls >= limit:
            raise ToolCallError(f"실행당 도구 호출 한도({limit}회)를 넘었습니다.")
        self.calls += 1
        if self.ctx.inject_failure and self.ctx.attempt == 1 and self.calls == 1:
            record_event(self.ctx.job_id, self.ctx.run_id, "error", node, f"[장애 시연] {server}.{tool} 일시적 연결 실패")
            raise TransientError(f"[장애 시연] {server}.{tool} 일시적 연결 실패")
        started = time.monotonic()
        try:
            res = await self.clients[server].call_tool(tool, args)
        except Exception as e:  # 프로세스 종료·시간 초과 등 연결 문제
            record_event(self.ctx.job_id, self.ctx.run_id, "error", node, f"{server}.{tool} 연결 실패: {e}")
            raise TransientError(f"{server}.{tool} 호출 실패: {e}") from e
        text = "".join(getattr(c, "text", "") for c in res.content)
        ms = int((time.monotonic() - started) * 1000)
        record_event(self.ctx.job_id, self.ctx.run_id, "tool_call", node, f"{server}.{tool}",
                     {"args": _short(args), "ok": not res.is_error, "ms": ms,
                      "error": text[:300] if res.is_error else None})
        if res.is_error:
            raise ToolCallError(text)
        if res.structured_content is not None:
            data = res.structured_content
            return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
        return json.loads(text) if text else {}


def _short(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        s = json.dumps(v, ensure_ascii=False)
        out[k] = v if len(s) < 200 else s[:200] + "…"
    return out
