"use strict";

const TOKEN = new URLSearchParams(location.search).get("k") || "";
const POLL_MS = 2000;
// While the push stream is healthy, polling is only a safety net (it also
// catches time-based changes no file write announces, like a stall threshold).
const STREAM_POLL_MS = 15000;
const STREAM_DEBOUNCE_MS = 300;
const FLEET_POLL_MS = 4000;

const state = {
  run: null,
  sessionId: new URLSearchParams(location.search).get("session") || "",
  live: true,
  view: "timeline",
  backoff: POLL_MS,
  selected: null,
  // Bumped whenever a new poll loop starts. A loop whose generation is stale
  // discards its result and stops rescheduling, so toggling Live or switching
  // sessions cannot leave two loops running on independent cadences.
  generation: 0,
  // agent_id -> { count }: how many of that agent's tool_calls are already
  // folded into `ticker`, so a re-fetched detail only contributes its tail.
  toolCache: {},
  // Merged tool-call feed across agents, newest first, capped below.
  ticker: [],
  // Free-text filter (matches description/id/type/model/objective) and a
  // status allow-set — empty means "no restriction," not "match nothing."
  filterText: "",
  filterStatuses: new Set(),
  // Which graph edges ("src>dst>kind") have already been drawn at least
  // once. The first render seeds this silently (nothing "just happened" on
  // page load); every edge key added after that is a real event — a file
  // handoff or message just detected between polls — and gets a one-time
  // traveling-dot animation instead of appearing as a plain static line.
  seenEdgeKeys: new Set(),
  graphSeeded: false,
  // Pan/zoom of the Graph view. userSet is false until the person moves it, so a
  // growing run keeps fitting the page instead of drifting out of view.
  graphView: { k: 1, tx: 12, ty: 12, userSet: false, session: "", el: null },
  // Desktop notifications for "something happened while I wasn't watching."
  // notifySeeded guards the same way graphSeeded does: the first poll only
  // records what already exists (a pre-existing failure or an already-ended
  // session is not a new event), so opening the dashboard never fires a
  // burst of notifications for history.
  notifyEnabled: false,
  notifySeeded: false,
  fleet: null,           // last /api/fleet payload
  fleetSeeded: false,    // first fleet poll only records, never notifies
  fleetAttention: {},    // session_id -> kind@since already announced
  fleetTimer: null,
  history: { data: null, selected: [], compare: null, timer: null },
  liveRun: null,         // the newest run from the server; state.run may be a replay of it
  replay: { on: false, t: 0, playing: false, speed: 30, timer: null },
  faviconKey: "",
  soundEnabled: false,   // off by default; turning it on is the click autoplay needs
  soundMemo: null,       // per-session baseline so history never makes noise
  soundFleetMemo: null,
  lastSoundAt: 0,
  soundPrefs: null,      // per-event mute + quiet hours; see normalizeSoundPrefs
  audio: null,
  pill: null,            // the open Picture-in-Picture window, if any
  pillUi: null,          // the elements built inside it
  pulseModel: null,      // what the live strip is showing
  pulseShape: "",        // its structure, so it is rebuilt only when that changes
  stream: null,          // the open EventSource, if any
  streamLive: false,     // true only while that stream is connected
  refreshTimer: null,    // pending debounced refresh after a push
  knownAttention: "",   // kind@since of the last attention already announced
  knownBudget: "",      // last budget state announced (ok / warn / exceeded)
  knownFailedIds: new Set(),
  knownLoopIds: new Set(),
  lastSessionLive: null,
  // agent_id -> { toolCount, tokenTotal } as of the last render, so the Work
  // Floor view can tell "this agent did something since the last poll" apart
  // from "this is just what the run currently looks like." Seeded the same
  // way graphSeeded/notifySeeded are: the first render records history
  // silently, only later deltas earn the one-shot activity pulse.
  floorActivity: {},
  floorSeeded: false,
  floorGroup: "type",     // "type" (by role) or "status" (who needs a look first)
  floorSig: "",           // what the floor last drew; an unchanged floor is not rebuilt
  // agent_id -> its last-seen status and a celebrate-until timestamp (ms),
  // so a live transition into "completed" gets a one-shot jump burst
  // instead of every render re-triggering it.
  agentPrevStatus: {},
  agentCelebrateUntil: {},
  // agent_id -> AgentSprite, the currently-mounted canvas animators. Always
  // stopped before the floor is rebuilt or the tab is left, so a detached
  // canvas never keeps a requestAnimationFrame loop running forever.
  agentSprites: {},
  // Set true by report.py's offline shim. A static report's "running" agent
  // was only running at export time — ticking its clock against the
  // viewer's real wall clock would make an old, safe-to-email snapshot claim
  // an agent has been running for however long it's sat on someone's disk.
  offline: false,
};

const $ = (id) => document.getElementById(id);

function api(path) {
  const join = path.includes("?") ? "&" : "?";
  const session = state.sessionId ? "&session=" + encodeURIComponent(state.sessionId) : "";
  return fetch(path + join + "k=" + encodeURIComponent(TOKEN) + session)
    .then((response) => {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const sign = seconds < 0 ? "-" : "";
  const total = Math.round(Math.abs(seconds));
  const pad = (n) => (n < 10 ? "0" : "") + n;
  if (total < 60) return sign + total + "s";
  if (total < 3600) return sign + Math.floor(total / 60) + "m " + pad(total % 60) + "s";
  return sign + Math.floor(total / 3600) + "h " + pad(Math.floor((total % 3600) / 60)) + "m";
}

// A possible loop, in words, with its evidence. Never a verdict: the same
// pattern is also what legitimate polling looks like.
function loopText(loop) {
  if (!loop) return "";
  const call = (c) => c.tool + (c.target ? " " + c.target : "");
  if (loop.kind === "cycle") {
    return "alternating " + loop.calls.map(call).join(" \u21C4 ") + " over its last " +
      loop.count + " calls";
  }
  return call(loop.calls[0] || loop) + " \u00D7" + loop.count;
}

// Money in the price file's currency. Tiny amounts say "<", not "$0.00", which
// would read as free.
function fmtMoney(amount, currency) {
  if (amount === null || amount === undefined) return "—";
  const symbol = !currency || currency === "USD" ? "$" : currency + " ";
  if (amount > 0 && amount < 0.01) return "<" + symbol + "0.01";
  return symbol + (amount >= 100 ? amount.toFixed(0) : amount.toFixed(2));
}

// A figure priced from the built-in list prices says so (orchestra/listprices.py).
function priceNote(cost) {
  return cost && cost.enabled && cost.source === "list" && cost.as_of ? "list price, as of " + cost.as_of : "";
}

function fmtCount(n) {
  if (n > 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n > 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

function fmtTokens(tokens) {
  return fmtCount(Object.values(tokens || {}).reduce((a, b) => a + b, 0));
}

// Only input-side tokens are cacheable; output is never served from cache,
// so it stays out of the denominator or every ratio would be diluted by
// however chatty the agent's replies happened to be.
function cacheHitRatio(tokens) {
  const t = tokens || {};
  const input = t.input || 0;
  const cacheRead = t.cache_read || 0;
  const cacheCreate = t.cache_create || 0;
  const denom = input + cacheRead + cacheCreate;
  return denom ? cacheRead / denom : null;
}

function fmtPct(ratio) {
  return ratio === null || ratio === undefined ? "—" : Math.round(ratio * 100) + "%";
}

function fmtTokenMix(tokens) {
  const t = tokens || {};
  return "input " + fmtCount(t.input || 0) +
    " · cache read " + fmtCount(t.cache_read || 0) +
    " · cache write " + fmtCount(t.cache_create || 0) +
    " · output " + fmtCount(t.output || 0);
}

// Shared by the drawer's tool-mix bar and the activity ticker: a fixed,
// small taxonomy rather than one swatch per literal tool name (which would
// run to dozens of mcp__* names and make the fingerprint unreadable).
const TOOL_BUCKETS = [
  ["Read", "tool-read", ["Read", "Grep", "Glob", "NotebookRead", "WebFetch", "WebSearch"]],
  ["Edit", "tool-edit", ["Edit", "Write", "NotebookEdit"]],
  ["Bash", "tool-bash", ["Bash"]],
  ["Task", "tool-task", ["Task", "Agent"]],
];
function toolBucket(name) {
  for (const [label, cssVar, names] of TOOL_BUCKETS) {
    if (names.includes(name)) return [label, cssVar];
  }
  return ["Other", "tool-other"];
}

function fmtClock(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

// The full model string ("claude-haiku-4-5-20251001") is the ground truth —
// shown as-is wherever there's room (the drawer). Compact spaces (Timeline
// rows, Graph nodes) get this shortened form instead: strip the "claude-"
// prefix and a trailing release-date suffix, so "sonnet-5" stays a real
// version identifier rather than collapsing back to the bare family name.
function fmtModelShort(model) {
  if (!model) return "?";
  return model.replace(/^claude-/, "").replace(/-\d{8}$/, "");
}

function renderToolMix(toolCalls) {
  if (!toolCalls || !toolCalls.length) return "";
  const counts = { Read: 0, Edit: 0, Bash: 0, Task: 0, Other: 0 };
  for (const call of toolCalls) counts[toolBucket(call.name)[0]] += 1;
  const order = [["Read", "tool-read"], ["Edit", "tool-edit"], ["Bash", "tool-bash"],
    ["Task", "tool-task"], ["Other", "tool-other"]].filter(([label]) => counts[label] > 0);
  const total = toolCalls.length;
  const bars = order.map(([label, cssVar]) =>
    '<span style="width:' + (counts[label] / total * 100) + '%;background:var(--' + cssVar + ')" ' +
    'title="' + esc(label) + " " + counts[label] + '"></span>').join("");
  const legend = order.map(([label, cssVar]) =>
    '<span><span class="swatch" style="background:var(--' + cssVar + ')"></span>' +
    esc(label) + " " + counts[label] + "</span>").join("");
  return '<div class="tool-mix"><div class="tool-mix-bar">' + bars + "</div>" +
    '<div class="tool-mix-legend">' + legend + "</div></div>";
}

// Diffs `toolCalls` against what's already been folded into the ticker for
// this agent, so re-fetching a running agent's growing detail only appends
// its new tail instead of duplicating everything seen so far.
function ingestToolCalls(agentId, label, toolCalls) {
  const calls = toolCalls || [];
  const cached = state.toolCache[agentId] || { count: 0 };
  if (calls.length > cached.count) {
    for (const call of calls.slice(cached.count)) {
      state.ticker.push({ ts: call.timestamp, agentId, label, name: call.name, target: call.target });
    }
    state.ticker.sort((a, b) => (b.ts || 0) - (a.ts || 0));
    if (state.ticker.length > 300) state.ticker.length = 300;
  }
  state.toolCache[agentId] = { count: calls.length };
}

async function refreshTicker(run) {
  // While searching, a live session's results are asked for again on every poll.
  if (typeof activitySearching === "function" && activitySearching()) {
    if (!state.offline) loadCalls();
    return;
  }
  const running = run.agents.filter((a) => a.status === "running");
  for (const agent of running) {
    let detail;
    try {
      detail = await api("/api/agent/" + encodeURIComponent(agent.agent_id));
    } catch (err) {
      continue;  // transient poll failure; the next cycle tries again
    }
    ingestToolCalls(agent.agent_id, agent.description || agent.agent_id, detail.tool_calls);
  }
  renderTicker();
}

function agentById(run) {
  const map = {};
  for (const a of run.agents) map[a.agent_id] = a;
  return map;
}

// Empty text and an empty status set both mean "no restriction" — a filter
// bar that starts out hiding everything would look like the dashboard broke.
function agentMatchesFilter(agent) {
  if (state.filterStatuses.size && !state.filterStatuses.has(agent.status)) return false;
  const q = state.filterText.trim().toLowerCase();
  if (!q) return true;
  const haystack = [agent.description, agent.agent_id, agent.agent_type,
    agent.model, agent.objective].filter(Boolean).join(" ").toLowerCase();
  return haystack.includes(q);
}

function renderFilterChips() {
  const box = $("filter-status");
  if (!box || !state.run) return;
  const order = ["running", "waiting", "completed", "failed", "stalled", "orphaned", "unknown"];
  const present = new Set(state.run.agents.map((a) => a.status));
  const statuses = order.filter((s) => present.has(s));
  box.innerHTML = statuses.map((s) =>
    '<button type="button" class="chip' + (state.filterStatuses.has(s) ? " active" : "") +
    '" data-status="' + s + '">' + esc(s) + "</button>").join("");
  for (const chip of box.querySelectorAll(".chip")) {
    chip.onclick = () => {
      const s = chip.dataset.status;
      if (state.filterStatuses.has(s)) state.filterStatuses.delete(s);
      else state.filterStatuses.add(s);
      renderFilterChips();
      render();
    };
  }
}

// Only shown while a filter is actually narrowing the view — at rest it
// would just duplicate the "N agents" the header already shows.
function renderFilterCount(run) {
  const el = $("filter-count");
  if (!el) return;
  const active = state.filterText.trim() || state.filterStatuses.size;
  if (!active) {
    el.textContent = "";
    return;
  }
  const shown = run.agents.filter(agentMatchesFilter).length;
  el.textContent = shown + " of " + run.agents.length;
}

// ------------------------------------------------------------ notifications
//
// The Notification global doesn't exist in every context this file runs in
// (this project's own headless-node report-render check among them) — every
// access is guarded so a missing API degrades to "no notifications," never
// a thrown error that blanks the page.

function notificationsSupported() {
  return typeof Notification !== "undefined";
}

function notify(title, body) {
  if (!notificationsSupported() || Notification.permission !== "granted") return;
  try {
    const n = new Notification(title, { body: body });
    n.onclick = () => { window.focus(); n.close(); };
  } catch (err) { /* best-effort; never fatal to the dashboard */ }
}

function updateNotifyButton() {
  const btn = $("notify-toggle");
  if (!btn) return;
  btn.setAttribute("aria-pressed", String(state.notifyEnabled));
  btn.textContent = state.notifyEnabled ? "Notify: on" : "Notify";
}

function setStoredNotifyPref(enabled) {
  try {
    if (typeof localStorage !== "undefined") {
      localStorage.setItem("orchestra-notify", enabled ? "1" : "0");
    }
  } catch (err) { /* private-browsing/blocked storage; not worth failing over */ }
}

// Fires for events that happen WHILE the dashboard is open — an agent that
// fails, a session that ends — not for state that already existed when the
// tab was opened (the Health box already shows that at a glance).
function checkNotifications(run) {
  const seeding = !state.notifySeeded;
  const currentlyFailed = new Set(
    run.agents.filter((a) => a.status === "failed").map((a) => a.agent_id));

  if (!seeding && state.notifyEnabled) {
    for (const agent of run.agents) {
      if (agent.status === "failed" && !state.knownFailedIds.has(agent.agent_id)) {
        notify("Agent failed", agent.description || agent.agent_id);
      }
    }
    if (state.lastSessionLive === true && !run.session_live) {
      const t = run.totals || {};
      notify("Cuelight session ended",
        (t.completed || 0) + " completed, " + ((t.failed || 0) + (t.orphaned || 0)) + " failed");
    }
  }

  // A prompt that appears while the tab is open is exactly what a notification
  // is for; one already pending at page load is shown by the banner instead.
  const att = run.live && run.live.attention;
  const attKey = att ? att.kind + "@" + att.since : "";
  if (!seeding && state.notifyEnabled && att && attKey !== state.knownAttention &&
      att.kind !== "idle") {
    notify(attentionTitle(att), att.message || "");
  }
  state.knownAttention = attKey;

  // A newly suspected loop is worth a nudge while the tab is open: it is
  // burning tokens right now. Seeded like failures, so history stays quiet.
  const loopingNow = new Set(run.agents.filter((a) => a.loop).map((a) => a.agent_id));
  if (!seeding && state.notifyEnabled) {
    for (const agent of run.agents) {
      if (agent.loop && !state.knownLoopIds.has(agent.agent_id)) {
        notify("Possible loop", (agent.description || agent.agent_id) + ": " + loopText(agent.loop));
      }
    }
  }
  state.knownLoopIds = loopingNow;

  // Budget: announce crossing INTO warn/exceeded while the tab is open.
  const budget = run.cost && run.cost.budget ? run.cost.budget.state : "";
  if (!seeding && state.notifyEnabled && budget !== state.knownBudget &&
      (budget === "warn" || budget === "exceeded")) {
    notify(budget === "exceeded" ? "Budget exceeded" : "Budget warning",
      costText(run.cost) + " (" + Math.round(run.cost.budget.ratio * 100) + "%)");
  }
  state.knownBudget = budget;

  state.knownFailedIds = currentlyFailed;
  state.lastSessionLive = run.session_live;
  state.notifySeeded = true;
}

// ------------------------------------------------------------- copy summary

// Plain markdown, meant to be pasted straight into a PR description or a
// Slack update — no HTML, no dashboard-specific jargon a reader wouldn't
// already have from opening the linked session.
function buildSummaryMarkdown(run) {
  const t = run.totals || {};
  const failedCount = (t.failed || 0) + (t.orphaned || 0);
  const lines = [
    "## Cuelight summary — " + (run.session_id || "session"),
    (t.agents || 0) + " agents · " + (t.completed || 0) + " completed · " +
      failedCount + " failed · " + (t.running || 0) + " running · " +
      fmtTokens(t.tokens) + " tokens (" + fmtPct(cacheHitRatio(t.tokens)) + " cached) · " +
      fmtDuration(t.wall_time_s) + " wall",
  ];
  if (run.cost && run.cost.enabled) {
    lines.push("Cost: " + costText(run.cost) + (run.cost.partial ? " (partial — no price for " +
      run.cost.unpriced_models.join(", ") + ")" : ""));
  }

  const trouble = run.agents.filter((a) =>
    ["waiting", "stalled", "failed", "orphaned"].includes(a.status));
  const att = run.live && run.live.attention;
  const loops = run.agents.filter((a) => a.loop);
  if (trouble.length || att || loops.length) {
    lines.push("", "### Needs attention");
    if (att) lines.push("- " + attentionTitle(att).toUpperCase() +
      (att.message ? " — " + att.message : ""));
    for (const agent of trouble) {
      lines.push("- " + agent.status.toUpperCase() + " — " + (agent.description || agent.agent_id));
    }
    for (const agent of loops) {
      lines.push("- POSSIBLE LOOP — " + (agent.description || agent.agent_id) + ": " +
        loopText(agent.loop));
    }
  }

  const conflicts = run.write_conflicts || [];
  if (conflicts.length) {
    lines.push("", "### Write conflicts");
    const byId = agentById(run);
    for (const c of conflicts) {
      const labels = c.writer_ids.map((id) => (byId[id] && byId[id].description) || id);
      lines.push("- `" + c.path + "` — written by " + labels.join(", "));
    }
  }

  return lines.join("\n");
}

async function copySummary() {
  const btn = $("copy-summary");
  if (!state.run || !btn) return;
  const text = buildSummaryMarkdown(state.liveRun || state.run);
  let ok = false;
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (err) { /* ok stays false; the button reports it below */ }
  const original = btn.textContent;
  btn.textContent = ok ? "Copied!" : "Copy failed";
  setTimeout(() => { btn.textContent = original; }, 1500);
}

function renderConflicts(run) {
  const box = $("conflicts");
  if (!box) return;
  const conflicts = run.write_conflicts || [];
  if (!conflicts.length) { box.hidden = true; return; }
  box.hidden = false;
  const byId = agentById(run);
  box.innerHTML = "<strong>" + conflicts.length +
    " file(s) written by more than one agent</strong>";
  const list = document.createElement("ul");
  for (const c of conflicts) {
    const item = document.createElement("li");
    const labels = c.writer_ids.map((id) => (byId[id] && byId[id].description) || id);
    item.textContent = c.path + " — written by " + labels.join(", ");
    item.setAttribute("data-kind", "conflict");
    item.onclick = () => openDrawer(c.writer_ids[0]);
    list.appendChild(item);
  }
  box.appendChild(list);
}

function renderTicker() {
  const box = $("ticker");
  if (!box) return;
  // A search (tabs.js) replaces the live tail with every matching call in the run.
  if (typeof activitySearching === "function" && activitySearching()) { renderCalls(box); return; }
  const count = $("calls-count");
  if (count) count.textContent = "";
  const byId = state.run ? agentById(state.run) : {};
  const rows = state.ticker.filter((e) => {
    const agent = byId[e.agentId];
    return !agent || agentMatchesFilter(agent);
  });
  if (!rows.length) {
    box.innerHTML = '<div class="ticker-empty">' + (state.ticker.length
      ? "No activity matches the current filter."
      : state.run && !state.run.session_live
        ? "Nothing is running now. Search above, or press Failed only, to look through every tool call this run made."
        : "No live tool-call activity yet — this fills in while agents are running. Search above to look through every call so far.") +
      "</div>";
    return;
  }
  box.innerHTML = rows.map((e) => {
    const [, cssVar] = toolBucket(e.name);
    return '<div class="ticker-row" data-agent="' + esc(e.agentId) + '">' +
      '<span class="ticker-time">' + esc(fmtClock(e.ts)) + "</span>" +
      '<span class="ticker-dot" style="background:var(--' + cssVar + ')"></span>' +
      '<span class="ticker-agent">' + esc(e.label) + "</span>" +
      '<span class="ticker-tool">' + esc(e.name) + "</span>" +
      '<span class="ticker-target">' + esc(e.target || "") + "</span>" +
      "</div>";
  }).join("");
  for (const row of box.querySelectorAll(".ticker-row")) {
    row.onclick = () => openDrawer(row.dataset.agent);
  }
}

// ------------------------------------------------------------ agent sprite
//
// Sprite slicing + animation is a JS port of the canvas pet engine from
// ntd4996/agentpet (MIT License — https://github.com/ntd4996/agentpet),
// including this file's own copy of that license:
//
//   MIT License
//   Copyright (c) 2026 Nguyễn Thành Đạt
//   Permission is hereby granted, free of charge, to any person obtaining a
//   copy of this software and associated documentation files (the
//   "Software"), to deal in the Software without restriction, including
//   without limitation the rights to use, copy, modify, merge, publish,
//   distribute, sublicense, and/or sell copies of the Software, and to
//   permit persons to whom the Software is furnished to do so, subject to
//   the following conditions: the above copyright notice and this
//   permission notice shall be included in all copies or substantial
//   portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT
//   WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
//   THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
//   NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
//   LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
//   OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
//   WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
//
// static/agent-sprite.png is that repo's own bundled sample character sheet
// (landing/sample-pet.png), reused here under the same license — served
// locally, never fetched from their CDN, so the dashboard's offline
// guarantee holds. report.py embeds it as a data: URI for the same reason
// a static report can't reference a sibling file it isn't shipped with.
//
// It's one character sheet, not a gallery — agentpet's full character
// variety lives on their CDN, which a fully offline dashboard can't reach.
// Every agent instead gets its own hue-rotated tint of the same sheet (see
// agentHue/AGENT_TINT below), so agents still read as visually distinct
// without needing more art.
const AGENT_SPRITE_URL = (typeof window !== "undefined" && window.ORCHESTRA_AGENT_SPRITE) ||
  "agent-sprite.png";
const SPRITE_COLS = 8, SPRITE_ROWS = 9, SPRITE_ALPHA_THRESHOLD = 16;
const SPRITE_REDUCED_MOTION = typeof window !== "undefined" && window.matchMedia &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Contiguous runs of `true` in an occupancy array → [start, end) pairs.
function spriteSegments(occ) {
  const out = [];
  let start = -1;
  for (let i = 0; i < occ.length; i++) {
    if (occ[i] && start < 0) start = i;
    else if (!occ[i] && start >= 0) { out.push([start, i]); start = -1; }
  }
  if (start >= 0) out.push([start, occ.length]);
  return out;
}

// Alpha-gutter slice: rows by transparent bands, then frames within each
// row, so a sheet with a different frame count per row (this one: 6, 8, 8,
// 4, 5, 8, 6, 6, 6) still slices correctly instead of assuming a fixed grid.
function sliceSpriteSheet(img) {
  const w = img.naturalWidth, h = img.naturalHeight;
  if (!w || !h) return [];
  const cv = document.createElement("canvas");
  cv.width = w; cv.height = h;
  const ctx = cv.getContext("2d", { willReadFrequently: true });
  if (!ctx) return [];
  ctx.drawImage(img, 0, 0);
  let data;
  try {
    data = ctx.getImageData(0, 0, w, h).data;
  } catch (err) {
    return []; // caller falls back to a fixed SPRITE_COLS x SPRITE_ROWS grid
  }
  const rowHas = new Uint8Array(h);
  for (let y = 0; y < h; y++) {
    const off = y * w * 4;
    for (let x = 0; x < w; x++) {
      if (data[off + x * 4 + 3] > SPRITE_ALPHA_THRESHOLD) { rowHas[y] = 1; break; }
    }
  }
  const clips = [];
  for (const [y0, y1] of spriteSegments(rowHas)) {
    const colHas = new Uint8Array(w);
    for (let y = y0; y < y1; y++) {
      const off = y * w * 4;
      for (let x = 0; x < w; x++) {
        if (data[off + x * 4 + 3] > SPRITE_ALPHA_THRESHOLD) colHas[x] = 1;
      }
    }
    const clip = spriteSegments(colHas).map(([x0, x1]) => ({ x: x0, y: y0, w: x1 - x0, h: y1 - y0 }));
    if (clip.length) clips.push(clip);
  }
  return clips;
}

// One decode + slice, shared by every agent card instead of one per agent.
class AgentSpriteSheet {
  constructor(url) {
    this.img = null;
    this.clips = [];
    this.clipMaxW = [];
    this._onReady = [];
    // No Image constructor (e.g. the report-renderer's headless DOM stub in
    // tests, which deliberately exposes only what it wires up): stay blank
    // forever rather than throw during module load, same as a real failed
    // image load would leave `img` unset below.
    if (typeof Image === "undefined") return;
    const img = new Image();
    img.onload = () => {
      this.img = img;
      try { this.clips = sliceSpriteSheet(img); } catch (err) { this.clips = []; }
      this.clipMaxW = this.clips.map((clip) => Math.max(...clip.map((r) => r.w)));
      for (const cb of this._onReady) cb();
      this._onReady = [];
    };
    img.onerror = () => { this._onReady = []; }; // stays blank; draw() no-ops
    img.src = url;
  }
  onReady(cb) {
    if (this.img) cb(); else this._onReady.push(cb);
  }
  clipFor(row) {
    return this.clips.length ? this.clips[Math.min(row, this.clips.length - 1)] : null;
  }
}

const agentSpriteSheet = new AgentSpriteSheet(AGENT_SPRITE_URL);

// Row + fps per Orchestra status, the same idea as agentpet's STATE_ROW /
// STATE_FPS mapped onto the six statuses Orchestra actually has. Sheet rows,
// top to bottom: 0 Idle, 1 RunRight, 2 RunLeft, 3 Waving, 4 Jumping,
// 5 Failed, 6 Waiting, 7 Running, 8 Review.
const AGENT_SPRITE_STATE = {
  running: { row: 7, fps: 8 },
  completed: { row: 3, fps: 3 },
  failed: { row: 5, fps: 3 },
  stalled: { row: 6, fps: 4 },
  waiting: { row: 6, fps: 3 },
  orphaned: { row: 0, fps: 2 },
  unknown: { row: 0, fps: 2 },
};
// A brief jumping burst the moment an agent finishes — agentpet's "celebrate"
// mood, fired once on the transition into "completed," not on every render.
const AGENT_CELEBRATE_STATE = { row: 4, fps: 8 };
const AGENT_CELEBRATE_MS = 3000;

// A stable hue per agent, purely a function of its id so the same agent
// keeps the same tint across every re-render instead of flickering colors.
function agentHue(id) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return h % 360;
}

class AgentSprite {
  constructor(canvas, sheet) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.ctx.imageSmoothingEnabled = false;
    this.sheet = sheet;
    this.row = 0;
    this.fps = 3;
    this.frame = 0;
    this.lastTick = 0;
    this.stopped = false;
    sheet.onReady(() => { if (!this.stopped) this.draw(); });
    this._raf = requestAnimationFrame((t) => this.loop(t));
  }
  setState(row, fps) {
    if (row !== this.row) { this.row = row; this.frame = 0; }
    this.fps = fps;
    if (this.sheet.img) this.draw();
  }
  stop() {
    this.stopped = true;
    cancelAnimationFrame(this._raf);
  }
  loop(t) {
    if (this.stopped) return;
    // Reduced motion: hold the current frame (still the right pose for the
    // status) instead of cycling — motion stops, the state stays legible.
    if (!SPRITE_REDUCED_MOTION && this.sheet.img && t - this.lastTick > 1000 / this.fps) {
      this.lastTick = t;
      this.frame++;
      this.draw();
    }
    this._raf = requestAnimationFrame((n) => this.loop(n));
  }
  draw() {
    const ctx = this.ctx;
    const W = this.canvas.width, H = this.canvas.height;
    ctx.clearRect(0, 0, W, H);
    const img = this.sheet.img;
    if (!img) return;
    const clip = this.sheet.clipFor(this.row);
    let r, scaleW;
    if (clip) {
      r = clip[this.frame % clip.length];
      scaleW = this.sheet.clipMaxW[Math.min(this.row, this.sheet.clips.length - 1)] || r.w;
    } else {
      const fw = img.naturalWidth / SPRITE_COLS, fh = img.naturalHeight / SPRITE_ROWS;
      if (!fw || !fh) return;
      r = { x: (this.frame % SPRITE_COLS) * fw, y: Math.min(this.row, SPRITE_ROWS - 1) * fh, w: fw, h: fh };
      scaleW = fw;
    }
    // Integer scale keeps pixel art crisp; anchored bottom-center so every
    // frame's feet stay put even as frame widths vary within a row.
    const fit = Math.min(W / scaleW, H / r.h);
    const s = fit >= 1 ? Math.floor(fit) : fit;
    const dw = r.w * s, dh = r.h * s;
    ctx.drawImage(img, r.x, r.y, r.w, r.h, (W - dw) / 2, H - dh, dw, dh);
  }
}

function agentTokenTotal(tokens) {
  return Object.values(tokens || {}).reduce((a, b) => a + b, 0);
}

function stopAgentSprites() {
  for (const sprite of Object.values(state.agentSprites)) sprite.stop();
  state.agentSprites = {};
}

// The agent's own subagent type ("general-purpose", "code-reviewer",
// "Explore", "honeycomb:honeycomb-investigator", ...), turned into a section
// label: last segment of a namespaced type, hyphens/underscores as spaces,
// title case. Falls back to "Ungrouped" for an inline launch with no type.
function humanizeAgentType(agentType) {
  const raw = (agentType || "").trim();
  if (!raw) return "Ungrouped";
  const last = raw.includes(":") ? raw.slice(raw.lastIndexOf(":") + 1) : raw;
  return last.replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Who needs a look first. Used to sort a group and to order the "by status" floor.
const FLOOR_STATUS_ORDER = ["failed", "stalled", "waiting", "orphaned", "running", "completed", "unknown"];
const FLOOR_STATUS_LABEL = { failed: "Failed", stalled: "Stalled", waiting: "Waiting on you", orphaned: "Orphaned",
  running: "Running", completed: "Completed", unknown: "Unknown" };

function floorRank(agent) {
  const at = FLOOR_STATUS_ORDER.indexOf(agent.status);
  return at < 0 ? FLOOR_STATUS_ORDER.length : at;
}

// What a tool call acted on, short enough for a card: a file by its name, anything
// else (a command, a query, a URL) as written.
function toolTargetLabel(tool) {
  if (!tool || !tool.target) return "";
  return ["Read", "Write", "Edit", "NotebookEdit", "NotebookRead"].indexOf(tool.name) >= 0
    ? baseName(tool.target) : tool.target;
}

// The same comb the timeline draws, as a sparkline: where the agent was busy.
function sparkHtml(bins) {
  if (!bins || !bins.length) return "";
  const peak = Math.max.apply(null, bins);
  return '<span class="spark" aria-hidden="true">' + bins.map((b) =>
    '<i' + (b ? "" : ' class="z"') + ' style="height:' + (b ? Math.max(22, Math.round((b / peak) * 100)) : 8) + '%"></i>').join("") +
    "</span>";
}

function floorSignature(run, celebrating) {
  return JSON.stringify([state.floorGroup, state.filterText, Array.from(state.filterStatuses),
    state.offline, celebrating,
    run.agents.map((a) => [a.agent_id, a.status, a.tool_call_count, agentTokenTotal(a.tokens), a.ended_at,
      a.last_tool ? a.last_tool.name + a.last_tool.target : "", (a.activity || []).join(","),
      agentMatchesFilter(a)])]);
}

function renderWorkfloor(run) {
  const box = $("workfloor");
  if (!box) return;
  if (!run.agents.length) {
    stopAgentSprites();
    box.innerHTML = '<div class="ticker-empty">No agents in this session yet.</div>';
    state.floorSig = "";
    return;
  }
  // Nothing visible changed since the last render: leave the DOM and the running
  // sprites alone. (Rebuilding every few seconds restarted every animation.)
  const celebrating = Object.keys(state.agentCelebrateUntil)
    .filter((id) => state.agentCelebrateUntil[id] > Date.now()).sort();
  const signature = floorSignature(run, celebrating);
  if (signature === state.floorSig && box.innerHTML) return;
  state.floorSig = signature;
  stopAgentSprites();

  const now = Date.now() / 1000;
  const seeded = state.floorSeeded;
  const nextActivity = {};
  const spriteStates = {}; // agent_id -> {row, fps}, resolved here so the DOM pass below just wires canvases

  // Group by role (agent_type) or by status. Roles sort by label, "Ungrouped" (inline
  // launches with no declared type) last since it is a catch-all; statuses sort by
  // who needs a look first. Inside a group the agents that need attention come first.
  const byStatus = state.floorGroup === "status";
  const groups = new Map(); // label -> agents[]
  for (const agent of run.agents) {
    const label = byStatus ? (FLOOR_STATUS_LABEL[agent.status] || "Unknown") : humanizeAgentType(agent.agent_type);
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(agent);
  }
  for (const list of groups.values()) {
    list.sort((a, b) => (floorRank(a) - floorRank(b)) || ((a.started_at || 0) - (b.started_at || 0)));
  }
  const statusLabels = FLOOR_STATUS_ORDER.map((s) => FLOOR_STATUS_LABEL[s]);
  const groupLabels = Array.from(groups.keys()).sort((a, b) => {
    if (byStatus) return statusLabels.indexOf(a) - statusLabels.indexOf(b);
    if (a === "Ungrouped") return 1;
    if (b === "Ungrouped") return -1;
    return a.localeCompare(b);
  });

  function renderAgentCard(agent) {
    const id = agent.agent_id;
    const status = agent.status || "unknown";
    const toolCount = agent.tool_call_count || 0;
    const tokenTotal = agentTokenTotal(agent.tokens);
    const prev = state.floorActivity[id];
    // Only a real increase since a *previous* render counts as activity —
    // an agent's first appearance (tab just opened, or it just spawned) is
    // history/creation, not something that "just happened."
    const pulse = seeded && prev &&
      (toolCount > prev.toolCount || tokenTotal > prev.tokenTotal);
    nextActivity[id] = { toolCount, tokenTotal };

    // A live transition into "completed" (not just being completed already
    // when the tab opens) earns a one-shot celebrate burst.
    const prevStatus = state.agentPrevStatus[id];
    if (seeded && prevStatus && prevStatus !== "completed" && status === "completed") {
      state.agentCelebrateUntil[id] = Date.now() + AGENT_CELEBRATE_MS;
    }
    state.agentPrevStatus[id] = status;
    const celebrate = (state.agentCelebrateUntil[id] || 0) > Date.now();
    spriteStates[id] = celebrate ? AGENT_CELEBRATE_STATE : (AGENT_SPRITE_STATE[status] || AGENT_SPRITE_STATE.unknown);

    // Same "open round" test Timeline uses for its dashed bar-open bars:
    // stalled and orphaned agents have gone quiet, but their round never
    // formally ended, so they still deserve a ticking clock, not a "—".
    const live = agent.started_at !== null && agent.ended_at === null;
    // A static report has no "now" — ticking against the viewer's wall
    // clock would make an old snapshot claim a stale run is still live.
    // last_activity_at is the report's own idea of "as of," already baked
    // into the payload, same fallback Timeline uses for an open bar's end.
    const asOf = state.offline ? (agent.last_activity_at || agent.started_at) : now;
    const label = agent.description || agent.agent_id;
    const clockText = live ? fmtDuration(asOf - agent.started_at) : fmtDuration(agent.duration_s);
    const tool = agent.last_tool;
    const doing = tool ? tool.name + " " + toolTargetLabel(tool) : "";
    const ariaLabel = label + ", " + status + ", " + fmtTokens(agent.tokens) + " tokens, " +
      (live ? clockText + " so far" : clockText + " total") + (doing ? ", last " + doing : "");
    const quiet = status === "orphaned" || status === "unknown";

    return '<div class="agent-card' + (agentMatchesFilter(agent) ? "" : " agent-dim") +
      (quiet ? " agent-quiet" : "") + '" data-agent="' + esc(id) + '" data-status="' + esc(status) + '"' +
      (live && !state.offline ? ' data-live="1" data-started="' + agent.started_at + '"' : "") +
      ' role="group" tabindex="0" aria-label="' + esc(ariaLabel) + '">' +
      '<div class="agent-stage' + (pulse ? " pulse" : "") + '">' +
        '<canvas class="agent-canvas bob" data-agent="' + esc(id) + '" width="60" height="68"></canvas>' +
      '</div>' +
      '<div class="agent-name" title="' + esc(label) + '">' + esc(label) + '</div>' +
      '<div class="agent-meta">' + esc(agent.agent_type + " · " + fmtModelShort(agent.model)) + '</div>' +
      '<div class="agent-line"><span class="agent-status s-' + esc(status) + '">' + esc(status) + '</span>' +
        '<span class="agent-clock">' + esc(clockText) + '</span></div>' +
      '<div class="agent-now" title="' + esc(doing) + '">' + (tool
        ? '<b>' + esc(tool.name) + '</b> ' + esc(toolTargetLabel(tool)) : '<span class="agent-idle">no tool calls yet</span>') + '</div>' +
      sparkHtml(agent.activity) +
      '<div class="agent-tokens" title="' + esc(fmtTokenMix(agent.tokens)) + '">' +
        esc(fmtTokens(agent.tokens)) + ' tok · ' + toolCount + (toolCount === 1 ? ' call' : ' calls') + '</div>' +
    '</div>';
  }

  const spot = noteFocus(box);
  box.innerHTML = groupLabels.map((label) => {
    const agents = groups.get(label);
    const groupTokens = agents.reduce((sum, a) => sum + agentTokenTotal(a.tokens), 0);
    return '<section class="floor-group">' +
      '<div class="floor-group-header"><h3>' + esc(label) + '</h3>' +
        '<span class="floor-group-meta">' + agents.length +
        (agents.length === 1 ? " agent · " : " agents · ") +
        esc(fmtCount(groupTokens)) + ' tok</span></div>' +
      '<div class="floor-group-grid">' + agents.map(renderAgentCard).join("") + '</div>' +
    '</section>';
  }).join("");

  state.floorActivity = nextActivity;
  state.floorSeeded = true;

  for (const canvas of box.querySelectorAll(".agent-canvas")) {
    const id = canvas.dataset.agent;
    const sprite = new AgentSprite(canvas, agentSpriteSheet);
    sprite.setState(spriteStates[id].row, spriteStates[id].fps);
    // Same character, a unique per-agent hue so a crowded floor still reads
    // as individuals rather than one sprite copy-pasted everywhere.
    canvas.style.filter = "hue-rotate(" + agentHue(id) + "deg)";
    state.agentSprites[id] = sprite;
  }
  for (const card of box.querySelectorAll(".agent-card")) {
    card.onclick = () => openDrawer(card.dataset.agent);
    card.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openDrawer(card.dataset.agent);
      }
    };
  }
  restoreFocus(box, spot);
}

// Elapsed time for a still-running agent should visibly tick like Claude
// Code's own live timer, without waiting on the 2s data poll for it — a
// cheap local interval that only touches DOM text, never re-renders.
function tickAgentClocks() {
  tickTransport();
  tickPulse();
  tickWaits();
  if (state.view !== "workfloor" || state.offline) return;
  const now = Date.now() / 1000;
  for (const card of document.querySelectorAll('.agent-card[data-live="1"]')) {
    const started = parseFloat(card.dataset.started);
    if (isNaN(started)) continue;
    const clockEl = card.querySelector(".agent-clock");
    if (clockEl) clockEl.textContent = fmtDuration(now - started);
  }
}

function svgEl(name, attrs, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const key in attrs) node.setAttribute(key, attrs[key]);
  if (text !== undefined) node.textContent = text;
  return node;
}

// ------------------------------------------------------------ text fitting
//
// SVG has no text-overflow: ellipsis, and character-count truncation lies as
// soon as a label mixes wide and narrow glyphs. Measuring against a real
// canvas context is the only way to guarantee a label never crosses the
// boundary it's drawn against.

let measureCtx = null;
function bodyFont(px) {
  // getComputedStyle is absent in some minimal JS execution contexts (this
  // project's own headless-node report-render check among them) — fall back
  // to a generic stack rather than throw and blank the whole report.
  const family = typeof getComputedStyle === "function"
    ? getComputedStyle(document.body).fontFamily
    : "sans-serif";
  return px + "px " + family;
}
function textWidth(text, font) {
  if (measureCtx === null) {
    const canvas = document.createElement("canvas");
    measureCtx = (canvas.getContext && canvas.getContext("2d")) || false;
  }
  if (!measureCtx) {
    // No canvas 2D context (the project's own headless-node report-render
    // check, e.g.) — a rough per-character estimate beats throwing.
    const px = parseInt(font, 10) || 11;
    return text.length * px * 0.55;
  }
  measureCtx.font = font;
  return measureCtx.measureText(text).width;
}
function fitText(text, maxWidth, font) {
  if (textWidth(text, font) <= maxWidth) return text;
  let lo = 0, hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (textWidth(text.slice(0, mid) + "…", font) <= maxWidth) lo = mid;
    else hi = mid - 1;
  }
  return lo > 0 ? text.slice(0, lo) + "…" : "…";
}

