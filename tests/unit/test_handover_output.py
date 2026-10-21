from types import SimpleNamespace

import markdown

from rework.agent.nodes import _question_for, _questions, _rank
from rework.business_mcp.services.artifacts import build_manual
from rework.contracts import explain as X

P = {"key": "P4", "title": "베이커리"}


def _claim(cid, status, **kw):
    return {"claim_id": cid, "statement": f"{cid} 결정", "status": status, "evidence": ["e1", "e2", "e3", "e4"],
            "hypotheses": [], "question": None, **kw}


def test_explained_claims_are_not_asked():
    qs = _questions(P, [_claim("P4-1", "explained"), _claim("P4-2", "unexplained")])
    assert [q["key"] for q in qs] == ["P4-2"]


def test_questions_are_multiple_choice_with_other():
    amb = _claim("P4-1", "ambiguous", hypotheses=[
        {"statement": "3개월 연속 감소", "verdict": "supported"}, {"statement": "기준선 대비 -15%", "verdict": "supported"},
        {"statement": "요일 편차", "verdict": "refuted"}])
    q = _question_for(P, amb)
    assert [o["label"] for o in q["options"]] == ["3개월 연속 감소", "기준선 대비 -15%", "기타 (직접 입력)"]
    q = _question_for(P, _claim("P4-2", "unexplained"))
    assert len(q["options"]) == 4 and q["options"][-1]["value"] == "other"
    assert len(q["context"]["evidence"]) == 3


def test_unexplained_questions_come_first():
    qs = [_question_for(P, _claim("a", "ambiguous")), _question_for(P, _claim("b", "unexplained"))]
    assert [q["key"] for q in _rank(qs)] == ["b", "a"]


RULE = {"op": "sum", "column": "net_sales_amount_krw", "denominator_column": None, "scale": 1000,
        "as_percent": False, "decimals": 0, "rounding": "half_up"}
SLOT = {"table_title": "카테고리별 실적", "metric_label": "월별 금액(천원)", "group_by": "category_l1"}


def test_rule_in_business_words_and_excel():
    assert X.rule_plain(RULE) == "순매출 합계"
    assert X.rule_excel(RULE, SLOT) == '=ROUND(SUMIFS(net_sales_amount_krw,month,"당월",category_l1,"대분류")/1000,0)'
    ratio = {**RULE, "op": "ratio", "column": "promotion_sales_quantity", "denominator_column": "sales_quantity",
             "scale": 1, "as_percent": True, "decimals": 1}
    assert X.rule_plain(ratio) == "행사 판매수량 합계 ÷ 판매수량 합계 × 100"


def test_common_format_is_stated_once():
    pct = {**RULE, "scale": 1, "as_percent": True, "decimals": 1}
    assert X.common_format([RULE, RULE, pct]) == (1000, 0, "half_up", False)
    assert X.common_format([RULE]) is None


def test_labels():
    assert X.calc_label("matched") == "confirmed" and X.calc_label("mismatched") == "unknown"
    assert X.claim_label({"status": "explained", "confirmation": {"status": "unconfirmed"}}) == "estimated"
    assert X.claim_label({"status": "unexplained", "confirmation": {"status": "unconfirmed"}}) == "unknown"
    assert X.claim_label({"status": "user_explained", "confirmation": {"status": "confirmed"}}) == "confirmed"


def test_manual_puts_rule_first_and_folds_evidence():
    rv = SimpleNamespace(version=3, spec={
        "report": {"title": "월간 보고서", "period": "2026-06", "prev_period": "2026-05"},
        "calc_rules": [{"slot": SLOT, "rule": RULE, "validation_status": "matched"}] * 2,
        "dataset_columns": ["net_sales_amount_krw"], "unresolved": []})
    claim = {"claim_id": "P4-1", "statement": "단팥빵 발주 1단위 축소", "status": "explained", "stated_reason": "몇 달째 감소",
             "criterion": {"description": "3개월 연속 감소하면 발주를 줄인다"}, "evidence": ["6.68→5.33"],
             "confirmation": {"status": "unconfirmed"}}
    rat = SimpleNamespace(proposal_key="P4", data={"title": "베이커리", "claims": [claim]})
    md = build_manual(rv, [rat], {"purpose": "목적", "steps": ["a"]}, ["참치캔 점검 이유는?"])
    assert "- 🟡 추정 **단팥빵 발주 1단위 축소** — 3개월 연속 감소하면 발주를 줄인다" in md
    assert "공통: 천원 단위, 소수 0자리 반올림" in md
    assert "- 참치캔 점검 이유는?" in md
    html = markdown.markdown(md, extensions=["tables", "fenced_code", "md_in_html"])
    assert "<details>" in html and "<li>데이터: 6.68→5.33</li>" in html
