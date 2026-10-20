from langgraph.graph import END, START, StateGraph

from rework.agent import nodes as N
from rework.agent.state import RunState


def build_graph(checkpointer=None):
    g = StateGraph(RunState)
    for fn in (N.load_inputs, N.retrieve_context, N.propose_calc_rules, N.verify_calc_rules,
               N.decompose_proposals, N.investigate, N.call_tools, N.judge_rationale,
               N.ask_user, N.apply_answers, N.save_results, N.build_artifacts):
        g.add_node(fn.__name__, fn)

    # A. 입력 준비
    g.add_edge(START, "load_inputs")
    g.add_edge("load_inputs", "retrieve_context")
    g.add_edge("retrieve_context", "propose_calc_rules")
    # B. 1층 계산 규칙: 불일치하면 최대 N회 다시 제안
    g.add_edge("propose_calc_rules", "verify_calc_rules")
    g.add_conditional_edges("verify_calc_rules", N.route_calc, ["propose_calc_rules", "decompose_proposals"])
    # C. 2층 판단 근거: 제안마다 investigate ⇄ call_tools 반복 후 판정
    g.add_edge("decompose_proposals", "investigate")
    g.add_conditional_edges("investigate", N.route_investigate, ["call_tools", "judge_rationale"])
    g.add_conditional_edges("call_tools", N.route_after_tools, ["investigate", "judge_rationale"])
    g.add_conditional_edges("judge_rationale", N.route_rationale, ["investigate", "ask_user", "save_results"])
    # D. 담당자 확인: ask_user에서 중단 → 답변이 오면 재개
    g.add_edge("ask_user", "apply_answers")
    g.add_edge("apply_answers", "save_results")
    # E. 결과물
    g.add_edge("save_results", "build_artifacts")
    g.add_edge("build_artifacts", END)
    return g.compile(checkpointer=checkpointer)
