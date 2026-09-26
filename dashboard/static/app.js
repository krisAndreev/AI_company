/* Mission Control - AI Company dashboard (vanilla JS, no build step). */
"use strict";

// ================================================================ helpers
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const eur = (v) => "€" + Number(v || 0).toFixed(2);
const clampPct = (v) => Math.max(0, Math.min(100, Number(v) || 0));
const trunc = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
const badge = (s, cls) => `<span class="badge ${esc(cls || s)}">${esc(s)}</span>`;
const bar = (pct, cls = "") => `<div class="bar ${cls}"><i data-pct="${clampPct(pct)}"></i></div>`;
const deptBadge = (d) => `<span class="badge dept">${esc(String(d).replace("_", " "))}</span>`;
function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso), s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
const clock = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
function dyn(root) {
  $$("[data-pct]", root).forEach((e) => { e.style.width = e.dataset.pct + "%"; });
  $$("[data-bg]", root).forEach((e) => { e.style.background = e.dataset.bg; });
  // Label every cell with its column header so tables can become cards on phones.
  $$("table", root).forEach((t) => {
    const heads = $$("tr:first-child th", t).map((th) => th.textContent.trim());
    if (!heads.length) return;
    t.classList.add("stack");
    $$("tr", t).forEach((tr) => [...tr.children].forEach((cell, i) => { if (cell.tagName === "TD") cell.dataset.label = heads[i] || ""; }));
  });
}
// Apply dyn() to whatever a view renders, without every view having to remember it.
new MutationObserver(() => {
  cancelAnimationFrame(dyn.raf);
  dyn.raf = requestAnimationFrame(() => dyn($("#view")));
}).observe(document.getElementById("view"), { childList: true, subtree: true });
function conf(v) { return `<span class="conf">${bar(v * 100, v >= 0.7 ? "green" : "")}<span class="mono">${Number(v).toFixed(2)}</span></span>`; }
function details(obj, max = 220) {
  const parts = Object.entries(obj || {}).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
  return esc(trunc(parts.join(" · "), max));
}

// ================================================================ api
async function api(path, { method = "GET", body } = {}) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") opt.headers["X-Requested-With"] = "dashboard";
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch { /* empty body */ }
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Please log in"); }
  if (!r.ok) {
    const d = data.detail;
    throw new Error(Array.isArray(d) ? d.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ") : (d || `HTTP ${r.status}`));
  }
  return data;
}

// ================================================================ ui primitives
function toast(msg, kind = "info", ms = 4500) {
  const t = document.createElement("div");
  t.className = `toast ${kind}`; t.innerHTML = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), ms);
}
function modal({ title, body, wide = false, actions = [] }) {
  const root = document.createElement("div");
  root.className = "modal-backdrop";
  root.innerHTML = `<div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true">
    <div class="modal-head"><h3>${title}</h3><button class="close-x" aria-label="Close">×</button></div>
    <div class="modal-body">${body}</div>
    ${actions.length ? `<div class="modal-foot">${actions.map((a, i) => `<button class="btn ${a.cls || ""}" data-i="${i}">${a.label}</button>`).join("")}</div>` : ""}
  </div>`;
  const close = () => { root.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  root.addEventListener("mousedown", (e) => { if (e.target === root) close(); });
  $(".close-x", root).addEventListener("click", close);
  actions.forEach((a, i) => $(`[data-i="${i}"]`, root).addEventListener("click", async (e) => {
    const btn = e.currentTarget; btn.disabled = true;
    try { if ((await a.onClick(root)) !== false) close(); }
    catch (err) { toast(esc(err.message), "error"); }
    finally { btn.disabled = false; }
  }));
  $("#modal-root").appendChild(root);
  dyn(root);
  const first = $("input, textarea, select", root); if (first) first.focus();
  return { root, close };
}
function confirmBox(text, { label = "Confirm", cls = "danger" } = {}) {
  return new Promise((resolve) => {
    const m = modal({ title: "Please confirm", body: `<p>${text}</p>`, actions: [
      { label: "Cancel", cls: "ghost", onClick: () => resolve(false) },
      { label, cls, onClick: () => resolve(true) },
    ] });
    $(".close-x", m.root).addEventListener("click", () => resolve(false));
  });
}
function formValues(root) {
  const out = {};
  $$("[name]", root).forEach((el) => {
    if (el.type === "checkbox") { if (el.dataset.group) { (out[el.dataset.group] = out[el.dataset.group] || []); if (el.checked) out[el.dataset.group].push(el.value); } else out[el.name] = el.checked; }
    else if (el.type === "number") out[el.name] = el.value === "" ? null : Number(el.value);
    else out[el.name] = el.value.trim();
  });
  return out;
}
function emergency(title, text) {
  const e = $("#emergency");
  $(".emergency-title", e).textContent = title;
  $(".emergency-text", e).textContent = text;
  e.classList.remove("hidden");
  e.style.animation = "none"; void e.offsetWidth; e.style.animation = "";
  clearTimeout(emergency.timer);
  emergency.timer = setTimeout(() => e.classList.add("hidden"), 4300);
}

// ================================================================ app state
const App = {
  view: "fleet", state: null, fleet: null, lastEventId: null, timer: null,
  filters: { logActor: "", logProject: "", logSearch: "" },
  configFile: "company.json",
};
const TITLES = { fleet: "Fleet", command: "Orchestrator", studio: "Studio & Files", resources: "Resources", overview: "Overview",
  settings: "Settings", log: "Activity log" };
const projectName = (id) => ((App.state && App.state.projects) || []).find((p) => p.id === id)?.name || id || "";

// ================================================================ auth
function showLogin() {
  $("#app").classList.add("hidden"); $("#login").classList.remove("hidden");
  clearInterval(App.timer); App.timer = null;
  $("#password").focus();
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { password: $("#password").value } });
    $("#password").value = "";
    boot();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }).catch(() => {}); showLogin(); });

// ================================================================ polling
async function poll() {
  try {
    const s = await api("/api/state");
    App.state = s;
    renderTopbar(s);
    if (App.fleet) App.fleet.update(s);
    await pollEvents(s.last_event_id);
    if (VIEWS[App.view].live && !$(".modal-backdrop")) VIEWS[App.view].render(true);
  } catch (err) { /* login handled in api(); transient errors ignored */ }
}
async function pollEvents(lastId) {
  if (App.lastEventId === null) { App.lastEventId = lastId; return; }
  if (lastId <= App.lastEventId) return;
  const events = (await api(`/api/events?after=${App.lastEventId}&limit=100`)).reverse();
  App.lastEventId = lastId;
  for (const ev of events) {
    const a = ev.action, d = ev.details || {};
    if (a.startsWith("Decision:") && ev.actor === "MASTER_ORCHESTRATOR")
      emergency("EMERGENCY MEETING", `${projectName(ev.project_id)}: ${a.replace("Decision: ", "").toUpperCase()} — ${trunc(d.reason, 140)}`);
    else if (a.startsWith("Checkpoint decision"))
      emergency("EMERGENCY MEETING", `Experiment checkpoint: ${a.split(": ")[1].toUpperCase()} — ${trunc(d.reason, 140)}`);
    else if (a === "Experiment SUCCEEDED") emergency("VICTORY", `${projectName(ev.project_id)} — ${d.reason || "success criteria met"}`);
    else if (a === "Experiment FAILED" || a === "Experiment STOPPED") emergency("DEFEAT", `${projectName(ev.project_id)} — ${d.reason || ""}`);
    else if (a === "Pivoted") emergency("PIVOT!", trunc(d.new_direction, 160));
    else if (a.startsWith("Verdict: approval") || a.startsWith("Verdict: human_step"))
      toast(`⚠ <b>Needs you:</b> ${esc(trunc(d.reason, 120))}`, "warn", 7000);
    else if (a === "Task failed" || a === "Loop error") toast(`✖ ${esc(a)}: ${esc(trunc(d.error, 120))}`, "error", 7000);
    else if (a === "Told owner") toast(`💬 <b>${esc(projectName(ev.project_id) || "Orchestrator")}:</b> ${esc(trunc(String(d.text || "").replace(/\*\*/g, ""), 160))}`, "info", 9000);
  }
}
function renderTopbar(s) {
  const r = s.runner || {};
  const pill = $("#pill-runner");
  pill.className = `pill ${r.state === "running" ? "running" : r.state === "paused" ? "paused" : ""}`;
  pill.innerHTML = `<span class="dot"></span>Loop ${esc(r.state || "stopped")}${s.autopilot ? " · autopilot" : ""}${r.activity && r.activity !== "idle" ? " · " + esc(r.activity) : ""}${r.keep_awake ? " · PC kept awake" : ""}`;
  $("#run-start").disabled = r.state === "running";
  $("#run-pause").disabled = r.state !== "running";
  const w = $("#pill-waiting");
  w.classList.toggle("hidden", !s.waiting);
  w.textContent = `⚠ ${s.waiting} need${s.waiting === 1 ? "s" : ""} you`;
  for (const id of ["#nav-waiting", "#bn-waiting"]) {
    const nb = $(id);
    nb.classList.toggle("hidden", !s.waiting); nb.textContent = s.waiting;
  }
  const j = $("#pill-jobs");
  j.classList.toggle("hidden", !(s.jobs || []).length);
  j.textContent = `⏳ ${(s.jobs || []).map((x) => x.label).join(", ")}`;
}
$("#pill-waiting").addEventListener("click", () => openTasks(null, "WAITING"));
for (const cmd of ["start", "pause", "step"]) {
  $(`#run-${cmd}`).addEventListener("click", async () => {
    try { await api(`/api/runner/${cmd}`, { method: "POST" }); toast(`Work loop: ${cmd}`, "success", 2500); poll(); }
    catch (err) { toast(esc(err.message), "error"); }
  });
}

