import json
from pathlib import Path

import pytest

from rework.business_mcp.services.dataset import load_dataset
from rework.business_mcp.services.report_parser import parse_report

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def df():
    return load_dataset(ROOT / "data" / "판매분석가_일별상세.csv")


@pytest.fixture(scope="session")
def report():
    return parse_report(ROOT / "data" / "2026-06_ST001_월간판매실적보고서.pdf")


@pytest.fixture(scope="session")
def answer_key():
    return json.loads((ROOT / "tests/fixtures/ground_truth/2026-06_answer_key.json").read_text())
