"""Q-06 장애 자동 복구 · Q-07 중단 후 재개.

    uv run python scripts/quality/faults.py

1. 코드 지점 장애 주입 (작업마다 시도 1·2·3에 하나씩): 모델 API 429, 503, MCP 연결 끊김 → 큐 지연 재시도.
2. 실제 장애: 분석 도중 business-mcp 컨테이너 중지(20초), RabbitMQ 재시작.
복구 = 재시도 한도 안에서 '나중에 답하기'까지 마치고 COMPLETED.
재개 검사 = 같은 실행이 체크포인트부터 이어졌고(이어서 실행 이벤트), 결과가 빠지거나 두 번 만들어지지 않았는지
(제안별 판정 1회, 규칙 버전 1개, 결과물 종류별 1개).
"""

import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select

from common import OUT, Client, compose, save, verdict
from rework.db.models import Artifact, Job, RuleVersion
from rework.db.session import session_scope

PLANS = [["llm_429", "llm_503", "mcp_error"], ["llm_503", "mcp_error", "llm_429"], ["mcp_error", "llm_429", "llm_503"]]
CODE_JOBS = 10
OUTAGE_ROUNDS = [("business-mcp", 2), ("rabbitmq", 2)]  # (대상, 회차 수). 회차마다 작업 3개가 동시에 겪는다


def finish(c: Client, job_id: str) -> str:
    j = c.wait(job_id, timeout=2400)
    if j["status"] == "WAITING_FOR_INPUT":
        c.answer_later(job_id)
        j = c.wait(job_id, timeout=1200, terminal={"COMPLETED", "FAILED"})
    return j["status"]


def integrity(c: Client, job_id: str) -> dict:
    ev = c.events(job_id)
    judged = Counter(e["message"].split(" ")[0] for e in ev if e["type"] == "status" and e["stage"] == "judge_rationale")
    resumes = sum(1 for e in ev if e["stage"] == "resume" and "체크포인트에서 이어서" in e["message"])
    with session_scope() as s:
        run_id = s.get(Job, job_id).current_run_id
        n_rv = s.scalar(select(func.count()).select_from(RuleVersion).where(RuleVersion.run_id == run_id))
        arts = Counter(s.scalars(select(Artifact.kind).where(Artifact.run_id == run_id)).all())
    res = c.get(f"/jobs/{job_id}/result")
    problems = []
    if any(v != 1 for v in judged.values()):
        problems.append(f"판정 중복 {dict(judged)}")
    if len(res["rationales"]) != len(judged) or not judged:
        problems.append(f"판정 누락 (저장 {len(res['rationales'])}, 판정 {len(judged)})")
    if n_rv != 1:
        problems.append(f"규칙 버전 {n_rv}개")
    if any(arts[k] != 1 for k in ("manual", "rules", "validation")):
        problems.append(f"결과물 {dict(arts)}")
    return {"checkpoint_resumes": resumes, "retries": sum(1 for e in ev if e["type"] == "retry"),
            "lease_waits": sum(1 for e in ev if "같은 실행을 잡고 있어" in e["message"]),
            "faults_seen": sum(1 for e in ev if e["type"] in ("fault", "error")), "problems": problems}


def code_faults(src: str, rep: str) -> list[dict]:
    def one(i: int) -> dict:
        c = Client()
        plan = PLANS[i % len(PLANS)]
        job_id = c.start(f"[품질] 장애 주입 {i:02d}", src, rep, faults=plan)
        status = finish(c, job_id)
        injected = sum(1 for e in c.events(job_id) if e["type"] == "fault")
        out = {"job_id": job_id, "plan": plan, "injected": injected, "final_status": status, **integrity(c, job_id)}
        print(f"  주입 {i:02d} {job_id} {plan} 주입 {injected} → {status} 재개 {out['checkpoint_resumes']} {out['problems']}")
        return out
    with ThreadPoolExecutor(3) as pool:
        return list(pool.map(one, range(CODE_JOBS)))


