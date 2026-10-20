"""실행 결과를 정답표(tests/fixtures/ground_truth)와 비교한다. Agent는 이 정답표를 읽지 않는다.

    uv run python scripts/eval_run.py <job_id>
"""

import asyncio
import json
import sys
from pathlib import Path

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from rework.agent.graph import build_graph
from rework.config import get_settings
from rework.db.models import Job
from rework.db.session import session_scope

KEY = Path(__file__).resolve().parents[1] / "tests/fixtures/ground_truth/2026-06_answer_key.json"
TOOL_TYPE = {"analyze_trend": "trend", "analyze_baseline": "baseline", "analyze_promo_lift": "promo_lift",
             "analyze_dow": "dow", "rank_items": "rank"}


async def load_state(job_id: str) -> dict:
    with session_scope() as s:
        run_id = s.get(Job, job_id).current_run_id
    async with AsyncPostgresSaver.from_conn_string(get_settings().checkpoint_url) as saver:
        return (await build_graph(saver).aget_state({"configurable": {"thread_id": run_id}})).values


def evaluate(state: dict, key: dict) -> dict:
    # 1층: 슬롯마다 수치 일치 + 정답 규칙과 같은 열·연산인지
    calc = []
    for s in state["slots"]:
        rule, st = state["calc_rules"].get(s["slot_id"]), state["calc_status"].get(s["slot_id"], {})
        exp = key["calc_rules"].get(s["metric_label"])
        same = bool(rule and exp and all(rule.get(k) == v for k, v in exp.items()))
        calc.append({"slot": s["metric_label"], "matched": st.get("status") == "matched", "same_as_key": same})
    # 2층: 제안마다 기대한 분석 유형으로 설명했는지, 핵심 품목을 다뤘는지
    rat = []
    for exp in key["proposals"]:
        r = next((x for x in state.get("rationales", []) if exp["title_contains"] in x["title"]), None)
        if r is None:
            rat.append({"proposal": exp["title_contains"], "found": False})
            continue
        crit_types = {c["criterion"]["type"] for c in r["claims"] if c.get("criterion") and c["status"] == "explained"}
        tools = {TOOL_TYPE[t["tool"]] for t in state["tool_results"].values() if t["proposal"] == r["key"]}
        covered = {v for c in r["claims"] for sc in (c.get("targets"), c.get("comparators")) if sc for v in sc["values"]}
        rat.append({"proposal": exp["title_contains"], "found": True,
                    "type_hit": bool(crit_types & set(exp["expected_types"])),
                    "tool_hit": bool(tools & set(exp["expected_types"])),
                    "items_covered": f"{len(set(exp['key_items']) & covered)}/{len(exp['key_items'])}",
                    "claims": {c["claim_id"]: c["status"] for c in r["claims"]},
                    "criteria": [c["criterion"]["description"] for c in r["claims"] if c.get("criterion")]})
    return {
        "calc_match_rate": f"{sum(c['matched'] for c in calc)}/{len(calc)}",
        "calc_same_as_key": f"{sum(c['same_as_key'] for c in calc)}/{len(calc)}",
        "rationale_type_hit": f"{sum(r.get('type_hit', False) for r in rat)}/{len(rat)}",
        "rationale_tool_hit": f"{sum(r.get('tool_hit', False) for r in rat)}/{len(rat)}",
        "questions": len(state.get("questions", [])),
        "calc": [c for c in calc if not (c["matched"] and c["same_as_key"])],
        "rationales": rat,
    }


if __name__ == "__main__":
    result = evaluate(asyncio.run(load_state(sys.argv[1])), json.loads(KEY.read_text()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
