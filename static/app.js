const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (x, cur = "USD") => (x == null ? "—" : (cur === "USD" || !cur ? "$" : cur + " ") + Number(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `Request failed (${r.status})`);
  return r.json();
};
const OUTCOME = { APPROVED: "Approved", HOLD: "On hold", REJECTED: "Rejected" };
const STAGE_NAMES = ["Receive document", "Read invoice", "Validate fields and arithmetic", "Verify vendor", "Check for duplicates", "Match purchase order", "Check amounts against PO balance", "Decide and route"];
const ICON = { ok: "✓", warn: "!", fail: "✕", skipped: "–", pending: "", running: "" };

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
  const reviewed = run.review ? ` after review` : "";
  return `<span class="chip ${run.final_state}">${OUTCOME[run.final_state] || run.final_state}${reviewed}</span>`;
}

function decisionCard(run) {
  const res = run.result;
  if (!res) return "";
  const cur = run.currency;
  const label = { APPROVED: "APPROVED · CLEARED FOR PAYMENT", HOLD: "ON HOLD · NEEDS A PERSON", REJECTED: "REJECTED · NOT ENTERED FOR PAYMENT" }[res.decision];
  let h = `<div class="decision ${res.decision}"><div class="decision-label">${label}</div><div class="decision-headline">${esc(run.headline)}</div>`;

  if (res.reasons?.length) {
    h += `<div class="sub">Why</div><ul>${res.reasons.map((f) => `<li><b>${esc(f.title)}.</b> ${esc(f.message)}</li>`).join("")}</ul>`;
  }
  if (res.passed?.length) h += `<div class="sub">Why</div><ul>${res.passed.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`;
  if (res.notes?.length) h += `<div class="sub">Worth knowing</div><ul>${res.notes.map((f) => `<li><b>${esc(f.title)}.</b> ${esc(f.message)}</li>`).join("")}</ul>`;

  h += `<div class="sub">What happens next</div><div>${esc(res.next_action || "")}</div>`;
  if (res.po_impact) h += `<div class="muted" style="font-size:13px">${esc(res.po_impact)}</div>`;

  const facts = [
    ["Vendor", run.vendor_name], ["Invoice", run.invoice_number], ["Dated", run.invoice_date], ["PO", run.po_number],
    ["Before tax", run.net_amount != null ? money(run.net_amount, cur) : null], ["Tax", run.tax_amount != null ? money(run.tax_amount, cur) : null],
    ["Total", run.total_amount != null ? money(run.total_amount, cur) : null], ["Owner", run.owner],
  ].filter(([, v]) => v);
  if (facts.length) h += `<div class="facts">${facts.map(([k, v]) => `<span><span class="muted">${k}</span> <b>${esc(v)}</b></span>`).join("")}</div>`;

  for (const m of res.messages || []) {
    h += `<div class="message"><div class="message-head"><span>${m.kind === "vendor" ? "Draft reply to vendor (not sent, a person sends it)" : "Note to reviewer"} · To: <b>${esc(m.to)}</b></span>
      <button class="btn ghost small" data-copy="${esc(m.subject + "\n\n" + m.body)}">Copy</button></div>
      <div style="font-weight:600;margin-top:6px">${esc(m.subject)}</div><pre>${esc(m.body)}</pre></div>`;
  }

  if (run.review) {
    const r = run.review;
    h += `<div class="reviewed"><b>${r.action === "approve" ? "Approved" : "Rejected"} by ${esc(r.reviewer)}</b> on ${esc(r.at.replace("T", " at "))}${r.note ? `: “${esc(r.note)}”` : ""}<br><span class="muted">The process had put this on hold. A person made the final call, and both are kept for audit.</span></div>`;
  } else if (run.final_state === "HOLD") {
    h += `<div class="review"><b>Your decision</b> <span class="muted">· nothing is paid until someone decides</span>
      <div class="review-row"><input id="rv-name" placeholder="Your name" maxlength="60"><input id="rv-note" placeholder="Reason (kept for audit)" maxlength="300" style="flex:2"></div>
      <div class="review-row"><button class="btn good" data-review="approve" data-run="${run.id}">Approve for payment</button><button class="btn danger" data-review="reject" data-run="${run.id}">Reject invoice</button></div></div>`;
  }
  return h + `</div>`;
}