def outages(src: str, rep: str) -> list[dict]:
    out = []
    for target, rounds in OUTAGE_ROUNDS:
        for r in range(rounds):
            c = Client()
            jobs = [c.start(f"[품질] 실제 장애 {target} {r + 1}-{k + 1}", src, rep) for k in range(3)]
            # 세 작업이 모두 판단 근거 조사 단계에 들어가면 장애를 낸다.
            for _ in range(300):
                if all(any(e["stage"] == "investigate" for e in c.events(j)) for j in jobs):
                    break
                time.sleep(2)
            t0 = time.time()
            if target == "rabbitmq":
                compose("restart", "rabbitmq")
            else:
                compose("stop", "business-mcp")
                time.sleep(20)
                compose("start", "business-mcp")
            print(f"  {target} 장애 {r + 1}회차 ({time.time() - t0:.0f}초)")
            for j in jobs:
                status = finish(c, j)
                info = integrity(c, j)
                # 재시도·오류 기록, 체크포인트 재개, lease 대기(죽은 Worker 이어받기) 중 하나라도 있으면 장애를 겪은 것
                affected = info["retries"] > 0 or info["faults_seen"] > 0 or info["checkpoint_resumes"] > 0 \
                    or info["lease_waits"] > 0 or target == "rabbitmq"
                out.append({"job_id": j, "outage": target, "round": r + 1, "affected": affected,
                            "final_status": status, **info})
                print(f"    {j} → {status} 재시도 {info['retries']} 재개 {info['checkpoint_resumes']} {info['problems']}")
    return out


def main(mode: str = "all") -> None:
    c = Client()
    src, rep = c.inputs()
    if mode == "outages":  # 코드 지점 장애는 이전 측정을 그대로 쓰고 실제 장애만 다시 잰다 (결함 수정 후 재측정)
        code = json.loads((OUT / "Q-06-before.json").read_text())["code_fault_jobs"]
    else:
        code = code_faults(src, rep)
    real = outages(src, rep)
    (OUT / "fault_runs.json").write_text(json.dumps(
        [{"job_id": r["job_id"], "final_status": r["final_status"]} for r in code + real], ensure_ascii=False, indent=1))

    injections = [{"kind": k, "job_id": r["job_id"], "recovered": r["final_status"] == "COMPLETED"}
                  for r in code for k in r["plan"][:r["injected"]]]
    injections += [{"kind": r["outage"], "job_id": r["job_id"], "recovered": r["final_status"] == "COMPLETED"}
                   for r in real if r["affected"]]
    rec = sum(i["recovered"] for i in injections)
    rate = rec / len(injections) * 100 if injections else 0
    by_kind = {k: f"{sum(i['recovered'] for i in injections if i['kind'] == k)}/{sum(1 for i in injections if i['kind'] == k)}"
               for k in sorted({i["kind"] for i in injections})}
    save("Q-06", {"verdict": verdict(len(injections) >= 40 and rate >= 95),
                  "summary": f"장애 {len(injections)}회 중 자동 복구 {rec}회 ({rate:.1f}%) — " + ", ".join(f"{k} {v}" for k, v in by_kind.items()),
                  "target": "95% 이상 (재시도 한도 3회 안에서)", "injections": len(injections), "recovered": rec,
                  "rate_pct": round(rate, 1), "by_kind": by_kind, "code_fault_jobs": code, "outage_jobs": real})

    runs = code + real
    resumes = sum(r["checkpoint_resumes"] for r in runs)
    bad = [r for r in runs if r["checkpoint_resumes"] and (r["problems"] or r["final_status"] != "COMPLETED")]
    ok = sum(r["checkpoint_resumes"] for r in runs if r not in bad)
    save("Q-07", {"verdict": verdict(resumes >= 20 and not bad),
                  "summary": f"체크포인트 재개 {resumes}회 중 손실·중복 없이 완료 {ok}회 ({ok / resumes * 100 if resumes else 0:.1f}%)",
                  "target": "100%", "resumes": resumes, "ok": ok,
                  "checks": "제안별 판정 1회, 저장된 판정 수 = 판정 수, 규칙 버전 1개, 결과물(manual·rules·validation) 각 1개",
                  "failures": [{"job_id": r["job_id"], "problems": r["problems"], "status": r["final_status"]} for r in bad]})


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")
