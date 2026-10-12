"""Keyboard focus survives a live redraw: every poll rewrites a view's markup (node)."""

import json
import unittest

from tests import test_insights_ui as ui

# A small DOM for the markup the views write: setting innerHTML parses it into elements
# (attributes, classes, parents), querySelectorAll matches compound selectors (tag, .class,
# [attr]) and focus() moves document.activeElement. Enough to redraw a view and look again.
FAKE_DOM = r"""
const VOID_TAGS = new Set(["br", "img", "input", "hr", "meta", "link", "wbr"]);
const unescapeAttr = (v) => v.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<")
  .replace(/&gt;/g, ">").replace(/&amp;/g, "&");
function matchesSel(el, sel) {
  const parts = sel.trim().split(/\s+/);
  if (!matchesOne(el, parts.pop())) return false;
  for (let p = el.parentElement; p && p !== box && parts.length; p = p.parentElement) {
    if (matchesOne(p, parts[parts.length - 1])) parts.pop();
  }
  return parts.length === 0;
}
function matchesOne(el, sel) {
  const m = /^([a-z0-9]+)?((?:\.[\w-]+)*)((?:\[[\w-]+\])*)$/i.exec(sel);
  if (!m) throw new Error("selector not supported by the stub: " + sel);
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  for (const c of m[2].match(/[\w-]+/g) || []) if (el.classList.indexOf(c) < 0) return false;
  for (const a of m[3].match(/[\w-]+/g) || []) if (!el.hasAttribute(a)) return false;
  return true;
}
function descendants(el) {
  const out = [];
  for (const c of el.children) out.push(c, ...descendants(c));
  return out;
}
function domMethods(el) {
  el.querySelectorAll = (sel) => descendants(el).filter((d) => matchesSel(d, sel));
  el.querySelector = (sel) => el.querySelectorAll(sel)[0] || null;
  // By the live tree: an element a redraw replaced is no longer inside.
  el.contains = (other) => other === el || descendants(el).indexOf(other) >= 0;
  return el;
}
function makeEl(tag, attrs, parent) {
  const el = domMethods({
    tagName: tag.toUpperCase(), attrs: attrs, parentElement: parent, children: [], style: {},
    get classList() { return (attrs["class"] || "").split(/\s+/).filter(Boolean); },
    get dataset() {
      const out = {};
      for (const k of Object.keys(attrs)) {
        if (k.startsWith("data-")) out[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = attrs[k];
      }
      return out;
    },
    get hidden() { return "hidden" in attrs; },
    set hidden(v) { if (v) attrs.hidden = ""; else delete attrs.hidden; },
    hasAttribute: (n) => n in attrs,
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
    setAttribute: (n, v) => { attrs[n] = String(v); },
    removeAttribute: (n) => { delete attrs[n]; },
    matches: (sel) => matchesSel(el, sel),
    focus: () => { document.focusCalls = (document.focusCalls || 0) + 1; document.activeElement = el; },
  });
  return el;
}
function parseInto(root, html) {
  root.children = [];
  let cur = root;
  const tags = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^\s=>\/]+(?:="[^"]*")?)*)\s*(\/?)>/g;
  let m;
  while ((m = tags.exec(html))) {
    const tag = m[2].toLowerCase();
    if (m[1]) {
      for (let p = cur; p && p !== root; p = p.parentElement) {
        if (p.tagName === tag.toUpperCase()) { cur = p.parentElement; break; }
      }
      continue;
    }
    const attrs = {};
    const pairs = /([^\s=]+)(?:="([^"]*)")?/g;
    let a;
    while ((a = pairs.exec(m[3]))) attrs[a[1]] = a[2] === undefined ? "" : unescapeAttr(a[2]);
    const el = makeEl(tag, attrs, cur);
    cur.children.push(el);
    if (!VOID_TAGS.has(tag) && !m[4]) cur = el;
  }
}
let boxHtml = "";
Object.defineProperty(box, "innerHTML", {get: () => boxHtml, set: (v) => { boxHtml = v; parseInto(box, v); }});
box.tagName = "DIV";
box.parentElement = null;
box.children = [];
domMethods(box);
document.activeElement = null;
// What has focus, by the attributes that name it (a summary by its <details>).
function focusedName() {
  const el = document.activeElement;
  if (!el || !box.contains(el)) return null;
  const named = el.tagName === "SUMMARY" ? el.parentElement : el;
  const all = box.querySelectorAll(named.tagName.toLowerCase());
  return {tag: el.tagName, attrs: named.attrs, nth: all.indexOf(named)};
}
"""

