const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (x, cur = "USD") => (x == null ? "—" : (cur === "USD" || !cur ? "$" : cur + " ") + Number(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `Request failed (${r.status})`);
  return r.json();
};
// Times are stored in UTC and shown in the viewer's own timezone.
const when = (s) => new Date(s).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const span = (ms) => { const m = Math.max(0, Math.round(ms / 60000)); return m < 60 ? `${m} min` : m < 2880 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.round(m / 1440)} days`; };
// How long a hold has been waiting, and when it escalates.
function waiting(run) {
  const age = Date.now() - new Date(run.created_at).getTime(), left = (state.slaHours || 48) * 3600000 - age;
  return { age: span(age), overdue: left <= 0, left: span(left) };
}
const touchedBy = (run) => (run.corrections || []).filter((e) => (e.kind || "correction") !== "recheck");
const cap = (t) => String(t).charAt(0).toUpperCase() + String(t).slice(1);
const OUTCOME = { APPROVED: "Approved", HOLD: "On hold", REJECTED: "Rejected" };
const STAGE_NAMES = ["Receive document", "Read invoice", "Validate fields and arithmetic", "Verify vendor", "Check for duplicates", "Match purchase order", "3-way match: PO balance and goods received", "Decide and route"];
const ICON = { ok: "✓", warn: "!", fail: "✕", skipped: "–", pending: "", running: "" };
const svg = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const SVG = {
  doc: svg(`<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/>`),
  bolt: svg(`<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>`),
  clock: svg(`<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>`),
  check: svg(`<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>`),
  shield: svg(`<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>`),
  play: `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg>`,
};

const state = { view: "process", filter: "all", runs: [], liveSource: null, toggled: {}, timer: null };

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (t.hidden = true), 3200);
}

/* ---------- navigation ---------- */
function show(view) {
  state.view = view;
  for (const b of $("#tabs").children) b.classList.toggle("active", b.dataset.view === view);
  for (const v of ["process", "dashboard", "reference"]) $(`#view-${v}`).hidden = v !== view;
  if (view === "dashboard") loadDashboard();
  if (view === "reference") loadReference();
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) show(b.dataset.view);
});

/* ---------- run rendering (shared by live view and drawer) ---------- */
function outcomeChip(run) {
  if (run.status === "running") return `<span class="chip running">Running</span>`;
  const ev = run.corrections || [];
  const reviewed = run.review ? ` after review` : touchedBy(run).length ? (touchedBy(run).some((e) => e.kind === "answer") ? ` after an answer` : ` after correction`) : ev.length && run.final_state === "APPROVED" ? ` · resumed by itself` : "";
  return `<span class="chip ${run.final_state}">${OUTCOME[run.final_state] || run.final_state}${reviewed}</span>`;
}

function eventsHtml(run) {
  return (run.corrections || []).map((c) => {
    const kind = c.kind || "correction";
    if (kind === "recheck") return `<div class="reviewed"><b>Re-checked automatically</b> on ${when(c.at)}: ${esc(c.note)}.<br><span class="muted">The data changed, so the process ran the rules again by itself. Nobody had to chase this invoice.</span></div>`;
    if (kind === "answer") return `<div class="reviewed"><b>${esc(c.by)} answered</b> on ${when(c.at)}: “${esc(c.note)}”<br><span class="muted">The answer was remembered, and the rules were run again.</span></div>`;
    return `<div class="reviewed"><b>Fields corrected by ${esc(c.by)}</b> on ${when(c.at)}: ${c.changes.map((x) => `${esc(x.field)} ${esc(x.from ?? "nothing")} → ${esc(x.to ?? "nothing")}`).join(", ")}. “${esc(c.note)}”<br><span class="muted">The rules were run again on the corrected fields. The original read is kept in this record.</span></div>`;
  }).join("");
}

