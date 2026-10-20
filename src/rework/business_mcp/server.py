"""business-mcp: 데이터 조회 · 실제 계산 · 결과 저장.

회사(tenant)와 실행(run) 정보는 요청마다 rework.auth.mcp_scope로 정한다
(stdio: Worker가 넣은 환경 변수, HTTP: Worker가 서명한 실행 토큰).
모델이 보내는 인자로는 권한을 정하지 않는다.
"""

import threading
import time
import uuid
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from rework.business_mcp.services import analysis as A
from rework.business_mcp.services import artifacts as ART
from rework.business_mcp.services.calc import build_slots, compare, execute, validate_rule
from rework.business_mcp.services.dataset import REQUIRED_COLUMNS, DatasetError, load_dataset, profile
from rework.business_mcp.services.report_parser import ReportError, parse_report
from rework.auth.mcp_scope import Scope, ScopeError, resolve_scope
from rework.config import get_settings
from rework.contracts.rules import CalcRule
from rework.db.models import File, RuleVersion, Run
from rework.db.session import session_scope
from rework.storage import resolve

server = MCPServer(
    "business-mcp",
    instructions="보고서 원천 데이터를 조회하고, 허용된 연산만으로 계산·분석하고, 규칙·근거·결과물을 저장한다. "
                 "임의 코드나 SQL은 실행하지 않는다.",
)
# execute_aggregation → compare_report 사이에만 쓰는 계산 결과. 여러 실행이 같은 서버를 쓰므로
# 실행 범위를 함께 저장해 다른 실행에서 꺼내지 못하게 하고, 오래된 것은 지운다.
CALC_TTL_SECONDS = 600
_calculations: dict[str, dict] = {}
_calc_lock = threading.Lock()

Month = Annotated[str, Field(description="YYYY-MM 형식의 월 (예: 2026-06)")]
ScopeColumn = Annotated[str, Field(description="분석 범위 열: item_name | category_l1 | category_l2")]
ScopeValues = Annotated[list[str], Field(description="범위 값 목록. 데이터의 정확한 이름 (예: ['단팥빵','카스텔라'])")]
Metric = Annotated[str, Field(description="sales_quantity | net_sales_amount_krw | gross_sales_amount_krw | transaction_count")]


def _scope(ctx: Context) -> Scope:
    try:
        return resolve_scope(ctx.headers)
    except ScopeError as e:
        raise ToolError(str(e)) from e


def _file(sc: Scope, file_id: str, kind: str | None = None) -> File:
    with session_scope() as s:
        f = s.get(File, file_id)
        if f is None or f.tenant_id != sc.tenant_id:
            raise ToolError(f"파일 {file_id}에 접근할 수 없습니다.")
        if kind and f.kind != kind:
            raise ToolError(f"파일 {file_id}는 {kind} 파일이 아닙니다.")
        s.expunge(f)
        return f


def _path(sc: Scope, file_id: str, kind: str) -> str:
    """DB 경로를 이 서버의 저장소 경로로 바꾼다. 컨테이너로 분리하면 저장소 볼륨 공유가 빠졌을 때 여기서 드러난다."""
    p = resolve(_file(sc, file_id, kind).path)
    if not p.exists():
        raise ToolError(f"파일 {file_id}가 저장소에 없습니다. 업로드 저장소가 이 서버와 공유되는지 확인하세요.")
    return str(p)


def _df(sc: Scope, dataset_id: str):
    try:
        return load_dataset(_path(sc, dataset_id, "source"))
    except DatasetError as e:
        raise ToolError(str(e)) from e


def _run(s, sc: Scope) -> Run:
    run = s.get(Run, sc.run_id) if sc.run_id else None
    if run is None or run.tenant_id != sc.tenant_id:
        raise ToolError("실행 정보를 찾을 수 없습니다.")
    return run


def _put_calc(sc: Scope, data: dict) -> str:
    calc_id = f"calc_{uuid.uuid4().hex[:8]}"
    cutoff = time.monotonic() - CALC_TTL_SECONDS
    with _calc_lock:
        for k in [k for k, v in _calculations.items() if v["at"] < cutoff]:
            del _calculations[k]
        _calculations[calc_id] = {"scope": sc, "at": time.monotonic(), **data}
    return calc_id


