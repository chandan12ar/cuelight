// Cuelight: the views that are tabs of their own (Prompts, Agents, Spend) and the Activity search.
//
// Loaded after app.js, whose globals (state, esc, insCard, openDrawer...) these use; app.js's
// render() calls renderPrompts, renderAgents and renderSpend. Split out so neither file
// passes the plugin directory's 256 KiB limit; a static report inlines both, in this order.

// ---------------------------------------------------------------- prompts
//
// The Prompts tab: one row per message you sent, how long the work it started took,
// where that time went (waiting on you, agents and tools running, Claude itself) and
// what it set off. A row opens to the full prompt, the agents it launched (each opens
// its panel), the files it edited and its commits. Open rows stay open across polls.

function promptsHtml(p, run) {
  const money = p.top.by === "cost";
  const metrics = [insMetric(p.prompts, p.prompts === 1 ? "prompt" : "prompts"),
    insMetric(p.median_s !== null && p.median_s !== undefined ? fmtDuration(p.median_s) : "—", "typical time per prompt")];
  if (p.top.share !== null && p.top.share !== undefined && p.prompts > 1) {
    metrics.push(insMetric(fmtPct(p.top.share), "of this session's " + (money ? "cost" : "fresh tokens") +
      " went to prompt #" + p.top.n));
  }
  const names = {};
  for (const a of (run && run.agents) || []) names[a.agent_id] = a;
  const open = state.openPrompts || new Set();
  const pct = (part, whole) => (whole > 0 ? Math.max(0, part / whole * 100) : 0).toFixed(1);
  const rows = '<ol class="prompt-list">' + p.rows.map((r) => {
    const total = r.split.claude + r.split.work + r.split.you;
    const words = fmtDuration(r.split.claude) + " Claude itself, " + fmtDuration(r.split.work) +
      " agents and tools, " + fmtDuration(r.split.you) + " waiting on you";
    const spend = r.cost !== null && r.cost !== undefined ? fmtMoney(r.cost, p.currency) : fmtCount(r.tokens) + " tokens";
    const what = [r.agents + (r.agents === 1 ? " agent" : " agents"), r.files + (r.files === 1 ? " file" : " files")];
    if (r.commits) what.push(r.commits + (r.commits === 1 ? " commit" : " commits"));
    what.push(spend);
    const agents = (r.agent_ids || []).map((id) => {
      const a = names[id];
      return '<span class="prompt-agent" data-agent="' + esc(id) + '" role="button" tabindex="0">' +
        '<i class="dot" style="background:' + statusVar(a ? a.status : "unknown") + '"></i>' +
        esc(a ? a.description || id : id) + "</span>";
    }).join("") + (r.agents > (r.agent_ids || []).length
      ? '<span class="prompt-more-n">+' + esc(r.agents - r.agent_ids.length) + " more</span>" : "");
    const files = (r.file_names || []).map((f) => '<span title="' + esc(f) + '">' + esc(fileLabel(f)) + "</span>").join("") +
      (r.files > (r.file_names || []).length ? '<span class="prompt-more-n">+' + esc(r.files - r.file_names.length) + " more</span>" : "");
    const commits = (r.commit_list || []).map((c) =>
      "<span><code>" + esc(c.sha || "—") + "</code> " + esc(c.message || "(no message seen)") + "</span>").join("");
    const detail = '<div class="prompt-detail"><p class="prompt-full">' + esc(r.prompt) + "</p><dl>" +
      "<dt>time</dt><dd>" + esc(fmtDuration(r.seconds) + (r.ongoing ? " so far" : "") + ": " + words) + "</dd>" +
      "<dt>agents</dt><dd>" + (agents || "none") + "</dd>" +
      "<dt>files</dt><dd>" + (files || "none") + "</dd>" +
      (commits ? "<dt>commits</dt><dd>" + commits + "</dd>" : "") +
      "<dt>" + (r.cost !== null && r.cost !== undefined ? "cost" : "tokens") + "</dt><dd>" + esc(spend) + "</dd></dl></div>";
    return '<li><details data-prompt="' + esc(r.n) + '"' + (open.has(String(r.n)) ? " open" : "") + "><summary>" +
      '<span class="prompt-n">#' + esc(r.n) + "</span>" +
      '<span class="prompt-main"><b>' + esc(r.prompt) + "</b>" +
      "<small>" + esc(fmtClock(r.at) + (r.source === "suggestion_accepted" ? " · accepted suggestion"
        : r.source === "queued" ? " · queued" : "")) + "</small></span>" +
      '<span class="prompt-time"><strong>' + esc(fmtDuration(r.seconds) + (r.ongoing ? " so far" : "")) + "</strong>" +
      '<span class="split" role="img" aria-label="' + esc(words) + '" title="' + esc(words) + '">' +
      '<i class="split-claude" style="width:' + pct(r.split.claude, total) + '%"></i>' +
      '<i class="split-work" style="width:' + pct(r.split.work, total) + '%"></i>' +
      '<i class="split-you" style="width:' + pct(r.split.you, total) + '%"></i></span></span>' +
      '<span class="prompt-what">' + esc(what.join(" · ")) + "</span></summary>" + detail + "</details></li>";
  }).join("") + "</ol>";
  const legend = '<p class="split-legend"><span><i class="split-claude"></i>Claude itself</span>' +
    '<span><i class="split-work"></i>agents and tools running</span><span><i class="split-you"></i>waiting on you</span></p>';
  return insCard("Your prompts",
    "Each message you sent, how long the work it started took, and what it set off. Agents, files and commits " +
    "count toward the prompt that was current when they started. Each moment of the time bar counts once: " +
    "waiting on you first, then agents and tools, then Claude itself. Open a prompt for its details.",
    '<div class="metrics">' + metrics.join("") + "</div>" + legend + rows, "wide");
}

