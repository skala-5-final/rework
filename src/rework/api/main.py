"""FastAPI: 인증 · 파일 · 작업 · 질문/답변 · 결과 조회 · 화면."""

import asyncio
import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path

import markdown
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from rework.auth.security import COOKIE, CurrentUser, create_token, current_user, verify_password
from rework.business_mcp.services.dataset import missing_columns
from rework.db.models import (Answer, Artifact, Document, Event, File as FileRow, Job, Outbox, Question, Rationale,
                              RuleVersion, Run, User, new_id)
from rework.db.session import get_db, init_db
from rework.observability import summarize_usage
from rework.queue.rabbit import outbox_relay
from rework.rag_mcp.services import index as rag_index
from rework.contracts import explain as X
from rework.storage import resolve, save_bytes

WEB = Path(__file__).resolve().parents[1] / "web"
templates = Jinja2Templates(directory=WEB / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    stop = asyncio.Event()
    task = asyncio.create_task(outbox_relay(stop))
    yield
    stop.set()
    await task


app = FastAPI(title="Re:Work", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")


def _owned(db: Session, model, obj_id: str, user: CurrentUser):
    """다른 회사의 데이터는 존재 여부도 알리지 않는다."""
    obj = db.get(model, obj_id)
    if obj is None or obj.tenant_id != user.tenant_id:
        raise HTTPException(404, "찾을 수 없습니다.")
    return obj


def _enqueue(db: Session, job: Job, run: Run, action: str) -> None:
    """작업 상태와 발행 예정 이벤트를 한 트랜잭션에 저장한다 (outbox)."""
    db.add(Outbox(payload={"message_id": new_id("msg"), "job_id": job.id, "run_id": run.id,
                           "action": action, "attempt": 1}))


# ---------------- 인증 ----------------

class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
async def login(request: Request, db: Session = Depends(get_db)):
    if request.headers.get("content-type", "").startswith("application/json"):
        body = LoginBody.model_validate(await request.json())
    else:
        form = await request.form()
        body = LoginBody(username=str(form.get("username", "")), password=str(form.get("password", "")))
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")
    token = create_token(user)
    resp = JSONResponse({"access_token": token, "token_type": "bearer", "user": user.display_name})
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax")
    return resp


@app.post("/auth/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/auth/me")
def me(user: CurrentUser = Depends(current_user)):
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "tenant_id": user.tenant_id}


# ---------------- 파일 ----------------

@app.post("/files")
async def upload_file(kind: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db),
                      user: CurrentUser = Depends(current_user)):
    data = await file.read()
    if kind == "source":
        try:
            header = next(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        except (UnicodeDecodeError, StopIteration) as e:
            raise HTTPException(400, "UTF-8 CSV 파일이 아닙니다.") from e
        if miss := missing_columns(header):
            raise HTTPException(400, f"원천 CSV에 필수 열이 없습니다: {', '.join(miss)}")
    elif kind == "report":
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, "완성 보고서는 PDF 파일이어야 합니다.")
    elif kind == "document":
        if not file.filename.lower().endswith((".md", ".txt")):
            raise HTTPException(400, "업무 규정 문서는 .md 또는 .txt 파일이어야 합니다.")
    else:
        raise HTTPException(400, "kind는 source | report | document 중 하나입니다.")
    fid = new_id("file")
    ref, digest = save_bytes(user.tenant_id, fid, file.filename, data)
    db.add(FileRow(id=fid, tenant_id=user.tenant_id, kind=kind, original_name=file.filename, path=ref,
                   sha256=digest, size=len(data)))
    return {"file_id": fid, "sha256": digest, "size": len(data)}


# ---------------- 작업 ----------------

class JobBody(BaseModel):
    name: str
    source_file_id: str
    report_file_id: str
    inject_failure: bool = False