// ================================================================ jobs (slow AI actions)
async function runJob(path, body) {
  const job = await api(path, { method: "POST", body });
  toast(`⏳ Started: <b>${esc(job.label)}</b> (AI is thinking…)`, "info", 3500);
  trackJob(job, (j) => { toast(`✔ <b>${esc(j.label)}</b> finished`, "success"); showJobResult(j); refresh(); });
}
/** Poll a background job; onDone(job) on success, onFail(job) (default: error toast) on failure. */
function trackJob(job, onDone, onFail) {
  const tick = async () => {
    let j;
    try { j = await api(`/api/jobs/${job.id}`); } catch { return setTimeout(tick, 3000); }
    if (j.status === "done") onDone(j);
    else if (j.status === "failed") (onFail || ((x) => toast(`✖ <b>${esc(x.label)}</b> failed: ${esc(x.error)}`, "error", 9000)))(j);
    else setTimeout(tick, 1500);
  };
  setTimeout(tick, 1200);
}
function drawAvatar(canvas, kind) {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth || 40, h = canvas.clientHeight || 48;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const s = h / 60;
  window.drawCrew(ctx, w / 2, h - 3, "#c51111",
    { facing: 1, hat: "captain", scale: s, phase: Date.now() / 300, working: kind === "think" });
}
function showJobResult(j) {
  const r = j.result || {};
  let body = "";
  if (j.kind === "plan") {
    body = `<p class="muted">Model: ${esc(r.model)}</p><h4>Accepted (${r.accepted.length})</h4>
      <ul>${r.accepted.map((x) => `<li>${esc(x)}</li>`).join("") || "<li class='muted'>none</li>"}</ul>
      <h4>Rejected by code (${r.rejected.length})</h4>
      <ul>${r.rejected.map((x) => `<li>${esc(x.task)} <span class="muted">— ${esc(x.reason)}</span></li>`).join("") || "<li class='muted'>none</li>"}</ul>`;
  } else if (j.kind === "review") {
    body = `<dl class="kv"><dt>Decision</dt><dd>${badge(r.decision, r.decision === "continue" ? "ok" : "WAITING")} ${r.applied ? "" : badge("deferred (low confidence)", "PAUSED")}</dd>
      <dt>Confidence</dt><dd>${conf(r.confidence)}</dd><dt>Source</dt><dd>${esc(r.source)} ${esc(r.model_key || "")}</dd>
      <dt>Reason</dt><dd>${esc(r.reason)}</dd></dl>`;
  } else if (j.kind === "evaluate") {
    body = `<dl class="kv"><dt>Action</dt><dd>${badge(r.action, r.action)}</dd><dt>Source</dt><dd>${esc(r.source)}</dd><dt>Reason</dt><dd>${esc(r.reason)}</dd></dl>`;
  } else if (j.kind === "learn") {
    body = `<h4>New lessons</h4><ul>${(r.lessons || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li class='muted'>none</li>"}</ul>
      <h4>Confirmed lessons</h4><ul>${(r.confirmed || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li class='muted'>none</li>"}</ul>
      <p>${(r.improvements || []).length} improvement proposal(s) waiting for you in <b>Guidelines &amp; Memory → Proposals</b>.</p>`;
  }
  modal({ title: esc(j.label), body, actions: [{ label: "OK", cls: "primary", onClick: () => {} }] });
}

