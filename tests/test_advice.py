"""Fix-it cards and the run verdict (orchestra.advice), on hand-built runs."""

import unittest
from collections import OrderedDict

from orchestra import advice
from orchestra import constants as C
from orchestra.errors import ErrorLog
from orchestra.model import Agent, Round, Run, ToolCall
from orchestra.waste import WasteLog


def agent(aid="a1", status=C.COMPLETED, start=0.0, end=600.0, desc=None, **kw):
    kw.setdefault("model", "claude-sonnet-5")
    kw.setdefault("agent_type", "general-purpose")
    return Agent(agent_id=aid, description=desc or "Agent " + aid, status=status,
                 rounds=[Round(started_at=start, ended_at=end, status=status)], **kw)


def calls(*specs):
    return [ToolCall(name=n, target=t, timestamp=at, ok=ok) for n, t, at, ok in specs]


def errlog(api=(), calls=(), timeouts=()):
    log = ErrorLog()
    log.api = [dict(e) for e in api]
    log.calls = [list(c) for c in calls]
    log.timeouts = [dict(t) for t in timeouts]
    return log


def api_error(at, kind="rate_limit", text="Usage limit reached"):
    return {"at": at, "kind": kind, "status": 429, "text": text, "resumed_at": None, "repeats": 1}


def run(agents, **kw):
    return Run(session_id="s1", agents=list(agents), **kw)


def cards(r, now=10_000.0, table=None):
    return advice.compute(r, now, table)["cards"]


def rule(r, name, **kw):
    return [c for c in cards(r, **kw) if c["rule"] == name]


class TestFailureCauses(unittest.TestCase):
    def fail(self, **kw):
        out = cards(run([agent(status=C.FAILED, **kw)]))
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["rule"], out[0]["severity"]), ("failure", "failure"))
        return out[0]

    def test_an_api_error_at_the_end_names_the_kind_and_its_fix(self):
        c = self.fail(errors=errlog(api=[api_error(590.0)]))
        self.assertIn("usage limit", c["title"].lower())
        self.assertIn("reset", c["fix"]["text"])

    def test_an_old_api_error_is_not_the_cause(self):
        c = self.fail(errors=errlog(api=[api_error(10.0)]))
        self.assertNotIn("usage limit", c["title"].lower())

    def test_an_expired_login(self):
        c = self.fail(errors=errlog(api=[api_error(595.0, kind="authentication_failed", text="401")]))
        self.assertIn("/login", c["fix"]["text"])

    def test_stuck_on_the_same_failing_call(self):
        log = errlog(calls=[[100.0 + i, "Bash", "npm test", False, "1 failed", "h1"] for i in range(4)])
        c = self.fail(errors=log)
        self.assertIn("Stuck", c["title"])
        self.assertIn("npm test", c["happened"])
        self.assertIn("fails twice, stop", c["fix"]["snippet"])

    def test_the_last_command_timed_out(self):
        log = errlog(calls=[[500.0, "Bash", "npm run e2e", True, "", "h2"]],
                     timeouts=[{"at": 500.0, "tool": "Bash", "target": "npm run e2e", "after_s": 120.0,
                                "background": False}])
        c = self.fail(errors=log)
        self.assertIn("timeout", c["title"].lower())
        self.assertIn('"BASH_DEFAULT_TIMEOUT_MS": "300000"', c["fix"]["snippet"])

    def test_a_timeout_moved_to_the_background_is_not_the_cause(self):
        log = errlog(calls=[[500.0, "Bash", "npm run e2e", True, "", "h2"]],
                     timeouts=[{"at": 500.0, "tool": "Bash", "target": "npm run e2e", "after_s": 120.0,
                                "background": True}])
        self.assertNotIn("timeout", self.fail(errors=log)["title"].lower())

    def test_context_ran_out(self):
        log = WasteLog()
        log.calls = OrderedDict([("m1", [100.0, "claude-sonnet-5", 0, 960_000, 10, 100])])
        self.assertIn("context", self.fail(waste=log)["title"].lower())

    def test_a_loop(self):
        tc = calls(*[("Bash", "npm test", 100.0 + i, False) for i in range(C.LOOP_REPEATS)])
        self.assertIn("circles", self.fail(tool_calls=tc)["title"])

    def test_cut_off_when_the_session_ended(self):
        c = cards(run([agent(status=C.ORPHANED)]))[0]
        self.assertEqual(c["fix"]["where"], "none")
        self.assertIn("session", c["title"].lower())

    def test_no_cause_in_the_record_says_so(self):
        tc = calls(("Read", "a.py", 1.0, True), ("Grep", "foo", 2.0, True), ("Read", "b.py", 3.0, True))
        c = self.fail(tool_calls=tc, result="Error: model refused\nmore")
        self.assertIn("No cause in the record", c["title"])
        self.assertIn("Read b.py", c["happened"])
        self.assertIn("Error: model refused", c["happened"])

    def test_the_same_api_error_across_agents_is_one_card(self):
        api = [api_error(590.0)]
        out = cards(run([agent("a1", status=C.FAILED, errors=errlog(api=api)),
                         agent("a2", status=C.FAILED, errors=errlog(api=api))]))
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0]["agents"]), ["a1", "a2"])
        self.assertTrue(out[0]["title"].startswith("2 agents"))

    def test_a_failure_shows_however_small(self):
        self.assertEqual(len(cards(run([agent(status=C.FAILED, start=0.0, end=1.0)]))), 1)

    def test_a_running_agent_gets_no_failure_card(self):
        self.assertEqual(cards(run([agent(status=C.RUNNING, end=None)])), [])