@app.post("/jobs")
def create_job(body: JobBody, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    src = _owned(db, FileRow, body.source_file_id, user)
    rep = _owned(db, FileRow, body.report_file_id, user)
    if src.kind != "source" or rep.kind != "report":
        raise HTTPException(400, "원천 CSV와 완성 보고서 PDF를 올바르게 지정하세요.")
    job = Job(tenant_id=user.tenant_id, created_by=user.id, name=body.name, source_file_id=src.id,
              report_file_id=rep.id, inject_failure=body.inject_failure)
    db.add(job)
    db.flush()
    run = Run(job_id=job.id, tenant_id=user.tenant_id, input_hashes={"source": src.sha256, "report": rep.sha256})
    db.add(run)
    db.flush()
    job.current_run_id = run.id
    _enqueue(db, job, run, "start")
    db.add(Event(job_id=job.id, run_id=run.id, type="status", stage="queued", message="작업 등록 (대기 중)"))
    return {"job_id": job.id, "run_id": run.id, "status": job.status}


def _job_view(db: Session, job: Job) -> dict:
    run = db.get(Run, job.current_run_id) if job.current_run_id else None
    src, rep = db.get(FileRow, job.source_file_id), db.get(FileRow, job.report_file_id)
    return {"job_id": job.id, "name": job.name, "status": job.status, "error": job.error, "attempts": job.attempts,
            "created_at": job.created_at.isoformat(), "updated_at": job.updated_at.isoformat(),
            "run": {"run_id": run.id, "version": run.version, "status": run.status, "kind": run.kind} if run else None,
            "source_file": src.original_name, "report_file": rep.original_name, "inject_failure": job.inject_failure}


@app.get("/jobs")
def list_jobs(db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    jobs = db.scalars(select(Job).where(Job.tenant_id == user.tenant_id).order_by(Job.created_at.desc()))
    return [_job_view(db, j) for j in jobs]


@app.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    job = _owned(db, Job, job_id, user)
    llm = db.scalars(select(Event.data).where(Event.job_id == job_id, Event.type == "llm"))
    last = db.scalar(select(Event).where(Event.job_id == job_id, Event.trace_id.is_not(None))
                     .order_by(Event.id.desc()).limit(1))
    link = db.scalar(select(Event.data).where(Event.job_id == job_id, Event.type == "trace")
                     .order_by(Event.id.desc()).limit(1))
    # 토큰·비용은 작업의 모든 시도(분석·재개·갱신)를 합친다. trace는 가장 최근 시도.
    return {**_job_view(db, job), "usage": summarize_usage(list(llm)),
            "trace": {"trace_id": last.trace_id if last else None,
                      "url": link.get("url") if link and last and link.get("trace_id") == last.trace_id else None}}


@app.get("/jobs/{job_id}/events")
def job_events(job_id: str, after: int = 0, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    _owned(db, Job, job_id, user)
    rows = db.scalars(select(Event).where(Event.job_id == job_id, Event.id > after).order_by(Event.id).limit(500))
    return [{"id": e.id, "ts": e.ts.isoformat(), "type": e.type, "stage": e.stage, "message": e.message,
             "data": e.data, "run_id": e.run_id, "trace_id": e.trace_id} for e in rows]


@app.get("/jobs/{job_id}/questions")
def job_questions(job_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    job = _owned(db, Job, job_id, user)
    rows = db.scalars(select(Question).where(Question.run_id == job.current_run_id).order_by(Question.created_at))
    out = []
    for q in rows:
        a = db.scalar(select(Answer).where(Answer.question_id == q.id))
        out.append({"question_id": q.id, "key": q.key, "kind": q.kind, "text": q.text, "reason": q.reason,
                    "options": q.options, "context": q.context, "status": q.status,
                    "answer": {"choice": a.choice, "text": a.text} if a else None})
    return out


class AnswerItem(BaseModel):
    question_id: str
    choice: str = ""
    text: str = ""


class AnswersBody(BaseModel):
    run_id: str
    run_version: int
    answers: list[AnswerItem]


@app.post("/jobs/{job_id}/answers")
def submit_answers(job_id: str, body: AnswersBody, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(current_user)):
    """실행 중 질문에 답하거나(답 없이 '나중에 답하기'도 가능), 완료 후 남은 질문에 언제든 답한다."""
    job = _owned(db, Job, job_id, user)
    run = db.get(Run, body.run_id)
    # 오래된 실행이나 중복 제출로 재개 작업이 여러 번 생기지 않게 막는다.
    if run is None or run.id != job.current_run_id or run.version != body.run_version:
        raise HTTPException(409, "이미 처리됐거나 오래된 실행에 대한 답변입니다. 화면을 새로고침하세요.")
    if run.status == "WAITING_FOR_INPUT":
        action, open_status = "resume", ("pending",)
    elif run.status == "COMPLETED" and run.kind == "analyze":
        action, open_status = "refresh", ("pending", "later")
        if not body.answers:
            raise HTTPException(400, "답변이 없습니다.")
    else:
        raise HTTPException(409, f"지금은 답변을 받을 수 없는 상태입니다 ({run.status}).")
    pending = {q.id: q for q in db.scalars(select(Question).where(Question.run_id == run.id,
                                                                  Question.status.in_(open_status)))}
    for a in body.answers:
        q = pending.get(a.question_id)
        if q is None:
            raise HTTPException(409, f"이미 답변했거나 없는 질문입니다: {a.question_id}")
        if a.choice in ("", "other") and not a.text.strip():
            raise HTTPException(400, f"답변이 비어 있습니다: {q.text[:30]}")
        db.add(Answer(question_id=q.id, run_id=run.id, user_id=user.id, choice=a.choice, text=a.text.strip()))
        q.status = "answered"
    # 답하지 않은 질문은 매뉴얼에 '확인 필요'로 남고, 완료 후에도 답할 수 있다.
    run.version += 1
    run.status = job.status = "QUEUED"
    _enqueue(db, job, run, action)
    db.add(Event(job_id=job.id, run_id=run.id, type="status", stage="answers",
                 message=f"{user.display_name}님이 답변 {len(body.answers)}개 제출 → "
                         + ("재개 대기" if action == "resume" else "매뉴얼 갱신 대기")))
    return {"ok": True, "run_version": run.version}


@app.get("/jobs/{job_id}/result")
def job_result(job_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    job = _owned(db, Job, job_id, user)
    rv = db.scalar(select(RuleVersion).where(RuleVersion.run_id == job.current_run_id))
    rats = db.scalars(select(Rationale).where(Rationale.run_id == job.current_run_id).order_by(Rationale.proposal_key))
    rationales = [r.data | {"proposal_key": r.proposal_key} for r in rats]
    for p in rationales:
        for c in p.get("claims", []):
            c["label"], c["rule"] = X.claim_label(c), X.claim_rule(c)
    view = None
    if rv:
        rules = rv.spec["calc_rules"]
        common = X.common_format([r["rule"] for r in rules])
        view = {"common": X.format_text(common) if common else None,
                "rules": [{"metric": f"{r['slot']['table_title']} · {r['slot']['metric_label']}",
                           "plain": X.rule_plain(r["rule"]) + ("" if X.display_format(r["rule"]) == common
                                                               else f" ({X.format_text(X.display_format(r['rule']))})"),
                           "excel": X.rule_excel(r["rule"], r["slot"]), "label": X.calc_label(r["validation_status"])}
                          for r in rules]}
    return {"rule_version": {"id": rv.id, "version": rv.version, "spec": rv.spec, "view": view} if rv else None,
            "rationales": rationales}


@app.get("/jobs/{job_id}/artifacts")
def job_artifacts(job_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    _owned(db, Job, job_id, user)
    rows = db.scalars(select(Artifact).where(Artifact.job_id == job_id).order_by(Artifact.created_at))
    return [{"artifact_id": a.id, "kind": a.kind, "filename": a.filename, "run_id": a.run_id,
             "created_at": a.created_at.isoformat()} for a in rows]


@app.get("/artifacts/{artifact_id}/download")
def download(artifact_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    a = _owned(db, Artifact, artifact_id, user)
    return FileResponse(resolve(a.path), filename=a.filename)


@app.get("/artifacts/{artifact_id}/preview", response_class=HTMLResponse)
def preview(artifact_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    a = _owned(db, Artifact, artifact_id, user)
    text = resolve(a.path).read_text(encoding="utf-8")
    if a.filename.endswith(".md"):
        return markdown.markdown(text, extensions=["tables", "fenced_code", "md_in_html"])
    return f"<pre>{text[:20000]}</pre>"


class RerunBody(BaseModel):
    source_file_id: str
    month: str


@app.post("/jobs/{job_id}/rerun")
def rerun(job_id: str, body: RerunBody, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    job = _owned(db, Job, job_id, user)
    src = _owned(db, FileRow, body.source_file_id, user)
    if src.kind != "source":
        raise HTTPException(400, "원천 CSV를 지정하세요.")
    rv = db.scalar(select(RuleVersion).where(RuleVersion.run_id == job.current_run_id))
    if rv is None:
        raise HTTPException(409, "확정된 규칙 버전이 아직 없습니다. 분석이 끝난 뒤 실행하세요.")
    run = Run(job_id=job.id, tenant_id=user.tenant_id, kind="rerun",
              params={"rule_version_id": rv.id, "dataset_id": src.id, "month": body.month},
              input_hashes={"source": src.sha256})
    db.add(run)
    db.flush()
    _enqueue(db, job, run, "rerun")
    db.add(Event(job_id=job.id, run_id=run.id, type="status", stage="queued",
                 message=f"다음 기간 보고서 생성 등록: {body.month} (규칙 v{rv.version})"))
    return {"run_id": run.id}


@app.post("/documents")
async def upload_document(title: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db),
                          user: CurrentUser = Depends(current_user)):
    if not file.filename.lower().endswith((".md", ".txt")):
        raise HTTPException(400, "업무 규정 문서는 .md 또는 .txt 파일이어야 합니다.")
    content = (await file.read()).decode("utf-8")
    return rag_index.register_document(db, tenant_id=user.tenant_id, title=title, content=content)


@app.get("/documents")
def list_documents(db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    rows = db.scalars(select(Document).where(Document.tenant_id == user.tenant_id).order_by(Document.created_at.desc()))
    return [{"document_id": d.id, "title": d.title, "kind": d.kind, "version": d.version,
             "created_at": d.created_at.isoformat()} for d in rows]


# ---------------- 화면 ----------------

def _page_user(request: Request, db: Session) -> CurrentUser | None:
    try:
        return current_user(request, db)
    except HTTPException:
        return None


@app.get("/", response_class=HTMLResponse)
def page_home(request: Request, db: Session = Depends(get_db)):
    user = _page_user(request, db)
    if user is None:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "dashboard.html", {"user": user})


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/jobs/{job_id}/view", response_class=HTMLResponse)
def page_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    user = _page_user(request, db)
    if user is None:
        return RedirectResponse("/login")
    _owned(db, Job, job_id, user)
    return templates.TemplateResponse(request, "job.html", {"user": user, "job_id": job_id})

