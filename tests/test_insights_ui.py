"""The Insights tab, the transport clock and deep links, executed under node."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from orchestra import demo
from orchestra.build import RunBuilder
from orchestra.pricing import PriceSource

NODE = shutil.which("node")
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "orchestra", "static")

SETUP = """
// esc() in app.js escapes through the DOM; this stub does what a real div does.
global.document = {createElement: () => {
  let text = "";
  return {set textContent(v) { text = String(v); },
          get innerHTML() { return text.replace(/&/g, "&amp;").replace(/</g, "&lt;")
                                       .replace(/>/g, "&gt;"); }};
}};
const box = {innerHTML: "", clientWidth: 900, querySelectorAll: () => []};
const $ = (id) => box;
const state = {offline: false};
"""


def read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def fn(js, name):
    start = js.index("function {}(".format(name))
    return js[start:js.index("\n}\n", start) + 3]


def const(js, name):
    start = js.index("const {} =".format(name))
    return js[start:js.index(";\n", start) + 2]


# Every redrawn view keeps keyboard focus through these (app.js), so every run has them.
FOCUS_FNS = ("focusMatches", "noteFocus", "restoreFocus")


def run_js(names, consts, body):
    js = read("app.js") + "\n" + read("tabs.js") + "\n" + read("advice.js")   # more tabs live in tabs.js and advice.js
    names = tuple(names) + tuple(n for n in FOCUS_FNS if n not in names)
    consts = tuple(consts) + (() if "FOCUS_NAMES" in consts else ("FOCUS_NAMES",))
    prelude = "\n".join([SETUP] + [const(js, c) for c in consts] + [fn(js, n) for n in names])
    path = os.path.join(tempfile.mkdtemp(), "i.js")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(prelude + "\n" + body)
    proc = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=30)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr[-1500:])
    return json.loads(proc.stdout)


INSIGHT_FNS = ("esc", "fmtDuration", "fmtCount", "fmtPct", "fmtMoney", "fmtModelShort", "statusVar",
               "insMetric", "insCard", "insEmpty", "insRank", "insStepChart", "insParallelism",
               "insCritical", "insTools", "insTokens", "insFiles", "insSlowest",
               "fmtClock", "liveSpan", "waitNow", "insWaits", "fileName", "checkText", "insChecks", "insChanges", "safeLink", "insOutcomes", "fileLabel", "insContext", "wasteCause", "insWaste",
               "fmtWindow", "compactionCause", "insPressureChart", "insPressure",
               "apiKind", "callText", "apiLost", "insErrors",
               "cardKey", "foldedCards", "saveFolded", "plural", "insHeadlines", "insGlance",
               "transportSeconds", "fmtTimecode", "renderInsights")


def demo_run(mutate=None, events=False):
    root = tempfile.mkdtemp()
    now = time.time()
    paths, _ = demo.build_demo(root, now=now)
    prices = os.path.join(root, "prices.json")
    demo.write_prices(prices)
    spool = None
    if events:
        from orchestra.events import EventSpool
        spool = EventSpool(os.path.join(tempfile.mkdtemp(), "events"))
        demo.write_events(spool, paths.session_id, now)
    built = RunBuilder(paths, now_fn=lambda: now, prices=PriceSource(prices), spool=spool).refresh()
    summary = built.to_summary_dict()
    if mutate:
        mutate(summary)
    return summary


def render_insights(summary):
    return run_js(INSIGHT_FNS, ["BUCKET_VARS"],
                  "renderInsights(%s); console.log(JSON.stringify(box.innerHTML));" % json.dumps(summary))


@unittest.skipIf(NODE is None, "node is not on PATH")
class TestInsightsTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = render_insights(demo_run())

    def test_every_card_is_drawn(self):
        for title in ("Parallelism", "Critical path", "Tool use", "Tokens and cache",
                      "Longest-running agents", "Files"):
            self.assertIn(">" + title + "</button></h3>", self.html)

    def test_numbers_are_real_not_nan_or_undefined(self):
        for bad in ("NaN", "undefined", "null", "Infinity"):
            self.assertNotIn(bad, self.html)

    def test_critical_path_names_the_chain_and_links_to_agents(self):
        self.assertIn("Compare payment provider APIs", self.html)
        self.assertIn("Design the checkout v2 architecture", self.html)
        self.assertIn('class="chain-step" data-agent="', self.html)

    def test_chart_is_labelled_and_axis_uses_whole_agents(self):
        self.assertIn('role="img" aria-label="Agents running over time. Peak', self.html)
        ticks = [int(t) for t in __import__("re").findall(
            r'<text class="ins-axis" x="20" y="[\d.]+" text-anchor="end">(\d+)</text>', self.html)]
        self.assertEqual(ticks, sorted(set(ticks)))
        self.assertTrue(all(isinstance(t, int) for t in ticks))
        self.assertEqual(ticks[0], 0)

    def test_cost_per_model_appears_when_prices_exist(self):
        self.assertIn("$", self.html.split("By model")[1])

    def test_spend_is_a_tab_of_its_own_now(self):
        # The Spend card moved to the Spend tab (tests/test_spend_ui.py).
        self.assertNotIn('data-card="spend"', self.html)


def card(html, title):
    """One card's markup after its title (Insights titles are fold buttons; elsewhere plain h3)."""
    for head in (">" + title + "</button></h3>", "<h3>" + title + "</h3>"):
        if head in html:
            return html.split(head)[1].split("</section>")[0]
    raise AssertionError("no card titled " + title)


