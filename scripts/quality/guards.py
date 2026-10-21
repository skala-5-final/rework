"""H-03 실행 제어 · H-04 컨텍스트 예산: 한도를 일부러 작게 잡은 Worker로 실제 분석을 돌려 한도가 지켜지는지 본다.

    uv run python scripts/quality/guards.py

정상 Worker를 잠시 멈추고, 시나리오마다 환경 변수를 바꾼 Worker 하나를 띄운다. 끝나면 정상 Worker 3개로 되돌린다.
H-04는 표준 분석(runs.json)의 모든 모델 호출이 예산 안이었는지(하네스가 센 값과 실제 API 사용량)도 함께 본다.
"""

import json
import subprocess
import time

from sqlalchemy import select

from common import OUT, ROOT, Client, compose, save, verdict
from rework.config import get_settings
from rework.db.models import Event
from rework.db.session import session_scope

SCENARIOS = [
    {"id": "soft-time", "env": {"RUN_SOFT_SECONDS": "25"}, "expect": "COMPLETED", "look": "조사 시간 한도",
     "what": "H-03 조사 시간 한도 25초 → 남은 조사를 건너뛰고 끝까지 완료"},
    {"id": "hard-time", "env": {"RUN_MAX_SECONDS": "30"}, "expect": "FAILED", "look": "실행시간 한도",
     "what": "H-03 시도 실행시간 한도 30초 → 중단하고 실패로 표시"},
    {"id": "run-budget", "env": {"RUN_TOKEN_BUDGET": "30000", "TOOL_RESULT_MAX_CHARS": "1500"}, "expect": "COMPLETED",
     "look": "토큰 예산", "what": "H-04 시도당 토큰 3만 + 도구 결과 1,500자 → 결과를 줄이고 조사를 건너뛰며 완료"},
    {"id": "call-budget", "env": {"CONTEXT_TOKEN_BUDGET": "4200"}, "expect": "FAILED", "look": "컨텍스트 예산",
     "what": "H-04 호출당 4,200토큰 → 넘는 호출은 보내지 않고 중단"},
]


def worker(env: dict) -> str:
    name = f"rework-guard-{int(time.time())}"
    args = ["run", "-d", "--name", name]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    compose(*args, "worker")
    return name


def scenario(c: Client, src: str, rep: str, sc: dict) -> dict:
    name = worker(sc["env"])
    try:
        time.sleep(3)
        t0 = time.time()
        job_id = c.start(f"[품질] 한도 시나리오 {sc['id']}", src, rep)
        j = c.wait(job_id, timeout=900)
        if j["status"] == "WAITING_FOR_INPUT":
            c.answer_later(job_id)
            j = c.wait(job_id, timeout=600, terminal={"COMPLETED", "FAILED"})
        ev = c.events(job_id)
        hits = [e["message"] for e in ev if sc["look"] in e["message"]] + ([j["error"]] if j.get("error") and sc["look"] in j["error"] else [])
        llm = [e["data"] for e in ev if e["type"] == "llm" and e["data"].get("input_tokens") is not None]
        budget = int(sc["env"].get("CONTEXT_TOKEN_BUDGET", get_settings().context_token_budget))
        over = [d for d in llm if d["input_tokens"] > budget]
        start = next(e for e in ev if e["message"].startswith("작업 시작"))["ts"]
        first_end = next((e["ts"] for e in ev if e["stage"] == "worker" and e["id"] > 0 and
                          (e["message"].startswith("담당자 답변 대기") or e["message"] == "완료" or e["type"] == "error")), None)
        res = c.get(f"/jobs/{job_id}/result")
        unresolved = (res.get("rule_version") or {}).get("spec", {}).get("unresolved", [])
        ok = j["status"] == sc["expect"] and bool(hits) and not over
        out = {"id": sc["id"], "what": sc["what"], "env": sc["env"], "job_id": job_id, "status": j["status"],
               "expected": sc["expect"], "evidence": hits[:4], "llm_calls": len(llm), "over_budget_calls": len(over),
               "unresolved_notes": [u for u in unresolved if "조사 중단" in u], "error": j.get("error"),
               "wall_seconds": round(time.time() - t0), "first_attempt": [start, first_end], "pass": ok}
        print(f"  {sc['id']}: {j['status']} (기대 {sc['expect']}) 근거 {len(hits)}건 → {'PASS' if ok else 'FAIL'}")
        return out
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def standard_calls() -> dict:
    """표준 분석의 모든 모델 호출: 하네스가 센 값과 실제 사용량이 기본 예산 안인지, 추정이 얼마나 정확한지."""
    jobs = [r["job_id"] for r in json.loads((OUT / "runs.json").read_text()) if r.get("job_id")]
    budget = get_settings().context_token_budget
    with session_scope() as s:
        data = [e.data for e in s.scalars(select(Event).where(Event.job_id.in_(jobs), Event.type == "llm"))
                if e.data.get("input_tokens") is not None]
    est = [d for d in data if d.get("est_input_tokens")]
    ratio = sorted(d["input_tokens"] / d["est_input_tokens"] for d in est)
    return {"calls": len(data), "budget": budget, "max_actual": max((d["input_tokens"] for d in data), default=0),
            "over_budget": sum(d["input_tokens"] > budget for d in data),
            "estimate_vs_actual": {"n": len(ratio), "median": round(ratio[len(ratio) // 2], 3) if ratio else None,
                                   "min": round(ratio[0], 3) if ratio else None, "max": round(ratio[-1], 3) if ratio else None}}


def main() -> None:
    c = Client()
    src, rep = c.inputs()
    compose("stop", "worker")
    try:
        results = [scenario(c, src, rep, sc) for sc in SCENARIOS]
    finally:
        compose("up", "-d", "--scale", "worker=3")
    std = standard_calls()
    h03 = [r for r in results if r["id"] in ("soft-time", "hard-time")]
    h04 = [r for r in results if r["id"] in ("run-budget", "call-budget")]
    runs03 = json.loads((OUT / "H-03-runs.json").read_text()) if (OUT / "H-03-runs.json").exists() else {}
    save("H-03", {"verdict": verdict(all(r["pass"] for r in h03) and runs03.get("verdict") == "PASS"),
                  "summary": f"한도 시나리오 {sum(r['pass'] for r in h03)}/{len(h03)} 통과, 표준 분석 {runs03.get('summary', '미측정')}",
                  "target": "100% 준수", "scenarios": h03, "standard_runs": runs03})
    save("H-04", {"verdict": verdict(all(r["pass"] for r in h04) and std["over_budget"] == 0),
                  "summary": f"예산 시나리오 {sum(r['pass'] for r in h04)}/{len(h04)} 통과, 표준 분석 모델 호출 {std['calls']}회 중 예산 초과 {std['over_budget']}회 (최대 {std['max_actual']:,} / {std['budget']:,}토큰)",
                  "target": "100% 준수", "scenarios": h04, "standard_runs": std})


if __name__ == "__main__":
    main()
