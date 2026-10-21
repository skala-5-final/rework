// Re:Work 화면 스크립트 (프레임워크 없이 fetch만 사용)

const STATUS_KO = {
  QUEUED: "대기 중", RUNNING: "실행 중", WAITING_FOR_INPUT: "담당자 확인 필요", RETRY_SCHEDULED: "재시도 예약",
  COMPLETED: "완료", FAILED: "실패", matched: "일치", mismatched: "불일치", untested: "미검증",
  evidence_confirmed: "근거 확인", confirmed: "확인됨", rejected: "반려", unconfirmed: "미확인", estimated: "추정", unknown: "모름",
  explained: "데이터로 설명됨", ambiguous: "판단 보류", unexplained: "설명 안 됨", user_explained: "담당자 설명으로 확정",
};
const STEP_OF = {
  load_inputs: "A", retrieve_context: "A", propose_calc_rules: "B", verify_calc_rules: "B",
  decompose_proposals: "C", investigate: "C", call_tools: "C", judge_rationale: "C",
  ask_user: "D", apply_answers: "D", save_results: "E", build_artifacts: "E",
  check_schema: "E", generate_report_draft: "E",
};

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const badge = (s) => `<span class="badge ${esc(s)}">${esc(STATUS_KO[s] || s)}</span>`;

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, { credentials: "same-origin", signal: AbortSignal.timeout(60000), ...opts });
  } catch (err) {
    throw new Error(err.name === "TimeoutError" ? "서버 응답이 60초 넘게 없습니다." : `서버에 연결할 수 없습니다 (${err.message}). API 서버가 켜져 있는지 확인하세요.`);
  }
  if (res.status === 401 && !path.startsWith("/auth/login")) { location.href = "/login"; throw new Error("로그인이 필요합니다."); }
  const type = res.headers.get("content-type") || "";
  const body = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && body.detail) || res.statusText);
  return body;
}
const postJSON = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

async function uploadFile(kind, file) {
  const fd = new FormData();
  fd.append("kind", kind);
  fd.append("file", file);
  return api("/files", { method: "POST", body: fd });
}

document.addEventListener("click", async (e) => {
  if (e.target.id === "logout") { await api("/auth/logout", { method: "POST" }); location.href = "/login"; }
});

// ---------- 로그인 ----------
function initLogin() {
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await postJSON("/auth/login", { username: f.get("username"), password: f.get("password") });
      location.href = "/";
    } catch (err) { $("#login-error").textContent = err.message; }
  });
}

// ---------- 파일 올리기 ----------
// 점선 영역 하나에 파일 칸(slot)들이 들어 있다. 영역에 끌어다 놓거나 클릭해 고른 파일은
// 확장자를 보고 맞는 칸의 <input type=file name=…>에 들어간다 (폼 전송은 그 입력으로).
function initUploads(root = document) {
  const fmtSize = (n) => n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))}KB` : `${(n / 1024 / 1024).toFixed(1)}MB`;
  root.querySelectorAll("[data-upload]").forEach((zone) => {
    const pick = zone.querySelector(".upload-pick"), msg = zone.querySelector(".upload-msg");
    const slots = [...zone.querySelectorAll("[data-slot]")].map((el) => {
      const input = el.querySelector("input[type=file]");
      return { el, input, exts: input.accept.split(",").map((x) => x.trim().toLowerCase()) };
    });
    const render = () => {
      slots.forEach(({ el, input }) => {
        const f = input.files[0];
        el.classList.toggle("filled", !!f);
        el.querySelector(".slot-file").textContent = f ? `${f.name} · ${fmtSize(f.size)}` : "선택 안 됨";
        el.querySelector(".slot-file").title = f ? f.name : "";
        el.querySelector(".slot-clear").setAttribute("aria-label", `${el.querySelector(".slot-label").textContent} 선택 취소`);
      });
      zone.classList.toggle("complete", slots.every((x) => x.input.files.length));
    };
    const accept = (files) => {
      const rejected = [];
      for (const f of files) {
        const slot = slots.find((x) => x.exts.some((e) => f.name.toLowerCase().endsWith(e)));
        if (!slot) { rejected.push(f.name); continue; }
        const dt = new DataTransfer();
        dt.items.add(f);
        slot.input.files = dt.files;
      }
      msg.textContent = rejected.length ? `올릴 수 없는 형식: ${rejected.join(", ")}` : "";
      render();
    };
    pick.addEventListener("change", () => { accept(pick.files); pick.value = ""; });
    ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      zone.classList.add("over");
    }));
    ["dragleave", "dragend"].forEach((ev) => zone.addEventListener(ev, (e) => {
      if (!zone.contains(e.relatedTarget)) zone.classList.remove("over");
    }));
    zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("over"); accept(e.dataTransfer.files); });
    slots.forEach(({ el, input }) => el.querySelector(".slot-clear").addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation(); input.value = ""; render();
    }));
    zone.closest("form")?.addEventListener("reset", () => setTimeout(() => { msg.textContent = ""; render(); }));
    render();
  });
}

function timeAgo(iso) {
  const sec = (Date.now() - new Date(iso)) / 1000;
  if (sec < 60) return "방금";
  if (sec < 3600) return `${Math.floor(sec / 60)}분 전`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}시간 전`;
  return new Date(iso).toLocaleDateString("ko-KR");
}

