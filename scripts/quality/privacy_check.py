"""Q-12 민감정보 보호율: 개인정보가 든 입력 50건이 모델 입력 · 진행 이벤트 로그 · LangSmith 전송본에서 가려지는지.

    uv run python scripts/quality/privacy_check.py

정책(privacy.py)이 정의한 대상: 이메일, 전화번호, 주민번호(하이픈), 카드번호, 이름(이름 칸·직함 앞).
업무 문장 30건이 잘못 가려지지 않는지(오탐)도 함께 잰다. 오탐은 보조 지표로 남긴다.
"""

import json

from langchain_core.messages import HumanMessage, ToolMessage
from sqlalchemy import delete, select

from common import OUT, save, verdict
from rework.db.models import Event
from rework.db.session import record_event, session_scope
from rework.privacy import mask_messages, mask_obj

NAMES = ["김민수", "이영희", "박지훈", "최수진", "정우성", "한지민", "오세훈", "윤아름", "장동건", "서지혜"]
CASES = (
    [(f"문의는 010-{1000 + i * 7:04d}-{2000 + i * 13:04d}로 주세요", f"010-{1000 + i * 7:04d}-{2000 + i * 13:04d}") for i in range(11)]
    + [("매장 전화 02-345-6789", "02-345-6789"), ("본사 031.234.5678", "031.234.5678"), ("010 9876 5432 로 연락", "010 9876 5432")]
    + [(f"담당 메일 {n}@store{i}.co.kr", f"{n}@store{i}.co.kr") for i, n in enumerate(["minsu", "yh.lee", "jihoon_p", "sj+choi", "admin"])]
    + [(f"주민번호 {900101 + i}-{1 + i % 4}{234567 + i:06d}", f"{900101 + i}-{1 + i % 4}{234567 + i:06d}") for i in range(5)]
    + [(f"카드 {4111 + i}-1111-2222-{3333 + i}", f"{4111 + i}-1111-2222-{3333 + i}") for i in range(5)]
    + [(f"작성자: {n}", n) for n in NAMES[:5]]
    + [(f"결재: {n} / 검토자: 홍길동", n) for n in NAMES[5:8]]
    + [(f"{n} 과장 지시로 진행", n) for n in NAMES[:4]]
    + [(f"{n}팀장 확인 후 발주", n) for n in NAMES[4:8]]
    + [(f"{n} 점장 요청", n) for n in NAMES[8:10]]
    + [("성명: 김철수, 연락처 010-1111-2222", "김철수"), ("보고자 : 이민호", "이민호"), ("박서준 대리가 정리", "박서준")]
)
NORMAL = ["2026-06-01 단팥빵 판매 12개", "gtin 8801234567890", "매출 1,234,000원", "대표 상품은 제로콜라",
          "담당자 확인 필요", "고객님 문의 증가", "과장된 표현 없이 작성", "전월 대비 15% 감소", "금요일 일평균 32.2개",
          "참치캔 3.9 → 3.2 → 2.6", "행사 비중 41.4%", "영업일수 30일", "제육볶음 도시락 행사 효과 +3%", "카테고리별 실적",
          "상위 10개 품목", "2026-07 부분월", "S1 매출액 18,199천원", "r3 결과 참조", "P1-2 판단 보류", "일평균 매출 606천원",
          "생수 500mL 묶음 진열", "에너지드링크 6월 급증", "할인액 1,048.5천원", "거래건수 9,912건", "주차 3주차 점검",
          "냉장 쇼케이스 1칸", "초코바 +155%", "감자칩 +82%", "크림빵 5월만 높음", "점장 회의 안건"]


def main() -> None:
    assert len(CASES) >= 50, len(CASES)
    cases = CASES[:50]
    job_id = next(r["job_id"] for r in json.loads((OUT / "runs.json").read_text()) if r.get("job_id"))
    results = []
    # 1) 모델 입력: 사람 메시지·도구 결과 모두
    msgs = [HumanMessage(t) if i % 2 else ToolMessage(content=json.dumps({"note": t}, ensure_ascii=False), tool_call_id=f"c{i}")
            for i, (t, _) in enumerate(cases)]
    masked, _ = mask_messages(msgs)
    # 2) 진행 이벤트 로그: 실제 DB에 쓰고 다시 읽는다 (측정 후 지움)
    for t, _ in cases:
        record_event(job_id, None, "status", "privacy_check", t, {"answer": t, "nested": [{"text": t}]})
    with session_scope() as s:
        rows = s.scalars(select(Event).where(Event.job_id == job_id, Event.stage == "privacy_check").order_by(Event.id)).all()
        logged = [(e.message, json.dumps(e.data, ensure_ascii=False)) for e in rows][-len(cases):]
        s.execute(delete(Event).where(Event.job_id == job_id, Event.stage == "privacy_check"))
    # 3) LangSmith 전송본: 공용 클라이언트에 건 hide_inputs/hide_outputs와 같은 함수
    sent = [json.dumps(mask_obj({"messages": [{"content": t}]}), ensure_ascii=False) for t, _ in cases]
    for i, (t, secret) in enumerate(cases):
        where = {"모델 입력": secret not in str(masked[i].content), "이벤트 메시지": secret not in logged[i][0],
                 "이벤트 data": secret not in logged[i][1], "LangSmith 전송본": secret not in sent[i]}
        results.append({"input": t, "secret": secret, **where, "pass": all(where.values())})
    fp = [t for t in NORMAL if mask_obj(t) != t]
    ok = sum(r["pass"] for r in results)
    save("Q-12", {"verdict": verdict(ok == len(results)),
                  "summary": f"민감정보 사례 {len(results)}건 × 4곳(모델 입력·이벤트 메시지·이벤트 data·LangSmith) 보호 {ok}건 ({ok / len(results) * 100:.0f}%), 업무 문장 오탐 {len(fp)}/{len(NORMAL)}",
                  "target": "100%", "cases": len(results), "protected": ok, "false_positives": fp,
                  "normal_sentences": len(NORMAL), "failed": [r for r in results if not r["pass"]],
                  "e2e_evidence": "job_4997e8911796: 답변에 이름·전화번호 입력 → 모델 입력 마스킹 이벤트 1회, 이벤트 로그 원문 0건",
                  "scope_note": "DB 업무 테이블(답변 원본)은 마스킹하지 않는다 (회사 내부 저장소, 정책상 대상 아님)"})


if __name__ == "__main__":
    main()