class TestCardShape(unittest.TestCase):
    def test_every_card_has_what_the_ui_reads_and_ascii_text(self):
        for c in cards(run([agent(status=C.FAILED)])):
            self.assertEqual(set(c), {"id", "rule", "tag", "severity", "title", "happened", "agents",
                                      "impact", "fix"})
            self.assertRegex(c["id"], r"^[0-9a-f]{12}$")
            self.assertEqual(set(c["impact"]), {"seconds", "tokens", "cost", "label"})
            self.assertEqual(set(c["fix"]), {"where", "text", "snippet"})
            for text in (c["title"], c["happened"], c["fix"]["text"], c["fix"]["snippet"]):
                text.encode("ascii")

    def test_order_and_cap(self):
        many = [advice._card("k%d" % i, "compact", "sure", "fix", "t", "h", [], "habit", "x", seconds=100.0 + i)
                for i in range(20)]
        shown = advice._select(many)
        self.assertEqual(len(shown), advice.MAX_CARDS)
        self.assertEqual(shown[0]["impact"]["seconds"], 119.0)

    def test_small_cards_are_dropped(self):
        small = advice._card("k", "compact", "sure", "fix", "t", "h", [], "habit", "x", seconds=59.0,
                             tokens=9_999, cost=0.049)
        self.assertEqual(advice._select([small]), [])


class TestVerdict(unittest.TestCase):
    def test_a_quiet_run(self):
        self.assertEqual(advice.compute(run([]), 0.0)["parts"], ["No agents", "nothing to fix"])

    def test_counts_outcomes_cost_and_cards(self):
        r = run([agent("a1"), agent("a2", status=C.FAILED)],
                insights={"outcomes": {"commits": 2, "prs": 1, "checks": {"passed": 3, "failed": 0}}},
                cost={"enabled": True, "total": 3.1, "currency": "USD", "source": "list", "as_of": "2026-10-12"})
        out = advice.compute(r, 10_000.0)
        self.assertEqual(out["parts"], ["2 agents, 1 failed", "2 commits, 1 PR", "tests passed",
                                        "$3.10 (list price)", "1 thing to fix"])
        self.assertEqual((out["priced_by"], out["as_of"], out["failures"]), ("list", "2026-10-12", 1))

    def test_failed_test_runs(self):
        r = run([agent()], insights={"outcomes": {"commits": 0, "prs": 0, "checks": {"passed": 2, "failed": 1}}})
        self.assertIn("1 of 3 test runs failed", advice.compute(r, 10_000.0)["parts"])


if __name__ == "__main__":
    unittest.main()
