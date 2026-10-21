"""표준 데이터셋으로 분석을 N회 실행한다 (Q-10 완료시간, 그리고 Q-04·H-01·H-02·H-03·H-05의 측정 대상).

    docker compose up -d --scale worker=3     # 병렬로 돌리려면
    uv run python scripts/quality/runs.py 30

분석 시간 = Worker의 '작업 시작' 이벤트부터 '담당자 답변 대기' 또는 '완료'까지 (사람의 응답 대기는 제외).
질문은 '나중에 답하기'로 넘겨 결과물까지 만든다. 실행한 작업 ID는 var/quality/runs.json에 남는다.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from common import OUT, Client, p95, save, verdict

TARGET_SECONDS = 300


def _ts(e: dict) -> datetime:
    return datetime.fromisoformat(e["ts"])


def one(i: int, src: str, rep: str) -> dict:
    try:
        return _one(i, src, rep)
    except Exception as e:  # noqa: BLE001 - 한 건이 실패해도 나머지는 계속 측정하고 실패로 기록한다
        print(f"  {i:02d} 측정 오류: {e}")
        return {"job_id": None, "analysis_seconds": None, "final_status": f"ERROR: {e}"}


def _one(i: int, src: str, rep: str) -> dict:
    c = Client()
    job_id = c.start(f"[품질] 표준 분석 {i:02d}", src, rep)
    j = c.wait(job_id)
    ev = c.events(job_id)
    started = next(e for e in ev if e["stage"] == "worker" and e["message"].startswith("작업 시작"))
    ended = [e for e in ev if e["stage"] == "worker" and (e["message"].startswith("담당자 답변 대기") or e["message"] == "완료")]
    analysis = (_ts(ended[0]) - _ts(started)).total_seconds() if ended else None
    status = j["status"]
    if status == "WAITING_FOR_INPUT":
        c.answer_later(job_id)
        status = c.wait(job_id, terminal={"COMPLETED", "FAILED"})["status"]
    print(f"  {i:02d} {job_id} 분석 {analysis}s → {status}")
    return {"job_id": job_id, "analysis_seconds": analysis, "final_status": status}


def main(n: int, workers: int) -> None:
    src, rep = Client().inputs()
    with ThreadPoolExecutor(workers) as pool:
        runs = list(pool.map(lambda i: one(i, src, rep), range(1, n + 1)))
    OUT.mkdir(parents=True, exist_ok=True)
    prev = json.loads((OUT / "runs.json").read_text()) if (OUT / "runs.json").exists() else []
    (OUT / "runs.json").write_text(json.dumps(prev + runs, ensure_ascii=False, indent=2))

    times = [r["analysis_seconds"] for r in runs if r["analysis_seconds"] is not None]
    failed = [r["job_id"] for r in runs if r["final_status"] != "COMPLETED"]
    v = p95(times) if times else None
    save("Q-10", {"verdict": verdict(bool(times) and len(times) == n and v <= TARGET_SECONDS),
                  "summary": f"분석 {len(times)}/{n}회, P95 {v:.1f}초 (목표 ≤ {TARGET_SECONDS}초)" if times else "측정 실패",
                  "target": f"P95 ≤ {TARGET_SECONDS}초", "n": n, "parallel_workers": workers,
                  "p95_seconds": v, "min": min(times, default=None), "max": max(times, default=None),
                  "mean": round(sum(times) / len(times), 1) if times else None,
                  "failed_jobs": failed, "runs": runs, "inputs": {"source_file_id": src, "report_file_id": rep}})


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30, int(sys.argv[2]) if len(sys.argv) > 2 else 3)