# ui.run_js adds the focus helpers (noteFocus, restoreFocus) to every run.
INSIGHTS = ui.INSIGHT_FNS
PROMPTS = ("esc", "fmtDuration", "fmtCount", "fmtPct", "fmtMoney", "fmtClock", "statusVar", "insMetric",
           "insCard", "cardKey", "insEmpty", "fileLabel", "promptsHtml", "renderPrompts")
AGENTS = ("esc", "fileName", "checkText", "fmtDuration", "fmtCount", "fmtMoney", "fmtModelShort", "statusVar",
          "insMetric", "cardKey", "insCard", "insEmpty", "plural", "liveSpan", "waitNow", "agentMatchesFilter",
          "agentFlags", "agentSpend", "agentSortValue", "agentsRows", "agentsHtml", "priceNote", "renderAgents")
SPEND = ("esc", "fmtDuration", "fmtCount", "fmtMoney", "fmtPct", "fmtClock", "fmtModelShort", "insMetric", "cardKey",
         "insCard", "insEmpty", "insRank", "plural", "spendFmt", "spendSeries", "spendReadout", "spendChart", "spendHtml",
         "priceNote", "renderSpend")
HISTORY = ("esc", "fmtCount", "fmtDuration", "fmtMoney", "fmtPct", "fmtWhen", "fmtHistoryValue", "deltaVerdict",
           "renderHistory", "renderCompare")


def run(names, consts, summary, body):
    return ui.run_js(names, consts, FAKE_DOM + "const RUN = %s;\n" % json.dumps(summary) + body)


def focus_then_redraw(names, consts, summary, draw, pick, between=""):
    """Draw, focus the element `pick` (JS, given `all`: every element in the view) chooses,
    redraw, and return what had focus before and after."""
    body = (draw + ";\n"
            "const every = (function walk(el) { return el.children.reduce((a, c) => a.concat([c], walk(c)), []); })(box);\n"
            "const target = (" + pick + ")(every);\n"
            "if (!target) throw new Error('nothing to focus');\n"
            "target.focus();\nconst before = focusedName();\n" + between + ";\n" + draw + ";\n"
            "console.log(JSON.stringify({before: before, after: focusedName(), "
            "same: document.activeElement !== target && box.contains(document.activeElement)}));")
    return run(names, consts, summary, body)


