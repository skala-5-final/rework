"""Q-13 자동화 테스트 커버리지: 핵심 모듈 Line ≥ 85%, Branch ≥ 75%.

    uv run pytest --cov=src/rework --cov-branch --cov-report=json:var/quality/coverage.json
    uv run python scripts/quality/coverage_check.py

핵심 모듈 = 모델·외부 연결 없이 결과가 정해지는 업무·보안·하네스 로직 (측정 전에 고정).
모델 호출 노드·Worker·API 라우팅은 실제 분석 실행(Q-05~Q-10, H-01~H-05)으로 검증하므로 참고 수치로만 남긴다.
"""

import json

from common import OUT, save, verdict

CORE = ["contracts/rules.py", "contracts/explain.py", "auth/security.py", "auth/mcp_scope.py", "privacy.py",
        "observability.py", "storage.py", "agent/budget.py", "agent/policy.py", "agent/limits.py",
        "business_mcp/services/calc.py", "business_mcp/services/analysis.py",
        "business_mcp/services/report_parser.py", "business_mcp/services/dataset.py"]


def main() -> None:
    files = json.loads((OUT / "coverage.json").read_text())["files"]
    rows, tot = [], {"lines": 0, "covered": 0, "branches": 0, "covered_branches": 0}
    for name in CORE:
        s = files[f"src/rework/{name}"]["summary"]
        rows.append({"module": name, "line_pct": round(s["covered_lines"] / s["num_statements"] * 100, 1) if s["num_statements"] else 100,
                     "branch_pct": round(s["covered_branches"] / s["num_branches"] * 100, 1) if s["num_branches"] else 100})
        tot["lines"] += s["num_statements"]
        tot["covered"] += s["covered_lines"]
        tot["branches"] += s["num_branches"]
        tot["covered_branches"] += s["covered_branches"]
    line = tot["covered"] / tot["lines"] * 100
    branch = tot["covered_branches"] / tot["branches"] * 100
    allf = json.loads((OUT / "coverage.json").read_text())["totals"]
    save("Q-13", {"verdict": verdict(line >= 85 and branch >= 75),
                  "summary": f"핵심 모듈 {len(CORE)}개 Line {line:.1f}% · Branch {branch:.1f}% (목표 85% · 75%), 전체 참고 {allf['percent_covered']:.1f}%",
                  "target": "Line ≥ 85%, Branch ≥ 75%", "line_pct": round(line, 1), "branch_pct": round(branch, 1),
                  "modules": rows, "all_src_percent": round(allf["percent_covered"], 1)})


if __name__ == "__main__":
    main()
