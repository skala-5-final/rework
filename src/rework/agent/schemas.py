"""모델 구조화 출력 스키마."""

from typing import Literal

from pydantic import BaseModel, Field

from rework.contracts.rules import CalcRule, DecisionCriterion


class CalcRuleSet(BaseModel):
    rules: list[CalcRule] = Field(description="검증할 슬롯마다 규칙 하나")


class Scope(BaseModel):
    column: Literal["item_name", "category_l1", "category_l2"]
    values: list[str]


class Claim(BaseModel):
    claim_id: str = Field(description="예: P1-1")
    statement: str = Field(description="제안 속 결정 하나 (예: 단팥빵 발주를 줄인다)")
    decision: Literal["확대", "축소", "유지", "제외", "포함", "점검", "기타"]
    stated_reason: str = Field(description="보고서에 적힌 근거 표현 그대로 (예: 몇 달째 계속 줄고 있어)")
    targets: Scope = Field(description="결정 대상")
    comparators: Scope | None = Field(None, description="같은 범주에서 다른 결정을 받은 비교 대상. 없으면 null")


class ProposalPlan(BaseModel):
    key: str
    claims: list[Claim]


class ProposalSet(BaseModel):
    proposals: list[ProposalPlan]


class Hypothesis(BaseModel):
    statement: str = Field(description="근거 가설 (예: 3개월 연속 감소한 품목만 줄였다)")
    result_ids: list[str] = Field(description="이 가설을 검증한 도구 결과 ID (예: r3)")
    holds_for_targets: bool
    holds_for_comparators: bool | None = Field(None, description="비교 대상에도 성립하면 true. 비교 대상이 없으면 null")
    verdict: Literal["supported", "refuted", "inconclusive"]


class QuestionSpec(BaseModel):
    text: str = Field(description="담당자에게 물을 질문 한 문장 (40자 이내)")
    reason: str = Field(description="왜 묻는지 (20자 이내)")
    options: list[str] = Field(default_factory=list, description="담당자가 고를 이유 후보 2~3개 (각 25자 이내)")


class ClaimJudgement(BaseModel):
    claim_id: str
    hypotheses: list[Hypothesis]
    status: Literal["explained", "ambiguous", "unexplained"]
    reason_quote: str = Field("", description="결정의 이유를 명시한 보고서 원문 연속 인용. 이유가 없으면 빈 문자열")
    reason_verified: bool = Field(False, description="인용한 이유와 결정 방향을 해당 대상·기간·지표의 도구 결과로 검증했는지")
    unresolved_details: list[str] = Field(default_factory=list, description="근거와 별개로 미확인인 실행 수량·위치·선행조건. 예: 발주 20% 조정 기준")
    criterion: DecisionCriterion | None = Field(None, description="재사용 가능한 후보 기준. 근거 없는 임계값을 만들지 말고 불명확하면 null")
    cited_numbers: list[float] = Field(default_factory=list, description="판정에 인용한 숫자 (도구 결과에 실제로 있어야 함)")
    evidence_summary: list[str] = Field(default_factory=list, description="근거 데이터를 한 줄씩 (숫자 포함)")
    question: QuestionSpec | None = None


class Judgement(BaseModel):
    claims: list[ClaimJudgement]


class AnswerInterpretation(BaseModel):
    criterion_description: str = Field(description="담당자 답변을 정리한 판단 기준 한 문장")
    criterion: DecisionCriterion | None = Field(None, description="데이터로 다시 계산할 수 있는 기준이면 구조화")


class ManualSpec(BaseModel):
    purpose: str = Field(description="이 보고서가 무엇이고 누구에게 언제 보내는지 (2문장 이내)")
    preparation: str = Field(description="자료 준비 방법 (1문장)")
    steps: list[str] = Field(description="매달 만드는 순서 (4~6단계, 단계마다 40자 이내)")
    validation: str = Field(description="검증 방법 (1문장)")
    exceptions: list[str] = Field(description="예외 처리 (2~3개, 각 1문장)")