function renderPrompts(run) {
  const box = $("prompts");
  if (!box) return;
  const p = run.insights && run.insights.prompts;
  if (!p) {
    box.innerHTML = insCard("Your prompts", "", insEmpty(run.replay_at !== undefined
      ? "Prompts describe the whole session. Leave replay to see them."
      : "No prompts recorded in this session yet. Each message you send appears here with what it set off."), "wide");
    return;
  }
  if (!state.openPrompts) state.openPrompts = new Set();
  const spot = noteFocus(box);
  box.innerHTML = promptsHtml(p, run);
  for (const d of box.querySelectorAll("details[data-prompt]")) {
    d.ontoggle = () => {
      if (d.open) state.openPrompts.add(d.getAttribute("data-prompt"));
      else state.openPrompts.delete(d.getAttribute("data-prompt"));
    };
  }
  for (const el of box.querySelectorAll("[data-agent]")) {
    const openAgent = () => openDrawer(el.getAttribute("data-agent"));
    el.onclick = openAgent;
    el.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openAgent(); } };
  }
  restoreFocus(box, spot);
}

// ---------------------------------------------------------------- agents
//
// The Agents tab: every agent on one row, so what each was asked and expected to produce
// sits beside what came back, and the rows that need a look say why. Columns sort (the
// Agent column by launch order), the filter bar applies, and a row opens the agent's panel.

// Each sortable column and the direction of its first click: biggest first for numbers.
const AGENT_SORTS = { start: 1, status: 1, checked: 1, time: -1, cost: -1, errors: -1 };

function agentFlags(agent) {
  const flags = [];
  const s = agent.status;
  if (s === "failed" || s === "orphaned") flags.push({ text: s, tone: "bad" });
  else if (s === "stalled") flags.push({ text: "stalled", tone: "warn" });
  else if (s === "waiting") flags.push({ text: "waiting on you", tone: "warn" });
  const v = agent.verification;
  if (v && v.state === "failing") flags.push({ text: "checks failing", tone: "bad" });
  else if (v && v.final && v.state === "unchecked") flags.push({ text: "unchecked", tone: "warn" });
  if (s === "completed" && !agent.result_gist) flags.push({ text: "no report", tone: "warn" });
  if (agent.loop) flags.push({ text: "possible loop", tone: "bad" });
  else if (agent.errors && agent.errors.stuck) flags.push({ text: "retrying", tone: "bad" });
  return flags;
}

// Money when the run is priced, else the fresh tokens it spent.
function agentSpend(agent, cost) {
  if (cost && cost.enabled) return { value: agent.cost || 0, text: fmtMoney(agent.cost, cost.currency) };
  const t = agent.tokens || {};
  const n = (t.input || 0) + (t.output || 0) + (t.cache_create || 0);
  return { value: n, text: fmtCount(n) };
}

function agentSortValue(row, key, run, now) {
  const a = row.agent;
  if (key === "status") {
    const rank = ["failed", "orphaned", "stalled", "waiting", "running", "completed"].indexOf(a.status);
    return (rank < 0 ? 6 : rank) * 10 - row.flags.length;
  }
  if (key === "checked") {
    const v = a.verification;
    return v ? ["failing", "unchecked", "checked"].indexOf(v.state) : 3;
  }
  if (key === "time") {
    if (a.duration_s !== null && a.duration_s !== undefined) return a.duration_s;
    return a.started_at ? Math.max(0, now - a.started_at) : 0;
  }
  if (key === "cost") return agentSpend(a, run.cost).value;
  if (key === "errors") return (a.errors && a.errors.failed) || 0;
  return a.started_at || 0;
}

function agentsRows(run) {
  const sort = state.agentsSort || { key: "start", dir: 1 };
  const now = waitNow(run);
  const rows = run.agents.filter(agentMatchesFilter).map((agent, i) => ({ agent, i, flags: agentFlags(agent) }))
    .filter((r) => !state.agentsFlagged || r.flags.length);
  const cmp = (x, y) => (typeof x === "string" ? x.localeCompare(y) : (x > y) - (x < y));
  rows.sort((p, q) => cmp(agentSortValue(p, sort.key, run, now), agentSortValue(q, sort.key, run, now)) * sort.dir ||
    p.i - q.i);
  return rows;
}

