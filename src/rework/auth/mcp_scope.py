"""MCP 서버가 요청마다 믿을 실행 범위(회사·실행)를 정한다.

- stdio: Worker가 서버 프로세스를 띄울 때 넣은 환경 변수.
- HTTP: Worker가 서명한 실행 토큰 (Authorization: Bearer). 헤더는 클라이언트 입력이라 서명을 검증해야 믿는다.
범위를 정하지 못하면 도구를 실행하지 않는다. 모델이 보내는 인자로는 정하지 않는다.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from rework.config import get_settings

AUDIENCE = "rework-mcp"
log = logging.getLogger("rework.mcp")


class ScopeError(PermissionError):
    """실행 범위를 정할 수 없다."""


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    run_id: str
    trace_id: str = ""


def issue_token(tenant_id: str, run_id: str, trace_id: str = "") -> str:
    """Worker가 MCP 연결마다 발급한다. 실행 하나에만 쓰이는 짧은 수명의 토큰. trace_id는 서버 로그를 실행과 잇는 데 쓴다."""
    cfg = get_settings()
    return jwt.encode({"tenant": tenant_id, "run": run_id, "trace": trace_id, "aud": AUDIENCE,
                       "exp": datetime.now(UTC) + timedelta(seconds=cfg.mcp_token_ttl_seconds)},
                      cfg.mcp_token_secret, algorithm="HS256")


def resolve_scope(headers: Mapping[str, str] | None) -> Scope:
    cfg = get_settings()
    if cfg.mcp_transport == "http":
        auth = (headers or {}).get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise ScopeError("실행 토큰이 없습니다.")
        try:
            claims = jwt.decode(auth[7:], cfg.mcp_token_secret, algorithms=["HS256"], audience=AUDIENCE)
        except jwt.PyJWTError as e:
            raise ScopeError(f"실행 토큰이 올바르지 않습니다: {e}") from e
        tenant, run, trace = claims.get("tenant", ""), claims.get("run", ""), claims.get("trace", "")
    else:
        tenant, run = os.environ.get("REWORK_TENANT_ID", ""), os.environ.get("REWORK_RUN_ID", "")
        trace = os.environ.get("REWORK_TRACE_ID", "")
    if not tenant:
        raise ScopeError("회사 정보가 없어 도구를 실행하지 않습니다.")
    log.info("요청 확인 trace=%s tenant=%s run=%s", trace or "-", tenant, run or "-")
    return Scope(tenant_id=tenant, run_id=run, trace_id=trace)