def _get_calc(sc: Scope, calc_id: str) -> dict | None:
    with _calc_lock:
        calc = _calculations.get(calc_id)
    return calc if calc and calc["scope"] == sc else None


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (A.AnalysisError, ValueError) as e:
        raise ToolError(str(e)) from e


# ---------- 조회 ----------

@server.tool()
def inspect_dataset(ctx: Context, dataset_id: Annotated[str, Field(description="등록된 원천 CSV 파일 ID")]) -> dict:
    """원천 판매 데이터의 구조를 확인한다. 열·타입·결측치·기간(월별 영업일수)·범주형 값 목록을 반환한다."""
    sc = _scope(ctx)
    df = _df(sc, dataset_id)
    return {"dataset_id": dataset_id, "required_columns": REQUIRED_COLUMNS, **profile(df)}


@server.tool()
def read_report(ctx: Context, report_id: Annotated[str, Field(description="등록된 완성 보고서 PDF 파일 ID")],
                dataset_id: Annotated[str, Field(description="함께 분석할 원천 CSV 파일 ID")]) -> dict:
    """완성 보고서 PDF를 구조화한다. 표, 제안 목록, 각주, 그리고 원천 데이터로 검증할 지표 슬롯을 반환한다."""
    sc = _scope(ctx)
    try:
        report = parse_report(_path(sc, report_id, "report"))
    except ReportError as e:
        raise ToolError(str(e)) from e
    slots = build_slots(report, _df(sc, dataset_id))
    return {"report_id": report_id, **report, "slots": slots}


# ---------- 1층 계산 ----------

@server.tool()
def execute_aggregation(
    ctx: Context,
    dataset_id: Annotated[str, Field(description="원천 CSV 파일 ID")],
    report_id: Annotated[str, Field(description="보고서 PDF 파일 ID (슬롯 정의를 읽는다)")],
    rules: Annotated[list[dict], Field(description="CalcRule 목록: slot_id, metric_name, op, column, denominator_column, scale, as_percent, decimals, rounding")],
) -> dict:
    """계산 규칙을 실제로 실행해 각 슬롯 값을 계산한다. 반환한 calculation_id로 compare_report를 호출한다."""
    sc = _scope(ctx)
    df = _df(sc, dataset_id)
    report = parse_report(_path(sc, report_id, "report"))
    slots = {s["slot_id"]: s for s in build_slots(report, df)}
    results, errors = [], []
    for raw in rules:
        try:
            rule = CalcRule.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            errors.append({"rule": raw, "error": f"규칙 형식 오류: {e}"})
            continue
        if rule.slot_id not in slots:
            errors.append({"slot_id": rule.slot_id, "error": "없는 슬롯"})
            continue
        if err := validate_rule(rule):
            errors.append({"slot_id": rule.slot_id, "error": err})
            continue
        results.append({"rule": rule.model_dump(), "values": execute(df, slots[rule.slot_id], rule)})
    calc_id = _put_calc(sc, {"slots": slots, "results": results})
    return {"calculation_id": calc_id, "computed": len(results), "errors": errors}


@server.tool()
def compare_report(ctx: Context, calculation_id: Annotated[str, Field(description="execute_aggregation이 반환한 ID")]) -> dict:
    """계산 결과를 보고서 값과 칸별로 비교한다. 슬롯마다 일치 칸 수, 상태(matched/mismatched), 차이 예시를 반환한다."""
    calc = _get_calc(_scope(ctx), calculation_id)
    if calc is None:
        raise ToolError("calculation_id를 찾을 수 없습니다. execute_aggregation을 먼저 호출하세요.")
    out = []
    for r in calc["results"]:
        c = compare(calc["slots"][r["rule"]["slot_id"]], r["values"])
        out.append({**c, "rule": r["rule"]})
    return {"comparisons": out}


# ---------- 2층 근거 분석 ----------

@server.tool()
def analyze_trend(ctx: Context, dataset_id: Annotated[str, Field(description="원천 CSV 파일 ID")], scope_column: ScopeColumn,
                  scope_values: ScopeValues, start_month: Month, end_month: Month, metric: Metric = "sales_quantity",
                  group_by: Annotated[str, Field(description="item_name | category_l1 | category_l2 | all")] = "item_name") -> dict:
    """여러 달의 월별 일평균 추세를 계산한다. 그룹마다 마지막 달 기준 연속 증가(consecutive_up)·연속 감소(consecutive_down) 개월 수, 전체 변화율(change_pct), 전월 대비(last_mom_pct)를 준다. '꾸준히 증가', '몇 달째 감소' 같은 근거를 검증할 때 쓴다."""
    sc = _scope(ctx)
    return _wrap(A.trend, _df(sc, dataset_id), scope_column, scope_values, start_month, end_month, metric, group_by)


