"""규칙 버전·근거 저장과 결과물(매뉴얼·규칙 JSON·검증표·다음 달 초안) 생성."""

import csv
import io
import json
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import func, select

from rework.business_mcp.services import analysis as A
from rework.business_mcp.services.calc import execute, missing_groups
from rework.contracts.rules import CalcRule, DecisionCriterion
from rework.db.models import Answer, Artifact, Question, Rationale, RuleVersion, Run
from rework.storage import artifact_dir, to_ref

CONFIRM_CHOICES = {"confirm": "confirmed", "reject": "rejected"}


def confirmation_from_db(s, run_id: str, question_id: str | None) -> dict:
    """확인 상태는 DB의 실제 답변 기록으로만 정한다. 모델의 주장은 쓰지 않는다."""
    if not question_id:
        return {"status": "unconfirmed"}
    q = s.get(Question, question_id)
    if q is None or q.run_id != run_id:
        return {"status": "unconfirmed", "note": "확인 기록 없음"}
    a = s.scalar(select(Answer).where(Answer.question_id == question_id))
    if a is None:
        return {"status": "unconfirmed"}
    if a.choice in CONFIRM_CHOICES:
        status = CONFIRM_CHOICES[a.choice]
    elif a.text.strip():
        status = "confirmed"  # 담당자가 직접 근거를 적어 준 경우
    else:
        status = "unconfirmed"
    return {"status": status, "question_id": q.id, "answer_id": a.id, "answer_choice": a.choice,
            "answer_text": a.text, "answered_by": a.user_id}


def save_rule_version(s, *, tenant_id: str, run: Run, payload: dict) -> dict:
    existing = s.scalar(select(RuleVersion).where(RuleVersion.run_id == run.id))
    calc_rules = []
    for r in payload.get("calc_rules", []):
        rule = CalcRule.model_validate(r["rule"])
        conf = confirmation_from_db(s, run.id, r.get("confirmation_ref"))
        calc_rules.append({"slot": r["slot"], "rule": rule.model_dump(), "validation_status": r["validation_status"],
                           "confirmation": conf})
    spec = {
        "schema_version": 1,
        "report": payload.get("report", {}),
        "dataset_columns": payload.get("dataset_columns", []),
        "calc_rules": calc_rules,
        "unresolved": payload.get("unresolved", []),
        "scope": payload.get("scope", {}),
    }
    if existing:  # 같은 실행에서 다시 저장하면 덮어쓴다 (중복 생성 방지)
        existing.spec = spec
        rv = existing
    else:
        version = (s.scalar(select(func.max(RuleVersion.version)).where(RuleVersion.tenant_id == tenant_id)) or 0) + 1
        rv = RuleVersion(tenant_id=tenant_id, job_id=run.job_id, run_id=run.id, version=version, spec=spec)
        s.add(rv)
    s.flush()
    return {"rule_version_id": rv.id, "version": rv.version,
            "confirmed": sum(r["confirmation"]["status"] == "confirmed" for r in calc_rules),
            "total": len(calc_rules)}


def save_rationale(s, *, tenant_id: str, run: Run, proposal_key: str, rationale: dict) -> dict:
    claims = rationale.get("claims", [])
    for c in claims:
        if c.get("criterion"):
            c["criterion"] = DecisionCriterion.model_validate(c["criterion"]).model_dump()
        c["confirmation"] = confirmation_from_db(s, run.id, c.get("confirmation_ref"))
    statuses = {c["confirmation"]["status"] for c in claims}
    rationale["confirmation"] = {"status": "confirmed" if statuses == {"confirmed"} else
                                 "rejected" if statuses == {"rejected"} else "unconfirmed"}
    row = s.scalar(select(Rationale).where(Rationale.run_id == run.id, Rationale.proposal_key == proposal_key))
    if row:
        row.data = rationale
    else:
        row = Rationale(tenant_id=tenant_id, run_id=run.id, proposal_key=proposal_key, data=rationale)
        s.add(row)
    s.flush()
    return {"rationale_id": row.id, "claims": {c["claim_id"]: c["confirmation"]["status"] for c in claims}}


def _put_artifact(s, *, tenant_id, run, kind, filename, content: str) -> str:
    path = artifact_dir(tenant_id, run.id) / filename
    path.write_text(content, encoding="utf-8")
    art = s.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.kind == kind))
    if art:
        art.filename, art.path = filename, to_ref(path)
    else:
        art = Artifact(tenant_id=tenant_id, job_id=run.job_id, run_id=run.id, kind=kind, filename=filename, path=to_ref(path))
        s.add(art)
    s.flush()
    return art.id


STATUS_KO = {"matched": "일치", "mismatched": "불일치", "untested": "미검증",
             "confirmed": "확인됨", "rejected": "반려", "unconfirmed": "미확인"}
