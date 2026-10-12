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
  const spot = noteFocus(box);
  const a = run.advice;
  box.innerHTML = adviceHtml(run, adviceHidden(), !!state.adviceShowHidden);
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
  const cards = ((run.advice && run.advice.cards) || []).filter((c) => hidden.indexOf(c.id) < 0);
  badge.hidden = !cards.length;
  badge.textContent = String(cards.length);
  if (badge.classList) badge.classList.toggle("badge-fail", cards.some((c) => c.severity === "failure"));
}
