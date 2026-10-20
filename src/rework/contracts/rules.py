"""규칙·근거 계약. Agent(모델 출력)와 business-mcp(실행)가 함께 쓴다."""

from typing import Literal

from pydantic import BaseModel, Field

ALLOWED_COLUMNS = [
    "sales_quantity", "gross_sales_amount_krw", "discount_amount_krw", "net_sales_amount_krw",
    "transaction_count", "sales_line_count", "promotion_sales_quantity", "promotion_line_count",
    "business_date", "item_id",
]


class CalcRule(BaseModel):
    """1층 계산 규칙: 보고서 지표 하나를 원천 데이터로 계산하는 방법."""

    slot_id: str = Field(description="검증할 보고서 지표 슬롯 ID (예: S1)")
    metric_name: str = Field(description="지표 이름 (예: 매출액)")
    op: Literal["sum", "count_distinct", "daily_mean", "ratio"] = Field(
        description="sum=합계, count_distinct=고유값 수, daily_mean=합계÷영업일수, ratio=column 합계÷denominator_column 합계")
    column: str = Field(description="계산에 쓸 원천 열")
    denominator_column: str | None = Field(None, description="op=ratio일 때 분모 열")
    scale: Literal[1, 1000] = Field(1, description="표시 단위로 나눌 값 (천원이면 1000)")
    as_percent: bool = Field(False, description="ratio를 %로 표시하면 true")
    decimals: int = Field(0, ge=0, le=2, description="표시 소수 자릿수")
    rounding: Literal["half_up", "floor", "ceil", "half_even"] = "half_up"
    reasoning: str = Field("", description="이 규칙을 고른 이유 (짧게)")


ANALYSIS_TYPES = ["trend", "baseline", "promo_lift", "dow", "rank"]


class Condition(BaseModel):
    field: str = Field(description="분석 결과 행의 필드 이름 (예: consecutive_down, lift_pct)")
    operator: Literal[">=", "<=", ">", "<", "=="]
    threshold: float


class DecisionCriterion(BaseModel):
    """2층 판단 기준: 다음 달에도 다시 적용할 수 있는 구조."""

    type: Literal["trend", "baseline", "promo_lift", "dow", "rank"]
    metric: Literal["sales_quantity", "net_sales_amount_krw", "gross_sales_amount_krw", "transaction_count"] = Field(
        "sales_quantity", description="분석에 쓴 원천 지표 열 (결과 필드 이름이 아님)")
    window_months: int = Field(3, ge=1, le=6, description="추세·기준선에 쓰는 개월 수")
    condition: Condition
    action: str = Field(description="조건을 만족하면 하는 일 (예: 발주 축소)")
    scope_column: Literal["item_name", "category_l1", "category_l2"] = Field("category_l1", description="적용 범위 열")
    scope_values: list[str] = Field(default_factory=list, description="적용 범위 값. 비우면 전체")
    description: str = Field(description="사람이 읽는 한 문장 기준")
