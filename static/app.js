"use strict";

// ── State ──────────────────────────────────────────────────────────────────────
const state = {
  sessionId: null,
  statementId: null,
  decisions: {},      // cluster_id -> 'pending'|'approved'|'denied'
  totalClusters: 0,
  allTaggedRows: [],
};

// ── Utilities ──────────────────────────────────────────────────────────────────
function $(id) { return document.getElementById(id); }

function toast(msg, duration = 3000) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), duration);
}

function fmtInr(n) {
  if (n == null || isNaN(n)) return "₹—";
  return "₹" + Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function tagPill(tag) {
  const t = tag || "untagged";
  return `<span class="tag-pill tag-${t}">${t.replace(/_/g," ")}</span>`;
}

function confSpan(c) {
  return `<span class="conf-${c||'low'}">${(c||"low").toUpperCase()}</span>`;
}

function confBar(pct) {
  const filled = Math.round(pct * 12);
  return "█".repeat(filled) + "░".repeat(12 - filled);
}

function confEmoji(band) {
  return { HIGH: "🟢", MEDIUM: "🟡", LOW: "🔴" }[band] || "⚪";
}

// Step navigation
function goStep(n) {
  [1,2,3,4].forEach(i => {
    $(`sec-${i}`).classList.toggle("active", i === n);
    const si = $(`si-${i}`);
    si.classList.remove("active","done");
    if (i < n) si.classList.add("done");
    else if (i === n) si.classList.add("active");
  });
}

// ── Init ───────────────────────────────────────────────────────────────────────
async function init() {
  // Ruleset info
  const ri = await fetch("/api/ruleset-info").then(r => r.json()).catch(() => ({ count: "—" }));
  $("ruleset-badge").textContent = `${ri.count} rules`;

  // Ollama status
  const om = await fetch("/api/models").then(r => r.json()).catch(() => ({ available: false }));
  const statusEl = $("ollama-status");
  if (om.available && om.models.length > 0) {
    statusEl.className = "ollama-status ollama-ok";
    statusEl.textContent = `✓ Ollama (${om.models.length} models)`;
    const sel = $("model-select");
    sel.innerHTML = om.models.map(m => `<option value="${m}">${m}</option>`).join("");
    initAIAnalysis(om.models);
  } else {
    statusEl.className = "ollama-status ollama-err";
    statusEl.textContent = "✗ Ollama not running";
    $("model-select").innerHTML = `<option value="">Ollama unavailable</option>`;
    initAIAnalysis([]);
  }

  // Tabs
  document.querySelectorAll(".tab").forEach(tab => {
    tab.addEventListener("click", () => {
      const t = tab.dataset.tab;
      document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(x => x.classList.remove("active"));
      tab.classList.add("active");
      $(`tab-${t}`).classList.add("active");
    });
  });

  // New statement button
  $("new-stmt-btn").addEventListener("click", () => location.reload());
}

// ── Upload ─────────────────────────────────────────────────────────────────────
function setupUpload() {
  const zone = $("upload-zone");
  const input = $("file-input");

  zone.addEventListener("click", () => input.click());
  zone.addEventListener("dragover", e => { e.preventDefault(); zone.classList.add("drag-over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  zone.addEventListener("drop", e => {
    e.preventDefault();
    zone.classList.remove("drag-over");
    if (e.dataTransfer.files[0]) doUpload(e.dataTransfer.files[0]);
  });
  input.addEventListener("change", () => { if (input.files[0]) doUpload(input.files[0]); });

  $("sample-btn").addEventListener("click", () => doUpload(null, true));
}

async function doUpload(file, useSample = false) {
  const fd = new FormData();
  if (useSample) {
    fd.append("use_sample", "true");
  } else {
    fd.append("file", file);
    fd.append("use_sample", "false");
  }

  goStep(2);
  animateRuleProgress();

  let res;
  try {
    res = await fetch("/api/upload", { method: "POST", body: fd }).then(r => r.json());
  } catch (e) {
    toast("Upload failed: " + e.message);
    goStep(1);
    return;
  }

  if (res.detail) { toast("Error: " + res.detail); goStep(1); return; }

  state.sessionId = res.session_id;
  state.statementId = res.statement_id;

  $("rule-count").textContent = document.getElementById("ruleset-badge").textContent;
  $("txn-count").textContent = res.total;
  $("m-total").textContent = res.total.toLocaleString();
  $("m-tagged").textContent = res.tagged.toLocaleString();
  $("m-coverage").textContent = `${res.coverage_pct}% coverage`;
  $("m-untagged").textContent = res.untagged.toLocaleString();

  // Fill tagged table (matches platform screenshot columns)
  const tbody = $("tagged-body");
  tbody.innerHTML = res.tagged_rows.map(r => `
    <tr>
      <td>${r.date}</td>
      <td class="narration-cell">${r.description}</td>
      <td class="cp-cell">${r.counterparty || "<span style='color:var(--muted)'>—</span>"}</td>
      <td class="amt-cell">${r.type === "debit"  ? fmtInr(r.amount) : ""}</td>
      <td class="amt-cell">${r.type === "credit" ? fmtInr(r.amount) : ""}</td>
      <td>${r.balance != null ? fmtInr(r.balance) : ""}</td>
      <td class="cat-cell">${r.transaction_categorisation || "—"}</td>
      <td>${r.payment_method ? `<span class="pay-badge">${r.payment_method}</span>` : ""}</td>
      <td style="font-size:11px;color:var(--muted)">${r.lender || ""}</td>
      <td style="font-size:11px;color:var(--muted)">${r.vendor || ""}</td>
      <td>${tagPill(r.tag)}</td>
      <td>${confSpan(r.confidence)}</td>
    </tr>`).join("");

  stopRuleProgress(100);
  $("rule-status").textContent = `Done — ${res.tagged} tagged by rules, ${res.untagged} need LLM fallback.`;

  if (res.untagged > 0) {
    $("untagged-section").style.display = "block";
    $("no-untagged").style.display = "none";
    $("untagged-count").textContent = res.untagged;

    const ubody = $("untagged-body");
    ubody.innerHTML = res.untagged_rows.map(r => `
      <tr>
        <td>${r.date}</td>
        <td class="narration-cell">${r.description}</td>
        <td class="cp-cell">${r.counterparty || "<span style='color:var(--muted)'>—</span>"}</td>
        <td>${fmtInr(r.amount)}</td>
        <td>${r.type}</td>
        <td>${r.payment_method ? `<span class="pay-badge">${r.payment_method}</span>` : ""}</td>
        <td style="font-size:11px;color:var(--muted)">${r.lender || ""}</td>
      </tr>`).join("");

    $("llm-btn").addEventListener("click", runLLM);
  } else {
    $("untagged-section").style.display = "none";
    $("no-untagged").style.display = "block";
    $("skip-to-report-btn").addEventListener("click", runFinalize);
  }
}

let _ruleInterval = null;
function animateRuleProgress() {
  let pct = 0;
  _ruleInterval = setInterval(() => {
    pct = Math.min(pct + Math.random() * 8, 90);
    $("rule-progress").style.width = pct + "%";
  }, 80);
}
function stopRuleProgress(to = 100) {
  clearInterval(_ruleInterval);
  $("rule-progress").style.width = to + "%";
}

// ── LLM stream ────────────────────────────────────────────────────────────────
async function runLLM() {
  const model = $("model-select").value;
  if (!model) { toast("Select an Ollama model first"); return; }

  $("llm-btn").disabled = true;
  goStep(3);

  $("llm-meta").textContent = `Sending untagged clusters to ${model}…`;
  $("thinking-model").textContent = model;
  $("clusters-container").innerHTML = "";
  $("llm-error").style.display = "none";
  $("finalize-row").style.display = "none";
  $("review-header").style.display = "none";

  const thinkingBody = $("thinking-body");
  thinkingBody.textContent = "";

  const url = `/api/llm-stream?session_id=${encodeURIComponent(state.sessionId)}&model=${encodeURIComponent(model)}`;
  const evtSource = new EventSource(url);
  let clusterCount = 0;

  evtSource.onmessage = (e) => {
    const data = JSON.parse(e.data);

    if (data.type === "thinking") {
      thinkingBody.textContent += data.token || "";
      thinkingBody.scrollTop = thinkingBody.scrollHeight;
    }
    else if (data.type === "cluster") {
      clusterCount++;
      renderClusterCard(data);
      // stop dot animation once first cluster arrives
      $("thinking-dot").style.animation = "none";
      $("thinking-dot").style.background = "#00B4D8";
    }
    else if (data.type === "done") {
      evtSource.close();
      state.totalClusters = clusterCount;
      const tok = (data.input_tokens||0) + (data.output_tokens||0);
      const ms  = data.elapsed_ms || 0;
      $("llm-meta").textContent =
        `${clusterCount} cluster(s) analysed · ${tok.toLocaleString()} tokens · ${(ms/1000).toFixed(1)}s`;
      $("thinking-wrap").style.display = "none";
      $("review-header").style.display = "block";
      $("finalize-row").style.display = "flex";
      updateReviewProgress();
      checkAllDone();
    }
    else if (data.type === "error") {
      evtSource.close();
      $("llm-error").textContent = data.message;
      $("llm-error").style.display = "flex";
      $("thinking-dot").style.animation = "none";
      $("thinking-dot").style.background = "#ef4444";
    }
  };

  evtSource.onerror = () => {
    evtSource.close();
    $("llm-error").textContent = "Connection to server lost.";
    $("llm-error").style.display = "flex";
  };

  // Skip-deny button
  $("skip-deny-btn").onclick = () => {
    evtSource.close();
    $("thinking-wrap").style.display = "none";
    // deny all pending
    document.querySelectorAll(".cluster-card").forEach(card => {
      const cid = parseInt(card.dataset.cid);
      if (state.decisions[cid] === "pending") denyCluster(cid, card);
    });
    updateReviewProgress();
    checkAllDone();
  };
}

function renderClusterCard(data) {
  const cid = data.cluster_id;
  state.decisions[cid] = "pending";

  const tags = ["salary","business_inflow","emi_payment","cheque_bounce","circular_transfer","gambling","regular_expense","other"];
  const tagOptions = tags.map(t => `<option value="${t}" ${t===data.suggested_tag?"selected":""}>${t.replace(/_/g," ")}</option>`).join("");

  const dir     = data.direction || data.suggested_direction || "";
  const dirBadge = dir ? `<span class="dir-badge dir-${dir}">${dir}</span>` : "";
  const confBadge = `<span class="conf-${data.confidence||'medium'}">${(data.confidence||"medium").toUpperCase()}</span>`;

  const amts = data.example_amounts || [];
  const txnRows = (data.example_descriptions || []).slice(0, 5).map((d, i) => {
    const amt = amts[i] != null ? fmtInr(amts[i]) : "";
    return `<div class="cluster-txn-row">
      <div class="cluster-txn-desc" title="${d}">${d}</div>
      <div class="cluster-txn-amt">${amt}</div>
    </div>`;
  }).join("");

  const amtRange = data.amount_range && data.amount_range.length === 2
    ? `${fmtInr(data.amount_range[0])} – ${fmtInr(data.amount_range[1])}`
    : "";

  const card = document.createElement("div");
  card.className = "cluster-card";
  card.id = `cluster-${cid}`;
  card.dataset.cid = cid;
  card.innerHTML = `
    <div class="cluster-head-bar">
      <div class="cluster-head-left">
        <div class="cluster-id-badge">${cid}</div>
        <div>
          <div class="cluster-title">${data.suggested_tag ? data.suggested_tag.replace(/_/g," ").toUpperCase() : "Unclassified"}</div>
          <div class="cluster-meta">${data.txn_count} transaction(s)${amtRange ? " · " + amtRange : ""}</div>
        </div>
      </div>
      <div class="cluster-head-right">${dirBadge} ${confBadge}</div>
    </div>
    <div class="cluster-body">
      <div class="cluster-section-label">Sample Transactions</div>
      <div class="cluster-txn-list">${txnRows}</div>

      <div class="cluster-reasoning-label">AI Reasoning</div>
      <div class="cluster-reasoning">${data.reasoning || "No reasoning provided."}</div>

      <div class="cluster-fields">
        <div class="field-group">
          <div class="field-label">Assign Tag</div>
          <select class="field-select" id="tag-${cid}">${tagOptions}</select>
        </div>
        <div class="field-group">
          <div class="field-label">Rule Regex (editable)</div>
          <input class="field-input" id="regex-${cid}" value="${(data.suggested_regex || "").replace(/"/g,"&quot;")}">
        </div>
        <div class="field-group">
          <div class="field-label">Confidence</div>
          <input class="field-input" value="${data.confidence || "medium"}" disabled style="background:#f8fafc;">
        </div>
      </div>

      <div class="cluster-actions" id="actions-${cid}">
        <button class="btn btn-success btn-sm" onclick="approveCluster(${cid})">✓ Approve &amp; Add Rule</button>
        <button class="btn btn-danger btn-sm"  onclick="denyCluster(${cid})">✕ Deny</button>
      </div>
    </div>`;

  $("clusters-container").appendChild(card);
}

async function approveCluster(cid) {
  const tag   = $(`tag-${cid}`).value;
  const regex = $(`regex-${cid}`).value;
  const card  = $(`cluster-${cid}`);

  const res = await fetch("/api/approve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: state.sessionId, cluster_id: cid, tag, regex }),
  }).then(r => r.json());

  if (res.success) {
    state.decisions[cid] = "approved";
    card.classList.add("approved");
    $(`actions-${cid}`).innerHTML = `<span class="decision-badge approved">Approved — rule ${res.rule_id}</span>`;
    $("ruleset-badge").textContent = `${parseInt($("ruleset-badge").textContent) + 1} rules`;
    toast(`Rule added: ${tag}`);
    updateReviewProgress();
    checkAllDone();
  }
}

async function denyCluster(cid, cardEl) {
  await fetch("/api/deny", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: state.sessionId, cluster_id: cid }),
  });

  state.decisions[cid] = "denied";
  const card = cardEl || $(`cluster-${cid}`);
  card.classList.add("denied");
  $(`actions-${cid}`).innerHTML = `<span class="decision-badge denied">Denied (used this run at medium confidence)</span>`;
  updateReviewProgress();
  checkAllDone();
}