// ---------------------------------------------------------------- header

// A transport-style clock: HH:MM:SS, so the run reads like a recording.
function fmtTimecode(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return "--:--:--";
  const total = Math.max(0, Math.floor(seconds));
  const pad = (n) => (n < 10 ? "0" : "") + n;
  return pad(Math.floor(total / 3600)) + ":" + pad(Math.floor((total % 3600) / 60)) + ":" + pad(total % 60);
}

function transportSeconds(run) {
  if (run.replay_at !== undefined) return run.replay_at - (run.started_at || run.replay_at);
  if (run.session_live && !state.offline && run.started_at) return Date.now() / 1000 - run.started_at;
  const wall = run.totals ? run.totals.wall_time_s : null;
  return wall === undefined ? null : wall;
}

function tickTransport() {
  const el = $("transport-time");
  if (el && state.run) el.textContent = fmtTimecode(transportSeconds(state.run));
}

const STAT_DOTS = ["running", "waiting", "done", "failed"];

// Claude Code's own "while you were away" recap of this session, one line until clicked.
// A recap describes the moment it was written, so say when that was, and say so plainly
// once the session has worked since.
function recapHtml(about) {
  if (!about || !about.recap) return "";
  return '<span class="recap-label">Recap' + (about.recap_at ? " · " + esc(fleetAgo(about.recap_at)) : "") +
    "</span>" + '<span class="recap-text">' + esc(about.recap) + "</span>" +
    (about.recap_stale ? '<span class="recap-stale">older than the latest activity</span>' : "");
}

function renderRecap(run) {
  const bar = $("recap");
  if (!bar) return;
  const html = recapHtml(run.session);
  bar.hidden = !html;
  if (!html) return;
  if (bar.dataset.html !== html) {       // re-render only when it changed, keeping the open state
    bar.dataset.html = html;
    bar.innerHTML = html;
  }
  if (!bar.onclick) {
    bar.onclick = () => {
      const open = bar.getAttribute("aria-expanded") !== "true";
      bar.setAttribute("aria-expanded", open ? "true" : "false");
    };
  }
}

function renderHeader(run) {
  const t = run.totals;
  const box = $("totals");
  box.innerHTML = "";
  const replaying = run.replay_at !== undefined;
  const live = run.session_live && !replaying && !state.offline;
  const add = (key, value, label, cls, valueId) => {
    const span = document.createElement("span");
    span.className = cls || "";
    span.setAttribute("data-k", key);
    span.setAttribute("data-n", String(value));
    // key, value and label are program-made (counts, fmt* output, fixed words),
    // never transcript text, so they are safe to concatenate into markup.
    span.innerHTML = "<strong" + (valueId ? ' id="' + valueId + '"' : "") + ">" + value +
      "</strong><em>" + (STAT_DOTS.indexOf(key) >= 0 ? '<i class="dot"></i>' : "") + label + "</em>";
    box.appendChild(span);
  };
  add("time", fmtTimecode(transportSeconds(run)),
    live ? "live" : replaying ? "replay" : "wall time",
    "transport" + (live ? " is-live" : ""), "transport-time");
  add("agents", t.agents, "agents");
  add("running", t.running, "running");
  if (t.waiting) add("waiting", t.waiting, "waiting");
  add("done", t.completed, "done");
  add("failed", t.failed + t.orphaned, "failed");
  add("tokens", replaying ? "\u2014" : fmtTokens(t.tokens), "tokens");
  if (run.orchestrator) {
    // The per-agent "tokens" above exclude the orchestrator; say so, don't hide it.
    add("orchestrator", fmtTokens(run.orchestrator.tokens), "orchestrator");
  }
  add("cached", replaying ? "\u2014" : fmtPct(cacheHitRatio(t.tokens)), "cached");
  renderCostPart(run);
  $("conn").textContent = run.session_live ? "" : "session ended";
}

// ------------------------------------------------------------- live pulse strip
//
// A ticker for the run: how many agents are working, how fast tool calls land,
// how many fresh tokens have been spent, and what just happened. Every line is a
// real series from `insights.pulse`; nothing is smoothed or estimated, and the
// y-axis always starts at zero so a small wobble never looks like a crash.

const PULSE_W = 232;
const PULSE_H = 46;
const PULSE_PAD = 3;

// Line, area and last point for evenly spaced values.
function sparkGeometry(values, width, height, pad) {
  const n = values.length;
  if (!n) return null;
  const top = Math.max(1, Math.max.apply(null, values));
  const x = (i) => (n === 1 ? width : (i / (n - 1)) * width);
  const y = (v) => height - pad - (v / top) * (height - 2 * pad);
  let line = "";
  for (let i = 0; i < n; i++) line += (i ? "L" : "M") + x(i).toFixed(1) + " " + y(values[i]).toFixed(1);
  const area = line + "L" + x(n - 1).toFixed(1) + " " + height + "L" + x(0).toFixed(1) + " " + height + "Z";
  return { line: line, area: area, last: [x(n - 1), y(values[n - 1])], top: top };
}

// The same for a step series [[time, level], ...] over a time window.
function stepGeometry(series, start, end, width, height, pad) {
  if (!series || !series.length) return null;
  const span = Math.max(end - start, 1);
  const top = Math.max(1, Math.max.apply(null, series.map((p) => p[1])));
  const x = (t) => Math.min(width, Math.max(0, ((t - start) / span) * width));
  const y = (v) => height - pad - (v / top) * (height - 2 * pad);
  let line = "M" + x(series[0][0]).toFixed(1) + " " + y(0).toFixed(1);
  let level = 0;
  for (const point of series) {
    line += "L" + x(point[0]).toFixed(1) + " " + y(level).toFixed(1);
    level = point[1];
    line += "L" + x(point[0]).toFixed(1) + " " + y(level).toFixed(1);
  }
  line += "L" + width + " " + y(level).toFixed(1);
  const area = line + "L" + width + " " + height + "L" + x(series[0][0]).toFixed(1) + " " + height + "Z";
  return { line: line, area: area, last: [width, y(level)], top: top };
}

// The level a step series held at time t (0 before its first point).
function levelAt(series, t) {
  let level = 0;
  for (const point of series || []) {
    if (point[0] > t) break;
    level = point[1];
  }
  return level;
}

function pulseDelta(now, before, format) {
  const diff = now - before;
  if (diff === 0) return { dir: "flat", text: "no change" };
  const amount = format ? format(Math.abs(diff)) : String(Math.abs(diff));
  return { dir: diff > 0 ? "up" : "down", text: (diff > 0 ? "+" : "−") + amount };
}

// The strip's content as plain data, so it can be tested without a page.
// Returns null when there is nothing to chart (no agent yet, a replay, or an
// older server that does not send the series).
function pulseModel(run) {
  const pulse = run && run.insights && run.insights.pulse;
  if (!pulse || run.replay_at !== undefined) return null;
  const par = run.insights.parallelism;
  const series = (par && par.series) || [];
  const t = run.totals;
  const running = t.running + (t.waiting || 0);
  const ago = pulse.now - pulse.rate.window_s;
  const tokensTotal = pulse.tokens.length ? pulse.tokens[pulse.tokens.length - 1] : 0;
  const issues = t.failed + t.orphaned + t.stalled + (t.waiting || 0);
  return {
    live: pulse.live,
    start: pulse.start,
    end: pulse.end,
    window: pulse.end - pulse.start,
    tiles: [
      { key: "running", label: "Agents running", value: String(running),
        delta: pulseDelta(running, levelAt(series, ago)), note: "vs 1 min ago",
        geo: stepGeometry(series, pulse.start, pulse.end, PULSE_W, PULSE_H, PULSE_PAD) },
      { key: "calls", label: "Tool calls per minute", value: String(pulse.rate.calls_last),
        delta: pulseDelta(pulse.rate.calls_last, pulse.rate.calls_prev), note: "vs the minute before",
        geo: sparkGeometry(pulse.calls, PULSE_W, PULSE_H, PULSE_PAD) },
      { key: "tokens", label: "Fresh tokens spent", value: fmtCount(tokensTotal),
        delta: pulseDelta(pulse.rate.tokens_last, pulse.rate.tokens_prev, fmtCount), note: "per minute, vs before",
        geo: sparkGeometry(pulse.tokens, PULSE_W, PULSE_H, PULSE_PAD) },
    ],
    health: { issues: issues, failed: t.failed + t.orphaned, stalled: t.stalled, waiting: t.waiting || 0 },
    marks: pulse.markers.slice(-8).reverse(),
  };
}

const PULSE_MARK = {
  start: { glyph: "▶", word: "started" },
  done: { glyph: "✓", word: "finished" },
  fail: { glyph: "✕", word: "failed" },
  stall: { glyph: "◷", word: "went quiet" },
};

function pulseTileHtml(tile) {
  const geo = tile.geo;
  const delta = tile.delta;
  const arrow = delta.dir === "up" ? "▲" : delta.dir === "down" ? "▼" : "▬";
  const label = tile.label + ": " + tile.value + ", " +
    (delta.dir === "flat" ? "no change" : delta.text + " " + tile.note);
  const chart = geo
    ? '<svg class="pulse-chart" viewBox="0 0 ' + PULSE_W + " " + PULSE_H + '" preserveAspectRatio="none" role="img" aria-label="' +
      esc(label) + '"><path class="pulse-area" d="' + geo.area + '"/><path class="pulse-line" d="' + geo.line + '"/></svg>' +
      '<i class="pulse-dot" style="left:' + ((geo.last[0] / PULSE_W) * 100).toFixed(1) + "%;top:" +
      ((geo.last[1] / PULSE_H) * 100).toFixed(1) + '%"></i><i class="pulse-cross" hidden></i>'
    : "";
  return '<div class="pulse-tile" data-k="' + tile.key + '" data-live="' + (tile.live ? 1 : 0) + '">' +
    '<div class="pulse-top"><span class="pulse-label">' + esc(tile.label) + "</span>" +
    '<span class="pulse-delta is-' + delta.dir + '" title="' + esc(tile.note) + '"><b aria-hidden="true">' + arrow +
    "</b> " + esc(delta.text) + "</span></div>" +
    '<div class="pulse-value" data-v="' + esc(tile.value) + '">' + esc(tile.value) + "</div>" +
    '<div class="pulse-plot">' + chart + '<span class="pulse-tip" hidden></span></div></div>';
}

function pulseHealthHtml(health) {
  const part = (n, word, cls) => (n ? '<span class="pulse-pill ' + cls + '"><i></i>' + n + " " + word + "</span>" : "");
  const body = health.issues
    ? part(health.failed, "failed", "p-failed") + part(health.stalled, "stalled", "p-stalled") +
      part(health.waiting, "waiting on you", "p-waiting")
    : '<span class="pulse-clear">All clear</span>';
  return '<div class="pulse-tile pulse-health" data-k="health"><div class="pulse-top"><span class="pulse-label">Needs you</span></div>' +
    '<div class="pulse-value" data-v="' + health.issues + '">' + health.issues + '</div>' +
    '<div class="pulse-pills">' + body + "</div></div>";
}

function pulseTapeHtml(marks) {
  if (!marks.length) return "";
  const chips = marks.map((m) => {
    const info = PULSE_MARK[m.kind] || PULSE_MARK.start;
    return '<button type="button" class="pulse-chip is-' + m.kind + '" data-agent="' + esc(m.agent_id) + '" title="' +
      esc(m.label + " " + info.word) + '"><i aria-hidden="true">' + info.glyph + '</i><span>' + esc(m.label) + "</span>" +
      '<time data-t="' + m.t + '"></time></button>';
  });
  return '<div class="pulse-tape" aria-label="Recent events">' + chips.join("") + "</div>";
}

function pulseCollapsed() {
  try { return typeof localStorage !== "undefined" && localStorage.getItem("cuelight-pulse") === "closed"; }
  catch (err) { return false; }
}

function setPulseCollapsed(closed) {
  try { if (typeof localStorage !== "undefined") localStorage.setItem("cuelight-pulse", closed ? "closed" : "open"); }
  catch (err) { /* private window: the choice just does not persist */ }
}

// The tile's chart, value and arrow, refreshed without rebuilding the tile.
function updatePulseTile(el, tile, live) {
  const geo = tile.geo;
  if (geo) {
    const area = el.querySelector(".pulse-area");
    const line = el.querySelector(".pulse-line");
    const dot = el.querySelector(".pulse-dot");
    if (area) area.setAttribute("d", geo.area);
    if (line) line.setAttribute("d", geo.line);
    if (dot) {
      dot.style.left = ((geo.last[0] / PULSE_W) * 100).toFixed(1) + "%";
      dot.style.top = ((geo.last[1] / PULSE_H) * 100).toFixed(1) + "%";
    }
    const chart = el.querySelector(".pulse-chart");
    if (chart) {
      chart.setAttribute("aria-label", tile.label + ": " + tile.value + ", " +
        (tile.delta.dir === "flat" ? "no change" : tile.delta.text + " " + tile.note));
    }
  }
  const value = el.querySelector(".pulse-value");
  const was = value.getAttribute("data-v");
  if (was !== tile.value) {
    value.textContent = tile.value;
    value.setAttribute("data-v", tile.value);
    // A value that moved flashes once: a cue that it is live, never a sound or a loop.
    if (live) {
      value.classList.add("is-tick");
      setTimeout(() => value.classList.remove("is-tick"), 700);
    }
  }
  const delta = el.querySelector(".pulse-delta");
  const arrow = tile.delta.dir === "up" ? "▲" : tile.delta.dir === "down" ? "▼" : "▬";
  delta.className = "pulse-delta is-" + tile.delta.dir;
  delta.innerHTML = '<b aria-hidden="true">' + arrow + "</b> " + esc(tile.delta.text);
}

function renderPulse(run) {
  const box = $("pulse");
  if (!box) return;
  const model = pulseModel(run);
  state.pulseModel = model;
  if (!model) { box.hidden = true; box.innerHTML = ""; state.pulseShape = ""; return; }
  box.hidden = false;
  const closed = pulseCollapsed();
  // What is on the page, apart from the numbers inside each chart. The charts
  // are updated in place on every poll so the live dot keeps pulsing instead of
  // restarting; the strip is rebuilt only when its shape really changes.
  const shape = JSON.stringify([closed, model.live, model.tiles.map((t) => !!t.geo), model.health, model.marks]);
  const sub = (model.live ? '<i class="pulse-live" aria-hidden="true"></i>live · ' : "") +
    "last " + esc(fmtDuration(model.window));
  if (shape !== state.pulseShape) {
    state.pulseShape = shape;
    const head = '<div class="pulse-head"><span class="pulse-title">Pulse</span><span class="pulse-sub">' + sub +
      '</span><button type="button" class="pulse-toggle" aria-expanded="' + (closed ? "false" : "true") + '">' +
      (closed ? "Show" : "Hide") + "</button></div>";
    let body = "";
    if (!closed) {
      for (const tile of model.tiles) tile.live = model.live;
      body = '<div class="pulse-tiles">' + model.tiles.map(pulseTileHtml).join("") + pulseHealthHtml(model.health) +
        "</div>" + pulseTapeHtml(model.marks);
    }
    box.className = "pulse" + (closed ? " is-closed" : "");
    box.innerHTML = head + body;
  } else {
    const subEl = box.querySelector(".pulse-sub");
    if (subEl) subEl.innerHTML = sub;
    if (!closed) {
      for (const tile of model.tiles) {
        const el = box.querySelector('.pulse-tile[data-k="' + tile.key + '"]');
        if (el) updatePulseTile(el, tile, model.live);
      }
    }
  }
  tickPulse();
}

// Ages on the event tape count up between polls, like the transport clock.
function tickPulse() {
  const model = state.pulseModel;
  if (!model) return;
  const now = model.live ? Date.now() / 1000 : model.end;
  for (const el of document.querySelectorAll("#pulse time[data-t]")) {
    el.textContent = fmtDuration(Math.max(0, now - parseFloat(el.getAttribute("data-t")))) + " ago";
  }
}

function setupPulse() {
  const box = $("pulse");
  if (!box) return;
  box.addEventListener("click", (event) => {
    const toggle = event.target.closest && event.target.closest(".pulse-toggle");
    if (toggle) {
      setPulseCollapsed(!pulseCollapsed());
      state.pulseSig = "";
      renderPulse(state.run);
      return;
    }
    const chip = event.target.closest && event.target.closest(".pulse-chip");
    if (chip) openDrawer(chip.getAttribute("data-agent"));
  });
  // Hover a chart to read the exact value under the pointer.
  box.addEventListener("pointermove", (event) => {
    const plot = event.target.closest && event.target.closest(".pulse-plot");
    const model = state.pulseModel;
    if (!plot || !model) {
      for (const el of box.querySelectorAll(".pulse-tip, .pulse-cross")) el.hidden = true;
      return;
    }
    const tile = plot.parentNode;
    const geo = model.tiles.filter((t) => t.key === tile.getAttribute("data-k"))[0];
    const pulse = state.run && state.run.insights && state.run.insights.pulse;
    if (!geo || !pulse) return;
    const rect = plot.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (event.clientX - rect.left) / Math.max(1, rect.width)));
    const when = pulse.start + frac * (pulse.end - pulse.start);
    let text;
    if (geo.key === "running") {
      text = levelAt((state.run.insights.parallelism || {}).series, when) + " running";
    } else {
      const values = geo.key === "calls" ? pulse.calls : pulse.tokens;
      const v = values[Math.min(values.length - 1, Math.floor(frac * values.length))];
      text = geo.key === "calls" ? v + " calls in this slice" : fmtCount(v) + " tokens by then";
    }
    const tip = plot.querySelector(".pulse-tip");
    const cross = plot.querySelector(".pulse-cross");
    tip.textContent = text + " · " + fmtDuration(when - pulse.start) + " in";
    tip.hidden = false;
    tip.style.left = Math.min(Math.max(frac * 100, 18), 82) + "%";
    cross.hidden = false;
    cross.style.left = (frac * 100).toFixed(1) + "%";
  });
  box.addEventListener("pointerleave", () => {
    for (const el of box.querySelectorAll(".pulse-tip, .pulse-cross")) el.hidden = true;
  });
}