// ---------- 대시보드 ----------
async function loadJobs() {
  const jobs = await api("/jobs");
  $("#job-count").textContent = jobs.length ? `${jobs.length}건` : "";
  $("#job-table tbody").innerHTML = jobs.map((j) => `
    <tr data-href="/jobs/${j.job_id}/view"><td><a href="/jobs/${j.job_id}/view">${esc(j.name)}</a></td>
    <td>${badge(j.status)}</td>
    <td class="small muted">${esc(j.source_file)}<br>${esc(j.report_file)}</td>
    <td class="small muted" title="${new Date(j.updated_at).toLocaleString("ko-KR")}">${timeAgo(j.updated_at)}</td></tr>`).join("")
    || `<tr><td colspan="4" class="empty">아직 작업이 없습니다. 왼쪽 위에서 파일 두 개를 올려 시작하세요.</td></tr>`;
}

const DOC_KIND_KO = { guideline: "작성 지침", dictionary: "데이터 사전", confirmed: "확인된 판단 기준" };

async function loadDocs() {
  // 같은 제목은 최신 버전만 (목록은 최신순)
  const seen = new Set(), docs = (await api("/documents")).filter((d) => !seen.has(d.title) && seen.add(d.title));
  const item = (d) => `<li><span class="kind ${esc(d.kind)}">${esc(DOC_KIND_KO[d.kind] || d.kind)}</span>${esc(d.title)} <span class="muted small">v${d.version}</span></li>`;
  const base = docs.filter((d) => d.kind !== "confirmed"), confirmed = docs.filter((d) => d.kind === "confirmed");
  $("#doc-list").innerHTML = (base.length ? `<ul class="doc-list">${base.map(item).join("")}</ul>` : `<p class="empty">등록된 문서가 없습니다.</p>`)
    + (confirmed.length ? `<details class="options"><summary>확인된 판단 기준 ${confirmed.length}건</summary><ul class="doc-list">${confirmed.map(item).join("")}</ul></details>` : "");
}

