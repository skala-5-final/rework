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
    text: str = Field(description="담당자에게 물을 질문")
    reason: str = Field(description="왜 묻는지")
    options: list[str] = Field(default_factory=list, description="선택지 (없으면 자유 입력)")


class ClaimJudgement(BaseModel):
    claim_id: str
    hypotheses: list[Hypothesis]
    status: Literal["explained", "ambiguous", "unexplained"]
    criterion: DecisionCriterion | None = Field(None, description="채택한 가설을 다음 달에도 적용할 수 있게 일반화한 기준")
    cited_numbers: list[float] = Field(default_factory=list, description="판정에 인용한 숫자 (도구 결과에 실제로 있어야 함)")
    evidence_summary: list[str] = Field(default_factory=list, description="근거 데이터를 한 줄씩 (숫자 포함)")
    question: QuestionSpec | None = None


class Judgement(BaseModel):
    claims: list[ClaimJudgement]


class AnswerInterpretation(BaseModel):
    criterion_description: str = Field(description="담당자 답변을 정리한 판단 기준 한 문장")
    criterion: DecisionCriterion | None = Field(None, description="데이터로 다시 계산할 수 있는 기준이면 구조화")


class ManualSpec(BaseModel):
    purpose: str = Field(description="업무 목적과 보고서 범위 (2~3문장)")
    preparation: str = Field(description="자료 준비 방법")
    steps: list[str] = Field(description="보고서 작성 단계 (5~8단계)")
    validation: str = Field(description="검증 방법")
    exceptions: list[str] = Field(description="예외 처리 (2~4개)")