function agentsHtml(run) {
  const sort = state.agentsSort || { key: "start", dir: 1 };
  const shown = run.agents.filter(agentMatchesFilter);
  const flagged = shown.filter((a) => agentFlags(a).length).length;
  const stated = shown.filter((a) => a.expected_output).length;
  const money = !!(run.cost && run.cost.enabled);
  const now = waitNow(run);
  const live = run.replay_at === undefined && !state.offline;
  const metrics = [insMetric(shown.length, shown.length === 1 ? "agent" : "agents"), insMetric(flagged, "need a look"),
    insMetric(stated + " of " + shown.length, "said what to deliver")];
  const bar = '<div class="agents-bar"><div class="metrics">' + metrics.join("") + "</div>" +
    '<button type="button" class="agents-only" aria-pressed="' + !!state.agentsFlagged + '">Only the ones that need a look</button></div>';
  const rows = agentsRows(run);
  if (!rows.length) {
    return insCard("Agents", "", bar + insEmpty(state.agentsFlagged ? "No agent needs a look right now." : "No agent matches the filter."), "wide");
  }
  const head = (label, key) => {
    if (!key) return "<th>" + esc(label) + "</th>";
    const on = sort.key === key;
    return '<th aria-sort="' + (on ? (sort.dir > 0 ? "ascending" : "descending") : "none") + '"><button type="button" data-sort="' +
      key + '">' + esc(label) + "</button></th>";
  };
  const text = (value, empty) => (value ? '<span title="' + esc(value) + '">' + esc(value) + "</span>"
    : '<em class="muted">' + esc(empty) + "</em>");
  const body = rows.map((r) => {
    const a = r.agent;
    const v = a.verification;
    const check = !v ? '<span class="muted" title="It edited no code">—</span>'
      : '<span class="ag-check" data-state="' + esc(v.state) + '" title="' + esc(checkText(v)) + '">' +
        esc(v.state === "unchecked" && !v.final ? "not yet" : v.state) + "</span>";
    const done = a.duration_s !== null && a.duration_s !== undefined;
    const time = done ? esc(fmtDuration(a.duration_s))
      : !a.started_at ? "—" : live ? liveSpan(0, a.started_at, now) : esc(fmtDuration(Math.max(0, now - a.started_at)));
    const back = a.result_gist ? text(a.result_gist)
      : text("", a.status === "running" ? "still running" : a.status === "completed" ? "no report" : "no report yet");
    const failed = a.errors && a.errors.failed;
    const flags = r.flags.length ? '<span class="flags">' + r.flags.map((f) =>
      '<span class="flag ' + f.tone + '">' + esc(f.text) + "</span>").join("") + "</span>" : "";
    return '<tr data-agent="' + esc(a.agent_id) + '" tabindex="0"' + (r.flags.length ? ' class="flagged"' : "") + ">" +
      '<td class="ag-agent" data-label="Agent"><b>' + esc(a.description || a.agent_id) + "</b><small>" +
      esc([a.agent_type, fmtModelShort(a.model)].filter(Boolean).join(" · ")) + "</small>" + flags + "</td>" +
      '<td class="ag-text" data-label="Asked">' + text(a.objective, "not found in the brief") + "</td>" +
      '<td class="ag-text" data-label="Expected">' + text(a.expected_output, "not stated") + "</td>" +
      '<td class="ag-text" data-label="Came back">' + back + "</td>" +
      '<td data-label="Status"><i class="dot" style="background:' + statusVar(a.status) + '"></i>' + esc(a.status) + "</td>" +
      '<td data-label="Checked">' + check + "</td>" +
      '<td class="num" data-label="Time">' + time + "</td>" +
      '<td class="num" data-label="' + (money ? "Cost" : "Tokens") + '">' + esc(agentSpend(a, run.cost).text) + "</td>" +
      '<td class="num" data-label="Errors">' + (failed ? esc(failed + " failed") : '<span class="muted">—</span>') + "</td></tr>";
  }).join("");
  const table = '<div class="agents-wrap"><table class="agents-table" aria-label="Agents"><thead><tr>' +
    head("Agent", "start") + head("Asked") + head("Expected") + head("Came back") + head("Status", "status") +
    head("Checked", "checked") + head("Time", "time") + head(money ? "Cost" : "Tokens", "cost") + head("Errors", "errors") +
    "</tr></thead><tbody>" + body + "</tbody></table></div>";
  return insCard("Agents",
    "Every agent on one row: what it was asked, what its brief said to deliver, and the start of what it reported back. " +
    "Rows that need a look say why. Click a heading to sort; the Agent column sorts by launch order. Open a row for the agent's panel.",
    bar + table, "wide");
}

