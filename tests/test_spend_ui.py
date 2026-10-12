"""The Spend tab: the running total over the run, who spent it, and the budget's crossings."""

import json
import re
import unittest

from tests import test_insights_ui as ui

FNS = ("esc", "fmtDuration", "fmtCount", "fmtMoney", "fmtPct", "fmtClock", "fmtModelShort", "insMetric", "cardKey",
       "insCard", "insEmpty", "insRank", "plural", "spendFmt", "spendSeries", "spendReadout", "spendChart", "spendHtml",
       "priceNote", "renderSpend")


def run_js(body, summary):
    return ui.run_js(FNS, ["SPEND_COLORS"], "const RUN = %s;\n" % json.dumps(summary) + body)


def html_of(summary, setup=""):
    return run_js(setup + "renderSpend(RUN); console.log(JSON.stringify(box.innerHTML));", summary)


def with_budget(summary):
    s = json.loads(json.dumps(summary))
    sp = s["insights"]["spend"]
    t = sp["t"]
    sp["budget"] = {"limit": 5.0, "warn_at": 4.0, "warn_t": t[60], "limit_t": t[100]}
    s["cost"]["budget"] = {"limit": 5.0, "spent": sp["total"], "ratio": sp["total"] / 5.0, "state": "exceeded"}
    return s


