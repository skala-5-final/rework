"""품질 측정 스크립트 공통: API 호출, 작업 실행·대기, 결과 저장.

결과는 var/quality/<지표>.json에 남기고, docs/05_품질검증결과.md가 이 값을 인용한다.
기준서 4장 규칙 7(증빙)에 따라 실행 시각, 코드 버전, 입력 ID, 기대값, 실제값, 판정을 함께 저장한다.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "var" / "quality"
API = os.environ.get("REWORK_API", "http://localhost:8000")
DATA = ROOT / "data"
SOURCE_CSV = DATA / "판매분석가_일별상세.csv"
REPORT_PDF = DATA / "2026-06_ST001_월간판매실적보고서.pdf"
TERMINAL = {"WAITING_FOR_INPUT", "COMPLETED", "FAILED"}


def code_version() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = subprocess.run(["git", "diff", "--quiet"], cwd=ROOT).returncode != 0
        return sha + ("+미커밋변경" if dirty else "")
    except Exception:  # noqa: BLE001 - 컨테이너 안에는 git이 없다
        return os.environ.get("REWORK_CODE_VERSION", "unknown")


def save(metric: str, result: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{metric}.json"
    body = {"metric": metric, "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "code_version": code_version(), **result}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str))
    print(f"[{metric}] {result.get('verdict')} — {result.get('summary', '')}  → {path.relative_to(ROOT)}")
    return path


def verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def p95(values: list[float]) -> float:
    """기준서 3장: 오름차순 정렬 후 95백분위 (nearest-rank)."""
    xs = sorted(values)
    return xs[max(0, -(-95 * len(xs) // 100) - 1)]


class Client:
    def __init__(self, username: str = "demo", password: str = "demo1234"):
        self.http = httpx.Client(base_url=API, timeout=60)
        r = self.http.post("/auth/login", json={"username": username, "password": password})
        r.raise_for_status()
        self.token = r.json()["access_token"]
        self.http.headers["Authorization"] = f"Bearer {self.token}"

    def get(self, path: str):
        r = self.http.get(path)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict):
        r = self.http.post(path, json=body)
        r.raise_for_status()
        return r.json()

    def upload(self, kind: str, path: Path) -> str:
        with path.open("rb") as f:
            r = self.http.post("/files", data={"kind": kind}, files={"file": (path.name, f)})
        r.raise_for_status()
        return r.json()["file_id"]

    def inputs(self) -> tuple[str, str]:
        return self.upload("source", SOURCE_CSV), self.upload("report", REPORT_PDF)

    def start(self, name: str, source_id: str, report_id: str, **extra) -> str:
        return self.post("/jobs", {"name": name, "source_file_id": source_id, "report_file_id": report_id, **extra})["job_id"]

    def wait(self, job_id: str, timeout: float = 1800, terminal=TERMINAL) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            j = self.get(f"/jobs/{job_id}")
            if j["status"] in terminal:
                return j
            time.sleep(3)
        raise TimeoutError(f"{job_id} 대기 시간 초과")

    def answer_later(self, job_id: str) -> None:
        j = self.get(f"/jobs/{job_id}")
        self.post(f"/jobs/{job_id}/answers", {"run_id": j["run"]["run_id"], "run_version": j["run"]["version"], "answers": []})

    def events(self, job_id: str) -> list[dict]:
        out, after = [], 0
        while True:
            batch = self.get(f"/jobs/{job_id}/events?after={after}")
            if not batch:
                return out
            out += batch
            after = batch[-1]["id"]


def compose(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, check=check, capture_output=True, text=True).stdout