function renderAgents(run) {
  const box = $("agents");
  if (!box) return;
  const spot = noteFocus(box);
  box.innerHTML = agentsHtml(run);
  for (const btn of box.querySelectorAll("[data-sort]")) {
    btn.onclick = () => {
      const key = btn.getAttribute("data-sort");
      const sort = state.agentsSort || { key: "start", dir: 1 };
      state.agentsSort = { key, dir: sort.key === key ? -sort.dir : AGENT_SORTS[key] };
      renderAgents(state.run);
    };
  }
  for (const btn of box.querySelectorAll(".agents-only")) {
    btn.onclick = () => {
      state.agentsFlagged = !state.agentsFlagged;
      renderAgents(state.run);
    };
  }
  for (const row of box.querySelectorAll("tr[data-agent]")) {
    const openAgent = () => openDrawer(row.getAttribute("data-agent"));
    row.onclick = openAgent;
    row.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openAgent(); } };
  }
  restoreFocus(box, spot);
}

// ----------------------------------------------------------------- spend
//
// The Spend tab: the running total over the run (orchestra/spend.py), the main session and its
// agents stacked, or each model, with the budget and where its warning and limit were passed.
// A crosshair (pointer or arrow keys) reads every series at a moment; the legend and the table
// carry every number without it. Money with a price file, else fresh tokens.

// Categorical slots (validated light and dark, see style.css), in fixed order.
const SPEND_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)"];

function spendFmt(sp, v) {
  return sp.unit === "money" ? fmtMoney(v, sp.currency) : fmtCount(Math.round(v));
}

// The stacked series, bottom first. Models are ordered by name ("other" last) so a model keeps
// its colour while the ranking of the others changes during a run.
function spendSeries(sp, split) {
  if (split === "model") {
    const models = sp.models.slice().sort((a, b) =>
      (a.model === "other") - (b.model === "other") || (a.model < b.model ? -1 : a.model > b.model ? 1 : 0));
    return models.map((m, i) => ({ name: m.model, values: m.values, color: SPEND_COLORS[Math.min(i, 4)] }));
  }
  return [{ name: "Main session", values: sp.main, color: SPEND_COLORS[0] },
    { name: "Agents", values: sp.agents, color: SPEND_COLORS[1] }];
}

// What the crosshair shows at bin i.
function spendReadout(sp, split, i) {
  const rows = spendSeries(sp, split).map((s) => ({ name: s.name, color: s.color, value: s.values[i] || 0 }));
  return { at: sp.t[i], rows, total: rows.reduce((sum, r) => sum + r.value, 0) };
}

