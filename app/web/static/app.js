/* Durable Lead Intake — UI logic. Wires the static UI to /api (API_CONTRACT.md).

   Four variants (slugs): agent_only (no Temporal), agent, agent_steps, orchestrated.
   Tool surfaces are vendor-neutral: CRM, scoring, enrichment, email verification, Slack. */

const $ = (id) => document.getElementById(id);

const VARIANTS = ["agent_only", "agent", "agent_steps", "orchestrated"];

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const ui = {
  variant: "agent",
  scenarios: [],
  wf: null,
  startedAt: 0,
  poll: null,
  lastWasted: 0,
  lastTotals: { new: 0, wasted: 0, total: 0 },
  swStart: null,
  swTimer: null,
  lastFlowCount: 0,
  reviewOpen: false,
  thresholds: { sales: 75, marketing: 50, qualify: 25 },
  timeMultiplier: 20,
  elapsedMs: 0,
  score: null,
};

function flowScrolled(count) {
  const s = $("flow-scroll");
  const flow = $("flow");
  if (s && flow) {
    const newest = flow.children[count - 1];
    const tall = newest && newest.offsetHeight > s.clientHeight * 0.6;
    const pad = count > 1 && tall ? Math.max(0, s.clientHeight - newest.offsetHeight) : 0;
    flow.style.paddingBottom = pad + "px";
    if (count > ui.lastFlowCount && ui.lastFlowCount > 0 && newest) {
      const top = s.scrollTop + (newest.getBoundingClientRect().top - s.getBoundingClientRect().top);
      s.scrollTo({ top, behavior: reduceMotion ? "auto" : "smooth" });
    }
  }
  ui.lastFlowCount = count;
}

// --- helpers ---------------------------------------------------------------

async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body;
}
const postJSON = (path, obj) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj || {}) });

