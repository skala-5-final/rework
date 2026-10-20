// Re:Work 화면 스크립트 (프레임워크 없이 fetch만 사용)

const STATUS_KO = {
  QUEUED: "대기 중", RUNNING: "실행 중", WAITING_FOR_INPUT: "담당자 확인 필요", RETRY_SCHEDULED: "재시도 예약",
  COMPLETED: "완료", FAILED: "실패", matched: "일치", mismatched: "불일치", untested: "미검증",
  confirmed: "확인됨", rejected: "반려", unconfirmed: "미확인",
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

// ---------- 대시보드 ----------
async function loadJobs() {
  const jobs = await api("/jobs");
  $("#job-table tbody").innerHTML = jobs.map((j) => `
    <tr><td><a href="/jobs/${j.job_id}/view">${esc(j.name)}</a><div class="muted small">${j.job_id}</div></td>
    <td>${badge(j.status)}</td>
    <td class="small">${esc(j.source_file)}<br>${esc(j.report_file)}</td>
    <td class="small muted">${new Date(j.updated_at).toLocaleString("ko-KR")}</td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">아직 작업이 없습니다.</td></tr>`;
}

async function loadDocs() {
  const docs = await api("/documents");
  $("#doc-list").innerHTML = docs.map((d) => `<li>${esc(d.title)} <span class="muted small">v${d.version} · ${esc(d.kind)}</span></li>`).join("")
    || `<li class="muted">등록된 문서가 없습니다.</li>`;
}

function initDashboard() {
  loadJobs(); loadDocs();
  setInterval(loadJobs, 4000);
  const say = (msg) => { $("#job-status-msg").textContent = msg; };
  $("#job-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target), btn = e.target.querySelector("button");
    const source = f.get("source"), report = f.get("report");
    $("#job-error").textContent = "";
    if (!source || !source.size) { $("#job-error").textContent = "원천 데이터 CSV를 선택하세요."; return; }
    if (!report || !report.size) { $("#job-error").textContent = "완성 보고서 PDF를 선택하세요."; return; }
    btn.disabled = true;
    try {
      say(`원천 데이터 업로드 중… (${source.name}, ${Math.round(source.size / 1024)}KB)`);
      const src = await uploadFile("source", source);
      say(`보고서 업로드 중… (${report.name}, ${Math.round(report.size / 1024)}KB)`);
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
    const f = new FormData(e.target), btn = e.target.querySelector("button");
    btn.disabled = true; $("#doc-error").textContent = "";
    try { await api("/documents", { method: "POST", body: f }); e.target.reset(); loadDocs(); }
    catch (err) { $("#doc-error").textContent = err.message; }
    btn.disabled = false;
  });
}

// ---------- 작업 상세 ----------
const job = { id: null, lastEvent: 0, status: null, run: null, resultLoaded: false, questionsShown: false };

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
  return `<li class="${e.type}"><span class="t">${time}</span><span class="k">${esc(e.type)} · ${esc(e.stage)}</span><span class="m">${msg}</span></li>`;
}

async function pollJob() {
  try {
    const j = await api(`/jobs/${job.id}`);
    job.status = j.status; job.run = j.run;
    $("#job-name").textContent = j.name;
    $("#job-meta").textContent = `${j.source_file} · ${j.report_file} · 시도 ${j.attempts}회${j.inject_failure ? " · 장애 시연" : ""}`;
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

    if (j.status === "WAITING_FOR_INPUT" && !job.questionsShown) await showQuestions();
    if (j.status !== "WAITING_FOR_INPUT") { $("#question-card").classList.add("hidden"); job.questionsShown = false; }
    if (["COMPLETED", "WAITING_FOR_INPUT"].includes(j.status) || events.some((e) => e.stage === "save_results")) await loadResults();
  } catch (err) { $("#job-error").textContent = err.message; }
}

function applyToolFilter() {
  const show = $("#show-tools").checked;
  document.querySelectorAll("#timeline li.tool_call").forEach((li) => li.classList.toggle("hidden", !show));
}

async function showQuestions() {
  const qs = (await api(`/jobs/${job.id}/questions`)).filter((q) => q.status === "pending");
  if (!qs.length) return;
  job.questionsShown = true;
  $("#question-card").classList.remove("hidden");
  $("#questions").innerHTML = qs.map((q, i) => {
    const ctx = q.context || {};
    const evidence = (ctx.evidence || []).map((x) => `<li>${esc(x)}</li>`).join("");
    const rules = (ctx.rules || []).map((r) => `<li>${esc(r.metric)}: <code>${esc(r.rule.op)}(${esc(r.rule.column)})</code> ÷${r.rule.scale} · 소수 ${r.rule.decimals}자리</li>`).join("");
    const opts = (q.options || []).map((o) => `<label><input type="radio" name="c_${q.question_id}" value="${esc(o.value)}"> ${esc(o.label)}</label>`).join("");
    const criteria = (ctx.criteria || []).map((x) => `<li>${esc(x)}</li>`).join("");
    return `<div class="q" data-qid="${q.question_id}">
      <div><b>Q${i + 1}.</b> ${esc(q.text)}</div>
      ${criteria ? `<ul class="crit">${criteria}</ul>` : ""}
      <div class="why">왜 묻나요? ${esc(q.reason)}</div>
      ${evidence || rules ? `<details><summary>근거 보기</summary><ul>${evidence}${rules}</ul>${(ctx.rejected || []).length ? `<div>기각한 가설: ${esc(ctx.rejected.join("; "))}</div>` : ""}</details>` : ""}
      ${opts ? `<div class="opts">${opts}</div>` : ""}
      <textarea name="t_${q.question_id}" placeholder="${q.kind === "free" ? "어떤 기준으로 결정하셨는지 적어 주세요" : "보충 설명 (선택)"}"></textarea>
    </div>`;
  }).join("");
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
  if (!answers.length) { $("#job-error").textContent = "답변을 하나 이상 입력하세요."; return; }
  const btn = e.target.querySelector("button"); btn.disabled = true;
  try {
    await postJSON(`/jobs/${job.id}/answers`, { run_id: job.run.run_id, run_version: job.run.version, answers });
    $("#question-card").classList.add("hidden");
  } catch (err) { $("#job-error").textContent = err.message; }
  btn.disabled = false;
}

function ruleText(r) {
  const op = { sum: "합계", count_distinct: "고유값 수", daily_mean: "합계 ÷ 영업일수", ratio: `÷ ${r.denominator_column}` }[r.op];
  return `<code>${esc(r.column)}</code> ${op}${r.as_percent ? " ×100(%)" : ""}${r.scale === 1000 ? " · 천원" : ""} · 소수 ${r.decimals}자리`;
}

async function loadResults() {
  const res = await api(`/jobs/${job.id}/result`);
  if (res.rule_version) {
    const rows = res.rule_version.spec.calc_rules.map((r) => `<tr>
      <td>${esc(r.slot.table_title)}<div class="small muted">${esc(r.slot.metric_label)}</div></td>
      <td>${ruleText(r.rule)}<div class="small muted">${esc(r.rule.reasoning || "")}</div></td>
      <td>${badge(r.validation_status)}</td><td>${badge(r.confirmation.status)}</td></tr>`).join("");
    const unresolved = (res.rule_version.spec.unresolved || []).map((u) => `<li>${esc(u)}</li>`).join("");
    $("#rules").innerHTML = `<p class="muted small">규칙 버전 v${res.rule_version.version} · 수치 검증(일치)과 담당자 확인은 별개입니다.</p>
      <table class="table"><thead><tr><th>지표</th><th>계산 방법</th><th>수치 검증</th><th>담당자 확인</th></tr></thead><tbody>${rows}</tbody></table>
      ${unresolved ? `<h3>미해결</h3><ul>${unresolved}</ul>` : ""}`;
  }
  if (res.rationales.length) {
    $("#rationales").innerHTML = res.rationales.map((p) => `<div class="proposal">
      <b>${esc(p.proposal_key)}. ${esc(p.title)}</b><blockquote>${esc(p.proposal_text)}</blockquote>
      ${p.claims.map((c) => `<div class="claim ${c.status}">
        <div><b>${esc(c.statement)}</b> ${badge(c.status)} ${badge(c.confirmation.status)}</div>
        <div class="small muted">보고서에 적힌 근거: “${esc(c.stated_reason)}”</div>
        ${c.criterion ? `<div class="crit">판단 기준: ${esc(c.criterion.description)}<br><code>${esc(c.criterion.type)} · ${esc(c.criterion.condition.field)} ${esc(c.criterion.condition.operator)} ${c.criterion.condition.threshold} · 최근 ${c.criterion.window_months}개월</code></div>` : ""}
        ${c.criterion_user ? `<div class="crit">담당자 설명: ${esc(c.criterion_user)}</div>` : ""}
        <ul>${(c.evidence || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        ${(c.rejected_hypotheses || []).length ? `<div class="small muted">기각한 가설: ${esc(c.rejected_hypotheses.join("; "))}</div>` : ""}
        ${(c.check_notes || []).length ? `<div class="small" style="color:var(--warn)">검사 메모: ${esc(c.check_notes.join("; "))}</div>` : ""}
      </div>`).join("")}</div>`).join("");
  }
  const arts = await api(`/jobs/${job.id}/artifacts`);
  $("#artifacts").innerHTML = arts.map((a) => `<li><a href="/artifacts/${a.artifact_id}/download">${esc(a.filename)}</a> <span class="muted small">${esc(a.kind)}</span>
    ${a.filename.endsWith(".md") ? ` · <button class="link" data-preview="${a.artifact_id}">미리보기</button>` : ""}</li>`).join("")
    || `<li class="muted">아직 없습니다.</li>`;
  const manual = arts.find((a) => a.kind === "manual");
  if (manual && !job.manualLoaded) { $("#manual").innerHTML = await api(`/artifacts/${manual.artifact_id}/preview`); job.manualLoaded = true; }
}

function initJob(id) {
  job.id = id;
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${b.dataset.tab}`));
  }));
  $("#show-tools").addEventListener("change", applyToolFilter);
  $("#answer-form").addEventListener("submit", submitAnswers);
  document.addEventListener("click", async (e) => {
    const id = e.target.dataset && e.target.dataset.preview;
    if (!id) return;
    $("#manual").innerHTML = await api(`/artifacts/${id}/preview`);
    $('#tabs button[data-tab="manual"]').click();
  });
  $("#rerun-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target), btn = e.target.querySelector("button");
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
