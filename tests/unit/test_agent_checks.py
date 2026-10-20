from rework.agent.nodes import _derive_comparators, _merge_claims, check_claim
from rework.agent.schemas import ClaimJudgement, Hypothesis

RESULTS = {"r1": {"proposal": "P4", "tool": "analyze_trend", "args": {},
                  "result": {"rows": [{"name": "단팥빵", "consecutive_down": 3}, {"name": "카스텔라", "consecutive_down": 1}]}}}


def _claim(comparators=True):
    return {"claim_id": "P4-1", "statement": "단팥빵 축소", "decision": "축소", "stated_reason": "몇 달째",
            "targets": {"column": "item_name", "values": ["단팥빵"]},
            "comparators": {"column": "item_name", "values": ["카스텔라"]} if comparators else None}


def _judgement(holds_for_comparators, cited):
    return ClaimJudgement(claim_id="P4-1", status="explained", cited_numbers=cited, hypotheses=[
        Hypothesis(statement="3개월 연속 감소", result_ids=["r1"], holds_for_targets=True,
                   holds_for_comparators=holds_for_comparators, verdict="supported")])


def test_explained_when_discriminates():
    assert check_claim(_claim(), _judgement(False, [3, 1]), RESULTS)["status"] == "explained"


def test_ambiguous_when_comparators_also_fit():
    out = check_claim(_claim(), _judgement(True, [3]), RESULTS)
    assert out["status"] == "ambiguous"


def test_ungrounded_number_downgrades():
    out = check_claim(_claim(), _judgement(False, [7.7]), RESULTS)
    assert out["status"] == "ambiguous" and "7.7" in out["check_notes"][0]


def test_merge_and_derive_comparators():
    claims = [
        {"claim_id": "a", "statement": "단팥빵 축소", "decision": "축소", "targets": {"column": "item_name", "values": ["단팥빵"]}, "comparators": None},
        {"claim_id": "b", "statement": "카스텔라 유지", "decision": "유지", "targets": {"column": "item_name", "values": ["카스텔라"]}, "comparators": None},
        {"claim_id": "c", "statement": "크림빵 유지", "decision": "유지", "targets": {"column": "item_name", "values": ["크림빵"]}, "comparators": None},
    ]
    merged = _merge_claims(claims, "P4")
    assert len(merged) == 2 and merged[1]["targets"]["values"] == ["카스텔라", "크림빵"]
    _derive_comparators(merged)
    assert merged[0]["comparators"]["values"] == ["카스텔라", "크림빵"]


def test_unsigned_citation_is_grounded():
    results = {"r1": {"proposal": "P4", "tool": "analyze_trend", "args": {}, "result": {"rows": [{"last_mom_pct": -13.1}]}}}
    assert check_claim(_claim(comparators=False), _judgement(None, [13.1]), results)["check_notes"] == []
