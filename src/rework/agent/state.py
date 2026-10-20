from typing import Any, TypedDict

from langgraph.managed import RemainingSteps


class RunState(TypedDict, total=False):
    # 실행 식별 (Worker가 넣는다)
    run_id: str
    job_id: str
    dataset_id: str
    report_id: str
    stage: str

    # A. 입력
    profile: dict
    report: dict
    slots: list[dict]
    evidence_docs: list[dict]

    # B. 1층 계산 규칙
    calc_rules: dict[str, dict]       # slot_id -> 마지막 규칙
    calc_status: dict[str, dict]      # slot_id -> 비교 결과
    calc_history: dict[str, list]     # slot_id -> 시도 기록
    calc_iteration: int
    calc_done: bool

    # C. 2층 판단 근거
    proposals: list[dict]
    proposal_index: int
    messages: list[Any]               # 현재 제안의 조사 대화
    tool_results: dict[str, dict]     # result_id -> {tool, args, result}
    proposal_tool_calls: int
    rationales: list[dict]

    # D. 담당자 확인
    questions: list[dict]             # 만들 질문 명세 (key 기준 중복 없음)
    question_ids: dict[str, str]      # key -> DB 질문 ID
    answers: dict[str, dict]          # key -> {question_id, answer_id, choice, text}

    # E. 결과
    unresolved: list[str]
    rule_version_id: str
    artifact_ids: dict

    remaining_steps: RemainingSteps