function costText(cost) {
  const total = fmtMoney(cost.total, cost.currency);
  const b = cost.budget;
  return total + (b ? " / " + fmtMoney(b.limit, cost.currency) : "");
}

function renderCostPart(run) {
  const cost = run.cost;
  if (!cost) return;
  const span = document.createElement("span");
  span.setAttribute("data-k", "cost");
  if (cost.enabled) {
    const strong = document.createElement("strong");
    strong.textContent = costText(cost);
    span.appendChild(strong);
    const label = document.createElement("em");
    label.textContent = cost.partial ? " cost (partial)" : " cost";
    span.appendChild(label);
    if (cost.budget && cost.budget.state !== "ok") span.className = "cost-" + cost.budget.state;
    if (cost.budget) {
      const meter = document.createElement("span");
      meter.className = "meter";
      const fill = document.createElement("i");
      fill.style.width = Math.min(100, Math.round(cost.budget.ratio * 100)) + "%";
      meter.appendChild(fill);
      span.appendChild(meter);
    }
    const notes = [];
    if (cost.partial) notes.push("No price for: " + cost.unpriced_models.join(", "));
    if (cost.budget) notes.push(Math.round(cost.budget.ratio * 100) + "% of budget");
    notes.push("agents " + fmtMoney(cost.agents, cost.currency) +
      " + orchestrator " + fmtMoney(cost.orchestrator, cost.currency));
    span.title = notes.join(" \u00b7 ");
  } else if (cost.error) {
    span.className = "cost-warn";
    span.textContent = cost.error;
  } else {
    return;     // no price file: tokens only, as documented
  }
  $("totals").appendChild(span);
}

function fileName(path) {
  return String(path || "").replace(/\.\.\.$/, "…").replace(/^.*[\\/]/, "");
}

// What an agent's verification says, in words (orchestra/verify.py decides the state).
function checkText(v) {
  const check = v.last_check;
  if (v.state === "failing") return "last check failed: " + check.target;
  if (v.state === "checked") {
    return (check.ok === null || check.ok === undefined ? "check running: " : "passed after the last edit: ") + check.target;
  }
  return "no test, build or lint after its last edit (" + fileName(v.last_edit.target) + ")" +
    (v.checked_before ? "; one ran before it" : "") + (v.final ? "" : ", so far");
}

function renderHealth(run) {
  const items = [];
  // Still running and failing the same call again and again (orchestra/errors.py).
  const errors = run.insights && run.insights.errors;
  const stuckFor = (id) => ((errors && errors.stuck) || []).find((s) => s.agent_id === id);
  const retrying = (label, s) => "RETRYING — " + label + ": " + callText(s) + " failed " + s.failed_in_a_row + " times in a row";
  for (const agent of run.agents) {
    const label = agent.description || agent.agent_id;
    const stuck = stuckFor(agent.agent_id);
    if (["waiting", "stalled", "failed", "orphaned"].includes(agent.status)) {
      items.push({ id: agent.agent_id, kind: agent.status,
        text: agent.status.toUpperCase() + " — " + label });
    }
    if (agent.loop) {
      items.push({ id: agent.agent_id, kind: "loop",
        text: "POSSIBLE LOOP — " + label + ": " + loopText(agent.loop) + (stuck ? ", failing every time" : "") });
    } else if (stuck) {
      items.push({ id: agent.agent_id, kind: "retrying", text: retrying(label, stuck) });
    }
    // Only once it has finished: a running agent may still be about to run its tests.
    const v = agent.verification;
    if (v && v.final && v.state !== "checked") {
      items.push({ id: agent.agent_id, kind: v.state === "failing" ? "checks-failing" : "unchecked",
        text: (v.state === "failing" ? "CHECKS FAILING — " : "UNCHECKED — ") + label + ": " + checkText(v) });
    }
  }
  // A context near its window is about to be compacted (orchestra/pressure.py).
  const pressure = run.insights && run.insights.pressure;
  for (const near of (pressure && pressure.near) || []) {
    items.push({ id: near.agent_id, kind: "context", card: "how-full-each-context-got",
      text: "CONTEXT " + fmtPct(near.fill) + " FULL — " + near.label + " (" + fmtCount(near.tokens) + " tokens)" });
  }
  const mainStuck = stuckFor("");
  if (mainStuck) items.push({ id: "", kind: "retrying", card: "what-went-wrong", text: retrying("Main session", mainStuck) });
  const box = $("health");
  if (!items.length) { box.hidden = true; return; }
  box.hidden = false;
  const ids = new Set(items.map((i) => i.id));
  const main = ids.delete("");       // the main session's own items carry no agent id
  box.innerHTML = "<strong>" + (main ? (ids.size ? "The main session and " + ids.size + " agent(s) need attention"
    : "The main session needs attention") : ids.size + " agent(s) need attention") + "</strong>";
  const list = document.createElement("ul");
  for (const entry of items) {
    const item = document.createElement("li");
    item.textContent = entry.text;
    item.setAttribute("data-kind", entry.kind);
    item.onclick = () => (entry.id ? openDrawer(entry.id) : openCard(entry.card));
    list.appendChild(item);
  }
  box.appendChild(list);
}

// Insights, scrolled to one card (by its key, the title as a slug), opened if it was folded.
function openCard(key) {
  if (foldedCards().delete(key)) saveFolded();      // a card you jump to opens
  setView("insights");
  const card = document.querySelector('#insights [data-card="' + key + '"]');
  if (!card) return;
  // Keep the card's title out from under the top bar where it is sticky (not on phones).
  const bar = document.querySelector(".topbar");
  const cover = bar && getComputedStyle(bar).position === "sticky" ? bar.offsetHeight : 0;
  window.scrollTo({ top: card.getBoundingClientRect().top + window.scrollY - cover - 12 });
}

function attentionTitle(att) {
  if (att.kind === "permission") return "Waiting for your permission";
  if (att.kind === "input") return "Waiting for your input";
  if (att.kind === "idle") return "Idle — waiting for your next prompt";
  if (att.kind === "error") {
    return "API error" + (att.error_type ? ": " + att.error_type : "");
  }
  return att.kind;
}

// What the session is blocked on, straight from hook events. Built with
// textContent, never innerHTML: the message is text from outside this page.
function renderAttention(run) {
  const box = $("attention");
  if (!box) return;
  const live = run.live;
  let kind = "";
  let title = "";
  let detail = "";
  let since = null;
  if (live && live.attention) {
    kind = live.attention.kind;
    title = attentionTitle(live.attention);
    detail = live.attention.message || "";
    since = live.attention.since;
  } else if (live && live.ended && !run.session_live) {
    kind = "ended";
    title = "Session ended" + (live.ended.reason ? " (" + live.ended.reason + ")" : "");
    since = live.ended.at;
  }
  if (!kind) { box.hidden = true; return; }
  box.hidden = false;
  box.setAttribute("data-kind", kind);
  box.setAttribute("role", kind === "permission" || kind === "error" ? "alert" : "status");
  box.textContent = "";
  const add = (cls, text) => {
    const span = document.createElement("span");
    span.className = cls;
    span.textContent = text;
    box.appendChild(span);
  };
  add("att-title", title);
  if (detail) add("att-detail", detail);
  if (since !== null && !state.offline) {
    add("att-since", fmtDuration(Math.max(0, Date.now() / 1000 - since)) + " ago");
  }
}

function renderDiagnostics(run) {
  const d = run.diagnostics || {};
  const bad = (d.unparsable_lines || 0);
  $("diagnostics").textContent = bad
    ? bad + " transcript line(s) could not be parsed; the view may be incomplete."
    : "";
}

// -------------------------------------------------------------- timeline

const ROW_H = 32;
const LEFT = 200;
const PAD = 16;
const DOT_R = 4;
const LABEL_X = 14;

function timeWindow(run) {
  // A replay scrubs along a FIXED axis (the whole run), so bars grow across a
  // stable scale instead of the scale itself rescaling on every frame.
  if (run.replay_window) return run.replay_window;
  let min = Infinity;
  let max = -Infinity;
  for (const agent of run.agents) {
    if (agent.started_at !== null) min = Math.min(min, agent.started_at);
    const end = agent.ended_at !== null ? agent.ended_at : agent.last_activity_at;
    if (end) max = Math.max(max, end);
  }
  if (!isFinite(min)) return [0, 1];
  if (!isFinite(max) || max <= min) max = min + 1;
  return [min, max];
}

// A comb of ticks along the foot of each bar: where the agent was actually calling
// tools. A burst, a long gap, or one repeated pattern is visible without opening it.
function drawActivityComb(row, agent, x, y, t1) {
  const bins = agent.activity;
  if (!bins || !bins.length || agent.started_at === null || agent.started_at === undefined) return;
  const end = agent.ended_at !== null && agent.ended_at !== undefined ? agent.ended_at
    : (agent.last_activity_at || t1);
  if (end <= agent.started_at) return;
  const peak = Math.max.apply(null, bins);
  const open = agent.ended_at === null || agent.ended_at === undefined;
  bins.forEach((count, k) => {
    if (!count) return;
    const tx = x(agent.started_at + ((k + 0.5) / bins.length) * (end - agent.started_at));
    row.appendChild(svgEl("line", {
      x1: tx, x2: tx, y1: y + 20, y2: y + ROW_H - 8.5,
      class: "bar-tick" + (open ? " bar-tick-open" : ""),
      "stroke-opacity": (0.4 + 0.6 * (count / peak)).toFixed(2),
    }));
  });
}

function renderTimeline(run) {
  const svg = $("timeline");
  svg.innerHTML = "";
  const agents = run.agents;
  const width = svg.clientWidth || 900;
  const height = PAD * 2 + Math.max(1, agents.length) * ROW_H + 20;
  svg.setAttribute("height", height);
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);

  const [t0, t1] = timeWindow(run);
  const plot = width - LEFT - PAD;
  const x = (t) => LEFT + ((t - t0) / (t1 - t0)) * plot;
  const rowOf = {};
  agents.forEach((agent, i) => { rowOf[agent.agent_id] = i; });
  const labelFont = bodyFont(12);
  const metaFont = bodyFont(10.5);
  // The longest dependency chain, from run insights: these bars get an outline.
  const crit = new Set();
  const cp = run.insights && run.insights.critical_path;
  if (cp && cp.chain && cp.chain.length > 1) cp.chain.forEach((c) => crit.add(c.agent_id));
  const labelMax = LEFT - LABEL_X - DOT_R * 2 - 14;

  // Zebra striping first, fully behind everything, so long rows stay easy to
  // track from the label to the bar without counting rows.
  agents.forEach((agent, i) => {
    if (i % 2 === 0) return;
    svg.appendChild(svgEl("rect", {
      x: 0, y: PAD + i * ROW_H, width, height: ROW_H, class: "row-alt",
    }));
  });

  // Batch bands sit above the stripes: they are what shows a parallel wave.
  (run.batches || []).forEach((batch, bi) => {
    const rows = batch.agent_ids.map((id) => rowOf[id]).filter((r) => r !== undefined);
    if (rows.length < 2) return;
    const top = PAD + Math.min(...rows) * ROW_H;
    const tall = (Math.max(...rows) - Math.min(...rows) + 1) * ROW_H;
    svg.appendChild(svgEl("rect", {
      x: LEFT - 4, y: top, width: plot + 8, height: tall, class: "batch-band",
      rx: 4,
    }));
    svg.appendChild(svgEl("text", {
      x: LEFT + 2, y: top - 3, class: "batch-label",
    }, "batch " + (bi + 1)));
  });

  for (let i = 0; i <= 4; i++) {
    const t = t0 + ((t1 - t0) * i) / 4;
    svg.appendChild(svgEl("line", {
      x1: x(t), y1: PAD - 6, x2: x(t), y2: height - PAD, class: "axis",
    }));
    svg.appendChild(svgEl("text", {
      x: x(t) + 3, y: height - PAD + 12, class: "axis-text",
    }, fmtDuration(t - t0)));
  }

  agents.forEach((agent, i) => {
    const y = PAD + i * ROW_H;
    const label = agent.description || agent.agent_id;
    const row = svgEl("g", { class: "row" + (agentMatchesFilter(agent) ? "" : " row-dim") });

    // A full-width hit target behind the row content: hover feedback should
    // not depend on landing the cursor on a 3px-wide bar.
    row.appendChild(svgEl("rect", {
      x: 0, y, width, height: ROW_H, class: "row-bg",
    }));

    row.appendChild(svgEl("circle", {
      cx: LABEL_X + DOT_R, cy: y + ROW_H / 2, r: DOT_R,
      class: "row-dot s-" + agent.status,
    }));
    if (agent.status === "running") {
      row.appendChild(svgEl("circle", {
        cx: LABEL_X + DOT_R, cy: y + ROW_H / 2, r: DOT_R, class: "row-dot-ping",
      }));
    }
    row.appendChild(svgEl("title", {}, agent.status));

    const labelX = LABEL_X + DOT_R * 2 + 6;
    row.appendChild(svgEl("text", { x: labelX, y: y + 14, class: "row-label" },
      fitText(label, labelMax, labelFont)));
    row.appendChild(svgEl("text", { x: labelX, y: y + 25, class: "row-meta" },
      fitText(agent.agent_type + " · " + fmtModelShort(agent.model), labelMax, metaFont)));

    const rounds = agent.rounds.length ? agent.rounds
      : [{ started_at: agent.started_at, ended_at: agent.ended_at }];
    for (const round of rounds) {
      const start = round.started_at !== null ? round.started_at : t0;
      const end = round.ended_at !== null ? round.ended_at
        : (agent.last_activity_at || t1);
      const bx = x(start);
      const bw = Math.max(4, x(Math.max(end, start)) - bx);
      const bar = svgEl("rect", {
        x: bx, y: y + 8, width: bw, height: ROW_H - 16, rx: 4,
        class: "bar s-" + agent.status + (round.ended_at === null ? " bar-open" : "") +
          (crit.has(agent.agent_id) ? " bar-critical" : ""),
      });
      bar.appendChild(svgEl("title", {},
        label + " — " + agent.status + " — " + fmtDuration(agent.duration_s) +
        (crit.has(agent.agent_id) ? " — on the critical path" : "")));
      row.appendChild(bar);
      if (bw > 70 && round.ended_at !== null) {
        row.appendChild(svgEl("text", { x: bx + 8, y: y + 18, class: "bar-text" },
          fmtDuration(end - start)));
      }
    }
    drawActivityComb(row, agent, x, y, t1);
    row.onclick = () => openDrawer(agent.agent_id);
    svg.appendChild(row);
  });

  if (crit.size) {
    svg.appendChild(svgEl("text", { x: LABEL_X, y: PAD - 4, class: "timeline-legend" },
      "outlined bars: critical path"));
  }
  if (run.session_live && run.replay_at === undefined && !state.offline) {
    const nx = Math.min(x(t1), width - PAD);
    svg.appendChild(svgEl("line", { x1: nx, y1: PAD - 6, x2: nx, y2: height - PAD, class: "playhead" }));
    svg.appendChild(svgEl("text", { x: nx - 4, y: PAD - 4, "text-anchor": "end", class: "playhead-label" }, "now"));
  }
}

// ------------------------------------------------------------ view state

const VIEWS = ["timeline", "graph", "agents", "insights", "spend", "prompts", "activity", "workfloor", "fleet", "history"];

function setView(view) {
  if (VIEWS.indexOf(view) < 0) view = "timeline";
  // Leaving Work Floor: stop every mounted sprite's rAF loop rather than let
  // it keep animating an off-screen, hidden canvas indefinitely.
  if (state.view === "workfloor" && view !== "workfloor") {
    stopAgentSprites();
    state.floorSig = "";     // the sprites are stopped, so the floor must be rebuilt on return
  }
  state.view = view;
  for (const name of VIEWS) {
    const section = $("view-" + name);
    if (section) section.hidden = view !== name;
  }
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.view === view;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.setAttribute("tabindex", active ? "0" : "-1");
  }
  writeHash();
  render();
  // Refresh immediately on switching in, rather than waiting up to POLL_MS
  // for the next cycle to notice the tab is now visible.
  if (view === "activity" && state.run) refreshTicker(state.run);
  if (view === "history") loadHistory();
}

const AGENT_VIEWS = ["timeline", "graph", "agents", "insights", "activity", "workfloor"];

// A live view is redrawn on every poll, which would throw a keyboard user back to the top of the
// page every few seconds. noteFocus(box) names the control in box that has focus: by the attribute
// that identifies it (a summary by its <details>; a control with none by its tag and first class)
// and which of the matches it is. restoreFocus(box, spot) focuses the same control after the
// redraw, once its handlers are bound. A control that is gone leaves focus where it fell.
const FOCUS_NAMES = ["data-agent", "data-session", "data-prompt", "data-fold", "data-jump", "data-view", "data-sort", "data-split"];

function focusMatches(box, spot) {
  return Array.from(box.querySelectorAll(spot.sel)).filter((el) => !spot.attr || el.getAttribute(spot.attr) === spot.value);
}

function noteFocus(box) {
  const active = typeof document !== "undefined" ? document.activeElement : null;
  if (!active || active === box || !box.contains || !box.contains(active)) return null;
  const summary = active.tagName === "SUMMARY" && active.parentElement ? active.parentElement : null;
  const el = summary || active;
  const attr = FOCUS_NAMES.find((name) => el.hasAttribute(name)) || null;
  const spot = { attr: attr, value: attr && el.getAttribute(attr), summary: !!summary,
    sel: attr ? "[" + attr + "]" : el.tagName.toLowerCase() + (el.classList.length ? "." + el.classList[0] : "") };
  spot.nth = focusMatches(box, spot).indexOf(el);
  return spot.nth < 0 ? null : spot;
}

function restoreFocus(box, spot) {
  if (!spot) return;
  const same = focusMatches(box, spot);
  let el = same[Math.min(spot.nth, same.length - 1)];
  if (el && spot.summary) el = el.querySelector("summary");
  if (el && el !== document.activeElement) el.focus({ preventScroll: true });
}

function render() {
  if (!state.run) return;
  const boot = $("boot");
  if (boot) boot.hidden = true;
  // A run with no agents has nothing for the agent views to draw: say so once,
  // instead of showing five empty panels.
  const none = state.run.agents.length === 0 && state.run.replay_at === undefined;
  const empty = $("empty-state");
  if (empty) empty.hidden = !(none && AGENT_VIEWS.indexOf(state.view) >= 0);
  for (const name of AGENT_VIEWS) {
    const section = $("view-" + name);
    if (section) section.hidden = state.view !== name || none;
  }
  renderHeader(state.run);
  renderRecap(state.run);
  renderPulse(state.run);
  updateChrome();
  renderAttention(state.run);
  renderHealth(state.run);
  renderConflicts(state.run);
  renderFilterChips();
  renderFilterCount(state.run);
  renderDiagnostics(state.run);
  if (none && AGENT_VIEWS.indexOf(state.view) >= 0) return;
  if (state.view === "timeline") renderTimeline(state.run);
  else if (state.view === "graph") renderGraph(state.run);
  else if (state.view === "workfloor") renderWorkfloor(state.run);
  else if (state.view === "fleet") renderFleet();
  else if (state.view === "history") renderHistory();
  else if (state.view === "insights") renderInsights(state.run);
  else if (state.view === "prompts") renderPrompts(state.run);
  else if (state.view === "agents") renderAgents(state.run);
  else if (state.view === "spend") renderSpend(state.run);
  else renderTicker();
}

function startPolling() {
  state.generation += 1;
  poll(state.generation);
}

async function poll(generation) {
  // A timer scheduled by a loop that has since been superseded must not fetch.
  if (generation !== state.generation) return;
  let run = null;
  try {
    run = await api("/api/run");
  } catch (err) {
    if (generation !== state.generation) return;
    $("conn").textContent = "reconnecting…";
    if (!state.run && $("boot-text")) $("boot-text").textContent = "Can’t reach the dashboard yet. Retrying…";
    state.backoff = Math.min(state.backoff * 2, 30000);
    if (state.live) setTimeout(() => poll(generation), state.backoff);
    return;
  }
  // A response that arrived after the session changed, or after this loop was
  // superseded, must not overwrite the current view.
  if (generation !== state.generation) return;
  state.liveRun = run;
  state.run = state.replay.on ? deriveRunAt(run, replayClamp(state.replay.t, run)) : run;
  state.backoff = state.streamLive ? STREAM_POLL_MS : POLL_MS;
  $("conn").textContent = run.session_live ? "" : "session ended";
  checkNotifications(run);
  checkSounds(run);
  render();
  // Only actively poll agent detail while the tab showing it is open, so
  // watching Timeline/Graph never costs N extra per-agent fetches.
  if (state.view === "activity") refreshTicker(run);
  if (state.live) setTimeout(() => poll(generation), state.backoff);
}

// ------------------------------------------------------------------ sounds
//
// Synthesized with Web Audio: no audio files, so nothing to fetch (the no-egress
// guarantee stands) and nothing to license. Off until the user clicks Sound,
// which is also the user gesture browsers require before a page may make noise.
//
// Three sounds, by what a person should do about it:
//   fail   something broke (API error, an agent failed)   -> look now
//   alert  Claude is waiting on you                       -> act now
//   done   everything that was running has finished       -> optional
// At most one plays per update (the most important), and never twice in 1.5 s.

const SOUND_PRIORITY = { fail: 3, alert: 2, done: 1 };
const SOUND_NOTES = {            // [frequency Hz, start offset s]
  alert: [[660, 0], [880, 0.14]],
  fail: [[330, 0], [220, 0.18]],
  done: [[523, 0], [659, 0.11], [784, 0.22]],
};

function topSound(names) {
  let best = null;
  for (const name of names) {
    if (!best || SOUND_PRIORITY[name] > SOUND_PRIORITY[best]) best = name;
  }
  return best;
}

// Mute per event and quiet hours, kept in this browser only. Quiet hours silence
// every event, including failures: if you want to be woken, leave them off.
const SOUND_PREF_DEFAULT = { mute: { alert: false, fail: false, done: false }, quiet: { on: false, from: "22:00", to: "07:00" } };

function clockMinutes(text) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(typeof text === "string" ? text : "");
  if (!m || +m[1] > 23 || +m[2] > 59) return null;
  return +m[1] * 60 + +m[2];
}

// Stored values are untrusted (any page script on this origin can write them):
// anything that is not exactly what we write falls back to the default.
function normalizeSoundPrefs(raw) {
  const prefs = JSON.parse(JSON.stringify(SOUND_PREF_DEFAULT));
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return prefs;
  if (raw.mute && typeof raw.mute === "object") {
    for (const name of Object.keys(prefs.mute)) prefs.mute[name] = raw.mute[name] === true;
  }
  if (raw.quiet && typeof raw.quiet === "object") {
    prefs.quiet.on = raw.quiet.on === true;
    if (clockMinutes(raw.quiet.from) !== null) prefs.quiet.from = raw.quiet.from;
    if (clockMinutes(raw.quiet.to) !== null) prefs.quiet.to = raw.quiet.to;
  }
  return prefs;
}

function inQuietHours(quiet, date) {
  if (!quiet || !quiet.on) return false;
  const from = clockMinutes(quiet.from);
  const to = clockMinutes(quiet.to);
  if (from === null || to === null || from === to) return false;
  const now = date.getHours() * 60 + date.getMinutes();
  return from < to ? (now >= from && now < to) : (now >= from || now < to);
}

// Applied BEFORE topSound(), so a muted "fail" does not hide an unmuted "alert".
function audibleSounds(names, prefs, date) {
  if (inQuietHours(prefs.quiet, date)) return [];
  return names.filter((name) => !prefs.mute[name]);
}

// What should sound, given this poll of the viewed session? `memo` is the
// baseline from the previous poll; the first call only records it.
function computeSounds(run, memo) {
  const att = run.live && run.live.attention;
  const attKey = att && att.kind !== "idle" ? att.kind + "@" + att.since : "";
  const failed = new Set(run.agents
    .filter((a) => a.status === "failed").map((a) => a.agent_id));
  const running = run.totals ? run.totals.running + (run.totals.waiting || 0) : 0;
  const budget = run.cost && run.cost.budget ? run.cost.budget.state : "";
  const out = [];
  if (memo.seeded) {
    if (budget === "exceeded" && memo.budget !== "exceeded") out.push("alert");
    if (attKey && attKey !== memo.attKey) out.push(att.kind === "error" ? "fail" : "alert");
    for (const id of failed) if (!memo.failed.has(id)) out.push("fail");
    if (memo.running > 0 && running === 0 && failed.size === 0) out.push("done");
  }
  memo.seeded = true;
  memo.attKey = attKey;
  memo.failed = failed;
  memo.running = running;
  memo.budget = budget;
  return out;
}

// Other sessions: a prompt or error in one you are not looking at.
function computeFleetSounds(fleet, viewing, memo) {
  const next = {};
  const out = [];
  for (const s of fleet.sessions) {
    const att = s.attention && s.session_live && s.urgency > 0 ? s.attention : null;
    if (!att) continue;
    const key = att.kind + "@" + att.since;
    next[s.session_id] = key;
    if (memo.seeded && s.session_id !== viewing && memo.keys[s.session_id] !== key) {
      out.push(att.kind === "error" ? "fail" : "alert");
    }
  }
  memo.seeded = true;
  memo.keys = next;
  return out;
}

function audioContext() {
  if (state.audio) return state.audio;
  const Ctor = typeof window !== "undefined" && (window.AudioContext || window.webkitAudioContext);
  if (!Ctor) return null;
  try { state.audio = new Ctor(); } catch (err) { return null; }
  return state.audio;
}

function playSound(name) {
  const ctx = state.soundEnabled ? audioContext() : null;
  const notes = SOUND_NOTES[name];
  if (!ctx || !notes) return;
  const now = Date.now();
  if (now - state.lastSoundAt < 1500) return;
  state.lastSoundAt = now;
  if (ctx.state === "suspended" && typeof ctx.resume === "function") ctx.resume();
  for (const [freq, offset] of notes) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    const t = ctx.currentTime + offset;
    osc.type = "sine";
    osc.frequency.value = freq;
    // A short attack and release: a bare square edge clicks.
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.12, t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(t);
    osc.stop(t + 0.25);
  }
}

function checkSounds(run) {
  if (!state.soundMemo) state.soundMemo = { seeded: false, attKey: "", failed: new Set(), running: 0 };
  const names = computeSounds(run, state.soundMemo);
  playAudible(names);
}

function playAudible(names) {
  if (!state.soundEnabled) return;
  if (!state.soundPrefs) state.soundPrefs = normalizeSoundPrefs(null);
  const audible = audibleSounds(names, state.soundPrefs, new Date());
  if (audible.length) playSound(topSound(audible));
}

function checkFleetSounds(fleet) {
  if (!state.soundFleetMemo) state.soundFleetMemo = { seeded: false, keys: {} };
  const viewing = state.run && state.run.session_id;
  const names = computeFleetSounds(fleet, viewing, state.soundFleetMemo);
  playAudible(names);
}

function updateSoundButton() {
  const btn = $("sound-toggle");
  if (!btn) return;
  btn.setAttribute("aria-pressed", String(state.soundEnabled));
  btn.textContent = state.soundEnabled ? "Sound: on" : "Sound";
}