function initDashboard() {
  initUploads();
  loadJobs(); loadDocs();
  setInterval(loadJobs, 4000);
  $("#job-table").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-href]");
    if (tr && !e.target.closest("a")) location.href = tr.dataset.href;
  });
  const say = (msg) => { $("#job-status-msg").textContent = msg; };
  $("#job-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target), btn = e.target.querySelector("button[type=submit]");
    const source = f.get("source"), report = f.get("report");
    $("#job-error").textContent = "";
    if (!source || !source.size) { $("#job-error").textContent = "원천 데이터 CSV를 선택하세요."; return; }
    if (!report || !report.size) { $("#job-error").textContent = "완성 보고서 PDF를 선택하세요."; return; }
    btn.disabled = true;
    try {
      say(`원천 데이터 업로드 중… (${source.name})`);
      const src = await uploadFile("source", source);
      say(`보고서 업로드 중… (${report.name})`);
      const rep = await uploadFile("report", report);
      say("작업 등록 중…");
      const job = await postJSON("/jobs", { name: f.get("name"), source_file_id: src.file_id, report_file_id: rep.file_id,
        inject_failure: f.get("inject_failure") === "on" });
      say("등록 완료. 작업 화면으로 이동합니다.");
      location.href = `/jobs/${job.job_id}/view`;
    } catch (err) { say(""); $("#job-error").textContent = `실패: ${err.message}`; btn.disabled = false; }
  });
  $("#doc-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target), btn = e.target.querySelector("button[type=submit]");
    const file = f.get("file");
    $("#doc-error").textContent = "";
    if (!file || !file.size) { $("#doc-error").textContent = "문서 파일을 선택하세요."; return; }
    if (!String(f.get("title")).trim()) f.set("title", file.name.replace(/\.(md|txt)$/i, ""));
    btn.disabled = true;
    try { await api("/documents", { method: "POST", body: f }); e.target.reset(); loadDocs(); }
    catch (err) { $("#doc-error").textContent = err.message; }
    btn.disabled = false;
  });
}

// ---------- 작업 상세 ----------
const job = { id: null, lastEvent: 0, status: null, run: null, resultLoaded: false, questionsShown: false, prevStatus: null };

function renderStepper(lastStage, status) {
  const order = ["A", "B", "C", "D", "E"];
  const cur = STEP_OF[lastStage];
  document.querySelectorAll("#stepper li").forEach((li) => {
    const i = order.indexOf(li.dataset.step), c = order.indexOf(cur);
    li.className = "";
    if (status === "COMPLETED") li.className = "done";
    else if (i < c) li.className = "done";
    else if (i === c) li.className = status === "WAITING_FOR_INPUT" ? "wait" : "active";
  });
}

function eventLine(e) {
  const d = new Date(e.ts), pad = (n) => String(n).padStart(2, "0");
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  let msg = esc(e.message);
  if (e.type === "tool_call") {
    const d = e.data || {};
    msg = `${d.ok ? "✓" : "✗"} ${esc(e.message)}(${esc(Object.keys(d.args || {}).filter((k) => k !== "dataset_id").map((k) => `${k}=${JSON.stringify(d.args[k])}`).join(", ").slice(0, 160))}) <span class="muted">${d.ms}ms</span>`;
    if (d.error) msg += `<div class="small" style="color:var(--bad)">${esc(d.error)}</div>`;
  }
  if (e.type === "llm" && e.data && e.data.input_tokens != null) {
    msg += ` <span class="muted">${(e.data.input_tokens + e.data.output_tokens).toLocaleString("ko-KR")} tok${e.data.cost_usd != null ? ` · $${e.data.cost_usd.toFixed(4)}` : ""}</span>`;
  }
  return `<li class="${e.type}"><span class="t">${time}</span><span class="k">${esc(e.type)} · ${esc(e.stage)}</span><span class="m">${msg}</span></li>`;
}