// ================================================================ navigation
function go(view) {
  if (!VIEWS[view]) view = "fleet";
  if (App.fleet && view !== "fleet") { App.fleet.destroy(); App.fleet = null; }
  App.view = view;
  closeMenu();
  $$("#nav button, #bottom-nav button[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("#view-title").textContent = TITLES[view];
  $("#view").className = "view" + (view === "fleet" ? " fleet-mode" : "");
  if (location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
  VIEWS[view].render(false);
}
$$("#nav button, #bottom-nav button[data-view]").forEach((b) => b.addEventListener("click", () => go(b.dataset.view)));
// Phone / tablet slide-in menu
function openMenu() { $(".sidebar").classList.add("open"); $("#nav-backdrop").classList.add("show"); }
function closeMenu() { $(".sidebar").classList.remove("open"); $("#nav-backdrop").classList.remove("show"); }
$("#menu-btn").addEventListener("click", openMenu);
$("#bn-more").addEventListener("click", openMenu);
$("#nav-backdrop").addEventListener("click", closeMenu);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
function refresh() { poll(); if (!VIEWS[App.view].live) VIEWS[App.view].render(true); }

// ================================================================ shared detail modals
async function openTask(taskId) {
  let t;
  try { t = await api(`/api/tasks/${taskId}`); } catch (err) { toast(esc(err.message), "error"); return; }
  const isApproval = t.status === "WAITING" && !String(t.wait_reason || "").startsWith("manual");
  const acts = [];
  if (t.status === "WAITING" && isApproval) acts.push({ label: "✔ Approve", cls: "success", onClick: async () => { await api(`/api/tasks/${t.id}/approve`, { method: "POST" }); toast("Approved — the next loop step will pick it up", "success"); refresh(); } });
  if (t.status === "WAITING") acts.push({ label: "I did it manually…", cls: "warn", onClick: () => { manualComplete(t); } });
  if (t.status === "FAILED") acts.push({ label: "↻ Retry", cls: "primary", onClick: async () => { await api(`/api/tasks/${t.id}/retry`, { method: "POST" }); toast("Task back in the queue", "success"); refresh(); } });
  if (["PENDING", "READY", "WAITING", "FAILED"].includes(t.status)) acts.push({ label: "Cancel task", cls: "danger", onClick: async () => {
    if (!(await confirmBox("Cancel this task? Tasks depending on it will be cancelled by the project manager."))) return false;
    await api(`/api/tasks/${t.id}/cancel`, { method: "POST", body: { reason: "cancelled from dashboard" } }); toast("Task cancelled"); refresh(); } });
  acts.push({ label: "Close", cls: "ghost", onClick: () => {} });
  modal({ title: `${deptBadge(t.department)} ${esc(trunc(t.description, 80))}`, wide: true, actions: acts, body: `
    <dl class="kv">
      <dt>Status</dt><dd>${badge(t.status)} ${t.wait_reason ? `<span class="muted">— ${esc(t.wait_reason)}</span>` : ""}</dd>
      <dt>Project</dt><dd>${esc(t.project_name)}</dd>
      <dt>Description</dt><dd>${esc(t.description)}</dd>
      <dt>Priority</dt><dd>P${t.priority}</dd>
      <dt>Capabilities</dt><dd>${(t.required_capabilities || []).map((c) => `<span class="badge">${esc(c)}</span>`).join(" ") || "—"}</dd>
      <dt>Cost</dt><dd>estimated ${eur(t.estimated_cost)} · actual ${eur(t.actual_cost)}${t.approved_by ? ` · approved by ${esc(t.approved_by)}` : ""}</dd>
      <dt>Attempts</dt><dd>${t.attempts}</dd>
      <dt>Depends on</dt><dd>${(t.dependency_info || []).map((d) => `${badge(d.status)} ${esc(trunc(d.description, 60))}`).join("<br>") || "—"}</dd>
      <dt>Created / done</dt><dd>${when(t.created_at)} / ${when(t.completed_at)}</dd>
    </dl>
    ${t.error ? `<h4>Error</h4><pre class="block">${esc(t.error)}</pre>` : ""}
    ${t.result ? `<h4>Result</h4><pre class="block">${esc(t.result)}</pre>` : ""}
    ${(t.workers || []).length ? `<h4>Workers</h4><div class="table-wrap"><table><tr><th>Worker</th><th>Status</th><th>Model</th><th>Tokens</th><th>Started</th></tr>
      ${t.workers.map((w) => `<tr><td class="mono">${esc(w.id)}</td><td>${badge(w.status)}</td><td>${esc(w.model_key || "—")}</td><td>${w.tokens}</td><td>${when(w.created_at)}</td></tr>`).join("")}</table></div>` : ""}` });
}
function manualComplete(t) {
  modal({ title: "Complete task manually", body: `
    <p class="muted">${esc(t.description)}</p>
    <label class="field"><span>What did you do / what is the result?</span><textarea name="result" rows="5" required></textarea></label>
    <label class="field"><span>Money spent (EUR)</span><input name="cost_eur" type="number" min="0" step="0.01" value="0"></label>`,
    actions: [{ label: "Cancel", cls: "ghost", onClick: () => {} }, { label: "Mark completed", cls: "success", onClick: async (root) => {
      const v = formValues(root);
      if (!v.result) throw new Error("Please describe the result");
      await api(`/api/tasks/${t.id}/complete`, { method: "POST", body: { result: v.result, cost_eur: v.cost_eur || 0 } });
      toast("Task completed — dependent tasks are now unblocked", "success"); refresh();
    } }] });
}
function openRoom(projectId, dept) {
  const p = (App.state.projects || []).find((x) => x.id === projectId);
  const tasks = (p?.tasks || []).filter((t) => t.department === dept);
  const m = modal({ title: `${deptBadge(dept)} room · ${esc(p?.name || "")}`, body: tasks.length ? `<div class="table-wrap"><table>
    <tr><th>Status</th><th>Task</th><th>P</th></tr>
    ${tasks.map((t) => `<tr class="clickable" data-task="${esc(t.id)}"><td>${badge(t.status)}</td><td>${esc(t.description)}</td><td>P${t.priority}</td></tr>`).join("")}</table></div>`
    : `<div class="empty">No tasks for this department in this project.</div>` });
  $$("[data-task]", m.root).forEach((r) => r.addEventListener("click", () => { m.close(); openTask(r.dataset.task); }));
}

// ================================================================ views
const VIEWS = {};

// ---------- Fleet
VIEWS.fleet = {
  render(isRefresh) {
    if (isRefresh && App.fleet) return;
    const v = $("#view"); v.innerHTML = "";
    const root = document.createElement("div"); v.appendChild(root);
    App.fleet = new FleetView(root, { onTask: openTask, onRoom: openRoom, onMothership: () => openChat("main"),
      onNewProject: newProject, onTasks: (projectId) => openTasks(projectId), onProject: openProject,
      onChat: (projectId) => openChat(projectId) });
    if (App.state) App.fleet.update(App.state);
    App.fleet.start();
  },
};

// ---------- Talk to the Master Orchestrator (main chat + one chat per project)
const ACTION_LABELS = { create_project: "Create project", plan_project: "Plan project", review_project: "Review project",
  pause_project: "Pause project", resume_project: "Resume project", finish_project: "Finish project", add_task: "Add task",
  start_work: "Start work loop", pause_work: "Pause work loop", complete_task: "Mark your task done", approve_task: "Approve task",
  record_metric: "Record result", add_guideline: "Save guideline" };
function actionCard(m, i, e) {
  const a = e.action;
  const proj = esc(projectName(a.project_id) || a.project_id);
  const what = a.type === "create_project" ? `${esc(a.name)} — budget ${eur(a.budget_eur)}, priority P${a.priority}<br><span class="muted">${esc(trunc(a.objective, 200))}</span>`
    : a.type === "add_guideline" ? `“${esc(a.content)}”`
    : a.type === "add_task" ? `${proj}: ${deptBadge(a.department)}${a.capability ? ` <span class="badge">${esc(a.capability)}</span>` : ""} ${esc(a.description)}`
    : a.type === "complete_task" ? `<span class="mono">${esc(a.task_id)}</span> — ${esc(trunc(a.result, 200))}`
    : a.type === "approve_task" ? `<span class="mono">${esc(a.task_id)}</span>`
    : a.type === "record_metric" ? `${proj}: ${esc(a.metric)} = <b>${esc(a.value)}</b>`
    : ["start_work", "pause_work"].includes(a.type) ? "" : proj;
  const status = { proposed: "PROPOSED", executing: "RUNNING", executed: "COMPLETED", dismissed: "CANCELLED", invalid: "FAILED", failed: "FAILED" }[e.status];
  const auto = e.status === "proposed" && e.needs_owner === false;   // about to run by itself
  const label = auto ? "starting" : e.status === "proposed" ? "needs your OK" : e.status === "executed" ? (e.by === "HUMAN" ? "done (you)" : "done") : e.status;
  return `<div class="action-card ${esc(e.status)}">
    <div class="item-head"><span class="what">${esc(ACTION_LABELS[a.type] || a.type)}</span>${badge(label, status)}</div>
    ${what ? `<div>${what}</div>` : ""}<div class="why">Why: ${esc(a.reason)}</div>
    ${e.problem ? `<div class="error-text">${esc(e.problem)}</div>` : ""}
    ${e.result ? `<div class="valid-msg">✔ ${esc(e.result)}</div>` : ""}
    ${e.status === "proposed" && !auto ? `<div class="btn-row mt8"><button class="btn small success" data-exec="${m.id}:${i}">✔ Do it</button><button class="btn small ghost" data-dismiss="${m.id}:${i}">Dismiss</button></div>` : ""}
  </div>`;
}
function chatMessage(m) {
  const body = esc(m.content).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>");
  if (m.role === "owner") return `<div class="msg owner"><div class="bubble">${body}<div class="meta">You · ${when(m.ts)}</div></div></div>`;
  const auto = m.model === "autopilot";
  return `<div class="msg"><canvas data-avatar="orch"></canvas><div class="bubble ${auto ? "update" : ""}">${body}
    ${m.actions.map((e, i) => actionCard(m, i, e)).join("")}
    <div class="meta">Master Orchestrator · ${auto ? "update" : esc(m.model || "")} · ${when(m.ts)}</div></div></div>`;
}
const seenKey = (t) => "aic_seen_" + t;
function seenId(t) { try { return Number(localStorage.getItem(seenKey(t)) || 0); } catch { return 0; } }
function markSeen(t, id) { try { localStorage.setItem(seenKey(t), String(id || 0)); } catch { /* storage blocked */ } }
function openChat(thread) { VIEWS.command.thread = thread || "main"; VIEWS.command.sig = null; go("command"); }
VIEWS.command = {
  live: true,
  sig: null,
  thread: "main",
  async render(isRefresh) {
    const threads = await api("/api/chat/threads");
    if (!threads.some((t) => t.id === this.thread)) this.thread = "main";
    const msgs = await api(`/api/chat?thread=${encodeURIComponent(this.thread)}`);
    const last = msgs[msgs.length - 1];
    const sig = JSON.stringify(threads.map((t) => [t.id, t.last_id])) + "|" + this.thread + "|" +
      (last ? last.id + JSON.stringify(last.actions.map((x) => x.status)) : "") + "|" + (App.chatJob ? App.chatJob.id : "");
    if (isRefresh && sig === this.sig) return;             // nothing new: keep what you are typing
    this.sig = sig;
    if (last) markSeen(this.thread, last.id);
    const typed = $("#chat-text") ? $("#chat-text").value : "";
    const cur = threads.find((t) => t.id === this.thread);
    const isMain = this.thread === "main";
    const thinking = App.chatJob && App.chatJob.thread === this.thread;
    const suggestions = isMain
      ? ["What do you need from me?", "How are my projects doing?", "Make and launch a printable product for busy parents (budget 15 EUR)", "Pause all work"]
      : ["What is the status?", "Tell me the results so far", "What do you need from me?", "Pause this project"];
    const intro = isMain
      ? `<h3>Master Orchestrator</h3><p>Tell me what you want — I create the projects, plan them and run the work. Every project also gets its own chat on the left.
         Money, publishing and finishing projects always wait for your <b>OK</b>.</p>`
      : `<h3>${esc(cur ? cur.name : "Project")}</h3><p>Ask me about this project: progress, results, research, files. Give feedback and I adjust the work.</p>`;
    $("#view").innerHTML = `<div class="chat-layout">
      <aside class="thread-list">${threads.map((t) => `<button class="thread ${t.id === this.thread ? "active" : ""}" data-thread="${esc(t.id)}">
          <span class="t-name">${t.id === "main" ? "🧠 " : ""}${esc(t.name)}</span>
          <span class="t-meta">${t.status ? badge(t.status) : `<span class="muted">all projects</span>`}${t.last_id && t.last_id > seenId(t.id) && t.id !== this.thread ? `<span class="nav-badge">new</span>` : ""}</span>
        </button>`).join("")}</aside>
      <div class="chat">
        <div class="chat-head"><b>${isMain ? "🧠 Master Orchestrator" : esc(cur ? cur.name : "")}</b>
          ${isMain ? `<span class="muted">sees every project</span>` : `<button class="btn small ghost" id="chat-project">Project details ▸</button>`}</div>
        <div class="chat-log" id="chat-log">${msgs.map(chatMessage).join("") || `<div class="chat-intro"><canvas data-avatar="orch"></canvas>${intro}</div>`}</div>
        <div class="chat-thinking ${thinking ? "" : "hidden"}" id="chat-thinking"><canvas data-avatar="think"></canvas><span class="dots">Orchestrator is thinking</span></div>
        <div class="chat-suggest">${suggestions.map((s) => `<button class="chip" data-suggest="${esc(s)}">${esc(s)}</button>`).join("")}</div>
        <form class="chat-input" id="chat-form"><textarea id="chat-text" rows="2" placeholder="${isMain ? "Message the Master Orchestrator…" : "Ask about this project…"} (Enter to send, Shift+Enter for a new line)"></textarea>
          <button class="btn primary" id="chat-send" type="submit" ${App.chatJob ? "disabled" : ""}>Send</button></form>
      </div></div>`;
    $$("canvas[data-avatar]").forEach((c) => drawAvatar(c, c.dataset.avatar));
    const log = $("#chat-log"); log.scrollTop = log.scrollHeight;
    const text = $("#chat-text");
    text.value = typed;
    $$("[data-thread]").forEach((b) => b.addEventListener("click", () => { this.thread = b.dataset.thread; this.sig = null; $("#chat-text").value = ""; this.render(); }));
    const pbtn = $("#chat-project"); if (pbtn) pbtn.addEventListener("click", () => openProject(this.thread));
    const send = async (message) => {
      message = message.trim();
      if (!message || App.chatJob) return;
      $("#chat-send").disabled = true;
      const thread = this.thread;
      try {
        const r = await api("/api/chat", { method: "POST", body: { message, thread } });
        App.chatJob = { id: r.job.id, thread };
        text.value = "";
        const done = () => { App.chatJob = null; if (App.view === "command") { this.sig = null; this.render(); } };
        trackJob(r.job, done, (j) => { toast(`✖ Orchestrator could not answer: ${esc(j.error)}`, "error", 9000); done(); });
        this.sig = null; this.render();
      } catch (e) { toast(esc(e.message), "error"); $("#chat-send").disabled = false; }
    };
    $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); send(text.value); });
    text.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(text.value); } });
    $$("[data-suggest]").forEach((b) => b.addEventListener("click", () => send(b.dataset.suggest)));
    $$("[data-exec]").forEach((b) => b.addEventListener("click", async () => {
      const [id, i] = b.dataset.exec.split(":");
      b.disabled = true;
      try {
        const j = await api(`/api/chat/${id}/actions/${i}/execute`, { method: "POST" });
        toast(`⏳ ${esc(j.label)}…`, "info", 2500);
        trackJob(j, () => { toast("✔ Done", "success"); refresh(); if (App.view === "command") { this.sig = null; this.render(); } },
          (x) => { toast(`✖ ${esc(x.error)}`, "error", 9000); if (App.view === "command") { this.sig = null; this.render(); } });
      } catch (e) { toast(esc(e.message), "error"); b.disabled = false; }
    }));
    $$("[data-dismiss]").forEach((b) => b.addEventListener("click", async () => {
      const [id, i] = b.dataset.dismiss.split(":");
      await api(`/api/chat/${id}/actions/${i}/dismiss`, { method: "POST" }).catch((e) => toast(esc(e.message), "error"));
      this.sig = null; this.render();
    }));
    if (!App.chatJob && !isRefresh) text.focus();
  },
};

