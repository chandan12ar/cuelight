"""The Advice tab: verdict, fix-it cards, hiding, copying and the badge, executed under node."""

import json
import unittest

from tests import test_insights_ui as ui

FNS = ("esc", "fmtDuration", "fmtCount", "fmtMoney", "plural", "priceNote", "adviceHidden", "setAdviceHidden",
       "adviceImpact", "adviceAgents", "adviceCardHtml", "adviceHtml", "renderAdviceBadge",
       "usualText", "adviceUsualHtml")
CONSTS = ("ADVICE_HIDDEN_KEY", "ADVICE_WHERE")
STORAGE = ("global.localStorage = {data: {}, getItem(k) { return k in this.data ? this.data[k] : null; },"
           " setItem(k, v) { this.data[k] = String(v); }};\n")


def html_of(summary, hidden=(), show=False):
    body = ("const RUN = %s;\nconsole.log(JSON.stringify(adviceHtml(RUN, %s, %s)));"
            % (json.dumps(summary), json.dumps(list(hidden)), "true" if show else "false"))
    return ui.run_js(FNS, CONSTS, body)


def card(**kw):
    base = {"id": "abc123abc123", "rule": "compact", "tag": "sure", "severity": "fix", "title": "T",
            "happened": "H", "agents": [], "impact": {"seconds": 90, "tokens": 0, "cost": None, "label": "lost"},
            "fix": {"where": "habit", "text": "Do X", "snippet": ""}}
    base.update(kw)
    return base


@unittest.skipIf(ui.NODE is None, "node is not on PATH")
class TestAdviceTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = ui.demo_run()
        cls.html = html_of(cls.summary)

    def with_cards(self, cards, **extra):
        s = json.loads(json.dumps(self.summary))
        s["advice"]["cards"] = cards
        s.update(extra)
        return s

    def test_the_demo_run_has_a_verdict_and_cards(self):
        a = self.summary["advice"]
        self.assertGreater(len(a["cards"]), 0)
        self.assertIn(" · ".join(a["parts"]), self.html)
        self.assertEqual(self.html.count('class="advice-card"'), len(a["cards"]))
        for bad in ("NaN", "undefined", "null"):
            self.assertNotIn(bad, self.html)

    def test_text_is_escaped(self):
        html = html_of(self.with_cards([card(title="<script>x</script>",
                                             fix={"where": "settings", "text": "t", "snippet": "<img src=x>"})]))
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", html)
        self.assertIn("&lt;img src=x&gt;", html)
        self.assertNotIn("<script>x", html)

    def test_a_snippet_has_a_copy_button_and_agents_are_links(self):
        s = self.with_cards([card(agents=["a1", ""], fix={"where": "settings", "text": "t", "snippet": "{}"})])
        html = html_of(s)
        self.assertIn('data-copy="abc123abc123"', html)
        self.assertIn('data-agent="a1"', html)
        self.assertIn("Main session", html)

    def test_hidden_cards_are_left_out_and_counted(self):
        s = self.with_cards([card(), card(id="def456def456", title="Other")])
        html = html_of(s, hidden=["abc123abc123"])
        self.assertEqual(html.count('class="advice-card"'), 1)
        self.assertIn("Show 1 hidden card", html)
        shown = html_of(s, hidden=["abc123abc123"], show=True)
        self.assertEqual(shown.count('class="advice-card"'), 2)
        self.assertIn(">Unhide<", shown)

    def test_nothing_to_fix(self):
        self.assertIn("Nothing to fix in this run.", html_of(self.with_cards([])))

    def test_replay_and_older_payloads(self):
        self.assertIn("Leave replay", html_of(self.with_cards([], replay_at=5)))
        s = self.with_cards([])
        del s["advice"]
        self.assertIn("older Cuelight", html_of(s))

    def test_the_list_price_note(self):
        s = self.with_cards([], cost={"enabled": True, "total": 1.0, "currency": "USD", "source": "list",
                                      "as_of": "2026-10-12"})
        self.assertIn("Dollar figures: list price, as of 2026-10-12.", html_of(s))

    def test_badge_counts_visible_cards_and_flags_failures(self):
        s = self.with_cards([card(severity="failure"), card(id="def456def456")])
        body = (STORAGE + "localStorage.setItem(ADVICE_HIDDEN_KEY, JSON.stringify(['def456def456']));\n"
                "box.cls = {}; box.classList = {toggle(c, on) { box.cls[c] = on; }};\n"
                "renderAdviceBadge(%s); console.log(JSON.stringify([box.hidden, box.textContent, box.cls]));"
                % json.dumps(s))
        hidden, text, cls = ui.run_js(FNS, CONSTS, body)
        self.assertEqual((hidden, text, cls), (False, "1", {"badge-fail": True}))

    def badge_of(self, cards):
        body = (STORAGE + "box.cls = {}; box.classList = {toggle(c, on) { box.cls[c] = on; }};\n"
                "renderAdviceBadge(%s); console.log(JSON.stringify([box.hidden, box.textContent, box.cls]));"
                % json.dumps(self.with_cards(cards)))
        return ui.run_js(FNS, CONSTS, body)

    def test_badge_counts_what_the_verdict_counts(self):
        info = card(id="aaa111aaa111", severity="info", fix={"where": "none", "text": "", "snippet": ""})
        # An information card says what happened and has nothing to change: it is not counted.
        self.assertEqual(self.badge_of([info, card()]), [False, "1", {"badge-fail": False}])
        self.assertEqual(self.badge_of([info])[:2], [True, "0"])
        # A failure is counted even when there is nothing to change; any visible failure makes the badge red.
        failure = card(id="bbb222bbb222", severity="failure", fix={"where": "none", "text": "", "snippet": ""})
        self.assertEqual(self.badge_of([info, failure]), [False, "1", {"badge-fail": True}])