@server.tool()
def analyze_baseline(ctx: Context, dataset_id: Annotated[str, Field(description="원천 CSV 파일 ID")], scope_column: ScopeColumn,
                     scope_values: ScopeValues, target_month: Month,
                     baseline_months: Annotated[list[str], Field(description="평소 수준으로 볼 기준 월 목록 (YYYY-MM)")],
                     metric: Metric = "sales_quantity") -> dict:
    """target_month의 일평균이 기준 기간 평균(평소 수준)보다 얼마나 높거나 낮은지(deviation_pct) 계산한다. '그 달이 유난히 많이 팔렸다' 같은 근거를 검증할 때 쓴다."""
    sc = _scope(ctx)
    return _wrap(A.baseline, _df(sc, dataset_id), scope_column, scope_values, target_month, baseline_months, metric)


@server.tool()
def analyze_promo_lift(ctx: Context, dataset_id: Annotated[str, Field(description="원천 CSV 파일 ID")], scope_column: ScopeColumn,
                       scope_values: ScopeValues, month: Month, metric: Metric = "sales_quantity") -> dict:
    """같은 달 안에서 행사일(promotion_sales_quantity>0)과 비행사일의 일평균을 비교해 행사 효과(lift_pct)를 계산한다. 행사가 없던 품목은 items_without_promo로 돌려준다."""
    sc = _scope(ctx)
    return _wrap(A.promo_lift, _df(sc, dataset_id), scope_column, scope_values, month, metric)


@server.tool()
def analyze_dow(ctx: Context, dataset_id: Annotated[str, Field(description="원천 CSV 파일 ID")], scope_column: ScopeColumn,
                scope_values: ScopeValues, month: Month, metric: Metric = "sales_quantity") -> dict:
    """범위 전체를 날짜별로 합한 뒤 요일별 일평균과 월 평균 대비 차이(vs_overall_pct)를 계산한다. '금요일에 많이 나간다' 같은 근거를 검증할 때 쓴다."""
    sc = _scope(ctx)
    return _wrap(A.dow, _df(sc, dataset_id), scope_column, scope_values, month, metric)


@server.tool()
def rank_items(ctx: Context, dataset_id: Annotated[str, Field(description="원천 CSV 파일 ID")], month: Month,
               metric: Metric = "net_sales_amount_krw",
               group_by: Annotated[str, Field(description="item_name | category_l1 | category_l2")] = "item_name",
               scope_column: ScopeColumn = "category_l1", scope_values: ScopeValues = [],  # noqa: B006
               compare_month: Annotated[str | None, Field(description="비교월 (YYYY-MM), 없으면 생략")] = None,
               top_n: int = 15) -> dict:
    """월 합계 순위와 비교월 대비 일평균 증감률(vs_compare_pct)을 계산한다."""
    sc = _scope(ctx)
    return _wrap(A.rank, _df(sc, dataset_id), month, metric, group_by, scope_column, scope_values, compare_month, top_n)


@server.tool()
def evaluate_criterion(ctx: Context, dataset_id: Annotated[str, Field(description="원천 CSV 파일 ID")],
                       criterion: Annotated[dict, Field(description="DecisionCriterion (type, metric, window_months, condition, scope_column, scope_values)")],
                       month: Month) -> dict:
    """판단 기준을 해당 월 데이터에 적용해 조건을 만족하는 대상(hits)을 돌려준다. 기준의 자기 일관성 검사와 다음 달 제안 후보 계산에 쓴다."""
    sc = _scope(ctx)
    return _wrap(ART.eval_criterion, _df(sc, dataset_id), criterion, month)


# ---------- 저장 (변경 도구) ----------