// Exports come from the server (one implementation of the format, the same
// scrubbed data the dashboard shows), so a static report has nothing to call.
function downloadExport(format) {
  const session = state.sessionId ? "&session=" + encodeURIComponent(state.sessionId) : "";
  const link = document.createElement("a");
  link.href = "/api/export?format=" + encodeURIComponent(format) +
    "&k=" + encodeURIComponent(TOKEN) + session;
  link.download = "";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

function setStoredSoundPref(enabled) {
  try {
    if (typeof localStorage !== "undefined") {
      localStorage.setItem("orchestra-sound", enabled ? "1" : "0");
    }
  } catch (err) { /* blocked storage; not worth failing over */ }
}

function loadSoundPrefs() {
  let raw = null;
  try {
    if (typeof localStorage !== "undefined") raw = JSON.parse(localStorage.getItem("orchestra-sound-prefs"));
  } catch (err) { /* blocked or corrupt: defaults */ }
  return normalizeSoundPrefs(raw);
}

function saveSoundPrefs(prefs) {
  try {
    if (typeof localStorage !== "undefined") localStorage.setItem("orchestra-sound-prefs", JSON.stringify(prefs));
  } catch (err) { /* blocked storage; not worth failing over */ }
}

// The small "Sound options" panel: one checkbox per event, plus quiet hours.
function setupSoundPrefs() {
  const panel = $("sound-prefs");
  if (!panel) return;
  state.soundPrefs = loadSoundPrefs();
  panel.hidden = false;
  const prefs = state.soundPrefs;
  const bind = (id, read, write) => {
    const el = $(id);
    if (!el) return;
    read(el);
    el.onchange = () => { write(el); saveSoundPrefs(prefs); };
  };
  for (const name of Object.keys(prefs.mute)) {
    // The checkbox says "play this sound", the stored flag says "muted".
    bind("sound-play-" + name, (el) => { el.checked = !prefs.mute[name]; },
         (el) => { prefs.mute[name] = !el.checked; });
  }
  bind("sound-quiet-on", (el) => { el.checked = prefs.quiet.on; }, (el) => { prefs.quiet.on = el.checked; });
  for (const key of ["from", "to"]) {
    bind("sound-quiet-" + key, (el) => { el.value = prefs.quiet[key]; },
         (el) => { if (clockMinutes(el.value) !== null) prefs.quiet[key] = el.value; });
  }
}

// ------------------------------------------------------------------ replay
//
// "What did this run look like at 02:13?" Rebuilt purely from the round start
// and end times already in the payload, so it works live AND in a static report
// shared after the fact (a post-mortem needs no server).
//
// What it can know: who had launched, who was still in a round, who had ended
// and how. What it cannot know: stalled / waiting / orphaned (those depend on
// the clock and on hook events of that moment) and token totals at that moment.
// So an open round replays as "running", and tokens are left out rather than
// shown as final numbers that would be wrong.

function replayBounds(run) {
  if (!run || run.started_at === null || run.started_at === undefined) return null;
  const times = [];
  for (const a of run.agents) {
    if (a.ended_at !== null && a.ended_at !== undefined) times.push(a.ended_at);
    if (a.last_activity_at) times.push(a.last_activity_at);
  }
  if (run.ended_at) times.push(run.ended_at);
  const end = times.length ? Math.max.apply(null, times) : run.started_at;
  return { start: run.started_at, end: Math.max(end, run.started_at) };
}

function agentAt(agent, t) {
  const rounds = (agent.rounds || []).filter((r) => r.started_at !== null &&
    r.started_at !== undefined && r.started_at <= t);
  if (!rounds.length) return null;                     // not launched yet
  const open = rounds.some((r) => r.ended_at === null || r.ended_at === undefined ||
    r.ended_at > t);
  const closed = rounds.filter((r) => r.ended_at !== null && r.ended_at !== undefined &&
    r.ended_at <= t);
  let status = "running";
  if (!open) {
    status = closed.length ? closed[closed.length - 1].status : "unknown";
  }
  const ended = open ? null : Math.max.apply(null, closed.map((r) => r.ended_at));
  const started = Math.min.apply(null, rounds.map((r) => r.started_at));
  return Object.assign({}, agent, {
    status: status,
    started_at: started,
    ended_at: ended,
    duration_s: ended === null ? null : ended - started,
    rounds: rounds.map((r) => ({
      started_at: r.started_at,
      ended_at: r.ended_at !== null && r.ended_at !== undefined && r.ended_at <= t ? r.ended_at : null,
      status: r.ended_at !== null && r.ended_at !== undefined && r.ended_at <= t ? r.status : "running",
    })),
    // An open agent has been going at least until t; this is what the bar is
    // drawn to. A finished one keeps its own last activity.
    last_activity_at: open ? t : Math.min(agent.last_activity_at || ended, t),
    tokens: {}, cost: null, loop: null, tool_call_count: 0, files_written_count: 0,
    activity: [], last_tool: null,
  });
}

function deriveRunAt(run, t) {
  const agents = run.agents.map((a) => agentAt(a, t)).filter((a) => a !== null);
  const present = new Set(agents.map((a) => a.agent_id));
  present.add("main");
  const counts = { agents: agents.length, running: 0, waiting: 0, completed: 0,
    failed: 0, stalled: 0, orphaned: 0, unknown: 0, tokens: {}, wall_time_s: null };
  for (const a of agents) counts[a.status] = (counts[a.status] || 0) + 1;
  const ends = agents.map((a) => a.ended_at).filter((e) => e !== null);
  const starts = agents.map((a) => a.started_at);
  if (starts.length) {
    counts.wall_time_s = (ends.length ? Math.max.apply(null, ends) : t) - Math.min.apply(null, starts);
  }
  return Object.assign({}, run, {
    agents: agents,
    totals: counts,
    edges: run.edges.filter((e) => present.has(e.src) && present.has(e.dst)),
    batches: run.batches.map((b) => Object.assign({}, b, {
      agent_ids: b.agent_ids.filter((id) => present.has(id)) })).filter((b) => b.agent_ids.length),
    hub_files: [], write_conflicts: [],
    live: null, cost: null, orchestrator: null, insights: null,
    session_live: t < replayBounds(run).end,
    ended_at: t >= replayBounds(run).end ? run.ended_at : null,
    replay_at: t,
    replay_window: [replayBounds(run).start,
      Math.max(replayBounds(run).end, replayBounds(run).start + 1)],
  });
}

// Replay controls. state.run is what the views draw; during a replay it is
// deriveRunAt(state.liveRun, t), and everything else (notifications, sounds,
// the pill, copy-summary) keeps reading the true live run.

function replayClamp(t, run) {
  const b = replayBounds(run);
  if (!b) return t;
  return Math.min(Math.max(t, b.start), b.end);
}

function fmtOffset(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (n) => (n < 10 ? "0" : "") + n;
  return (h ? h + ":" + pad(m) : m) + ":" + pad(s % 60);
}

function setReplayTime(t) {
  const base = state.liveRun;
  if (!base) return;
  state.replay.t = replayClamp(t, base);
  state.run = deriveRunAt(base, state.replay.t);
  updateReplayBar();
  render();
}

function stopReplayTimer() {
  if (state.replay.timer) { clearInterval(state.replay.timer); state.replay.timer = null; }
  state.replay.playing = false;
}

function playReplay() {
  const b = replayBounds(state.liveRun);
  if (!b) return;
  if (state.replay.t >= b.end) setReplayTime(b.start);      // play again from the top
  state.replay.playing = true;
  updateReplayBar();
  state.replay.timer = setInterval(() => {
    const bounds = replayBounds(state.liveRun);
    if (!bounds) { stopReplayTimer(); return; }
    const next = state.replay.t + state.replay.speed * 0.1;
    if (next >= bounds.end) {
      setReplayTime(bounds.end);
      stopReplayTimer();
      updateReplayBar();
    } else {
      setReplayTime(next);
    }
  }, 100);
}

function updateReplayBar() {
  const bar = $("replay-bar");
  const b = replayBounds(state.liveRun);
  if (!bar || !b || bar.hidden) return;
  const refs = state.replayRefs;
  if (!refs) return;
  refs.slider.min = String(b.start);
  refs.slider.max = String(b.end);
  refs.slider.value = String(state.replay.t);
  refs.play.textContent = state.replay.playing ? "Pause" : "Play";
  refs.time.textContent = fmtOffset(state.replay.t - b.start) + " / " + fmtOffset(b.end - b.start);
}

function buildReplayBar() {
  const bar = $("replay-bar");
  bar.textContent = "";
  const make = (tag, cls, text) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const play = make("button", "", "Play");
  play.type = "button";
  play.onclick = () => (state.replay.playing ? (stopReplayTimer(), updateReplayBar()) : playReplay());
  const slider = make("input");
  slider.type = "range";
  slider.step = "1";
  slider.setAttribute("aria-label", "Replay position");
  slider.oninput = () => { stopReplayTimer(); setReplayTime(Number(slider.value)); };
  const time = make("span", "replay-time", "");
  const speed = make("select");
  speed.setAttribute("aria-label", "Replay speed");
  for (const x of [1, 10, 30, 60, 120]) {
    const option = make("option", "", x + "×");
    option.value = String(x);
    speed.appendChild(option);
  }
  speed.value = String(state.replay.speed);
  speed.onchange = () => { state.replay.speed = Number(speed.value); };
  const note = make("span", "replay-note",
    "Statuses are reconstructed from start/end times; token totals are not shown.");
  for (const el of [play, slider, time, speed, note]) bar.appendChild(el);
  state.replayRefs = { play: play, slider: slider, time: time };
}

function toggleReplay() {
  const bar = $("replay-bar");
  const btn = $("replay-toggle");
  if (!bar || !btn) return;
  if (state.replay.on) {
    stopReplayTimer();
    state.replay.on = false;
    bar.hidden = true;
    btn.setAttribute("aria-pressed", "false");
    state.run = state.liveRun;
    render();
    return;
  }
  const b = replayBounds(state.liveRun);
  if (!b) return;
  state.replay.on = true;
  bar.hidden = false;
  btn.setAttribute("aria-pressed", "true");
  buildReplayBar();
  setReplayTime(b.start);
}

// ----------------------------------------------------------------- history
//
// Past runs, from the opt-in metrics store. Metrics only: there is nothing here
// to leak, because nothing but counts, durations, tokens and cost is kept.

// For each comparable metric: does a bigger number mean better, worse, or neither?
const HISTORY_METRICS = [
  ["agents", "Agents", "neutral", "count"],
  ["completed", "Completed", "higher", "count"],
  ["failed", "Failed", "lower", "count"],
  ["wall_s", "Wall time", "lower", "duration"],
  ["tokens_total", "Tokens", "lower", "count"],
  ["cost", "Cost", "lower", "money"],
  ["loops", "Possible loops", "lower", "count"],
  ["write_conflicts", "Write conflicts", "lower", "count"],
  ["cache_hit_ratio", "Cache hit", "higher", "pct"],
];

function fmtHistoryValue(value, kind, currency) {
  if (value === null || value === undefined) return "—";
  if (kind === "duration") return fmtDuration(value);
  if (kind === "money") return fmtMoney(value, currency);
  if (kind === "pct") return fmtPct(value);
  return fmtCount(value);
}

// "better" / "worse" / "flat" for a change in a metric.
function deltaVerdict(change, direction) {
  if (change === null || change === undefined || change === 0) return "flat";
  if (direction === "neutral") return "flat";
  const up = change > 0;
  return (direction === "higher") === up ? "good" : "bad";
}

function fmtWhen(seconds) {
  if (!seconds) return "—";
  const d = new Date(seconds * 1000);
  const pad = (n) => (n < 10 ? "0" : "") + n;
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
    pad(d.getHours()) + ":" + pad(d.getMinutes());
}

async function loadHistory() {
  try {
    state.history.data = await api("/api/history?limit=100");
  } catch (err) {
    state.history.data = { enabled: true, runs: [], error: "could not load history" };
  }
  if (state.view === "history") renderHistory();
}

// The earlier run first, whatever order the user clicked them in: the compare
// table reads "Earlier / Later", so a click order of new-then-old would
// otherwise label the newer run "Earlier" and flip every verdict.
function orderRuns(ids, runs) {
  const when = (id) => {
    const run = runs.find((r) => r.session_id === id);
    return run ? (run.started_at || run.first_seen_at || 0) : 0;
  };
  return ids.slice().sort((x, y) => when(x) - when(y));
}

async function loadCompare() {
  if (state.history.selected.length < 2) { state.history.compare = null; return; }
  const runs = state.history.data ? state.history.data.runs : [];
  const [a, b] = orderRuns(state.history.selected, runs);
  try {
    state.history.compare = await api("/api/history/compare?a=" + encodeURIComponent(a) +
      "&b=" + encodeURIComponent(b));
  } catch (err) { state.history.compare = null; }
  if (state.view === "history") renderHistory();
}

function selectHistoryRun(sessionId) {
  const sel = state.history.selected;
  const at = sel.indexOf(sessionId);
  if (at >= 0) sel.splice(at, 1);
  else { sel.push(sessionId); if (sel.length > 2) sel.shift(); }
  state.history.compare = null;
  renderHistory();
  loadCompare();
}

function renderHistory() {
  const box = $("history");
  if (!box) return;
  const data = state.history.data;
  if (!data) { box.innerHTML = '<div class="history-note">Loading…</div>'; return; }
  if (!data.enabled) {
    box.innerHTML = '<div class="history-note"><strong>Run history is off.</strong><br>' +
      "Set <code>ORCHESTRA_HISTORY=on</code> in the environment Claude Code runs in, then " +
      "restart the dashboard, to start remembering how runs went.<br>Only counts, durations, " +
      "tokens and cost are kept — never prompts, results, or file contents.</div>";
    return;
  }
  if (data.error) {
    box.innerHTML = '<div class="history-note">' + esc(data.error) + "</div>";
    return;
  }
  if (!data.runs.length) {
    box.innerHTML = '<div class="history-note">No runs recorded yet — they appear as you ' +
      "view sessions.<br>Select two runs here to compare them.</div>";
    return;
  }
  const sel = new Set(state.history.selected);
  const spot = noteFocus(box);
  const rows = data.runs.map((r) =>
    '<tr data-session="' + esc(r.session_id) + '" aria-selected="' + sel.has(r.session_id) + '" tabindex="0">' +
    "<td>" + esc(fmtWhen(r.started_at || r.first_seen_at)) + "</td>" +
    "<td>" + esc(r.project_name || "(unknown)") + "</td>" +
    '<td class="num">' + esc(r.agents) + "</td>" +
    '<td class="num">' + esc(r.failed) + "</td>" +
    '<td class="num">' + esc(fmtHistoryValue(r.wall_s, "duration")) + "</td>" +
    '<td class="num">' + esc(fmtHistoryValue(r.tokens_total, "count")) + "</td>" +
    '<td class="num">' + esc(fmtHistoryValue(r.cost, "money", r.currency)) + "</td>" +
    "</tr>").join("");
  box.innerHTML = "<table><thead><tr><th>Started</th><th>Project</th>" +
    '<th class="num">Agents</th><th class="num">Failed</th><th class="num">Wall</th>' +
    '<th class="num">Tokens</th><th class="num">Cost</th></tr></thead><tbody>' + rows +
    "</tbody></table>" + renderCompare();
  for (const tr of box.querySelectorAll("tbody tr")) {
    tr.onclick = () => selectHistoryRun(tr.dataset.session);
    tr.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectHistoryRun(tr.dataset.session); }
    };
  }
  restoreFocus(box, spot);
}

function renderCompare() {
  const c = state.history.compare;
  if (state.history.selected.length < 2) {
    return '<div class="history-compare history-note" style="padding:12px">Select two runs to compare them.</div>';
  }
  if (!c) return '<div class="history-compare history-note" style="padding:12px">Comparing…</div>';
  const label = (r) => (r.project_name || "run") + " · " + fmtWhen(r.started_at || r.first_seen_at);
  const currency = c.a.currency || c.b.currency;
  const lines = HISTORY_METRICS.map(([key, name, direction, kind]) => {
    const d = c.delta[key];
    if (!d) {
      return "<tr><td>" + esc(name) + '</td><td class="num">—</td><td class="num">—</td>' +
        '<td class="num delta-flat">n/a</td></tr>';
    }
    const verdict = deltaVerdict(d.change, direction);
    // Sign and magnitude are formatted apart: the duration formatter has no
    // notion of a negative ("-2m -30s"), and a change of 0 should read "0".
    const sign = d.change > 0 ? "+" : d.change < 0 ? "\u2212" : "";
    const pct = d.pct === null || d.pct === undefined ? "" :
      " (" + (d.pct > 0 ? "+" : d.pct < 0 ? "\u2212" : "") + Math.abs(d.pct).toFixed(0) + "%)";
    const size = Math.abs(d.change);
    const change = d.change === 0 ? "0" : (kind === "pct"
      ? sign + (size * 100).toFixed(1) + " pts"
      : sign + fmtHistoryValue(size, kind, currency));
    return "<tr><td>" + esc(name) + '</td><td class="num">' + esc(fmtHistoryValue(d.a, kind, currency)) +
      '</td><td class="num">' + esc(fmtHistoryValue(d.b, kind, currency)) +
      '</td><td class="num delta-' + verdict + '">' + esc(change + pct) + "</td></tr>";
  }).join("");
  return '<div class="history-compare"><h3>' + esc(label(c.b)) + " vs " + esc(label(c.a)) +
    "</h3><table><thead><tr><th>Metric</th>" +
    '<th class="num">Earlier</th><th class="num">Later</th><th class="num">Change</th></tr></thead><tbody>' +
    lines + "</tbody></table></div>";
}

// ------------------------------------------------------- pill + tab chrome
//
// The same few facts drive three surfaces: the browser tab's title, its
// favicon, and an optional always-on-top "pill" window. One pure model decides
// what to say; the surfaces only draw it.

const PILL_COLORS = { permission: "#7950f2", input: "#7950f2", error: "#e03131",
  running: "#1c7ed6", idle: "#8790ad" };

function pillModel(run, fleet) {
  const live = fleet ? fleet.sessions.filter((s) => s.session_live) : [];
  let needing = live.filter((s) => s.urgency > 0 && s.attention);
  // Before the first fleet poll, fall back to the session on screen.
  if (!fleet && run && run.live && run.live.attention && run.session_live &&
      run.live.attention.kind !== "idle") {
    needing = [{ attention: run.live.attention, project_name: "", session_id: run.session_id,
      title: (run.session && run.session.title) || "" }];
  }
  const totals = run ? run.totals : null;
  const running = fleet
    ? live.reduce((n, s) => n + ((s.totals && s.totals.running) || 0), 0)
    : (totals ? totals.running : 0);
  const top = needing[0] || null;
  let kind = "idle";
  if (top) kind = top.attention.kind;
  else if (running > 0) kind = "running";
  const where = top ? (top.title || top.project_name || (top.session_id || "").slice(0, 8)) : "";
  const more = needing.length - 1;
  return {
    kind: kind,
    count: needing.length,
    headline: top ? attentionTitle(top.attention)
      : (running > 0 ? running + " running" : "All quiet"),
    detail: top
      ? where + (more > 0 ? " · +" + more + " more" : "")
      : (totals ? totals.agents + " agents · " + totals.completed + " done" : ""),
    agents: run ? run.agents.slice(0, 16).map((a) => a.status) : [],
    title: (run && run.session && run.session.title) || "",
  };
}

function tabTitle(model) {
  const name = (model.title ? model.title + " \u00B7 " : "") + "Cuelight";
  if (model.count > 0) return "(" + model.count + ") " + name;
  if (model.kind === "running") return "\u25B6 " + name;
  return name;
}

// Blend two #rrggbb colours; t is how much of `to` to mix in.
function mixHex(from, to, t) {
  const n = (hex, i) => parseInt(hex.substr(1 + i * 2, 2), 16);
  let out = "#";
  for (let i = 0; i < 3; i++) {
    const v = Math.round(n(from, i) * (1 - t) + n(to, i) * t);
    out += (v < 16 ? "0" : "") + v.toString(16);
  }
  return out;
}

// The same cue-light face as the pill, drawn on a canvas at 64x64 so it still
// reads at 16px: bold eyes, one clear mouth, and the count in a red badge.
function drawCue(ctx, model) {
  const colour = PILL_COLORS[model.kind] || PILL_COLORS.idle;
  const ink = "#161827";
  ctx.fillStyle = "#3a3e5c";
  ctx.beginPath();
  if (typeof ctx.roundRect === "function") ctx.roundRect(12, 50, 40, 12, 6); else ctx.rect(12, 50, 40, 12);
  ctx.fill();
  let fill = colour;
  if (typeof ctx.createRadialGradient === "function") {
    fill = ctx.createRadialGradient(24, 18, 3, 32, 30, 30);
    fill.addColorStop(0, mixHex(colour, "#ffffff", 0.46));
    fill.addColorStop(1, mixHex(colour, "#000000", 0.2));
  }
  ctx.fillStyle = fill;
  ctx.beginPath();
  ctx.arc(32, 30, 25, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = ink;
  ctx.fillStyle = ink;
  ctx.lineWidth = 3.4;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  const eye = (x) => {
    ctx.beginPath();
    if (typeof ctx.ellipse === "function") ctx.ellipse(x, 28, 3.8, 5.4, 0, 0, Math.PI * 2);
    else ctx.arc(x, 28, 4.5, 0, Math.PI * 2);
    ctx.fill();
  };
  if (model.kind === "idle") {            // asleep: two downward arcs
    for (const x of [22, 42]) {
      ctx.beginPath();
      ctx.moveTo(x - 5, 27);
      ctx.quadraticCurveTo(x, 33, x + 5, 27);
      ctx.stroke();
    }
  } else {
    eye(22);
    eye(42);
  }
  ctx.beginPath();
  if (model.kind === "permission" || model.kind === "input") {          // "oh": a round mouth
    if (typeof ctx.ellipse === "function") ctx.ellipse(32, 41, 3.6, 4.4, 0, 0, Math.PI * 2);
    else ctx.arc(32, 41, 4, 0, Math.PI * 2);
    ctx.fill();
  } else if (model.kind === "error") {                                  // frown
    ctx.moveTo(25, 45);
    ctx.quadraticCurveTo(32, 37, 39, 45);
    ctx.stroke();
  } else if (model.kind === "running") {                                // open smile
    ctx.moveTo(24, 38);
    ctx.quadraticCurveTo(32, 50, 40, 38);
    ctx.closePath();
    ctx.fill();
  } else {                                                              // small smile
    ctx.moveTo(26, 40);
    ctx.quadraticCurveTo(32, 45, 38, 40);
    ctx.stroke();
  }
  if (model.count > 0) {
    ctx.beginPath();
    ctx.arc(51, 50, 12, 0, Math.PI * 2);
    ctx.fillStyle = "#d6303a";
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = "#fff";
    ctx.stroke();
    ctx.fillStyle = "#fff";
    ctx.font = "bold 17px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(model.count > 9 ? "9+" : String(model.count), 51, 51);
  }
}

function drawFavicon(model) {
  if (typeof document.createElement !== "function") return null;
  const canvas = document.createElement("canvas");
  if (!canvas || typeof canvas.getContext !== "function") return null;
  canvas.width = canvas.height = 64;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  try {
    drawCue(ctx, model);
    return canvas.toDataURL("image/png");
  } catch (err) { return null; }
}

function updateTabChrome(model) {
  document.title = tabTitle(model);
  // Redrawing a canvas on every poll is wasted work if nothing changed.
  const key = model.kind + ":" + model.count;
  if (key === state.faviconKey) return;
  state.faviconKey = key;
  const href = drawFavicon(model);
  if (!href || typeof document.querySelectorAll !== "function") return;
  let link = null;
  for (const el of document.querySelectorAll("link")) {
    if (el.rel === "icon") link = el;
  }
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.href = href;
}

const PILL_CSS = `
@property --c { syntax: "<color>"; inherits: true; initial-value: #8790ad; }
:root { color-scheme: light dark; --bg:#f6f6fb; --ink:#181a2a; --muted:#585c72; --seg:#d9dbe8;
  --base:#3a3e5c; --eye:#161827; --run:#1c7ed6; --done:#2f9e44; --bad:#e03131; --stall:#b36a08; --wait:#7950f2; }
@media (prefers-color-scheme: dark) { :root { --bg:#1a1c2b; --ink:#eceef8; --muted:#a4a8c0; --seg:#32354d;
  --base:#0e0f19; --eye:#10111c; --run:#4da3ff; --done:#4cc16a; --bad:#ff6b6b; --stall:#f5b13d; --wait:#9d7bff; } }
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin:0; overflow:hidden; background:var(--bg); color:var(--ink);
  font:13px/1.3 system-ui,"Segoe UI",Roboto,sans-serif; }
.pill { position:relative; display:flex; align-items:center; gap:14px; height:100%; padding:12px 18px 12px 16px;
  background:radial-gradient(120% 150% at 0% 50%, color-mix(in srgb, var(--c) 24%, var(--bg)), var(--bg) 64%);
  transition:--c .5s ease; }
.pill::after { content:""; position:absolute; inset:0; pointer-events:none;
  box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--c) 30%, transparent); }

.cue { position:relative; flex:none; width:72px; height:72px; }
.cue svg { display:block; width:72px; height:72px; overflow:visible; }
.badge { position:absolute; top:-4px; right:-8px; min-width:20px; height:20px; padding:0 5px; border-radius:10px;
  background:#d6303a; color:#fff; font:700 11px/20px system-ui,"Segoe UI",sans-serif; text-align:center;
  box-shadow:0 0 0 2px var(--bg); }
.badge[hidden] { display:none; }

.base { fill:var(--base); }
.body { transform-origin:32px 47px; filter:drop-shadow(0 2px 6px color-mix(in srgb, var(--c) 45%, transparent)); }
.glint { fill:#fff; opacity:.38; }
.cheek { fill:#fff; opacity:.22; }
.eye { fill:var(--eye); }
.spark { fill:#fff; }
.eyes-open { transform-origin:32px 28px; animation:blink 5.5s infinite; }
.line { fill:none; stroke:var(--eye); stroke-width:2.2; stroke-linecap:round; stroke-linejoin:round; }
.fill { fill:var(--eye); stroke:none; }
.halo { fill:none; stroke:var(--c); stroke-width:2; transform-origin:32px 29px; opacity:0; }
.orbit { fill:none; stroke:var(--c); stroke-width:2.6; stroke-linecap:round; stroke-dasharray:30 140;
  transform-origin:32px 29px; }
.zz { fill:var(--muted); font:700 11px system-ui,"Segoe UI",sans-serif; }

.halo, .orbit, .zz, .eyes-sleep, .m-idle, .m-run, .m-ask, .m-err, .brows-ask, .brows-err { display:none; }
.pill[data-kind="idle"] .eyes-open { display:none; }
.pill[data-kind="idle"] .eyes-sleep, .pill[data-kind="idle"] .m-idle, .pill[data-kind="idle"] .zz { display:inline; }
.pill[data-kind="running"] .m-run, .pill[data-kind="running"] .orbit { display:inline; }
.pill[data-kind="permission"] .m-ask, .pill[data-kind="input"] .m-ask,
.pill[data-kind="permission"] .brows-ask, .pill[data-kind="input"] .brows-ask,
.pill[data-kind="permission"] .halo, .pill[data-kind="input"] .halo { display:inline; }
.pill[data-kind="error"] .m-err, .pill[data-kind="error"] .brows-err, .pill[data-kind="error"] .halo { display:inline; }

.pill[data-kind="idle"] .body { animation:breathe 4.4s ease-in-out infinite; }
.pill[data-kind="running"] .body { animation:bob 1.7s ease-in-out infinite; }
.pill[data-kind="permission"] .body, .pill[data-kind="input"] .body { animation:hop 2s ease-in-out infinite; }
.pill[data-kind="error"] .body { animation:shake 2.6s ease-in-out infinite; }
.pill[data-kind="running"] .orbit { animation:spin 2.2s linear infinite; }
.halo { animation:ring 2s ease-out infinite; } .halo.h2 { animation-delay:1s; }
.pill[data-kind="idle"] .zz { animation:zzz 3.6s ease-in-out infinite; }

@keyframes breathe { 0%,100% { transform:scale(1,1); } 50% { transform:scale(1.035,.965); } }
@keyframes bob { 0%,100% { transform:translateY(0); } 50% { transform:translateY(-2.5px); } }
@keyframes hop { 0%,60%,100% { transform:translateY(0) scale(1,1); }
  14% { transform:translateY(0) scale(1.08,.9); } 30% { transform:translateY(-9px) scale(.96,1.05); }
  46% { transform:translateY(0) scale(1.05,.94); } }
@keyframes shake { 0%,60%,100% { transform:translateX(0); } 10%,30%,50% { transform:translateX(-2.5px); }
  20%,40% { transform:translateX(2.5px); } }
@keyframes ring { 0% { transform:scale(1); opacity:.6; } 100% { transform:scale(1.5); opacity:0; } }
@keyframes spin { to { transform:rotate(360deg); } }
@keyframes blink { 0%,92%,100% { transform:scaleY(1); } 95.5% { transform:scaleY(.1); } }
@keyframes zzz { 0% { transform:translate(0,6px); opacity:0; } 30% { opacity:.9; }
  100% { transform:translate(5px,-8px); opacity:0; } }
@keyframes seg { 50% { opacity:.45; } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation:none !important; transition:none !important; }
  .halo, .zz { display:none !important; }
}

.txt { min-width:0; flex:1; }
.head { font-size:15px; font-weight:700; letter-spacing:-.005em; white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; }
.sub { margin-top:2px; color:var(--muted); font-size:12px; white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; }
.segs { display:flex; gap:3px; margin-top:9px; }
.segs:empty { display:none; }
.segs i { display:block; flex:1 1 0; max-width:18px; height:5px; border-radius:3px; background:var(--seg); }
.segs i[data-s="running"] { background:var(--run); animation:seg 1.5s ease-in-out infinite; }
.segs i[data-s="completed"] { background:var(--done); }
.segs i[data-s="failed"], .segs i[data-s="orphaned"] { background:var(--bad); }
.segs i[data-s="stalled"] { background:var(--stall); }
.segs i[data-s="waiting"] { background:var(--wait); }
`;

const SVG_NS = "http://www.w3.org/2000/svg";

// The mascot: a stage cue light. Its face and motion carry the state, so the
// pill can be read from across the screen; the words only add the detail.
function buildPillAvatar(doc) {
  const node = (name, attrs, parent) => {
    const el = doc.createElementNS(SVG_NS, name);
    for (const key in attrs) el.setAttribute(key, attrs[key]);
    if (parent) parent.appendChild(el);
    return el;
  };
  const svg = node("svg", { viewBox: "0 0 64 64", "aria-hidden": "true", focusable: "false" });
  const defs = node("defs", {}, svg);
  const glass = node("radialGradient", { id: "cue-glass", cx: ".35", cy: ".3", r: ".85" }, defs);
  node("stop", { offset: "0", style: "stop-color:color-mix(in srgb, var(--c) 46%, #fff)" }, glass);
  node("stop", { offset: "1", style: "stop-color:color-mix(in srgb, var(--c) 80%, #000)" }, glass);
  node("circle", { "class": "halo", cx: 32, cy: 29, r: 20 }, svg);
  node("circle", { "class": "halo h2", cx: 32, cy: 29, r: 20 }, svg);
  node("circle", { "class": "orbit", cx: 32, cy: 29, r: 27 }, svg);
  node("rect", { "class": "base", x: 17, y: 46.5, width: 30, height: 9.5, rx: 4.75 }, svg);
  const body = node("g", { "class": "body" }, svg);
  node("circle", { cx: 32, cy: 29, r: 20, fill: "url(#cue-glass)" }, body);
  node("ellipse", { "class": "glint", cx: 24.5, cy: 17.5, rx: 6.5, ry: 3.2, transform: "rotate(-28 24.5 17.5)" }, body);
  node("ellipse", { "class": "cheek", cx: 20.5, cy: 35, rx: 3.4, ry: 2.2 }, body);
  node("ellipse", { "class": "cheek", cx: 43.5, cy: 35, rx: 3.4, ry: 2.2 }, body);
  const open = node("g", { "class": "eyes-open" }, body);
  node("ellipse", { "class": "eye", cx: 25.5, cy: 28, rx: 2.5, ry: 3.5 }, open);
  node("ellipse", { "class": "eye", cx: 38.5, cy: 28, rx: 2.5, ry: 3.5 }, open);
  node("circle", { "class": "spark", cx: 26.3, cy: 26.4, r: 0.95 }, open);
  node("circle", { "class": "spark", cx: 39.3, cy: 26.4, r: 0.95 }, open);
  const sleep = node("g", { "class": "eyes-sleep" }, body);
  node("path", { "class": "line", d: "M22 27 Q25.5 31 29 27" }, sleep);
  node("path", { "class": "line", d: "M35 27 Q38.5 31 42 27" }, sleep);
  const ask = node("g", { "class": "brows-ask" }, body);
  node("path", { "class": "line", d: "M22 21 Q25.5 18.5 29 20.5" }, ask);
  node("path", { "class": "line", d: "M35 20.5 Q38.5 18.5 42 21" }, ask);
  const worry = node("g", { "class": "brows-err" }, body);
  node("path", { "class": "line", d: "M21.5 23 L29 20" }, worry);
  node("path", { "class": "line", d: "M35 20 L42.5 23" }, worry);
  node("path", { "class": "line m-idle", d: "M28 37 Q32 40 36 37" }, body);
  node("path", { "class": "fill m-run", d: "M27.5 36 Q32 43.5 36.5 36 Z" }, body);
  node("ellipse", { "class": "fill m-ask", cx: 32, cy: 38, rx: 2.6, ry: 3.1 }, body);
  node("path", { "class": "line m-err", d: "M27.5 40 Q32 35 36.5 40" }, body);
  node("text", { "class": "zz", x: 47, y: 14 }, svg).textContent = "z";
  return svg;
}

function buildPill(doc) {
  const make = (tag, cls) => {
    const el = doc.createElement(tag);
    if (cls) el.className = cls;
    return el;
  };
  const pill = make("div", "pill");
  const cue = make("div", "cue");
  cue.appendChild(buildPillAvatar(doc));
  const badge = make("span", "badge");
  badge.hidden = true;
  cue.appendChild(badge);
  pill.appendChild(cue);
  const txt = make("div", "txt");
  txt.setAttribute("role", "status");
  const head = make("div", "head");
  const sub = make("div", "sub");
  const segs = make("div", "segs");
  txt.appendChild(head);
  txt.appendChild(sub);
  txt.appendChild(segs);
  pill.appendChild(txt);
  let root = doc.getElementById("pill-root");
  if (!root) {
    root = make("div");
    root.id = "pill-root";
    root.style.height = "100%";
    doc.body.appendChild(root);
  }
  root.textContent = "";
  root.appendChild(pill);
  return { doc: doc, pill: pill, badge: badge, head: head, sub: sub, segs: segs, sig: null };
}

function renderPill(model) {
  const win = state.pill;
  if (!win || win.closed) return;
  const doc = win.document;
  // The window is built once and then only updated, so the mascot's animations
  // are not restarted by every poll.
  if (!state.pillUi || state.pillUi.doc !== doc) state.pillUi = buildPill(doc);
  const ui = state.pillUi;
  const sig = JSON.stringify(model);
  if (sig === ui.sig) return;
  ui.sig = sig;
  ui.pill.setAttribute("data-kind", model.kind);
  ui.pill.style.setProperty("--c", PILL_COLORS[model.kind] || PILL_COLORS.idle);
  ui.head.textContent = model.headline;
  ui.sub.textContent = model.detail;
  ui.badge.hidden = model.count < 2;
  ui.badge.textContent = model.count < 2 ? "" : String(model.count);
  ui.segs.textContent = "";
  for (const status of model.agents) {
    const cell = doc.createElement("i");
    cell.setAttribute("data-s", status);
    ui.segs.appendChild(cell);
  }
}

function updatePillButton() {
  const btn = $("pill-toggle");
  if (!btn) return;
  btn.setAttribute("aria-pressed", String(!!state.pill));
}

async function togglePill() {
  if (state.pill) { state.pill.close(); return; }
  if (typeof window.documentPictureInPicture === "undefined") return;
  let win;
  try {
    win = await window.documentPictureInPicture.requestWindow({ width: 360, height: 104 });
  } catch (err) { return; }   // refused (no user gesture, or the user declined)
  const style = win.document.createElement("style");
  style.textContent = PILL_CSS;
  win.document.head.appendChild(style);
  win.document.title = "Cuelight";
  win.addEventListener("pagehide", () => { state.pill = null; state.pillUi = null; updatePillButton(); });
  state.pill = win;
  updatePillButton();
  updateChrome();
}

function updateChrome() {
  const model = pillModel(state.liveRun || state.run, state.fleet);
  updateTabChrome(model);
  renderPill(model);
}

// ------------------------------------------------------------------ fleet
//
// Every recently active session across every project, most urgent first: the
// answer to "which of my sessions needs me?" without opening each one.

function fleetAgo(modifiedAt) {
  return fmtDuration(Math.max(0, Date.now() / 1000 - modifiedAt)) + " ago";
}

function renderFleetBadge() {
  const badge = $("fleet-badge");
  if (!badge) return;
  const n = state.fleet ? state.fleet.attention_count : 0;
  badge.hidden = !n;
  badge.textContent = n ? String(n) : "";
}

function fleetStatus(s) {
  const t = s.totals;
  if (!s.session_live) return "Ended" + (s.ended && s.ended.reason ? " (" + s.ended.reason + ")" : "");
  return t && t.running ? t.running + " agent(s) running" : "Idle";
}

// A row's second line: what the session needs from you comes first, then Claude
// Code's own recap of it, then the last thing you asked, then its status.
function fleetSub(s) {
  const att = s.attention && s.session_live ? s.attention : null;
  if (att) return { kind: "attention", text: attentionTitle(att) + (att.message ? " — " + att.message : "") };
  if (s.recap) {
    return { kind: "recap",
      text: "Recap" + (s.recap_at ? " · " + fleetAgo(s.recap_at) : "") + ": " + s.recap };
  }
  if (s.last_prompt) return { kind: "prompt", text: "Last asked: " + s.last_prompt };
  return { kind: "status", text: fleetStatus(s) };
}

function fleetRow(s, current) {
  const att = s.attention && s.session_live ? s.attention : null;
  const t = s.totals;
  const sub = fleetSub(s);
  // The status moves to the meta line when the second line is taken by something else.
  const meta = (sub.kind === "status" ? "" : fleetStatus(s) + " · ") +
    (t ? t.agents + " agents" +
      (t.waiting ? " · " + t.waiting + " waiting" : "") +
      (t.failed ? " · " + t.failed + " failed" : "") + " · " : "") + fleetAgo(s.modified_at);
  // A session Claude Code (or you) titled leads with that title; its project follows.
  const name = s.title
    ? esc(s.title) + '<span class="fleet-project">' + esc(s.project_name || s.session_id.slice(0, 8)) + "</span>"
    : esc(s.project_name || "(unknown project)") +
      '<span class="fleet-id">' + esc(s.session_id.slice(0, 8)) + "</span>";
  const stale = sub.kind === "recap" && s.recap_stale;
  return '<div class="fleet-row' + (s.session_id === current ? " fleet-current" : "") +
    (s.session_live ? "" : " fleet-quiet") + '" data-session="' + esc(s.session_id) + '"' +
    (att ? ' data-kind="' + esc(att.kind) + '"' : "") +
    (s.session_live ? ' data-live="1"' : "") + ' role="button" tabindex="0">' +
    '<span class="fleet-dot"></span>' +
    '<div class="fleet-main"><div class="fleet-title">' + name + "</div>" +
      '<div class="fleet-sub fleet-sub-' + sub.kind + (stale ? " is-stale" : "") + '"' +
      (stale ? ' title="Older than the latest activity"' : "") + ">" + esc(sub.text) + "</div></div>" +
    '<div class="fleet-meta">' + esc(meta) + "</div></div>";
}

function renderFleet() {
  const box = $("fleet");
  if (!box) return;
  const data = state.fleet;
  if (!data) { box.innerHTML = '<div class="fleet-empty">Loading sessions…</div>'; return; }
  if (!data.sessions.length) {
    box.innerHTML = '<div class="fleet-empty">No sessions active in the last ' +
      esc(fmtDuration(data.window_s)) + ".</div>";
    return;
  }
  const current = state.run && state.run.session_id;
  const spot = noteFocus(box);
  box.innerHTML = data.sessions.map((s) => fleetRow(s, current)).join("");
  for (const row of box.querySelectorAll(".fleet-row")) {
    const open = () => { switchSession(row.dataset.session); setView("timeline"); };
    row.onclick = open;
    row.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    };
  }
  restoreFocus(box, spot);
}

