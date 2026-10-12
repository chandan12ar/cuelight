"""Cost in the header, drawer-adjacent formatting, and budget alerts (node)."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from orchestra.model import Agent, Round, Run
from orchestra.report import render_report
from tests.test_report_renders import HARNESS

NODE = shutil.which("node")
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "orchestra", "static")

FOOTER = r"""
if (typeof onReady === "function") onReady();
realSetTimeout(() => {
  const walk = (el) => [el._text, ...(el.children || []).flatMap(walk)];
  console.log(JSON.stringify({
    failure: failure,
    parts: ids["totals"].children.map((c) => ({
      cls: c.className || "", title: c.title || "",
      text: walk(c).join("") + (c._html || "").replace(/<[^>]+>/g, "")})),
  }));
}, 200);
"""


def read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def fn(js, name):
    start = js.index("function {}(".format(name))
    return js[start:js.index("\n}\n", start) + 3]


def eval_js(expression, *names):
    js = read("app.js")
    program = "\n".join(fn(js, n) for n in names) + \
        "\nconsole.log(JSON.stringify(" + expression + "));"
    path = os.path.join(tempfile.mkdtemp(), "c.js")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(program)
    proc = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=30)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr[-1500:])
    return json.loads(proc.stdout)


@unittest.skipIf(NODE is None, "node is not on PATH")
class TestFormatting(unittest.TestCase):
    def money(self, amount, currency="USD"):
        return eval_js("fmtMoney(%s, %s)" % (json.dumps(amount), json.dumps(currency)),
                       "fmtMoney")

    def test_usd_uses_a_dollar_sign_and_two_decimals(self):
        self.assertEqual(self.money(1.2345), "$1.23")

    def test_large_amounts_drop_the_cents(self):
        self.assertEqual(self.money(1234.5), "$1235")

    def test_a_tiny_amount_never_reads_as_free(self):
        self.assertEqual(self.money(0.0004), "<$0.01")

    def test_exactly_zero_is_zero(self):
        self.assertEqual(self.money(0), "$0.00")

    def test_other_currencies_are_named(self):
        self.assertEqual(self.money(5, "EUR"), "EUR 5.00")

    def test_missing_is_a_dash_not_zero(self):
        self.assertEqual(self.money(None), "—")

    def test_cost_text_shows_the_budget_when_there_is_one(self):
        out = eval_js('[costText({total: 1.5, currency: "USD", budget: {limit: 5}}), '
                      'costText({total: 1.5, currency: "USD", budget: null})]',
                      "fmtMoney", "costText")
        self.assertEqual(out, ["$1.50 / $5.00", "$1.50"])


def run_with_cost(cost, orchestrator=None):
    run = Run(session_id="s1", agents=[Agent(agent_id="a1", status="completed",
                                             rounds=[Round(started_at=1.0, ended_at=2.0)])],
              cost=cost, orchestrator=orchestrator)
    return run


@unittest.skipIf(NODE is None, "node is not on PATH")
class TestHeader(unittest.TestCase):
    def render(self, run):
        html = render_report(run, {})
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        ids = sorted(set(re.findall(r'id="([^"]+)"', html)))
        program = (HARNESS.replace("__IDS__", json.dumps(ids)) + "\n" +
                   "\n".join(scripts) + "\n" + FOOTER)
        path = os.path.join(tempfile.mkdtemp(), "h.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(program)
        proc = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertIsNone(out["failure"], out["failure"])
        return out["parts"]

    def cost(self, **over):
        base = {"enabled": True, "currency": "USD", "total": 2.0, "agents": 1.5,
                "orchestrator": 0.5, "partial": False, "unpriced_models": [],
                "budget": None}
        base.update(over)
        return base

    def texts(self, parts):
        return " | ".join(p["text"] for p in parts)

    def test_no_cost_block_shows_nothing_extra(self):
        self.assertNotIn("cost", self.texts(self.render(run_with_cost(None))))

    def test_disabled_without_an_error_is_silent(self):
        self.assertNotIn("cost", self.texts(self.render(run_with_cost({"enabled": False}))))

    def test_a_broken_price_file_is_shown_not_hidden(self):
        parts = self.render(run_with_cost({"enabled": False, "error": "prices file unusable: x"}))
        self.assertIn("prices file unusable", self.texts(parts))

    def test_enabled_shows_the_total(self):
        self.assertIn("$2.00 cost", self.texts(self.render(run_with_cost(self.cost()))))

    def test_list_prices_are_labelled_in_the_header(self):
        parts = self.render(run_with_cost(self.cost(source="list", as_of="2026-10-12")))
        cost = [p for p in parts if "cost" in p["text"]][0]
        self.assertIn("$2.00 cost (list price)", cost["text"])
        self.assertIn("list price, as of 2026-10-12", cost["title"])
        self.assertIn("agents $1.50 + orchestrator $0.50", cost["title"])

    def test_your_own_prices_are_not_labelled_in_the_header(self):
        parts = self.render(run_with_cost(self.cost(source="yours", as_of="")))
        cost = [p for p in parts if "cost" in p["text"]][0]
        self.assertNotIn("list price", cost["text"])
        self.assertNotIn("list price", cost["title"])

    def test_partial_is_labelled_and_names_the_unpriced_model(self):
        parts = self.render(run_with_cost(self.cost(partial=True, unpriced_models=["mystery-9"])))
        cost = [p for p in parts if "cost" in p["text"]][0]
        self.assertIn("(partial)", cost["text"])
        self.assertIn("mystery-9", cost["title"])

    def test_budget_is_shown_and_states_are_styled(self):
        for state, cls in (("ok", ""), ("warn", "cost-warn"), ("exceeded", "cost-exceeded")):
            budget = {"limit": 2.5, "spent": 2.0, "ratio": 0.8, "state": state}
            parts = self.render(run_with_cost(self.cost(budget=budget)))
            cost = [p for p in parts if "cost" in p["text"]][0]
            self.assertIn("$2.00 / $2.50", cost["text"])
            self.assertEqual(cost["cls"], cls)
            self.assertIn("80% of budget", cost["title"])

    def test_the_orchestrators_tokens_are_shown_separately(self):
        run = run_with_cost(None, {"tokens": {"output": 5000}, "model": "m", "cost": None})
        self.assertIn("orchestrator", self.texts(self.render(run)))


class TestBudgetAlerts(unittest.TestCase):
    def sounds(self, states):
        if NODE is None:
            self.skipTest("node is not on PATH")
        js = read("app.js")
        start = js.index("const SOUND_PRIORITY")
        prelude = js[start:js.index(";\n", js.index("const SOUND_NOTES")) + 2] + fn(js, "computeSounds")
        runs = ", ".join(
            '{live: null, agents: [], totals: {running: 0, waiting: 0}, '
            'cost: {budget: %s}}' % ('{state: "%s"}' % s if s else "null") for s in states)
        program = prelude + ("\nconst memo = {seeded: false, attKey: '', failed: new Set(), running: 0};"
                             "\nconsole.log(JSON.stringify([%s].map((r) => computeSounds(r, memo))));" % runs)
        path = os.path.join(tempfile.mkdtemp(), "b.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(program)
        proc = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        return json.loads(proc.stdout)

    def test_crossing_the_budget_sounds_once(self):
        self.assertEqual(self.sounds(["ok", "warn", "exceeded", "exceeded"]),
                         [[], [], ["alert"], []])

    def test_already_over_budget_at_load_is_silent(self):
        self.assertEqual(self.sounds(["exceeded", "exceeded"]), [[], []])


class TestWiring(unittest.TestCase):
    def test_notifications_cover_budget_transitions(self):
        js = read("app.js")
        self.assertIn('notify(budget === "exceeded"', js)
        self.assertIn("state.knownBudget", js)

    def test_switching_sessions_reseeds_the_budget_baseline(self):
        js = read("app.js")
        body = js[js.index("function switchSession("):]
        self.assertIn('state.knownBudget = ""', body[:body.index("\n}\n")])

    def test_cost_text_reaches_the_dom_via_textcontent_not_innerhtml(self):
        js = read("app.js")
        body = js[js.index("function renderCostPart("):]
        self.assertNotIn("innerHTML", body[:body.index("\n}\n")])


if __name__ == "__main__":
    unittest.main()
