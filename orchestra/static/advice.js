// Cuelight: the Advice tab. The run's one-line verdict and its fix-it cards (orchestra/advice.py):
// what happened, what it cost and the change to make, which you copy. Cuelight never applies one.
//
// Loaded after app.js and tabs.js, whose globals (state, $, esc, fmtDuration, fmtCount, fmtMoney,
// plural, priceNote, openDrawer, noteFocus, restoreFocus) it uses; app.js's render() calls
// renderAdvice and renderAdviceBadge. A static report inlines it after tabs.js.

const ADVICE_HIDDEN_KEY = "cuelight-advice-hidden";
const ADVICE_WHERE = {
  settings: "In settings.json:", agent: "In the agent's definition:", claude_md: "In CLAUDE.md:",
  instructions: "In the agent's instructions:", habit: "A habit:", none: "",
};

// Hidden cards are a per-browser convenience: a private window simply forgets them.
function adviceHidden() {
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(ADVICE_HIDDEN_KEY) : null;
    const ids = raw ? JSON.parse(raw) : [];
    return Array.isArray(ids) ? ids : [];
  } catch (err) { return []; }
}

function setAdviceHidden(ids) {
  try { if (typeof localStorage !== "undefined") localStorage.setItem(ADVICE_HIDDEN_KEY, JSON.stringify(ids.slice(-200))); }
  catch (err) { /* hiding lasts until the page reloads */ }
}

function adviceImpact(impact, currency) {
  const parts = [];
  if (impact.cost) parts.push(fmtMoney(impact.cost, currency));
  if (impact.seconds >= 1) parts.push(fmtDuration(impact.seconds));
  if (impact.tokens) parts.push(fmtCount(impact.tokens) + " tokens");
  if (!parts.length) return "";
  return '<span class="advice-impact">' + esc((impact.label ? impact.label + ": " : "") + parts.join(", ")) + "</span>";
}

function adviceAgents(card, run) {
  const names = (card.agents || []).map((id) => {
    if (!id) return '<span class="advice-agent">Main session</span>';
    const agent = (run.agents || []).find((a) => a.agent_id === id);
    return '<button type="button" class="advice-agent" data-agent="' + esc(id) + '">' +
      esc((agent && agent.description) || id) + "</button>";
  });
  return names.length ? '<p class="advice-agents">' + names.join("") + "</p>" : "";
}

function adviceCardHtml(card, run, currency, isHidden) {
  const fix = card.fix || {};
  const where = ADVICE_WHERE[fix.where] || "";
  return '<article class="advice-card" data-severity="' + esc(card.severity) + '">' +
    '<header><span class="advice-tag" data-tag="' + esc(card.tag) + '">' + esc(card.tag) + "</span>" +
    "<h3>" + esc(card.title) + "</h3>" + adviceImpact(card.impact || {}, currency) +
    '<button type="button" class="advice-hide" data-hide="' + esc(card.id) + '">' + (isHidden ? "Unhide" : "Hide") +
    "</button></header>" +
    '<p class="advice-happened">' + esc(card.happened) + "</p>" + adviceAgents(card, run) +
    '<div class="advice-fix"><p>' + (where ? '<span class="advice-where">' + esc(where) + "</span> " : "") +
    esc(fix.text || "") + "</p>" +
    (fix.snippet ? '<pre class="advice-snippet">' + esc(fix.snippet) + '</pre><button type="button" class="advice-copy" data-copy="' +
      esc(card.id) + '">Copy</button>' : "") + "</div></article>";
}

