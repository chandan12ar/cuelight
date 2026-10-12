"""The Advice tab: verdict, fix-it cards, hiding, copying and the badge, executed under node."""

import json
import unittest

from tests import test_insights_ui as ui

FNS = ("esc", "fmtDuration", "fmtCount", "fmtMoney", "plural", "priceNote", "adviceHidden", "setAdviceHidden",
       "adviceImpact", "adviceAgents", "adviceCardHtml", "adviceHtml", "renderAdviceBadge")
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