function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4600);
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function animateNumber(el, from, to, prefix) {
  prefix = prefix || "";
  if (reduceMotion || from === to) { el.textContent = prefix + to.toLocaleString(); return; }
  const dur = 450, t0 = performance.now();
  const tick = (t) => {
    const k = Math.min(1, (t - t0) / dur);
    const v = Math.round(from + (to - from) * (1 - Math.pow(1 - k, 3)));
    el.textContent = prefix + v.toLocaleString();
    if (k < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// --- stopwatch (MM:SS.t) ---------------------------------------------------

function fmtElapsed(ms) {
  const tenths = Math.floor(Math.max(0, ms) / 100);
  const t = tenths % 10, s = Math.floor(tenths / 10) % 60, m = Math.floor(tenths / 600);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${t}`;
}
function tickStopwatch() {
  if (ui.swStart == null) return;
  ui.elapsedMs = performance.now() - ui.swStart;
  $("stopwatch").textContent = fmtElapsed(ui.elapsedMs);
  renderTime();
}
function startStopwatch() {
  if (ui.swTimer) clearInterval(ui.swTimer);
  ui.swStart = performance.now();
  ui.swTimer = setInterval(tickStopwatch, 100);
  tickStopwatch();
}
function stopStopwatch() {
  if (ui.swTimer) { clearInterval(ui.swTimer); ui.swTimer = null; }
  tickStopwatch();
}
function resetStopwatch() {
  if (ui.swTimer) { clearInterval(ui.swTimer); ui.swTimer = null; }
  ui.swStart = null;
  ui.elapsedMs = 0;
  $("stopwatch").textContent = "00:00.0";
  $("time-new").textContent = "\u2013";
  $("time-wasted").textContent = "\u2013";
  $("time-wasted").classList.remove("na");
  $("time-total").textContent = "\u2013";
}

// --- Time Spend (simulated) ------------------------------------------------
// Elapsed run time scaled by TIME_MULTIPLIER, split New/Wasted in the SAME proportion as
// the tokens. Apportioning by tokens is what makes it tell the truth about durability
// granularity: Agent as Workflow throws away most of its tokens on a crash, so most of
// its time is wasted too, while Agent Steps wastes ~one step of each. Nothing here is
// persisted — it is a display figure derived from the real stopwatch.

function fmtSpan(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

function renderTime() {
  const total = ui.elapsedMs * ui.timeMultiplier;
  const t = ui.lastTotals || { new: 0, wasted: 0, total: 0 };
  const naWasted = ui.wastedIsNA;
  // Before any tokens are reported there is nothing to apportion by, so it all counts as
  // work in progress rather than guessing a split.
  const share = t.total > 0 ? t.wasted / t.total : 0;
  const wasted = naWasted ? 0 : total * share;
  $("time-new").textContent = fmtSpan(total - wasted);
  $("time-total").textContent = fmtSpan(total);
  const w = $("time-wasted");
  w.textContent = naWasted ? "N/A" : fmtSpan(wasted);
  w.classList.toggle("na", !!naWasted);
  $("sim-note").textContent = `\u00d7${ui.timeMultiplier} simulated`;
}

// --- health ----------------------------------------------------------------

async function pollHealth() {
  try {
    const h = await api("/api/health");
    const agentOnly = h.variant === "agent_only";
    $("dot-temporal").className = "dot " + (agentOnly ? "na" : h.temporal_connected ? "ok" : "bad");
    $("dot-worker").className = "dot " + (agentOnly ? "na" : h.worker_running ? "ok" : "bad");
    $("queue-label").textContent = h.task_queue ? h.task_queue : "";
    $("temporal-link").href = ui.wf && !agentOnly
      ? `${h.temporal_ui}/namespaces/default/workflows/${ui.wf}` : h.temporal_ui;
    if (h.time_multiplier) { ui.timeMultiplier = h.time_multiplier; renderTime(); }
    if (h.thresholds) {
      ui.thresholds = h.thresholds;
      renderScale();
      if (ui.score == null) renderScore(null);   // re-render the band caption
    }
    if (h.variant !== ui.variant && VARIANTS.includes(h.variant)) setVariantUI(h.variant);
  } catch (e) {
    $("dot-temporal").className = "dot bad";
    $("dot-worker").className = "dot bad";
  }
}

// --- variant ---------------------------------------------------------------

const VARIANT_SUBTITLE = {
  agent_only: "Agent Only · no Temporal — if the process dies, the run is gone",
  agent: "Agent as Workflow · the whole agent runs inside one Temporal Activity",
  agent_steps: "Activities as Tools · the agent calls tools; each call is its own Activity",
  orchestrated: "Fully Orchestrated · the workflow leads; a human reviews low scores",
};
const VARIANT_LABEL = {
  agent_only: "Agent Only",
  agent: "Agent as Workflow",
  agent_steps: "Activities as Tools",
  orchestrated: "Fully Orchestrated",
};

function setVariantUI(variant) {
  ui.variant = variant;
  VARIANTS.forEach((v) => $("btn-" + v).setAttribute("aria-pressed", String(variant === v)));
  $("subtitle").textContent = VARIANT_SUBTITLE[variant] || "";
  const agentOnly = variant === "agent_only";
  // Agent Only has no worker: the same button position becomes "Kill process", which is
  // that variant's failure mode — the run is discarded with nothing to resume.
  $("crash").textContent = agentOnly ? "Kill process" : "Crash worker";
  $("restart").disabled = agentOnly;
  $("restart").title = agentOnly ? "Agent Only has no worker to restart" : "";
  $("flow").style.display = agentOnly ? "none" : "flex";
  $("ao-wrap").style.display = agentOnly ? "block" : "none";
  progressInit();
}

async function switchVariant(variant) {
  if (variant === ui.variant) return;
  try {
    closeReview();
    await postJSON("/api/variant", { variant });
    setVariantUI(variant);
    clearLiveRun();
    toast(
      variant === "agent_only"
        ? "Switched to Agent Only — no Temporal, no worker."
        : `Switched to ${VARIANT_LABEL[variant]} — worker restarting.`,
      "ok"
    );
  } catch (e) {
    toast(`Could not switch variant: ${e.message}`, "err");
  }
}

function clearLiveRun() {
  if (ui.poll) { clearInterval(ui.poll); ui.poll = null; }
  ui.wf = null;
  closeReview();
  resetRun();
}

// --- scenarios + form ------------------------------------------------------

async function loadScenarios() {
  ui.scenarios = await api("/api/scenarios");
  const sel = $("scenario");
  sel.innerHTML = "";
  ui.scenarios.forEach((s) => {
    const o = document.createElement("option");
    o.value = s.id;
    o.textContent = `${s.id} — ${s.label}`;
    sel.appendChild(o);
  });
  sel.onchange = () => fillForm(sel.value);
  fillForm(ui.scenarios[0].id);
}

function fillForm(id) {
  const s = ui.scenarios.find((x) => x.id === id);
  if (!s) return;
  $("from_name").value = s.from_name || "";
  $("from_email").value = s.from_email || "";
  $("company").value = s.company || "";
  $("subject").value = s.subject;
  $("body").value = s.body;
  const src = $("source");
  if (![...src.options].some((o) => o.value === s.source)) {
    const o = document.createElement("option"); o.value = s.source; o.textContent = s.source; src.appendChild(o);
  }
  src.value = s.source;
  $("leadid").value = s.id;
  // Deliberately NOT showing the expected outcome or the seeded score: the audience is
  // watching this modal, and revealing the answer before the run defeats the point.
}

// --- lead score vs. thresholds --------------------------------------------

function renderScale() {
  // Ticks sit proportionally UP the bar (bottom = 0, top = 100), which puts the default
  // 25 / 50 / 75 thresholds at three evenly stacked hash marks.
  const t = ui.thresholds || {};
  const put = (id, v) => { const el = $(id); if (el && v != null) el.style.bottom = v + "%"; };
  put("tick-qualify", t.qualify);
  put("tick-marketing", t.marketing);
  put("tick-sales", t.sales);
}

const TIER_OF = (score) => {
  const t = ui.thresholds;
  if (score >= t.sales) return "sales";
  if (score >= t.marketing) return "marketing";
  if (score >= t.qualify) return "nurture";
  return "archive";
};

// Before a score exists, name the bands rather than a destination.
function tierBands() {
  const t = ui.thresholds;
  return `${t.qualify} / ${t.marketing} / ${t.sales} thresholds`;
}

// Short enough to fit the narrow card: the band, then where it goes.
function tierCaption(tier) {
  const t = ui.thresholds;
  if (tier === "sales") return `\u2265 ${t.sales} \u2192 sales`;
  if (tier === "marketing") return `${t.marketing}\u2013${t.sales - 1} \u2192 marketing`;
  if (tier === "nurture") return `${t.qualify}\u2013${t.marketing - 1} \u2192 nurture`;
  return `< ${t.qualify} \u2192 not pursued`;
}

function renderScore(score, opts) {
  // The card is permanently on screen; passing null blanks the VALUE only (grey "--" and
  // a zero-height fill) while the threshold marks stay visible for context.
  // opts.override: a person overruled the model, so the needle shows where THEY put the
  // lead and the caption says so — the model's rejected number is misleading by then.
  const num = $("score-val"), fill = $("score-fill"), foot = $("score-foot");
  if (score == null) {
    ui.score = null;
    num.textContent = "\u2013\u2013";
    num.className = "score-num empty";
    fill.style.height = "0";
    fill.className = "vscale-fill";
    foot.textContent = tierBands();
    return;
  }
  ui.score = score;
  const override = !!(opts && opts.override);
  const tier = TIER_OF(score);
  num.textContent = score;
  num.className = "score-num " + tier;
  fill.style.height = Math.max(2, Math.min(100, score)) + "%";
  fill.className = "vscale-fill " + tier;
  foot.textContent = override ? "Human Override" : tierCaption(tier);
  foot.classList.toggle("override", override);
}

// --- shared box helper -----------------------------------------------------

function fcBox(label, kind, state) {
  const tag = state === "replayed" ? `<span class="fc-tag">↺ replayed</span>` : "";
  return `<div class="fc-box ${kind} ${state}">${label}${tag}</div>`;
}

// --- Agent as Workflow flowchart: Start → Agent → End ----------------------

function renderFlow(flow) {
  const el = $("flow");
  el.dataset.sig = "";
  const rows = (flow && flow.rows) || [];
  const multi = rows.length > 1;
  while (el.children.length > rows.length) el.removeChild(el.lastChild);
  rows.forEach((r, i) => {
    const kicked = i === 0 && r.agent !== "pending";
    const startCls = "fc-start" + (kicked ? " started" : "");
    const endCls = "fc-end" + (r.result === "done" ? " done" : r.result === "failed" ? " failed" : "");
    const sig = [startCls, r.agent, r.result, multi].join("|");
    let row = el.children[i];
    if (row && row.dataset.sig === sig) return;
    const html =
      (multi ? `<span class="fc-idx">${i + 1}</span>` : "") +
      `<span class="${startCls}"></span><span class="fc-arrow"></span>` +
      fcBox("Lead-intake agent", "agent", r.agent) +
      `<span class="fc-arrow"></span><span class="${endCls}"></span>`;
    if (!row) { row = document.createElement("div"); row.className = "flow-row"; el.appendChild(row); }
    row.innerHTML = html;
    row.dataset.sig = sig;
  });
  flowScrolled(rows.length);
}

// --- Activities as Tools: hub-and-spoke (LLM hub + the tool surfaces) ------

const STEPS_NODE_KEYS = ["inference", "read_crm", "score", "enrich", "verify", "write_crm", "route", "notify"];

const STEP_CALLS = {
  read_crm:  { verb: "Reading", done: "Read", why: "pulling the lead record and checking for an existing contact", resp: "CRM record returned" },
  score:     { verb: "Scoring", done: "Scored", why: "asking the scoring service how strong this lead is", resp: "score returned" },
  enrich:    { verb: "Enriching", done: "Enriched", why: "looking up company data for this contact", resp: "company record returned" },
  verify:    { verb: "Verifying", done: "Verified", why: "checking the address is deliverable — this provider is slow", resp: "verification returned" },
  write_crm: { verb: "Writing", done: "Updated", why: "recording the routing decision on the lead", resp: "lead updated" },
  route:     { verb: "Routing", done: "Routed", why: "handing the lead to its destination", resp: "routing accepted" },
  notify:    { verb: "Posting", done: "Notified", why: "telling the team where the lead went", resp: "message delivered" },
};

// Tool surfaces around the model hub. Vendor-neutral by design.
const STEP_TOOLS = [
  { key: "crm", name: "CRM", subs: ["route", "write_crm", "read_crm"] },
  { key: "scoring", name: "Scoring", subs: ["score"] },
  { key: "enrichment", name: "Enrichment", subs: ["enrich"] },
  { key: "emailverify", name: "Email verify", subs: ["verify"] },
  { key: "slack", name: "Slack", subs: ["notify"] },
];

function stepsPick(nodes, tool) {
  const subs = tool.subs.map((k) => ({ k, st: nodes[k] || "gray" }));
  const chosen =
    subs.find((s) => s.st === "blue") || subs.find((s) => s.st === "failed") ||
    subs.find((s) => s.st === "green") || subs.find((s) => s.st === "replayed") ||
    subs[subs.length - 1];
  return { state: chosen.st, sub: chosen.k };
}
function stepsToolLabel(state, sub) {
  const c = STEP_CALLS[sub];
  if (!c) return "";
  if (state === "blue") return c.verb + "…";
  if (state === "failed") return c.verb + " ✕";
  if (state === "green") return c.done;
  if (state === "replayed") return "↺ " + c.done;
  return "";
}

const STEP_POS = {
  llm:         { cx: 170, cy: 130, w: 106, h: 52 },
  crm:         { cx: 170, cy: 26,  w: 96,  h: 36 },
  scoring:     { cx: 56,  cy: 88,  w: 92,  h: 36 },
  enrichment:  { cx: 284, cy: 88,  w: 100, h: 36 },
  emailverify: { cx: 62,  cy: 212, w: 104, h: 36 },
  slack:       { cx: 282, cy: 212, w: 92,  h: 36 },
};
const STEP_CONN = {
  crm:         { x1: 170, y1: 102, x2: 170, y2: 48 },
  scoring:     { x1: 128, y1: 114, x2: 96,  y2: 100 },
  enrichment:  { x1: 212, y1: 114, x2: 240, y2: 100 },
  emailverify: { x1: 132, y1: 150, x2: 100, y2: 196 },
  slack:       { x1: 208, y1: 150, x2: 244, y2: 196 },
};

const WRENCH_D = "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z";

function stepsToolTag(cx, top) {
  const y = top - 13;
  return `<g class="v2-tool-tag">` +
    `<g transform="translate(${cx - 17},${y}) scale(0.38)"><path class="v2-wrench" d="${WRENCH_D}"/></g>` +
    `<text class="v2-tool-tag-txt" x="${cx - 4}" y="${y + 8.5}">tool</text></g>`;
}
function foToolTag(cx, top) {
  const y = top - 11;
  return `<g class="v3-tool-tag">` +
    `<g transform="translate(${cx - 14},${y}) scale(0.34)"><path class="v3-wrench" d="${WRENCH_D}"/></g>` +
    `<text class="v3-tool-tagtxt" x="${cx - 3}" y="${y + 7.5}">tool</text></g>`;
}

// Small vendor-neutral glyphs beside each tool surface. Slack keeps its mark (it is the
// one tool in the demo that really is Slack); the rest are generic.
const TOOL_GLYPHS = {
  crm: `<path class="tg" d="M3 6c0-1.7 4-3 9-3s9 1.3 9 3-4 3-9 3-9-1.3-9-3zM3 6v12c0 1.7 4 3 9 3s9-1.3 9-3V6M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>`,
  scoring: `<path class="tg" d="M12 21a9 9 0 1 1 9-9M12 21a9 9 0 0 0 9-9"/><path class="tg" d="M12 12l5-4"/>`,
  enrichment: `<path class="tg" d="M4 21V6l7-3 7 3v15M9 21v-5h5v5M8 9h1M12 9h1M8 12.5h1M12 12.5h1"/>`,
  emailverify: `<path class="tg" d="M3 6.5h14v8H3zM3 6.5l7 5 7-5"/><path class="tg" d="M15 17.5l2.5 2.5 4-5"/>`,
  slack: `<rect x="3.5" y="3.5" width="7.5" height="7.5" rx="2" fill="#c2185b"/>` +
         `<rect x="13" y="3.5" width="7.5" height="7.5" rx="2" fill="#0277bd"/>` +
         `<rect x="3.5" y="13" width="7.5" height="7.5" rx="2" fill="#1b7f4f"/>` +
         `<rect x="13" y="13" width="7.5" height="7.5" rx="2" fill="#b8860b"/>`,
};
function toolGlyph(key, cx, cy, s) {
  const inner = TOOL_GLYPHS[key];
  if (!inner) return "";
  return `<g class="tool-glyph" transform="translate(${cx - s / 2},${cy - s / 2}) scale(${s / 24})">${inner}</g>`;
}

function stepsNodeSvg(pos, state, name, sub, isLlm) {
  const { cx, cy, w, h } = pos;
  const x = cx - w / 2, y = cy - h / 2;
  const nameY = sub ? cy - 2 : cy + 4;
  const subTxt = sub ? `<text class="v2n-sub" x="${cx}" y="${cy + 13}" text-anchor="middle">${sub}</text>` : "";
  return `<g class="v2n ${state} ${isLlm ? "llm" : ""}"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="10"/>` +
    `<text class="v2n-name" x="${cx}" y="${nameY}" text-anchor="middle">${name}</text>${subTxt}</g>`;
}

function buildStepsSvg(nodes) {
  const blueTool = STEP_TOOLS.find((t) => t.subs.some((k) => nodes[k] === "blue"));
  let edges = "";
  for (const t of STEP_TOOLS) {
    const c = STEP_CONN[t.key];
    const active = blueTool && blueTool.key === t.key;
    edges += `<line class="v2c ${active ? "active" : ""}" x1="${c.x1}" y1="${c.y1}" x2="${c.x2}" y2="${c.y2}" marker-end="url(#v2ah${active ? "a" : ""})"/>`;
  }
  const toolActive = !!blueTool;
  const llmState = nodes.inference === "blue" || toolActive ? "blue"
    : nodes.inference === "failed" ? "failed"
    : (nodes.inference === "green" || nodes.inference === "replayed" ? "green" : "gray");
  const llmSub = nodes.inference === "blue" ? "reasoning…"
    : toolActive ? "calling " + blueTool.name
    : nodes.inference === "failed" ? "interrupted" : "";
  let boxes = stepsNodeSvg(STEP_POS.llm, llmState, "LLM", llmSub, true);
  for (const t of STEP_TOOLS) {
    const { state, sub } = stepsPick(nodes, t);
    const pos = STEP_POS[t.key];
    boxes += stepsToolTag(pos.cx, pos.cy - pos.h / 2);
    boxes += stepsNodeSvg(pos, state, t.name, stepsToolLabel(state, sub), false);
    boxes += toolGlyph(t.key, pos.cx - pos.w / 2 + 13, pos.cy, 14);
  }
  return `<svg class="v2svg" viewBox="0 -14 340 262" preserveAspectRatio="xMidYMid meet">
    <defs>
      <marker id="v2ah" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" class="v2ahfill"/></marker>
      <marker id="v2aha" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" class="v2ahfilla"/></marker>
    </defs>${edges}${boxes}</svg>`;
}

function stepsCaption(nodes) {
  const failedTool = STEP_TOOLS.find((t) => t.subs.some((k) => nodes[k] === "failed"));
  if (failedTool) {
    const sub = failedTool.subs.find((k) => nodes[k] === "failed");
    return `<div class="v2-cap-line">${failedTool.name} · <b class="crashed">${STEP_CALLS[sub].verb}</b></div><div class="v2-cap-why crashed"><i>interrupted by worker crash</i></div>`;
  }
  if (nodes.inference === "failed") {
    return `<div class="v2-cap-line"><b class="crashed">Reasoning</b></div><div class="v2-cap-why crashed"><i>interrupted by worker crash</i></div>`;
  }
  const blueTool = STEP_TOOLS.find((t) => t.subs.some((k) => nodes[k] === "blue"));
  if (blueTool) {
    const sub = blueTool.subs.find((k) => nodes[k] === "blue");
    ui.stepsLastSub = sub;
    const c = STEP_CALLS[sub];
    return `<div class="v2-cap-line">${blueTool.name} · <b>${c.verb}…</b></div><div class="v2-cap-why"><i>${c.why}</i></div>`;
  }
  const last = ui.stepsLastSub && nodes[ui.stepsLastSub] && nodes[ui.stepsLastSub] !== "gray" ? STEP_CALLS[ui.stepsLastSub] : null;
  if (nodes.inference === "blue") {
    return `<div class="v2-cap-line"><b>Reasoning…</b></div>` + (last ? `<div class="v2-cap-resp">↳ ${last.resp}</div>` : ``);
  }
  if (last) return `<div class="v2-cap-resp">↳ ${last.resp}</div>`;
  return `<div class="v2-cap-why idle"><i>idle</i></div>`;
}

function stepsAgent(nodes) {
  return `<div class="fc-box agent v2box"><div class="agent-title">Lead-intake agent</div>` +
    buildStepsSvg(nodes) + `<div class="v2-caption">${stepsCaption(nodes)}</div></div>`;
}
function renderStepsFlow(flow) {
  const el = $("flow");
  el.dataset.sig = "";
  const charts = (flow && flow.charts) || [];
  const multi = charts.length > 1;
  while (el.children.length > charts.length) el.removeChild(el.lastChild);
  charts.forEach((c, i) => {
    const kicked = i === 0 && Object.values(c.nodes).some((s) => s !== "gray");
    const startCls = "fc-start" + (kicked ? " started" : "");
    const endCls = "fc-end" + (c.result === "done" ? " done" : c.result === "failed" ? " failed" : "");
    const sig = [startCls, JSON.stringify(c.nodes), c.result, multi].join("|");
    let row = el.children[i];
    if (row && row.dataset.sig === sig) return;
    const html =
      (multi ? `<span class="fc-idx">${i + 1}</span>` : "") +
      `<span class="${startCls}"></span><span class="fc-arrow"></span>` +
      stepsAgent(c.nodes) +
      `<span class="fc-arrow"></span><span class="${endCls}"></span>`;
    if (!row) { row = document.createElement("div"); row.className = "flow-row v2"; el.appendChild(row); }
    row.innerHTML = html;
    row.dataset.sig = sig;
  });
  flowScrolled(charts.length);
}
const STEPS_IDLE = { charts: [{ nodes: Object.fromEntries(STEPS_NODE_KEYS.map((k) => [k, "gray"])), result: "pending" }] };

// --- Fully Orchestrated: directed flowchart (workflow-led) -----------------
// Rectangles = activities (tool-tagged), diamonds = decisions, the human-review gate is a
// diamond, stadiums = terminal outcomes. Spine goes DOWN; each decision branches RIGHT.

const TOOL_NAME = { crm: "CRM", scoring: "SCORING", enrichment: "ENRICHMENT", emailverify: "EMAIL VERIFY", llm: "LLM" };
// Node keys match the server's orchestrated_flow output.
const FO_KEYS = ["intake", "quality_q", "score", "threshold_q", "review", "enrich", "verify", "tier_q",
  "archive", "archived", "needs_info", "needs_info_end",
  "to_sales", "sales_end", "to_marketing", "marketing_end", "to_nurture", "nurture_end"];

// LAYOUT: a wide left-to-right spine with branches leaving it vertically. The spine is
// the happy path (fetch -> judge -> score -> threshold -> enrich -> verify -> tier); every
// terminal branch peels off ABOVE or BELOW it and runs right to its own stadium. This
// keeps the aspect ratio wide and short so the whole chart fits the panel without
// scrolling, which a tall top-to-bottom chart did not.
const FO_SPINE = 150;          // y of the main left-to-right spine
const FO_UP = 46;              // y of the branch row above the spine
const FO_DOWN = 268;           // y of the branch row below the spine
const FO_SALES = 60, FO_MKT = 150, FO_NURT = 240;   // the tier fan-out rows

const FON = {
  start:          { t: "start", x: 34,   y: FO_SPINE, label: "lead in" },
  intake:         { t: "rect",  x: 126,  y: FO_SPINE, tool: "crm", label: "Fetch lead" },
  quality_q:      { t: "dec",   x: 268,  y: FO_SPINE, tool: "llm", label: "Enough info?" },
  needs_info:     { t: "rect",  x: 268,  y: FO_UP,    tool: "crm", label: "Request info" },
  needs_info_end: { t: "term",  x: 410,  y: FO_UP,    label: "needs info" },
  score:          { t: "rect",  x: 410,  y: FO_SPINE, tool: "scoring", label: "Score lead" },
  threshold_q:    { t: "dec",   x: 552,  y: FO_SPINE, label: "Score ok?" },
  review:         { t: "dec",   x: 552,  y: FO_DOWN,  human: true, label: "Real lead?" },
  archive:        { t: "rect",  x: 716,  y: FO_DOWN,  tool: "crm", label: "Archive" },
  archived:       { t: "term",  x: 858,  y: FO_DOWN,  label: "archived" },
  enrich:         { t: "rect",  x: 716,  y: FO_SPINE, tool: "enrichment", label: "Enrich" },
  verify:         { t: "rect",  x: 858,  y: FO_SPINE, tool: "emailverify", wait: true, label: "Verify email" },
  tier_q:         { t: "dec",   x: 1000, y: FO_SPINE, label: "Which tier?" },
  to_sales:       { t: "rect",  x: 1156, y: FO_SALES, tool: "crm", label: "To sales" },
  sales_end:      { t: "term",  x: 1296, y: FO_SALES, label: "sales" },
  to_marketing:   { t: "rect",  x: 1156, y: FO_MKT,   tool: "crm", label: "To marketing" },
  marketing_end:  { t: "term",  x: 1296, y: FO_MKT,   label: "marketing" },
  to_nurture:     { t: "rect",  x: 1156, y: FO_NURT,  tool: "crm", label: "To nurture" },
  nurture_end:    { t: "term",  x: 1296, y: FO_NURT,  label: "nurture" },
};

// [from, to, label, route, opts]
//   route "h"  straight right   — leaves A's right edge, enters B's LEFT edge
//   route "v"  straight up/down — leaves A's near edge, enters B's facing edge
//   route "hv" right, then vertical, then right into B's LEFT edge
//   opts: {dx, dy, anchor} nudge the label; {stub} widens the horizontal stub before the
//         vertical, used to keep a merging edge clear of a neighbouring label.
// Every edge enters its target from the left, top or bottom — never from behind.
const FO_EDGES = [
  ["start", "intake", "", "h"],
  ["intake", "quality_q", "", "h"],
  ["quality_q", "needs_info", "no", "v"],
  ["needs_info", "needs_info_end", "", "h"],
  ["quality_q", "score", "yes", "h"],
  ["score", "threshold_q", "", "h"],
  ["threshold_q", "review", "low", "v"],
  ["review", "archive", "no", "h", { dx: 14 }],   // clear of the override vertical
  ["archive", "archived", "", "h"],
  // The human override rejoins the spine: right, up to the spine, then into Enrich's left
  // edge alongside the ordinary "ok" path. The wider stub keeps its vertical clear of the
  // "ok" label.
  ["review", "enrich", "yes", "hv", { stub: 26 }],
  ["threshold_q", "enrich", "ok", "h", { dx: -18 }],
  ["enrich", "verify", "", "h"],
  ["verify", "tier_q", "", "h"],
  ["tier_q", "to_sales", "high", "hv"],
  ["to_sales", "sales_end", "", "h"],
  ["tier_q", "to_marketing", "mid", "h", { dx: 14 }],
  ["to_marketing", "marketing_end", "", "h"],
  ["tier_q", "to_nurture", "low", "hv"],
  ["to_nurture", "nurture_end", "", "h"],
];
const FO_W = 104, FO_RH = 34, FO_DW = 112, FO_DH = 52, FO_TW = 104;
// Height leaves room for the "waiting on a person…" caption that appears under the
// HUMAN diamond while the workflow is parked.
const FO_VIEWBOX = "0 0 1360 324";

function foHalfH(d) { return d.t === "dec" ? FO_DH / 2 : d.t === "start" ? 10 : FO_RH / 2; }
function foHalfW(d) { return d.t === "dec" ? FO_DW / 2 : d.t === "term" ? FO_TW / 2 : d.t === "start" ? 10 : FO_W / 2; }

function foEdge(A, B, lbl, route, off) {
  const opts = off || {};
  const ax = A.x + foHalfW(A), bxLeft = B.x - foHalfW(B);
  let d, lx, ly, anchor = "middle";
  if (route === "v") {
    // Up or down, entering the facing edge.
    const down = B.y > A.y;
    const y1 = down ? A.y + foHalfH(A) : A.y - foHalfH(A);
    const y2 = down ? B.y - foHalfH(B) : B.y + foHalfH(B);
    d = `M${A.x},${y1} V${y2}`;
    lx = A.x + 6; ly = (y1 + y2) / 2 + 3; anchor = "start";
  } else if (route === "hv") {
    // Right out of A, vertical to B's row, then right into B's LEFT edge. The little
    // horizontal stub keeps the vertical clear of A's own shape.
    const mid = ax + (opts.stub || 19);
    d = `M${ax},${A.y} H${mid} V${B.y} H${bxLeft}`;
    lx = mid + 5; ly = (A.y + B.y) / 2 + 3; anchor = "start";
  } else {
    d = `M${ax},${A.y} H${bxLeft}`;
    lx = (ax + bxLeft) / 2; ly = A.y - 7;
  }
  lx += opts.dx || 0; ly += opts.dy || 0; if (opts.anchor) anchor = opts.anchor;
  const label = lbl
    ? `<text class="v3el" x="${lx}" y="${ly}" text-anchor="${anchor}">${lbl}</text>` : "";
  return `<path class="v3e" d="${d}" fill="none" marker-end="url(#v3ah)"/>${label}`;
}

function buildFoSvg(chart) {
  const n = chart.nodes || {};
  const stateOf = (key) => {
    if (key === "start") return (n.intake && n.intake !== "gray") ? "green" : "gray";
    return n[key] || "gray";
  };
  let parts = "";
  for (const [a, b, lbl, route, off] of FO_EDGES) parts += foEdge(FON[a], FON[b], lbl, route, off);
  for (const key in FON) parts += foShape(key, FON[key], stateOf(key));
  return `<svg class="v3svg" viewBox="${FO_VIEWBOX}" preserveAspectRatio="xMidYMin meet">
    <defs><marker id="v3ah" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">
      <path d="M0,0 L6,3 L0,6 Z" class="v3ahfill"/></marker></defs>${parts}</svg>`;
}
function foShape(key, d, state) {
  const g = `class="v3n ${state}"`;
  if (d.t === "start") {
    // Label BELOW the circle: above it was clipped by the top of the viewBox.
    return `<g ${g}><circle cx="${d.x}" cy="${d.y}" r="9"/>` +
      `<text class="v3lbl" x="${d.x}" y="${d.y + 24}" text-anchor="middle">${d.label}</text></g>`;
  }
  if (d.t === "dec") {
    const w = FO_DW / 2, h = FO_DH / 2;
    const pts = `${d.x},${d.y - h} ${d.x + w},${d.y} ${d.x},${d.y + h} ${d.x - w},${d.y}`;
    let tag = "";
    if (d.human) tag = `<text class="v3human" x="${d.x}" y="${d.y - 9}" text-anchor="middle">⚑ HUMAN</text>`;
    else if (d.tool) tag = `<text class="v3tool ${d.tool}" x="${d.x}" y="${d.y - 9}" text-anchor="middle">${TOOL_NAME[d.tool]}</text>`;
    const ty = (d.human || d.tool) ? d.y + 6 : d.y + 3;
    const waiting = d.human && state === "blue"
      ? `<text class="v3waiting" x="${d.x}" y="${d.y + FO_DH / 2 + 12}" text-anchor="middle">waiting on a person…</text>` : "";
    return `<g ${g}><polygon points="${pts}"/>${tag}<text class="v3t" x="${d.x}" y="${ty}" text-anchor="middle">${d.label}</text>${waiting}</g>`;
  }
  const w = d.t === "term" ? FO_TW : FO_W, h = FO_RH, rx = d.t === "term" ? h / 2 : 4;
  const x = d.x - w / 2, y = d.y - h / 2;
  let txt;
  if (d.tool) {
    txt = `<text class="v3tool ${d.tool}" x="${d.x}" y="${d.y - 3}" text-anchor="middle">${TOOL_NAME[d.tool]}</text>` +
          `<text class="v3t" x="${d.x}" y="${d.y + 9}" text-anchor="middle">${d.label}</text>`;
  } else {
    txt = `<text class="v3t" x="${d.x}" y="${d.y + 3}" text-anchor="middle">${d.label}</text>`;
  }
  const tag = d.tool ? foToolTag(d.x, y) : "";
  // The durable-wait marker sits BELOW the node — to its right it collided with the
  // next node on the spine.
  const wait = d.wait
    ? `<text class="v3wait" x="${d.x}" y="${y + h + 12}" text-anchor="middle">⏱ durable wait</text>` : "";
  return `<g ${g}><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${rx}"/>${txt}</g>${tag}${wait}`;
}
function renderFoFlow(flow) {
  const el = $("flow");
  el.dataset.sig = "";
  const charts = (flow && flow.charts) || [];
  const multi = charts.length > 1;
  while (el.children.length > charts.length) el.removeChild(el.lastChild);
  charts.forEach((c, i) => {
    const endCls = "fc-end" + (c.result === "done" ? " done" : c.result === "failed" ? " failed" : "");
    const sig = [endCls, JSON.stringify(c), multi].join("|");
    let row = el.children[i];
    if (row && row.dataset.sig === sig) return;
    const html =
      (multi ? `<span class="fc-idx">${i + 1}</span>` : "") +
      `<div class="fc-box agent v3box"><div class="agent-title">Workflow</div>${buildFoSvg(c)}</div>` +
      `<div class="v3-down right"><i></i></div>` +
      `<div class="v3-cap bottom"><span class="${endCls}"></span></div>`;
    if (!row) { row = document.createElement("div"); row.className = "flow-row v3"; el.appendChild(row); }
    row.innerHTML = html;
    row.dataset.sig = sig;
  });
  flowScrolled(charts.length);
}
const FO_IDLE = { charts: [{ nodes: Object.fromEntries(FO_KEYS.map((k) => [k, "gray"])), result: "pending" }] };

// --- Agent Only: a plain in-process step list (no Temporal, no history) ----

function renderAgentOnly(steps, lost) {
  const el = $("ao-wrap");
  if (lost) {
    el.innerHTML =
      `<div class="ao-lost"><div class="ao-lost-title">Run lost</div>` +
      `<div class="ao-lost-body">The process died mid-run. There is no workflow, no event ` +
      `history and no checkpoint — nothing to resume and nothing to inspect. Every token ` +
      `spent so far is gone.</div></div>`;
    return;
  }
  if (!steps || !steps.length) {
    el.innerHTML = `<div class="ao-empty">No Temporal here — the agent runs in the web-server process. Submit a lead to watch it work.</div>`;
    return;
  }
  el.innerHTML =
    `<div class="ao-note">In-process steps. This list lives in memory only — it is not an event history.</div>` +
    `<div class="ao-list">` +
    steps.map((s) => `<div class="ao-step ${s.type}"><span class="ao-dot"></span><span class="ao-label">${esc(s.label)}</span></div>`).join("") +
    `</div>`;
}

// --- progress init / dispatch ----------------------------------------------

function progressInit() {
  const el = $("flow");
  el.innerHTML = "";
  el.dataset.sig = "";
  $("ao-wrap").innerHTML = "";
  if (ui.variant === "agent_only") renderAgentOnly([], false);
  else if (ui.variant === "agent") renderFlow({ rows: [{ agent: "pending", result: "pending" }] });
  else if (ui.variant === "agent_steps") renderStepsFlow(STEPS_IDLE);
  else renderFoFlow(FO_IDLE);
}

function renderFlowFor(flow) {
  if (ui.variant === "agent") renderFlow(flow || { rows: [] });
  else if (ui.variant === "agent_steps") renderStepsFlow(flow || STEPS_IDLE);
  else if (ui.variant === "orchestrated") renderFoFlow(flow || FO_IDLE);
}

// --- token meter -----------------------------------------------------------

function renderTokens(tok) {
  const tilde = tok.estimated ? "~" : "";
  const naWasted = tok.wasted == null; // Agent Only has no Wasted bucket
  animateNumber($("fig-new"), ui.lastTotals.new, tok.new, tilde);
  animateNumber($("fig-total"), ui.lastTotals.total, tok.total, tilde);
  if (naWasted) {
    $("fig-wasted").textContent = "N/A";
  } else {
    animateNumber($("fig-wasted"), ui.lastTotals.wasted, tok.wasted, tilde);
    if (tok.wasted > ui.lastWasted) {
      const wrap = $("fig-wasted-wrap");
      wrap.classList.remove("bump"); void wrap.offsetWidth; wrap.classList.add("bump");
    }
    ui.lastWasted = tok.wasted;
  }
  ui.lastTotals = { new: tok.new, wasted: naWasted ? 0 : tok.wasted, total: tok.total };
  ui.wastedIsNA = naWasted;
  ui.lastEstimated = !!tok.estimated;
  renderTime();

  const total = Math.max(tok.total, 1);
  $("seg-new").style.width = (100 * tok.new / total) + "%";
  $("seg-wasted").style.width = naWasted ? "0%" : (100 * tok.wasted / total) + "%";
  $("fig-wasted-wrap").classList.toggle("na", naWasted);
  $("meter-cap").textContent = tok.estimated ? "~ estimated (provider did not report usage)" : "";
}

// --- outcome card ----------------------------------------------------------

const ACTION_COPY = {
  route_to_sales: "routed to sales",
  route_to_marketing: "routed to marketing",
  route_to_nurture: "routed to nurture",
  archive: "archived",
  needs_info: "needs info",
};

function closeResult() {
  $("result-overlay").classList.remove("show");
  $("result-overlay").setAttribute("aria-hidden", "true");
}
function renderDecision(result, durationMs) {
  if (!result) { closeResult(); return; }
  const badge = $("decision-badge");
  badge.className = "badge " + (result.action || "");
  badge.textContent = ACTION_COPY[result.action] || (result.action || "").replace(/_/g, " ");
  $("decision-summary").textContent = result.summary || result.error || "";
  const links = $("decision-links");
  links.innerHTML = "";
  if (result.score != null) links.innerHTML += `<div>Model scored it <b>${result.score}</b> (qualify ${ui.thresholds.qualify} · marketing ${ui.thresholds.marketing} · sales ${ui.thresholds.sales})</div>`;
  if (result.human_override) {
    links.innerHTML += `<div class="override">Human review overruled the model` +
      (result.override_score != null ? ` — repositioned to <b>${result.override_score}</b>` : "") +
      `</div>`;
  }
  if (result.owner) links.innerHTML += `<div>Owner <b>${esc(result.owner)}</b></div>`;
  if (result.matched_contact) links.innerHTML += `<div>Matched existing contact <b>${esc(result.matched_contact)}</b></div>`;
  if (result.detail) links.innerHTML += `<div style="color:var(--muted)">${esc(result.detail)}</div>`;
  const tok = result.tokens || ui.lastTotals;
  const secs = (durationMs / 1000).toFixed(1);
  $("decision-meta").textContent = `${VARIANT_LABEL[ui.variant]} · ${secs}s · ${(tok.total || 0).toLocaleString()} tok`;
  $("result-overlay").classList.add("show");
  $("result-overlay").setAttribute("aria-hidden", "false");
}

// --- human lead-review modal (Fully Orchestrated) -------------------------

function openReview(rv) {
  if (ui.reviewOpen) return;
  ui.reviewOpen = true;
  $("review-lead-id").textContent = rv.lead_id || "";
  $("review-score").textContent = rv.score != null ? rv.score : "—";
  $("review-threshold").textContent = rv.qualify_threshold != null ? rv.qualify_threshold : ui.thresholds.qualify;
  const who = [rv.from_name, rv.from_email].filter(Boolean).join(" · ") || "(no sender details)";
  $("review-from").textContent = rv.company ? `${who} — ${rv.company}` : who;
  $("review-source").textContent = rv.source || "";
  $("review-subject").textContent = rv.subject || "";
  $("review-body-text").textContent = rv.body || "";
  $("review-signals").innerHTML = (rv.signals || []).map((s) => `<li>${esc(s)}</li>`).join("");
  $("review-error").textContent = "";
  $("review-overlay").classList.add("show");
  $("review-overlay").setAttribute("aria-hidden", "false");
  setTimeout(() => $("review-sales").focus(), 50);
}
function closeReview() {
  ui.reviewOpen = false;
  $("review-overlay").classList.remove("show");
  $("review-overlay").setAttribute("aria-hidden", "true");
}
// destination === null means "confirm the archive"; otherwise it is the tier the reviewer
// picked and the run continues as if the lead had scored into that band on its own.
async function sendReview(destination) {
  const body = {
    confirm_archive: !destination,
    route_to: destination,
    note: destination
      ? `Reviewed as a real lead; routed to ${destination}.`
      : "Confirmed: not worth pursuing.",
  };
  try {
    const r = await postJSON(`/api/review/${ui.wf}`, body);
    closeReview();
    if (destination) {
      // Stop showing the score the model was overruled on, immediately. The effective
      // score comes back in the Update's acknowledgement, so it is the workflow's own
      // number rather than something the UI made up.
      const eff = r && r.ack && r.ack.effective_score;
      ui.override = eff != null ? eff : null;
      if (eff != null) renderScore(eff, { override: true });
      toast(`Override accepted — routing to ${destination}.`, "ok");
    } else {
      toast("Confirmed — archiving.", "ok");
    }
  } catch (e) {
    $("review-error").textContent = e.message;
  }
}

// --- run / poll ------------------------------------------------------------

function resetRun() {
  ui.lastWasted = 0;
  ui.lastTotals = { new: 0, wasted: 0, total: 0 };
  ui.crashed = false;
  ui.resumed = false;
  ui.frozenFlow = null;
  ui.lastFlow = null;
  ui.stepsLastSub = null;
  ui.score = null;
  ui.override = null;
  closeResult();
  renderTokens({ new: 0, wasted: ui.variant === "agent_only" ? null : 0, total: 0, estimated: false });
  renderScore(null);
  resetStopwatch();
  ui.lastFlowCount = 0;
  const s = $("flow-scroll"); if (s) s.scrollTop = 0;
  const f = $("flow"); if (f) f.style.paddingBottom = "0px";
  progressInit();
}

// --- incoming-lead modal ---------------------------------------------------

function openLead() {
  $("lead-overlay").classList.add("show");
  $("lead-overlay").setAttribute("aria-hidden", "false");
  setTimeout(() => $("subject").focus(), 50);
}
function closeLead() {
  $("lead-overlay").classList.remove("show");
  $("lead-overlay").setAttribute("aria-hidden", "true");
}

async function submitLead() {
  if (ui.poll) { clearInterval(ui.poll); ui.poll = null; }
  closeReview();
  resetRun();
  ui.leadId = $("leadid").value;
  try {
    const r = await postJSON("/api/intake", {
      id: $("leadid").value,
      from_name: $("from_name").value,
      from_email: $("from_email").value,
      company: $("company").value,
      subject: $("subject").value,
      body: $("body").value,
      source: $("source").value,
    });
    ui.wf = r.workflow_id;
    ui.startedAt = performance.now();
    startStopwatch();
    packetFly();
    closeLead();
    toast(
      r.durable
        ? `Intake started for ${ui.leadId} — workflow ${r.workflow_id}.`
        : `Intake started for ${ui.leadId} — in-process, no Temporal.`,
      "ok"
    );
    ui.poll = setInterval(pollRun, 800);
    pollRun();
  } catch (e) {
    toast(e.message, "err");
  }
}

function freezeFlowForCrash(flow) {
  if (!flow) return flow;
  const f = JSON.parse(JSON.stringify(flow));
  const arr = f.charts || f.rows || [];
  const last = arr[arr.length - 1];
  if (last) {
    if (last.agent === "running") last.agent = "failed";
    if (last.nodes) for (const k in last.nodes) if (last.nodes[k] === "blue") last.nodes[k] = "failed";
  }
  return f;
}
function mergeFrozen(live) {
  if (!ui.frozenFlow) return live;
  const key = ui.frozenFlow.charts ? "charts" : "rows";
  const frozen = ui.frozenFlow[key] || [];
  const liveArr = (live && live[key]) || [];
  if (liveArr.length > frozen.length) {
    // The server split a new segment (crash detected during an activity) — append it.
    return { [key]: frozen.concat(liveArr.slice(frozen.length)) };
  }
  if (ui.resumed && liveArr.length) {
    // Worker is back but the server hasn't split a segment yet (e.g. the crash landed
    // between activities). Show the live state as a fresh "resumed" chart under the frozen
    // one immediately, so the second diagram pops in the moment work resumes.
    return { [key]: frozen.concat([liveArr[liveArr.length - 1]]) };
  }
  return { [key]: frozen };
}

async function pollAgentOnly() {
  try {
    const st = await api(`/api/status/${ui.wf}`);
    renderTokens(st.tokens);
    const steps = await api(`/api/timeline/${ui.wf}`);
    renderAgentOnly(steps, false);
    if (st.done) {
      if (st.result && st.result.score != null) renderScore(st.result.score);
      clearInterval(ui.poll); ui.poll = null;
      stopStopwatch();
      renderDecision(st.result, performance.now() - ui.startedAt);
    }
  } catch (e) {
    // A killed Agent-Only run 404s and stays 404 — there is nothing to recover.
    if (/unknown run/i.test(e.message)) {
      clearInterval(ui.poll); ui.poll = null;
      stopStopwatch();
      renderAgentOnly(null, true);
    }
  }
}

async function pollRun() {
  if (!ui.wf) return;
  if (ui.variant === "agent_only") return pollAgentOnly();

  try { renderTokens(await api(`/api/tokens/${ui.wf}`)); } catch (e) {}
  try {
    const st = await api(`/api/status/${ui.wf}`);
    // Human review prompt (Fully Orchestrated).
    if (st.review && st.review.awaiting) openReview(st.review);
    else if (ui.reviewOpen) closeReview();
    // The score card is deliberately withheld until the run is OVER — mid-run it is
    // either unknown (the agent variants only report it at the end) or a distraction.
    // The one exception is Fully Orchestrated parked at the review gate, where the score
    // against the threshold is the entire question being asked.
    const parked = !!(st.review && st.review.awaiting && st.review.score != null);
    const overridden = (st.result && st.result.override_score) ||
                       (st.review && st.review.override_score) || ui.override;
    if (overridden != null && overridden !== false) renderScore(overridden, { override: true });
    else if (parked) renderScore(st.review.score);
    else if (st.done && st.result && st.result.score != null) renderScore(st.result.score);

    if (ui.crashed) { if (ui.frozenFlow) renderFlowFor(ui.frozenFlow); }
    else { ui.lastFlow = st.flow; renderFlowFor(mergeFrozen(st.flow)); }
    if (st.done) {
      clearInterval(ui.poll); ui.poll = null;
      stopStopwatch();
      renderDecision(st.result, performance.now() - ui.startedAt);
      if (st.tokens) renderTokens(st.tokens);
    }
  } catch (e) {}
}

function packetFly() {
  if (reduceMotion) return;
  const p = document.createElement("div");
  p.style.cssText =
    "position:fixed;left:30%;top:46%;width:14px;height:14px;border-radius:50%;background:var(--cyan);box-shadow:0 0 16px var(--cyan);z-index:60;transition:all .7s cubic-bezier(.5,0,.3,1)";
  document.body.appendChild(p);
  requestAnimationFrame(() => { p.style.left = "72%"; p.style.opacity = "0"; });
  setTimeout(() => p.remove(), 750);
}

// --- worker controls -------------------------------------------------------

async function crashWorker() {
  // Agent Only: the equivalent gesture is killing the run itself.
  if (ui.variant === "agent_only") {
    if (!ui.wf) { toast("Nothing running to kill.", "err"); return; }
    try {
      await postJSON(`/api/agent_only/kill/${ui.wf}`);
      if (ui.poll) { clearInterval(ui.poll); ui.poll = null; }
      stopStopwatch();
      // Everything spent on this run is gone — there is no durable record to resume from,
      // so none of it was banked. Move the whole New figure into Wasted and zero New, so
      // the meter says what actually happened: 100% waste. (Until the kill, Wasted reads
      // N/A for this variant because there is no bucket to put anything in.)
      const spent = ui.lastTotals.total || ui.lastTotals.new || 0;
      renderTokens({ new: 0, wasted: spent, total: spent, estimated: ui.lastEstimated });
      renderAgentOnly(null, true);
      if (!reduceMotion) { document.body.classList.add("glitch"); setTimeout(() => document.body.classList.remove("glitch"), 1000); }
      toast("Process killed — the run is gone. Nothing to resume.", "err");
    } catch (e) { toast(e.message, "err"); }
    return;
  }
  ui.crashed = true;
  ui.frozenFlow = freezeFlowForCrash(ui.lastFlow);
  if (ui.frozenFlow) renderFlowFor(ui.frozenFlow);
  try {
    await postJSON("/api/worker/stop");
    if (!reduceMotion) { document.body.classList.add("glitch"); setTimeout(() => document.body.classList.remove("glitch"), 1000); }
    toast("Worker crashed — stays down until you restart.", "err");
  } catch (e) { toast(e.message, "err"); }
}
function toggleFlow() {
  const panel = $("run-panel");
  const collapsed = panel.classList.toggle("flow-collapsed");
  const btn = $("flow-toggle");
  btn.setAttribute("aria-expanded", String(!collapsed));
  btn.setAttribute("aria-label", collapsed ? "Expand flow" : "Collapse flow");
}
async function restartWorker() {
  try {
    await postJSON("/api/worker/start");
    ui.crashed = false;
    ui.resumed = true;  // let the resumed chart pop immediately (see mergeFrozen)
    toast("Worker restarting → work resumes.", "ok");
  } catch (e) { toast(e.message, "err"); }
}

// --- init ------------------------------------------------------------------

function init() {
  VARIANTS.forEach((v) => { $("btn-" + v).onclick = () => switchVariant(v); });
  $("submit").onclick = submitLead;
  $("clear").onclick = clearLiveRun;
  $("crash").onclick = crashWorker;
  $("restart").onclick = restartWorker;
  $("flow-toggle").onclick = toggleFlow;
  $("review-sales").onclick = () => sendReview("sales");
  $("review-marketing").onclick = () => sendReview("marketing");
  $("review-nurture").onclick = () => sendReview("nurture");
  $("review-archive").onclick = () => sendReview(null);
  $("open-lead").onclick = openLead;
  $("lead-close").onclick = closeLead;
  $("lead-overlay").addEventListener("click", (e) => { if (e.target === $("lead-overlay")) closeLead(); });
  $("result-close").onclick = closeResult;
  $("result-dismiss").onclick = closeResult;
  $("result-overlay").addEventListener("click", (e) => { if (e.target === $("result-overlay")) closeResult(); });
  // The review modal is deliberately NOT dismissable by clicking away: the workflow is
  // parked on it, and losing the modal mid-demo would be awkward.
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeLead(); closeResult(); } });
  renderScale();
  renderTime();
  renderScore(null);   // card visible, value withheld until a run finishes
  setVariantUI(ui.variant);
  loadScenarios();
  pollHealth();
  setInterval(pollHealth, 5000);
}
init();