function spendChart(sp, split, width) {
  const H = 230;
  const series = spendSeries(sp, split);
  const labels = width >= 560;                    // direct labels at the right end, room permitting
  const left = 58;
  const right = labels ? 100 : 10;
  const top = 18;
  const bottom = 24;
  const plotW = Math.max(60, width - left - right);
  const plotH = H - top - bottom;
  const n = sp.t.length;
  const t0 = sp.start;
  const span = Math.max(sp.end - t0, 1e-6);
  const x = (t) => left + ((t - t0) / span) * plotW;
  const b = sp.budget;
  const showBudget = !!(b && b.limit <= Math.max(sp.total, 1e-9) * 3);
  const ceil = Math.max(sp.total, showBudget ? b.limit : 0, 1e-9) * 1.1;
  const y = (v) => top + plotH - (v / ceil) * plotH;
  const base = new Array(n).fill(0);
  let bands = "";
  let ends = "";
  let lastLabelY = Infinity;
  for (const s of series) {
    const lower = base.slice();
    for (let i = 0; i < n; i++) base[i] += s.values[i] || 0;
    let line = "M" + x(t0).toFixed(1) + "," + y(0).toFixed(1);
    for (let i = 0; i < n; i++) line += " L" + x(sp.t[i]).toFixed(1) + "," + y(base[i]).toFixed(1);
    let back = "";
    for (let i = n - 1; i >= 0; i--) back += " L" + x(sp.t[i]).toFixed(1) + "," + y(lower[i]).toFixed(1);
    bands += '<path class="spend-band" style="fill:' + s.color + '" d="' + line + back + " L" + x(t0).toFixed(1) + "," +
      y(0).toFixed(1) + ' Z"/><path class="spend-line" style="stroke:' + s.color + '" d="' + line + '"/>';
    // A band too thin to hold its name leaves it to the legend, the table and the crosshair.
    const mid = (y(base[n - 1]) + y(lower[n - 1])) / 2;
    if (labels && y(lower[n - 1]) - y(base[n - 1]) >= 13 && lastLabelY - mid >= 13) {
      ends += '<text class="spend-end" x="' + (left + plotW + 8) + '" y="' + (mid + 3.5).toFixed(1) + '">' + esc(s.name) + "</text>";
      lastLabelY = mid;
    }
  }
  let grid = "";
  for (let k = 0; k <= 3; k++) {
    const v = (ceil / 1.1) * k / 3;
    grid += '<line class="ins-grid-line" x1="' + left + '" x2="' + (left + plotW) + '" y1="' + y(v).toFixed(1) + '" y2="' +
      y(v).toFixed(1) + '"/><text class="ins-axis" x="' + (left - 6) + '" y="' + (y(v) + 3.5).toFixed(1) +
      '" text-anchor="end">' + esc(v ? spendFmt(sp, v) : "0") + "</text>";
  }
  let budget = "";
  if (showBudget) {
    budget += '<line class="spend-budget" x1="' + left + '" x2="' + (left + plotW) + '" y1="' + y(b.limit).toFixed(1) +
      '" y2="' + y(b.limit).toFixed(1) + '"/>';
    // Each crossing is a dashed line with its name above. Two close together put their names on
    // opposite sides; two at the same spot keep only the limit's.
    const both = b.warn_t !== null && b.limit_t !== null;
    const gap = both ? x(b.limit_t) - x(b.warn_t) : Infinity;
    const mark = (t, cls, text, side) => {
      if (t === null || t === undefined) return "";
      const cx = x(t);
      const anchor = side || (cx > left + plotW - 50 ? "end" : cx < left + 50 ? "start" : "middle");
      const tx = anchor === "end" && side ? cx - 4 : anchor === "start" && side ? cx + 4 : cx;
      return '<line class="' + cls + '" x1="' + cx.toFixed(1) + '" x2="' + cx.toFixed(1) + '" y1="' + top + '" y2="' + y(0).toFixed(1) + '"/>' +
        '<text class="' + cls + '-label" x="' + tx.toFixed(1) + '" y="' + (top - 6) + '" text-anchor="' + anchor + '">' +
        esc(text) + "</text>";
    };
    const close = gap < 160;
    budget += (gap < 6 ? "" : mark(b.warn_t, "spend-warn", fmtPct(b.warn_at / b.limit) + " of budget", close ? "end" : null)) +
      mark(b.limit_t, "spend-limit", "budget", close && gap >= 6 ? "start" : null);
  }
  const ticks = [0, 0.5, 1].map((f) => '<text class="ins-axis" x="' + (left + f * plotW).toFixed(1) + '" y="' + (H - 6) +
    '" text-anchor="' + (f === 0 ? "start" : f === 1 ? "end" : "middle") + '">' + esc(fmtClock(t0 + f * span)) + "</text>").join("");
  const words = "Spend over the run: " + spendFmt(sp, sp.total) + " in all, " + series.map((s) =>
    spendFmt(sp, s.values[n - 1] || 0) + " " + (split === "model" ? "on " + s.name : "by the " + s.name.toLowerCase())).join(", ") +
    ". Use the arrow keys to read it at any moment.";
  const geo = { left, plotW, t0, span, n };
  return '<svg class="ins-chart spend-chart" viewBox="0 0 ' + width + " " + H + '" width="' + width + '" height="' + H +
    '" role="img" tabindex="0" aria-label="' + esc(words) + '" data-geo="' + esc(JSON.stringify(geo)) + '">' + grid + bands +
    budget + ends + ticks + '<line class="spend-cross" x1="0" x2="0" y1="' + top + '" y2="' + y(0).toFixed(1) + '" hidden/></svg>';
}