// ---------- Resources (computers, GPU, models, tools)
VIEWS.resources = {
  live: true,
  async render() {
    const [d, md] = await Promise.all([api("/api/resources"), api("/api/models").catch(() => null)]);
    const meter = (label, value, pct, cls = "") => `<div class="meter"><div class="row"><span>${label}</span><span>${value}</span></div>${bar(pct, cls)}</div>`;
    const node = (n) => {
      const st = !n.enabled ? badge("disabled", "PENDING") : n.reachable ? badge("online", "ok") : badge("offline", "FAILED");
      const s = n.slots || {}, m = n.metrics || {};
      const gpus = (m.gpus || []).map((g) => `
        ${meter(`GPU · ${esc(g.name)}`, `${g.utilization_pct}% · ${g.temperature_c}°C`, g.utilization_pct)}
        ${meter("VRAM", `${(g.vram_used_mb / 1024).toFixed(1)} / ${(g.vram_total_mb / 1024).toFixed(1)} GB`, g.vram_used_mb / g.vram_total_mb * 100, g.vram_used_mb / g.vram_total_mb > 0.9 ? "over" : "budget")}`).join("");
      return `<div class="card node-card">
        <h3><span>${esc(n.name)} <span class="url">${esc(n.ollama_url)}</span></span>${st}</h3>
        <p class="muted">${esc(n.description)}${n.version ? ` · Ollama ${esc(n.version)}` : ""}</p>
        ${gpus}
        ${m.cpu_pct != null ? meter(`CPU · ${m.cpu_cores} threads`, `${m.cpu_pct}%`, m.cpu_pct) + meter("RAM", `${m.ram_used_gb} / ${m.ram_total_gb} GB`, m.ram_used_gb / m.ram_total_gb * 100) : ""}
        ${n.enabled ? `${meter("GPU slots in use", `${s.active || 0} / ${n.max_concurrent_gpu_tasks}${s.waiting ? ` · ${s.waiting} waiting` : ""}`, (s.active || 0) / n.max_concurrent_gpu_tasks * 100, s.waiting ? "over" : "green")}
        <p class="muted">${s.calls || 0} model calls · ${s.errors || 0} errors · busy ${Math.round(s.busy_seconds || 0)} s${s.last_model ? ` · last: ${esc(s.last_model)}` : ""}</p>` : ""}
        <h4>Loaded in memory</h4>${(n.loaded || []).length ? n.loaded.map((l) => `<div>${esc(l.name)} <span class="muted">${(l.size / 1e9).toFixed(2)} GB · ${Math.round(l.size_vram / l.size * 100)}% on GPU</span></div>`).join("") : `<div class="muted">nothing loaded</div>`}
        <h4>Installed</h4><div class="chips">${(n.installed || []).map((x) => `<span class="badge">${esc(x)}</span>`).join("") || `<span class="muted">—</span>`}</div>
      </div>`;
    };
    const q = d.queue;
    const models = md ? `
      <h3 class="section-title">Models &amp; tools</h3>
      ${md.ollama_error ? `<div class="card danger-card">⚠ Ollama not reachable: ${esc(md.ollama_error)}</div>` : ""}
      <div class="grid two">
        <div class="card"><h3>Models</h3><div class="table-wrap"><table>
          <tr><th>Model</th><th>Status</th><th>Quality</th><th>~Latency</th></tr>
          ${md.models.map((m) => `<tr><td>${esc(m.model)} <span class="muted">(${esc(m.key)})</span></td>
          <td>${!m.enabled ? badge("disabled", "PENDING") : m.installed ? badge("ready", "ok") : badge("not installed", "FAILED")}</td>
          <td>${"★".repeat(m.quality)}</td><td>${m.typical_latency_seconds}s</td></tr>`).join("")}</table></div></div>
        <div class="card"><h3>Performance by model &amp; department <span class="muted">from real runs</span></h3>
          ${md.stats.length ? `<table><tr><th>Model</th><th>Dept</th><th>Runs</th><th>Success</th><th>Avg s</th></tr>${md.stats.map((s) => `<tr><td class="mono">${esc(s.model_key || "—")}</td><td>${deptBadge(s.department)}</td><td>${s.runs}</td><td>${Math.round(s.success_rate * 100)}%</td><td>${s.avg_seconds}</td></tr>`).join("")}</table>` : `<div class="empty">No runs yet.</div>`}</div>
        <div class="card"><h3>Tools</h3>${md.tools.map((t) => `<div class="item"><div class="item-head"><b class="mono">${esc(t.name)}</b>${t.available ? badge("available", "ok") : badge(t.reason, "WAITING")}</div>
          <p class="muted">${esc(t.description)}</p></div>`).join("")}</div>
      </div>` : "";
    $("#view").innerHTML = `
      <div class="grid kpis">
        <div class="card kpi"><div class="label">Running now</div><div class="value">${q.RUNNING}</div><div class="sub">${esc(trunc(d.running[0]?.task || "—", 40))}</div></div>
        <div class="card kpi"><div class="label">Ready queue</div><div class="value">${q.READY}</div><div class="sub">${q.PENDING} pending on dependencies</div></div>
        <div class="card kpi"><div class="label">Waiting for you</div><div class="value">${q.WAITING}</div></div>
        <div class="card kpi"><div class="label">Background AI jobs</div><div class="value">${d.jobs.length}</div><div class="sub">${esc(d.jobs.map((j) => j.label).join(", ") || "none")}</div></div>
        <div class="card kpi"><div class="label">Work loop</div><div class="value">${esc(d.runner.state)}</div><div class="sub">${esc(d.runner.activity)}${d.runner.keep_awake ? " · PC kept awake" : ""}</div></div>
      </div>
      <div class="grid two mt16">${d.nodes.map(node).join("") || `<div class="empty">No node pool (test mode).</div>`}</div>
      <p class="muted mt16">One GPU slot per 4 GB card: the work loop, background jobs and chat queue for the GPU instead of running two models at once.
        To add a computer, run Ollama on it and register it in <b>Settings → Advanced → nodes.json</b>.</p>
      ${models}`;
    dyn($("#view"));
  },
};

// ---------- Overview
VIEWS.overview = {
  live: true,
  async render() {
    const s = App.state; if (!s) return;
    const events = await api("/api/events?limit=300").catch(() => []);
    const decisions = events.filter((e) => e.action.startsWith("Decision") || e.action.startsWith("Checkpoint decision") || e.action.startsWith("Experiment ") || e.action === "Pivoted").slice(0, 8);
    const problems = events.filter((e) => /failed|error|Refused|Gave up|Escalated/i.test(e.action)).slice(0, 8);
    const working = (s.agents || []).filter((a) => a.status === "RUNNING");
    const waiting = s.projects.flatMap((p) => (p.tasks || []).filter((t) => t.status === "WAITING").map((t) => ({ ...t, project: p.name })));
    const active = s.projects.filter((p) => p.status === "ACTIVE");
    const b = s.budget;
    $("#view").innerHTML = `
      <div class="grid kpis">
        <div class="card kpi"><div class="label">Active projects</div><div class="value">${active.length}</div><div class="sub">${s.projects.length} total</div></div>
        <div class="card kpi"><div class="label">Crew working now</div><div class="value">${working.length}</div><div class="sub">${esc(working[0]?.department || "—")} ${working[0] ? "· " + esc(trunc(working[0].task, 30)) : ""}</div></div>
        <div class="card kpi"><div class="label">Needs you</div><div class="value">${s.waiting}</div><div class="sub">approvals &amp; manual steps</div></div>
        <div class="card kpi"><div class="label">Spent</div><div class="value">${eur(b.spent)}</div><div class="sub">${eur(b.allocated)} allocated of ${eur(b.company_total)}</div>${bar(b.company_total ? b.spent / b.company_total * 100 : 0, "budget")}</div>
        <div class="card kpi"><div class="label">Work loop</div><div class="value">${esc(s.runner.state)}</div><div class="sub">${s.runner.tasks_run} tasks run this session</div></div>
      </div>
      <div class="grid two mt16">
        <div class="card"><h3>Needs you <span class="muted">approvals &amp; manual steps</span></h3>
          ${waiting.length ? `<div class="list">${waiting.map((t) => `<div class="item"><div class="item-head"><span>${deptBadge(t.department)} <b>${esc(trunc(t.description, 70))}</b></span>
            <button class="btn small warn" data-task="${esc(t.id)}">Review</button></div><p class="muted">${esc(t.project)} · ${esc(t.wait_reason)}</p></div>`).join("")}</div>` : `<div class="empty">Nothing waiting for you 🎉</div>`}</div>
        <div class="card"><h3>Projects</h3>
          ${s.projects.length ? `<div class="list">${s.projects.map((p) => `<div class="item clickable" data-project="${esc(p.id)}">
            <div class="item-head"><b>${esc(p.name)}</b>${badge(p.status)}</div>
            <div class="form-grid mt8"><div><span class="muted">Tasks ${p.progress}%</span>${bar(p.progress, "green")}</div>
            <div><span class="muted">Budget ${eur(p.spent)} / ${eur(p.budget_eur)}</span>${bar(p.budget_eur ? p.spent / p.budget_eur * 100 : 0, "budget")}</div></div></div>`).join("")}</div>` : `<div class="empty">No projects yet.</div>`}</div>
        <div class="card"><h3>Recent decisions</h3>
          ${decisions.length ? `<div class="list">${decisions.map((e) => `<div class="item"><div class="item-head"><b>${esc(e.action)}</b><span class="muted">${when(e.ts)}</span></div>
            <p class="muted">${esc(projectName(e.project_id))} · ${details(e.details, 260)}</p></div>`).join("")}</div>` : `<div class="empty">No decisions yet.</div>`}</div>
        <div class="card"><h3>Problems <span class="muted">failures, refusals, escalations</span></h3>
          ${problems.length ? `<div class="list">${problems.map((e) => `<div class="item"><div class="item-head"><b>${esc(e.action)}</b><span class="muted">${when(e.ts)}</span></div>
            <p class="muted">${esc(e.actor)} · ${esc(projectName(e.project_id))} · ${details(e.details, 220)}</p></div>`).join("")}</div>` : `<div class="empty">No problems 👍</div>`}</div>
      </div>`;
    dyn($("#view"));
    $$("[data-task]").forEach((b) => b.addEventListener("click", () => openTask(b.dataset.task)));
    $$("[data-project]").forEach((b) => b.addEventListener("click", () => openProject(b.dataset.project)));
  },
};