RAT_KO = {"explained": "데이터로 설명됨", "ambiguous": "판단 보류", "unexplained": "데이터로 설명 안 됨",
          "user_explained": "담당자 설명으로 확정"}


def _rule_text(rule: dict) -> str:
    col = rule["column"]
    base = {"sum": f"`{col}` 합계", "count_distinct": f"`{col}` 고유값 수",
            "daily_mean": f"`{col}` 합계 ÷ 영업일수",
            "ratio": f"`{col}` 합계 ÷ `{rule.get('denominator_column')}` 합계"}[rule["op"]]
    if rule.get("as_percent"):
        base += " × 100(%)"
    if rule["scale"] == 1000:
        base += ", 천원 단위"
    return base + f", 소수 {rule['decimals']}자리 {'반올림' if rule['rounding'] == 'half_up' else rule['rounding']}"


def build_manual(rv: RuleVersion, rationales: list[Rationale], manual_spec: dict) -> str:
    spec = rv.spec
    rep = spec.get("report", {})
    L = [f"# 인수인계 매뉴얼: {rep.get('title', '월간 판매 실적 보고서')}", "",
         f"> 규칙 버전 v{rv.version} · 생성 {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · Re:Work 자동 생성", ""]
    L += ["## 1. 업무 목적과 보고서 범위", manual_spec.get("purpose", "-"), ""]
    L += ["## 2. 필요한 입력 파일과 필수 열",
          "- 원천 데이터: POS 일별 판매 CSV (영업일 × 매장 × 상품)",
          "- 필수 열: " + ", ".join(f"`{c}`" for c in spec.get("dataset_columns", [])), ""]
    L += ["## 3. 자료 준비 방법", manual_spec.get("preparation", "-"), ""]
    L += ["## 4. 집계 기간과 날짜 기준",
          f"- 당월: {rep.get('period', '-')}, 비교월: {rep.get('prev_period', '-')}",
          "- 날짜 기준: `business_date`(영업일)의 연·월", ""]
    L += ["## 5. 계산 규칙", "", "| 지표 | 계산 방법 | 수치 검증 | 담당자 확인 |", "|---|---|---|---|"]
    for r in spec["calc_rules"]:
        L.append(f"| {r['slot']['table_title']} · {r['slot']['metric_label']} | {_rule_text(r['rule'])} | "
                 f"{STATUS_KO[r['validation_status']]} | {STATUS_KO[r['confirmation']['status']]} |")
    L += ["", "## 6. 판단 기준 (제안별 근거)", "",
          "보고서에는 결론만 적혀 있습니다. 아래는 담당자가 그 제안을 하기까지 본 데이터를 원천 데이터에서 되찾은 결과입니다.", ""]
    for rat in sorted(rationales, key=lambda r: r.proposal_key):
        d = rat.data
        L += [f"### {rat.proposal_key}. {d.get('title', '')}", f"> {d.get('proposal_text', '')}", ""]
        for c in d.get("claims", []):
            L.append(f"**{c['statement']}** — {RAT_KO.get(c['status'], c['status'])} · 담당자 확인: "
                     f"{STATUS_KO[c['confirmation']['status']]}")
            L.append(f"- 보고서에 적힌 근거: “{c.get('stated_reason', '')}”")
            if c.get("criterion"):
                k = c["criterion"]
                L.append(f"- **판단 기준:** {k['description']} "
                         f"(`{k['type']}` · `{k['condition']['field']} {k['condition']['operator']} {k['condition']['threshold']}`"
                         f" · 최근 {k['window_months']}개월)")
            if c.get("criterion_user"):
                L.append(f"- **담당자 설명 기준:** {c['criterion_user']}")
            for e in c.get("evidence", []):
                L.append(f"- 근거 데이터: {e}")
            if c.get("rejected_hypotheses"):
                L.append("- 기각한 가설: " + "; ".join(c["rejected_hypotheses"]))
            if c.get("check_notes"):
                L.append("- 검사 메모: " + "; ".join(c["check_notes"]))
            if c["confirmation"].get("answer_text"):
                L.append(f"- 담당자 답변: “{c['confirmation']['answer_text']}”")
            L.append("")
    L += ["## 7. 단계별 작업 순서"] + [f"{i}. {step}" for i, step in enumerate(manual_spec.get("steps", []), 1)] + [""]
    L += ["## 8. 검증 방법", manual_spec.get("validation", "계산 규칙을 다시 실행해 보고서 수치와 비교합니다."), ""]
    L += ["## 9. 예외 처리"] + [f"- {x}" for x in manual_spec.get("exceptions", [])] + [""]
    L += ["## 10. 미확인 사항"]
    unresolved = list(spec.get("unresolved", []))
    unresolved += [f"{r.proposal_key} {c['statement']} ({RAT_KO.get(c['status'], c['status'])}, 담당자 미확인)"
                   for r in rationales for c in r.data.get("claims", [])
                   if c["confirmation"]["status"] != "confirmed" and c["status"] != "explained"]
    L += [f"- {u}" for u in unresolved] or ["- 없음"]
    L += ["", "※ 파일 입수처·마감일·담당자 연락처는 입력 자료에 없어 기록하지 않았습니다."]
    return "\n".join(L)