function updateReviewProgress() {
  const total = Object.keys(state.decisions).length;
  const done  = Object.values(state.decisions).filter(v => v !== "pending").length;
  $("review-count").textContent = `${done} / ${total} reviewed`;
  $("review-progress").style.width = total > 0 ? `${done/total*100}%` : "0%";
}

function checkAllDone() {
  const allDone = Object.values(state.decisions).every(v => v !== "pending");
  $("finalize-btn").disabled = !allDone;
  if (!$("finalize-btn").dataset.bound) {
    $("finalize-btn").dataset.bound = "1";
    $("finalize-btn").addEventListener("click", runFinalize);
  }
}

// ── Finalize ───────────────────────────────────────────────────────────────────
async function runFinalize() {
  $("finalize-btn") && ($("finalize-btn").disabled = true);
  goStep(4);
  $("report-meta").textContent = `Computing metrics for ${state.statementId}…`;

  let report;
  try {
    report = await fetch("/api/finalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId }),
    }).then(r => r.json());
  } catch (e) {
    toast("Finalize error: " + e.message);
    return;
  }

  $("report-meta").textContent = `${state.statementId} · ${report.transactions_total} transactions analysed`;

  renderTrust(report.metrics);
  renderAnomalies(report.anomalies);
  renderTransactions(report.tagged_rows);
  renderRuleset(report.rules_learned);
  await renderCharts();
}