// Announce a prompt or failure in a session you are NOT looking at — the case
// the current-session notifications cannot cover. Seeded like every other alert:
// whatever is already pending when the page opens is shown, not announced.
function checkFleetNotifications(data) {
  const viewing = state.run && state.run.session_id;
  const next = {};
  for (const s of data.sessions) {
    const att = s.attention && s.session_live && s.urgency > 0 ? s.attention : null;
    if (!att) continue;
    const key = att.kind + "@" + att.since;
    next[s.session_id] = key;
    if (state.fleetSeeded && state.notifyEnabled && s.session_id !== viewing &&
        state.fleetAttention[s.session_id] !== key) {
      notify(attentionTitle(att) + " — " + (s.title || s.project_name || s.session_id.slice(0, 8)),
        att.message || "");
    }
  }
  state.fleetAttention = next;
  state.fleetSeeded = true;
}

async function pollFleet() {
  if (state.fleetTimer) { clearTimeout(state.fleetTimer); state.fleetTimer = null; }
  try {
    const data = await api("/api/fleet");
    state.fleet = data;
    checkFleetNotifications(data);
    checkFleetSounds(data);
    renderFleetBadge();
    updateChrome();
    if (state.view === "fleet") renderFleet();
  } catch (err) { /* the fleet is an extra; the current session still works */ }
  if (state.live && !state.offline) state.fleetTimer = setTimeout(pollFleet, FLEET_POLL_MS);
}

// One place that knows everything to reset when the viewed session changes.
function switchSession(sessionId) {
  state.sessionId = sessionId;
  const picker = $("session-picker");
  if (picker) {
    if (![...picker.options].some((o) => o.value === sessionId)) {
      const option = document.createElement("option");
      option.value = sessionId;
      option.textContent = sessionId.slice(0, 8);
      picker.appendChild(option);
    }
    picker.value = sessionId;
  }
  if (state.replay.on) toggleReplay();      // a replay belongs to one session
  state.run = null;
  state.liveRun = null;
  // A different session's edges are all pre-existing history to us, not
  // events happening live — reseed instead of flashing every one of them.
  state.seenEdgeKeys = new Set();
  state.graphSeeded = false;
  // Same reasoning for notifications: a different session's existing
  // failures/end-state are history, not something to alert on.
  state.notifySeeded = false;
  state.knownFailedIds = new Set();
  state.knownLoopIds = new Set();
  state.knownAttention = "";
  state.knownBudget = "";
  state.soundMemo = null;           // reseed: another session's history is silent
  state.lastSessionLive = null;
  // A different session's agents are all pre-existing history — reseed so
  // switching sessions doesn't read as a burst of simultaneous activity.
  state.floorActivity = {};
  state.floorSeeded = false;
  state.agentPrevStatus = {};
  state.agentCelebrateUntil = {};
  startPolling();
  startStream();
}

// Live push. The server only says *that* something changed; the data still
// comes from /api/run, so there is one place anything is redacted. Without
// EventSource, or if the stream drops, the 2-second poll simply carries on.
function stopStream() {
  if (state.stream) { state.stream.close(); state.stream = null; }
  state.streamLive = false;
}

function scheduleRefresh() {
  if (!state.live || state.refreshTimer) return;
  state.refreshTimer = setTimeout(() => {
    state.refreshTimer = null;
    if (state.live) startPolling();
  }, STREAM_DEBOUNCE_MS);
}

function startStream() {
  stopStream();
  if (state.offline || typeof EventSource === "undefined") return;
  const session = state.sessionId ? "&session=" + encodeURIComponent(state.sessionId) : "";
  let source;
  try {
    source = new EventSource("/api/stream?k=" + encodeURIComponent(TOKEN) + session);
  } catch (err) { return; }
  state.stream = source;
  source.addEventListener("hello", () => { state.streamLive = true; });
  source.addEventListener("tick", scheduleRefresh);
  source.onerror = () => {
    // EventSource retries by itself; meanwhile go back to fast polling.
    const wasLive = state.streamLive;
    state.streamLive = false;
    if (wasLive) scheduleRefresh();
  };
}

// The picker names a session by its title when it has one. Plain text: it goes into textContent.
function sessionLabel(session, current) {
  return (session.title || session.session_id.slice(0, 8)) + " · " +
    session.agent_count + " agents" + (session.session_id === current ? " (current)" : "");
}

async function loadSessions() {
  try {
    const data = await api("/api/sessions");
    const picker = $("session-picker");
    picker.innerHTML = "";
    for (const session of data.sessions) {
      const option = document.createElement("option");
      option.value = session.session_id;
      option.textContent = sessionLabel(session, data.current);
      picker.appendChild(option);
    }
    picker.value = state.sessionId || data.current;
  } catch (err) { /* picker is optional; the run view still works */ }
}

// ------------------------------------------------------- command palette
//
// One box for "take me there": agents and commands answer instantly from the run
// already on the page; tool calls and files come from /api/search (or, in a static
// report, from the details baked into it). Keyboard first: Ctrl/Cmd+K or /.

const PALETTE_VIEWS = [
  ["timeline", "Timeline", "1"], ["graph", "Graph", "2"], ["agents", "Agents", "3"], ["insights", "Insights", "4"],
  ["spend", "Spend", "5"], ["prompts", "Prompts", "6"], ["activity", "Activity", "7"], ["workfloor", "Work Floor", "8"],
  ["fleet", "Fleet", "9"], ["history", "History", "0"],
];
const palette = { open: false, query: "", items: [], index: 0, remote: null, seq: 0, timer: null, opener: null };
const PALETTE_SEARCH_DELAY_MS = 160;

function isMac() {
  return typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || "");
}

function viewAvailable(view) {
  return !(state.offline && (view === "fleet" || view === "history"));
}

function matchTerms(query) {
  return String(query || "").toLowerCase().split(/\s+/).filter(Boolean).slice(0, 6);
}

function matchesAll(terms, text) {
  const haystack = String(text).toLowerCase();
  return terms.every((t) => haystack.indexOf(t) >= 0);
}

// Escaped first, marked second: a hostile agent name can never become markup.
function highlight(text, terms) {
  if (!terms.length) return esc(text);
  const pattern = new RegExp("(" + terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "ig");
  return String(text).split(pattern).map((part, i) => (i % 2 ? "<mark>" + esc(part) + "</mark>" : esc(part))).join("");
}

function baseName(path) {
  return String(path).replace(/[\\/]+$/, "").replace(/^.*[\\/]/, "") || String(path);
}

function paletteCommands() {
  const cmds = [];
  for (const [view, label, key] of PALETTE_VIEWS) {
    if (viewAvailable(view)) {
      cmds.push({ group: "Go to", title: label, hint: key, run: () => setView(view) });
    }
  }
  const live = $("live-toggle");
  if (live && !live.hidden) {
    cmds.push({ group: "Actions", title: state.live ? "Pause live updates" : "Resume live updates", hint: "L",
      run: () => live.click() });
  }
  cmds.push({ group: "Actions", title: "Replay the run", hint: "R", run: () => toggleReplay() });
  cmds.push({ group: "Actions", title: "Copy run summary", run: () => copySummary() });
  if (!state.offline) {
    cmds.push({ group: "Actions", title: "Export agents as CSV", run: () => downloadExport("csv") });
    cmds.push({ group: "Actions", title: "Export everything as JSON", run: () => downloadExport("json") });
  }
  const sound = $("sound-toggle");
  if (sound && !sound.hidden) cmds.push({ group: "Actions", title: "Toggle sound", run: () => sound.click() });
  cmds.push({ group: "Actions", title: "Change theme", hint: "T", run: () => cycleTheme() });
  cmds.push({ group: "Actions", title: "Keyboard shortcuts", hint: "?", run: () => openHelp() });
  return cmds;
}

function agentItem(agent) {
  const bits = [agent.agent_type, fmtModelShort(agent.model), agent.status].filter(Boolean);
  return { title: agent.description || agent.agent_id, sub: bits.join(" · "), dot: statusVar(agent.status),
    hint: agent.duration_s !== null && agent.duration_s !== undefined ? fmtDuration(agent.duration_s) : "",
    run: () => openDrawer(agent.agent_id) };
}

function buildPaletteItems() {
  const terms = matchTerms(palette.query);
  const run = state.run;
  const agents = run ? run.agents : [];
  const items = [];
  const add = (group, list) => list.forEach((item) => { item.group = group; items.push(item); });
  const commands = paletteCommands().filter((c) => !terms.length || matchesAll(terms, c.group + " " + c.title));
  if (!terms.length) {
    add("Go to", commands.filter((c) => c.group === "Go to"));
    const needs = agents.filter((a) => ["failed", "stalled", "waiting", "orphaned"].indexOf(a.status) >= 0 || a.loop);
    add("Needs attention", needs.slice(0, 6).map(agentItem));
    add("Actions", commands.filter((c) => c.group === "Actions"));
    return items;
  }
  add("Agents", agents.filter((a) => matchesAll(terms, [a.description, a.agent_id, a.agent_type, a.model, a.status,
    a.objective].join(" "))).slice(0, 8).map(agentItem));
  const remote = palette.remote && palette.remote.query === palette.query.trim() ? palette.remote.data : null;
  if (remote) {
    add("Tool calls", remote.tools.slice(0, 8).map((t) => ({
      title: t.tool + "  " + baseName(t.target), sub: t.target || t.description,
      hint: (t.count > 1 ? "×" + t.count + "  " : "") + t.description,
      run: () => openDrawer(t.agent_id) })));
    add("Files", remote.files.slice(0, 6).map((f) => {
      const first = (f.writers[0] || f.readers[0]);
      const parts = [];
      if (f.writers.length) parts.push(f.writers.length + (f.writers.length === 1 ? " writer" : " writers"));
      if (f.readers.length) parts.push(f.readers.length + (f.readers.length === 1 ? " reader" : " readers"));
      return { title: baseName(f.path), sub: f.path, hint: parts.join(", "), run: () => openDrawer(first) };
    }));
  }
  add("Commands", commands.slice(0, 6));
  return items;
}

function renderPalette() {
  const list = $("palette-list");
  const input = $("palette-input");
  if (!list) return;
  const terms = matchTerms(palette.query);
  palette.items = buildPaletteItems();
  palette.index = Math.min(palette.index, Math.max(0, palette.items.length - 1));
  if (!palette.items.length) {
    const searching = palette.query.trim().length >= 2 && !palette.remote && !state.offline;
    list.innerHTML = '<div class="palette-empty">' + (searching ? "Searching…"
      : palette.query.trim() ? "Nothing matches “" + esc(palette.query.trim()) + "”." : "Nothing to show yet.") + "</div>";
    if (input) input.removeAttribute("aria-activedescendant");
    return;
  }
  let html = "";
  let group = "";
  palette.items.forEach((item, i) => {
    if (item.group !== group) {
      group = item.group;
      html += '<div class="pal-group" role="presentation">' + esc(group) + "</div>";
    }
    html += '<button type="button" class="pal-item" role="option" id="pal-' + i + '" data-i="' + i +
      '" aria-selected="' + (i === palette.index) + '" tabindex="-1">' +
      (item.dot ? '<span class="pal-dot" style="background:' + item.dot + '"></span>' : "") +
      '<span class="pal-main"><span class="pal-title">' + highlight(item.title, terms) + "</span>" +
      (item.sub ? '<span class="pal-sub">' + highlight(item.sub, terms) + "</span>" : "") + "</span>" +
      (item.hint ? '<span class="pal-hint">' + esc(item.hint) + "</span>" : "") + "</button>";
  });
  list.innerHTML = html;
  if (input) input.setAttribute("aria-activedescendant", "pal-" + palette.index);
}

function setPaletteIndex(next, scroll) {
  if (!palette.items.length) return;
  const count = palette.items.length;
  palette.index = (next + count) % count;
  const list = $("palette-list");
  for (const el of list.querySelectorAll(".pal-item")) {
    const on = Number(el.getAttribute("data-i")) === palette.index;
    el.setAttribute("aria-selected", String(on));
    if (on && scroll && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
  }
  const input = $("palette-input");
  if (input) input.setAttribute("aria-activedescendant", "pal-" + palette.index);
}

function runPaletteItem(index) {
  const item = palette.items[index];
  if (!item) return;
  closePalette(true);
  item.run();
}

// A static report has no server to ask, so it searches the details baked into it.
function localSearch(query) {
  const terms = matchTerms(query);
  const out = { query: query, agents: [], tools: [], files: [], truncated: false };
  const details = (typeof window !== "undefined" && window.ORCHESTRA_DETAILS) || {};
  const files = {};
  for (const id of Object.keys(details)) {
    const agent = details[id];
    for (const call of agent.tool_calls || []) {
      if (matchesAll(terms, call.name + " " + call.target + " " + agent.description) && out.tools.length < 60) {
        out.tools.push({ agent_id: id, description: agent.description, tool: call.name,
          target: call.target, timestamp: call.timestamp, count: 1 });
      }
    }
    for (const [role, paths] of [["writers", agent.files_written || []], ["readers", agent.files_read || []]]) {
      for (const path of paths) {
        if (!matchesAll(terms, path)) continue;
        const slot = files[path] || (files[path] = { path: path, writers: [], readers: [] });
        slot[role].push(id);
      }
    }
  }
  out.tools.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
  out.files = Object.keys(files).map((p) => files[p]).slice(0, 15);
  return out;
}

function schedulePaletteSearch() {
  if (palette.timer) clearTimeout(palette.timer);
  const query = palette.query.trim();
  if (query.length < 2) { palette.remote = null; return; }
  palette.timer = setTimeout(async () => {
    const seq = ++palette.seq;
    let data = null;
    try {
      data = state.offline ? localSearch(query) : await api("/api/search?q=" + encodeURIComponent(query));
    } catch (err) { data = null; }
    if (seq !== palette.seq || !palette.open || !data) return;
    palette.remote = { query: query, data: data };
    renderPalette();
  }, PALETTE_SEARCH_DELAY_MS);
}

function openPalette(prefill) {
  const root = $("palette");
  if (!root || palette.open) return;
  closeHelp();
  palette.open = true;
  palette.opener = document.activeElement;
  palette.query = prefill || "";
  palette.index = 0;
  palette.remote = null;
  root.hidden = false;
  const input = $("palette-input");
  input.value = palette.query;
  renderPalette();
  input.focus();
}

function closePalette(keepFocus) {
  if (!palette.open) return;
  palette.open = false;
  palette.seq += 1;
  if (palette.timer) clearTimeout(palette.timer);
  $("palette").hidden = true;
  const back = palette.opener;
  palette.opener = null;
  if (!keepFocus && back && back.focus) back.focus();
}

const SHORTCUTS = [
  [["Ctrl K", "/"], "Search agents, tool calls, files and commands"],
  [["1", "–", "0"], "Switch view"],
  [["L"], "Pause or resume live updates"],
  [["R"], "Replay the run"],
  [["F"], "Filter agents"],
  [["T"], "Change theme"],
  [["?"], "Show this list"],
  [["Esc"], "Close a dialog or the agent panel"],
];

function helpOpen() {
  const root = $("help");
  return !!root && !root.hidden;
}

function openHelp() {
  const root = $("help");
  if (!root) return;
  closePalette(true);
  const list = $("help-list");
  list.innerHTML = SHORTCUTS.map(([keys, label]) =>
    "<dt>" + keys.map((k) => "<kbd>" + esc(isMac() ? k.replace("Ctrl", "⌘") : k) + "</kbd>").join("") +
    "</dt><dd>" + esc(label) + "</dd>").join("");
  palette.opener = document.activeElement;
  root.hidden = false;
}

function closeHelp() {
  const root = $("help");
  if (!root || root.hidden) return;
  root.hidden = true;
  const back = palette.opener;
  palette.opener = null;
  if (back && back.focus) back.focus();
}

function isTyping(target) {
  const tag = target && target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!(target && target.isContentEditable);
}

function onGlobalKey(event) {
  if (event.key === "Escape") {
    if (palette.open) { closePalette(); return; }
    if (helpOpen()) { closeHelp(); return; }
  }
  if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (palette.open) closePalette(); else openPalette();
    return;
  }
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  // A focused control already used this key (the graph fits itself on 0): no shortcut as well.
  if (event.defaultPrevented) return;
  if (palette.open || helpOpen() || isTyping(event.target)) return;
  const key = event.key;
  if (key === "/") { event.preventDefault(); openPalette(); return; }
  if (key === "?") { event.preventDefault(); openHelp(); return; }
  if (key === "t" || key === "T") { cycleTheme(); return; }
  if (key === "r" || key === "R") { toggleReplay(); return; }
  if (key === "f" || key === "F") {
    const filter = $("filter-text");
    if (filter) { event.preventDefault(); filter.focus(); }
    return;
  }
  if (key === "l" || key === "L") {
    const live = $("live-toggle");
    if (live && !live.hidden) live.click();
    return;
  }
  const index = "1234567890".indexOf(key);
  if (index >= 0 && key.length === 1 && viewAvailable(PALETTE_VIEWS[index][0])) setView(PALETTE_VIEWS[index][0]);
}

function setupPalette() {
  const input = $("palette-input");
  const root = $("palette");
  const list = $("palette-list");
  const trigger = $("palette-open");
  if (trigger) {
    trigger.onclick = () => openPalette();
    const hint = trigger.querySelector ? trigger.querySelector("kbd") : null;
    if (hint) hint.textContent = isMac() ? "⌘ K" : "Ctrl K";
  }
  const help = $("help-open");
  if (help) help.onclick = () => openHelp();
  const helpRoot = $("help");
  if (helpRoot) helpRoot.onmousedown = (event) => { if (event.target === helpRoot) closeHelp(); };
  if (root) root.onmousedown = (event) => { if (event.target === root) closePalette(); };
  if (input) {
    input.oninput = () => {
      palette.query = input.value;
      palette.index = 0;
      renderPalette();
      schedulePaletteSearch();
    };
    input.onkeydown = (event) => {
      const key = event.key;
      if (key === "ArrowDown") { event.preventDefault(); setPaletteIndex(palette.index + 1, true); }
      else if (key === "ArrowUp") { event.preventDefault(); setPaletteIndex(palette.index - 1, true); }
      else if (key === "Home") { event.preventDefault(); setPaletteIndex(0, true); }
      else if (key === "End") { event.preventDefault(); setPaletteIndex(palette.items.length - 1, true); }
      else if (key === "Enter") { event.preventDefault(); runPaletteItem(palette.index); }
      else if (key === "Tab") { event.preventDefault(); }       // a one-field dialog: focus stays put
      else return;
      event.stopPropagation();
    };
  }
  if (list) {
    list.onclick = (event) => {
      const el = event.target.closest ? event.target.closest(".pal-item") : null;
      if (el) runPaletteItem(Number(el.getAttribute("data-i")));
    };
    list.onmousemove = (event) => {
      const el = event.target.closest ? event.target.closest(".pal-item") : null;
      if (el && Number(el.getAttribute("data-i")) !== palette.index) setPaletteIndex(Number(el.getAttribute("data-i")), false);
    };
  }
  document.addEventListener("keydown", onGlobalKey);
}

// -------------------------------------------------------------- theme + toast

function themeMode() {
  try {
    const stored = localStorage.getItem("orchestra-theme");
    if (stored === "light" || stored === "dark") return stored;
  } catch (err) { /* blocked storage: follow the system */ }
  return "auto";
}

function applyTheme(mode) {
  const root = typeof document !== "undefined" ? document.documentElement : null;
  if (root && root.setAttribute) {
    if (mode === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", mode);
  }
  try {
    if (mode === "auto") localStorage.removeItem("orchestra-theme");
    else localStorage.setItem("orchestra-theme", mode);
  } catch (err) { /* blocked storage: not worth failing over */ }
  const btn = $("theme-toggle");
  if (btn) {
    const label = "Theme: " + (mode === "auto" ? "automatic" : mode);
    btn.setAttribute("aria-label", label);
    btn.title = label;
  }
}

function cycleTheme() {
  const order = ["auto", "light", "dark"];
  const next = order[(order.indexOf(themeMode()) + 1) % order.length];
  applyTheme(next);
  toast("Theme: " + (next === "auto" ? "automatic" : next));
}

let toastTimer = null;
function toast(message) {
  const el = $("toast");
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2200);
}