async function pollJob() {
  try {
    const j = await api(`/jobs/${job.id}`);
    job.status = j.status; job.run = j.run;
    $("#job-name").textContent = j.name;
    $("#job-meta").textContent = `${j.source_file} · ${j.report_file} · 시도 ${j.attempts}회${j.inject_failure ? " · 장애 시연" : ""}`;
    renderUsage(j.usage, j.trace);
    renderGuards(j.guards);
    $("#job-status").outerHTML = `<span class="badge ${j.status}" id="job-status">${STATUS_KO[j.status] || j.status}</span>`;
    $("#job-error").textContent = j.error && j.status === "FAILED" ? j.error : "";

    const events = await api(`/jobs/${job.id}/events?after=${job.lastEvent}`);
    if (events.length) {
      job.lastEvent = events[events.length - 1].id;
      $("#timeline").insertAdjacentHTML("beforeend", events.map(eventLine).join(""));
      applyToolFilter();
      const tl = $("#timeline"); tl.scrollTop = tl.scrollHeight;
      const stage = [...events].reverse().find((e) => e.type === "stage");
      if (stage) job.lastStage = stage.stage;
    }
    renderStepper(job.lastStage, j.status);
    renderArtifacts();

    // 실행 중 질문(최대 3개)은 대기 상태에서, 남은 질문은 완료 후 언제든 답할 수 있게 보여 준다.
    if (["WAITING_FOR_INPUT", "COMPLETED"].includes(j.status) && j.run && j.run.kind !== "rerun" && !job.questionsShown) await showQuestions(j.status);
    if (!["WAITING_FOR_INPUT", "COMPLETED"].includes(j.status)) { $("#question-card").classList.add("hidden"); job.questionsShown = false; }
    if (j.status === "COMPLETED" && job.prevStatus && job.prevStatus !== "COMPLETED") job.manualLoaded = false;
    job.prevStatus = j.status;
    if (["COMPLETED", "WAITING_FOR_INPUT"].includes(j.status) || events.some((e) => e.stage === "save_results")) await loadResults();
  } catch (err) { $("#job-error").textContent = err.message; }
}

// 실행별 관측: 모델 호출 수·토큰·비용과 trace_id (LangSmith 링크가 있으면 연결)
function renderUsage(u, t) {
  const el = $("#job-usage");
  if (!u || (!u.llm_calls && !(t && t.trace_id))) { el.classList.add("hidden"); return; }
  const n = (x) => Number(x || 0).toLocaleString("ko-KR");
  const cost = `$${Number(u.cost_usd || 0).toFixed(4)}${u.cost_complete ? "" : "+"}`;
  const trace = t && t.trace_id
    ? (t.url ? `<a href="${esc(t.url)}" target="_blank" rel="noopener">LangSmith에서 보기 ↗</a>` : "")
      + `<code title="trace_id: 이벤트·MCP 로그·LangSmith 공통">${esc(t.trace_id.slice(0, 8))}</code>`
    : "";
  el.innerHTML = `<span><b>${n(u.llm_calls)}</b> 모델 호출</span><span>입력 <b>${n(u.input_tokens)}</b> · 출력 <b>${n(u.output_tokens)}</b> 토큰</span>
    <span>예상 비용 <b>${cost}</b></span>${trace ? `<span class="usage-trace">${trace}</span>` : ""}`;
  el.classList.remove("hidden");
}

// 하네스 개입: 컨텍스트 예산·승인 정책·개인정보 마스킹. 예산으로 조사를 줄였으면 결과를 볼 때 알 수 있게 경고로 띄운다.
function renderGuards(g) {
  const el = $("#job-guards");
  if (!g) { el.classList.add("hidden"); return; }
  const chips = [
    g.masked && `개인정보 가림 ${g.masked}회`,
    g.approved && `승인 확인 ${g.approved}건`,
    g.blocked && `변경 차단 ${g.blocked}건`,
    g.clip && `긴 도구 결과 줄임 ${g.clip}회`,
    g.compact && `컨텍스트 정리 ${g.compact}회`,
  ].filter(Boolean);
  const stopped = g.stop + g.exceeded;
  if (!chips.length && !stopped) { el.classList.add("hidden"); return; }
  el.innerHTML = (stopped ? `<div class="guard-warn"><b>⚠ 컨텍스트 예산으로 조사 ${stopped}건을 줄였습니다.</b> 해당 결정은 지금까지의 결과로 판정했고, 계산 규칙 탭의 '미해결'에 남겼습니다.
      ${(g.notes || []).map((n) => `<div class="small">${esc(n)}</div>`).join("")}</div>` : "")
    + (chips.length ? `<div class="guard-chips">${chips.map((c) => `<span>${esc(c)}</span>`).join("")}</div>` : "");
  el.classList.remove("hidden");
}