// ── Trust dashboard ───────────────────────────────────────────────────────────
function renderTrust(m) {
  if (!m) return;
  const rows = [
    { name: "ABB (Avg Bank Balance)", val: fmtInr(m.abb?.value), pct: m.abb?.confidence_pct ?? 1, band: m.abb?.confidence, note: "balance-derived — tag-independent" },
    { name: "BTO (Monthly Turnover)", val: fmtInr(m.bto?.value), pct: m.bto?.confidence_pct ?? 0, band: m.bto?.confidence, note: `median of monthly inflows` },
    { name: "Bounce Ratio", val: `${((m.bounce_ratio?.value||0)*100).toFixed(2)}%`, pct: m.bounce_ratio?.confidence_pct ?? 0, band: m.bounce_ratio?.confidence, note: `${m.bounce_ratio?.bounce_count||0} bounces / ${m.bounce_ratio?.emi_count||0} EMIs` },
    { name: "OTI (Obligation/Income)", val: `${((m.oti?.value||0)*100).toFixed(2)}%`, pct: m.oti?.confidence_pct ?? 0, band: m.oti?.confidence, note: `EMI ${fmtInr(m.oti?.total_emi)} / Inflow ${fmtInr(m.oti?.total_inflow)}` },
  ];

  $("trust-container").innerHTML = rows.map(r => `
    <div class="trust-row">
      <div class="trust-name">${r.name}</div>
      <div class="trust-val">${r.val}</div>
      <div class="trust-bar" title="${Math.round((r.pct||0)*100)}% high-confidence tags">${confBar(r.pct||0)}</div>
      <div class="trust-conf">${confEmoji(r.band)} ${r.band||"?"}</div>
      <div class="trust-note">${r.note}</div>
    </div>`).join("");
}