@unittest.skipIf(NODE is None, "node is not on PATH")
class TestWaitsCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = render_insights(demo_run(events=True))
        cls.waits = card(cls.html, "Waiting on you")

    def test_headline_numbers_and_the_open_wait(self):
        for text in ("of your time with an agent held up", "agent time lost, every wait added",
                     "5</strong><span>waits", "1 agent is waiting on you now.", "longest: "):
            self.assertIn(text, self.waits)
        for bad in ("NaN", "undefined", "null", "Infinity"):
            self.assertNotIn(bad, self.waits)

    def test_open_durations_count_up_in_place(self):
        # Your time and the open prompt in the list; both carry what tickWaits needs.
        self.assertEqual(self.waits.count('class="wait-live" data-wait-base="'), 2)
        self.assertIn('<li class="wait-open">', self.waits)

    def test_rows_name_agents_and_link_to_them(self):
        self.assertIn("Update the developer docs", self.waits)
        self.assertIn("Main session", self.waits)
        self.assertIn("Claude needs your permission to use Bash", self.waits)
        self.assertIn('data-agent="a', self.waits)

    def test_without_hooks_it_says_where_prompts_come_from(self):
        self.assertIn("none have reported for this session", card(self.render_plain(), "Waiting on you"))

    def test_with_hooks_but_no_prompts_it_says_none(self):
        def quiet(summary):
            summary["live"] = {"has_events": True}
        self.assertIn("No agent has waited", card(render_insights(demo_run(quiet)), "Waiting on you"))

    def test_unanswered_prompts_are_explained(self):
        def ended(summary):
            w = summary["insights"]["waits"]
            w["unanswered"], w["open"] = 2, 0
            w["recent"][0]["state"] = "unanswered"
        html = card(render_insights(demo_run(ended, events=True)), "Waiting on you")
        self.assertIn("2 prompts were still up when the session went quiet", html)
        self.assertIn("never answered", html)

    def test_hostile_labels_and_messages_are_text(self):
        evil = '"><img src=x onerror=alert(1)>'

        def hit(summary):
            w = summary["insights"]["waits"]
            for row in w["recent"] + w["by_agent"]:
                row["label"] = evil
            for row in w["recent"]:
                row["message"] = evil
            w["longest"]["label"] = evil
        html = card(render_insights(demo_run(hit, events=True)), "Waiting on you")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)

    @staticmethod
    def render_plain():
        return render_insights(demo_run())


@unittest.skipIf(NODE is None, "node is not on PATH")
class TestInsightsSafety(unittest.TestCase):
    def test_hostile_agent_names_cannot_inject_markup(self):
        evil = '"><img src=x onerror=alert(1)>'

        def hit(summary):
            for a in summary["insights"]["critical_path"]["chain"]:
                a["description"] = evil
            for a in summary["insights"]["tokens"]["top_agents"]:
                a["description"] = evil
            summary["insights"]["slowest"][0]["description"] = evil
        html = render_insights(demo_run(hit))
        self.assertNotIn("<img", html)
        self.assertNotIn("onerror=alert(1)>", html.replace("&gt;", ""))   # only the escaped form
        self.assertIn("&quot;&gt;&lt;img src=x onerror=alert(1)&gt;", html)   # shown as text
        # and no attribute (title="...") was broken out of
        self.assertNotIn('title=""', html)

    def test_no_insights_during_replay_says_why(self):
        html = render_insights({"insights": None, "replay_at": 5})
        self.assertIn("Leave replay", html)

    def test_no_insights_at_all_says_so(self):
        self.assertIn("not available", render_insights({"insights": None}))


@unittest.skipIf(NODE is None, "node is not on PATH")
class TestTransportAndLinks(unittest.TestCase):
    def test_timecode(self):
        out = run_js(("fmtTimecode",), [], "console.log(JSON.stringify([0, 59, 61, 3599, 3600, 86399, "
                     "-5, null, NaN, 125.9].map(fmtTimecode)));")
        self.assertEqual(out, ["00:00:00", "00:00:59", "00:01:01", "00:59:59", "01:00:00",
                               "23:59:59", "00:00:00", "--:--:--", "--:--:--", "00:02:05"])

    def test_hash_parsing_handles_new_and_old_forms_and_garbage(self):
        cases = ["", "#", "#view=insights", "#agent=a1", "#view=graph&agent=a%20b",
                 "#view=%E0%A4%A", "#nonsense", "#view=&agent="]
        out = run_js(("parseHash",), [],
                     "const cases = %s; console.log(JSON.stringify(cases.map((h) => {"
                     " global.location = {hash: h}; return parseHash(); })));" % json.dumps(cases))
        self.assertEqual(out, [
            {"view": "", "agent": ""}, {"view": "", "agent": ""},
            {"view": "insights", "agent": ""}, {"view": "", "agent": "a1"},
            {"view": "graph", "agent": "a b"}, {"view": "", "agent": ""},
            {"view": "", "agent": ""}, {"view": "", "agent": ""}])


if __name__ == "__main__":
    unittest.main()