function init() {
  applyTheme(themeMode());
  setupFloorGroup();
  setupPalette();
  setupActivitySearch();
  setupPulse();
  setupGraphInteractions();
  const themeBtn = $("theme-toggle");
  if (themeBtn) themeBtn.onclick = cycleTheme;
  $("live-toggle").onclick = (event) => {
    state.live = !state.live;
    event.target.setAttribute("aria-pressed", String(state.live));
    event.target.textContent = state.live ? "Live" : "Paused";
    if (state.live) { startPolling(); startStream(); pollFleet(); }
    else stopStream();
  };
  $("session-picker").onchange = (event) => switchSession(event.target.value);
  if (notificationsSupported()) {
    let stored = "0";
    try {
      if (typeof localStorage !== "undefined") {
        stored = localStorage.getItem("orchestra-notify") || "0";
      }
    } catch (err) { /* fall back to off */ }
    state.notifyEnabled = stored === "1" && Notification.permission === "granted";
    updateNotifyButton();
    $("notify-toggle").onclick = async () => {
      if (Notification.permission === "granted") {
        state.notifyEnabled = !state.notifyEnabled;
      } else {
        const permission = await Notification.requestPermission();
        state.notifyEnabled = permission === "granted";
      }
      setStoredNotifyPref(state.notifyEnabled);
      updateNotifyButton();
    };
  } else {
    $("notify-toggle").hidden = true;
  }
  $("copy-summary").onclick = copySummary;
  const replayBtn = $("replay-toggle");
  if (replayBtn) replayBtn.onclick = toggleReplay;
  const exportSelect = $("export-select");
  if (exportSelect && !state.offline) {
    exportSelect.hidden = false;
    exportSelect.onchange = () => {
      if (exportSelect.value) downloadExport(exportSelect.value);
      exportSelect.value = "";        // it is a menu, not a setting
    };
  }
  const soundBtn = $("sound-toggle");
  const hasAudio = typeof window !== "undefined" && (window.AudioContext || window.webkitAudioContext);
  if (soundBtn && !state.offline && hasAudio) {
    let stored = "0";
    try {
      if (typeof localStorage !== "undefined") stored = localStorage.getItem("orchestra-sound") || "0";
    } catch (err) { /* off */ }
    soundBtn.hidden = false;
    setupSoundPrefs();
    // A remembered "on" still needs a fresh click before the browser lets this
    // page make noise, so the first click after a reload re-arms it.
    state.soundEnabled = stored === "1";
    updateSoundButton();
    // Browsers keep an audio context silent until a gesture; the first click
    // anywhere on the page re-arms it, so a remembered "on" works after reload.
    document.addEventListener("click", () => {
      const ctx = state.soundEnabled ? audioContext() : null;
      if (ctx && ctx.state === "suspended" && typeof ctx.resume === "function") ctx.resume();
    }, { once: true });
    soundBtn.onclick = () => {
      state.soundEnabled = !state.soundEnabled;
      setStoredSoundPref(state.soundEnabled);
      updateSoundButton();
      if (state.soundEnabled) { state.lastSoundAt = 0; playSound("done"); }  // hear what you turned on
    };
  }
  // Only where the browser can do it (Chromium); a static report has no use.
  const pillBtn = $("pill-toggle");
  if (pillBtn && !state.offline && typeof window.documentPictureInPicture !== "undefined") {
    pillBtn.hidden = false;
    pillBtn.onclick = togglePill;
  }
  for (const tab of document.querySelectorAll(".tab")) {
    tab.onclick = () => setView(tab.dataset.view);
    // Arrow keys move between tabs, as in any tablist; Tab leaves the list.
    tab.onkeydown = (event) => {
      const shown = Array.from(document.querySelectorAll(".tab")).filter((t) => !t.hidden);
      const at = shown.indexOf(tab);
      let next = -1;
      if (event.key === "ArrowRight") next = (at + 1) % shown.length;
      else if (event.key === "ArrowLeft") next = (at - 1 + shown.length) % shown.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = shown.length - 1;
      if (next < 0) return;
      event.preventDefault();
      shown[next].focus();
      setView(shown[next].dataset.view);
    };
  }
  $("filter-text").oninput = (event) => {
    state.filterText = event.target.value;
    $("filter-clear").hidden = !state.filterText;
    render();
  };
  $("filter-clear").onclick = () => {
    state.filterText = "";
    const input = $("filter-text");
    input.value = "";
    input.focus();
    $("filter-clear").hidden = true;
    render();
  };
  window.addEventListener("resize", () => render());
  // A static report has nothing to tick (see state.offline) and, more
  // concretely, must not leave a live timer running: Node's event loop
  // never exits with one pending, which is exactly what hung the
  // report-rendering test suite until this was scoped to the live dashboard.
  if (!state.offline) setInterval(tickAgentClocks, 500);
  loadSessions();
  startPolling();
  startStream();
  if (state.offline) {
    // A frozen snapshot has no other sessions to list.
    for (const tab of document.querySelectorAll(".tab")) {
      if (tab.dataset.view === "fleet" || tab.dataset.view === "history") tab.hidden = true;
    }
  } else {
    pollFleet();
  }
  // A #agent=<id> link (pasted from a health-box item, a ticker row, or an
  // earlier session) opens straight to that agent's drawer. openDrawer fetches
  // independently of run state, so this doesn't need to wait for the first poll.
  const linked = parseHash();
  if (linked.view && VIEWS.indexOf(linked.view) >= 0 &&
      !(state.offline && (linked.view === "fleet" || linked.view === "history"))) {
    setView(linked.view);
  }
  if (linked.agent) openDrawer(linked.agent);
}

document.addEventListener("DOMContentLoaded", init);

// -------------------------------------------------------------- insights
//
// Run-level answers, computed server-side (orchestra/insights.py) and drawn here:
// how parallel the run was, which chain of agents set its length, which tools
// and files dominated, and how well the prompt cache worked.

const BUCKET_VARS = { Read: "tool-read", Edit: "tool-edit", Bash: "tool-bash", Task: "tool-task", Other: "tool-other" };

function statusVar(status) {
  const known = ["running", "completed", "failed", "stalled", "orphaned", "waiting"];
  return "var(--" + (known.indexOf(status) >= 0 ? status : "unknown") + ")";
}

function insMetric(value, label) {
  return '<div class="metric"><strong>' + esc(value) + "</strong><span>" + esc(label) + "</span></div>";
}

// A card's key: its title as a slug, "What went wrong" -> "what-went-wrong".
function cardKey(title) {
  return String(title).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

// The keys of folded Insights cards, remembered in this browser when it allows.
function foldedCards() {
  if (!state.folded) {
    let keys = [];
    try {
      const raw = typeof localStorage !== "undefined" ? JSON.parse(localStorage.getItem("cuelight-folded") || "[]") : [];
      if (Array.isArray(raw)) keys = raw.filter((k) => typeof k === "string");
    } catch (err) { /* blocked or unreadable: start unfolded */ }
    state.folded = new Set(keys);
  }
  return state.folded;
}

function saveFolded() {
  try { if (typeof localStorage !== "undefined") localStorage.setItem("cuelight-folded", JSON.stringify([...foldedCards()])); }
  catch (err) { /* private window: folding lasts for this visit */ }
}

// While Insights draws (state.cardsFold), its cards fold and the title is the toggle; a folded
// card shows its headline (state.cardHeadlines, from insHeadlines). Cards elsewhere do not fold.
function insCard(title, sub, body, cls) {
  const key = cardKey(title);
  const open = '<section class="card' + (cls ? " " + cls : "");
  if (!state.cardsFold) {
    return open + '" data-card="' + key + '"><h3>' + esc(title) + "</h3>" +
      (sub ? '<p class="card-sub">' + esc(sub) + "</p>" : "") + body + "</section>";
  }
  const folded = foldedCards().has(key);
  const head = '<h3><button type="button" class="card-fold" data-fold="' + key + '" aria-expanded="' + !folded + '">' +
    esc(title) + "</button></h3>";
  if (folded) {
    const line = (state.cardHeadlines || {})[key];
    return open + ' folded" data-card="' + key + '">' + head +
      (line ? '<p class="card-headline" data-tone="' + line.tone + '">' + esc(line.text) + "</p>" : "") + "</section>";
  }
  return open + '" data-card="' + key + '">' + head + (sub ? '<p class="card-sub">' + esc(sub) + "</p>" : "") + body + "</section>";
}

function plural(n, one, many) {
  return n + " " + (n === 1 ? one : many);
}

// One line per Insights card that has something to say: "bad" (something is wrong), "warn" (worth a
// look) or "info" (a plain fact). Worst first, then card order. Read from the cards' own data.
function insHeadlines(ins, run) {
  const out = [];
  const add = (title, text, tone) => out.push({ key: cardKey(title), text: text, tone: tone });
  const p = ins.parallelism;
  if (p && p.peak > 1) add("Parallelism", "peak " + p.peak + " agents at once", "info");
  const c = ins.critical_path;
  if (c && c.chain && c.chain.length > 1) add("Critical path", "critical path " + fmtPct(c.share) + " of the run", "info");
  const o = ins.outcomes;
  if (o) {
    const made = [];
    if (o.commits) made.push(plural(o.commits, "commit", "commits"));
    if (o.prs) made.push(plural(o.prs, "PR", "PRs"));
    if (o.pushes && !o.prs) made.push(plural(o.pushes, "push", "pushes"));
    if (made.length) add("What the run produced", made.join(", "), "info");
  }
  const k = ins.checks;
  if (k && k.edited) {
    const n = k.counts;
    const parts = [];
    if (n.failing) parts.push(plural(n.failing, "failing check", "failing checks"));
    if (n.unchecked) parts.push(n.unchecked + " unchecked");
    add("Did they check their work?", parts.length ? parts.join(", ") : n.checked + " of " + k.edited + " checked their work",
      n.failing ? "bad" : n.unchecked ? "warn" : "info");
  }
  const e = ins.errors;
  if (e) {
    const parts = [];
    if (e.stuck.length) parts.push(plural(e.stuck.length, "agent stuck retrying", "agents stuck retrying"));
    if (e.api_count) {
      parts.push(plural(e.api_count, "API stall", "API stalls") + (e.api_lost_s ? " (" + fmtDuration(e.api_lost_s) + ")" : ""));
    }
    if (e.failed) parts.push(plural(e.failed, "failed call", "failed calls"));
    if (parts.length) add("What went wrong", parts.slice(0, 2).join(", "), e.stuck.length ? "bad" : e.api_count ? "warn" : "info");
  }
  const w = ins.waits;
  if (w && w.count) add("Waiting on you", "waited on you " + fmtDuration(w.you_s), w.open ? "warn" : "info");
  const ws = ins.waste;
  if (ws && ws.rebuilds) add("Where tokens were wasted", fmtCount(ws.rebuilt_tokens) + " tokens rewritten to the cache", "warn");
  const pr = ins.pressure;
  if (pr && pr.near.length) {
    add("How full each context got", plural(pr.near.length, "context near its window", "contexts near their window"), "bad");
  } else if (pr && pr.main) {
    add("How full each context got", "context peaked at " + fmtPct(pr.main.fill), pr.main.fill >= pr.near_at ? "warn" : "info");
  }
  const ch = ins.changes;
  if (ch && ch.files) add("What changed", "+" + fmtCount(ch.added) + " −" + fmtCount(ch.removed) + " in " + plural(ch.files, "file", "files"), "info");
  const cx = ins.context;
  if (cx && cx.missing.length) {
    add("What each agent was told", plural(cx.missing.length, "agent without your instructions", "agents without your instructions"), "warn");
  }
  const t = ins.tokens;
  if (t && t.cache_hit_ratio !== null && t.cache_hit_ratio !== undefined) add("Tokens and cache", fmtPct(t.cache_hit_ratio) + " from the cache", "info");
  const cost = run.cost;
  if (cost && cost.enabled) {
    const b = cost.budget;
    add("Spend", b ? fmtMoney(cost.total, cost.currency) + " of " + fmtMoney(b.limit, cost.currency) : fmtMoney(cost.total, cost.currency) + " spent",
      b && b.state === "exceeded" ? "bad" : b && b.state === "warn" ? "warn" : "info");
    out[out.length - 1].view = "spend";       // Spend is a tab of its own: the chip opens it
  }
  const f = ins.files;
  if (f && f.contended && f.contended.length) {
    add("Files", plural(f.contended.length, "file written by more than one agent", "files written by more than one agent"), "warn");
  }
  const rank = { bad: 0, warn: 1, info: 2 };
  return out.map((line, i) => [line, i]).sort((a, b) => rank[a[0].tone] - rank[b[0].tone] || a[1] - b[1]).map((pair) => pair[0]);
}

// The strip above the cards: a chip per headline that jumps to its card, and fold or unfold all.
function insGlance(lines) {
  const anyFolded = foldedCards().size > 0;
  return '<nav class="glance" aria-label="Insights at a glance">' + lines.map((line) =>
    '<button type="button" class="glance-chip" data-tone="' + line.tone + '" ' +
    (line.view ? 'data-view="' + line.view + '"' : 'data-jump="' + line.key + '"') + ">" +
    esc(line.text) + "</button>").join("") +
    '<button type="button" class="glance-fold" data-fold-all="' + (anyFolded ? "unfold" : "fold") + '">' +
    (anyFolded ? "Unfold all" : "Fold all") + "</button></nav>";
}

function toggleFold(key) {
  const folded = foldedCards();
  if (folded.has(key)) folded.delete(key); else folded.add(key);
  saveFolded();
  if (state.run) renderInsights(state.run);
  const again = document.querySelector('#insights [data-fold="' + key + '"]');
  if (again) again.focus();      // the button was redrawn: keep keyboard focus on it
}

function foldAll(fold) {
  const folded = foldedCards();
  folded.clear();
  if (fold) for (const el of document.querySelectorAll("#insights [data-card]")) folded.add(el.getAttribute("data-card"));
  saveFolded();
  if (state.run) renderInsights(state.run);
}

function insEmpty(text) {
  return '<p class="card-empty">' + esc(text) + "</p>";
}

// One ranked row: a name, a bar sized against the largest value, and a figure.
function insRank(rows) {
  const max = Math.max.apply(null, rows.map((r) => r.value).concat([1e-9]));
  return '<div class="rank">' + rows.map((r) =>
    '<div class="rank-row"' + (r.agent ? ' data-agent="' + esc(r.agent) + '" role="button" tabindex="0"' : "") + ">" +
    '<span class="rank-name" title="' + esc(r.title || r.name) + '">' + esc(r.name) +
    (r.note ? "<small>" + esc(r.note) + "</small>" : "") + "</span>" +
    '<span class="rank-track"><i style="width:' + Math.max(2, (r.value / max) * 100).toFixed(1) +
    "%" + (r.color ? ";background:" + r.color : "") + '"></i></span>' +
    '<span class="rank-val">' + esc(r.label) + "</span></div>").join("") + "</div>";
}

function insStepChart(p, width) {
  const H = 150;
  const left = 26;
  const right = 6;
  const top = 10;
  const bottom = 22;
  const plotW = Math.max(60, width - left - right);
  const plotH = H - top - bottom;
  const t0 = p.start;
  const span = Math.max(p.end - p.start, 1e-6);
  const x = (t) => left + ((t - t0) / span) * plotW;
  const peak = Math.max(p.peak, 1);
  const y = (n) => top + plotH - (n / peak) * plotH;
  let d = "M" + x(p.series[0][0]).toFixed(1) + "," + y(0).toFixed(1);
  for (let i = 0; i < p.series.length; i++) {
    const [t, n] = p.series[i];
    d += " L" + x(t).toFixed(1) + "," + (i ? y(p.series[i - 1][1]) : y(0)).toFixed(1) +
      " L" + x(t).toFixed(1) + "," + y(n).toFixed(1);
  }
  d += " L" + x(p.end).toFixed(1) + "," + y(p.series[p.series.length - 1][1]).toFixed(1) +
    " L" + x(p.end).toFixed(1) + "," + y(0).toFixed(1) + " Z";
  let grid = "";
  const stride = Math.max(1, Math.ceil(peak / 5));   // whole agents only: 0, 2, 4, 6
  for (let n = 0; n <= peak; n += stride) {
    grid += '<line class="ins-grid-line" x1="' + left + '" x2="' + (left + plotW) + '" y1="' + y(n).toFixed(1) +
      '" y2="' + y(n).toFixed(1) + '"/><text class="ins-axis" x="' + (left - 6) + '" y="' + (y(n) + 3.5).toFixed(1) +
      '" text-anchor="end">' + n + "</text>";
  }
  const ticks = [0, 0.5, 1].map((f) =>
    '<text class="ins-axis" x="' + (left + f * plotW).toFixed(1) + '" y="' + (H - 5) + '" text-anchor="' +
    (f === 0 ? "start" : f === 1 ? "end" : "middle") + '">' + esc(fmtDuration(f * span)) + "</text>").join("");
  const avgY = y(Math.min(p.average, peak)).toFixed(1);
  return '<svg class="ins-chart" viewBox="0 0 ' + width + " " + H + '" width="' + width + '" height="' + H +
    '" role="img" aria-label="Agents running over time. Peak ' + p.peak + ", average " + p.average.toFixed(1) + '.">' +
    grid + '<path class="ins-area" d="' + d + '"/>' +
    '<line class="ins-avg" x1="' + left + '" x2="' + (left + plotW) + '" y1="' + avgY + '" y2="' + avgY + '"/>' +
    '<text class="ins-avg-label" x="' + (left + plotW - 4) + '" y="' + (Number(avgY) - 5) +
    '" text-anchor="end">average ' + p.average.toFixed(1) + "</text>" + ticks + "</svg>";
}

function insParallelism(ins, width) {
  const p = ins.parallelism;
  if (!p) return insCard("Parallelism", "", insEmpty("No agent has started yet."), "wide");
  const metrics = '<div class="metrics">' + insMetric(p.peak, "peak agents at once") +
    insMetric(p.average.toFixed(1), "average running") +
    insMetric(fmtPct(p.solo_share), "of the run with a single agent") +
    insMetric(fmtDuration(p.idle_s), "with nothing running") + "</div>";
  return insCard("Parallelism",
    "How many agents were running at each moment. A run that is mostly one agent is a run that could be split.",
    metrics + insStepChart(p, width), "wide");
}

function insCritical(ins) {
  const c = ins.critical_path;
  if (!c) return insCard("Critical path", "", insEmpty("No dependency chain yet."), "wide");
  const max = Math.max.apply(null, c.chain.map((s) => s.duration_s).concat([1e-9]));
  const steps = c.chain.map((s) =>
    '<div class="chain-step" data-agent="' + esc(s.agent_id) + '" role="button" tabindex="0">' +
    "<b>" + esc(s.description || s.agent_id) + "</b>" +
    "<span>" + esc(s.status) + " · " + esc(fmtDuration(s.duration_s)) + "</span>" +
    '<span class="bar-mini" style="color:' + statusVar(s.status) + '"><i style="width:' +
    Math.max(4, (s.duration_s / max) * 100).toFixed(0) + '%"></i></span></div>').join("");
  const sub = c.chain.length > 1
    ? "These agents set the length of the run: " + fmtDuration(c.duration_s) + ", " + fmtPct(c.share) +
      " of the wall time. Making any other agent faster will not finish the run sooner."
    : "No agent waited on another, so the longest single agent sets the length of the run.";
  return insCard("Critical path", sub, '<div class="chain">' + steps + "</div>", "wide");
}

function insTools(ins) {
  const t = ins.tools;
  if (!t || !t.total) return insCard("Tool use", "", insEmpty("No tool calls yet."));
  const split = '<div class="split">' + t.buckets.map((b) =>
    '<span style="width:' + (b.share * 100).toFixed(1) + "%;background:var(--" + BUCKET_VARS[b.name] + ')" title="' +
    esc(b.name + " " + b.count) + '"></span>').join("") + "</div>" +
    '<div class="legend-row">' + t.buckets.map((b) =>
      '<span><i class="swatch" style="background:var(--' + BUCKET_VARS[b.name] + ')"></i>' +
      esc(b.name) + " " + fmtCount(b.count) + "</span>").join("") + "</div>";
  const rows = insRank(t.by_tool.map((r) => ({
    name: r.name, value: r.count, label: fmtCount(r.count),
    note: r.agents + (r.agents === 1 ? " agent" : " agents"), color: "var(--" + BUCKET_VARS[r.bucket] + ")" })));
  const meta = [t.total + " calls"];
  if (t.per_minute) meta.push(t.per_minute.toFixed(1) + " per minute");
  if (t.busiest) meta.push("busiest: " + (t.busiest.description || t.busiest.agent_id) + " (" + t.busiest.calls + ")");
  return insCard("Tool use", meta.join(" · "), split + rows);
}

function insTokens(ins, run) {
  const k = ins.tokens;
  const ratio = k.cache_hit_ratio;
  let body = "";
  if (ratio === null || ratio === undefined) {
    body += insEmpty("No token usage recorded yet.");
  } else {
    body += '<div class="ratio"><strong>' + esc(fmtPct(ratio)) + "</strong><span>of input served from the prompt cache</span></div>" +
      '<div class="ratio-track"><i style="width:' + (ratio * 100).toFixed(1) + '%"></i></div>';
  }
  if (k.top_agents.length) {
    body += "<h4>Who used the most fresh tokens</h4>" + insRank(k.top_agents.map((a) => ({
      name: a.description || a.agent_id, agent: a.agent_id, value: a.fresh,
      label: fmtCount(a.fresh) + " · " + fmtPct(a.share) })));
  }
  if (k.by_model.length) {
    const currency = run.cost && run.cost.currency;
    body += "<h4>By model</h4>" + insRank(k.by_model.map((m) => ({
      name: fmtModelShort(m.model), title: m.model, value: m.fresh,
      label: fmtCount(m.fresh) + (m.cost !== null && m.cost !== undefined && currency ? " · " + fmtMoney(m.cost, currency) : "") })));
  }
  return insCard("Tokens and cache",
    "Fresh tokens are what was actually processed (input, output and new cache writes), not read back from the cache.", body);
}

function insFiles(ins) {
  const f = ins.files;
  let body = "";
  if (f.contended.length) {
    body += "<h4>Written by more than one agent</h4>" + insRank(f.contended.map((r) => ({
      name: r.path.replace(/^.*[\\/]/, ""), title: r.path, value: r.agents, label: r.agents + " writers", color: "var(--failed)" })));
  }
  if (f.read.length) {
    body += "<h4>Read by the most agents</h4>" + insRank(f.read.map((r) => ({
      name: r.path.replace(/^.*[\\/]/, ""), title: r.path, value: r.agents, label: r.agents + " readers" })));
  }
  if (!body) body = insEmpty("No file is shared between agents yet.");
  return insCard("Files",
    f.files_written + " written, " + f.files_read + " read. A file with several writers is a correctness risk.", body, "wide");
}

function insSlowest(ins) {
  if (!ins.slowest.length) return insCard("Longest-running agents", "", insEmpty("Nothing has finished yet."));
  return insCard("Longest-running agents", "Time spent in a round, whether or not the agent was on the critical path.",
    insRank(ins.slowest.map((a) => ({
      name: a.description || a.agent_id, agent: a.agent_id, value: a.duration_s,
      label: fmtDuration(a.duration_s), color: statusVar(a.status) }))));
}

// A duration that is still growing: tickWaits() keeps its text current between polls.
function liveSpan(base, since, now) {
  return '<span class="wait-live" data-wait-base="' + Number(base || 0) + '" data-wait-since="' + Number(since) + '">' +
    esc(fmtDuration((base || 0) + Math.max(0, now - since))) + "</span>";
}

function waitNow(run) {
  const w = run && run.insights && run.insights.waits;
  return state.offline && w ? w.now : Date.now() / 1000;
}

// How long agents sat on prompts only you could answer. Overlapping waits count once
// in "your time" and add up in "agent time": three agents stuck together for five
// minutes are five minutes of yours and fifteen of theirs.
function insWaits(ins, run) {
  const w = ins.waits;
  const title = "Waiting on you";
  if (!w) {
    return insCard(title, "", insEmpty(run.live
      ? "No agent has waited on a permission or input prompt in this run."
      : "Prompts are known from the plugin's hooks, and none have reported for this session."));
  }
  const now = waitNow(run);
  // While anything is waiting, your time grows by exactly one second per second.
  const yours = w.open ? liveSpan(w.you_s, w.now, now) : esc(fmtDuration(w.you_s));
  const metrics = '<div class="metrics">' +
    '<div class="metric"><strong>' + yours + "</strong><span>of your time with an agent held up</span></div>" +
    insMetric(fmtDuration(w.agent_s), "agent time lost, every wait added") +
    insMetric(w.count, w.count === 1 ? "wait" : "waits") +
    (w.longest ? insMetric(fmtDuration(w.longest.seconds), "longest: " + w.longest.label) : "") + "</div>";
  const notes = [];
  if (w.open) notes.push(w.open + (w.open === 1 ? " agent is" : " agents are") + " waiting on you now.");
  if (w.unanswered) notes.push(w.unanswered + (w.unanswered === 1 ? " prompt was" : " prompts were") +
    " still up when the session went quiet, so it has no length.");
  const rows = insRank(w.by_agent.map((a) => ({
    name: a.label, agent: a.agent_id || null, value: a.seconds + (a.open_since ? Math.max(0, now - w.now) : 0),
    label: fmtDuration(a.seconds),
    note: a.count + (a.count === 1 ? " wait" : " waits") + (a.open_since ? ", waiting now" : ""), color: "var(--waiting)" })));
  const recent = '<ol class="wait-list">' + w.recent.map((r) =>
    '<li class="wait-' + esc(r.state) + '"><span class="wait-when">' + esc(fmtClock(r.start)) + "</span>" +
    '<span class="wait-who">' + esc(r.label) + (r.message ? "<small>" + esc(r.message) + "</small>" : "") + "</span>" +
    '<span class="wait-len">' + (r.state === "open" ? "waiting " + liveSpan(0, r.start, now)
      : r.state === "unanswered" ? "never answered" : esc(fmtDuration(r.seconds))) + "</span></li>").join("") + "</ol>";
  return insCard(title,
    "Time agents sat on a permission or input prompt. A wait ends when the agent moves again, so an approved command's own run time is included.",
    metrics + (notes.length ? '<p class="card-note">' + esc(notes.join(" ")) + "</p>" : "") +
    "<h4>By agent</h4>" + rows + "<h4>Latest prompts</h4>" + recent, "wide");
}

// Did the agents that edited code run a test, build, type check or lint afterwards?
function insChecks(ins) {
  const c = ins.checks;
  const title = "Did they check their work?";
  if (!c) return "";
  const notes = c.pattern_error ? '<p class="card-note">' + esc(c.pattern_error) + "</p>" : "";
  if (!c.edited) return insCard(title, "", notes + insEmpty("No agent has edited a code file yet."));
  const n = c.counts;
  const metrics = '<div class="metrics">' +
    insMetric(n.checked + " of " + c.edited, "ran a check after their last edit") +
    insMetric(n.failing, "finished with a failing check") +
    insMetric(n.unchecked, "ran no check after their last edit") + "</div>";
  const split = '<div class="split">' + [["checked", "completed"], ["failing", "failed"], ["unchecked", "stalled"]]
    .filter(([k]) => n[k]).map(([k, color]) =>
      '<span style="width:' + ((n[k] / c.edited) * 100).toFixed(1) + "%;background:var(--" + color + ')" title="' +
      esc(k + " " + n[k]) + '"></span>').join("") + "</div>";
  const rows = c.attention.length ? "<h4>Worth a look</h4>" + '<ol class="check-list">' + c.attention.map((r) =>
    '<li class="check-' + esc(r.state) + '" data-agent="' + esc(r.agent_id) + '" role="button" tabindex="0">' +
    '<span class="check-state">' + esc(r.state === "failing" ? "failing" : r.final ? "unchecked" : "unchecked so far") + "</span>" +
    '<span class="check-who">' + esc(r.label || r.agent_id) + "<small>" + esc(checkText(r)) + "</small></span></li>").join("") +
    "</ol>" : "";
  return insCard(title,
    "A check is a test, build, type check or lint command (or running the file just edited) after an agent's last code edit. " +
    "Unchecked means none was seen, not that the work is wrong. Docs and scratch files do not count.",
    metrics + split + notes + rows, "wide");
}

// An API error type in words.
function apiKind(kind) {
  if (kind === "rate_limit") return "usage or rate limit";
  if (kind === "authentication_failed") return "login failed";
  if (kind === "server_error") return "API server error";
  return kind ? String(kind).replace(/_/g, " ") : "API error";
}

// "Bash npm test": a call in words.
function callText(r) {
  return r.tool + (r.target ? " " + r.target : "");
}

// How an API error ended: answered again after a while, still waiting, or the session stopped there.
function apiLost(e) {
  if (e.ongoing) return "no answer yet, " + fmtDuration(e.lost_s || 0);
  if (e.resumed_at === null || e.resumed_at === undefined) return "the session stopped there";
  return "answered again after " + fmtDuration(e.lost_s || 0);
}

// What went wrong: API errors and the time they cost, failed calls, retries and timeouts.
function insErrors(ins) {
  const e = ins.errors;
  if (!e) return "";
  const metrics = [];
  if (e.api_count) {
    metrics.push(insMetric(e.api_count, e.api_count === 1 ? "API error" : "API errors"));
    if (e.api_lost_s) metrics.push(insMetric(fmtDuration(e.api_lost_s), "until the API answered again"));
  }
  metrics.push(insMetric(e.failed + " of " + e.calls, "tool calls failed"));
  if (e.retry_count) metrics.push(insMetric(e.retries_ok + " of " + e.retry_count, "retried calls worked"));
  if (e.timeout_count) metrics.push(insMetric(e.timeout_count, "ran past the timeout"));
  const notes = e.stuck.map((s) => s.label + " keeps failing " + callText(s) + " (" + s.failed_in_a_row + " times in a row).");
  const row = (cls, agentId, state, who, detail) =>
    '<li class="' + cls + '"' + (agentId ? ' data-agent="' + esc(agentId) + '" role="button" tabindex="0"' : "") + ">" +
    '<span class="check-state">' + esc(state) + '</span><span class="check-who">' + esc(who) +
    "<small>" + esc(detail) + "</small></span></li>";
  const api = e.api.length ? "<h4>API errors</h4>" + '<ol class="check-list">' + e.api.map((a) =>
    row("check-failing", a.agent_id, apiKind(a.kind) + (a.status ? " " + a.status : ""),
      (a.text || apiKind(a.kind)) + (a.repeats > 1 ? " (" + a.repeats + " times)" : ""),
      a.label + " · " + apiLost(a) + " · " + fmtClock(a.at))).join("") + "</ol>" : "";
  const tools = e.tools.length ? "<h4>Failed calls by tool</h4>" + insRank(e.tools.map((t) => ({
    name: t.tool, value: t.failed, title: t.example || t.tool, note: t.example,
    label: t.failed + " of " + t.calls, color: "var(--failed)" }))) : "";
  const agents = e.agents.length > 1 ? "<h4>Failed calls by agent</h4>" + insRank(e.agents.map((a) => ({
    name: a.label, agent: a.agent_id, value: a.failed, label: a.failed + " of " + a.calls, color: "var(--failed)" }))) : "";
  const retries = e.retries.length ? "<h4>Tried again</h4>" + '<ol class="check-list">' + e.retries.map((r) =>
    row(r.ok ? "check-ok" : "check-failing", r.agent_id,
      r.ok ? "worked on try " + r.attempts : r.attempts + " tries, still failing", callText(r),
      r.label + (r.error ? " · " + r.error : "") + " · " + fmtClock(r.at))).join("") + "</ol>" +
    (e.retry_count > e.retries.length ? '<p class="card-note">' + esc("And " + (e.retry_count - e.retries.length) + " more.") + "</p>" : "") : "";
  const timeouts = e.timeouts.length ? "<h4>Ran past the timeout</h4>" + '<ol class="check-list">' + e.timeouts.map((t) =>
    row("", t.agent_id, "after " + fmtDuration(t.after_s), callText(t),
      t.label + " · " + (t.background ? "moved to the background, still running then" : "stopped") + " · " + fmtClock(t.at))).join("") +
    "</ol>" : "";
  return insCard("What went wrong",
    "Errors from the API, with how long until it answered again; tool calls whose result was an error; the same " +
    "call tried again after failing (usually after a fix); and commands that ran past their timeout. A failed call " +
    "is not always a problem: a failing test run is often the point.",
    '<div class="metrics">' + metrics.join("") + "</div>" +
    (notes.length ? '<p class="card-note warn-text">' + esc("Stuck now: " + notes.join(" ")) + "</p>" : "") +
    api + retries + tools + agents + timeouts, "wide errors");
}

// The agent panel's line: what went wrong for this agent.
function errorsRow(e) {
  if (!e) return "";
  const parts = [];
  if (e.failed) parts.push(e.failed + " of " + e.calls + " calls failed");
  if (e.retries) parts.push(e.retries_ok + " of " + e.retries + (e.retries === 1 ? " retry" : " retries") + " worked");
  if (e.api) parts.push(e.api + (e.api === 1 ? " API error" : " API errors") + " (" + e.api_kinds.map(apiKind).join(", ") + ")");
  if (e.timeouts) parts.push(e.timeouts + " past the timeout");
  return "<dt>errors</dt><dd>" + esc(parts.join(" · ")) +
    (e.stuck ? '<br><span class="warn-text">' + esc("Keeps failing " + callText(e.stuck) + " (" +
      e.stuck.failed_in_a_row + " times in a row)") + "</span>" : "") + "</dd>";
}

// What the run changed: totals, who changed the most, which files changed the most.
function insChanges(ins) {
  const c = ins.changes;
  if (!c) return "";
  const metrics = '<div class="metrics">' + insMetric(c.files, c.files === 1 ? "file changed" : "files changed") +
    insMetric("+" + fmtCount(c.added), "lines added") + insMetric("−" + fmtCount(c.removed), "lines removed") +
    insMetric(c.created, c.created === 1 ? "new file" : "new files") + "</div>";
  const agents = "<h4>By agent</h4>" + insRank(c.by_agent.map((a) => ({
    name: a.label || a.agent_id, agent: a.agent_id, value: a.added + a.removed,
    label: "+" + fmtCount(a.added) + " −" + fmtCount(a.removed),
    note: a.files + (a.files === 1 ? " file" : " files"), color: "var(--running)" })));
  const files = "<h4>Most changed files</h4>" + insRank(c.top_files.map((f) => ({
    name: fileName(f.path), title: f.path, value: f.added + f.removed,
    label: "+" + fmtCount(f.added) + " −" + fmtCount(f.removed),
    note: (f.created ? "new" : "") + (f.agents > 1 ? (f.created ? ", " : "") + f.agents + " agents" : "") })));
  return insCard("What changed",
    "Lines added and removed by each agent, from the patches Claude Code recorded. Open an agent to read its diffs. Scratch files are not counted.",
    metrics + agents + files);
}

// A link out of the dashboard, only ever to an https URL, opened without a referrer.
function safeLink(url, text) {
  return /^https:\/\/[^\s"'<>]+$/.test(String(url || ""))
    ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(text) + "</a>"
    : esc(text);
}

// What the run produced: commits, pull requests, pushes and test runs, and what each cost.
function insOutcomes(ins) {
  const o = ins.outcomes;
  if (!o) return "";
  const tests = o.checks.passed + o.checks.failed;
  const metrics = [insMetric(o.commits, o.commits === 1 ? "commit" : "commits"),
    insMetric(o.prs, o.prs === 1 ? "pull request" : "pull requests"),
    insMetric(o.pushes, o.pushes === 1 ? "push" : "pushes"),
    insMetric(tests ? o.checks.passed + " of " + tests : "0", "test, build and lint runs passed")];
  const per = (key, noun) => {
    const p = o.per[key];
    if (!p || (p.cost === null && p.tokens === null)) return;
    metrics.push(insMetric(p.cost !== null ? fmtMoney(p.cost, o.currency) : fmtCount(p.tokens),
      (p.cost !== null ? "" : "fresh tokens ") + "per " + noun));
  };
  per("commit", "commit");
  per("pr", "pull request");
  const prs = o.pull_requests.length ? "<h4>Pull requests</h4>" + '<ul class="out-list">' + o.pull_requests.map((p) =>
    "<li><b>" + safeLink(p.url, (p.number !== null ? "#" + p.number : "pull request") + (p.repo ? " " + p.repo : "")) + "</b>" +
    "<span>" + esc((p.action || "linked") + " by " + p.label) + "</span></li>").join("") + "</ul>" : "";
  const commits = o.recent_commits.length ? "<h4>Latest commits</h4>" + '<ul class="out-list">' + o.recent_commits.map((c) =>
    '<li><code title="' + esc(c.sha ? "commit " + c.sha : "git printed no commit id (quiet mode)") + '">' + esc(c.sha || "—") + "</code>" +
    "<b>" + esc(c.message || "(no message seen)") + "</b>" +
    "<span>" + esc([c.branch, c.label, fmtClock(c.at)].filter(Boolean).join(" · ")) + "</span></li>").join("") + "</ul>" : "";
  return insCard("What the run produced",
    "Commits, pushes and pull requests as Claude Code recorded them, and every test, build or lint run. " +
    "Cost per commit divides the whole run's cost by its commits.",
    '<div class="metrics">' + metrics.join("") + "</div>" + prs + commits, "wide");
}

// Why the prompt cache was written again, in words.
function wasteCause(r) {
  if (r.cause === "idle_long" || r.cause === "idle_short") return "idle " + fmtDuration(r.gap_s);
  if (r.cause === "model") return "the model changed";
  if (r.cause === "compaction") return "after a compaction";
  return "cause not recorded";
}

// Where tokens were wasted: prompt-cache rebuilds (with the prompt you were answering, when
// that is why the session sat idle), the biggest tool results, and unchanged re-reads.
function insWaste(ins) {
  const w = ins.waste;
  if (!w) return "";
  const metrics = [insMetric(fmtCount(w.rebuilt_tokens), "tokens rewritten to the cache"),
    insMetric(w.rebuilds, w.rebuilds === 1 ? "cache rebuild" : "cache rebuilds")];
  if (w.rebuilds && w.extra_cost !== null && w.extra_cost !== undefined) {
    metrics.push(insMetric(fmtMoney(w.extra_cost, w.currency), "paid above the cache-read price"));
  }
  if (w.rebuilds && w.share_of_cache_writes !== null && w.share_of_cache_writes !== undefined) {
    metrics.push(insMetric(fmtPct(w.share_of_cache_writes), "of everything written to the cache"));
  }
  const notes = [];
  if (w.unpriced) notes.push("Some models have no price, so the cost leaves them out.");
  if (w.rereads) {
    notes.push(w.rereads + (w.rereads === 1 ? " re-read of an unchanged file" : " re-reads of unchanged files") +
      (w.reread_tokens >= 100 ? " added about " + fmtCount(w.reread_tokens) + " tokens." : " added next to nothing."));
  }
  const waited = (r) => r.wait_s
    ? ", while waiting for your " + (r.wait_kind === "permission" ? "approval" : "answer") + " (" + fmtDuration(r.wait_s) + ")" : "";
  const rows = w.rows.length ? "<h4>Rebuilds</h4>" + '<ul class="out-list">' + w.rows.map((r) =>
    "<li" + (r.agent_id ? ' data-agent="' + esc(r.agent_id) + '" role="button" tabindex="0"' : "") + ">" +
    "<code>" + esc(fmtCount(r.tokens)) + "</code><b>" + esc(r.label) + "</b>" +
    "<span>" + esc(wasteCause(r) + waited(r) + " · " + fmtClock(r.at) +
      (r.extra_cost !== null && r.extra_cost !== undefined ? " · " + fmtMoney(r.extra_cost, w.currency) : "")) +
    "</span></li>").join("") + "</ul>" : "";
  const big = w.big.length ? "<h4>Biggest things pulled into context</h4>" + '<ul class="out-list">' + w.big.map((b) =>
    "<li" + (b.agent_id ? ' data-agent="' + esc(b.agent_id) + '" role="button" tabindex="0"' : "") + ">" +
    "<code>~" + esc(fmtCount(b.tokens)) + "</code><b>" + esc(b.tool + (b.target ? " " + b.target : "")) + "</b>" +
    "<span>" + esc(b.label + " · carried through " + b.carried + (b.carried === 1 ? " later call" : " later calls")) +
    "</span></li>").join("") + "</ul>" : "";
  return insCard("Where tokens were wasted",
    "When the prompt cache expires (about five minutes idle, or an hour on the longer cache) or is reset by a " +
    "model switch or a compaction, the next call writes the whole conversation to the cache again at the higher " +
    "write price. Tool result sizes are estimated at four characters a token.",
    '<div class="metrics">' + metrics.join("") + "</div>" +
    (notes.length ? '<p class="card-note">' + esc(notes.join(" ")) + "</p>" : "") + rows + big, "wide");
}

// The agent panel's line: how often this agent's cache was rebuilt, how much, and why.
function wasteRow(w) {
  if (!w || !w.rebuilds || !w.rebuilds.length) return "";
  const causes = [];
  for (const r of w.rebuilds) {
    const text = wasteCause(r);
    if (causes.indexOf(text) < 0) causes.push(text);
  }
  return "<dt>cache</dt><dd>" + esc("Cache rebuilt " + w.rebuilds.length + "× · " +
    fmtCount(w.rebuilt_tokens) + " tokens · " + causes.join(", ")) + "</dd>";
}

// A context window in words: "1M", "200k".
function fmtWindow(limit) {
  return limit >= 1000000 ? +(limit / 1000000).toFixed(1) + "M" : Math.round(limit / 1000) + "k";
}

// Why a compaction ran, in words.
function compactionCause(c) {
  if (c.trigger === "manual") return "you ran /compact";
  if (c.trigger === "auto") return "Claude Code compacted it";
  return "compacted";
}

// The main session's context on each call, with a marker at every compaction.
function insPressureChart(m, width) {
  const H = 150;
  const left = 40;
  const right = 6;
  const top = 14;
  const bottom = 22;
  const pts = m.points;
  const plotW = Math.max(60, width - left - right);
  const plotH = H - top - bottom;
  const t0 = pts[0][0];
  const span = Math.max(pts[pts.length - 1][0] - t0, 1e-6);
  const x = (t) => left + ((t - t0) / span) * plotW;
  const ceil = Math.max(m.tokens, 1) * 1.12;
  const y = (n) => top + plotH - (n / ceil) * plotH;
  let line = "";
  for (let i = 0; i < pts.length; i++) line += (i ? " L" : "M") + x(pts[i][0]).toFixed(1) + "," + y(pts[i][1]).toFixed(1);
  const area = line + " L" + x(pts[pts.length - 1][0]).toFixed(1) + "," + y(0).toFixed(1) +
    " L" + x(t0).toFixed(1) + "," + y(0).toFixed(1) + " Z";
  let grid = "";
  for (let i = 0; i <= 3; i++) {
    const n = (m.tokens * i) / 3;
    grid += '<line class="ins-grid-line" x1="' + left + '" x2="' + (left + plotW) + '" y1="' + y(n).toFixed(1) +
      '" y2="' + y(n).toFixed(1) + '"/><text class="ins-axis" x="' + (left - 6) + '" y="' + (y(n) + 3.5).toFixed(1) +
      '" text-anchor="end">' + esc(n ? fmtCount(Math.round(n)) : "0") + "</text>";
  }
  const near = m.limit * 0.8;
  const nearLine = near <= ceil ? '<line class="ins-avg" x1="' + left + '" x2="' + (left + plotW) + '" y1="' + y(near).toFixed(1) +
    '" y2="' + y(near).toFixed(1) + '"/>' : "";
  let lastLabel = -Infinity;     // markers closer than this to the last label go unlabelled
  const marks = m.compactions.filter((c) => c.at >= t0).map((c) => {
    const cx = x(c.at);
    const label = cx - lastLabel >= 60;
    if (label) lastLabel = cx;
    return '<line class="pressure-mark" x1="' + cx.toFixed(1) + '" x2="' + cx.toFixed(1) + '" y1="' + top + '" y2="' + y(0).toFixed(1) + '"/>' +
      (label ? '<text class="pressure-mark-label" x="' + cx.toFixed(1) + '" y="' + (top - 4) + '" text-anchor="' +
        (cx > left + plotW - 30 ? "end" : cx < left + 30 ? "start" : "middle") + '">' +
        esc(c.trigger === "manual" ? "/compact" : "compacted") + "</text>" : "");
  }).join("");
  const ticks = [0, 0.5, 1].map((f) =>
    '<text class="ins-axis" x="' + (left + f * plotW).toFixed(1) + '" y="' + (H - 5) + '" text-anchor="' +
    (f === 0 ? "start" : f === 1 ? "end" : "middle") + '">' + esc(fmtDuration(f * span)) + "</text>").join("");
  return '<svg class="ins-chart" viewBox="0 0 ' + width + " " + H + '" width="' + width + '" height="' + H +
    '" role="img" aria-label="' + esc("The main session's context over time. Peak " + fmtCount(m.tokens) + " tokens, " +
      m.compactions.length + (m.compactions.length === 1 ? " compaction." : " compactions.")) + '">' +
    grid + '<path class="ins-area" d="' + area + '"/>' + nearLine + marks + ticks + "</svg>" +
    (nearLine ? '<p class="card-note">' + esc("The dashed line is 80% of the assumed " + fmtWindow(m.limit) + " window.") + "</p>" : "");
}

// How full each context got: the main session's curve and compactions, and agents by peak.
function insPressure(ins, width) {
  const p = ins.pressure;
  if (!p) return "";
  const m = p.main;
  const metrics = [];
  if (m) {
    metrics.push(insMetric(fmtCount(m.tokens), "peak in the main session"));
    metrics.push(insMetric(fmtPct(m.fill), "of an assumed " + fmtWindow(m.limit) + " window"));
    metrics.push(insMetric(m.compactions.length, m.compactions.length === 1 ? "compaction" : "compactions"));
  }
  if (p.agents.length) metrics.push(insMetric(fmtCount(p.agents[0].tokens), "fullest agent"));
  const notes = [];
  if (p.near.length) {
    notes.push("Near the window now: " + p.near.map((n) => n.label + " (" + fmtPct(n.fill) + ")").join(", ") + ".");
  }
  if (p.agent_compactions) {
    notes.push("Agents were compacted " + p.agent_compactions + (p.agent_compactions === 1 ? " time." : " times."));
  }
  if ((m && m.fill > 1) || p.agents.some((a) => a.fill > 1)) {
    notes.push("A context went past its assumed window, so that window is too small: set ORCHESTRA_CONTEXT_LIMITS.");
  }
  const chart = m && m.points.length > 1 ? insPressureChart(m, width) : "";
  const compactions = m && m.compactions.length ? "<h4>Compactions</h4>" + '<ul class="out-list">' + m.compactions.map((c) =>
    "<li><code>" + esc((c.pre_tokens !== null ? fmtCount(c.pre_tokens) : "?") + " → " +
      (c.post_tokens !== null ? fmtCount(c.post_tokens) : "?")) + "</code><b>" + esc(compactionCause(c)) + "</b>" +
    "<span>" + esc((c.duration_s ? "took " + fmtDuration(c.duration_s) + " · " : "") + fmtClock(c.at)) + "</span></li>").join("") +
    "</ul>" : "";
  const agents = p.agents.length ? "<h4>Agents by peak context</h4>" + insRank(p.agents.map((a) => ({
    name: a.label, agent: a.agent_id, value: a.tokens,
    note: a.compactions ? "compacted " + a.compactions + "×" : "",
    color: a.fill >= p.near_at ? "var(--stalled)" : "",
    label: fmtCount(a.tokens) + " · " + fmtPct(a.fill) }))) +
    (p.agent_count > p.agents.length ? '<p class="card-note">' + esc("And " + (p.agent_count - p.agents.length) + " more.") + "</p>" : "") : "";
  return insCard("How full each context got",
    "Everything the model was sent on each call, cached or not. Near the model's window Claude Code compacts the " +
    "conversation into a summary (you can also run /compact), and detail from before it is gone. Windows are assumed: " +
    "1M, and 200k for Haiku; set ORCHESTRA_CONTEXT_LIMITS to change them.",
    '<div class="metrics">' + metrics.join("") + "</div>" +
    (notes.length ? '<p class="card-note">' + esc(notes.join(" ")) + "</p>" : "") + chart + compactions + agents, "wide pressure");
}

// The agent panel's line: how full its context got.
function pressureRow(c) {
  if (!c) return "";
  return "<dt>context</dt><dd>" + esc("Peak " + fmtCount(c.tokens) + " tokens · " + fmtPct(c.fill) + " of an assumed " +
    fmtWindow(c.limit) + " window" + (c.compactions ? " · compacted " + c.compactions + "×" : "")) + "</dd>";
}

// What each agent was told: the instruction files and skills it had, and who ran without
// the project rules the main session had.
function insContext(ins) {
  const c = ins.context;
  if (!c) return "";
  const metrics = [];
  if (c.covered !== null && c.covered !== undefined) {
    metrics.push(insMetric(c.covered + " of " + c.agents, "agents loaded your project instructions"));
  }
  metrics.push(insMetric(c.files.length, c.files.length === 1 ? "instruction file" : "instruction files"));
  if (c.skills) metrics.push(insMetric(c.skills, "skills offered"));
  const missing = c.missing.length ? "<h4>Ran without your project instructions</h4>" + '<ol class="check-list">' +
    c.missing.map((m) => '<li class="check-unchecked" data-agent="' + esc(m.agent_id) + '" role="button" tabindex="0">' +
      '<span class="check-state">' + esc(m.agent_type || "agent") + "</span>" +
      '<span class="check-who">' + esc(m.label || m.agent_id) + "<small>" + esc("did not load " +
        m.missing.map(fileLabel).join(", ")) + "</small></span></li>").join("") + "</ol>" : "";
  const files = "<h4>Instruction files</h4>" + '<ul class="out-list">' + c.files.map((f) =>
    '<li><code>' + esc(f.type || "?") + "</code><b title=\"" + esc(f.path) + '">' + esc(fileLabel(f.path)) + "</b>" +
    "<span>" + esc((f.main ? "main session" + (f.agents ? " + " : "") : "") +
      (f.agents ? f.agents + (f.agents === 1 ? " agent" : " agents") : "") +
      (f.chars ? " · " + fmtCount(f.chars) + " characters" : "")) + "</span></li>").join("") + "</ul>";
  const required = c.required.length ? "Your project instructions are " + c.required.map(fileLabel).join(" and ") + ". " : "";
  return insCard("What each agent was told",
    required + "The CLAUDE.md, rules and memory files each agent loaded, from what Claude Code records. " +
    "Some agent types may be meant to run without project instructions; this shows what happened, not whether it was wrong.",
    '<div class="metrics">' + metrics.join("") + "</div>" + missing + files, "wide");
}

// "CLAUDE.md in claude-SA/.claude": a file name with the folder it sits in.
function fileLabel(path) {
  const parts = String(path || "").split(/[\\/]/).filter(Boolean);
  if (parts.length < 2) return parts.join("");
  return parts[parts.length - 1] + " in " + parts.slice(Math.max(0, parts.length - 3), -1).join("/");
}

// The agent panel's "instructions" line.
function contextRow(ctx, agentId, run) {
  if (!ctx || (!ctx.files.length && !ctx.skills)) return "";
  const parts = ctx.files.map((f) => fileLabel(f.path) + " (" + (f.type || "?") + ")");
  if (ctx.skills) parts.push(ctx.skills + " skills offered");
  const cov = run && run.insights && run.insights.context;
  const gap = cov ? cov.missing.find((m) => m.agent_id === agentId) : null;
  return "<dt>instructions</dt><dd>" + esc(parts.join(", ") || "none recorded") +
    (gap ? '<br><span class="warn-text">' + esc("Did not load " + gap.missing.map(fileLabel).join(", ")) + "</span>" : "") + "</dd>";
}

// The agent panel's "produced" line.
function producedRow(o) {
  if (!o) return "";
  const parts = [];
  if (o.commits.length) parts.push(o.commits.length + (o.commits.length === 1 ? " commit" : " commits"));
  if (o.prs.length) parts.push(o.prs.map((p) => p.number !== null ? "PR #" + p.number : "a pull request").join(", "));
  if (o.pushes) parts.push(o.pushes + (o.pushes === 1 ? " push" : " pushes"));
  const tests = o.checks.passed + o.checks.failed;
  if (tests) parts.push(o.checks.passed + " of " + tests + " test runs passed");
  return parts.length ? "<dt>produced</dt><dd>" + esc(parts.join(", ")) + "</dd>" : "";
}

function tickWaits() {
  if (state.offline) return;
  const now = Date.now() / 1000;
  for (const el of document.querySelectorAll("[data-wait-since]")) {
    const since = parseFloat(el.dataset.waitSince);
    if (isNaN(since)) continue;
    el.textContent = fmtDuration((parseFloat(el.dataset.waitBase) || 0) + Math.max(0, now - since));
  }
}

function renderInsights(run) {
  const box = $("insights");
  if (!box) return;
  const ins = run.insights;
  if (!ins) {
    box.innerHTML = insCard("Insights",
      run.replay_at !== undefined ? "Insights describe the whole run. Leave replay to see them." :
        "Insights are not available for this run yet.", "", "wide");
    return;
  }
  const width = Math.max(320, (box.clientWidth || 960) - 38);
  const lines = insHeadlines(ins, run);
  state.cardHeadlines = {};
  for (const line of lines) state.cardHeadlines[line.key] = line;
  state.cardsFold = true;
  let cards = "";
  try {
    cards = insParallelism(ins, width) + insCritical(ins) + insOutcomes(ins) + insChecks(ins) + insErrors(ins) + insWaits(ins, run) + insWaste(ins) + insPressure(ins, width) + insChanges(ins) + insContext(ins) + insTools(ins) +
      insTokens(ins, run) + insSlowest(ins) + insFiles(ins);
  } finally {
    state.cardsFold = false;
  }
  const spot = noteFocus(box);
  box.innerHTML = insGlance(lines) + cards;
  for (const el of box.querySelectorAll("[data-agent]")) {
    const open = () => openDrawer(el.getAttribute("data-agent"));
    el.onclick = open;
    el.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } };
  }
  for (const el of box.querySelectorAll("[data-fold]")) el.onclick = () => toggleFold(el.getAttribute("data-fold"));
  for (const el of box.querySelectorAll("[data-jump]")) el.onclick = () => openCard(el.getAttribute("data-jump"));
  for (const el of box.querySelectorAll(".glance-chip[data-view]")) el.onclick = () => setView(el.getAttribute("data-view"));
  for (const el of box.querySelectorAll("[data-fold-all]")) el.onclick = () => foldAll(el.getAttribute("data-fold-all") === "fold");
  restoreFocus(box, spot);
}