function decisionCard(run) {
  const res = run.result;
  if (!res) return "";
  const cur = run.currency;
  const open = run.final_state === "HOLD" && !run.review;
  const ask = open ? res.ask : null;
  const label = { APPROVED: "APPROVED · CLEARED FOR PAYMENT", HOLD: ask ? `ON HOLD · A QUESTION FOR ${esc((ask.to || run.owner || "a person").toUpperCase())}` : "ON HOLD · NEEDS A PERSON", REJECTED: "REJECTED · NOT ENTERED FOR PAYMENT" }[res.decision];
  let h = `<div class="decision ${res.decision}"><div class="decision-banner"><div class="decision-icon">${{ APPROVED: "✓", HOLD: ask ? "?" : "!", REJECTED: "✕" }[res.decision]}</div>
    <div><div class="decision-label">${label}</div><div class="decision-headline">${esc(run.headline)}</div></div></div>`;

  const facts = [
    ["Vendor", run.vendor_name], ["Invoice", run.invoice_number], ["Dated", run.invoice_date], ["PO", run.po_number],
    ["Before tax", run.net_amount != null ? money(run.net_amount, cur) : null], ["Tax", run.tax_amount != null ? money(run.tax_amount, cur) : null],
    ["Total", run.total_amount != null ? money(run.total_amount, cur) : null], ["Owner", run.owner],
  ].filter(([, v]) => v);
  if (facts.length) h += `<div class="facts">${facts.map(([k, v]) => `<span><span class="muted">${k}</span> <b>${esc(v)}</b></span>`).join("")}</div>`;
  h += `<div class="decision-body">`;

  // A hold is a question, not a pile of evidence: ask it, recommend an answer, offer the answers.
  if (ask) {
    const w = waiting(run);
    h += `<div class="ask"><div class="ask-q">${esc(ask.question)}</div>
      ${ask.recommend ? `<div class="ask-rec"><b>Recommended:</b> ${esc(ask.recommend)}</div>` : ""}
      <div class="review-row"><input data-ask-name placeholder="Your name (kept with the answer)" maxlength="60" style="max-width:280px"></div>
      <div class="review-row">${ask.options.map((o, i) => `<button class="btn ${i === 0 ? "" : "ghost"}" data-answer="${i}" data-action="${o.action}" data-run="${run.id}" data-po="${esc(run.po_number || "")}">${esc(o.label)}</button>`).join("")}</div>
      <div class="ask-sla ${w.overdue ? "overdue" : ""}">Waiting ${w.age} · ${w.overdue ? "overdue, escalated to the AP manager" : `escalates to the AP manager in ${w.left}`}</div></div>`;
  }

  if (res.reasons?.length) {
    h += `<div class="sub">Why</div><ul>${res.reasons.map((f) => `<li><b>${esc(f.title)}.</b> ${esc(f.message)}</li>`).join("")}</ul>`;
  }
  if (res.related_run) h += `<div style="margin:-2px 0 6px 18px"><a class="link" href="/api/runs/${res.related_run}/file" target="_blank" rel="noopener">Open the earlier invoice (run ${res.related_run}) to compare</a> · <a class="link" href="#" data-open-run="${res.related_run}">see how it was decided</a></div>`;
  if (res.passed?.length) h += `<div class="sub">Why</div><ul>${res.passed.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`;
  if (res.notes?.length) h += `<div class="sub">Worth knowing</div><ul>${res.notes.map((f) => `<li><b>${esc(f.title)}.</b> ${esc(f.message)}</li>`).join("")}</ul>`;

  h += `<div class="next"><div class="next-icon">→</div><div><div class="next-title">What happens next</div><div>${esc(res.next_action || "")}</div>
    ${res.po_impact ? `<div class="muted">${esc(res.po_impact)}</div>` : ""}</div></div>`;

  for (const m of (res.messages || []).filter((m) => m.kind === "vendor")) {
    h += `<div class="message"><div class="message-head"><span>Draft reply to vendor (not sent, a person sends it) · To: <b>${esc(m.to)}</b></span>
      <button class="btn ghost small" data-copy="${esc(m.subject + "\n\n" + m.body)}">Copy</button></div>
      <div style="font-weight:600;margin-top:6px">${esc(m.subject)}</div><pre>${esc(m.body)}</pre></div>`;
  }

  h += eventsHtml(run);
  if (run.review) {
    const r = run.review;
    h += `<div class="reviewed"><b>${r.action === "approve" ? "Approved" : "Rejected"} by ${esc(r.reviewer)}</b> on ${when(r.at)}${r.note ? `: “${esc(r.note)}”` : ""}<br><span class="muted">The process had put this on hold. A person made the final call, and both are kept for audit.</span></div>`;
  }
  if (!run.review && (run.final_state === "HOLD" || run.final_state === "REJECTED")) {
    const x = run.extraction || {};
    const f = (k, label, type = "text") => `<label>${label}<input data-fix="${k}" type="${type}" ${type === "number" ? 'step="0.01"' : ""} value="${esc(x[k] ?? "")}"></label>`;
    h += `<details class="fix" data-fixbox><summary>Something read wrongly or missing? Correct the fields and re-check</summary>
      <div class="fix-grid">${f("vendor_name", "Vendor name")}${f("invoice_number", "Invoice number")}${f("invoice_date", "Invoice date (YYYY-MM-DD)")}${f("po_number", "PO number")}${f("subtotal", "Subtotal", "number")}${f("tax_amount", "Tax", "number")}${f("total", "Total", "number")}</div>
      <div class="review-row"><input data-fix-name placeholder="Your name" maxlength="60"><input data-fix-note placeholder="Why you are changing it (kept for audit)" maxlength="300" style="flex:2"></div>
      <div class="review-row"><button class="btn" data-rerun="${run.id}">Re-check with these fields</button><span class="muted" style="align-self:center;font-size:13px">The document is not re-read. The same rules decide again.</span></div></details>`;
  }
  if (open) {
    h += `<details class="fix"><summary>Decide differently from the answers above</summary><div class="review" style="border:0;padding:0;margin-top:8px">
      <div class="review-row"><input id="rv-name" placeholder="Your name" maxlength="60"><input id="rv-note" placeholder="Reason (kept for audit)" maxlength="300" style="flex:2"></div>
      <div class="review-row"><button class="btn good" data-review="approve" data-run="${run.id}">Approve for payment</button><button class="btn danger" data-review="reject" data-run="${run.id}">Reject invoice</button></div></div></details>`;
  }
  return h + `</div></div>`;
}

