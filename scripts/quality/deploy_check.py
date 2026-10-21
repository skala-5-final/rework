"""Q-16 신규 환경 배포: 빈 볼륨의 새 compose 프로젝트를 3번 만들고, 코드 수정 없이 설치·기동·분석까지 되는지.

    uv run python scripts/quality/deploy_check.py

README의 절차 그대로: `docker compose up -d --build` → `seed_demo.py` → 로그인 → 업로드 → 분석.
기존 스택(데이터 포함)은 잠시 멈췄다가 그대로 다시 켠다. 새 프로젝트는 볼륨까지 지운다(-v).
"""

import subprocess
import time

from common import ROOT, Client, save, verdict

PROJECT = "rwq16"


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=ROOT, capture_output=True, text=True, check=check)


def cycle(i: int) -> dict:
    steps, t0 = {}, time.time()
    try:
        r = sh("docker", "compose", "-p", PROJECT, "up", "-d", "--build", "--wait", check=False)
        steps["up"] = r.returncode == 0 or "worker" in r.stderr  # worker는 healthcheck가 없어 --wait 경고만 남는다
        steps["seed"] = sh("docker", "compose", "-p", PROJECT, "exec", "-T", "api", "python", "scripts/seed_demo.py",
                           check=False).returncode == 0
        for _ in range(30):
            try:
                c = Client()
                break
            except Exception:  # noqa: BLE001
                time.sleep(2)
        steps["login"] = True
        src, rep = c.inputs()
        steps["upload"] = True
        job = c.start(f"[품질] 배포 검증 {i}", src, rep)
        j = c.wait(job, timeout=900)
        steps["analysis"] = j["status"] in ("WAITING_FOR_INPUT", "COMPLETED")
        steps["job"] = job
    except Exception as e:  # noqa: BLE001
        steps["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        sh("docker", "compose", "-p", PROJECT, "down", "-v", "--remove-orphans", check=False)
    ok = all(steps.get(k) for k in ("up", "seed", "login", "upload", "analysis"))
    print(f"  {i}회차: {'PASS' if ok else 'FAIL'} {steps} ({time.time() - t0:.0f}초)")
    return {"cycle": i, "pass": ok, "seconds": round(time.time() - t0), **steps}


def main() -> None:
    sh("docker", "compose", "stop")
    try:
        cycles = [cycle(i) for i in (1, 2, 3)]
    finally:
        sh("docker", "compose", "up", "-d", "--scale", "worker=3")
    n = sum(c["pass"] for c in cycles)
    save("Q-16", {"verdict": verdict(n == len(cycles)),
                  "summary": f"빈 환경 배포 {len(cycles)}회 중 {n}회 성공 (설치·시드·로그인·업로드·분석까지)",
                  "target": "100% (3회 이상)", "cycles": cycles,
                  "procedure": "docker compose -p rwq16 up -d --build --wait → seed_demo.py → API 로그인·업로드·분석 → down -v"})


if __name__ == "__main__":
    main()
