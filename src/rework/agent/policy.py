"""도구 승인 정책. MCP 서버에 보내기 전에 ToolHub가 확인한다 (서버도 같은 조건을 다시 검사한다).

- 조회·계산 도구: 어느 단계에서든 부를 수 있다.
- 변경 도구: 정해진 코드 단계에서만 부른다. 모델이 고르는 조사 단계(investigate)에서는 부를 수 없다.
- 확정 업무 지식 등록: 담당자 답변(승인) ID가 있어야 한다. 서버가 그 답변이 이 실행의 것인지, 반려가 아닌지 확인한다.
목록에 없는 도구는 부르지 않는다.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
    kind: str  # read | write
    nodes: frozenset[str] = field(default_factory=frozenset)  # write일 때 부를 수 있는 단계
    approval_arg: str | None = None  # 승인 근거로 반드시 있어야 하는 인자


READ = Rule("read")


def _write(*nodes: str, approval_arg: str | None = None) -> Rule:
    return Rule("write", frozenset(nodes), approval_arg)


TOOL_POLICY: dict[tuple[str, str], Rule] = {
    **{("business", t): READ for t in (
        "inspect_dataset", "read_report", "execute_aggregation", "compare_report", "analyze_trend", "analyze_baseline",
        "analyze_promo_lift", "analyze_dow", "rank_items", "evaluate_criterion", "check_schema")},
    ("rag", "search_work_rules"): READ,
    ("rag", "get_source_excerpt"): READ,
    ("business", "save_rule_version"): _write("save_results"),
    ("business", "save_rationale"): _write("save_results"),
    ("business", "generate_artifacts"): _write("build_artifacts"),
    ("business", "generate_report_draft"): _write("generate_report_draft"),
    ("rag", "register_confirmed_rule"): _write("save_results", approval_arg="answer_id"),
    # register_document는 화면(API)에서 사람이 올릴 때만 쓴다. Agent에게는 열지 않는다.
}


def check(server: str, tool: str, args: dict, node: str) -> str | None:
    """막아야 하면 이유를, 통과하면 None을 돌려준다."""
    rule = TOOL_POLICY.get((server, tool))
    if rule is None:
        return f"허용 목록에 없는 도구입니다: {server}.{tool}"
    if rule.kind == "write" and node not in rule.nodes:
        return f"변경 도구 {tool}는 {', '.join(sorted(rule.nodes))} 단계에서만 실행합니다 (요청 단계: {node})"
    if rule.approval_arg and not str(args.get(rule.approval_arg) or "").strip():
        return f"{tool}는 담당자 승인({rule.approval_arg}) 없이 실행할 수 없습니다"
    return None


def needs_approval(server: str, tool: str) -> str | None:
    rule = TOOL_POLICY.get((server, tool))
    return rule.approval_arg if rule else None