function spendHtml(run, width) {
  if (run.replay_at !== undefined) {
    return insCard("Spend", "", insEmpty("Spend describes the whole session. Leave replay to see it."), "wide");
  }
  const sp = run.insights && run.insights.spend;
  if (!sp) {
    return insCard("Spend", "", insEmpty("No API calls recorded in this session yet. Spend appears here with the first one."), "wide");
  }
  const money = sp.unit === "money";
  const split = state.spendSplit === "model" ? "model" : "who";
  const minutes = Math.max((sp.end - sp.start) / 60, 1 / 60);
  const metrics = [insMetric(spendFmt(sp, sp.total), money ? "spent so far" + (priceNote(run.cost) ? " (" + priceNote(run.cost) + ")" : "") : "fresh tokens"),
    insMetric(spendFmt(sp, sp.main_total), "main session"), insMetric(spendFmt(sp, sp.agents_total), "agents"),
    insMetric(spendFmt(sp, sp.total / minutes), "a minute on average")];
  if (sp.rate_now !== null && sp.rate_now !== undefined) metrics.push(insMetric(spendFmt(sp, sp.rate_now), "a minute, last 5 minutes"));
  const notes = [];
  let meter = "";
  const budget = money && run.cost && run.cost.budget;
  if (budget) {
    const left = budget.limit - sp.total;
    const pace = sp.rate_now || sp.total / minutes;
    metrics.push(insMetric(fmtPct(sp.total / budget.limit), "of the " + fmtMoney(budget.limit, sp.currency) + " budget"));
    if (left <= 0) notes.push("Over budget by " + fmtMoney(-left, sp.currency) + ".");
    else if (run.session_live && pace > 0) notes.push("At this pace the budget runs out in " + fmtDuration((left / pace) * 60) + ".");
    if (sp.budget && sp.budget.limit > sp.total * 3) notes.push("The budget is far above the curve, so the chart leaves it out.");
    const tone = sp.total >= budget.limit ? "failed" : sp.budget && sp.total >= sp.budget.warn_at ? "stalled" : "completed";
    meter = '<div class="ratio-track"><i style="width:' + Math.min(100, (sp.total / budget.limit) * 100).toFixed(1) +
      "%;background:var(--" + tone + ')"></i></div>';
  }
  if (!money) {
    notes.push("No price file, so this counts fresh tokens (input, output and cache writes). To see money, add prices in " +
      "~/.config/cuelight/prices.json (on Windows %APPDATA%\\cuelight\\prices.json).");
  }
  if (sp.unpriced_models && sp.unpriced_models.length) {
    notes.push("No price for " + sp.unpriced_models.join(", ") + "; those calls count as nothing here.");
  }
  const series = spendSeries(sp, split);
  const n = sp.t.length;
  const toggle = '<div class="spend-split" role="group" aria-label="Split the total">' +
    '<button type="button" data-split="who" aria-pressed="' + (split === "who") + '">Main session and agents</button>' +
    '<button type="button" data-split="model" aria-pressed="' + (split === "model") + '">By model</button></div>';
  const legend = '<ul class="spend-legend">' + series.map((s) =>
    '<li><i style="background:' + s.color + '"></i>' + esc(s.name) + "<b>" + esc(spendFmt(sp, s.values[n - 1] || 0)) + "</b></li>").join("") + "</ul>";
  const table = '<table class="spend-table"><thead><tr><th scope="col">' + (split === "model" ? "Model" : "Who") +
    '</th><th scope="col">' + (money ? "Spent" : "Fresh tokens") + '</th><th scope="col">Share</th></tr></thead><tbody>' +
    series.map((s) => {
      const v = s.values[n - 1] || 0;
      return '<tr><th scope="row">' + esc(s.name) + "</th><td>" + esc(spendFmt(sp, v)) + "</td><td>" +
        esc(fmtPct(sp.total ? v / sp.total : 0)) + "</td></tr>";
    }).join("") + "</tbody></table>";
  const peak = sp.peak ? "<h4>Most expensive five minutes</h4><p class=\"spend-peak\">" +
    esc(fmtClock(sp.peak.start) + "–" + fmtClock(sp.peak.end) + ": " + spendFmt(sp, sp.peak.value) + ", " +
      fmtPct(sp.total ? sp.peak.value / sp.total : 0) + " of the whole. ") + sp.peak.who.map((w) =>
      (w.agent_id ? '<span class="prompt-agent" data-agent="' + esc(w.agent_id) + '" role="button" tabindex="0">' + esc(w.label) + "</span>"
        : esc(w.label)) + " " + esc(spendFmt(sp, w.value))).join(", ") + "</p>" : "";
  const spent = (a) => (money ? a.cost || 0 : (a.tokens.input || 0) + (a.tokens.output || 0) + (a.tokens.cache_create || 0));
  const top = run.agents.filter((a) => spent(a) > 0).sort((a, b) => spent(b) - spent(a)).slice(0, 8);
  const agents = top.length ? "<h4>Most expensive agents</h4>" + insRank(top.map((a) => ({
    name: a.description || a.agent_id, agent: a.agent_id, value: spent(a), label: spendFmt(sp, spent(a)) }))) : "";
  return insCard("Spend",
    "What this session cost as it ran: the main session and its agents stacked, or each model. Hover the chart, or " +
    "focus it and use the arrow keys, for the running total at any moment.",
    '<div class="metrics">' + metrics.join("") + "</div>" + meter +
    (notes.length ? '<p class="card-note">' + esc(notes.join(" ")) + "</p>" : "") + toggle +
    '<div class="spend-plot">' + spendChart(sp, split, width) + '<div class="spend-tip" hidden></div></div>' +
    legend + table + peak + agents, "wide");
}