// ── Anomalies ─────────────────────────────────────────────────────────────────
function renderAnomalies(anomalies) {
  if (!anomalies || anomalies.length === 0) {
    $("anomaly-container").innerHTML = `<div class="alert alert-ok" style="margin:4px 0;">✅ No anomalies detected.</div>`;
    return;
  }
  $("anomaly-container").innerHTML = anomalies.map(a => `
    <div class="anomaly-card">
      <div class="anomaly-title">⚠️ ${a.type.replace(/_/g," ").toUpperCase()} — ${a.severity.toUpperCase()} severity</div>
      <div style="font-size:13px;color:var(--muted);margin-bottom:10px;">Counterparty: <code>${a.counterparty}</code></div>
      <div class="anomaly-grid">
        <div class="anomaly-item"><div class="al">Outflow date</div><div class="av">${a.outflow_date}</div></div>
        <div class="anomaly-item"><div class="al">Outflow amount</div><div class="av">${fmtInr(a.outflow_amount)}</div></div>
        <div class="anomaly-item"><div class="al">Inflow date</div><div class="av">${a.inflow_date}</div></div>
        <div class="anomaly-item"><div class="al">Inflow amount</div><div class="av">${fmtInr(a.inflow_amount)}</div></div>
        <div class="anomaly-item"><div class="al">Spread</div><div class="av">${a.spread_days} days</div></div>
        <div class="anomaly-item"><div class="al">Δ amount</div><div class="av">${a.amount_delta_pct}%</div></div>
      </div>
      <div style="margin-top:10px;">
        ${(a.evidence||[]).map(e => `<div style="font-family:var(--mono);font-size:12px;color:var(--muted);margin-top:4px;">• ${e}</div>`).join("")}
      </div>
    </div>`).join("");
}

