from functools import lru_cache
from pathlib import Path

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

    database_url: str = "postgresql+psycopg://rework:rework@localhost:5433/rework"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    jwt_secret: str = "change-me"
    jwt_ttl_minutes: int = 60 * 12

    storage_dir: Path = ROOT / "var" / "storage"

    # Agent 실행 한도 (업무 한도: 넘으면 미확인으로 정리하고 정상 종료)
    calc_max_iterations: int = 3
    investigate_max_tool_calls: int = 8
    run_max_tool_calls: int = 120
    llm_timeout_seconds: int = 45
    llm_max_retries: int = 2
    # 안전망: 그래프 전체 단계 수
    graph_recursion_limit: int = 200

    # 큐 재시도
    retry_delays_seconds: tuple[int, ...] = (10, 30, 60)
    run_lease_seconds: int = 600

    @property
    def checkpoint_url(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