function renderSpend(run) {
  const box = $("spend");
  if (!box) return;
  const spot = noteFocus(box);
  const oldTip = box.querySelector && box.querySelector(".spend-tip");
  const reading = oldTip && !oldTip.hidden ? state.spendAt : null;
  box.innerHTML = spendHtml(run, Math.max(320, (box.clientWidth || 960) - 38));
  for (const btn of box.querySelectorAll("[data-split]")) {
    btn.onclick = () => { state.spendSplit = btn.getAttribute("data-split"); renderSpend(state.run); };
  }
  for (const el of box.querySelectorAll("[data-agent]")) {
    const open = () => openDrawer(el.getAttribute("data-agent"));
    el.onclick = open;
    el.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } };
  }
  const sp = run.insights && run.insights.spend;
  const svg = box.querySelector && box.querySelector("svg.spend-chart");
  const tip = box.querySelector && box.querySelector(".spend-tip");
  if (!sp || !svg || !tip) { restoreFocus(box, spot); return; }
  const split = state.spendSplit === "model" ? "model" : "who";
  const geo = JSON.parse(svg.getAttribute("data-geo"));
  const cross = svg.querySelector(".spend-cross");
  const xAt = (i) => geo.left + ((sp.t[i] - geo.t0) / geo.span) * geo.plotW;
  const show = (i) => {
    i = Math.max(0, Math.min(geo.n - 1, i));
    state.spendAt = i;
    const r = spendReadout(sp, split, i);
    const cx = xAt(i);
    cross.setAttribute("x1", cx.toFixed(1));
    cross.setAttribute("x2", cx.toFixed(1));
    cross.removeAttribute("hidden");
    tip.textContent = "";
    const head = document.createElement("div");
    head.className = "spend-tip-time";
    head.textContent = fmtClock(r.at);
    tip.appendChild(head);
    for (const row of r.rows.concat([{ name: "in all", value: r.total }])) {
      const line = document.createElement("div");
      if (row.color) {
        const key = document.createElement("i");
        key.style.background = row.color;
        line.appendChild(key);
      }
      const value = document.createElement("b");
      value.textContent = spendFmt(sp, row.value);
      line.appendChild(value);
      line.appendChild(document.createTextNode(" " + row.name));
      tip.appendChild(line);
    }
    tip.hidden = false;
    const scale = svg.getBoundingClientRect().width / Math.max(1, svg.viewBox.baseVal.width);
    const px = cx * scale;
    tip.style.left = Math.max(0, Math.min(px + 12, svg.getBoundingClientRect().width - tip.offsetWidth)) + "px";
  };
  const hide = () => { state.spendAt = null; cross.setAttribute("hidden", ""); tip.hidden = true; };
  svg.onpointermove = (event) => {
    const rect = svg.getBoundingClientRect();
    const vx = (event.clientX - rect.left) / Math.max(1, rect.width) * svg.viewBox.baseVal.width;
    const t = geo.t0 + ((vx - geo.left) / geo.plotW) * geo.span;
    let best = 0;
    for (let i = 1; i < geo.n; i++) if (Math.abs(sp.t[i] - t) < Math.abs(sp.t[best] - t)) best = i;
    show(best);
  };
  svg.onpointerleave = () => { if (document.activeElement !== svg) hide(); };
  svg.onfocus = () => show(state.spendAt === null || state.spendAt === undefined ? geo.n - 1 : state.spendAt);
  svg.onblur = hide;
  svg.onkeydown = (event) => {
    const at = state.spendAt === null || state.spendAt === undefined ? geo.n - 1 : state.spendAt;
    const step = { ArrowLeft: -1, ArrowRight: 1, PageDown: -10, PageUp: 10 }[event.key];
    if (step) { event.preventDefault(); show(at + step); } else if (event.key === "Home") { event.preventDefault(); show(0); }
    else if (event.key === "End") { event.preventDefault(); show(geo.n - 1); }
  };
  // A live refresh redraws the chart: keep the reading (and focus) where it was. The reading is
  // put back directly, since a focus event does not fire while the window is in the background.
  restoreFocus(box, spot);
  if (reading !== null && reading !== undefined) show(reading);
}

// --------------------------------------------------------- activity search
//
// The Activity tab's search: every tool call the run made, not only the live tail, matched on
// tool, file or command and agent, or only the failed ones. The server matches scrubbed text
// (/api/calls); a static report runs the same rules over the details baked into it (localCalls).
// Enter steps to the next match, Shift+Enter back, Escape clears.

function activitySearching() {
  return String(state.callQuery || "").trim().length >= 2 || !!state.callFailed;
}

// Text with every search term marked, case-insensitively; each piece escaped on its own.
function markTerms(text, query) {
  const raw = String(text || "");
  const lower = raw.toLowerCase();
  const terms = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
  const hit = new Array(raw.length).fill(false);
  for (const term of terms) {
    for (let at = lower.indexOf(term); at >= 0; at = lower.indexOf(term, at + 1)) {
      for (let i = at; i < at + term.length; i++) hit[i] = true;
    }
  }
  let out = "";
  let i = 0;
  while (i < raw.length) {
    let j = i;
    while (j < raw.length && hit[j] === hit[i]) j++;
    out += hit[i] ? "<mark>" + esc(raw.slice(i, j)) + "</mark>" : esc(raw.slice(i, j));
    i = j;
  }
  return out;
}

// The same rules as orchestra/search.py calls(), over a static report's baked-in details.
function localCalls(query, failedOnly) {
  query = String(query || "").trim().slice(0, 200);
  const out = { query, failed_only: !!failedOnly, rows: [], matched: 0, truncated: false };
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean).slice(0, 8);
  if (!failedOnly && (query.length < 2 || !terms.length)) return out;
  const details = (typeof window !== "undefined" && window.ORCHESTRA_DETAILS) || {};
  const merged = new Map();
  let scanned = 0;
  for (const a of (state.run && state.run.agents) || []) {
    const d = details[a.agent_id];
    if (!d) continue;
    const label = d.description || a.description || "";
    const calls = d.tool_calls || [];
    for (let i = calls.length - 1; i >= 0; i--) {
      if (++scanned > 20000) { out.truncated = true; break; }
      const c = calls[i];
      if (failedOnly && c.ok !== false) continue;
      const target = c.target || "";
      const hay = [c.name, target, label].join(" ").toLowerCase();
      if (terms.length && !terms.every((t) => hay.includes(t))) continue;
      out.matched += 1;
      const key = JSON.stringify([a.agent_id, c.name, target]);
      let row = merged.get(key);
      if (!row) {
        row = { agent_id: a.agent_id, description: label, tool: c.name, target, timestamp: c.timestamp,
          ok: c.ok === undefined ? null : c.ok, count: 0, failed: 0 };
        merged.set(key, row);
      }
      row.count += 1;
      if (c.ok === false) row.failed += 1;
    }
  }
  const rows = [...merged.values()].sort((p, q) => (q.timestamp || 0) - (p.timestamp || 0));
  if (rows.length > 300) out.truncated = true;
  out.rows = rows.slice(0, 300);
  return out;
}