function adviceHtml(run, hidden, showHidden) {
  if (run.replay_at !== undefined) return '<p class="advice-note">Advice describes the whole session. Leave replay to see it.</p>';
  const a = run.advice;
  if (!a) return '<p class="advice-note">This run has no advice: it was recorded by an older Cuelight.</p>';
  const currency = run.cost && run.cost.currency;
  const note = priceNote(run.cost);
  const cards = a.cards || [];
  const hiddenHere = cards.filter((c) => hidden.indexOf(c.id) >= 0).length;
  const shown = showHidden ? cards : cards.filter((c) => hidden.indexOf(c.id) < 0);
  let html = '<section class="advice-verdict"><p>' + esc((a.parts || []).join(" · ")) + "</p>" +
    '<button type="button" class="advice-copy" data-copy="verdict">Copy</button>' +
    (note ? '<p class="advice-price">Dollar figures: ' + esc(note) + ".</p>" : "") + "</section>";
  html += shown.length
    ? shown.map((c) => adviceCardHtml(c, run, currency, hidden.indexOf(c.id) >= 0)).join("")
    : '<p class="advice-note">' + (cards.length ? "Every card is hidden." : "Nothing to fix in this run.") + "</p>";
  if (hiddenHere) {
    html += '<button type="button" class="advice-show-hidden" data-show-hidden="' + (showHidden ? "0" : "1") + '">' +
      (showHidden ? "Leave the hidden cards out" : "Show " + plural(hiddenHere, "hidden card", "hidden cards")) + "</button>";
  }
  return html;
}

async function copyAdviceText(text, btn) {
  let ok = false;
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (err) { /* ok stays false; the button says so below */ }
  const original = btn.textContent;
  btn.textContent = ok ? "Copied!" : "Copy failed";
  setTimeout(() => { btn.textContent = original; }, 1500);
}

function renderAdvice(run) {
  const box = $("advice");
  if (!box) return;
  if (state.usual === undefined && !state.offline) loadUsual();     // opened straight from a link
  const a = run.advice;
  const html = adviceHtml(run, adviceHidden(), !!state.adviceShowHidden) + adviceUsualHtml(state.usual);
  // The page redraws every few seconds: leave identical markup (and its handlers, selection and scroll) alone.
  if (box.dataset && box.dataset.html === html) return;
  const spot = noteFocus(box);
  if (box.dataset) box.dataset.html = html;
  box.innerHTML = html;
  for (const el of box.querySelectorAll("[data-agent]")) el.onclick = () => openDrawer(el.getAttribute("data-agent"));
  for (const btn of box.querySelectorAll("[data-copy]")) {
    btn.onclick = () => {
      const id = btn.getAttribute("data-copy");
      const card = ((a && a.cards) || []).find((c) => c.id === id);
      copyAdviceText(id === "verdict" ? ((a && a.parts) || []).join(" · ") : (card && card.fix.snippet) || "", btn);
    };
  }
  for (const btn of box.querySelectorAll("[data-hide]")) {
    btn.onclick = () => {
      const id = btn.getAttribute("data-hide");
      const ids = adviceHidden();
      const at = ids.indexOf(id);
      if (at >= 0) ids.splice(at, 1); else ids.push(id);
      setAdviceHidden(ids);
      renderAdvice(state.run);
      renderAdviceBadge(state.run);
    };
  }
  for (const btn of box.querySelectorAll("[data-show-hidden]")) {
    btn.onclick = () => { state.adviceShowHidden = btn.getAttribute("data-show-hidden") === "1"; renderAdvice(state.run); };
  }
  restoreFocus(box, spot);
}

function renderAdviceBadge(run) {
  const badge = $("advice-badge");
  if (!badge) return;
  const hidden = adviceHidden();
  // What the verdict counts: a failure, or a card with something to change. Not an information card.
  const cards = ((run.advice && run.advice.cards) || []).filter((c) => hidden.indexOf(c.id) < 0 &&
    (c.severity === "failure" || !(c.fix && c.fix.where === "none")));
  badge.hidden = !cards.length;
  badge.textContent = String(cards.length);
  if (badge.classList) badge.classList.toggle("badge-fail", cards.some((c) => c.severity === "failure"));
}

