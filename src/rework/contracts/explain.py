"""규칙·판단을 인수자가 읽는 말로 바꾼다. 매뉴얼(business-mcp)과 화면(API)이 같은 표현을 쓴다."""

from collections import Counter

COLUMN_KO = {
    "net_sales_amount_krw": "순매출", "gross_sales_amount_krw": "총매출(할인 전)", "discount_amount_krw": "할인액",
    "sales_quantity": "판매수량", "transaction_count": "거래건수", "promotion_sales_quantity": "행사 판매수량",
    "sales_line_count": "판매 행 수", "promotion_line_count": "행사 행 수", "business_date": "영업일", "item_id": "품목",
}
GROUP_KO = {"item_name": "품목", "category_l1": "대분류", "category_l2": "소분류", "day_of_week": "요일"}
ROUNDING_KO = {"half_up": "반올림", "floor": "버림", "ceil": "올림", "half_even": "짝수 반올림"}

# 판단 하나의 상태 라벨: 확인됨 / 추정(데이터 근거 있음) / 모름 / 반려
LABEL_KO = {"confirmed": "확인됨", "evidence_confirmed": "근거 확인", "estimated": "추정", "unknown": "모름", "rejected": "반려"}


def ko(col: str | None) -> str:
    return COLUMN_KO.get(col or "", col or "")


def display_format(rule: dict) -> tuple:
    """표시 형식 (단위, 소수 자릿수, 반올림). 공통 규칙을 묶는 기준."""
    return (rule.get("scale", 1), rule.get("decimals", 0), rule.get("rounding", "half_up"), bool(rule.get("as_percent")))


def format_text(fmt: tuple) -> str:
    scale, decimals, rounding, pct = fmt
    unit = "천원 단위" if scale == 1000 else ("% 단위" if pct else "그대로")
    return f"{unit}, 소수 {decimals}자리 {ROUNDING_KO.get(rounding, rounding)}"


def common_format(rules: list[dict]) -> tuple | None:
    """가장 많이 쓰인 표시 형식. 규칙이 하나뿐이면 묶지 않는다."""
    if len(rules) < 2:
        return None
    fmt, n = Counter(display_format(r) for r in rules).most_common(1)[0]
    return fmt if n >= 2 else None


def rule_plain(rule: dict) -> str:
    """계산 방법을 업무 언어로. 표시 형식은 따로 적는다."""
    col, den = ko(rule["column"]), ko(rule.get("denominator_column"))
    return {"sum": f"{col} 합계", "count_distinct": f"{col} 수(중복 제외)",
            "daily_mean": f"{col} 합계 ÷ 영업일수",
            "ratio": f"{col} 합계 ÷ {den} 합계" + (" × 100" if rule.get("as_percent") else "")}[rule["op"]]


def rule_excel(rule: dict, slot: dict) -> str:
    """엑셀에서 같은 값을 내는 수식. 열 이름은 원천 CSV 머리글, '당월'·'그룹'은 해당 칸의 값."""
    cond = 'month,"당월"' + (f',{slot["group_by"]},"{GROUP_KO.get(slot["group_by"], "그룹")}"' if slot.get("group_by") else "")
    sumifs = lambda c: f"SUMIFS({c},{cond})"  # noqa: E731
    expr = {"sum": sumifs(rule["column"]),
            "count_distinct": f"COUNTA(UNIQUE(FILTER({rule['column']},month=\"당월\")))",
            "daily_mean": f"{sumifs(rule['column'])}/영업일수",
            "ratio": f"{sumifs(rule['column'])}/{sumifs(rule.get('denominator_column'))}"}[rule["op"]]
    if rule.get("as_percent"):
        expr += "*100"
    if rule.get("scale", 1) != 1:
        expr += f"/{rule['scale']}"
    fn = {"floor": "ROUNDDOWN", "ceil": "ROUNDUP"}.get(rule.get("rounding"), "ROUND")
    return f"={fn}({expr},{rule.get('decimals', 0)})"


def calc_label(validation_status: str) -> str:
    """계산 규칙은 보고서 숫자를 그대로 재현하면 확인된 것으로 본다. 재현하지 못하면 모름."""
    return "confirmed" if validation_status == "matched" else "unknown"


def claim_label(claim: dict) -> str:
    conf = (claim.get("confirmation") or {}).get("status")
    if conf == "rejected":
        return "rejected"
    if conf == "confirmed":
        return "confirmed"
    if claim.get("status") == "explained" and claim.get("reason_verified"):
        return "evidence_confirmed"
    return "estimated" if claim.get("status") == "explained" else "unknown"


def claim_rule(claim: dict) -> str:
    """다음 달에도 쓸 판단 규칙 한 문장. 없으면 빈 문자열."""
    return claim.get("criterion_user") or (claim.get("criterion") or {}).get("description") or ""
