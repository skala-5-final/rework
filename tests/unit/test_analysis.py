import pytest

from rework.business_mcp.services import analysis as A


def _row(res, name):
    return next(r for r in res["rows"] if r["name"] == name)


def test_trend_signals(df):
    res = A.trend(df, "item_name", ["단팥빵", "카스텔라", "제로콜라 500mL"], "2026-02", "2026-06")
    assert _row(res, "단팥빵")["consecutive_down"] == 3
    assert _row(res, "카스텔라")["consecutive_down"] == 1
    assert _row(res, "제로콜라 500mL")["consecutive_up"] == 4


def test_baseline_spike(df):
    res = A.baseline(df, "item_name", ["카스텔라"], "2026-05", ["2026-02", "2026-03", "2026-04"])
    assert _row(res, "카스텔라")["deviation_pct"] > 50


def test_promo_lift(df):
    res = A.promo_lift(df, "item_name", ["초코바", "단팥빵", "제육볶음 도시락"], "2026-06")
    assert _row(res, "초코바")["lift_pct"] > 150
    assert _row(res, "단팥빵")["lift_pct"] < -20
    assert abs(_row(res, "제육볶음 도시락")["lift_pct"]) < 5


def test_dow_friday(df):
    res = A.dow(df, "category_l2", ["도시락"], "2026-06")
    assert res["rows"][0]["name"] == "FRIDAY"


def test_unknown_item_is_reported(df):
    with pytest.raises(A.AnalysisError, match="없는 값"):
        A.trend(df, "item_name", ["없는상품"], "2026-02", "2026-06")