function applyToolFilter() {
  const show = $("#show-tools").checked;
  document.querySelectorAll("#timeline li.tool_call").forEach((li) => li.classList.toggle("hidden", !show));
}

async function showQuestions(status) {
  const waiting = status === "WAITING_FOR_INPUT";
  const qs = (await api(`/jobs/${job.id}/questions`)).filter((q) => q.status === "pending" || (!waiting && q.status === "later"));
  job.questionsShown = true;
  const card = $("#question-card");
  // 답을 기다리는 동안은 맨 위, 완료 후 남은 질문은 결과물 아래에 둔다 (결과물을 먼저 본다).
  if (waiting) $("#stepper").closest(".card").after(card);
  else $("main").append(card);
  card.classList.toggle("later", !waiting);
  card.classList.toggle("hidden", !qs.length);
  if (!qs.length) return;
  $("#q-title").textContent = waiting ? `확인이 필요한 결정 ${qs.length}개` : `나중에 답할 질문 ${qs.length}개`;
  $("#q-help").textContent = waiting
    ? "데이터로 설명되지 않은 결정만 묻습니다. 답하지 않아도 매뉴얼은 만들어지고, 나중에 답할 수 있습니다."
    : "알게 되었을 때 누구든 답하면 매뉴얼의 라벨이 갱신됩니다.";
  $("#answer-later").classList.toggle("hidden", !waiting);
  $("#questions").innerHTML = qs.map((q) => {
    const ctx = q.context || {};
    const opts = (q.options || []).map((o) => `<label><input type="radio" name="c_${q.question_id}" value="${esc(o.value)}"> ${esc(o.label)}</label>`).join("");
    const evidence = (ctx.evidence || []).map((x) => `<li>${esc(x)}</li>`).join("");
    return `<div class="q" data-qid="${q.question_id}">
      <div><span class="muted small">${esc(ctx.proposal_title || "")}</span><br><b>${esc(q.text)}</b></div>
      ${opts ? `<div class="opts">${opts}</div>` : ""}
      <textarea rows="1" name="t_${q.question_id}" placeholder="보충 설명 (선택, '기타'를 고르면 적어 주세요)"></textarea>
      <details><summary>근거</summary><div>${esc(q.reason)}</div>${evidence ? `<ul>${evidence}</ul>` : ""}</details>
    </div>`;
  }).join("");
}

async function sendAnswers(answers, btn) {
  btn.disabled = true;
  try {
    await postJSON(`/jobs/${job.id}/answers`, { run_id: job.run.run_id, run_version: job.run.version, answers });
    $("#question-card").classList.add("hidden");
    job.questionsShown = false;
  } catch (err) { $("#job-error").textContent = err.message; }
  btn.disabled = false;
}

async function submitAnswers(e) {
  e.preventDefault();
  const answers = [];
  document.querySelectorAll("#questions .q").forEach((div) => {
    const qid = div.dataset.qid;
    const choice = (div.querySelector(`input[name="c_${qid}"]:checked`) || {}).value || "";
    const text = div.querySelector("textarea").value.trim();
    if (choice || text) answers.push({ question_id: qid, choice, text });
  });
  if (!answers.length) { $("#job-error").textContent = "답변을 하나 이상 고르세요. 지금 답하기 어려우면 '나중에 답하기'를 누르세요."; return; }
  await sendAnswers(answers, e.target.querySelector("button[type=submit]"));
}