// ── Transactions ──────────────────────────────────────────────────────────────
function renderTransactions(rows) {
  if (!rows) return;
  state.allTaggedRows = rows;

  // Populate filter
  const tags = [...new Set(rows.map(r => r.tag))].sort();
  $("txn-filter").innerHTML = `<option value="">All</option>` + tags.map(t => `<option value="${t}">${t}</option>`).join("");
  $("txn-filter").addEventListener("change", () => renderTxnTable($("txn-filter").value));
  renderTxnTable("");
}

function renderTxnTable(filterTag) {
  const rows = filterTag ? state.allTaggedRows.filter(r => r.tag === filterTag) : state.allTaggedRows;
  $("final-txn-body").innerHTML = rows.slice(0, 300).map(r => `
    <tr>
      <td>${r.date}</td>
      <td class="narration-cell">${r.description}</td>
      <td class="cp-cell">${r.counterparty || ""}</td>
      <td class="amt-cell">${r.type === "debit"  ? fmtInr(r.amount) : ""}</td>
      <td class="amt-cell">${r.type === "credit" ? fmtInr(r.amount) : ""}</td>
      <td class="cat-cell">${r.transaction_categorisation || "—"}</td>
      <td>${r.payment_method ? `<span class="pay-badge">${r.payment_method}</span>` : ""}</td>
      <td style="font-size:11px;color:var(--muted)">${r.lender || ""}</td>
      <td style="font-size:11px;color:var(--muted)">${r.vendor || ""}</td>
      <td>${tagPill(r.tag)}</td>
      <td style="color:var(--muted);font-size:11px;">${r.tag_source||""}</td>
      <td>${confSpan(r.confidence)}</td>
    </tr>`).join("");
}

