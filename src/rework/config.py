from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # .env에 OPEN_API_KEY로 적어 둔 경우도 읽는다.
    openai_api_key: str = Field("", validation_alias=AliasChoices("OPENAI_API_KEY", "OPEN_API_KEY"))
    openai_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # 관측: LangSmith는 키가 있을 때만 켠다. 가격표는 100만 토큰당 (입력, 출력) USD, 모델 이름 앞부분으로 찾는다.
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "rework"
    llm_prices_per_mtok: dict[str, tuple[float, float]] = {
        "gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00),
        "gpt-4.1-mini": (0.40, 1.60), "gpt-4.1": (2.00, 8.00),
    }

    database_url: str = "postgresql+psycopg://rework:rework@localhost:5433/rework"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    jwt_secret: str = "change-me"
    jwt_ttl_minutes: int = 60 * 12

    storage_dir: Path = ROOT / "var" / "storage"

    # MCP 연결: stdio = Worker가 서버를 자식 프로세스로 띄움, http = 따로 떠 있는 서버에 Streamable HTTP로 연결
    mcp_transport: Literal["stdio", "http"] = "stdio"
    business_mcp_url: str = "http://localhost:8001/mcp"
    rag_mcp_url: str = "http://localhost:8002/mcp"
    business_mcp_port: int = 8001
    rag_mcp_port: int = 8002
    mcp_token_secret: str = "change-me-mcp-token-secret-32-bytes!"
    mcp_token_ttl_seconds: int = 1800
    mcp_timeout_seconds: int = 120

    # Agent 실행 한도 (업무 한도: 넘으면 미확인으로 정리하고 정상 종료)
    calc_max_iterations: int = 3
    investigate_max_tool_calls: int = 8
    run_max_tool_calls: int = 120
    llm_timeout_seconds: int = 45
    llm_max_retries: int = 2
    # 안전망: 그래프 전체 단계 수
    graph_recursion_limit: int = 200
    # 담당자에게 한 번에 묻는 질문 수. 나머지는 '나중에 확인'으로 남긴다.
    max_questions: int = 3

    # 컨텍스트 예산 (양 제한). 시연 데이터는 호출당 수천 토큰이라 넉넉한 안전망이다.
    context_token_budget: int = 100_000      # 모델 호출 한 번에 넣는 토큰 (gpt-4o-mini 창 128k보다 작게)
    tool_result_max_chars: int = 12_000      # 도구 결과 하나를 대화에 넣는 최대 글자 수
    run_token_budget: int = 2_000_000        # 시도 한 번의 누적 토큰. 넘으면 남은 조사를 건너뛰고 판정·저장한다

    # 실행시간 한도 (시도 하나). soft: 넘으면 남은 조사를 건너뛰고 판정·저장, max: 넘으면 중단하고 실패 처리
    run_soft_seconds: int = 600
    run_max_seconds: int = 1500

    # 큐 재시도
    retry_delays_seconds: tuple[int, ...] = (10, 30, 60)
    run_lease_seconds: int = 60  # 처리 중에는 lease/3마다 연장한다. Worker가 죽으면 이 시간 뒤 다른 Worker가 이어받는다

    @property
    def checkpoint_url(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
