"""Q-11 비인가 접근 차단 (API): 토큰 없음·형식 오류·서명 위조·만료 토큰, 다른 회사 데이터 접근.

    uv run python scripts/quality/access_api.py

정상 요청(대조군)은 통과해야 하고, 비인가 요청은 모두 401(인증 실패) 또는 404(다른 회사 데이터는 존재도 알리지 않음)여야 한다.
MCP 쪽은 컨테이너 안에서 mcp_checks.py가 측정하고, 두 결과를 합쳐 Q-11로 판정한다.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import jwt

from common import API, OUT, Client, save, verdict
from rework.config import get_settings


def main() -> None:
    demo, other = Client("demo", "demo1234"), Client("other", "other1234")
    job_id = next(r["job_id"] for r in json.loads((OUT / "runs.json").read_text()) if r.get("final_status") == "COMPLETED")
    job = demo.get(f"/jobs/{job_id}")
    run_id = job["run"]["run_id"]
    art = demo.get(f"/jobs/{job_id}/artifacts")[0]["artifact_id"]
    src, _ = demo.inputs()
    q = demo.get(f"/jobs/{job_id}/questions")
    qid = q[0]["question_id"] if q else "q_none"

    reads = [f"/jobs/{job_id}", f"/jobs/{job_id}/events", f"/jobs/{job_id}/questions", f"/jobs/{job_id}/result",
             f"/jobs/{job_id}/artifacts", f"/artifacts/{art}/download", f"/artifacts/{art}/preview", "/jobs", "/documents"]
    writes = [("post", f"/jobs/{job_id}/answers", {"run_id": run_id, "run_version": job["run"]["version"],
                                                    "answers": [{"question_id": qid, "choice": "other", "text": "x"}]}),
              ("post", f"/jobs/{job_id}/rerun", {"source_file_id": src, "month": "2026-05"}),
              ("post", "/jobs", {"name": "[품질] 접근 시험", "source_file_id": src, "report_file_id": src})]

    secret = get_settings().jwt_secret
    claims = {"sub": "u", "tenant": "demo-store"}
    bad_tokens = {
        "토큰 없음": None,
        "형식 오류": "not-a-jwt",
        "서명 위조": jwt.encode({**claims, "exp": datetime.now(UTC) + timedelta(hours=1)}, "wrong-secret-0123456789abcdef0123", algorithm="HS256"),
        "만료": jwt.encode({**claims, "exp": datetime.now(UTC) - timedelta(minutes=1)}, secret, algorithm="HS256"),
        "알고리즘 none": jwt.encode({**claims, "exp": datetime.now(UTC) + timedelta(hours=1)}, None, algorithm="none"),
    }
    cases = []

    def call(client: httpx.Client, method: str, path: str, body=None, headers=None):
        return client.request(method.upper(), path, json=body, headers=headers or {}).status_code

    raw = httpx.Client(base_url=API, timeout=30)
    # 1) 인증 실패: 보호된 엔드포인트 × 위조 토큰 종류 → 401
    for name, tok in bad_tokens.items():
        h = {"Authorization": f"Bearer {tok}"} if tok else {}
        for p in reads + ["/auth/me"]:
            st = call(raw, "get", p, headers=h)
            cases.append({"kind": "인증 실패", "token": name, "req": f"GET {p}", "status": st, "blocked": st == 401})
        for m, p, b in writes:
            st = call(raw, m, p, b, headers=h)
            cases.append({"kind": "인증 실패", "token": name, "req": f"{m.upper()} {p}", "status": st, "blocked": st == 401})
    # 2) 다른 회사: other 사용자가 demo의 자원에 접근 → 404 (존재 여부도 알리지 않음)
    for p in reads[:7]:
        st = call(other.http, "get", p)
        cases.append({"kind": "회사 격리", "req": f"GET {p}", "status": st, "blocked": st == 404})
    for m, p, b in writes:
        st = call(other.http, m, p, b)
        cases.append({"kind": "회사 격리", "req": f"{m.upper()} {p}", "status": st, "blocked": st == 404})
    listed = other.get("/jobs")
    cases.append({"kind": "회사 격리", "req": "GET /jobs (목록에 다른 회사 작업)", "status": 200,
                  "blocked": all(j["job_id"] != job_id for j in listed)})
    # 3) 대조군: 정상 사용자의 정상 요청은 통과
    controls = [{"req": f"GET {p}", "status": call(demo.http, "get", p)} for p in reads]
    controls_ok = all(c["status"] == 200 for c in controls)

    blocked = sum(c["blocked"] for c in cases)
    rate = blocked / len(cases) * 100
    save("Q-11-api", {"verdict": verdict(blocked == len(cases) and controls_ok),
                      "summary": f"비인가 API 요청 {len(cases)}건 중 차단 {blocked}건 ({rate:.1f}%), 정상 요청 {sum(c['status'] == 200 for c in controls)}/{len(controls)} 통과",
                      "target": "100%", "requests": len(cases), "blocked": blocked, "rate_pct": round(rate, 1),
                      "controls": controls, "not_blocked": [c for c in cases if not c["blocked"]]})


if __name__ == "__main__":
    main()