function timeline(run) {
  const toggled = (state.toggled[run.id] ||= {});
  const stages = run.stages?.length ? run.stages : STAGE_NAMES.map((t, i) => ({ seq: i + 1, title: t, status: "pending", details: [] }));
  return `<ol class="timeline">${stages.map((s) => {
    const auto = s.status === "warn" || s.status === "fail" || (run.status === "running" && s.status !== "pending");
    const open = s.details.length && (toggled[s.key] ?? auto);
    const summary = s.status === "running" ? "Working…" : s.summary || "";
    return `<li class="stage ${s.status}">
      <div class="dot">${ICON[s.status] ?? ""}</div>
      <div class="stage-head" data-toggle="${s.key}" data-run="${run.id}" data-open="${open ? 1 : 0}">
        <span class="stage-title">${s.seq}. ${esc(s.title)}</span>
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
    <div class="run-meta">Run ${run.id} · received ${esc(run.created_at.replace("T", " "))}${secs}${reader}</div></div>${outcomeChip(run)}`;
  const focus = document.activeElement?.id;
  const vals = { name: $("#rv-name", shell)?.value, note: $("#rv-note", shell)?.value };
  $(".run-main", shell).innerHTML = (run.status !== "running" ? decisionCard(run) : "") + `<h3 style="margin-top:0">What the process did</h3>` + timeline(run);
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
  const src = new EventSource(`/api/runs/${id}/events`);
  state.liveSource = src;
  const finish = () => { src.close(); setBusy(false); refreshBadge(); };
  src.onmessage = (ev) => {
    const run = JSON.parse(ev.data);
    renderRun(run, live);
    if (run.status !== "running") finish();
  };
  src.onerror = async () => {
    // Stream dropped: fall back to fetching the final state so the screen never hangs.
    src.close();
    for (let i = 0; i < 60; i++) {
      const run = await api(`/api/runs/${id}`).catch(() => null);
      if (run) renderRun(run, live);
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
  $("#samples").innerHTML = samples.map((s) => `<div class="sample">
      <div class="sample-top"><span class="sample-title">${esc(s.title)}</span><span class="chip ${s.expected}" title="Expected outcome">${OUTCOME[s.expected] || ""}</span></div>
      <div class="sample-desc">${esc(s.description)}</div>
      <div class="sample-actions"><button class="btn small" data-sample="${esc(s.file)}">Run</button><a href="/api/samples/${encodeURIComponent(s.file)}" target="_blank" rel="noopener">View PDF</a></div>
    </div>`).join("");
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
  $("#kpis").innerHTML = [
    ["Invoices processed", stats.processed, `${stats.avg_seconds} s average per invoice`],
    ["Touchless rate", stats.processed ? stats.touchless_rate + "%" : "—", `${stats.auto_approved} approved with no human touch`],
    ["Waiting for a person", stats.awaiting_review, `${money(stats.awaiting_value)} blocked until reviewed`, stats.awaiting_review > 0],
    ["Cleared for payment", money(stats.cleared_value), "including invoices approved after review"],
    ["Stopped before payment", money(stats.stopped_value), `${stats.held} held and ${stats.rejected} rejected by the process`],
  ].map(([l, v, s, attn]) => `<div class="kpi ${attn ? "attn" : ""}"><div class="kpi-label">${l}</div><div class="kpi-value">${v}</div><div class="kpi-sub">${s}</div></div>`).join("");
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
      <td class="muted" style="white-space:nowrap">${esc(r.created_at.slice(5, 16).replace("T", " "))}</td>
      <td><b style="font-weight:600">${esc(r.vendor_name || "Unknown")}</b><div class="muted" style="font-size:12px">${esc(r.filename)}</div></td>
      <td>${esc(r.invoice_number || "—")}</td><td>${esc(r.po_number || "—")}</td>
      <td class="num">${money(r.total_amount, r.currency)}</td>
      <td>${outcomeChip(r)}</td>
      <td class="why">${esc(r.headline || "")}</td>
      <td>${esc(r.review ? r.review.reviewer : r.owner || "")}</td>
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
    return `<tr><td><b style="font-weight:600">${p.po_number}</b></td><td>${esc(vname[p.vendor_id])}</td>
      <td>${esc(p.description)}<div class="muted" style="font-size:12px">${p.lines.map((l) => `${l.qty} × ${esc(l.description)} @ ${money(l.unit_price)}`).join(" · ")}</div></td>
      <td>${esc(p.buyer)}</td><td class="num">${money(p.amount)}</td><td class="num">${money(p.invoiced)}</td><td class="num">${money(p.remaining)}</td>
      <td><div class="bar"><i class="${p.remaining < 0 ? "over" : ""}" style="width:${pct}%"></i></div></td>
      <td><span class="chip ${p.state === "closed" ? "" : p.state === "open" ? "ok" : "warn"}">${p.state}</span></td></tr>`;
  }).join("");
  $("#vendor-body").innerHTML = ref.vendors.map((v) => `<tr><td>${v.id}</td><td><b style="font-weight:600">${esc(v.name)}</b><div class="muted" style="font-size:12px">also: ${esc(v.aliases.join(", ") || "—")}</div></td>
      <td>${v.tax_id}</td><td>····${v.bank_last4}</td><td><span class="chip ${v.status === "approved" ? "ok" : "fail"}">${v.status}</span></td></tr>`).join("");
  const r = ref.rules;
  $("#rules").innerHTML = [
    `<b>Amount tolerance.</b> An invoice may exceed what is left on its PO by up to ${r.tolerance_pct}% of the PO or ${money(r.tolerance_abs)}, whichever is lower. Beyond that it goes to the buyer.`,
    `<b>Unit price.</b> A line price more than ${r.unit_price_tolerance_pct}% away from the PO price is held.`,
    `<b>Split POs.</b> Invoices are compared with the balance <i>remaining</i> on the PO, not its original value, so part-invoices work and over-billing across several invoices is caught.`,
    `<b>Duplicates.</b> Same file or same invoice number from a vendor is rejected. Same vendor and total within ${r.near_duplicate_window_days} days under a different number is held as a probable duplicate.`,
    `<b>Vendor identity.</b> The name must match the vendor master (${Math.round(r.vendor_fuzzy_threshold * 100)}% similarity at minimum), and any tax ID or bank account mismatch stops payment.`,
    `<b>No PO on the invoice.</b> The PO is inferred from the vendor's open POs by amount and description. It auto-approves only when one PO is a clear winner and the invoice is under ${money(r.implied_po_auto_limit)}.`,
    `<b>Missing information.</b> No invoice number, date, vendor or total means the invoice is returned to the vendor with a drafted reply.`,
    `<b>Dates.</b> Future-dated invoices and invoices older than ${r.max_invoice_age_days} days are held.`,
    `<b>Held invoices never consume PO balance</b> until a named person approves them with a reason.`,
  ].map((x) => `<li>${x}</li>`).join("");
}

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
  const pill = $("#ai-pill");
  if (h?.ai_enabled) {
    pill.textContent = "AI reader: Gemini";
    pill.className = "pill on";
    pill.title = "Invoices are read by " + h.models[0] + ". Decisions are made by rules.";
  } else {
    pill.textContent = "AI reader off · offline parser";
    pill.className = "pill off";
    pill.title = "No GEMINI_API_KEY set. Digital PDFs are read by the offline parser; scanned invoices will be held.";
  }
})();