// ----------------------------------------------------------------- graph

const NODE_W = 210;
const NODE_H = 52;
const COL_GAP = 84;
const ROW_GAP = 22;
const ACCENT_W = 4;
const NODE_TEXT_X = 16;
const EXACT_KINDS = ["spawn", "artifact", "message"];
const GRAPH_MIN_K = 0.3;
const GRAPH_MAX_K = 2.5;
const GRAPH_SWEEPS = 8;

// Columns by longest path over exact edges. An inferred edge never sets a rank, so
// a bad guess cannot rearrange the whole picture. Columns are indexed 0..n-1 rather
// than by raw rank: artifact edges can form a cycle, the loop is bounded by node
// count, and a raw rank could climb past the number of columns actually occupied,
// putting nodes outside the canvas with no visible cue that anything is missing.
function graphRankColumns(nodes, structural) {
  const rank = {};
  nodes.forEach((n) => { rank[n.id] = 0; });
  for (let pass = 0; pass < nodes.length; pass++) {
    let moved = false;
    for (const edge of structural) {
      const want = rank[edge.src] + 1;
      if (rank[edge.dst] < want) { rank[edge.dst] = want; moved = true; }
    }
    if (!moved) break;                  // also the cycle guard
  }
  const byRank = {};
  for (const node of nodes) (byRank[rank[node.id]] = byRank[rank[node.id]] || []).push(node);
  return Object.keys(byRank).map(Number).sort((a, b) => a - b).map((r, column) => {
    byRank[r].sort((a, b) => ((a.startedAt || 0) - (b.startedAt || 0)) || (a.id < b.id ? -1 : 1));
    byRank[r].forEach((n) => { n.column = column; });
    return byRank[r];
  });
}

// The duration-weighted longest chain, not the hop count: a 5-second agent and a
// 5-minute agent are equally "one hop", but only one of them can be why the run
// took as long as it did.
function graphCriticalPath(nodes, byId, structural) {
  const pathDuration = {};
  const critPrev = {};
  nodes.forEach((n) => { pathDuration[n.id] = n.duration || 0; critPrev[n.id] = null; });
  for (let pass = 0; pass < nodes.length; pass++) {
    let moved = false;
    for (const edge of structural) {
      const candidate = pathDuration[edge.src] + (byId[edge.dst].duration || 0);
      if (candidate > pathDuration[edge.dst] + 1e-9) {
        pathDuration[edge.dst] = candidate;
        critPrev[edge.dst] = edge.src;
        moved = true;
      } else if (critPrev[edge.dst] === null && candidate >= pathDuration[edge.dst] - 1e-9) {
        // A tie against the node's own-duration base case (its most common
        // predecessor is "main", whose duration is always 0) must still record a
        // predecessor, or the chain silently ends one hop short.
        critPrev[edge.dst] = edge.src;
      }
    }
    if (!moved) break;
  }
  let end = nodes[0].id;
  for (const n of nodes) if (pathDuration[n.id] > pathDuration[end]) end = n.id;
  const criticalNodes = new Set();
  const criticalEdges = new Set();
  for (let cur = end; cur && !criticalNodes.has(cur); cur = critPrev[cur]) {
    criticalNodes.add(cur);
    if (critPrev[cur]) criticalEdges.add(critPrev[cur] + "→" + cur);
  }
  return { criticalNodes, criticalEdges };
}

// Edge crossings between neighbouring columns: the number a person actually sees.
function graphCountCrossings(columns, links) {
  const pos = {};
  const col = {};
  columns.forEach((nodes, c) => nodes.forEach((n, i) => { pos[n.id] = i; col[n.id] = c; }));
  const groups = {};
  for (const l of links) {
    if (col[l.src] === undefined || col[l.dst] !== col[l.src] + 1) continue;
    (groups[col[l.src]] = groups[col[l.src]] || []).push([pos[l.src], pos[l.dst]]);
  }
  let total = 0;
  for (const key in groups) {
    const g = groups[key];
    for (let i = 0; i < g.length; i++) {
      for (let j = i + 1; j < g.length; j++) {
        if ((g[i][0] - g[j][0]) * (g[i][1] - g[j][1]) < 0) total += 1;
      }
    }
  }
  return total;
}

// Layered ordering: alternate down and up sweeps, ordering each column by the mean
// position of its neighbours, and keep the best ordering seen. Positions are
// normalised per column so neighbours in columns of different heights compare fairly.
function graphOrderColumns(columns, links) {
  const up = {};
  const down = {};
  for (const l of links) {
    (down[l.src] = down[l.src] || []).push(l.dst);
    (up[l.dst] = up[l.dst] || []).push(l.src);
  }
  const norm = {};
  const refresh = (c) => columns[c].forEach((n, i) => { norm[n.id] = (i + 0.5) / columns[c].length; });
  columns.forEach((_, c) => refresh(c));
  const sweep = (c, neighbours) => {
    const keyed = columns[c].map((n, i) => {
      const around = (neighbours[n.id] || []).filter((id) => norm[id] !== undefined);
      const key = around.length ? around.reduce((s, id) => s + norm[id], 0) / around.length : norm[n.id];
      return { n: n, key: key, i: i };
    });
    keyed.sort((a, b) => (a.key - b.key) || (a.i - b.i));
    columns[c] = keyed.map((k) => k.n);
    refresh(c);
  };
  let best = columns.map((nodes) => nodes.slice());
  let bestCount = graphCountCrossings(columns, links);
  for (let iter = 0; iter < GRAPH_SWEEPS && bestCount > 0; iter++) {
    for (let c = 1; c < columns.length; c++) sweep(c, up);
    for (let c = columns.length - 2; c >= 0; c--) sweep(c, down);
    const count = graphCountCrossings(columns, links);
    if (count < bestCount) { bestCount = count; best = columns.map((nodes) => nodes.slice()); }
  }
  best.forEach((nodes, c) => { columns[c] = nodes; });
  columns.forEach((nodes) => nodes.forEach((n, i) => { n.row = i; }));
  return bestCount;
}

// Vertical placement: each node wants to sit level with its neighbours, but nodes in
// a column cannot overlap and must keep their order. That is an isotonic regression,
// solved exactly by pooling adjacent violators, so flows run straight where they can.
function graphPlaceRows(columns, links) {
  const step = NODE_H + ROW_GAP;
  const around = {};
  for (const l of links) {
    (around[l.src] = around[l.src] || []).push(l.dst);
    (around[l.dst] = around[l.dst] || []).push(l.src);
  }
  const y = {};
  columns.forEach((nodes) => nodes.forEach((n, i) => { y[n.id] = i * step; }));
  const settle = (nodes) => {
    const z = nodes.map((n, i) => {
      const near = (around[n.id] || []).filter((id) => y[id] !== undefined);
      const want = near.length ? near.reduce((s, id) => s + y[id], 0) / near.length : y[n.id];
      return want - i * step;
    });
    const blocks = [];
    for (const value of z) {
      blocks.push({ sum: value, count: 1 });
      while (blocks.length > 1 &&
             blocks[blocks.length - 2].sum / blocks[blocks.length - 2].count >
             blocks[blocks.length - 1].sum / blocks[blocks.length - 1].count) {
        const last = blocks.pop();
        blocks[blocks.length - 1].sum += last.sum;
        blocks[blocks.length - 1].count += last.count;
      }
    }
    let i = 0;
    for (const block of blocks) {
      for (let k = 0; k < block.count; k++, i++) y[nodes[i].id] = block.sum / block.count + i * step;
    }
  };
  for (let pass = 0; pass < 6; pass++) {
    const order = columns.map((_, c) => c);
    if (pass % 2) order.reverse();
    for (const c of order) settle(columns[c]);
  }
  let min = Infinity;
  for (const id in y) min = Math.min(min, y[id]);
  columns.forEach((nodes) => nodes.forEach((n) => { n.y = 20 + y[n.id] - min; }));
}

