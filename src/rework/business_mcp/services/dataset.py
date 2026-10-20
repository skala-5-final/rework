"""원천 판매 데이터 로딩과 구조 확인."""

from functools import lru_cache
from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = [
    "business_date", "store_id", "item_id", "item_name", "category_l1", "category_l2",
    "day_of_week", "weekend_yn", "sales_quantity", "gross_sales_amount_krw",
    "discount_amount_krw", "net_sales_amount_krw", "transaction_count", "promotion_sales_quantity",
]
CATEGORICAL_COLUMNS = ["store_id", "category_l1", "category_l2", "item_name", "day_of_week", "weekend_yn"]
NUMERIC_COLUMNS = [
    "sales_quantity", "gross_sales_amount_krw", "discount_amount_krw", "net_sales_amount_krw",
    "transaction_count", "sales_line_count", "promotion_sales_quantity", "promotion_line_count",
]
KNOWN_DAYS = {"MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"}


class DatasetError(ValueError):
    """입력 데이터 문제. 재시도해도 해결되지 않는다."""


def missing_columns(header: list[str]) -> list[str]:
    return [c for c in REQUIRED_COLUMNS if c not in header]


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    missing = missing_columns(list(df.columns))
    if missing:
        raise DatasetError(f"필수 열이 없습니다: {', '.join(missing)}")
    df["business_date"] = pd.to_datetime(df["business_date"], errors="coerce")
    if df["business_date"].isna().any():
        raise DatasetError("business_date에 날짜로 읽을 수 없는 값이 있습니다.")
    df["month"] = df["business_date"].dt.strftime("%Y-%m")
    if df["weekend_yn"].dtype != bool:
        df["weekend_yn"] = df["weekend_yn"].astype(str).str.upper().eq("TRUE")
    for c in NUMERIC_COLUMNS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="raise")
    return df


def load_dataset(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    return _load(str(p), p.stat().st_mtime)


def month_days(df: pd.DataFrame) -> dict[str, int]:
    return df.groupby("month")["business_date"].nunique().to_dict()


def profile(df: pd.DataFrame) -> dict:
    days = month_days(df)
    return {
        "row_count": int(len(df)),
        "date_range": [df.business_date.min().strftime("%Y-%m-%d"), df.business_date.max().strftime("%Y-%m-%d")],
        "months": [{"month": m, "business_days": int(n),
                    "partial": int(n) < pd.Period(m).days_in_month} for m, n in sorted(days.items())],
        "columns": [
            {"name": c, "dtype": str(df[c].dtype), "nulls": int(df[c].isna().sum())}
            for c in df.columns if c != "month"
        ],
        "categorical_values": {
            c: sorted(map(str, df[c].unique().tolist())) for c in CATEGORICAL_COLUMNS if c in df.columns
        },
        "unknown_day_values": sorted(set(df.day_of_week.unique()) - KNOWN_DAYS),
    }