async function loadResults() {
  const res = await api(`/jobs/${job.id}/result`);
  if (res.rule_version) {
    const v = res.rule_version.view;
    const rows = v.rules.map((r) => `<tr><td>${esc(r.metric)}</td><td>${esc(r.plain)}</td>
      <td><code>${esc(r.excel)}</code></td><td>${badge(r.label)}</td></tr>`).join("");
    const unresolved = (res.rule_version.spec.unresolved || []).map((u) => `<li>${esc(u)}</li>`).join("");
    $("#rules").innerHTML = `<p class="muted small">규칙 버전 v${res.rule_version.version} · 보고서 숫자를 그대로 재현한 규칙은 확인됨으로 봅니다.</p>
      ${v.common ? `<p><b>공통:</b> ${esc(v.common)}. 다른 경우만 계산 칸에 따로 적었습니다.</p>` : ""}
      <table class="table"><thead><tr><th>지표</th><th>계산</th><th>엑셀 수식</th><th>상태</th></tr></thead><tbody>${rows}</tbody></table>
      ${unresolved ? `<h3>미해결</h3><ul>${unresolved}</ul>` : ""}`;
  }
  if (res.rationales.length) {
    $("#rationales").innerHTML = `<p class="muted small">✅ 근거 확인(보고서 이유 검증·담당자 미확인) · ✅ 확인됨(담당자 확인) · 🟡 추정(데이터 근거 있음) · ❓ 모름 · ⛔ 반려</p>` + res.rationales.map((p) => `<div class="proposal">
      <b>${esc(p.title)}</b>
      ${p.claims.map((c) => {
        const detail = [`보고서 표현: “${c.stated_reason || ""}”`, ...(c.evidence || []).map((x) => `데이터: ${x}`),
          ...(c.confirmation && c.confirmation.answer_text ? [`담당자 답변: “${c.confirmation.answer_text}”`] : []),
          ...((c.rejected_hypotheses || []).length ? [`아니었던 이유: ${c.rejected_hypotheses.join("; ")}`] : []),
          ...((c.check_notes || []).length ? [`검사 메모: ${c.check_notes.join("; ")}`] : [])];
        return `<div class="claim ${esc(c.label)}">
          <div>${badge(c.label)} <b>${esc(c.statement)}</b>${c.rule ? ` — ${esc(c.rule)}` : ""}</div>
          ${(c.unresolved_details || []).length ? `<p class="small">❓ 추가 확인 사항: ${esc(c.unresolved_details.join("; "))}</p>` : ""}
          ${c.detail_confirmation?.answer_text ? `<p class="small">세부사항 담당자 답변: ${esc(c.detail_confirmation.answer_text)}</p>` : ""}
          <details><summary>근거</summary><ul>${detail.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>
        </div>`;
      }).join("")}</div>`).join("");
  }
  const arts = await api(`/jobs/${job.id}/artifacts`);
  job.arts = arts;
  renderArtifacts();
  const manual = arts.find((a) => a.kind === "manual");
  if (manual && !job.manualLoaded) { $("#manual").innerHTML = await api(`/artifacts/${manual.artifact_id}/preview`); job.manualLoaded = true; }
}

const ART_INFO = {
  manual: { title: "인수인계 매뉴얼", desc: "후임자가 읽는 업무 설명서" },
  rules: { title: "업무 규칙", desc: "계산 규칙과 판단 기준 (다음 기간 재실행용)" },
  validation: { title: "검증 결과", desc: "보고서 수치와 다시 계산한 값 비교" },
  draft_md: { title: "보고서 초안", desc: "확인된 규칙을 새 기간 데이터에 적용" },
  draft_csv: { title: "보고서 초안 표", desc: "초안의 수치 데이터" },
};
const STEP_KO = { A: "입력 준비", B: "계산 규칙", C: "판단 근거", D: "담당자 확인", E: "결과물" };

// 결과물이 나오기 전에도 무엇이 만들어질지와 지금 어느 단계인지 보여 준다.
function renderArtifacts() {
  const arts = job.arts || [], s = job.status;
  const busy = ["QUEUED", "RUNNING", "RETRY_SCHEDULED"].includes(s);
  const step = STEP_OF[job.lastStage];
  const expected = ["manual", "rules", "validation", ...(busy && job.run && job.run.kind === "rerun" ? ["draft_md", "draft_csv"] : [])];
  const have = new Set(arts.map((a) => a.kind));

  let state = "";
  if (busy) state = `<span class="spinner" aria-hidden="true"></span>${arts.length ? "결과물을 갱신하고 있습니다" : step === "E" ? "결과물을 작성하고 있습니다" : `분석 중 · ${STEP_KO[step] || "준비"} 단계`}`;
  else if (s === "WAITING_FOR_INPUT") state = "담당자 확인을 마치면 작성됩니다";
  else if (s === "FAILED") state = "실행이 실패해 만들지 못했습니다";
  else if (arts.length) state = `${arts.length}개 완료`;
  $("#art-state").innerHTML = state;
  $("#art-state").className = `art-state${busy ? " busy" : ""}`;
  $("#files-dot").classList.toggle("hidden", !busy);

  const card = (a) => {
    const info = ART_INFO[a.kind] || { title: a.filename, desc: "" };
    const ext = a.filename.split(".").pop().toUpperCase();
    const dl = `<a href="/artifacts/${a.artifact_id}/download" download>다운로드</a>`;
    return `<li class="art">
      <div class="art-top"><span class="art-ico ${esc(ext)}">${esc(ext)}</span>
        <div><div class="art-title">${esc(info.title)}${busy ? ` <span class="badge RUNNING">갱신 중</span>` : ""}</div>
        <div class="art-file">${esc(a.filename)}</div></div></div>
      <p class="art-desc">${esc(info.desc)}</p>
      <div class="art-actions">${ext === "MD" ? `<button type="button" class="main" data-preview="${a.artifact_id}">미리보기</button>${dl}` : dl.replace("<a ", '<a class="main" ')}</div>
    </li>`;
  };
  const pending = (kind) => {
    const info = ART_INFO[kind];
    return `<li class="art pending${busy ? " busy" : ""}">
      <div class="art-top"><span class="art-ico">${busy ? '<span class="spinner" aria-hidden="true"></span>' : "…"}</span>
        <div><div class="art-title">${esc(info.title)}</div><div class="art-file">${busy ? "작성 중…" : "아직 없음"}</div></div></div>
      <p class="art-desc">${esc(info.desc)}</p>
      <div class="art-actions"><span class="skel"></span></div>
    </li>`;
  };
  $("#artifacts").innerHTML = arts.map(card).join("") + expected.filter((k) => !have.has(k)).map(pending).join("");
}

function initJob(id) {
  job.id = id;
  initUploads();
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${b.dataset.tab}`));
  }));
  $("#show-tools").addEventListener("change", applyToolFilter);
  $("#answer-form").addEventListener("submit", submitAnswers);
  $("#answer-later").addEventListener("click", (e) => sendAnswers([], e.target));
  document.addEventListener("click", async (e) => {
    const id = e.target.dataset && e.target.dataset.preview;
    if (!id) return;
    $("#manual").innerHTML = await api(`/artifacts/${id}/preview`);
    $('#tabs button[data-tab="manual"]').click();
  });
  $("#rerun-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target), btn = e.target.querySelector("button[type=submit]");
    if (!f.get("source") || !f.get("source").size) { $("#rerun-error").textContent = "새 기간 원천 CSV를 선택하세요."; return; }
    btn.disabled = true; $("#rerun-error").textContent = "";
    try {
      const src = await uploadFile("source", f.get("source"));
      await postJSON(`/jobs/${job.id}/rerun`, { source_file_id: src.file_id, month: f.get("month") });
      $('#tabs button[data-tab="progress"]').click();
    } catch (err) { $("#rerun-error").textContent = err.message; }
    btn.disabled = false;
  });
  pollJob();
  setInterval(pollJob, 2000);
}