// ---------- Projects + tasks (opened from Fleet, Overview and the chats)
async function knownPermissions() {
  try { return JSON.parse((await api("/api/config/policy.json")).text).known_permissions || []; } catch { return []; }
}
async function newProject() {
  const perms = await knownPermissions();
  modal({ title: "New project", body: `
    <p class="muted">Tip: you can also just tell the orchestrator what you want — it creates and plans the project for you.</p>
    <label class="field"><span>Name</span><input name="name" required maxlength="200"></label>
    <label class="field"><span>Goal</span><textarea name="objective" rows="3" required placeholder="What should this project achieve? Audience, constraints, what success looks like."></textarea></label>
    <div class="form-grid">
      <label class="field"><span>Budget (EUR)</span><input name="budget_eur" type="number" min="0" step="0.5" value="10"></label>
      <label class="field"><span>Priority (1 = highest)</span><select name="priority">${[1, 2, 3, 4, 5].map((n) => `<option ${n === 3 ? "selected" : ""}>${n}</option>`).join("")}</select></label>
    </div>
    <span class="muted">Permissions</span>
    <div class="checks">${perms.map((p) => `<label><input type="checkbox" name="perm_${esc(p)}" data-group="permissions" value="${esc(p)}"> ${esc(p)}</label>`).join("") || "<span class='muted'>none defined</span>"}</div>`,
    actions: [{ label: "Cancel", cls: "ghost", onClick: () => {} }, { label: "Create project", cls: "primary", onClick: async (root) => {
      const v = formValues(root);
      const p = await api("/api/projects", { method: "POST", body: { name: v.name, objective: v.objective, budget_eur: v.budget_eur || 0, priority: Number(v.priority), permissions: v.permissions || [] } });
      toast(`🚀 Ship launched: <b>${esc(p.name)}</b> — the orchestrator plans it when the work loop runs`, "success"); refresh();
    } }] });
}
async function openProject(id) {
  let d;
  try { d = await api(`/api/projects/${id}`); } catch (err) { toast(esc(err.message), "error"); return; }
  const p = d.project, r = d.report, st = p.status;
  const acts = [{ label: "💬 Chat about it", cls: "primary", onClick: () => openChat(id) }];
  if (st === "ACTIVE") {
    acts.push({ label: "🧠 Plan next tasks", onClick: () => runJob(`/api/projects/${id}/plan`) });
    acts.push({ label: "Pause", cls: "warn", onClick: () => setProjectStatus(id, "PAUSED") });
  }
  if (st === "PAUSED") acts.push({ label: "Resume", cls: "success", onClick: () => setProjectStatus(id, "ACTIVE") });
  if (st === "ACTIVE" || st === "PAUSED") acts.push({ label: "Finish", onClick: () => setProjectStatus(id, "FINISHED") });
  acts.push({ label: "🗑 Delete", cls: "danger", onClick: () => deleteProject(p) });
  const b = r.budget_eur;
  const m = modal({ title: `${esc(p.name)} ${badge(st)}`, wide: true, actions: acts, body: `
    <p>${esc(p.objective).replace(/\n/g, "<br>")}</p>
    <div class="grid kpis">
      <div class="card kpi"><div class="label">Progress</div><div class="value">${r.progress_percent}%</div>${bar(r.progress_percent, "green")}</div>
      <div class="card kpi"><div class="label">Spent</div><div class="value">${eur(b.spent)}</div><div class="sub">of ${eur(b.allocated)}</div>${bar(b.allocated ? b.spent / b.allocated * 100 : 0, "budget")}</div>
      <div class="card kpi"><div class="label">Files</div><div class="value">${d.files_total || 0}</div><div class="sub">${d.files_draft || 0} to review</div></div>
      <div class="card kpi"><div class="label">Priority</div><div class="value">P${p.priority}</div><div class="sub">${(p.permissions || []).join(", ") || "no extra permissions"}</div></div>
    </div>
    <div class="btn-row mt8"><button class="btn small ghost" id="proj-files">Open files ▸</button></div>
    <h4 class="section-title">Tasks</h4>
    ${d.tasks.length ? `<div class="table-wrap"><table><tr><th>Status</th><th>Dept</th><th>Task</th><th>Cost</th></tr>
      ${d.tasks.map((t) => `<tr class="clickable" data-task="${esc(t.id)}"><td>${badge(t.status)}</td><td>${deptBadge(t.department)}</td>
      <td>${esc(trunc(t.description, 110))}${t.wait_reason ? `<br><span class="muted">${esc(t.wait_reason)}</span>` : ""}</td><td>${eur(t.actual_cost || t.estimated_cost)}</td></tr>`).join("")}</table></div>`
      : `<div class="empty">No tasks yet — the orchestrator plans them when the work loop runs.</div>`}` });
  $$("[data-task]", m.root).forEach((row) => row.addEventListener("click", () => { m.close(); openTask(row.dataset.task); }));
  $("#proj-files", m.root).addEventListener("click", () => { m.close(); VIEWS.studio.tab = "files"; VIEWS.studio.filters = { project: id, kind: "", status: "" }; go("studio"); });
}
async function setProjectStatus(id, status) {
  const verbs = { PAUSED: "Pause", ACTIVE: "Resume", FINISHED: "Finish", CANCELLED: "Cancel" };
  if (["FINISHED", "CANCELLED"].includes(status) && !(await confirmBox(`${verbs[status]} this project? Its open tasks will be cancelled. This cannot be undone.`))) return false;
  await api(`/api/projects/${id}/status`, { method: "POST", body: { status, reason: `${verbs[status]} from dashboard` } });
  toast(`Project: ${verbs[status].toLowerCase()}d`, "success"); refresh();
}
async function deleteProject(p) {
  if (!(await confirmBox(`Delete <b>${esc(p.name)}</b> for good? Its tasks, chat, research and produced files are removed permanently. The activity log and expenses are kept.`, { label: "Delete permanently" }))) return false;
  try {
    const r = await api(`/api/projects/${p.id}`, { method: "DELETE" });
    toast(`🗑 Deleted <b>${esc(r.deleted)}</b> (${r.tasks} tasks, ${r.files} files)`, "success");
    if (VIEWS.command.thread === p.id) VIEWS.command.thread = "main";
    refresh();
  } catch (e) { toast(esc(e.message), "error"); return false; }
}
function openTasks(projectId, status) {
  const f = { project: projectId || "", status: status || "" };
  const statuses = ["", "WAITING", "READY", "RUNNING", "PENDING", "FAILED", "COMPLETED", "CANCELLED"];
  const m = modal({ title: "Tasks", wide: true, body: `<div id="tasks-box"><div class="empty">Loading…</div></div>` });
  const draw = async () => {
    const q = new URLSearchParams(); if (f.status) q.set("status", f.status); if (f.project) q.set("project_id", f.project);
    const tasks = await api(`/api/tasks?${q}`).catch(() => []);
    $("#tasks-box", m.root).innerHTML = `
      <div class="toolbar">
        <select id="t-status">${statuses.map((s) => `<option value="${s}" ${s === f.status ? "selected" : ""}>${s || "All statuses"}</option>`).join("")}</select>
        <select id="t-project"><option value="">All projects</option>${(App.state?.projects || []).map((p) => `<option value="${esc(p.id)}" ${p.id === f.project ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
        <span class="muted">${tasks.length} task(s) · click one for details</span></div>
      <div class="table-wrap"><table>
        <tr><th>Status</th><th>Project</th><th>Dept</th><th>Task</th><th>Updated</th></tr>
        ${tasks.map((t) => `<tr class="clickable" data-task="${esc(t.id)}"><td>${badge(t.status)}</td><td>${esc(t.project_name)}</td><td>${deptBadge(t.department)}</td>
          <td>${esc(trunc(t.description, 110))}${t.wait_reason ? `<br><span class="muted">${esc(t.wait_reason)}</span>` : ""}${t.error && t.status !== "COMPLETED" ? `<br><span class="error-text">${esc(trunc(t.error, 100))}</span>` : ""}</td>
          <td>${when(t.completed_at || t.started_at || t.created_at)}</td></tr>`).join("") || `<tr><td colspan="5"><div class="empty">No tasks match.</div></td></tr>`}
      </table></div>`;
    dyn(m.root);
    $("#t-status", m.root).addEventListener("change", (e) => { f.status = e.target.value; draw(); });
    $("#t-project", m.root).addEventListener("change", (e) => { f.project = e.target.value; draw(); });
    $$("[data-task]", m.root).forEach((r) => r.addEventListener("click", () => { m.close(); openTask(r.dataset.task); }));
  };
  draw();
}

// ---------- Studio: web research, products, images, videos, campaigns + the files they made
const THEMES = ["modern", "classic", "playful", "minimal", "bold", "botanical"];
const opt = (list, cur) => list.map((x) => `<option ${x === cur ? "selected" : ""}>${esc(x)}</option>`).join("");
const KIND_ICON = { product_pdf: "📄", bundle: "🗜", pack: "🗜", video: "🎬", report: "🔎", listing: "🏷", copy: "✍", captions: "💬", audio: "🔊" };
const kb = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
const STUDIO_FORMS = {
  research: { tool: "web_research", label: "🔎 Web research", needsProject: false, fields: () => `
    <label class="field"><span>What do we need to find out?</span><textarea name="goal" rows="2" placeholder="e.g. What printable wedding planners sell best on Etsy, at what prices, and what buyers complain about"></textarea></label>
    <div class="form-grid"><label class="field"><span>Search query 1</span><input name="q1" placeholder="best selling printable wedding planner"></label>
    <label class="field"><span>Search query 2 (optional)</span><input name="q2"></label>
    <label class="field"><span>Search query 3 (optional)</span><input name="q3"></label>
    <label class="field"><span>Pages to read in full</span><input name="max_pages" type="number" min="1" max="6" value="4"></label></div>`,
    params: (v) => ({ goal: v.goal, queries: [v.q1, v.q2, v.q3].filter(Boolean), max_pages: v.max_pages || 4 }) },
  product: { tool: "product_builder", label: "📄 Digital product", needsProject: true, fields: () => `
    <div class="form-grid"><label class="field"><span>Type</span><select name="product_type">${opt(["planner", "worksheet", "checklist", "tracker", "journal", "workbook", "ebook", "guide"], "planner")}</select></label>
    <label class="field"><span>Theme</span><select name="theme">${opt(THEMES, "modern")}</select></label>
    <label class="field"><span>Content pages</span><input name="pages" type="number" min="3" max="24" value="8"></label></div>
    <label class="field"><span>Title</span><input name="title" maxlength="70" placeholder="Wedding Planner Essentials"></label>
    <label class="field"><span>Subtitle</span><input name="subtitle" maxlength="110" placeholder="Your 12-month printable planning kit"></label>
    <label class="field"><span>Audience</span><input name="audience" maxlength="200" placeholder="couples planning a small wedding on a budget"></label>
    <label class="field"><span>Must contain (from your research)</span><textarea name="content_notes" rows="3" maxlength="1200" placeholder="budget tracker, guest list, 12-month checklist, vendor contacts…"></textarea></label>`,
    params: (v) => ({ product_type: v.product_type, title: v.title, subtitle: v.subtitle, audience: v.audience, content_notes: v.content_notes, pages: v.pages || 8, theme: v.theme }) },
  campaign: { tool: "campaign_builder", label: "📣 Campaign", needsProject: true, fields: () => `
    <label class="field"><span>Campaign name</span><input name="name" maxlength="80" placeholder="Launch week"></label>
    <label class="field"><span>Goal</span><input name="goal" maxlength="300" placeholder="first 20 sales in 14 days"></label>
    <label class="field"><span>Audience</span><input name="audience" maxlength="200"></label>
    <label class="field"><span>Key message</span><input name="key_message" maxlength="300"></label>
    <label class="field"><span>Offer (optional)</span><input name="offer" maxlength="120" placeholder="20% off launch week"></label>
    <div class="checks">${["instagram", "pinterest", "tiktok", "facebook", "x", "email", "blog"].map((ch) => `<label><input type="checkbox" name="channels" data-group="channels" value="${ch}" ${["instagram", "pinterest", "email"].includes(ch) ? "checked" : ""}> ${ch}</label>`).join("")}</div>
    <div class="form-grid"><label class="field"><span>Days</span><input name="duration_days" type="number" min="3" max="30" value="14"></label>
    <label class="field"><span>Posts per channel</span><input name="posts_per_channel" type="number" min="1" max="6" value="3"></label>
    <label class="field"><span>Videos</span><input name="videos" type="number" min="0" max="3" value="1"></label>
    <label class="field"><span>Theme</span><select name="theme">${opt(THEMES, "modern")}</select></label></div>`,
    params: (v) => ({ name: v.name, goal: v.goal, audience: v.audience, key_message: v.key_message, offer: v.offer, channels: v.channels || [], duration_days: v.duration_days || 14, posts_per_channel: v.posts_per_channel || 3, videos: v.videos ?? 1, theme: v.theme }) },
  video: { tool: "video_studio", label: "🎬 Video", needsProject: true, fields: () => `
    <div class="form-grid"><label class="field"><span>Title</span><input name="title" maxlength="80"></label>
    <label class="field"><span>Format</span><select name="format"><option value="story">9:16 Reels / TikTok / Shorts</option><option value="square">1:1 feed</option><option value="wide">16:9 YouTube</option></select></label>
    <label class="field"><span>Theme</span><select name="theme">${opt(THEMES, "modern")}</select></label>
    <label class="field"><span>End-card call to action</span><input name="cta" maxlength="40" placeholder="Shop the planner"></label></div>
    <label class="field"><span>Scenes — one per line: <span class="mono">on-screen text | spoken narration</span> (2–8 lines; scenes show your product images)</span><textarea name="scenes" rows="5" class="mono" placeholder="Planning a wedding? | Planning a wedding feels overwhelming.&#10;Everything in one place | This printable kit keeps every detail in one place.&#10;Print it tonight | Download it, print it, and start planning tonight."></textarea></label>
    <div class="checks"><label><input type="checkbox" name="voiceover" checked> Voiceover</label></div>`,
    params: (v) => ({ title: v.title, format: v.format, theme: v.theme, cta: v.cta, voiceover: v.voiceover,
      scenes: v.scenes.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => { const [cap, nar] = l.split("|"); return { caption: (cap || "").trim().slice(0, 110), narration: (nar || "").trim().slice(0, 350), visual: "product", seconds: 4 }; }) }) },
  image: { tool: "image_studio", label: "🖼 Image", needsProject: true, fields: () => `
    <div class="form-grid"><label class="field"><span>Format</span><select name="format">${opt(["square", "portrait", "story", "pin", "landscape", "listing", "thumbnail"], "square")}</select></label>
    <label class="field"><span>Layout</span><select name="layout"><option value="product">product image</option><option value="photo">AI photo + text</option><option value="headline">headline card</option><option value="quote">quote card</option></select></label>
    <label class="field"><span>Theme</span><select name="theme">${opt(THEMES, "modern")}</select></label>
    <label class="field"><span>Variants</span><input name="variants" type="number" min="1" max="3" value="1"></label></div>
    <label class="field"><span>Headline</span><input name="headline" maxlength="90"></label>
    <label class="field"><span>Subline</span><input name="subline" maxlength="160"></label>
    <label class="field"><span>Call to action</span><input name="cta" maxlength="28" placeholder="Shop now"></label>
    <label class="field"><span>AI photo (optional) — describe it, no text in the photo</span><textarea name="photo_prompt" rows="2" maxlength="400" placeholder="cozy desk with a printed planner, coffee and a plant, morning light"></textarea></label>`,
    params: (v) => ({ purpose: v.headline || "marketing image", format: v.format, layout: v.layout, headline: v.headline, subline: v.subline, cta: v.cta, photo_prompt: v.photo_prompt, theme: v.theme, variants: v.variants || 1 }) },
};
function assetCard(a) {
  const thumb = a.has_thumb ? `<img class="asset-thumb" src="/api/assets/${esc(a.id)}/thumb" alt="" loading="lazy">` : `<div class="asset-thumb asset-icon">${KIND_ICON[a.kind] || "📁"}</div>`;
  return `<div class="asset-card" data-asset="${esc(a.id)}">${thumb}
    <div class="asset-body"><div class="asset-title">${esc(trunc(a.title, 70))}</div>
    <div class="muted">${badge(a.kind, "dept")} ${badge(a.status, a.status)} · ${kb(a.bytes)} · ${when(a.ts)}</div>
    ${a.project_name ? `<div class="muted">${esc(trunc(a.project_name, 40))}</div>` : ""}</div></div>`;
}
async function openAsset(id) {
  let a;
  try { a = await api(`/api/assets/${id}`); } catch (e) { toast(esc(e.message), "error"); return; }
  const file = `/api/assets/${esc(a.id)}/file`;
  let preview = "";
  if (a.mime.startsWith("image/")) preview = `<img class="preview-img" src="${file}" alt="">`;
  else if (a.mime.startsWith("video/")) preview = `<video class="preview-img" controls preload="metadata" ${a.has_thumb ? `poster="/api/assets/${esc(a.id)}/thumb"` : ""} src="${file}"></video>`;
  else if (a.mime.startsWith("audio/")) preview = `<audio controls src="${file}"></audio>`;
  else if (a.mime.startsWith("text/") || /\.(md|csv|srt)$/.test(a.path)) {
    const t = await api(`/api/assets/${a.id}/text`).catch(() => ({ text: "(could not load)" }));
    preview = `<pre class="block">${esc(t.text)}</pre>`;
  } else if (a.has_thumb) preview = `<img class="preview-img" src="/api/assets/${esc(a.id)}/thumb" alt="">`;
  const m = a.meta || {};
  const warn = (m.warnings || m.notes || []).length ? `<h4>Review notes</h4><ul>${(m.warnings || m.notes).map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
  const listing = m.listing ? `<h4>Listing</h4><p><b>${esc(m.listing.title)}</b></p><p class="muted">EUR ${Number(m.listing.price_eur).toFixed(2)} · ${esc((m.listing.tags || []).join(", "))}</p>` : "";
  const acts = [];
  if (a.status !== "APPROVED") acts.push({ label: "✔ Approve", cls: "success", onClick: async () => { await api(`/api/assets/${a.id}/decide`, { method: "POST", body: { approve: true } }); toast("Approved", "success"); refresh(); } });
  if (a.status !== "REJECTED") acts.push({ label: "Reject", cls: "danger", onClick: async () => { await api(`/api/assets/${a.id}/decide`, { method: "POST", body: { approve: false } }); toast("Rejected"); refresh(); } });
  acts.push({ label: "Close", cls: "ghost", onClick: () => {} });
  modal({ title: `${esc(KIND_ICON[a.kind] || "")} ${esc(trunc(a.title, 80))}`, wide: true, actions: acts, body: `
    <div class="btn-row mb8"><a class="btn small primary" href="${file}?download=true">⬇ Download</a><a class="btn small ghost" href="${file}" target="_blank" rel="noopener">Open in new tab</a></div>
    ${preview}
    <dl class="kv mt16"><dt>Status</dt><dd>${badge(a.status, a.status)}${a.decided_by ? ` <span class="muted">by ${esc(a.decided_by)} ${when(a.decided_at)}</span>` : ""}</dd>
      <dt>Kind</dt><dd>${esc(a.kind)} · ${esc(a.mime)} · ${kb(a.bytes)}</dd><dt>Made by</dt><dd>${esc(a.source)}${m.photo_provider ? ` (AI photo: ${esc(m.photo_provider)})` : ""}</dd>
      <dt>File</dt><dd class="mono">workspace/${esc(a.path)}</dd><dt>SHA-256</dt><dd class="mono">${esc(a.sha256.slice(0, 16))}…</dd>
      ${a.task_id ? `<dt>Task</dt><dd class="mono">${esc(a.task_id)}</dd>` : ""}</dl>
    ${listing}${warn}` });
}
VIEWS.studio = {
  tab: "create", form: "product", project: "", filters: { project: "", kind: "", status: "DRAFT" },
  async render(isRefresh) {
    if (isRefresh && this.tab === "create") return;       // never wipe a half-filled form
    const s = await api("/api/studio");
    const drafts = (await api(`/api/assets?status=DRAFT&limit=1000`)).length;
    const tool = (n) => s.tools.find((t) => t.name === n) || {};
    const photo = (s.image_providers || []).find((p) => p.available);
    const tabs = [["create", "Create"], ["files", `Files${drafts ? ` (${drafts} to review)` : ""}`], ["sources", "Web sources"]];
    let body = "";
    if (this.tab === "create") {
      const f = STUDIO_FORMS[this.form];
      const t = tool(f.tool);
      body = `<div class="card"><h3>What should we make? <span class="muted">AI drafts, code builds the files · everything lands in Files as DRAFT for you to approve</span></h3>
        <div class="tabs">${Object.entries(STUDIO_FORMS).map(([k, x]) => `<button data-form="${k}" class="${k === this.form ? "active" : ""}">${x.label}</button>`).join("")}</div>
        <label class="field"><span>Project${f.needsProject ? "" : " (optional)"}</span><select name="project_id"><option value="">${f.needsProject ? "— choose a project —" : "— none —"}</option>${s.projects.filter((p) => p.status === "ACTIVE" || p.status === "PAUSED").map((p) => `<option value="${esc(p.id)}" ${p.id === this.project ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
        <div id="studio-fields">${f.fields()}</div>
        <div class="btn-row mt8"><button class="btn primary" id="studio-go" ${t.available ? "" : "disabled"}>Start</button>
        <span class="muted">${t.available ? `${t.calls_today}/${t.max_calls_per_day} runs today` : `unavailable: ${esc(t.reason || "")}`}</span></div></div>
        <div class="grid two mt16">
          <div class="card"><h3>Engines</h3><dl class="kv">
            <dt>AI photos</dt><dd>${photo ? badge(photo.provider, "ok") : badge("none — designed backgrounds only", "WAITING")}</dd>
            <dt>Voiceover</dt><dd>${s.voice && s.voice.available ? badge("ready", "ok") : badge((s.voice && s.voice.reason) || "off", "WAITING")}</dd>
            <dt>Web search</dt><dd>${s.search_provider ? badge(s.search_provider, "ok") : badge("none", "FAILED")}</dd>
            <dt>Video encoder</dt><dd>${s.engines.ffmpeg.path ? badge("ffmpeg", "ok") : badge("missing", "FAILED")}</dd>
            <dt>Fonts</dt><dd>${s.engines.fonts.installed}/${s.engines.fonts.expected} open-licence fonts</dd>
            <dt>Brand name</dt><dd>${s.brand_name ? esc(s.brand_name) : `<span class="muted">not set (Configuration → company.json)</span>`}</dd></dl>
            <button class="btn small ghost" id="backup-now">Back up database now</button></div>
          <div class="card"><h3>Recent studio jobs</h3>${(s.jobs || []).length ? `<div class="list">${s.jobs.slice(0, 8).map((j) => `<div class="item"><div class="item-head"><span>${badge(j.status, j.status)} ${esc(j.label)}</span><span class="muted">${when(j.created)}</span></div>${j.error ? `<p class="error-text">${esc(trunc(j.error, 220))}</p>` : j.result ? `<p class="muted">${(j.result.assets || []).length} file(s) · EUR ${Number(j.result.cost_eur || 0).toFixed(2)}</p>` : ""}</div>`).join("")}</div>` : `<div class="empty">No studio jobs yet.</div>`}</div></div>`;
    } else if (this.tab === "files") {
      const q = new URLSearchParams({ limit: 300 }); const fl = this.filters;
      if (fl.project) q.set("project_id", fl.project); if (fl.kind) q.set("kind", fl.kind); if (fl.status) q.set("status", fl.status);
      const assets = await api(`/api/assets?${q}`);
      const kinds = ["product_pdf", "bundle", "mockup", "preview", "listing", "image", "video", "captions", "report", "copy", "pack"];
      body = `<div class="toolbar">
          <select id="fa-project"><option value="">All projects</option>${s.projects.map((p) => `<option value="${esc(p.id)}" ${p.id === fl.project ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
          <select id="fa-kind"><option value="">All kinds</option>${kinds.map((k) => `<option ${k === fl.kind ? "selected" : ""}>${k}</option>`).join("")}</select>
          <select id="fa-status"><option value="">Any status</option>${["DRAFT", "APPROVED", "REJECTED"].map((k) => `<option ${k === fl.status ? "selected" : ""}>${k}</option>`).join("")}</select>
          <span class="muted">${assets.length} file(s)</span></div>
        ${assets.length ? `<div class="gallery">${assets.map(assetCard).join("")}</div>` : `<div class="empty">No files here yet. Create something in the Create tab, or let the work loop run tasks with tool capabilities.</div>`}`;
    } else {
      const src = await api("/api/web_sources?limit=200");
      body = `<div class="card"><h3>Web sources <span class="muted">every page the research tool found, read or skipped — with retrieval time</span></h3>
        ${src.length ? `<div class="table-wrap"><table><tr><th>When</th><th>Status</th><th>Source</th><th>Query</th><th>Chars</th></tr>
        ${src.map((r) => `<tr><td>${when(r.ts)}</td><td>${badge(r.status.split(":")[0], r.status === "read" ? "ok" : r.status === "snippet" ? "PENDING" : "WAITING")}${r.status.includes(":") ? ` <span class="muted">${esc(r.status.split(":").slice(1).join(":"))}</span>` : ""}</td>
          <td><b>${esc(trunc(r.title || r.domain, 70))}</b><br><span class="mono muted">${esc(trunc(r.url, 90))}</span></td><td>${esc(r.query)}</td><td>${r.chars}</td></tr>`).join("")}</table></div>` : `<div class="empty">No web research yet.</div>`}</div>`;
    }
    $("#view").innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button data-stab="${k}" class="${k === this.tab ? "active" : ""}">${esc(l)}</button>`).join("")}</div>${body}`;
    $$("[data-stab]").forEach((b) => b.addEventListener("click", () => { this.tab = b.dataset.stab; this.render(); }));
    $$("[data-form]").forEach((b) => b.addEventListener("click", () => { this.form = b.dataset.form; this.render(); }));
    const ps = $("select[name=project_id]"); if (ps) ps.addEventListener("change", () => { this.project = ps.value; });
    const go = $("#studio-go");
    if (go) go.addEventListener("click", async () => {
      const f = STUDIO_FORMS[this.form], v = formValues($("#view"));
      if (f.needsProject && !v.project_id) return toast("Choose a project first", "error");
      go.disabled = true;
      try {
        const job = await api(`/api/studio/${f.tool}`, { method: "POST", body: { project_id: v.project_id, params: f.params(v) } });
        toast(`⏳ Started: <b>${esc(job.label)}</b> — this can take several minutes`, "info", 5000);
        trackJob(job, (j) => { toast(`✔ <b>${esc(j.label)}</b> done: ${(j.result.assets || []).length} file(s)`, "success", 7000); if (App.view === "studio") { this.tab = "files"; this.filters.status = "DRAFT"; this.render(); } poll(); },
          (j) => toast(`✖ <b>${esc(j.label)}</b> failed: ${esc(trunc(j.error, 300))}`, "error", 12000));
        setTimeout(() => this.render(), 800);
      } catch (e) { toast(esc(e.message), "error"); go.disabled = false; }
    });
    for (const [id, key] of [["#fa-project", "project"], ["#fa-kind", "kind"], ["#fa-status", "status"]]) {
      const el = $(id); if (el) el.addEventListener("change", () => { this.filters[key] = el.value; this.render(); });
    }
    $$("[data-asset]").forEach((c) => c.addEventListener("click", () => openAsset(c.dataset.asset)));
    const bk = $("#backup-now");
    if (bk) bk.addEventListener("click", async () => {
      try { const r = await api("/api/backup", { method: "POST" }); toast(`✔ Backup saved: <span class="mono">${esc(r.file)}</span>`, "success", 7000); }
      catch (e) { toast(esc(e.message), "error"); } });
  },
};

// ---------- Settings: the common switches as a form, owner guidelines, advanced config files
VIEWS.settings = {
  tab: "general",
  async render() {
    const tabs = [["general", "General"], ["guidelines", "Your guidelines"], ["advanced", "Advanced (config files)"]];
    $("#view").innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button data-stab="${k}" class="${k === this.tab ? "active" : ""}">${esc(l)}</button>`).join("")}</div><div id="settings-body"></div>`;
    $$("[data-stab]").forEach((b) => b.addEventListener("click", () => { this.tab = b.dataset.stab; this.render(); }));
    const box = $("#settings-body");
    if (this.tab === "general") return this.general(box);
    if (this.tab === "guidelines") return this.guidelines(box);
    return this.advanced(box);
  },

  async general(box) {
    const [co, to] = await Promise.all([api("/api/config/company.json"), api("/api/config/tools.json")]);
    const company = JSON.parse(co.text), tools = JSON.parse(to.text);
    const web = tools.tools.web_research?.settings || {}, img = tools.tools.image_studio?.settings || {};
    const voice = tools.tools.video_studio?.settings?.voice || {};
    const num = (name, label, value, step = "1", hint = "") => `<label class="field"><span>${label}</span><input name="${name}" type="number" min="0" step="${step}" value="${esc(value)}">${hint ? `<small class="muted">${hint}</small>` : ""}</label>`;
    const chk = (name, label, on, hint = "") => `<label class="switch"><input type="checkbox" name="${name}" ${on ? "checked" : ""}> <span><b>${label}</b>${hint ? `<br><small class="muted">${hint}</small>` : ""}</span></label>`;
    const sel = (name, label, value, options, hint = "") => `<label class="field"><span>${label}</span><select name="${name}">${options.map(([v, l]) => `<option value="${v}" ${v === value ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>${hint ? `<small class="muted">${hint}</small>` : ""}</label>`;
    box.innerHTML = `
      <div class="grid two">
        <div class="card"><h3>Company</h3>
          <label class="field"><span>Brand name</span><input name="brand_name" maxlength="60" value="${esc(company.brand_name || "")}" placeholder="Shown on products and graphics"></label>
          <div class="form-grid">
            ${num("total_budget_eur", "Total budget (EUR)", company.total_budget_eur, "1")}
            ${num("max_active_projects", "Max active projects", company.max_active_projects)}
          </div></div>
        <div class="card"><h3>Orchestrator</h3>
          ${chk("autopilot", "Autopilot", company.autopilot, "The orchestrator creates, plans and runs projects itself. Money, publishing and finishing projects still wait for you.")}
          <div class="form-grid">
            ${num("autopilot_max_project_budget_eur", "Budget it may use without asking (EUR)", company.autopilot_max_project_budget_eur, "1")}
            ${num("max_stages_per_project", "Stages per project before it finishes", company.max_stages_per_project)}
          </div></div>
        <div class="card"><h3>Power</h3>
          ${chk("keep_awake_while_working", "Keep the PC awake only while working", company.keep_awake_while_working, "Normal Windows sleep when there is nothing to do.")}
          ${num("keep_awake_grace_minutes", "Stay awake after the last work (minutes)", company.keep_awake_grace_minutes, "1")}</div>
        <div class="card"><h3>Engines</h3>
          ${sel("search", "Web search", web.provider || "auto", [["auto", "Automatic (best available)"], ["tavily", "Tavily (needs TAVILY_API_KEY)"], ["brave", "Brave (needs BRAVE_API_KEY)"], ["duckduckgo", "DuckDuckGo (no key, best effort)"]])}
          ${chk("local_photos", "AI photos on this PC (Stable Diffusion)", img.sdcpp ? img.sdcpp.enabled !== false : true, "Free, about 1 minute per photo.")}
          ${sel("voice", "Video voiceover", voice.engine || "piper", [["piper", "Piper (free, on this PC)"], ["elevenlabs", "ElevenLabs (needs ELEVENLABS_API_KEY)"], ["none", "No voiceover"]])}
          <p class="muted">API keys are read from Windows environment variables, e.g. <span class="mono">setx TAVILY_API_KEY "..."</span>, then restart the dashboard.</p></div>
      </div>
      <div class="btn-row mt16"><button class="btn primary" id="settings-save">Save settings</button><span class="muted">Checked, backed up and applied immediately.</span></div>`;
    $("#settings-save").addEventListener("click", async () => {
      const v = formValues(box);
      Object.assign(company, { brand_name: v.brand_name, total_budget_eur: v.total_budget_eur, max_active_projects: v.max_active_projects,
        autopilot: v.autopilot, autopilot_max_project_budget_eur: v.autopilot_max_project_budget_eur,
        max_stages_per_project: v.max_stages_per_project, keep_awake_while_working: v.keep_awake_while_working,
        keep_awake_grace_minutes: v.keep_awake_grace_minutes });
      if (tools.tools.web_research) tools.tools.web_research.settings = { ...web, provider: v.search };
      if (tools.tools.image_studio) tools.tools.image_studio.settings = { ...img, sdcpp: { ...(img.sdcpp || {}), enabled: v.local_photos } };
      if (tools.tools.video_studio) tools.tools.video_studio.settings = { ...(tools.tools.video_studio.settings || {}), voice: { ...voice, engine: v.voice } };
      try {
        const a = await api("/api/config/company.json", { method: "PUT", body: { text: JSON.stringify(company, null, 2) + "\n" } });
        const b = await api("/api/config/tools.json", { method: "PUT", body: { text: JSON.stringify(tools, null, 2) + "\n" } });
        toast(a.changed || b.changed ? "Settings saved and applied ✔" : "No changes", "success");
        refresh();
      } catch (e) { toast(esc(e.message), "error", 9000); }
    });
  },

  async guidelines(box) {
    const d = await api("/api/memory");
    const items = d.items.filter((i) => i.status === "active" && i.scope === "personal");
    box.innerHTML = `
      <div class="card"><h3>Your guidelines <span class="muted">the orchestrator and every task follow these</span></h3>
        <p class="muted">You can also tell the orchestrator “remember that …” — it asks you to confirm before saving.</p>
        <label class="field"><span>New guideline</span><textarea name="content" rows="2" placeholder="e.g. Prefer digital products that need under 2 hours of my time per week"></textarea></label>
        <button class="btn primary" id="add-guideline">Add</button></div>
      <div class="list mt16">${items.map((i) => `<div class="item"><div class="item-head"><span>${esc(i.content)}</span>
        <button class="btn small ghost" data-retire="${esc(i.id)}">Remove</button></div><p class="muted">${when(i.updated_at)}</p></div>`).join("") || `<div class="empty">No guidelines yet.</div>`}</div>`;
    $("#add-guideline").addEventListener("click", async () => {
      const v = formValues(box);
      try { await api("/api/memory/guideline", { method: "POST", body: { content: v.content, kind: "preference" } }); toast("Guideline added", "success"); this.render(); }
      catch (e) { toast(esc(e.message), "error"); }
    });
    $$("[data-retire]", box).forEach((b) => b.addEventListener("click", async () => {
      await api(`/api/memory/${b.dataset.retire}/retire`, { method: "POST" }); toast("Removed"); this.render(); }));
  },

  async advanced(box) {
    const files = await api("/api/config");
    const cur = App.configFile;
    const d = await api(`/api/config/${cur}`);
    box.innerHTML = `
      <p class="muted">Every setting of the system, as JSON. Each save is validated against its schema and the other files, backed up, logged with a diff, and applied immediately.</p>
      <div class="config-layout">
        <div class="config-side">
          <div class="file-list">${files.map((f) => `<button data-file="${esc(f.name)}" class="${f.name === cur ? "active" : ""}"><span class="mono">${esc(f.name)}</span><small>${esc(f.description)}</small></button>`).join("")}</div>
          <div class="config-history">
            <h4 class="section-title">History of ${esc(cur)}</h4>
            ${d.history.length ? d.history.slice(0, 15).map((h) => `<div class="item"><div class="item-head"><span class="mono">${esc(h.saved)}</span><span class="btn-row"><button class="btn small ghost" data-view-version="${esc(h.version)}">View</button><button class="btn small" data-restore="${esc(h.version)}">Restore</button></span></div></div>`).join("") : `<div class="empty">No earlier versions.</div>`}
          </div>
        </div>
        <div class="card">
          <h3><span class="mono">${esc(cur)}</span><span class="btn-row"><button class="btn small ghost" id="cfg-reload">Revert</button><button class="btn small" id="cfg-validate">Validate</button><button class="btn small primary" id="cfg-save">Save (Ctrl+S)</button></span></h3>
          <textarea id="cfg-text" class="editor" spellcheck="false"></textarea>
          <div id="cfg-msg"></div>
        </div>
      </div>`;
    const ta = $("#cfg-text"); ta.value = d.text;
    const msg = (problems) => { $("#cfg-msg").innerHTML = problems.length ? `<ul class="problems">${problems.map((p) => `<li>✖ ${esc(p)}</li>`).join("")}</ul>` : `<div class="valid-msg">✔ Valid</div>`; };
    const validate = async () => { const r = await api(`/api/config/${cur}/validate`, { method: "POST", body: { text: ta.value } }); msg(r.problems); return r.problems; };
    const save = async () => {
      if ((await validate()).length) return toast("Not saved — fix the problems first", "error");
      if (!(await confirmBox(`Save <span class="mono">${esc(cur)}</span>? The running system reloads its configuration.`, { label: "Save", cls: "primary" }))) return;
      try {
        const r = await api(`/api/config/${cur}`, { method: "PUT", body: { text: ta.value } });
        if (!r.changed) return toast("No changes");
        toast("Saved and applied ✔", "success");
        modal({ title: "Saved — what changed", wide: true, body: `<pre class="block">${r.diff.split("\n").map((l) => `<span class="${l.startsWith("+") ? "diff-add" : l.startsWith("-") ? "diff-del" : ""}">${esc(l)}</span>`).join("\n")}</pre>` });
        this.render();
      } catch (e) { toast(esc(e.message), "error"); }
    };
    $$("[data-file]", box).forEach((b) => b.addEventListener("click", () => { App.configFile = b.dataset.file; this.render(); }));
    $("#cfg-validate").addEventListener("click", validate);
    $("#cfg-save").addEventListener("click", save);
    $("#cfg-reload").addEventListener("click", () => this.render());
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Tab") { e.preventDefault(); const s = ta.selectionStart; ta.setRangeText("  ", s, ta.selectionEnd, "end"); }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
    });
    $$("[data-view-version]", box).forEach((b) => b.addEventListener("click", async () => {
      const v = await api(`/api/config/${cur}/version/${b.dataset.viewVersion}`);
      modal({ title: `${esc(cur)} · ${esc(b.dataset.viewVersion)}`, wide: true, body: `<pre class="block">${esc(v.text)}</pre>` }); }));
    $$("[data-restore]", box).forEach((b) => b.addEventListener("click", async () => {
      if (!(await confirmBox(`Restore ${esc(cur)} to version ${esc(b.dataset.restore)}? The current version is backed up first.`, { label: "Restore", cls: "warn" }))) return;
      try { await api(`/api/config/${cur}/restore`, { method: "POST", body: { version: b.dataset.restore } }); toast("Restored ✔", "success"); this.render(); }
      catch (e) { toast(esc(e.message), "error"); } }));
  },
};

// ---------- Activity log
VIEWS.log = {
  live: true,
  async render() {
    const f = App.filters;
    const q = new URLSearchParams({ limit: 300 }); if (f.logActor) q.set("actor", f.logActor); if (f.logProject) q.set("project_id", f.logProject);
    let events = await api(`/api/events?${q}`).catch(() => []);
    if (f.logSearch) { const s = f.logSearch.toLowerCase(); events = events.filter((e) => (e.action + JSON.stringify(e.details)).toLowerCase().includes(s)); }
    const actors = ["MASTER_ORCHESTRATOR", "PROJECT_MANAGER", "WORKER", "PERMISSION_GATE", "EXPERIMENT_MANAGER", "LEARNING", "RUNNER", "HUMAN"];
    const view = $("#view");
    const focused = document.activeElement && document.activeElement.id === "f-search";
    view.innerHTML = `
      <div class="toolbar">
        <select id="f-actor"><option value="">All actors</option>${actors.map((a) => `<option ${a === f.logActor ? "selected" : ""}>${a}</option>`).join("")}</select>
        <select id="f-lproject"><option value="">All projects</option>${(App.state?.projects || []).map((p) => `<option value="${esc(p.id)}" ${p.id === f.logProject ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
        <input id="f-search" placeholder="Search…" value="${esc(f.logSearch)}">
        <span class="muted">${events.length} events · live</span>
      </div>
      <div class="card">${events.map((e) => `<div class="event"><div class="muted mono">${clock(e.ts)}</div><div class="actor ${esc(e.actor)}">${esc(e.actor)}</div>
        <div><b>${esc(e.action)}</b>${e.project_id ? ` <span class="muted">· ${esc(projectName(e.project_id))}</span>` : ""}<div class="details">${details(e.details, 400)}</div></div></div>`).join("") || `<div class="empty">No events.</div>`}</div>`;
    $("#f-actor").addEventListener("change", (e) => { f.logActor = e.target.value; this.render(); });
    $("#f-lproject").addEventListener("change", (e) => { f.logProject = e.target.value; this.render(); });
    const s = $("#f-search");
    s.addEventListener("input", () => { f.logSearch = s.value; clearTimeout(this.t); this.t = setTimeout(() => this.render(), 300); });
    if (focused) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); }
  },
};

// ================================================================ boot
async function boot() {
  try { App.state = await api("/api/state"); }
  catch { return; }
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  renderTopbar(App.state);
  App.lastEventId = App.state.last_event_id;
  go((location.hash || "#fleet").slice(1));
  clearInterval(App.timer);
  App.timer = setInterval(() => { if (!document.hidden) poll(); }, 2000);
}
boot();