// While a run is in flight the server has only recorded the stages reached so far; show the whole pipeline, with the rest pending.
function allStages(run) {
  const got = run.stages || [];
  if (run.status !== "running" && got.length) return got;
  return STAGE_NAMES.map((t, i) => got.find((s) => s.seq === i + 1) || { seq: i + 1, title: t, status: "pending", details: [] });
}

// One segment per stage: the whole pipeline at a glance, filling in as it runs.
function progress(run) {
  const stages = allStages(run);
  const now = stages.find((s) => s.status === "running");
  const skipped = stages.filter((s) => s.status === "skipped").length, ran = stages.filter((s) => !["pending", "running", "skipped"].includes(s.status)).length;
  const text = run.status === "running" ? (now ? `Stage ${now.seq} of ${stages.length} · ${esc(now.title)}` : `Stage ${Math.min(ran + 1, stages.length)} of ${stages.length}`)
    : `${ran} of ${stages.length} stages ran${skipped ? ` · ${skipped} skipped` : ""}`;
  return `<div class="progress"><div class="progress-bar" style="grid-template-columns:repeat(${stages.length},1fr)">${stages.map((s) => `<i class="seg ${s.status}" title="${s.seq}. ${esc(s.title)}"></i>`).join("")}</div><span class="progress-text">${text}</span></div>`;
}

function timeline(run) {
  const toggled = (state.toggled[run.id] ||= {});
  const stages = allStages(run);
  const lastDone = Math.max(0, ...stages.filter((s) => !["pending", "running", "skipped"].includes(s.status)).map((s) => s.seq));
  return `<ol class="timeline">${stages.map((s) => {
    const auto = run.status === "running" ? s.seq === lastDone : s.status === "warn" || s.status === "fail";
    const open = s.details.length && (toggled[s.key] ?? auto);
    const summary = s.status === "running" ? "Working…" : s.summary || "";
    return `<li class="stage ${s.status}">
      <div class="dot">${ICON[s.status] ?? ""}</div>
      <div class="stage-head" data-toggle="${s.key}" data-run="${run.id}" data-open="${open ? 1 : 0}">
        <span class="stage-title">${s.seq}. ${esc(s.title)}</span>${s.details.length ? `<span class="caret" aria-hidden="true">›</span>` : ""}
        ${s.duration_ms != null ? `<span class="stage-ms">${s.duration_ms < 1000 ? s.duration_ms + " ms" : (s.duration_ms / 1000).toFixed(1) + " s"}</span>` : ""}
      </div>
      ${summary ? `<div class="stage-summary">${esc(summary)}</div>` : ""}
      ${open ? `<div class="details">${s.details.map((x) => `<div class="detail ${x.status || ""}"><div class="detail-label">${esc(x.label)}</div><div class="detail-value">${esc(x.value)}</div></div>`).join("")}</div>` : ""}
    </li>`;
  }).join("")}</ol>`;
}

