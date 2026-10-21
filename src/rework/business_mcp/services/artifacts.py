"""규칙 버전·근거 저장과 결과물(매뉴얼·규칙 JSON·검증표·다음 달 초안) 생성."""

import csv
import io
import json
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import func, select

from rework.business_mcp.services import analysis as A
from rework.business_mcp.services.calc import execute, missing_groups
from rework.contracts import explain as X
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


RAT_KO = {"explained": "데이터로 설명됨", "ambiguous": "판단 보류", "unexplained": "데이터로 설명 안 됨",
          "user_explained": "담당자 설명으로 확정"}
LABEL_MARK = {"confirmed": "✅ 확인됨", "estimated": "🟡 추정", "unknown": "❓ 모름", "rejected": "⛔ 반려"}


def build_manual(rv: RuleVersion, rationales: list[Rationale], manual_spec: dict, open_questions: list[str]) -> str:
    """인수자용 매뉴얼. 규칙을 먼저 한 줄로 쓰고, 근거는 접어 둔다."""
    spec = rv.spec
    rep = spec.get("report", {})
    rules = spec["calc_rules"]
    L = [f"# 인수인계 매뉴얼: {rep.get('title', '월간 판매 실적 보고서')}", "",
         f"> 규칙 버전 v{rv.version} · {datetime.now(timezone.utc):%Y-%m-%d} 생성 · 당월 {rep.get('period', '-')}, "
         f"비교월 {rep.get('prev_period', '-')}", "",
         "라벨: ✅ 확인됨(숫자 재현 또는 담당자 확인) · 🟡 추정(데이터 근거 있음) · ❓ 모름 · ⛔ 반려", ""]

    L += ["## 1. 이 보고서는", manual_spec.get("purpose", "-"), ""]
    L += ["## 2. 매달 만드는 순서", f"- 준비: {manual_spec.get('preparation', '-')}"]
    L += [f"{i}. {step}" for i, step in enumerate(manual_spec.get("steps", []), 1)]
    L += [f"- 검증: {manual_spec.get('validation', '계산 규칙을 다시 실행해 보고서 수치와 비교합니다.')}"]
    L += [f"- 예외: {x}" for x in manual_spec.get("exceptions", [])] + [""]

    L += ["## 3. 판단 규칙", "다음 달에도 같은 조건이면 같은 결정을 합니다.", ""]
    for rat in sorted(rationales, key=lambda r: r.proposal_key):
        d = rat.data
        L.append(f"### {d.get('title', rat.proposal_key)}")
        for c in d.get("claims", []):
            rule = X.claim_rule(c)
            L.append(f"- {LABEL_MARK[X.claim_label(c)]} **{c['statement']}**" + (f" — {rule}" if rule else ""))
            detail = [f"보고서 표현: “{c.get('stated_reason', '')}”"] + [f"데이터: {e}" for e in c.get("evidence", [])]
            if c["confirmation"].get("answer_text"):
                detail.append(f"담당자 답변: “{c['confirmation']['answer_text']}”")
            if c.get("rejected_hypotheses"):
                detail.append("아니었던 이유: " + "; ".join(c["rejected_hypotheses"]))
            L += ["", '<details markdown="1"><summary>근거</summary>', ""] + [f"- {x}" for x in detail] + ["", "</details>", ""]
        L.append("")

    common = X.common_format([r["rule"] for r in rules])
    L += ["## 4. 계산 규칙"]
    if common:
        L.append(f"공통: {X.format_text(common)}. 다른 경우만 표에 따로 적었습니다.")
    L += ["", "| 지표 | 계산 | 엑셀 수식 | 상태 |", "|---|---|---|---|"]
    for r in rules:
        fmt = X.display_format(r["rule"])
        plain = X.rule_plain(r["rule"]) + ("" if fmt == common else f" ({X.format_text(fmt)})")
        L.append(f"| {r['slot']['table_title']} · {r['slot']['metric_label']} | {plain} | "
                 f"`{X.rule_excel(r['rule'], r['slot'])}` | {LABEL_MARK[X.calc_label(r['validation_status'])]} |")
    L += ["", "엑셀 수식의 열 이름은 원천 CSV 머리글입니다. `month`는 `business_date`의 연-월이고, `\"당월\"`·`\"품목\"` 같은 값은 해당 칸으로 바꿉니다.", ""]

    L += ["## 5. 원천 데이터 열", ", ".join(f"`{c}`" + (f"({X.ko(c)})" if X.ko(c) != c else "")
                                      for c in spec.get("dataset_columns", [])), ""]

    L += ["## 6. 확인 필요"]
    todo = list(spec.get("unresolved", [])) + open_questions
    L += [f"- {u}" for u in todo] or ["- 없음"]
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
    answered = select(Answer.question_id)
    open_questions = [q.text for q in s.scalars(select(Question).where(Question.run_id == run.id,
                                                                     Question.id.not_in(answered))
                                                 .order_by(Question.created_at))]
    ids["manual"] = _put_artifact(s, tenant_id=tenant_id, run=run, kind="manual", filename="인수인계_매뉴얼.md",
                                  content=build_manual(rv, rationales, manual_spec, open_questions))
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
