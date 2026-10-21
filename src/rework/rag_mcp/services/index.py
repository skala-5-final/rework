"""업무 지식 색인: 문서를 제목 단위로 나누고 OpenAI 임베딩으로 pgvector에 저장한다."""

import re

from openai import OpenAI
from sqlalchemy import delete, func, select

from rework.config import get_settings
from rework.db.models import Document, DocumentChunk

MIN_SCORE = 0.30  # 이보다 낮은 유사도는 "관련 규정 없음"으로 본다
MAX_CHARS = 900


def _client() -> OpenAI:
    return OpenAI(api_key=get_settings().openai_api_key, timeout=30, max_retries=2)


def embed(texts: list[str]) -> list[list[float]]:
    res = _client().embeddings.create(model=get_settings().embedding_model, input=texts)
    return [d.embedding for d in res.data]


def chunk_markdown(content: str) -> list[tuple[str, str]]:
    """(제목, 본문) 목록. ##/### 제목 단위로 자르고 너무 길면 문단 단위로 다시 자른다."""
    parts: list[tuple[str, str]] = []
    title, buf = "", []
    for line in content.splitlines():
        if re.match(r"^#{1,3}\s", line):
            if "".join(buf).strip():
                parts.append((title, "\n".join(buf).strip()))
            title, buf = line.lstrip("#").strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        parts.append((title, "\n".join(buf).strip()))
    out = []
    for t, body in parts:
        while len(body) > MAX_CHARS:
            cut = body.rfind("\n", 0, MAX_CHARS)
            cut = cut if cut > 200 else MAX_CHARS
            out.append((t, body[:cut]))
            body = body[cut:].strip()
        out.append((t, body))
    return out


def register_document(s, *, tenant_id: str, title: str, content: str, kind: str = "guideline") -> dict:
    prev = s.scalar(select(func.max(Document.version)).where(Document.tenant_id == tenant_id, Document.title == title))
    doc = Document(tenant_id=tenant_id, title=title, kind=kind, version=(prev or 0) + 1, content=content)
    s.add(doc)
    s.flush()
    # 같은 제목의 이전 버전 조각은 검색 대상에서 뺀다 (원본은 documents에 남는다)
    old_ids = select(Document.id).where(Document.tenant_id == tenant_id, Document.title == title, Document.id != doc.id)
    s.execute(delete(DocumentChunk).where(DocumentChunk.document_id.in_(old_ids)))
    chunks = chunk_markdown(content)
    vectors = embed([f"{title} / {t}\n{b}" for t, b in chunks]) if chunks else []
    for i, ((t, body), v) in enumerate(zip(chunks, vectors)):
        s.add(DocumentChunk(document_id=doc.id, tenant_id=tenant_id, chunk_index=i, title=t or title,
                            content=body, source_location=f"{title} § {t or '본문'}", embedding=v))
    return {"document_id": doc.id, "document_version": doc.version, "chunks": len(chunks)}


def search(s, *, tenant_id: str, query: str, top_k: int = 4) -> list[dict]:
    v = embed([query])[0]
    dist = DocumentChunk.embedding.cosine_distance(v)
    rows = s.execute(
        select(DocumentChunk, Document, dist.label("d"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.tenant_id == tenant_id)
        .order_by(dist).limit(top_k)
    ).all()
    out = []
    for chunk, doc, d in rows:
        score = 1 - float(d)
        if score < MIN_SCORE:
            continue
        out.append({"document_id": doc.id, "document_version": doc.version, "chunk_id": chunk.id,
                    "title": doc.title, "section": chunk.title, "excerpt": chunk.content[:500],
                    "source_location": chunk.source_location, "kind": doc.kind, "score": round(score, 3)})
    return out


def excerpt(s, *, tenant_id: str, chunk_id: str) -> dict:
    chunk = s.get(DocumentChunk, chunk_id)
    if chunk is None or chunk.tenant_id != tenant_id:
        raise LookupError("문서 조각을 찾을 수 없습니다.")
    doc = s.get(Document, chunk.document_id)
    return {"document_id": doc.id, "document_version": doc.version, "chunk_id": chunk.id, "title": doc.title,
            "excerpt": chunk.content, "source_location": chunk.source_location}


KINDS = ("guideline", "dictionary", "confirmed")


def list_documents(s, *, tenant_id: str, kind: str) -> list[dict]:
    """종류별 문서 목록. 제목마다 최신 버전만 돌려준다."""
    if kind not in KINDS:
        raise LookupError(f"문서 종류는 {' | '.join(KINDS)} 중 하나입니다.")
    latest = (select(Document.title, func.max(Document.version).label("v"))
              .where(Document.tenant_id == tenant_id, Document.kind == kind).group_by(Document.title).subquery())
    rows = s.scalars(select(Document).join(latest, (Document.title == latest.c.title) & (Document.version == latest.c.v))
                     .where(Document.tenant_id == tenant_id, Document.kind == kind).order_by(Document.created_at))
    return [{"document_id": d.id, "title": d.title, "version": d.version,
             "created_at": d.created_at.isoformat()} for d in rows]