function layoutGraph(run) {
  const nodes = [{ id: "main", label: "orchestrator", status: "completed",
                   sub: "this session", isMain: true, matches: true }];
  for (const agent of run.agents) {
    nodes.push({
      id: agent.agent_id,
      label: agent.description || agent.agent_id,
      sub: agent.agent_type + " · " + fmtModelShort(agent.model),
      status: agent.status,
      startedAt: agent.started_at,
      duration: agent.duration_s || 0,
      matches: agentMatchesFilter(agent),
    });
  }
  const byId = {};
  nodes.forEach((n) => { byId[n.id] = n; });
  const edges = (run.edges || []).filter((e) => byId[e.src] && byId[e.dst]).map((e) => Object.assign({}, e));
  const structural = edges.filter((e) => EXACT_KINDS.indexOf(e.kind) >= 0);

  // Every agent is spawned by the orchestrator, so those edges are the same fan in
  // every run and bury the real dependencies. Draw one only where it IS the
  // explanation: an agent with no other incoming exact edge. Hovering the
  // orchestrator brings the rest back.
  const explained = new Set(structural.filter((e) => e.src !== "main").map((e) => e.dst));
  edges.forEach((e) => { e.hidden = e.src === "main" && explained.has(e.dst); });

  const { criticalNodes, criticalEdges } = graphCriticalPath(nodes, byId, structural);
  const columns = graphRankColumns(nodes, structural);
  const links = structural.filter((e) => e.src !== "main");
  const crossings = graphOrderColumns(columns, links);
  graphPlaceRows(columns, links);

  const rowsDrawn = columns.length;
  columns.forEach((col, column) => col.forEach((node) => { node.x = 20 + column * (NODE_W + COL_GAP); }));
  // The orchestrator sits level with the agents it launches directly.
  if (columns[0] && columns[0].length === 1 && columns[0][0].isMain) {
    const roots = edges.filter((e) => e.src === "main" && !e.hidden).map((e) => byId[e.dst].y);
    if (roots.length) columns[0][0].y = roots.reduce((s, v) => s + v, 0) / roots.length;
  }
  let bottom = 0;
  nodes.forEach((n) => { bottom = Math.max(bottom, n.y); });
  return { nodes, edges, byId, crossings, criticalNodes, criticalEdges,
           width: 40 + rowsDrawn * (NODE_W + COL_GAP) - COL_GAP, height: bottom + NODE_H + 40 };
}

// The points an edge is drawn through: from the right side of its source to the left side of
// its target. An edge that skips columns keeps its single curve unless that curve would run
// through a node it skips; then it crosses each skipped column level, through the free lane
// (above, between or below that column's nodes) nearest the straight line between its ends.
// The bends happen in the gaps between columns, where there are no nodes.
const GRAPH_LANE_PAD = 8;

function graphRoute(layout, edge) {
  const a = layout.byId[edge.src];
  const b = layout.byId[edge.dst];
  // An edge to an earlier column (an inferred handoff can point back) leaves its source's left
  // side and enters its target's right side, so it never loops back across its own source.
  const back = b.x + NODE_W <= a.x;
  const start = { x: back ? a.x : a.x + NODE_W, y: a.y + NODE_H / 2 };
  const end = { x: back ? b.x + NODE_W : b.x, y: b.y + NODE_H / 2 };
  if (Math.abs(b.x - a.x) <= NODE_W + COL_GAP) return [start, end];   // neighbours, or the same column
  const lo = Math.min(a.x, b.x);
  const hi = Math.max(a.x, b.x);
  const cols = new Map();
  for (const n of layout.nodes) {
    if (n === a || n === b || n.x <= lo || n.x >= hi) continue;
    if (!cols.has(n.x)) cols.set(n.x, []);
    cols.get(n.x).push(n);
  }
  const blocked = (pts) => {
    for (let s = 0; s + 1 < pts.length; s++) {
      const p = pts[s];
      const q = pts[s + 1];
      const dir = q.x >= p.x ? 1 : -1;
      const bend = dir * Math.max(12, Math.min(Math.abs(q.x - p.x) / 2, 160));
      for (let k = 1; k < 32; k++) {
        const t = k / 32;
        const u = 1 - t;
        const x = u * u * u * p.x + 3 * u * u * t * (p.x + bend) + 3 * u * t * t * (q.x - bend) + t * t * t * q.x;
        const y = u * u * u * p.y + 3 * u * u * t * p.y + 3 * u * t * t * q.y + t * t * t * q.y;
        for (const nodes of cols.values()) {
          for (const n of nodes) {
            if (x > n.x && x < n.x + NODE_W && y > n.y - 2 && y < n.y + NODE_H + 2) return true;
          }
        }
      }
    }
    return false;
  };
  if (!cols.size || !blocked([start, end])) return [start, end];
  const pts = [start];
  const bottom = (layout.height || Infinity) - 4;
  for (const x of [...cols.keys()].sort((p, q) => (back ? q - p : p - q))) {
    const nodes = cols.get(x).slice().sort((p, q) => p.y - q.y);
    const want = start.y + (end.y - start.y) * (x + NODE_W / 2 - start.x) / (end.x - start.x);
    let lane = null;
    const consider = (lo, hi) => {
      if (hi < lo) return;
      const y = Math.max(lo, Math.min(hi, want));
      if (lane === null || Math.abs(y - want) < Math.abs(lane - want)) lane = y;
    };
    consider(4, nodes[0].y - GRAPH_LANE_PAD);
    for (let i = 0; i + 1 < nodes.length; i++) {
      consider(nodes[i].y + NODE_H + GRAPH_LANE_PAD, nodes[i + 1].y - GRAPH_LANE_PAD);
    }
    consider(nodes[nodes.length - 1].y + NODE_H + GRAPH_LANE_PAD, bottom);
    if (lane === null) lane = nodes[nodes.length - 1].y + NODE_H + GRAPH_LANE_PAD;
    if (back) pts.push({ x: x + NODE_W + 10, y: lane }, { x: x - 10, y: lane });
    else pts.push({ x: x - 10, y: lane }, { x: x + NODE_W + 10, y: lane });
  }
  pts.push(end);
  return pts;
}

// A smooth path through the points: each step a curve that leaves and arrives level, its bend
// half the step (12 to 160 wide), either way. The last point stops short for the arrowhead.
function graphPath(pts) {
  let d = "M" + pts[0].x + "," + pts[0].y;
  for (let i = 1; i < pts.length; i++) {
    const p = pts[i - 1];
    const q = pts[i];
    const dir = q.x >= p.x ? 1 : -1;
    const bend = dir * Math.max(12, Math.min(Math.abs(q.x - p.x) / 2, 160));
    d += " C" + (p.x + bend) + "," + p.y + " " + (q.x - bend) + "," + q.y + " " +
      (i === pts.length - 1 ? q.x - dir * 6 : q.x) + "," + q.y;
  }
  return d;
}

function graphClamp(k) {
  return Math.max(GRAPH_MIN_K, Math.min(GRAPH_MAX_K, k));
}

function applyGraphView() {
  const view = state.graphView;
  if (view.el) view.el.setAttribute("transform", "translate(" + view.tx.toFixed(1) + "," + view.ty.toFixed(1) +
    ") scale(" + view.k.toFixed(3) + ")");
}

function graphPoint(event) {
  const svg = $("graph");
  const rect = svg.getBoundingClientRect ? svg.getBoundingClientRect() : { left: 0, top: 0, width: 1, height: 1 };
  const box = svg.viewBox && svg.viewBox.baseVal && svg.viewBox.baseVal.width ? svg.viewBox.baseVal : null;
  const sx = box && rect.width ? box.width / rect.width : 1;
  const sy = box && rect.height ? box.height / rect.height : 1;
  return [(event.clientX - rect.left) * sx, (event.clientY - rect.top) * sy];
}

function zoomGraph(factor, cx, cy) {
  const view = state.graphView;
  const next = graphClamp(view.k * factor);
  const f = next / view.k;
  view.tx = cx - (cx - view.tx) * f;
  view.ty = cy - (cy - view.ty) * f;
  view.k = next;
  view.userSet = true;
  applyGraphView();
}

function fitGraph() {
  state.graphView.userSet = false;
  if (state.run) renderGraph(state.run);
}

// Pan and zoom: drag the background, Ctrl/Cmd+wheel (or pinch) to zoom, +/-/0 and the
// arrow keys when the graph has focus, or the buttons. Plain wheel still scrolls the page.
function setupFloorGroup() {
  const select = $("floor-group");
  if (!select) return;
  try {
    const stored = localStorage.getItem("orchestra-floor-group");
    if (stored === "type" || stored === "status") state.floorGroup = stored;
  } catch (err) { /* blocked storage: use the default */ }
  select.value = state.floorGroup;
  select.onchange = () => {
    state.floorGroup = select.value === "status" ? "status" : "type";
    try { localStorage.setItem("orchestra-floor-group", state.floorGroup); } catch (err) { /* ignore */ }
    state.floorSig = "";
    if (state.run) renderWorkfloor(state.run);
  };
}

function setupGraphInteractions() {
  const svg = $("graph");
  if (!svg || !svg.addEventListener) return;
  const centre = () => {
    const w = svg.clientWidth || 900;
    const h = Number(svg.getAttribute("height")) || 400;
    return [w / 2, h / 2];
  };
  const zin = $("graph-zoom-in");
  const zout = $("graph-zoom-out");
  const fit = $("graph-fit");
  if (zin) zin.onclick = () => zoomGraph(1.25, ...centre());
  if (zout) zout.onclick = () => zoomGraph(0.8, ...centre());
  if (fit) fit.onclick = fitGraph;
  svg.addEventListener("wheel", (event) => {
    if (!(event.ctrlKey || event.metaKey)) return;
    event.preventDefault();
    const [x, y] = graphPoint(event);
    zoomGraph(event.deltaY < 0 ? 1.12 : 1 / 1.12, x, y);
  }, { passive: false });
  let drag = null;
  svg.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || (event.target.closest && event.target.closest(".node, .edge"))) return;
    const view = state.graphView;
    drag = { x: event.clientX, y: event.clientY, tx: view.tx, ty: view.ty };
    if (svg.setPointerCapture) svg.setPointerCapture(event.pointerId);
    svg.classList.add("panning");
  });
  svg.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const view = state.graphView;
    view.tx = drag.tx + (event.clientX - drag.x);
    view.ty = drag.ty + (event.clientY - drag.y);
    view.userSet = true;
    applyGraphView();
  });
  const stop = () => { drag = null; svg.classList.remove("panning"); };
  svg.addEventListener("pointerup", stop);
  svg.addEventListener("pointercancel", stop);
  svg.addEventListener("keydown", (event) => {
    const [cx, cy] = centre();
    const view = state.graphView;
    const pan = (dx, dy) => { view.tx += dx; view.ty += dy; view.userSet = true; applyGraphView(); };
    if (event.key === "+" || event.key === "=") zoomGraph(1.25, cx, cy);
    else if (event.key === "-" || event.key === "_") zoomGraph(0.8, cx, cy);
    else if (event.key === "0") fitGraph();
    else if (event.key === "ArrowLeft") pan(48, 0);
    else if (event.key === "ArrowRight") pan(-48, 0);
    else if (event.key === "ArrowUp") pan(0, 48);
    else if (event.key === "ArrowDown") pan(0, -48);
    else return;
    event.preventDefault();
  });
}

function renderGraph(run) {
  const svg = $("graph");
  svg.innerHTML = "";
  $("edge-evidence").hidden = true;
  // The first render of a session just seeds what's already-existing history;
  // nothing on it "just happened," so nothing should flash. Only edges that
  // appear on a LATER render — after real polls have run — are live events.
  const firstRender = !state.graphSeeded;
  state.graphSeeded = true;
  const reducedMotion = typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const layout = layoutGraph(run);

  // Fit to width (but never below a readable size) until the person moves the view.
  const view = state.graphView;
  if (view.session !== state.sessionId) { view.session = state.sessionId; view.userSet = false; }
  const width = svg.clientWidth || 900;
  if (!view.userSet) {
    view.k = Math.max(0.6, Math.min(1, (width - 24) / layout.width));
    view.tx = 12;
    view.ty = 12;
  }
  const height = Math.round(Math.min(Math.max(layout.height * view.k + 24, 300), 820));
  svg.setAttribute("height", height);
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);

  const defs = svgEl("defs");
  defs.appendChild(svgEl("marker", {
    id: "arrow", viewBox: "0 0 8 8", refX: 7, refY: 4,
    markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse",
  }, "")).appendChild(svgEl("path", { d: "M0,0 L8,4 L0,8 z", class: "edge-arrow" }));
  svg.appendChild(defs);
  svg.appendChild(svgEl("rect", { x: 0, y: 0, width: width, height: height, class: "graph-bg" }));
  const viewport = svgEl("g", { class: "graph-viewport" });
  svg.appendChild(viewport);
  view.el = viewport;
  applyGraphView();

  const labelFont = bodyFont(11.5);
  const subFont = bodyFont(9.5);
  const textMax = NODE_W - NODE_TEXT_X - ACCENT_W - 12;

  const edgesByNode = {};
  const neighbours = {};
  const edgeEls = [];
  const hiddenEls = [];
  for (const edge of layout.edges) {
    // Around the nodes it skips (graphRoute), as one smooth path (graphPath).
    const isCritical = layout.criticalEdges.has(edge.src + "→" + edge.dst);
    const d = graphPath(graphRoute(layout, edge));
    const path = svgEl("path", {
      d: d,
      class: "edge" + (edge.confidence === "inferred" ? " edge-inferred" : "") +
        (edge.src === "main" ? " edge-main" : "") +
        (edge.hidden ? " edge-hidden" : "") +
        (isCritical ? " edge-critical" : ""),
      "marker-end": "url(#arrow)",
    });
    path.appendChild(svgEl("title", {}, edge.kind + " (" + edge.confidence + ")"));
    path.onclick = () => showEvidence(edge);
    viewport.appendChild(path);
    edgeEls.push(path);
    if (edge.hidden) hiddenEls.push(path);
    for (const id of [edge.src, edge.dst]) (edgesByNode[id] = edgesByNode[id] || []).push(path);
    (neighbours[edge.src] = neighbours[edge.src] || new Set()).add(edge.dst);
    (neighbours[edge.dst] = neighbours[edge.dst] || new Set()).add(edge.src);

    // A brand-new edge key is a real event: this handoff was JUST detected
    // between polls. Flash a dot traveling the same path once, then let it
    // settle into an ordinary static line for good.
    const edgeKey = edge.src + ">" + edge.dst + ">" + edge.kind;
    const isNewEdge = !firstRender && !state.seenEdgeKeys.has(edgeKey);
    state.seenEdgeKeys.add(edgeKey);
    if (isNewEdge && !reducedMotion && !edge.hidden) {
      const packet = svgEl("circle", { r: 4, class: "packet" });
      packet.appendChild(svgEl("animateMotion", {
        dur: "1s", begin: "0s", fill: "freeze", path: d,
      }));
      packet.appendChild(svgEl("animate", {
        attributeName: "opacity", from: "1", to: "0",
        begin: "0.7s", dur: "0.3s", fill: "freeze",
      }));
      viewport.appendChild(packet);
    }
  }

  const nodeEls = {};
  for (const node of layout.nodes) {
    const isCritical = layout.criticalNodes.has(node.id);
    const group = svgEl("g", { class: "node" + (node.isMain ? " main" : "") +
      (isCritical ? " critical" : "") + (node.matches ? "" : " dim") +
      (state.selected === node.id ? " selected" : "") });
    nodeEls[node.id] = group;
    group.appendChild(svgEl("rect", {
      x: node.x, y: node.y, width: NODE_W, height: NODE_H, rx: 9, class: "card",
    }));
    if (!node.isMain) {
      group.appendChild(svgEl("rect", {
        x: node.x, y: node.y, width: ACCENT_W, height: NODE_H,
        rx: 2, class: "accent s-" + node.status,
      }));
      if (node.status === "running") {
        group.appendChild(svgEl("rect", {
          x: node.x, y: node.y, width: NODE_W, height: NODE_H, rx: 9,
          class: "node-ping",
        }));
      }
    }
    const tx = node.x + NODE_TEXT_X;
    group.appendChild(svgEl("text", { x: tx, y: node.y + 22 },
      fitText(node.label, textMax, labelFont)));
    group.appendChild(svgEl("text", { x: tx, y: node.y + 38, class: "sub" },
      fitText(node.status + " · " + node.sub, textMax - (node.duration ? 44 : 0), subFont)));
    if (node.duration) {
      group.appendChild(svgEl("text", { x: node.x + NODE_W - 10, y: node.y + 38, class: "sub node-time",
        "text-anchor": "end" }, fmtDuration(node.duration)));
    }
    group.appendChild(svgEl("title", {}, node.label + " — " + node.status));
    const connected = edgesByNode[node.id] || [];
    group.onmouseenter = () => {
      if (node.isMain) hiddenEls.forEach((el) => el.classList.remove("edge-hidden"));
      if (!connected.length) return;
      const keep = new Set(connected);
      for (const el of edgeEls) el.classList.toggle("edge-dim", !keep.has(el));
      // Focus: everything not directly connected steps back.
      const near = neighbours[node.id] || new Set();
      for (const id in nodeEls) nodeEls[id].classList.toggle("faded", id !== node.id && !near.has(id));
    };
    group.onmouseleave = () => {
      if (node.isMain) hiddenEls.forEach((el) => el.classList.add("edge-hidden"));
      for (const el of edgeEls) el.classList.remove("edge-dim");
      for (const id in nodeEls) nodeEls[id].classList.remove("faded");
    };
    if (!node.isMain) group.onclick = () => openDrawer(node.id);
    viewport.appendChild(group);
  }

  const legend = $("graph-legend");
  if (legend) {
    legend.textContent = "Solid edges are exact, dashed are inferred (click an edge for its evidence). " +
      "Hover an agent to trace it" + (layout.edges.some((e) => e.hidden) ? ", or the orchestrator to see every launch" : "") +
      "." + (layout.criticalNodes.size > 1 ? " Outlined: the critical path, the longest chain by duration." : "");
  }

  // Hub files are context, not dependencies, so they are listed rather than
  // drawn. They go to the footer, not #edge-evidence, which showEvidence()
  // overwrites wholesale.
  const hubs = (run.hub_files || []).map(
    (hub) => "shared context: " + hub.path + " (read by " +
             hub.reader_ids.length + " agents)");
  if (hubs.length) {
    // Appended, not assigned: renderDiagnostics ran first and may have put a
    // parse warning there that must not be thrown away.
    const footer = $("diagnostics");
    footer.textContent = [footer.textContent, hubs.join(" · ")]
      .filter(Boolean).join("  |  ");
  }
}

function showEvidence(edge) {
  const box = $("edge-evidence");
  box.hidden = false;
  // Everything here is transcript-derived and untrusted. e.path in particular
  // is a tool call's literal file_path argument, and < > " are all legal in a
  // filename — so this must be escaped exactly as openDrawer escapes the same
  // data. scrub() redacts credentials; it does not HTML-escape.
  const bits = ["<strong>" + esc(edge.kind) + "</strong> — " + esc(edge.confidence),
                esc(edge.src) + " → " + esc(edge.dst)];
  const e = edge.evidence || {};
  if (e.path) bits.push("file: <code>" + esc(e.path) + "</code>");
  if (e.score !== undefined) {
    bits.push("overlap score: " + esc(e.score) + " · longest match: " +
              esc(e.run_words) + " words");
  }
  if (e.snippet) bits.push("<pre>" + esc(e.snippet) + "</pre>");
  if (e.handoff) {
    bits.push("<em>result text also reused:</em> " + esc(e.handoff.run_words) +
              " word match");
  }
  box.innerHTML = bits.join("<br>");
}

// ---------------------------------------------------------------- drawer

// Safe in text AND in a quoted attribute. Serializing a DOM text node escapes
// < > & but not quotes, so a value containing " would end an attribute early
// (data-agent="..."), which is how markup gets injected. Ids and paths come from
// file names and transcripts, i.e. from outside this page.
function esc(text) {
  const div = document.createElement("div");
  div.textContent = text === null || text === undefined ? "" : String(text);
  return div.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Deep-linkable: #view=<tab>&agent=<id> reflects what is on screen (replaceState,
// not location.hash=, so browsing around doesn't spam back history), so any view
// or open agent is a pasteable link. The old #agent=<id> form still opens.
function parseHash() {
  const out = { view: "", agent: "" };
  const raw = ((typeof location !== "undefined" && location.hash) || "").replace(/^#/, "");
  for (const part of raw.split("&")) {
    const at = part.indexOf("=");
    if (at < 0) continue;
    let value = "";
    try { value = decodeURIComponent(part.slice(at + 1)); } catch (err) { value = ""; }
    const key = part.slice(0, at);
    if (key === "view") out.view = value;
    if (key === "agent") out.agent = value;
  }
  return out;
}

function writeHash() {
  if (typeof history === "undefined" || !history.replaceState) return;
  const parts = [];
  if (state.view && state.view !== "timeline") parts.push("view=" + encodeURIComponent(state.view));
  if (state.selected) parts.push("agent=" + encodeURIComponent(state.selected));
  history.replaceState(null, "", (location.pathname || "") + (location.search || "") +
    (parts.length ? "#" + parts.join("&") : ""));
}

function setAgentHash(agentId) {
  writeHash();
}

const DRAWER_TRANSITION_MS = 220;

function closeDrawer() {
  if (state.selected === null) return;
  const drawer = $("drawer");
  const scrim = $("scrim");
  drawer.classList.remove("show");
  scrim.classList.remove("show");
  state.selected = null;
  setAgentHash(null);
  // A reopen within this window (closing one agent, immediately picking
  // another) must not have this timeout hide the drawer out from under it —
  // only finish hiding if nothing else got selected in the meantime.
  setTimeout(() => {
    if (state.selected === null) {
      drawer.hidden = true;
      scrim.hidden = true;
    }
  }, DRAWER_TRANSITION_MS);
}

// One file's diff as table rows, numbered from the hunk starts when the patch gave them.
function diffHtml(file) {
  let rows = "";
  for (const h of file.hunks) {
    let oldNo = h.old_start;
    let newNo = h.new_start;
    const known = typeof oldNo === "number" && typeof newNo === "number";
    rows += '<tr class="diff-hunk"><td colspan="3">' +
      esc((known ? "@@ −" + oldNo + " +" + newNo + " @@" : "@@ edit @@") + (h.at ? "  " + fmtClock(h.at) : "")) + "</td></tr>";
    for (const line of h.lines) {
      const sign = line.charAt(0);
      const cls = sign === "+" ? "add" : sign === "-" ? "del" : "ctx";
      const o = known && sign !== "+" ? oldNo++ : "";
      const n = known && sign !== "-" ? newNo++ : "";
      rows += '<tr class="diff-' + cls + '"><td class="ln">' + o + '</td><td class="ln">' + n +
        '</td><td class="code">' + esc(line) + "</td></tr>";
    }
  }
  return '<div class="diff-wrap"><table class="diff">' + rows + "</table></div>";
}

// What the agent changed, file by file, each diff folded until opened.
function changesHtml(files) {
  if (!files || !files.length) return '<p class="source-note">no file changes recorded</p>';
  const real = files.filter((f) => !f.scratch);
  const added = real.reduce((s, f) => s + f.added, 0);
  const removed = real.reduce((s, f) => s + f.removed, 0);
  const one = (f) => '<details class="change"><summary>' +
    '<span class="change-name" title="' + esc(f.path) + '">' + esc(fileName(f.path)) + "</span>" +
    (f.created ? '<span class="change-tag">new</span>' : "") + (f.scratch ? '<span class="change-tag">scratch</span>' : "") +
    '<span class="change-count"><b class="plus">+' + f.added + '</b> <b class="minus">−' + f.removed + "</b>" +
    (f.edits > 1 ? " · " + f.edits + " edits" : "") + "</span></summary>" +
    '<div class="change-path">' + esc(f.path) + "</div>" + diffHtml(f) +
    (f.truncated ? '<p class="source-note">Longer than Cuelight keeps per agent; the counts above are complete.</p>' : "") +
    "</details>";
  const scratch = files.filter((f) => f.scratch);
  return '<p class="change-total">' + esc("+" + added + " −" + removed + " in " + real.length +
    (real.length === 1 ? " file" : " files")) + "</p>" + real.map(one).join("") +
    (scratch.length ? '<p class="source-note">Scratch files (not counted):</p>' + scratch.map(one).join("") : "");
}

// The agent panel's "waited on you" line: answered waits, plus the one still open, live.
function waitRow(agent, now) {
  const count = agent.wait_count || 0;
  if (!count) return "";
  const parts = [];
  if (agent.waited_s > 0 || !agent.wait_open_since) parts.push(esc(fmtDuration(agent.waited_s || 0)));
  if (agent.wait_open_since) parts.push("waiting now for " + liveSpan(0, agent.wait_open_since, now));
  return "<dt>waited on you</dt><dd>" + parts.join(", ") + " (" + count + (count === 1 ? " wait" : " waits") + ")</dd>";
}

async function openDrawer(agentId) {
  const drawer = $("drawer");
  const scrim = $("scrim");
  drawer.hidden = false;
  scrim.hidden = false;
  scrim.onclick = closeDrawer;
  // Force a layout flush so the browser registers the off-screen starting
  // position before .show is added on the next line — added in the same
  // tick, the transform and its transition would both apply at once and
  // nothing would visibly slide.
  void drawer.offsetWidth;
  drawer.classList.add("show");
  scrim.classList.add("show");
  drawer.innerHTML = "<p>Loading…</p>";
  let agent;
  try {
    agent = await api("/api/agent/" + encodeURIComponent(agentId));
  } catch (err) {
    drawer.innerHTML = "<p>Could not load this agent.</p>";
    return;
  }
  state.selected = agentId;
  setAgentHash(agentId);
  ingestToolCalls(agentId, agent.description || agent.agent_id, agent.tool_calls);
  if (state.view === "activity") renderTicker();

  const rows = [
    ["status", agent.status],
    ["type", agent.agent_type],
    ["model", fmtModelShort(agent.model) +
      (agent.model && agent.model !== fmtModelShort(agent.model) ? "  (" + agent.model + ")" : "")],
    ["launch", agent.launch_mode],
    ["duration", fmtDuration(agent.duration_s)],
    ["tokens", fmtTokens(agent.tokens)],
    ...(agent.loop ? [["possible loop", loopText(agent.loop)]] : []),
    ...(agent.cost !== null && agent.cost !== undefined && state.run && state.run.cost
      ? [["cost", fmtMoney(agent.cost, state.run.cost.currency)]] : []),
    ["cache hit", fmtPct(cacheHitRatio(agent.tokens)) +
      (cacheHitRatio(agent.tokens) !== null ? "  (" + fmtTokenMix(agent.tokens) + ")" : "")],
    ["rounds", agent.rounds.length],
    ["tool calls", agent.tool_calls.length],
  ];

  const tools = agent.tool_calls.slice(-40)
    .map((t) => esc(t.name) + "  " + esc(t.target)).join("\n");

  drawer.innerHTML =
    '<div class="drawer-head">' +
    '<button class="close" type="button" id="drawer-close" aria-label="Close">&times;</button>' +
    "<h2>" + esc(agent.description || agent.agent_id) + "</h2>" +
    '<div class="drawer-sub"><span class="status-pill s-' + esc(agent.status) + '">' +
    esc(agent.status) + "</span><span>" + esc(agent.agent_type) + "</span><code>" +
    esc(agent.agent_id) + "</code></div></div>" +
    '<div class="drawer-body">' +
    "<dl>" + rows.map(([k, v]) =>
      "<dt>" + esc(k) + "</dt><dd>" + esc(v) + "</dd>").join("") + waitRow(agent, waitNow(state.run)) +
      (agent.verification ? "<dt>checked its work</dt><dd>" + esc((agent.verification.state === "checked" ? "yes, " : "") +
        checkText(agent.verification)) + "</dd>" : "") + producedRow(agent.outcomes) + wasteRow(agent.waste) + pressureRow(agent.context_peak) + errorsRow(agent.errors) +
      contextRow(agent.context, agent.agent_id, state.run) + "</dl>" +
    "<h3>Tool mix</h3>" + (renderToolMix(agent.tool_calls) || '<p class="source-note">no tool calls yet</p>') +
    "<h3>Objective</h3><pre>" + esc(agent.objective || "\u2014") + "</pre>" +
    '<div class="source-note">' + esc(agent.objective_source) + "</div>" +
    "<h3>Expected output</h3><pre>" + esc(agent.expected_output || "\u2014") + "</pre>" +
    '<div class="source-note">' + esc(agent.expected_output_source) + "</div>" +
    "<h3>Returned result</h3><pre>" + esc(agent.result || "(still running)") + "</pre>" +
    "<details><summary>Full brief</summary><pre>" + esc(agent.brief) + "</pre></details>" +
    "<details><summary>Tool calls (last 40)</summary><pre>" + tools + "</pre></details>" +
    "<h3>Changes</h3>" + changesHtml(agent.change_files) +
    "<h3>Files written</h3><pre>" + esc(agent.files_written.join("\n") || "\u2014") + "</pre>" +
    "<h3>Files read</h3><pre>" + esc(agent.files_read.join("\n") || "\u2014") + "</pre>" +
    "</div>";

  $("drawer-close").onclick = closeDrawer;
}

document.addEventListener("keydown", (event) => {
  // Escape closes the topmost thing only: a dialog first, then the agent panel.
  if (event.key === "Escape" && !palette.open && !helpOpen()) closeDrawer();
});
