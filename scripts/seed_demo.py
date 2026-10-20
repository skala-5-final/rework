"""데모 회사·사용자를 만들고 업무 지식(지침·데이터 사전)을 등록한다.

    uv run python scripts/seed_demo.py
"""

import csv
from pathlib import Path

from sqlalchemy import select

from rework.auth.security import hash_password
from rework.db.models import Document, Tenant, User
from rework.db.session import init_db, session_scope
from rework.rag_mcp.services import index

ROOT = Path(__file__).resolve().parents[1]
USERS = [("demo-store", "ST001 점포 운영팀", "demo", "demo1234", "점포 운영 담당"),
         ("other-co", "다른 회사", "other", "other1234", "다른 회사 사용자")]


SOURCE_CSV = "판매분석가_일별상세.csv"
COLUMN_SPEC = ROOT / "data" / "컬럼명세서.csv"


def data_dictionary() -> str | None:
    """컬럼명세서에서 일별상세 열 설명만 뽑는다. 계산식(source_or_formula)은 넣지 않는다. 명세서가 없으면 None."""
    if not COLUMN_SPEC.exists():
        return None
    lines = ["# 데이터 사전: 판매분석가 일별상세", "",
             f"원천 데이터 {SOURCE_CSV}의 열 설명이다.", ""]
    with open(COLUMN_SPEC, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            # 예전 명세서는 파일 이름 앞에 agent_가 붙어 있다.
            if row["file_name"].removeprefix("agent_") != SOURCE_CSV:
                continue
            lines += [f"## {row['column_name']}",
                      f"- 타입: {row['data_type']} · 단위: {row['unit'] or '-'} · 설명: {row['description']}", ""]
    return "\n".join(lines)


def main() -> None:
    init_db()
    with session_scope() as s:
        for tid, tname, username, pw, display in USERS:
            if s.get(Tenant, tid) is None:
                s.add(Tenant(id=tid, name=tname))
                s.flush()
            if s.scalar(select(User).where(User.username == username)) is None:
                s.add(User(tenant_id=tid, username=username, password_hash=hash_password(pw), display_name=display))
        docs = [("점포 월간 판매 실적 보고서 작성 지침", (ROOT / "docs/knowledge/점포_보고서_작성_지침.md").read_text(), "guideline")]
        if (dictionary := data_dictionary()) is not None:
            docs.append(("데이터 사전: 판매분석가 일별상세", dictionary, "dictionary"))
        else:
            print(f"데이터 사전 건너뜀: {COLUMN_SPEC.relative_to(ROOT)} 없음")
        for title, content, kind in docs:
            if s.scalar(select(Document).where(Document.tenant_id == "demo-store", Document.title == title)) is None:
                print(title, index.register_document(s, tenant_id="demo-store", title=title, content=content, kind=kind))
    print("완료: demo/demo1234, other/other1234")


if __name__ == "__main__":
    main()
