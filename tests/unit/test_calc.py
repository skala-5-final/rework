from rework.business_mcp.services.calc import build_slots, compare, execute, validate_rule
from rework.contracts.rules import CalcRule


def test_answer_key_reproduces_every_slot(report, df, answer_key):
    slots = build_slots(report, df)
    assert len(slots) == 16
    for s in slots:
        spec = answer_key["calc_rules"][s["metric_label"]]
        rule = CalcRule(slot_id=s["slot_id"], metric_name=s["metric_label"], **spec)
        result = compare(s, execute(df, s, rule))
        assert result["status"] == "matched", (s["metric_label"], result["diffs"])


def test_wrong_rule_is_detected(report, df):
    """할인 전 금액으로 매출을 계산하면 불일치로 잡혀야 한다."""
    slot = next(s for s in build_slots(report, df) if s["metric_label"] == "매출액")
    rule = CalcRule(slot_id=slot["slot_id"], metric_name="매출액", op="sum", column="gross_sales_amount_krw", scale=1000)
    assert compare(slot, execute(df, slot, rule))["status"] == "mismatched"


def test_disallowed_column_rejected():
    rule = CalcRule(slot_id="S1", metric_name="x", op="sum", column="item_name")
    assert validate_rule(rule)