// ── Ruleset ───────────────────────────────────────────────────────────────────
async function renderRuleset(learned) {
  const rules = await fetch("/api/ruleset").then(r => r.json()).catch(() => []);

  if (learned && learned.length > 0) {
    $("learned-banner").style.display = "flex";
    $("learned-banner").textContent = `${learned.length} rule(s) added this session.`;
  }

  const learnedIds = new Set((learned||[]).map(r => r.id));
  $("ruleset-body").innerHTML = rules.map(r => `
    <tr style="${learnedIds.has(r.id) ? "background:#f0fdf4;font-weight:600;" : ""}">
      <td style="font-size:11px;color:var(--muted)">${r.id}</td>
      <td>${tagPill(r.tag)}</td>
      <td style="font-family:var(--mono);font-size:11px;max-width:280px;overflow:hidden;text-overflow:ellipsis;">${r.match?.description_regex || ""}</td>
      <td>${r.match?.direction || "any"}</td>
      <td><span style="font-size:11px;padding:2px 7px;border-radius:99px;background:${r.source==="user_confirmed"?"#d1fae5":r.source==="llm_suggested"?"#dbeafe":"#f1f5f9"}">${r.source}</span></td>
      <td>${r.priority}</td>
    </tr>`).join("");
}

// ── Charts ───────────────────────────────────────────────────────────────────
let _charts = {};
async function renderCharts() {
  const log = await fetch("/api/run-log").then(r => r.json()).catch(() => []);
  if (!log.length) return;

  const labels   = log.map((_, i) => `Run ${i+1}`);
  const coverage = log.map(r => r.coverage_pct || 0);
  const ruleSize = log.map(r => r.ruleset_size_after || 0);
  const annotations  = log.map(r => r.rules_learned_this_run > 0 ? `+${r.rules_learned_this_run}` : "");

  const opts = (extra) => ({
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { position: "top" } },
    ...extra
  });

  // Destroy previous charts
  Object.values(_charts).forEach(c => c.destroy());

  _charts.coverage = new Chart($("chart-coverage"), {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: "Rule coverage %",
        data: coverage,
        borderColor: "#16a34a",
        backgroundColor: "#d1fae5",
        tension: .3, fill: true, pointRadius: 5,
      }]
    },
    options: opts({
      scales: { y: { min: 0, max: 100, title: { display: true, text: "% tagged by rules" } } },
      plugins: {
        legend: { position: "top" },
        tooltip: {
          callbacks: {
            afterLabel: (ctx) => annotations[ctx.dataIndex] ? `Rules learned: ${annotations[ctx.dataIndex]}` : "",
          }
        }
      }
    }),
  });

  _charts.ruleset = new Chart($("chart-ruleset"), {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Ruleset size",
        data: ruleSize,
        backgroundColor: "#00B4D8",
        borderRadius: 4,
      }]
    },
    options: opts({ scales: { y: { title: { display: true, text: "# rules" } } } }),
  });

}

// ── AI Analysis ───────────────────────────────────────────────────────────────
function initAIAnalysis(models) {
  const sel = $("ai-model-select");
  if (sel) {
    sel.innerHTML = models.length
      ? models.map(m => `<option value="${m}">${m}</option>`).join("")
      : `<option value="">No models found</option>`;
  }
  const btn = $("ai-run-btn");
  if (btn) btn.addEventListener("click", runAIAnalysis);
}

async function runAIAnalysis() {
  const model = $("ai-model-select").value;
  if (!model) { toast("Select an Ollama model first"); return; }
  if (!state.sessionId) { toast("Finalize a report first"); return; }

  $("ai-run-btn").disabled = true;
  $("ai-report-container").style.display = "none";
  $("ai-error").style.display = "none";
  $("ai-thinking-wrap").style.display = "block";
  $("ai-thinking-dot").style.animation = "pulse 1.2s infinite";
  $("ai-thinking-dot").style.background = "#00B4D8";
  $("ai-thinking-body").textContent = "";
  $("ai-thinking-model").textContent = model;

  const url = `/api/ai-analysis?session_id=${encodeURIComponent(state.sessionId)}&model=${encodeURIComponent(model)}`;
  const src = new EventSource(url);
  let fullText = "";

  src.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === "token") {
      fullText += data.text;
      $("ai-thinking-body").textContent = fullText;
      $("ai-thinking-body").scrollTop = $("ai-thinking-body").scrollHeight;
    } else if (data.type === "done") {
      src.close();
      $("ai-thinking-wrap").style.display = "none";
      $("ai-run-btn").disabled = false;
      renderAIReport(data.full_text || fullText);
    } else if (data.type === "error") {
      src.close();
      $("ai-thinking-wrap").style.display = "none";
      $("ai-error").textContent = data.message;
      $("ai-error").style.display = "flex";
      $("ai-run-btn").disabled = false;
    }
  };

  src.onerror = () => {
    src.close();
    $("ai-thinking-wrap").style.display = "none";
    $("ai-error").textContent = "Connection lost.";
    $("ai-error").style.display = "flex";
    $("ai-run-btn").disabled = false;
  };
}

