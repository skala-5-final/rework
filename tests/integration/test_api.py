"""실행 중인 PostgreSQL이 필요하다 (docker compose up -d, scripts/seed_demo.py). 작업은 만들지 않는다."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from rework.db.session import get_engine

ROOT = Path(__file__).resolve().parents[2]

try:
    with get_engine().connect() as conn:
        conn.execute(text("select 1 from users where username='demo'"))
except Exception:  # noqa: BLE001
    pytest.skip("DB가 없거나 시드되지 않았습니다.", allow_module_level=True)

from rework.api.main import app  # noqa: E402


def _client(user: str, pw: str) -> TestClient:
    c = TestClient(app)
    assert c.post("/auth/login", json={"username": user, "password": pw}).status_code == 200
    return c


def test_login_required_and_wrong_password():
    c = TestClient(app)
    assert c.get("/jobs").status_code == 401
    assert c.post("/auth/login", json={"username": "demo", "password": "wrong"}).status_code == 401


def test_upload_validation():
    c = _client("demo", "demo1234")
    bad_csv = c.post("/files", data={"kind": "source"}, files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert bad_csv.status_code == 400 and "필수 열" in bad_csv.json()["detail"]
    not_pdf = c.post("/files", data={"kind": "report"}, files={"file": ("r.pdf", b"hello", "application/pdf")})
    assert not_pdf.status_code == 400


def test_other_tenant_cannot_use_files():
    demo, other = _client("demo", "demo1234"), _client("other", "other1234")
    src = demo.post("/files", data={"kind": "source"},
                    files={"file": ("d.csv", (ROOT / "data/agent_판매분석가_일별상세.csv").read_bytes(), "text/csv")}).json()
    rep = demo.post("/files", data={"kind": "report"},
                    files={"file": ("r.pdf", (ROOT / "reports/2026-06_ST001_월간판매실적보고서.pdf").read_bytes(), "application/pdf")}).json()
    r = other.post("/jobs", json={"name": "x", "source_file_id": src["file_id"], "report_file_id": rep["file_id"]})
    assert r.status_code == 404
    demo_jobs = {j["job_id"] for j in demo.get("/jobs").json()}
    assert demo_jobs.isdisjoint({j["job_id"] for j in other.get("/jobs").json()})
    for job_id in list(demo_jobs)[:3]:
        assert other.get(f"/jobs/{job_id}").status_code == 404