@server.tool()
def save_rule_version(
    ctx: Context,
    calc_rules: Annotated[list[dict], Field(description="[{slot, rule, validation_status, confirmation_ref}]")],
    report: Annotated[dict, Field(description="보고서 제목·기간 정보")],
    dataset_columns: Annotated[list[str], Field(description="규칙이 필요로 하는 원천 열")],
    unresolved: Annotated[list[str], Field(description="해결하지 못한 항목")] = [],  # noqa: B006
    scope: Annotated[dict, Field(description="적용 범위 (기간, 요일 값 등)")] = {},  # noqa: B006
) -> dict:
    """검증한 계산 규칙을 새 규칙 버전으로 저장한다. 확인 상태는 confirmation_ref(질문 ID)에 연결된 실제 답변 기록으로만 정해진다."""
    sc = _scope(ctx)
    with session_scope() as s:
        return _wrap(ART.save_rule_version, s, tenant_id=sc.tenant_id, run=_run(s, sc), payload={
            "calc_rules": calc_rules, "report": report, "dataset_columns": dataset_columns,
            "unresolved": unresolved, "scope": scope})


@server.tool()
def save_rationale(ctx: Context, proposal_key: Annotated[str, Field(description="제안 키 (예: P1)")],
                   rationale: Annotated[dict, Field(description="제안·결정(claims)·판정·근거·판단 기준. claim마다 confirmation_ref(질문 ID)를 둘 수 있다")]) -> dict:
    """제안 하나의 판단 근거를 저장한다. 결정마다 확인 상태를 DB 답변 기록으로 정한다. 같은 실행에서 다시 저장하면 덮어쓴다."""
    sc = _scope(ctx)
    with session_scope() as s:
        return _wrap(ART.save_rationale, s, tenant_id=sc.tenant_id, run=_run(s, sc), proposal_key=proposal_key,
                     rationale=rationale)


@server.tool()
def generate_artifacts(ctx: Context, rule_version_id: Annotated[str, Field(description="저장한 규칙 버전 ID")],
                       manual_spec: Annotated[dict, Field(description="매뉴얼 서술: purpose, preparation, steps[], validation, exceptions[]")],
                       dataset_id: Annotated[str, Field(description="검증에 쓴 원천 CSV 파일 ID")],
                       report_id: Annotated[str, Field(description="검증에 쓴 보고서 PDF 파일 ID")]) -> dict:
    """인수인계 매뉴얼(Markdown), 업무 규칙 JSON, 검증 결과 CSV를 생성해 저장한다. 표와 숫자는 저장된 규칙을 다시 실행해 채운다."""
    sc = _scope(ctx)
    _file(sc, report_id, "report")
    with session_scope() as s:
        try:
            return ART.generate_artifacts(s, tenant_id=sc.tenant_id, run=_run(s, sc), rule_version_id=rule_version_id,
                                          manual_spec=manual_spec, df=_df(sc, dataset_id))
        except PermissionError as e:
            raise ToolError(str(e)) from e


@server.tool()
def check_schema(ctx: Context, rule_version_id: Annotated[str, Field(description="적용할 규칙 버전 ID")],
                 dataset_id: Annotated[str, Field(description="새 원천 CSV 파일 ID")]) -> dict:
    """새 데이터가 규칙 버전과 맞는지 확인한다. 빠진 열, 처음 보는 요일 값, 사라진 품목·카테고리를 반환한다."""
    sc = _scope(ctx)
    with session_scope() as s:
        rv = s.get(RuleVersion, rule_version_id)
        if rv is None or rv.tenant_id != sc.tenant_id:
            raise ToolError("규칙 버전에 접근할 수 없습니다.")
        return ART.check_schema(rv, _df(sc, dataset_id))


@server.tool()
def generate_report_draft(ctx: Context, rule_version_id: Annotated[str, Field(description="적용할 규칙 버전 ID")],
                          dataset_id: Annotated[str, Field(description="새 원천 CSV 파일 ID")], month: Month) -> dict:
    """확인된 규칙을 새 기간 데이터에 적용해 보고서 초안(CSV·Markdown)을 만든다. 판단 기준도 다시 적용해 제안 후보를 뽑는다."""
    sc = _scope(ctx)
    with session_scope() as s:
        rv = s.get(RuleVersion, rule_version_id)
        if rv is None or rv.tenant_id != sc.tenant_id:
            raise ToolError("규칙 버전에 접근할 수 없습니다.")
        return _wrap(ART.generate_report_draft, s, tenant_id=sc.tenant_id, run=_run(s, sc), rv=rv, df=_df(sc, dataset_id), month=month)


if __name__ == "__main__":
    cfg = get_settings()
    if cfg.mcp_transport == "http":
        server.run(transport="streamable-http", host="0.0.0.0", port=cfg.business_mcp_port)
    else:
        server.run()
