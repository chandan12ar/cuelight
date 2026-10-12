"""The Agents tab: every agent on one row, what it was asked beside what came back."""

import json
import re
import unittest

from tests import test_insights_ui as ui

FNS = ("esc", "fileName", "checkText", "fmtDuration", "fmtCount", "fmtMoney", "fmtModelShort", "statusVar", "insMetric", "cardKey", "insCard",
       "insEmpty", "plural", "liveSpan", "waitNow", "agentMatchesFilter", "agentFlags", "agentSpend",
       "agentSortValue", "agentsRows", "agentsHtml", "priceNote", "renderAgents")

PRE = "state.filterStatuses = new Set(); state.filterText = '';\n"


def run_js(body, summary):
    return ui.run_js(FNS, ["AGENT_SORTS"], "const RUN = %s;\n" % json.dumps(summary) + PRE + body)


def html_of(summary, setup=""):
    return run_js(setup + "renderAgents(RUN); console.log(JSON.stringify(box.innerHTML));", summary)


def rows_of(summary, setup=""):
    return run_js(setup + "console.log(JSON.stringify(agentsRows(RUN).map((r) => "
                  "[r.agent.description, r.flags.map((f) => f.text)])));", summary)


def row_html(html, description):
    """The <tr> whose agent cell names this description."""
    for row in re.findall(r"<tr data-agent=.*?</tr>", html, re.S):
        if ">" + description + "<" in row:
            return row
    raise AssertionError("no row for " + description)


