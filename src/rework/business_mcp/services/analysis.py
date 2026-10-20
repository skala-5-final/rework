"""2층: 제안의 판단 근거 후보를 검증하는 분석 도구.

모든 값은 '월별 일평균'(월 합계 ÷ 그 달 영업일수) 기준이다.
"""

import pandas as pd

from rework.business_mcp.services.dataset import month_days

ANALYSIS_METRICS = ["sales_quantity", "net_sales_amount_krw", "gross_sales_amount_krw", "transaction_count"]
SCOPE_COLUMNS = ["item_name", "category_l1", "category_l2"]
GROUP_COLUMNS = ["item_name", "category_l1", "category_l2", "all"]


class AnalysisError(ValueError):
    pass


def _check(df: pd.DataFrame, metric: str, scope_column: str, scope_values: list[str], months: list[str]):
    if metric not in ANALYSIS_METRICS:
        raise AnalysisError(f"metric은 {ANALYSIS_METRICS} 중 하나여야 합니다.")
    if scope_column not in SCOPE_COLUMNS:
        raise AnalysisError(f"scope_column은 {SCOPE_COLUMNS} 중 하나여야 합니다.")
    unknown = sorted(set(scope_values) - set(df[scope_column].astype(str)))
    if unknown:
        raise AnalysisError(f"{scope_column}에 없는 값: {unknown}. 데이터의 정확한 이름을 사용하세요.")
    have = set(df["month"])
    missing = [m for m in months if m not in have]
    if missing:
        raise AnalysisError(f"데이터에 없는 월: {missing}. 가능한 월: {sorted(have)}")


def _scope(df, scope_column, scope_values):
    return df[df[scope_column].astype(str).isin(scope_values)] if scope_values else df


def _key(df, group_by):
    return pd.Series("전체", index=df.index) if group_by == "all" else df[group_by].astype(str)


def _months_between(start: str, end: str) -> list[str]:
    return [p.strftime("%Y-%m") for p in pd.period_range(start, end, freq="M")]


def _monthly_daily_avg(df, metric, group_by, months):
    days = month_days(df)
    sub = df[df["month"].isin(months)]
    totals = sub.groupby([_key(sub, group_by), "month"])[metric].sum().unstack(fill_value=0)
    return totals.apply(lambda col: col / days[col.name]).reindex(columns=months)


def _streak(values: list[float], up: bool) -> int:
    n = 0
    for a, b in zip(values[-2::-1], values[:0:-1]):  # 끝에서부터 비교
        if (b > a) if up else (b < a):
            n += 1
        else:
            break
    return n


def trend(df, scope_column, scope_values, start_month, end_month, metric="sales_quantity", group_by="item_name"):
    """기간 동안의 월별 일평균 추세와, 마지막 달 기준 연속 증가·감소 개월 수."""
    months = _months_between(start_month, end_month)
    _check(df, metric, scope_column, scope_values, months)
    table = _monthly_daily_avg(_scope(df, scope_column, scope_values), metric, group_by, months)
    rows = []
    for key, s in table.iterrows():
        vals = [round(float(v), 2) for v in s.values]
        rows.append({
            "name": key,
            "series": dict(zip(months, vals)),
            "consecutive_up": _streak(vals, True),
            "consecutive_down": _streak(vals, False),
            "change_pct": round((vals[-1] / vals[0] - 1) * 100, 1) if vals[0] else None,
            "last_mom_pct": round((vals[-1] / vals[-2] - 1) * 100, 1) if len(vals) > 1 and vals[-2] else None,
        })
    return {"analysis": "trend", "metric": metric, "unit": "월별 일평균", "months": months, "rows": rows}


