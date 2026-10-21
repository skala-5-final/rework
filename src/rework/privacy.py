"""개인정보 마스킹. 모델 입력, LangSmith 기록, 진행 이벤트 로그로 나가기 전에 가린다.

원천 판매 데이터에는 개인정보가 없지만, 보고서 PDF(작성자·결재란), 업무 규정 문서, 담당자 자유 답변에는 들어올 수 있다.
DB 원본은 그대로 둔다 (회사 내부 저장소). 밖으로 나가는 복사본만 가린다.
"""

import re

# 일반 단어와 겹치는 호칭(님·씨·대표·사원 등)은 넣지 않는다: '고객님', '대표 상품'이 가려지면 안 된다.
TITLES = "과장|대리|부장|차장|팀장|주임|점장|실장|매니저"
PARTICLES = "에게|께서|께|이|가|은|는|을|를|의|도|와|과|랑"
# 숫자 경계는 \b 대신 앞뒤 숫자 여부로 본다: '010-1234-5678로'처럼 한글 조사가 붙으면 \b가 경계로 보지 않는다.
PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[이메일]"),
    # 하이픈 필수: 13자리 상품 바코드(gtin)를 주민번호로 오인하지 않게
    ("rrn", re.compile(r"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)"), "[주민번호]"),
    ("card", re.compile(r"(?<!\d)\d{4}-\d{4}-\d{4}-\d{4}(?!\d)"), "[카드번호]"),
    ("phone", re.compile(r"(?<![\d.])(?:01[016789]|0\d{1,2})[-. ]?\d{3,4}[-. ]?\d{4}(?!\d)"), "[전화번호]"),
    # '작성자: 김민수' 처럼 이름 칸 (콜론이 있을 때만. '담당자 확인'의 '확인'을 가리지 않게)
    ("name_field", re.compile(r"((?:작성자|담당자|결재|성명|이름|보고자|검토자)\s*[:：]\s*)[가-힣]{2,4}(?![가-힣])"), r"\1[이름]"),
    # '김민수 과장', '박지훈팀장', '박서준 대리가' 처럼 직함 앞. 직함 뒤에는 호칭·조사만 허용해 '과장된'은 제외한다.
    ("name_title", re.compile(rf"(?<![가-힣])[가-힣]{{2,4}}(?=\s?(?:{TITLES})님?(?:{PARTICLES})?(?![가-힣]))"), "[이름]"),
]


def mask(text: str) -> str:
    for _, pat, repl in PATTERNS:
        text = pat.sub(repl, text)
    return text


def mask_obj(x):
    """dict·list 안의 문자열을 모두 가린다 (LangSmith 입력·출력, 이벤트 data)."""
    if isinstance(x, str):
        return mask(x)
    if isinstance(x, dict):
        return {k: mask_obj(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [mask_obj(v) for v in x]
    return x


def mask_messages(messages: list) -> tuple[list, int]:
    """LangChain 메시지의 본문만 가린 복사본과 가린 메시지 수. 도구 호출 정보(tool_calls)와 ID는 그대로 둔다."""
    out, changed = [], 0
    for m in messages:
        c = m.content
        if isinstance(c, str):
            masked = mask(c)
        elif isinstance(c, list):
            masked = [{**p, "text": mask(p["text"])} if isinstance(p, dict) and isinstance(p.get("text"), str) else p for p in c]
        else:
            masked = c
        if masked != c:
            changed += 1
        out.append(m if masked == c else m.model_copy(update={"content": masked}))
    return out, changed