@unittest.skipIf(ui.NODE is None, "node is not on PATH")
class TestFocusHelper(unittest.TestCase):
    """noteFocus / restoreFocus on hand-made markup."""

    def go(self, html, pick, new_html=None, extra=""):
        body = ("box.innerHTML = %s;\n" % json.dumps(html) +
                "const every = (function walk(el) { return el.children.reduce((a, c) => a.concat([c], walk(c)), []); })(box);\n"
                "(" + pick + ")(every).focus();\n" + extra +
                "const spot = noteFocus(box);\nbox.innerHTML = %s;\n" % json.dumps(new_html or html) +
                "document.focusCalls = 0; restoreFocus(box, spot);\n"
                "console.log(JSON.stringify({spot: spot, after: focusedName(), calls: document.focusCalls}));")
        return run(ui.FOCUS_FNS, (), {}, body)

    def test_a_named_control_is_found_again_by_its_name(self):
        html = '<button data-jump="a">A</button><button data-jump="b">B</button>'
        out = self.go(html, "(e) => e[1]", '<button data-jump="c">C</button><button data-jump="b">B</button>'
                      '<button data-jump="a">A</button>')
        self.assertEqual(out["after"]["attrs"]["data-jump"], "b")

    def test_the_same_name_twice_keeps_which_one_it_was(self):
        html = '<p data-agent="x">1</p><p data-agent="y">2</p><p data-agent="x">3</p>'
        out = self.go(html, "(e) => e[2]")
        self.assertEqual(out["after"]["nth"], 2)

    def test_a_summary_is_named_by_its_details(self):
        html = '<details data-prompt="1"><summary>one</summary></details><details data-prompt="2"><summary>two</summary></details>'
        out = self.go(html, "(e) => e.find((x) => x.tagName === 'SUMMARY' && x.parentElement.attrs['data-prompt'] === '2')",
                      '<details data-prompt="0"><summary>z</summary></details>' + html)
        self.assertEqual(out["after"]["tag"], "SUMMARY")
        self.assertEqual(out["after"]["attrs"]["data-prompt"], "2")

    def test_a_control_without_a_name_goes_by_tag_and_class(self):
        html = '<button class="glance-fold" data-fold-all="fold">Fold all</button>'
        out = self.go(html, "(e) => e[0]", '<button class="glance-fold" data-fold-all="unfold">Unfold all</button>')
        self.assertEqual(out["after"]["attrs"]["data-fold-all"], "unfold")

    def test_a_control_that_is_gone_moves_focus_nowhere(self):
        out = self.go('<button data-jump="a">A</button>', "(e) => e[0]", '<button data-jump="b">B</button>')
        self.assertEqual(out["calls"], 0)

    def test_focus_outside_the_view_is_left_alone(self):
        out = self.go('<button data-jump="a">A</button>', "(e) => e[0]",
                      extra="document.activeElement = {tagName: 'BUTTON', parentElement: null};\n")
        self.assertIsNone(out["spot"])
        self.assertEqual(out["calls"], 0)


