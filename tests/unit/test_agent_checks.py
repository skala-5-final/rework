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


def _verified_judgement(**changes):
    j = _judgement(False, [3, 1])
    return j.model_copy(update={"reason_quote": "몇 달째 감소", "reason_verified": True,
                                "evidence_summary": ["단팥빵 3개월 감소, 카스텔라 1개월 감소"], **changes})


def test_report_reason_verified_without_inventing_threshold():
    from rework.contracts.explain import claim_label
    out = check_claim(_claim(), _verified_judgement(), RESULTS, "단팥빵은 몇 달째 감소하여 발주를 줄입니다.")
    assert out["reason_verified"] and out["criterion"] is None
    assert claim_label(out) == "evidence_confirmed"


def test_missing_quote_or_wrong_target_never_promotes():
    for text, claim in [("단팥빵 발주 축소", _claim()), ("몇 달째 감소", {**_claim(), "targets": {"column": "item_name", "values": ["크림빵"]}})]:
        assert not check_claim(claim, _verified_judgement(), RESULTS, text)["reason_verified"]


def test_bad_numbers_or_comparison_block_verified_label():
    from rework.contracts.explain import claim_label
    for j in [_verified_judgement(cited_numbers=[999]),
              _verified_judgement(hypotheses=_judgement(True, [3]).hypotheses)]:
        out = check_claim(_claim(), j, RESULTS, "몇 달째 감소")
        assert not out["reason_verified"]
        assert claim_label(out) == "unknown"


def test_verified_reason_keeps_amount_question_separate():
    from rework.agent.nodes import _questions
    from rework.contracts.explain import claim_label
    out = check_claim({**_claim(), "statement": "단팥빵 발주를 1단위 줄인다"},
                      _verified_judgement(), RESULTS, "몇 달째 감소")
    assert claim_label(out) == "evidence_confirmed"
    questions = _questions({"key": "P4", "title": "베이커리"}, [out])
    assert len(questions) == 1 and "1단위" in questions[0]["text"]
    assert _questions({"key": "P4", "title": "베이커리"}, [{**out, "unresolved_details": []}]) == []


def test_complementary_evidence_keeps_explained():
    j = _verified_judgement()
    j.hypotheses.append(Hypothesis(statement="평균보다 낮음", result_ids=["r1"],
                                  holds_for_targets=True, holds_for_comparators=False, verdict="supported"))
    assert check_claim(_claim(), j, RESULTS, "몇 달째 감소")["reason_verified"]


def test_different_reasons_are_not_merged():
    a = {**_claim(False), "stated_reason": "연속 감소"}
    b = {**_claim(False), "stated_reason": "행사 효과 없음"}
    assert len(_merge_claims([a, b], "P4")) == 2