def validation_rows(rv: RuleVersion, df: pd.DataFrame) -> list[dict]:
    """저장된 규칙을 다시 실행해 보고서 값과 칸별로 비교한다."""
    rows = []
    for r in rv.spec["calc_rules"]:
        rule, slot = CalcRule.model_validate(r["rule"]), r["slot"]
        for cell, comp in zip(slot["cells"], execute(df, slot, rule)):
            diff = None if comp["value"] is None else round(comp["value"] - cell["value"], 4)
            rows.append({"지표": f"{slot['table_title']} · {slot['metric_label']}", "그룹": cell["group"] or "",
                         "기간": cell["period"], "보고서 값": cell["value"], "재계산 값": comp["value"], "차이": diff,
                         "허용 오차": f"0 (소수 {rule.decimals}자리 반올림 후)", "일치 여부": "일치" if diff == 0 else "불일치"})
    return rows


def generate_artifacts(s, *, tenant_id: str, run: Run, rule_version_id: str, manual_spec: dict,
                       df: pd.DataFrame) -> dict:
    rv = s.get(RuleVersion, rule_version_id)
    if rv is None or rv.tenant_id != tenant_id:
        raise PermissionError("규칙 버전에 접근할 수 없습니다.")
    rationales = list(s.scalars(select(Rationale).where(Rationale.run_id == run.id)))
    ids = {}
    ids["manual"] = _put_artifact(s, tenant_id=tenant_id, run=run, kind="manual", filename="인수인계_매뉴얼.md",
                                  content=build_manual(rv, rationales, manual_spec))
    rules_json = {
        "schema_version": 1, "rule_version": rv.version, "rule_version_id": rv.id,
        "calc_rules": rv.spec["calc_rules"],
        "decision_criteria": [{"proposal_key": r.proposal_key, "title": r.data.get("title"),
                               "claim_id": c["claim_id"], "statement": c["statement"],
                               "criterion": c.get("criterion"), "criterion_user": c.get("criterion_user"),
                               "status": c["status"], "confirmation": c["confirmation"]}
                              for r in rationales for c in r.data.get("claims", [])],
        "scope": rv.spec.get("scope", {}),
    }
    ids["rules"] = _put_artifact(s, tenant_id=tenant_id, run=run, kind="rules", filename="업무규칙.json",
                                 content=json.dumps(rules_json, ensure_ascii=False, indent=2))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["지표", "그룹", "기간", "보고서 값", "재계산 값", "차이", "허용 오차", "일치 여부"])
    w.writeheader()
    w.writerows(validation_rows(rv, df))
    ids["validation"] = _put_artifact(s, tenant_id=tenant_id, run=run, kind="validation", filename="검증결과.csv",
                                      content="﻿" + buf.getvalue())
    return {"artifact_ids": ids}


# ---------- 다음 기간 재실행 ----------

def check_schema(rv: RuleVersion, df: pd.DataFrame) -> dict:
    need = set(rv.spec.get("dataset_columns", []))
    missing_cols = sorted(need - set(df.columns))
    known_status = set(rv.spec.get("scope", {}).get("day_values", []))
    new_values = sorted(set(df["day_of_week"].astype(str)) - known_status) if known_status else []
    groups = {}
    for r in rv.spec["calc_rules"]:
        miss = missing_groups(r["slot"], df)
        if miss:
            groups[r["slot"]["metric_label"]] = miss
    return {"missing_columns": missing_cols, "new_day_values": new_values, "missing_groups": groups,
            "ok": not missing_cols and not new_values}


def eval_criterion(df: pd.DataFrame, crit: dict, month: str) -> dict:
    if crit.get("metric") not in A.ANALYSIS_METRICS:  # 예전 버전에서 결과 필드명을 넣은 경우
        crit = {**crit, "metric": "sales_quantity"}
    if crit.get("scope_column") not in A.SCOPE_COLUMNS:
        crit = {**crit, "scope_column": "category_l1", "scope_values": []}
    c = DecisionCriterion.model_validate(crit)
    months_all = sorted(df["month"].unique())
    idx = months_all.index(month)
    start = months_all[max(0, idx - c.window_months)]
    if c.type == "trend":
        res = A.trend(df, c.scope_column, c.scope_values, start, month, c.metric)
    elif c.type == "baseline":
        res = A.baseline(df, c.scope_column, c.scope_values, month, months_all[max(0, idx - c.window_months):idx], c.metric)
    elif c.type == "promo_lift":
        res = A.promo_lift(df, c.scope_column, c.scope_values, month, c.metric)
    elif c.type == "dow":
        res = A.dow(df, c.scope_column, c.scope_values, month, c.metric)
    else:
        res = A.rank(df, month, c.metric, "item_name", c.scope_column, c.scope_values)
    ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
           "<": lambda a, b: a < b, "==": lambda a, b: a == b}
    hits = [r for r in res["rows"] if r.get(c.condition.field) is not None
            and ops[c.condition.operator](r[c.condition.field], c.condition.threshold)]
    return {"description": c.description, "action": c.action, "hits": [{"name": h["name"], c.condition.field: h[c.condition.field]} for h in hits]}