function renderRun(run, el) {
  const fileUrl = `/api/runs/${run.id}/file`;
  const isPdf = run.mime === "application/pdf";
  // Keep the document frame across re-renders so the PDF does not reload on every stage update.
  let shell = el.querySelector(`[data-shell="${run.id}"]`);
  if (!shell) {
    el.innerHTML = `<div data-shell="${run.id}"><div class="run-head"></div><div class="run-body"><div class="run-main"></div>
      <div class="doc"><div class="doc-label"><span>Source document</span><a href="${fileUrl}" target="_blank" rel="noopener">Open in new tab</a></div>
      ${isPdf ? `<iframe src="${fileUrl}#toolbar=0&view=FitH" title="Invoice document"></iframe>` : `<img src="${fileUrl}" alt="Invoice document">`}</div></div></div>`;
    shell = el.firstElementChild;
  }
  const secs = run.duration_ms != null ? ` · ${(run.duration_ms / 1000).toFixed(1)} s end to end` : "";
  const reader = run.extractor ? ` · read by ${run.extractor === "gemini" ? "Gemini" : "offline parser"}` : "";
  $(".run-head", shell).innerHTML = `<div><div class="run-title">${esc(run.filename)}</div>
    <div class="run-meta">Run <span class="mono">${run.id}</span> · received ${when(run.created_at)}${secs}${reader}</div></div>${outcomeChip(run)}${progress(run)}`;
  const focus = document.activeElement?.id;
  const vals = { name: $("#rv-name", shell)?.value, note: $("#rv-note", shell)?.value };
  const why = run.stages?.find((x) => x.key === "extract")?.details.find((x) => x.label === "Why not the AI model")?.value;
  const degraded = run.extractor === "text-parser" && state.aiEnabled
    ? `<div class="strip"><b>The AI reader did not answer, so the offline parser read this invoice.</b> ${esc(why || "")} Digital PDFs still work; scanned invoices will be held until the reader is back.</div>` : "";
  if (run.status !== "running") {
    setPill(run.extractor);
    api("/api/health").then((h) => { state.resting = h.resting || []; if (run.extractor !== "text-parser") setPill(); }).catch(() => {});
  }
  $(".run-main", shell).innerHTML = degraded + (run.status !== "running" ? decisionCard(run) : "") + `<h3 style="margin-top:0">What the process did</h3>` + timeline(run);
  if (vals.name && $("#rv-name", shell)) $("#rv-name", shell).value = vals.name;
  if (vals.note && $("#rv-note", shell)) $("#rv-note", shell).value = vals.note;
  if (focus && document.getElementById(focus)) document.getElementById(focus).focus();
}

/* ---------- live run ---------- */
async function startRun(body) {
  if (state.liveSource) state.liveSource.close();
  $("#run-empty").hidden = true;
  const live = $("#run-live");
  live.innerHTML = "";
  setBusy(true);
  let id;
  try {
    ({ id } = await api("/api/runs", { method: "POST", body }));
  } catch (e) {
    setBusy(false);
    $("#run-empty").hidden = false;
    return toast(e.message);
  }
  show("process");
  state.liveRun = id;
  follow(id, live, () => setBusy(false));
}

// Stream a run's progress into el until it finishes.
function follow(id, el, onDone = () => {}) {
  if (state.liveSource) state.liveSource.close();
  const src = new EventSource(`/api/runs/${id}/events`);
  state.liveSource = src;
  const finish = () => { src.close(); onDone(); refreshBadge(); if (state.view === "dashboard") loadDashboard(); };
  src.onmessage = (ev) => {
    const run = JSON.parse(ev.data);
    renderRun(run, el);
    if (run.status !== "running") finish();
  };
  src.onerror = async () => {
    // Stream dropped: fall back to fetching the final state so the screen never hangs.
    src.close();
    for (let i = 0; i < 60; i++) {
      const run = await api(`/api/runs/${id}`).catch(() => null);
      if (run) renderRun(run, el);
      if (run && run.status !== "running") break;
      await new Promise((r) => setTimeout(r, 500));
    }
    finish();
  };
}
function setBusy(b) {
  document.querySelectorAll("[data-sample]").forEach((x) => (x.disabled = b));
}

const dz = $("#dropzone");
$("#file-input").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (f) upload(f);
  e.target.value = "";
});
["dragover", "dragenter"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) upload(f); });
function upload(file) {
  const fd = new FormData();
  fd.append("file", file);
  startRun(fd);
}

async function loadSamples() {
  const samples = await api("/api/samples");
  $("#samples").innerHTML = samples.map((s, i) => `<div class="sample"><span class="sample-n">${i + 1}</span><div class="sample-main">
      <div class="sample-title">${esc(s.title)}</div>
      <div class="sample-desc">${esc(s.description)}</div>
      <div class="sample-actions"><button class="btn small" data-sample="${esc(s.file)}">${SVG.play}Run</button><a href="/api/samples/${encodeURIComponent(s.file)}" target="_blank" rel="noopener">View PDF</a><span class="chip ${s.expected}" title="Expected outcome">${OUTCOME[s.expected] || ""}</span></div>
    </div></div>`).join("");
}

