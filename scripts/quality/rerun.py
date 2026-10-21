"""Q-05 다음 기간 재실행 정확도: 확정 규칙 버전을 다른 기간에 적용한 초안이 기대값과 일치하고 모델 호출이 0회인지.

    uv run python scripts/quality/rerun.py [job_id]

기대값은 Agent 결과와 별개로 정답표(tests/fixtures/ground_truth)의 규칙을 pandas로 직접 계산한다.
2026-06은 보고서 원본 숫자(슬롯 칸 값)와도 대조한다.
"""

import csv
import io
import json
import sys
import time
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd
from sqlalchemy import select

from common import OUT, ROOT, SOURCE_CSV, Client, save, verdict
from rework.db.models import Job, RuleVersion
from rework.db.session import session_scope

MONTHS = ["2026-04", "2026-05", "2026-07"]  # 보고서(2026-06)와 다른 기간
CONTROL = "2026-06"
KEY = json.loads((ROOT / "tests/fixtures/ground_truth/2026-06_answer_key.json").read_text())


def _round(x: float, d: int) -> float:
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP))


def expected(df: pd.DataFrame, rule: dict, group_col: str | None, group: str, period: str):
    sub = df[df["month"] == period]
    if group_col and group:
        sub = sub[sub[group_col].astype(str) == group]
    if sub.empty:
        return None
    op, col = rule["op"], rule["column"]
    if op == "sum":
        v = sub[col].sum()
    elif op == "count_distinct":
        v = sub[col].nunique()
    elif op == "daily_mean":
        v = sub[col].sum() / sub["business_date"].nunique()
    else:
        v = sub[col].sum() / sub[rule["denominator_column"]].sum() * (100 if rule.get("as_percent") else 1)
    return _round(float(v) / rule.get("scale", 1), rule.get("decimals", 0))


def main(job_id: str | None) -> None:
    c = Client()
    if not job_id:
        runs = json.loads((OUT / "runs.json").read_text())
        job_id = next(r["job_id"] for r in runs if r["final_status"] == "COMPLETED")
    with session_scope() as s:
        rv = s.scalar(select(RuleVersion).where(RuleVersion.run_id == s.get(Job, job_id).current_run_id))
        slots = {(r["slot"]["table_title"], r["slot"]["metric_label"]): r["slot"] for r in rv.spec["calc_rules"]}
    df = pd.read_csv(SOURCE_CSV, encoding="utf-8-sig", parse_dates=["business_date"])
    df["month"] = df["business_date"].dt.strftime("%Y-%m")
    src = c.upload("source", SOURCE_CSV)

    results, mismatches = [], []
    for month in MONTHS + [CONTROL]:
        run_id = c.post(f"/jobs/{job_id}/rerun", {"source_file_id": src, "month": month})["run_id"]
        name = f"{month}_보고서초안.csv"
        for _ in range(200):
            arts = [a for a in c.get(f"/jobs/{job_id}/artifacts") if a["run_id"] == run_id and a["filename"] == name]
            if arts:
                break
            time.sleep(2)
        else:
            raise TimeoutError(f"{month} 초안이 만들어지지 않았습니다")
        text = c.http.get(f"/artifacts/{arts[0]['artifact_id']}/download").content.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        llm = [e for e in c.events(job_id) if e["run_id"] == run_id and e["type"] == "llm"]
        checked = ok = 0
        for r in rows:
            slot = slots[(r["표"], r["지표"])]
            rule = KEY["calc_rules"].get(slot["metric_label"])
            if rule is None:
                continue
            exp = expected(df, rule, slot.get("group_by"), r["그룹"], r["기간"])
            got = float(r["값"]) if r["값"] not in ("", "None") else None
            checked += 1
            if exp == got:
                ok += 1
            else:
                mismatches.append({"month": month, **r, "expected": exp})
        # 보고서 원본 숫자 대조 (2026-06)
        report_ok = report_n = 0
        if month == CONTROL:
            for (title, label), slot in slots.items():
                for cell in slot["cells"]:
                    if cell.get("period") != CONTROL or cell.get("value") is None:
                        continue
                    row = next((r for r in rows if r["표"] == title and r["지표"] == label
                                and r["그룹"] == (cell.get("group") or "") and r["기간"] == CONTROL), None)
                    report_n += 1
                    if row and float(row["값"]) == float(cell["value"]):
                        report_ok += 1
        results.append({"month": month, "run_id": run_id, "rows": len(rows), "checked": checked, "matched": ok,
                        "llm_calls": len(llm), **({"report_cells": report_n, "report_matched": report_ok} if month == CONTROL else {})})
        print(f"  {month}: {ok}/{checked} 일치, 모델 호출 {len(llm)}회")

    total = sum(r["checked"] for r in results)
    hit = sum(r["matched"] for r in results)
    llm = sum(r["llm_calls"] for r in results)
    ctrl = results[-1]
    passed = total > 0 and hit == total and llm == 0 and ctrl.get("report_matched") == ctrl.get("report_cells")
    save("Q-05", {"verdict": verdict(passed),
                  "summary": f"새 기간 {len(MONTHS)}개 + 대조 1개: 값 {hit}/{total} 일치, 모델 호출 {llm}회, "
                             f"2026-06 보고서 원본 {ctrl.get('report_matched')}/{ctrl.get('report_cells')} 일치",
                  "target": "100% 일치, 모델 호출 0회", "base_job": job_id, "rule_version": rv.version,
                  "periods": results, "mismatches": mismatches[:20],
                  "note": "새 기간은 같은 원천 파일의 다른 월(4·5·7월, 7월은 부분월)이다. 기대값은 정답표 규칙으로 독립 계산했다."})


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