def generate_report_draft(s, *, tenant_id: str, run: Run, rv: RuleVersion, df: pd.DataFrame, month: str) -> dict:
    if month not in set(df["month"]):
        raise ValueError(f"데이터에 {month} 기간이 없습니다.")
    days = df[df["month"] == month]["business_date"].nunique()
    last = df[df["month"] == month]["business_date"].max().strftime("%Y-%m-%d")
    months_all = sorted(df["month"].unique())
    prev = months_all[months_all.index(month) - 1] if months_all.index(month) > 0 else None

    rows = []
    for r in rv.spec["calc_rules"]:
        if r["validation_status"] != "matched":
            continue
        rule, slot = CalcRule.model_validate(r["rule"]), r["slot"]
        # 그룹이 있는 슬롯은 새 데이터의 그룹 값 전체로 다시 펼친다.
        groups = sorted(df[slot["group_by"]].astype(str).unique()) if slot["group_by"] else [None]
        for p in [x for x in (prev, month) if x]:
            fake = {**slot, "cells": [{"group": g, "period": p} for g in groups]}
            for cell in execute(df, fake, rule):
                v = cell["value"]
                rows.append({"표": slot["table_title"], "지표": slot["metric_label"], "그룹": cell["group"] or "",
                             "기간": p, "값": int(v) if v is not None and rule.decimals == 0 else v,
                             "규칙버전": f"v{rv.version}"})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["표", "지표", "그룹", "기간", "값", "규칙버전"])
    w.writeheader()
    w.writerows(rows)

    partial = "(부분월)" if pd.Timestamp(last).day < pd.Timestamp(last).days_in_month else ""
    md = [f"# {month} 판매 실적 보고서 초안 {partial}", "",
          f"> 규칙 버전 v{rv.version} 적용 · 집계 {month}-01 ~ {last} ({days}영업일) · 비교월 {prev or '-'}", "",
          "## 1. 주요 지표", "", f"| 지표 | {prev or ''} | {month} |", "|---|---|---|"]
    for r in rows:
        if r["그룹"] == "" and r["기간"] == month:
            pv = next((x["값"] for x in rows if x["지표"] == r["지표"] and x["그룹"] == "" and x["기간"] == prev), "")
            fmt = lambda x: f"{x:,}" if isinstance(x, (int, float)) else x  # noqa: E731
            md.append(f"| {r['지표']} | {fmt(pv)} | {fmt(r['값'])} |")
    md += ["", "## 2. 판단 기준 적용 결과 (제안 후보)", ""]
    applied = 0
    for rat in s.scalars(select(Rationale).where(Rationale.run_id == rv.run_id)):
        for c in rat.data.get("claims", []):
            if not c.get("criterion") or c["status"] not in ("explained", "user_explained") \
                    or c["confirmation"]["status"] == "rejected":
                continue
            try:
                ev = eval_criterion(df, c["criterion"], month)
            except Exception as e:  # noqa: BLE001
                md.append(f"- {c['statement']}: 적용 실패 ({e})")
                continue
            applied += 1
            names = ", ".join(f"{h['name']}" for h in ev["hits"]) or "해당 없음"
            mark = "" if c["confirmation"]["status"] == "confirmed" else " _(담당자 미확인 기준)_"
            md.append(f"- **{ev['description']}**{mark} → {ev['action']} 후보: {names}")
    if not applied:
        md.append("- 적용할 수 있는 판단 기준이 없습니다.")
    md += ["", "※ 이 초안은 확인된 규칙을 새 데이터에 적용한 결과입니다. 제안 문장은 담당자가 확정합니다."]

    ids = {
        "draft_csv": _put_artifact(s, tenant_id=tenant_id, run=run, kind="draft_csv", filename=f"{month}_보고서초안.csv",
                                   content="﻿" + buf.getvalue()),
        "draft_md": _put_artifact(s, tenant_id=tenant_id, run=run, kind="draft_md", filename=f"{month}_보고서초안.md",
                                  content="\n".join(md)),
    }
    return {"artifact_ids": ids, "rows": len(rows), "partial_month": bool(partial)}