/* ---------- global click handling ---------- */
document.addEventListener("click", async (e) => {
  const t = e.target;
  const sample = t.closest("[data-sample]");
  if (sample) {
    const fd = new FormData();
    fd.append("sample", sample.dataset.sample);
    return startRun(fd);
  }
  const tog = t.closest("[data-toggle]");
  if (tog) {
    (state.toggled[tog.dataset.run] ||= {})[tog.dataset.toggle] = tog.dataset.open !== "1";
    const run = await api(`/api/runs/${tog.dataset.run}`);
    return renderRun(run, tog.closest("#drawer-body") || $("#run-live"));
  }
  const copy = t.closest("[data-copy]");
  if (copy) {
    await navigator.clipboard.writeText(copy.dataset.copy).catch(() => {});
    return toast("Copied to clipboard");
  }
  const openRun = t.closest("[data-open-run]");
  if (openRun) { e.preventDefault(); return openDrawer(openRun.dataset.openRun); }
  const ans = t.closest("[data-answer]");
  if (ans) {
    const el = ans.closest("#drawer-body") || $("#run-live"), action = ans.dataset.action;
    if (action === "fix") {
      const box = $("[data-fixbox]", el);
      box.open = true;
      box.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (action === "receipt") {
      closeDrawer();
      state.receiptPo = ans.dataset.po;
      show("reference");
      return toast("Record the receipt here. The held invoice will re-check itself.");
    }
    const reviewer = $("[data-ask-name]", ans.closest(".ask")).value.trim();
    if (!reviewer) return toast("Add your name, it is kept with the answer for audit");
    try {
      const run = await api(`/api/runs/${ans.dataset.run}/answer`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ index: +ans.dataset.answer, reviewer }) });
      if (action === "learn_alias") { toast("Remembered. Re-checking the invoice…"); follow(ans.dataset.run, el); }
      else { renderRun(run, el); toast(action === "approve" ? "Approved for payment" : "Invoice rejected"); refreshBadge(); if (state.view === "dashboard") loadDashboard(); }
    } catch (err) { toast(err.message); }
    return;
  }
  const rr = t.closest("[data-rerun]");
  if (rr) {
    const box = rr.closest(".fix"), el = rr.closest("#drawer-body") || $("#run-live");
    if (state.liveSource && state.liveRun !== rr.dataset.rerun) state.liveSource.close();
    const reviewer = $("[data-fix-name]", box).value.trim(), note = $("[data-fix-note]", box).value.trim();
    if (!reviewer || !note) return toast("Add your name and why you are changing it, both are kept for audit");
    const fields = {};
    box.querySelectorAll("[data-fix]").forEach((i) => (fields[i.dataset.fix] = i.value.trim() === "" ? null : i.value.trim()));
    try {
      await api(`/api/runs/${rr.dataset.rerun}/rerun`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fields, reviewer, note }) });
      follow(rr.dataset.rerun, el);
    } catch (err) { toast(err.message); }
    return;
  }
  const rv = t.closest("[data-review]");
  if (rv) {
    const box = rv.closest(".review");
    const reviewer = $("#rv-name", box).value.trim(), note = $("#rv-note", box).value.trim();
    if (!reviewer || !note) return toast("Add your name and a reason, both are kept for audit");
    try {
      const run = await api(`/api/runs/${rv.dataset.run}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: rv.dataset.review, reviewer, note }) });
      renderRun(run, rv.closest("#drawer-body") || $("#run-live"));
      if (state.liveRun === run.id && $("#run-live").firstElementChild) renderRun(run, $("#run-live"));
      toast(rv.dataset.review === "approve" ? "Approved for payment" : "Invoice rejected");
      refreshBadge();
      if (state.view === "dashboard") loadDashboard();
    } catch (err) { toast(err.message); }
    return;
  }
  const row = t.closest("tr[data-run]");
  if (row) return openDrawer(row.dataset.run);
});

/* ---------- dashboard ---------- */
const FILTERS = [["all", "All"], ["review", "Needs review"], ["APPROVED", "Approved"], ["HOLD", "On hold"], ["REJECTED", "Rejected"]];
async function loadDashboard() {
  const [stats, runs] = await Promise.all([api("/api/stats"), api("/api/runs")]);
  state.runs = runs;
  state.slaHours = stats.sla_hours;
  $("#kpis").innerHTML = [
    ["Invoices processed", stats.processed, `${stats.avg_seconds} s average per invoice`, "doc", ""],
    ["Touchless rate", stats.processed ? stats.touchless_rate + "%" : "—", `${stats.auto_approved} approved with no human touch`, "bolt", "", stats.processed ? `<div class="bar"><i class="good" style="width:${stats.touchless_rate}%"></i></div>` : ""],
    ["Waiting for a person", stats.awaiting_review, `${money(stats.awaiting_value)} blocked until reviewed`, "clock", "t-warn" + (stats.awaiting_review > 0 ? " attn" : "")],
    ["Cleared for payment", money(stats.cleared_value), "including invoices approved after review", "check", "t-ok"],
    ["Stopped before payment", money(stats.stopped_value), `${stats.held} held and ${stats.rejected} rejected by the process`, "shield", "t-bad"],
  ].map(([l, v, s, icon, cls, extra]) => `<div class="kpi ${cls}"><div class="kpi-top"><span class="kpi-label">${l}</span><span class="kpi-icon">${SVG[icon]}</span></div><div class="kpi-value">${v}</div>${extra || ""}<div class="kpi-sub">${s}</div></div>`).join("");
  const done = runs.filter((r) => r.status !== "running"), n = (k) => done.filter((r) => r.final_state === k).length;
  $("#split").innerHTML = !done.length ? "" : `<div class="split">${["APPROVED", "HOLD", "REJECTED"].filter(n).map((k) => `<i class="${k}" style="flex:${n(k)}"></i>`).join("")}</div>
    <div class="split-legend">${["APPROVED", "HOLD", "REJECTED"].map((k) => `<span class="${k}"><b>${n(k)}</b> ${OUTCOME[k].toLowerCase()}</span>`).join("")}</div>`;
  const mins = stats.minutes_saved, hrs = mins >= 60 ? (mins / 60).toFixed(1) + " hours" : mins + " minutes";
  $("#impact").innerHTML = !stats.processed ? `<div class="muted">Process an invoice to see the impact.</div>` : `
    <div class="impact-line"><b>${hrs}</b> of manual work avoided on ${stats.processed} invoice${stats.processed === 1 ? "" : "s"}
      <span class="muted">· assumes ${stats.impact.manual_minutes_per_invoice} min to handle an invoice by hand and ${stats.impact.review_minutes_per_hold} min for a person to answer a prepared question</span></div>
    ${stats.resumed || stats.corrected ? `<div class="muted" style="margin:-6px 0 12px;font-size:13px">${stats.resumed} held invoice${stats.resumed === 1 ? "" : "s"} re-checked by the process when data changed · ${stats.corrected} resolved by a person's correction or answer</div>` : ""}
    ${stats.reasons.length ? `<div class="table-wrap"><table><thead><tr><th>Why invoices were stopped</th><th>Outcome</th><th>Goes to</th><th class="num">Invoices</th><th class="num">Value protected</th></tr></thead><tbody>
      ${stats.reasons.map((g) => `<tr><td>${esc(g.reason)}</td><td><span class="chip ${g.outcome}">${OUTCOME[g.outcome]}</span></td><td>${esc(g.owner || "")}</td><td class="num">${g.count}</td><td class="num">${money(g.value)}</td></tr>`).join("")}
    </tbody></table></div>` : `<div class="muted">Nothing has been stopped yet.</div>`}`;
  $("#filters").innerHTML = FILTERS.map(([k, l]) => `<button data-filter="${k}" class="${state.filter === k ? "active" : ""}">${l}</button>`).join("");
  renderRows();
  setBadge(stats.awaiting_review);
}
$("#filters").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.filter = b.dataset.filter;
  for (const x of $("#filters").children) x.classList.toggle("active", x === b);
  renderRows();
});
function renderRows() {
  const f = state.filter;
  const rows = state.runs.filter((r) => f === "all" || (f === "review" ? r.final_state === "HOLD" && r.status !== "running" : r.final_state === f));
  $("#runs-empty").hidden = rows.length > 0;
  $("#runs-empty").textContent = state.runs.length ? "No invoices match this filter." : "No invoices yet. Process one to see it here.";
  $("#runs-body").innerHTML = rows.map((r) => `<tr data-run="${r.id}">
      <td class="muted" style="white-space:nowrap">${when(r.created_at)}</td>
      <td><b style="font-weight:600">${esc(r.vendor_name || "Unknown")}</b><div class="muted" style="font-size:12px">${esc(r.filename)}</div></td>
      <td class="mono">${esc(r.invoice_number || "—")}</td><td class="mono">${esc(r.po_number || "—")}</td>
      <td class="num">${money(r.total_amount, r.currency)}</td>
      <td>${outcomeChip(r)}</td>
      <td class="why">${esc(r.headline || "")}</td>
      <td>${esc(r.review ? r.review.reviewer : r.owner || "")}${r.final_state === "HOLD" && !r.review && r.status !== "running" ? `<div class="${waiting(r).overdue ? "overdue" : "muted"}" style="font-size:12px">waiting ${waiting(r).age}${waiting(r).overdue ? " · overdue" : ""}</div>` : ""}</td>
      <td class="num muted">${r.duration_ms != null ? (r.duration_ms / 1000).toFixed(1) + " s" : ""}</td>
    </tr>`).join("");
}
function setBadge(n) {
  const b = $("#review-badge");
  b.hidden = !n;
  b.textContent = n;
}
async function refreshBadge() {
  const s = await api("/api/stats").catch(() => null);
  if (s) setBadge(s.awaiting_review);
}

/* ---------- drawer ---------- */
async function openDrawer(id) {
  const run = await api(`/api/runs/${id}`);
  $("#drawer-body").innerHTML = "";
  $("#drawer").hidden = $("#drawer-backdrop").hidden = false;
  renderRun(run, $("#drawer-body"));
}
function closeDrawer() {
  $("#drawer").hidden = $("#drawer-backdrop").hidden = true;
}
$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());

/* ---------- reference data ---------- */
async function loadReference() {
  const ref = await api("/api/reference");
  const vname = Object.fromEntries(ref.vendors.map((v) => [v.id, v.name]));
  $("#po-body").innerHTML = ref.purchase_orders.map((p) => {
    const pct = Math.min(100, Math.round((p.invoiced / p.amount) * 100));
    return `<tr><td class="mono" style="white-space:nowrap"><b>${p.po_number}</b></td><td>${esc(vname[p.vendor_id])}</td>
      <td class="po-for">${esc(p.description)}<div class="muted" style="font-size:12px">${p.lines.map((l) => `${l.qty} × ${esc(l.description)} @ ${money(l.unit_price)}`).join(" · ")}</div></td>
      <td class="nowrap">${esc(p.buyer)}</td><td class="num">${money(p.amount)}</td><td class="num">${money(p.received_value)}</td><td class="num">${money(p.invoiced)}</td><td class="num">${money(p.remaining)}</td>
      <td><div class="bar"><i class="${p.remaining < 0 ? "over" : ""}" style="width:${pct}%"></i></div></td>
      <td><span class="chip ${p.state === "closed" ? "" : p.state === "open" ? "ok" : "warn"}">${cap(p.state)}</span></td></tr>`;
  }).join("");
  $("#vendor-body").innerHTML = ref.vendors.map((v) => `<tr><td class="mono">${v.id}</td><td><b style="font-weight:600">${esc(v.name)}</b><div class="muted" style="font-size:12px">also: ${esc(v.aliases.join(", ") || "—")}</div></td>
      <td class="mono">${v.tax_id}</td><td class="mono">····${v.bank_last4}</td><td><span class="chip ${v.status === "approved" ? "ok" : "fail"}">${cap(v.status)}</span></td></tr>`).join("");
  const lines = ref.purchase_orders.flatMap((p) => p.lines.filter((l) => l.received < l.qty).map((l) => ({ p, l })));
  $("#receipt-form").innerHTML = `<select id="rc-line">${lines.map(({ p, l }) => `<option value="${p.po_number}|${l.line_no}" ${p.po_number === state.receiptPo ? "selected" : ""}>${p.po_number} · ${esc(l.description)} · ${l.received} of ${l.qty} received</option>`).join("")}</select>
    <input id="rc-qty" type="number" min="0.01" step="any" value="1" style="width:80px" aria-label="Quantity received"><input id="rc-by" placeholder="Your name" maxlength="60" style="width:150px">
    <button class="btn small" id="rc-save">Record receipt</button>`;
  state.receiptPo = null;
  $("#rule-inputs").innerHTML = `<div class="rule-row"><span class="muted">Changes are made by</span><input id="rule-by" placeholder="Your name" maxlength="60"></div>` +
    Object.entries(ref.editable).map(([k, m]) => `<div class="rule-row"><label for="rule-${k}">${esc(m.label)}</label>
      <span><input id="rule-${k}" type="number" step="any" min="${m.min}" max="${m.max}" value="${ref.rules[k]}"> <span class="muted">${m.unit}</span>
      <button class="btn ghost small" data-rule="${k}">Save</button>${ref.rules[k] !== m.default ? ` <span class="chip warn" title="Default is ${m.default}">changed</span>` : ""}</span></div>`).join("");
  const log = [
    ...ref.learned.map((x) => ({ at: x.at, text: `<b>Learned from ${esc(x.by)}:</b> ${esc(x.what)}` })),
    ...ref.rule_changes.map((x) => ({ at: x.at, text: `<b>Rule changed by ${esc(x.by)}:</b> ${esc(ref.editable[x.key]?.label || x.key)} ${x.old} → ${x.new}` })),
  ].sort((a, b) => (a.at < b.at ? 1 : -1));
  $("#learned").innerHTML = log.length ? log.map((x) => `<li>${x.text} <span class="muted">· ${when(x.at)}</span></li>`).join("") : `<li class="muted">Nothing yet. Answers people give to the process's questions, and rule changes, are recorded here.</li>`;
  const r = ref.rules;
  $("#rules").innerHTML = [
    `<b>Amount tolerance.</b> An invoice may exceed what is left on its PO by up to ${r.tolerance_pct}% of the PO or ${money(r.tolerance_abs)}, whichever is lower. Beyond that it goes to the buyer.`,
    `<b>Unit price.</b> A line price more than ${r.unit_price_tolerance_pct}% away from the PO price is held.`,
    `<b>3-way match.</b> An invoice is only paid for goods and services that have been received and not invoiced before. Billing ahead of receipts is held for the buyer.`,
    `<b>Scanned invoices.</b> Two models read every scan independently. If they disagree on anything that affects payment, it is held for a person to correct.`,
    `<b>Split POs.</b> Invoices are compared with the balance <i>remaining</i> on the PO, not its original value, so part-invoices work and over-billing across several invoices is caught.`,
    `<b>Duplicates.</b> Same file or same invoice number from a vendor is rejected. Same vendor and total within ${r.near_duplicate_window_days} days under a different number is held as a probable duplicate.`,
    `<b>Vendor identity.</b> The name must match the vendor master (${Math.round(r.vendor_fuzzy_threshold * 100)}% similarity at minimum), and any tax ID or bank account mismatch stops payment.`,
    `<b>No PO on the invoice.</b> The PO is inferred from the vendor's open POs by amount and description. It auto-approves only when one PO is a clear winner and the invoice is under ${money(r.implied_po_auto_limit)}.`,
    `<b>Missing information.</b> No invoice number, date, vendor or total means the invoice is returned to the vendor with a drafted reply.`,
    `<b>Dates.</b> Future-dated invoices and invoices older than ${r.max_invoice_age_days} days are held.`,
    `<b>Approval limit.</b> Even a clean invoice above ${money(r.auto_approve_limit)} needs the finance controller's sign-off.`,
    `<b>What is billed must be on the PO.</b> Fitting inside the remaining balance is not enough: unrelated items and extra units are held.`,
    `<b>Credit notes and zero invoices</b> are never approved as payments.`,
    `<b>A hold is a question.</b> It names who must answer, recommends an answer, and escalates to the AP manager after ${r.hold_sla_hours} hours.`,
    `<b>Holds resume by themselves.</b> When a receipt is recorded, a rule changes or a vendor name is confirmed, every held invoice is re-checked.`,
    `<b>Held invoices never consume PO balance</b> until a named person approves them with a reason.`,
  ].map((x) => `<li>${x}</li>`).join("");
}

function setPill(lastReader) {
  const pill = $("#ai-pill");
  if (!state.aiEnabled) {
    pill.textContent = "AI reader off · offline parser";
    pill.className = "pill off";
    pill.title = "No GEMINI_API_KEY set. Digital PDFs are read by the offline parser; scanned invoices will be held.";
  } else if (lastReader === "text-parser") {
    pill.textContent = "AI reader not answering · offline parser";
    pill.className = "pill off";
    pill.title = "The last invoice could not be read by Gemini (busy or over quota) and fell back to the offline parser.";
  } else {
    pill.textContent = "AI reader: Gemini";
    pill.className = "pill on";
    pill.title = `Invoices are read by Gemini; decisions are made by rules. Reader pool: ${state.pool.length} models` + (state.resting.length ? `, ${state.resting.length} resting because their free daily quota is used up.` : ".");
  }
}

document.addEventListener("click", async (e) => {
  const save = e.target.closest("[data-rule]"), rc = e.target.closest("#rc-save");
  try {
    if (save) {
      const by = $("#rule-by").value.trim(), key = save.dataset.rule;
      if (!by) return toast("Add your name: rule changes are recorded");
      await api("/api/rules", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key, value: +$(`#rule-${key}`).value, by }) });
      toast("Rule saved. Held invoices are being re-checked against it.");
    } else if (rc) {
      const [po_number, line_no] = $("#rc-line").value.split("|"), by = $("#rc-by").value.trim();
      const res = await api("/api/receipts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ po_number, line_no: +line_no, qty: +$("#rc-qty").value, by }) });
      toast(`Receipt ${res.receipt_no} recorded. Held invoices are being re-checked.`);
    } else return;
    const name = $("#rule-by")?.value;
    setTimeout(async () => { await loadReference(); if (name && $("#rule-by")) $("#rule-by").value = name; refreshBadge(); }, 1500);
  } catch (err) { toast(err.message); }
});

/* ---------- boot ---------- */
$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("Delete all runs and restore the demo data to its starting state?")) return;
  await api("/api/reset", { method: "POST" });
  if (state.liveSource) state.liveSource.close();
  $("#run-live").innerHTML = "";
  $("#run-empty").hidden = false;
  state.toggled = {};
  setBadge(0);
  show(state.view);
  toast("Demo data reset");
});

(async function boot() {
  $("#stage-preview").innerHTML = STAGE_NAMES.map((s) => `<li>${s}</li>`).join("");
  loadSamples();
  refreshBadge();
  const h = await api("/api/health").catch(() => null);
  state.aiEnabled = !!h?.ai_enabled;
  state.pool = h?.models || [];
  state.resting = h?.resting || [];
  setPill();
})();