@unittest.skipIf(ui.NODE is None, "node is not on PATH")
class TestAgentsTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = ui.demo_run(events=True)
        cls.html = html_of(cls.summary)

    def test_one_row_per_agent_in_launch_order(self):
        rows = rows_of(self.summary)
        self.assertEqual(len(rows), len(self.summary["agents"]))
        self.assertEqual(len(re.findall(r"<tr data-agent=", self.html)), len(self.summary["agents"]))
        starts = sorted(self.summary["agents"], key=lambda a: (a["started_at"] or 0))
        self.assertEqual(rows[0][0], starts[0]["description"])

    def test_the_columns(self):
        heads = re.findall(r"<th[^>]*>(?:<button[^>]*>)?([^<]+)", self.html)
        self.assertEqual(heads, ["Agent", "Asked", "Expected", "Came back", "Status", "Checked", "Time",
                                 "Cost", "Errors"])

    def test_expected_sits_beside_what_came_back(self):
        row = row_html(self.html, "Write the database migration")
        self.assertIn("A reversible migration for payment intents.", row)
        self.assertIn("Migration 0042 adds payment_intents and is reversible.", row)

    def test_flags_say_what_needs_a_look(self):
        flags = dict(rows_of(self.summary))
        self.assertEqual(flags["Write the unit tests"], ["failed", "checks failing"])
        self.assertEqual(flags["Build the payment adapter"], ["unchecked"])
        self.assertEqual(flags["Review the checkout for security issues"], ["stalled", "retrying"])
        self.assertEqual(flags["Run the end-to-end suite"], ["possible loop"])
        self.assertEqual(flags["Update the developer docs"], ["waiting on you"])
        self.assertEqual(flags["Implement the cart service"], [])
        self.assertEqual(flags["Build the checkout UI"], [])      # unchecked, but still running
        self.assertIn('class="flag bad">checks failing<', row_html(self.html, "Write the unit tests"))

    def test_a_finished_agent_with_no_report_is_flagged(self):
        s = json.loads(json.dumps(self.summary))
        quiet = next(a for a in s["agents"] if a["description"] == "Write the database migration")
        quiet["result_gist"] = ""
        self.assertEqual(dict(rows_of(s))["Write the database migration"], ["no report"])
        self.assertIn("no report", row_html(html_of(s), "Write the database migration"))

    def test_a_running_agent_says_so_and_its_time_counts_up(self):
        row = row_html(self.html, "Build the checkout UI")
        self.assertIn("still running", row)
        self.assertIn('class="wait-live"', row)

    def test_sorting_by_cost_puts_the_most_expensive_first_and_flips(self):
        by_cost = rows_of(self.summary, "state.agentsSort = {key: 'cost', dir: -1};\n")
        top = max(self.summary["agents"], key=lambda a: a["cost"])
        self.assertEqual(by_cost[0][0], top["description"])
        flipped = rows_of(self.summary, "state.agentsSort = {key: 'cost', dir: 1};\n")
        self.assertEqual(flipped[-1][0], top["description"])
        html = html_of(self.summary, "state.agentsSort = {key: 'cost', dir: -1};\n")
        self.assertIn('aria-sort="descending"><button type="button" data-sort="cost"', html)
        self.assertEqual(html.count('aria-sort="none"'), 5)
        self.assertIn('aria-sort="ascending"><button type="button" data-sort="start">Agent<', self.html)

    def test_sorting_by_status_puts_trouble_first(self):
        rows = rows_of(self.summary, "state.agentsSort = {key: 'status', dir: 1};\n")
        self.assertEqual(rows[0][0], "Write the unit tests")            # failed
        self.assertEqual(rows[-1][1], [])                                 # a completed one, nothing to see

    def test_only_the_ones_that_need_a_look(self):
        rows = rows_of(self.summary, "state.agentsFlagged = true;\n")
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(flags for _, flags in rows))
        self.assertIn('aria-pressed="true"', html_of(self.summary, "state.agentsFlagged = true;\n"))
        self.assertIn(">5</strong><span>need a look</span>", self.html)

    def test_the_filter_bar_applies(self):
        # The same rule as every other view: description, id, type, model or objective.
        rows = rows_of(self.summary, "state.filterText = 'payment';\n")
        want = [a["description"] for a in self.summary["agents"]
                if "payment" in " ".join([a["description"], a["agent_id"], a["agent_type"], a["model"],
                                          a["objective"]]).lower()]
        self.assertEqual(sorted(r[0] for r in rows), sorted(want))
        self.assertLess(len(rows), len(self.summary["agents"]))

    def test_without_prices_it_shows_tokens(self):
        s = json.loads(json.dumps(self.summary))
        for a in s["agents"]:
            a["cost"] = None
        s["cost"] = {"enabled": False}
        html = html_of(s)
        self.assertIn('data-sort="cost">Tokens<', html)
        self.assertNotIn("$", html)

    def test_list_prices_are_labelled_on_the_cost_column(self):
        s = json.loads(json.dumps(self.summary))
        s["cost"].update(source="list", as_of="2026-10-12")
        self.assertIn('data-sort="cost" title="list price, as of 2026-10-12">Cost (list price)<', html_of(s))

    def test_your_own_prices_leave_the_cost_column_alone(self):
        s = json.loads(json.dumps(self.summary))
        s["cost"].update(source="yours", as_of="")
        html = html_of(s)
        self.assertIn('data-sort="cost">Cost<', html)
        self.assertNotIn("list price", html)

    def test_an_unstated_deliverable_says_so(self):
        s = json.loads(json.dumps(self.summary))
        s["agents"][0]["expected_output"] = ""
        self.assertIn("not stated", row_html(html_of(s), s["agents"][0]["description"]))

    def test_text_is_escaped(self):
        s = json.loads(json.dumps(self.summary))
        a = s["agents"][0]
        a["description"] = '<img src=x onerror="alert(1)">'
        a["expected_output"] = '"><script>alert(2)</script>'
        a["result_gist"] = "<b>bold</b>"
        html = html_of(s)
        self.assertNotIn("<img", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("<b>bold", html)

    def test_numbers_are_real(self):
        for bad in ("NaN", "undefined", "null", "Infinity"):
            self.assertNotIn(bad, self.html)

    # A refresh keeping keyboard focus on a row or heading: tests/test_focus_ui.py.

    def test_no_agents_matching_says_so(self):
        html = html_of(self.summary, "state.filterText = 'nothing matches this';\n")
        self.assertIn("No agent matches", html)
        self.assertNotIn("<tr data-agent=", html)


class TestAgentsTabWiring(unittest.TestCase):
    def test_a_tab_of_its_own_after_graph_on_key_3(self):
        js, html = ui.read("app.js"), ui.read("index.html")
        self.assertLess(html.index('data-view="graph"'), html.index('data-view="agents"'))
        self.assertLess(html.index('data-view="agents"'), html.index('data-view="insights"'))
        self.assertIn('id="view-agents"', html)
        self.assertIn('["agents", "Agents", "3"]', js)
        self.assertIn('else if (state.view === "agents") renderAgents(state.run);', js)
        agent_views = js[js.index("const AGENT_VIEWS"):js.index(";", js.index("const AGENT_VIEWS"))]
        self.assertIn('"agents"', agent_views)          # "No subagents yet" covers it too

    def test_static_reports_keep_it(self):
        from orchestra import report
        i = report._SHELL.index('data-view="agents"')
        self.assertNotIn("hidden", report._SHELL[i - 60:i])


if __name__ == "__main__":
    unittest.main()
