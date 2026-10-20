"""1층: 보고서 표를 검증 슬롯으로 바꾸고, 계산 규칙을 실행·비교한다."""

import re
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

import pandas as pd

from rework.business_mcp.services.dataset import CATEGORICAL_COLUMNS
from rework.business_mcp.services.report_parser import parse_number
from rework.contracts.rules import ALLOWED_COLUMNS, CalcRule

DOW_KO = {"월": "MONDAY", "화": "TUESDAY", "수": "WEDNESDAY", "목": "THURSDAY",
          "금": "FRIDAY", "토": "SATURDAY", "일": "SUNDAY"}
TOTAL_LABELS = {"합계", "계", "총계"}
DERIVED_RE = re.compile(r"증감률|전월 대비|구성비|순위|단위")
MONTH_RE = re.compile(r"^(\d{1,2})월")
UNIT_RE = re.compile(r"\(([^)]*원|개|건|일|%)\)")
ROUNDING = {"half_up": ROUND_HALF_UP, "floor": ROUND_FLOOR, "ceil": ROUND_CEILING, "half_even": ROUND_HALF_EVEN}


def _group_column(values: list[str], df: pd.DataFrame) -> str | None:
    vals = [v for v in values if v and v not in TOTAL_LABELS]
    if not vals:
        return None
    for col in ["item_name", "category_l1", "category_l2", "day_of_week"]:
        if set(vals) <= set(df[col].astype(str)):
            return col
    return None


def _unit(text: str) -> str:
    m = UNIT_RE.search(text)
    return m.group(1) if m else ""


def build_slots(report: dict, df: pd.DataFrame) -> list[dict]:
    slots: list[dict] = []
    periods = report["periods"]

    def add(table, metric_label, unit, group_by, cells):
        cells = [c for c in cells if c["value"] is not None]
        if cells:
            slots.append({
                "slot_id": f"S{len(slots) + 1}", "table_id": table["table_id"], "table_title": table["title"],
                "metric_label": metric_label, "unit": unit, "group_by": group_by, "cells": cells,
            })

    for t in report["tables"]:
        cols, rows = t["columns"], t["rows"]
        month_cols = {i: periods.get(MONTH_RE.match(c).group(1) + "월") for i, c in enumerate(cols)
                      if MONTH_RE.match(c) and periods.get(MONTH_RE.match(c).group(1) + "월")}

        # C. 요일처럼 그룹 값이 열 머리글에 있는 넓은 표
        if len(cols) > 2 and all(c in DOW_KO for c in cols[1:]):
            for r in rows:
                add(t, r[0], _unit(r[0]), "day_of_week", [
                    {"group": DOW_KO[c], "period": report["period"], "raw": r[i], "value": parse_number(r[i]),
                     "is_percent": "%" in r[i]} for i, c in enumerate(cols) if i > 0])
            continue

        group_by, label_col = None, None
        for ci in range(len(cols)):
            g = _group_column([r[ci] for r in rows], df)
            if g:
                group_by, label_col = g, ci
                break

        # A. 행이 지표, 열이 월인 표 (주요 지표)
        if group_by is None:
            unit_col = next((i for i, c in enumerate(cols) if c == "단위"), None)
            for r in rows:
                unit = r[unit_col] if unit_col is not None else _unit(r[0])
                add(t, r[0], unit, None, [
                    {"group": None, "period": p, "raw": r[i], "value": parse_number(r[i]), "is_percent": "%" in r[i]}
                    for i, p in month_cols.items()])
            continue

        # B. 행이 그룹(품목·카테고리)인 표
        attr_cols = {ci for ci in range(len(cols)) if ci != label_col and _group_column([r[ci] for r in rows], df)}
        data_rows = [r for r in rows if r[label_col] not in TOTAL_LABELS]
        if month_cols:
            unit = _unit(cols[next(iter(month_cols))])
            add(t, f"{t['title']} · 월별 {'금액' if '원' in unit else '값'}({unit})", unit, group_by, [
                {"group": r[label_col], "period": p, "raw": r[i], "value": parse_number(r[i]), "is_percent": "%" in r[i]}
                for r in data_rows for i, p in month_cols.items()])
        for ci, c in enumerate(cols):
            if ci == label_col or ci in attr_cols or ci in month_cols or DERIVED_RE.search(c):
                continue
            add(t, c, _unit(c), group_by, [
                {"group": r[label_col], "period": report["period"], "raw": r[ci], "value": parse_number(r[ci]),
                 "is_percent": "%" in r[ci]} for r in data_rows])
    return slots


def _round(x: float, decimals: int, mode: str) -> float:
    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(x)).quantize(q, rounding=ROUNDING[mode]))


def _compute(sub: pd.DataFrame, rule: CalcRule) -> float | None:
    if sub.empty:
        return None
    if rule.op == "sum":
        v = float(sub[rule.column].sum())
    elif rule.op == "count_distinct":
        v = float(sub[rule.column].nunique())
    elif rule.op == "daily_mean":
        v = float(sub[rule.column].sum()) / sub["business_date"].nunique()
    else:
        den = float(sub[rule.denominator_column].sum())
        v = float(sub[rule.column].sum()) / den if den else 0.0
    v = v / rule.scale
    if rule.as_percent:
        v *= 100
    return _round(v, rule.decimals, rule.rounding)


def validate_rule(rule: CalcRule) -> str | None:
    if rule.column not in ALLOWED_COLUMNS:
        return f"허용되지 않은 열: {rule.column}"
    if rule.op == "ratio" and rule.denominator_column not in ALLOWED_COLUMNS:
        return f"ratio에는 허용된 denominator_column이 필요합니다: {rule.denominator_column}"
    return None


def execute(df: pd.DataFrame, slot: dict, rule: CalcRule, *, period: str | None = None) -> list[dict]:
    """슬롯의 각 칸(그룹×기간)을 규칙으로 계산한다. period를 주면 그 기간으로 바꿔 계산한다."""
    out = []
    for cell in slot["cells"]:
        p = period or cell["period"]
        sub = df[df["month"] == p]
        if slot["group_by"]:
            sub = sub[sub[slot["group_by"]].astype(str) == str(cell["group"])]
        out.append({"group": cell["group"], "period": p, "value": _compute(sub, rule)})
    return out


def compare(slot: dict, computed: list[dict]) -> dict:
    diffs = []
    matched = 0
    for cell, comp in zip(slot["cells"], computed):
        ok = comp["value"] is not None and abs(comp["value"] - cell["value"]) < 1e-6
        matched += ok
        if not ok:
            diffs.append({"group": cell["group"], "period": cell["period"], "report": cell["value"],
                          "computed": comp["value"],
                          "diff": None if comp["value"] is None else round(comp["value"] - cell["value"], 4)})
    total = len(slot["cells"])
    return {"slot_id": slot["slot_id"], "metric_label": slot["metric_label"], "cells": total, "matched": matched,
            "status": "matched" if matched == total else "mismatched", "diffs": diffs[:5]}


def missing_groups(slot: dict, df: pd.DataFrame) -> list[str]:
    """새 데이터에 기존 그룹 값(품목·카테고리 등)이 없는지 확인한다."""
    if not slot["group_by"]:
        return []
    have = set(df[slot["group_by"]].astype(str))
    return sorted({str(c["group"]) for c in slot["cells"]} - have)


__all__ = ["build_slots", "execute", "compare", "validate_rule", "missing_groups", "CATEGORICAL_COLUMNS"]