// "Your usual": what each kind of agent takes, from past sessions (orchestra/baselines.py, /api/usual).
function usualText(u, seconds) {
  if (!u) return "";
  if (!u.enough) return "not enough past runs yet (" + u.n + ")";
  const where = u.scope === "project" ? "this project" : "all your projects";
  let text = fmtDuration(u.typical_s) + " typical, " + fmtDuration(u.slow_s) + " slow case, " +
    fmtCount(u.typical_tokens) + " tokens, fails " + Math.round(u.fail_rate * 100) + "% (" + u.n + " runs, " + where + ")";
  if (seconds && u.typical_s) text += ". This one: " + fmtDuration(seconds) + ", " + (seconds / u.typical_s).toFixed(1) + "x";
  return text;
}

function adviceUsualHtml(usual) {
  if (state.offline) return "";
  const head = '<section class="advice-usual"><h3>Your usual</h3>';
  if (!usual) return head + '<p class="advice-note">Loading your past runs\u2026</p></section>';
  if (usual.state === "off") return "";
  const groups = usual.groups || [];
  // A failed rescan keeps the numbers already read: show what went wrong, and the numbers under it.
  const problem = usual.state === "error"
    ? '<p class="advice-note">' + esc(usual.error || "Could not read your past runs.") + "</p>" : "";
  if (usual.state === "error" && !groups.length) return head + problem + "</section>";
  if (!groups.length) {
    return head + '<p class="advice-note">' + (usual.state === "scanning" ? "Learning from your past runs\u2026"
      : "No finished agents in your past sessions yet. Cuelight learns what each kind of agent usually takes from them.") +
      "</p></section>";
  }
  const enough = groups.filter((g) => g.enough);
  const few = groups.filter((g) => !g.enough);
  const price = usual.source === "list" ? " Costs: list price, as of " + usual.as_of + "." : "";
  let html = head + problem + '<p class="advice-note">' + esc("From " + plural(usual.sessions, "past session", "past sessions") +
    ". Typical is the middle run; slow case is the 90th percentile." + price) + "</p>";
  if (enough.length) {
    const num = (v) => '<td class="num">' + esc(v) + "</td>";
    html += '<div class="advice-usual-wrap"><table><thead><tr><th scope="col">Agent type</th><th scope="col">Model</th>' +
      '<th scope="col">Runs</th><th scope="col">Typical</th><th scope="col">Slow case</th><th scope="col">Tokens</th>' +
      '<th scope="col">Cost</th><th scope="col">Fails</th><th scope="col">From</th></tr></thead><tbody>' +
      enough.map((g) => "<tr><td>" + esc(g.type) + "</td><td>" + esc(g.model) + "</td>" + num(String(g.n)) +
        num(fmtDuration(g.typical_s)) + num(fmtDuration(g.slow_s)) + num(fmtCount(g.typical_tokens)) +
        num(g.typical_cost === null || g.typical_cost === undefined ? "\u2014" : fmtMoney(g.typical_cost, usual.currency)) +
        num(Math.round(g.fail_rate * 100) + "%") + "<td>" + (g.scope === "project" ? "this project" : "all projects") +
        "</td></tr>").join("") + "</tbody></table></div>";
  }
  if (few.length) {
    html += '<p class="advice-note">' + esc("Not enough past runs yet (fewer than 5): " +
      few.slice(0, 12).map((g) => g.type + " on " + g.model + " (" + g.n + ")").join(", ")) + "</p>";
  }
  return html + "</section>";
}

async function loadUsual() {
  if (state.offline || state.usualLoading) return;
  state.usualLoading = true;
  const asked = state.sessionId;
  let answer;
  try { answer = await api("/api/usual"); }
  catch (err) { answer = { state: "error", error: "Could not load your usual numbers.", groups: [] }; }
  finally { state.usualLoading = false; }
  if (asked !== state.sessionId) {
    // The session changed while this was on its way: these numbers are for the old project. Drop them.
    if (state.view === "advice") loadUsual();
    return;
  }
  state.usual = answer;
  if (state.view === "advice" && state.run) renderAdvice(state.run);
  if (state.usual && state.usual.state === "scanning" && state.view === "advice") setTimeout(loadUsual, 3000);
}