@unittest.skipIf(ui.NODE is None, "node is not on PATH")
class TestViewsKeepFocus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = ui.demo_run(events=True)

    def kept(self, out, attr):
        self.assertTrue(out["same"], out)
        self.assertEqual(out["after"]["tag"], out["before"]["tag"])
        self.assertEqual(out["after"]["attrs"].get(attr), out["before"]["attrs"].get(attr))
        self.assertEqual(out["after"]["nth"], out["before"]["nth"])

    # ---------------------------------------------------------------- prompts
    def test_prompts_keep_focus_on_a_prompt(self):
        out = focus_then_redraw(PROMPTS, (), self.summary, "renderPrompts(RUN)",
                                "(e) => e.find((x) => x.tagName === 'SUMMARY' && x.parentElement.attrs['data-prompt'] === '2')")
        self.kept(out, "data-prompt")
        self.assertEqual(out["after"]["attrs"]["data-prompt"], "2")

    def test_prompts_keep_focus_on_an_agent_inside_an_open_prompt(self):
        out = focus_then_redraw(PROMPTS, (), self.summary,
                                "state.openPrompts = new Set(['2']); renderPrompts(RUN)",
                                "(e) => e.filter((x) => x.attrs['data-agent'] !== undefined)[3]")
        self.kept(out, "data-agent")

    # --------------------------------------------------------------- insights
    def test_insights_keep_focus_on_a_fold_button(self):
        out = focus_then_redraw(INSIGHTS, ("BUCKET_VARS",), self.summary, "renderInsights(RUN)",
                                "(e) => e.find((x) => x.attrs['data-fold'] === 'files')")
        self.kept(out, "data-fold")

    def test_insights_keep_focus_on_a_chip(self):
        # Found again by its card, not its place: chips sort worst first and can move between polls
        # (test_a_named_control_is_found_again_by_its_name moves one).
        out = focus_then_redraw(INSIGHTS, ("BUCKET_VARS",), self.summary, "renderInsights(RUN)",
                                "(e) => e.filter((x) => x.attrs['data-jump'] !== undefined).pop()")
        self.kept(out, "data-jump")

    def test_insights_keep_focus_on_fold_all_as_it_turns_into_unfold_all(self):
        out = focus_then_redraw(INSIGHTS, ("BUCKET_VARS",), self.summary, "renderInsights(RUN)",
                                "(e) => e.find((x) => x.attrs['data-fold-all'] !== undefined)",
                                between="state.folded = new Set(['files', 'parallelism'])")
        self.assertTrue(out["same"], out)
        self.assertEqual(out["before"]["attrs"]["data-fold-all"], "fold")
        self.assertEqual(out["after"]["attrs"]["data-fold-all"], "unfold")

    def test_insights_keep_focus_on_an_agent_named_twice(self):
        out = focus_then_redraw(INSIGHTS, ("BUCKET_VARS",), self.summary, "renderInsights(RUN)",
                                "(e) => { const named = e.filter((x) => x.attrs['data-agent'] !== undefined);"
                                " return named.find((x, i) => named.findIndex((y) => y.attrs['data-agent'] === "
                                "x.attrs['data-agent']) !== i); }")
        self.kept(out, "data-agent")

    # ----------------------------------------------------------------- agents
    def agents(self, pick):
        return focus_then_redraw(AGENTS, ("AGENT_SORTS",), self.summary,
                                 "state.filterStatuses = new Set(); state.filterText = ''; renderAgents(RUN)", pick)

    def test_agents_keep_focus_on_a_row(self):
        self.kept(self.agents("(e) => e.filter((x) => x.tagName === 'TR' && x.attrs['data-agent'])[2]"), "data-agent")

    def test_agents_keep_focus_on_a_sort_heading(self):
        self.kept(self.agents("(e) => e.find((x) => x.attrs['data-sort'] === 'cost')"), "data-sort")

    def test_agents_keep_focus_on_the_needs_a_look_toggle(self):
        out = self.agents("(e) => e.find((x) => x.classList.indexOf('agents-only') >= 0)")
        self.assertTrue(out["same"], out)
        self.assertIn("agents-only", out["after"]["attrs"]["class"])

    # ------------------------------------------------------------------ spend
    def test_spend_keeps_focus_on_the_split_toggle(self):
        out = focus_then_redraw(SPEND, ("SPEND_COLORS",), self.summary, "renderSpend(RUN)",
                                "(e) => e.find((x) => x.attrs['data-split'] === 'model')")
        self.kept(out, "data-split")

    def test_spend_keeps_focus_on_the_chart(self):
        out = focus_then_redraw(SPEND, ("SPEND_COLORS",), self.summary, "renderSpend(RUN)",
                                "(e) => e.find((x) => x.tagName === 'SVG')")
        self.assertTrue(out["same"], out)
        self.assertEqual(out["after"]["tag"], "SVG")

    def test_spend_keeps_focus_on_an_agent_link(self):
        out = focus_then_redraw(SPEND, ("SPEND_COLORS",), self.summary, "renderSpend(RUN)",
                                "(e) => e.filter((x) => x.attrs['data-agent'] !== undefined)[1]")
        self.kept(out, "data-agent")

    # ---------------------------------------------------------------- history
    def test_history_keeps_focus_on_a_run(self):
        runs = [{"session_id": sid, "project_name": "api", "started_at": 1790000000 + i, "first_seen_at": 1790000000,
                 "agents": 4, "failed": 0, "wall_s": 60.0, "tokens_total": 1000, "cost": None, "currency": None}
                for i, sid in enumerate(("s1", "s2", "s3"))]
        setup = ("state.history = {data: {enabled: true, runs: %s}, selected: [], compare: null}; renderHistory()"
                 % json.dumps(runs))
        out = focus_then_redraw(HISTORY, (), {}, setup, "(e) => e.find((x) => x.attrs['data-session'] === 's2')")
        self.kept(out, "data-session")


class TestEveryLiveViewKeepsFocus(unittest.TestCase):
    """Views redrawn on a poll note focus before writing their markup and restore it after."""

    def test_each_redraw_is_bracketed(self):
        js = ui.read("app.js") + "\n" + ui.read("tabs.js")
        for name in ("renderPrompts", "renderInsights", "renderAgents", "renderSpend", "renderWorkfloor",
                     "renderFleet", "renderHistory"):
            body = ui.fn(js, name)
            self.assertIn("noteFocus(box)", body, name)
            self.assertIn("restoreFocus(box, ", body, name)
            note = body.index("noteFocus(box)")
            self.assertLess(note, body.rindex("box.innerHTML ="), name)
            self.assertGreater(body.index("restoreFocus(box, "), body.rindex("box.innerHTML ="), name)


if __name__ == "__main__":
    unittest.main()