function renderAIReport(text) {
  const container = $("ai-report-container");

  // Parse sections by ## headings
  const sectionRe = /^##\s+(.+)$/gm;
  const parts = [];
  let last = 0;
  let match;
  const sectionMatches = [];
  while ((match = sectionRe.exec(text)) !== null) {
    sectionMatches.push({ title: match[1].trim(), start: match.index, end: sectionRe.lastIndex });
  }

  let html = "";
  for (let i = 0; i < sectionMatches.length; i++) {
    const s = sectionMatches[i];
    const nextStart = sectionMatches[i + 1]?.start ?? text.length;
    const body = text.slice(s.end, nextStart).trim().replace(/\n+/g, "<br>");
    const titleLower = s.title.toLowerCase();

    if (titleLower.includes("recommendation")) {
      let cls = "conditional", badge = "CONDITIONAL APPROVE";
      if (/APPROVE(?!\s*CONDITIONAL)/i.test(body) && !/CONDITIONAL/i.test(body)) {
        cls = "approve"; badge = "APPROVE";
      } else if (/REJECT/i.test(body)) {
        cls = "reject"; badge = "REJECT";
      } else if (/CONDITIONAL.*APPROVE/i.test(body)) {
        cls = "conditional"; badge = "CONDITIONAL APPROVE";
      }
      html += `<div class="ai-recommendation ${cls}">
        <div class="ai-rec-badge">${badge}</div>
        <div class="ai-rec-text">${body}</div>
      </div>`;
    } else {
      html += `<div class="ai-report-section">
        <h2>${s.title}</h2>
        <p>${body}</p>
      </div>`;
    }
  }

  if (!html) {
    // No sections found — show raw
    html = `<div class="ai-report-section"><p>${text.replace(/\n/g,"<br>")}</p></div>`;
  }

  container.innerHTML = html;
  container.style.display = "block";
}

// ── Sidebar resize + toggle ───────────────────────────────────────────────────
function setupSidebar() {
  const sidebar = $("sidebar");
  const handle  = $("sidebar-resize-handle");
  const toggle  = $("sidebar-toggle");
  if (!sidebar) return;

  const COLLAPSED_THRESHOLD = 80;
  const MIN_WIDTH = 54;
  const MAX_WIDTH = 320;
  const KEY_WIDTH = "fl_sidebar_width";

  function applyWidth(w, animate) {
    if (!animate) sidebar.classList.add("resizing");
    sidebar.style.width = w + "px";
    sidebar.classList.toggle("is-collapsed", w < COLLAPSED_THRESHOLD);
    if (!animate) requestAnimationFrame(() => sidebar.classList.remove("resizing"));
    localStorage.setItem(KEY_WIDTH, w);
  }

  // Restore saved width
  const saved = parseInt(localStorage.getItem(KEY_WIDTH));
  if (saved) applyWidth(Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, saved)), false);

  // Drag-to-resize
  if (handle) {
    let dragging = false, startX = 0, startW = 0;

    handle.addEventListener("mousedown", e => {
      dragging = true;
      startX = e.clientX;
      startW = sidebar.offsetWidth;
      handle.classList.add("dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      e.preventDefault();
    });

    document.addEventListener("mousemove", e => {
      if (!dragging) return;
      const w = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, startW + (e.clientX - startX)));
      applyWidth(w, false);
    });

    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      // Snap fully collapsed if below threshold
      const w = sidebar.offsetWidth;
      if (w < COLLAPSED_THRESHOLD) applyWidth(MIN_WIDTH, false);
    });
  }

  // Toggle button — snap between collapsed (54px) and default (200px)
  if (toggle) {
    toggle.addEventListener("click", () => {
      const w = sidebar.offsetWidth;
      applyWidth(w < COLLAPSED_THRESHOLD ? 200 : MIN_WIDTH, true);
    });
  }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  init();
  setupUpload();
  setupSidebar();
});