@unittest.skipIf(ui.NODE is None, "node is not on PATH")
class TestUsualUi(unittest.TestCase):
    USUAL = {"state": "ready", "error": "", "sessions": 12, "agents": 60, "source": "list", "as_of": "2026-10-12",
             "currency": "USD", "groups": [
                 {"type": "general-purpose", "model": "sonnet-5", "scope": "project", "n": 54, "enough": True,
                  "typical_s": 180, "slow_s": 540, "typical_tokens": 76000, "slow_tokens": 150000,
                  "typical_cost": 0.31, "slow_cost": 0.9, "fail_rate": 0.07, "unchecked_rate": 0.2},
                 {"type": "Explore", "model": "haiku-4-5", "scope": "none", "n": 2, "enough": False}]}

    def usual_html(self, usual, offline=False):
        body = "state.offline = %s; console.log(JSON.stringify(adviceUsualHtml(%s)));" % (
            "true" if offline else "false", json.dumps(usual))
        return ui.run_js(FNS, CONSTS, body)

    def test_the_table_and_the_not_enough_line(self):
        html = self.usual_html(self.USUAL)
        self.assertIn("<td>general-purpose</td><td>sonnet-5</td>", html)
        self.assertIn("this project", html)
        self.assertIn("Explore on haiku-4-5 (2)", html)
        self.assertIn("list price, as of 2026-10-12", html)

    def test_states(self):
        self.assertIn("Learning from your past runs", self.usual_html({"state": "scanning", "groups": []}))
        self.assertIn("No finished agents", self.usual_html({"state": "ready", "groups": [], "sessions": 0}))
        self.assertEqual(self.usual_html(self.USUAL, offline=True), "")

    def test_usual_text_for_the_agent_panel(self):
        u = self.USUAL["groups"][0]
        text = ui.run_js(FNS, CONSTS, "console.log(JSON.stringify(usualText(%s, 540)));" % json.dumps(u))
        self.assertIn("3m 00s typical", text)
        self.assertIn("This one: 9m 00s, 3.0x", text)
        few = ui.run_js(FNS, CONSTS, "console.log(JSON.stringify(usualText(%s, 10)));" % json.dumps(self.USUAL["groups"][1]))
        self.assertEqual(few, "not enough past runs yet (2)")

    def health(self, run_):
        """renderHealth in Node: the items it lists, and what clicking each opens."""
        body = """
const opened = [];
function openDrawer(id) { opened.push("drawer:" + id); }
function openCard(name) { opened.push("card:" + name); }
function loopText(loop) { return "a loop"; }
global.document = {createElement: () => ({children: [], attrs: {}, textContent: "",
  setAttribute(k, v) { this.attrs[k] = v; }, appendChild(c) { this.children.push(c); }})};
box.appendChild = (c) => { box.list = c; };
renderHealth(%s);
const items = box.list ? box.list.children : [];
items.forEach((i) => i.onclick());
console.log(JSON.stringify({items: items.map((i) => [i.textContent, i.attrs["data-kind"]]), opened: opened}));
""" % json.dumps(run_)
        return ui.run_js(("esc", "fmtDuration", "fmtCount", "fmtPct", "callText", "renderHealth"), [], body)

    def test_the_health_box_names_a_slow_running_agent(self):
        slow = [{"agent_id": "a1", "elapsed_s": 460, "typical_s": 192, "slow_s": 270, "n": 24,
                 "kind": "general-purpose on sonnet-5"}]
        run_ = {"agents": [{"agent_id": "a1", "description": "Build the UI", "status": "running"}],
                "insights": None, "advice": {"slow": slow}}
        out = self.health(run_)
        self.assertEqual(out["items"], [["SLOWER THAN USUAL — Build the UI: 7m 40s, usually 3m 12s", "slow"]])
        self.assertEqual(out["opened"], ["drawer:a1"])
        # A replay shows an earlier moment: the list is for now, so it stays out.
        self.assertEqual(self.health(dict(run_, replay_at=100))["items"], [])

    def test_an_error_keeps_the_numbers_it_has(self):
        failed = dict(self.USUAL, state="error", error="past runs unreadable: disk full", groups=self.USUAL["groups"][:1])
        html = self.usual_html(failed)
        self.assertIn("past runs unreadable: disk full", html)
        self.assertIn("<td>general-purpose</td><td>sonnet-5</td>", html)
        self.assertLess(html.index("unreadable"), html.index("<table>"))
        alone = self.usual_html({"state": "error", "error": "<b>x</b>", "groups": []})
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", alone)
        self.assertNotIn("<table>", alone)

    def test_numbers_that_arrive_after_a_session_switch_are_dropped(self):
        js = ui.read("advice.js")
        start = js.index("async function loadUsual(")
        source = js[start:js.index("\n}\n", start) + 3]
        body = source + """
const drawn = [], asked = [];
let release;
function api(path) { asked.push(state.sessionId); return new Promise((resolve) => { release = resolve; }); }
function renderAdvice(run) { drawn.push(state.usual.sessions); }
state.view = "advice"; state.run = {}; state.sessionId = "s1";
(async () => {
  const first = loadUsual();
  state.sessionId = "s2"; state.usual = undefined;          // the user switched while it was on its way
  release({state: "ready", sessions: 1, groups: []});
  await first;
  const dropped = [state.usual === undefined, drawn.length, state.usualLoading];
  release({state: "ready", sessions: 2, groups: []});       // the fresh load, for the current session
  await new Promise((resolve) => setTimeout(resolve, 0));
  console.log(JSON.stringify({dropped, asked, drawn, kept: state.usual.sessions, loading: state.usualLoading}));
})();
"""
        out = ui.run_js((), [], body)
        self.assertEqual(out["dropped"], [True, 0, True])      # nothing kept or drawn; a new load is under way
        self.assertEqual(out["asked"], ["s1", "s2"])
        self.assertEqual((out["drawn"], out["kept"], out["loading"]), ([2], 2, False))