def baseline(df, scope_column, scope_values, target_month, baseline_months: list[str], metric="sales_quantity",
             group_by="item_name"):
    """target_month가 기준 기간 평균(평소 수준)에서 얼마나 벗어났는지."""
    months = sorted(set(baseline_months + [target_month]))
    _check(df, metric, scope_column, scope_values, months)
    table = _monthly_daily_avg(_scope(df, scope_column, scope_values), metric, group_by, months)
    rows = []
    for key, s in table.iterrows():
        base = float(s[baseline_months].mean())
        tgt = float(s[target_month])
        rows.append({
            "name": key, "baseline_avg": round(base, 2), "target_value": round(tgt, 2),
            "deviation_pct": round((tgt / base - 1) * 100, 1) if base else None,
            "series": {m: round(float(s[m]), 2) for m in months},
        })
    return {"analysis": "baseline", "metric": metric, "target_month": target_month,
            "baseline_months": baseline_months, "rows": rows}


def promo_lift(df, scope_column, scope_values, month, metric="sales_quantity"):
    """같은 달 안에서 행사일과 비행사일의 일평균을 비교한다. 행사일 = promotion_sales_quantity > 0."""
    _check(df, metric, scope_column, scope_values, [month])
    sub = _scope(df, scope_column, scope_values)
    sub = sub[sub["month"] == month]
    rows, no_promo = [], []
    for name, g in sub.groupby("item_name"):
        promo = g[g["promotion_sales_quantity"] > 0]
        if promo.empty:
            no_promo.append(name)
            continue
        normal = g[g["promotion_sales_quantity"] == 0]
        a, b = float(promo[metric].mean()), float(normal[metric].mean()) if len(normal) else 0.0
        rows.append({"name": name, "promo_days": int(len(promo)), "promo_day_avg": round(a, 2),
                     "normal_day_avg": round(b, 2), "lift_pct": round((a / b - 1) * 100, 1) if b else None})
    rows.sort(key=lambda r: -(r["lift_pct"] or 0))
    return {"analysis": "promo_lift", "metric": metric, "month": month, "rows": rows, "items_without_promo": no_promo}


def dow(df, scope_column, scope_values, month, metric="sales_quantity"):
    """범위 전체 합계를 날짜별로 더한 뒤 요일별 일평균을 낸다."""
    _check(df, metric, scope_column, scope_values, [month])
    sub = _scope(df, scope_column, scope_values)
    sub = sub[sub["month"] == month]
    daily = sub.groupby(["business_date", "day_of_week"])[metric].sum().reset_index()
    by = daily.groupby("day_of_week")[metric].mean()
    overall = float(daily[metric].mean())
    rows = [{"name": d, "daily_avg": round(float(v), 2), "vs_overall_pct": round((v / overall - 1) * 100, 1)}
            for d, v in by.sort_values(ascending=False).items()]
    return {"analysis": "dow", "metric": metric, "month": month, "overall_daily_avg": round(overall, 2), "rows": rows}


def rank(df, month, metric="net_sales_amount_krw", group_by="item_name", scope_column="category_l1",
         scope_values: list[str] | None = None, compare_month: str | None = None, top_n: int = 15):
    """월 합계 순위와 비교월 대비 증감률(일평균 기준)."""
    scope_values = scope_values or []
    months = [month] + ([compare_month] if compare_month else [])
    _check(df, metric, scope_column, scope_values, months)
    if group_by not in GROUP_COLUMNS[:-1]:
        raise AnalysisError(f"group_by는 {GROUP_COLUMNS[:-1]} 중 하나여야 합니다.")
    table = _monthly_daily_avg(_scope(df, scope_column, scope_values), metric, group_by, months)
    totals = df[df["month"] == month].groupby(group_by)[metric].sum()
    rows = []
    for key, s in table.sort_values(month, ascending=False).head(top_n).iterrows():
        row = {"name": key, "month_total": float(totals.get(key, 0)), "daily_avg": round(float(s[month]), 2)}
        if compare_month:
            prev = float(s[compare_month])
            row["vs_compare_pct"] = round((float(s[month]) / prev - 1) * 100, 1) if prev else None
        rows.append(row)
    return {"analysis": "rank", "metric": metric, "month": month, "compare_month": compare_month, "rows": rows}