function callsCount(res) {
  if (!res.rows.length) return "No tool call matches";
  const n = res.matched;
  let text = n + " " + (res.failed_only ? "failed call" : "call") + (n === 1 ? "" : "s") +
    (res.query ? (n === 1 ? " matches" : " match") : "");
  if (res.truncated) text += " · showing the newest " + res.rows.length + " rows";
  const at = state.callAt;
  if (at !== null && at !== undefined && at >= 0 && at < res.rows.length) text += " · row " + (at + 1) + " of " + res.rows.length;
  return text;
}

function callsHtml(res) {
  if (!res.rows.length) {
    return '<div class="ticker-empty">' + esc(res.failed_only && !res.query ? "No tool call failed in this run."
      : "No tool call matches. Try fewer words, or part of a file name.") + "</div>";
  }
  return res.rows.map((r, i) => {
    const [, cssVar] = toolBucket(r.tool);
    return '<div class="ticker-row call-row' + (i === state.callAt ? " current" : "") + '" data-agent="' + esc(r.agent_id) +
      '" data-i="' + i + '">' +
      '<span class="ticker-time">' + esc(fmtClock(r.timestamp)) + "</span>" +
      '<span class="ticker-dot" style="background:var(--' + cssVar + ')"></span>' +
      '<span class="ticker-agent">' + markTerms(r.description, res.query) + "</span>" +
      '<span class="ticker-tool">' + markTerms(r.tool, res.query) + "</span>" +
      '<span class="ticker-target">' + markTerms(r.target, res.query) + "</span>" +
      (r.count > 1 ? '<span class="call-count">×' + esc(r.count) + "</span>" : "") +
      (r.failed ? '<span class="call-failed">' + esc(r.count > 1 ? r.failed + " failed" : "failed") + "</span>" : "") +
      "</div>";
  }).join("");
}

async function loadCalls() {
  const seq = (state.callSeq = (state.callSeq || 0) + 1);
  if (!activitySearching()) {
    state.callResults = null;
    renderTicker();
    return;
  }
  let res;
  try {
    res = state.offline ? localCalls(state.callQuery, state.callFailed)
      : await api("/api/calls?q=" + encodeURIComponent(String(state.callQuery || "").trim()) + (state.callFailed ? "&failed=1" : ""));
  } catch (err) {
    return;      // a transient failure; the next poll asks again
  }
  if (seq !== state.callSeq) return;      // a newer search has been sent since
  state.callResults = res;
  if (state.callAt >= res.rows.length) state.callAt = -1;
  renderTicker();
}

function renderCalls(box) {
  const res = state.callResults;
  const count = $("calls-count");
  if (!res) {
    box.innerHTML = '<div class="ticker-empty">Searching…</div>';
    if (count) count.textContent = "";
    return;
  }
  box.innerHTML = callsHtml(res);
  if (count) count.textContent = callsCount(res);
  for (const row of box.querySelectorAll(".call-row")) row.onclick = () => openDrawer(row.dataset.agent);
}

function setupActivitySearch() {
  const input = $("calls-q");
  const failed = $("calls-failed");
  if (!input || !failed) return;
  let timer = null;
  const search = () => { state.callQuery = input.value; state.callAt = -1; loadCalls(); };
  input.oninput = () => { clearTimeout(timer); timer = setTimeout(search, 160); };
  input.onkeydown = (event) => {
    if (event.key === "Escape") {
      if (input.value || state.callFailed) {
        event.preventDefault();
        event.stopPropagation();
        input.value = "";
        state.callFailed = false;
        failed.setAttribute("aria-pressed", "false");
        clearTimeout(timer);
        search();
      }
      return;
    }
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (input.value !== (state.callQuery || "")) { clearTimeout(timer); search(); return; }
    const rows = (state.callResults && state.callResults.rows) || [];
    if (!rows.length) return;
    const at = state.callAt === null || state.callAt === undefined ? -1 : state.callAt;
    state.callAt = (at + (event.shiftKey ? -1 : 1) + rows.length) % rows.length;
    renderTicker();
    const row = document.querySelector('#ticker .call-row[data-i="' + state.callAt + '"]');
    if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
  };
  failed.onclick = () => {
    state.callFailed = !state.callFailed;
    failed.setAttribute("aria-pressed", String(state.callFailed));
    state.callAt = -1;
    loadCalls();
  };
}
