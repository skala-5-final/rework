"""Agent 쪽 MCP 연결. 모든 도구 호출을 세고 기록한다.

같은 서버 코드를 설정(mcp_transport)에 따라 두 방식으로 연결한다.
- stdio: 실행마다 서버를 자식 프로세스로 띄우고, 회사·실행 정보를 환경 변수로 넣는다 (로컬 개발).
- http: 따로 떠 있는 서버에 Streamable HTTP로 연결하고, 서명한 실행 토큰을 헤더로 보낸다 (compose 기본).
"""

import json
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from rework.agent.errors import ToolCallError, TransientError
from rework.auth.mcp_scope import issue_token
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

    async def _transport(self, name: str, module: str):
        cfg = get_settings()
        if cfg.mcp_transport == "http":
            # 토큰은 연결(실행 시작·재개)마다 새로 발급한다. 답변 대기가 길어져도 만료된 토큰을 쓰지 않는다.
            token = issue_token(self.ctx.tenant_id, self.ctx.run_id)
            http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"},
                                          timeout=httpx2.Timeout(30.0, read=cfg.mcp_timeout_seconds))
            await self._stack.enter_async_context(http)  # 직접 만든 클라이언트는 전송 계층이 닫지 않는다
            url = {"business": cfg.business_mcp_url, "rag": cfg.rag_mcp_url}[name]
            return streamable_http_client(url, http_client=http)
        env = {"REWORK_TENANT_ID": self.ctx.tenant_id, "REWORK_RUN_ID": self.ctx.run_id, "MCP_TRANSPORT": "stdio"}
        return StdioServerParameters(command=sys.executable, args=["-m", module], env=env)

    async def __aenter__(self):
        try:
            for name, module in SERVERS.items():
                client = Client(await self._transport(name, module),
                                read_timeout_seconds=get_settings().mcp_timeout_seconds)
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