@unittest.skipIf(ui.NODE is None, "node is not on PATH")
class TestSpendTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = ui.demo_run(events=True)
        cls.html = html_of(cls.summary)

    def test_the_headline_numbers(self):
        sp = self.summary["insights"]["spend"]
        money = lambda v: run_js("console.log(JSON.stringify(fmtMoney(%r, 'USD')));" % v, self.summary)
        self.assertIn(">" + money(sp["total"]) + "</strong><span>spent so far</span>", self.html)
        self.assertIn(">" + money(sp["main_total"]) + "</strong><span>main session</span>", self.html)
        self.assertIn(">" + money(sp["agents_total"]) + "</strong><span>agents</span>", self.html)
        self.assertIn("</strong><span>a minute, last 5 minutes</span>", self.html)

    def test_list_prices_are_labelled(self):
        s = json.loads(json.dumps(self.summary))
        s["cost"].update(source="list", as_of="2026-10-12")
        self.assertIn("spent so far (list price, as of 2026-10-12)", html_of(s))

    def test_the_chart_stacks_main_and_agents(self):
        self.assertIn('role="img"', self.html)
        self.assertEqual(len(re.findall(r'class="spend-band"', self.html)), 2)
        self.assertIn('aria-label="Spend over the run: ', self.html)
        legend = re.findall(r'<li[^>]*><i[^>]*></i>([^<]+)<b>', self.html)
        self.assertEqual(legend, ["Main session", "Agents"])

    def test_the_table_view_lists_every_series_with_its_share(self):
        rows = re.findall(r'<tr><th scope="row">([^<]+)</th><td>([^<]+)</td><td>([^<]+)</td></tr>', self.html)
        self.assertEqual([r[0] for r in rows], ["Main session", "Agents"])
        shares = [int(r[2].rstrip("%")) for r in rows]
        self.assertIn(sum(shares), (99, 100, 101))

    def test_by_model(self):
        html = html_of(self.summary, "state.spendSplit = 'model';\n")
        # By name, "other" last: a model keeps its colour while the others' ranking changes.
        models = sorted((m["model"] for m in self.summary["insights"]["spend"]["models"]),
                        key=lambda m: (m == "other", m))
        self.assertEqual(len(re.findall(r'class="spend-band"', html)), len(models))
        rows = re.findall(r'<tr><th scope="row">([^<]+)</th>', html)
        self.assertEqual(rows, models)
        self.assertIn('aria-pressed="true">By model', html)

    def test_the_most_expensive_five_minutes_and_agents(self):
        self.assertIn("Most expensive five minutes", self.html)
        self.assertIn("Most expensive agents", self.html)
        peak = self.summary["insights"]["spend"]["peak"]
        self.assertIn(peak["who"][0]["label"], self.html)

    def test_budget_line_and_crossings(self):
        html = html_of(with_budget(self.summary))
        self.assertIn('class="spend-budget"', html)
        self.assertIn(">80% of budget</text>", html)
        self.assertIn(">budget</text>", html)
        self.assertIn("Over budget by", html)
        self.assertNotIn('class="spend-budget"', self.html)          # none set, none drawn

    def test_over_budget_says_by_how_much(self):
        html = html_of(with_budget(self.summary))
        self.assertIn("Over budget by", html)
        self.assertIn("background:var(--failed)", html)

    def test_under_budget_forecasts_when_it_runs_out(self):
        s = json.loads(json.dumps(self.summary))
        s["cost"]["budget"] = {"limit": 1000.0, "spent": 1.0, "ratio": 0.001, "state": "ok"}
        html = html_of(s)
        self.assertIn("the budget runs out in", html)
        self.assertIn("background:var(--completed)", html)
        self.assertIn("</strong><span>of the $1000 budget</span>", html)

    def test_without_prices_it_counts_fresh_tokens(self):
        s = json.loads(json.dumps(self.summary))
        sp = s["insights"]["spend"]
        sp["unit"], sp["currency"] = "tokens", None
        s["cost"] = {"enabled": False}
        html = html_of(s)
        self.assertNotIn("$", html)
        self.assertIn("</strong><span>fresh tokens</span>", html)
        self.assertIn("prices.json", html)

    def test_the_readout_for_the_crosshair(self):
        sp = self.summary["insights"]["spend"]
        out = run_js("console.log(JSON.stringify(spendReadout(RUN.insights.spend, 'who', 119)));", self.summary)
        self.assertEqual(out["at"], sp["t"][119])
        self.assertEqual([r["name"] for r in out["rows"]], ["Main session", "Agents"])
        self.assertAlmostEqual(out["total"], sp["total"])

    def test_replay_and_empty(self):
        s = json.loads(json.dumps(self.summary))
        s["replay_at"] = 1
        self.assertIn("Leave replay", html_of(s))
        s = json.loads(json.dumps(self.summary))
        s["insights"]["spend"] = None
        self.assertIn("No API calls recorded", html_of(s))

    def test_names_are_escaped(self):
        s = json.loads(json.dumps(self.summary))
        s["insights"]["spend"]["models"][0]["model"] = "<img src=x onerror=alert(1)>"
        s["insights"]["spend"]["peak"]["who"][0]["label"] = "<script>alert(2)</script>"
        html = html_of(s, "state.spendSplit = 'model';\n")
        self.assertNotIn("<img", html)
        self.assertNotIn("<script", html)

    def test_numbers_are_real(self):
        for html in (self.html, html_of(with_budget(self.summary), "state.spendSplit = 'model';\n")):
            for bad in ("NaN", "undefined", "null", "Infinity"):
                self.assertNotIn(bad, html)


class TestSpendTabWiring(unittest.TestCase):
    def test_a_tab_of_its_own_after_insights_on_key_5(self):
        js, html = ui.read("app.js"), ui.read("index.html")
        self.assertLess(html.index('data-view="insights"'), html.index('data-view="spend"'))
        self.assertLess(html.index('data-view="spend"'), html.index('data-view="prompts"'))
        self.assertIn('id="view-spend"', html)
        self.assertIn('["spend", "Spend", "5"]', js)
        self.assertIn('["history", "History", "0"]', js)
        self.assertIn('"1234567890".indexOf(key)', js)
        self.assertIn('else if (state.view === "spend") renderSpend(state.run);', js)

    def test_insights_has_no_spend_card_and_its_chip_opens_the_tab(self):
        if ui.NODE is None:
            self.skipTest("node is not on PATH")
        html = ui.render_insights(ui.demo_run())
        self.assertNotIn('data-card="spend"', html)
        self.assertIn('data-view="spend"', html)

    def test_static_reports_keep_it(self):
        from orchestra import report
        i = report._SHELL.index('data-view="spend"')
        self.assertNotIn("hidden", report._SHELL[i - 60:i])


if __name__ == "__main__":
    unittest.main()
