"""Fix-it cards and the run verdict (orchestra.advice), on hand-built runs."""

import hashlib
import json
import unittest
from collections import OrderedDict

from orchestra import advice
from orchestra import constants as C
from orchestra import errors as error_module
from orchestra import listprices
from orchestra.errors import TARGET_CUT, ErrorLog
from orchestra.livestate import ANSWERED, PERMISSION, UNANSWERED, Wait
from orchestra.model import Agent, Batch, Edge, Round, Run, ToolCall, WriteConflict
from orchestra.pricing import PriceTable
from orchestra.turns import TurnLog
from orchestra.waste import WasteLog
from tests import fake_secrets as fake


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
        self.assertEqual(c["fix"]["text"], "Wait for the limit to reset, then resume the agent.")

    def test_an_old_api_error_is_not_the_cause(self):
        c = self.fail(errors=errlog(api=[api_error(10.0)]))
        self.assertNotIn("usage limit", c["title"].lower())

    def test_an_api_error_the_agent_recovered_from_is_not_the_cause(self):
        c = self.fail(errors=errlog(api=[dict(api_error(590.0), resumed_at=595.0)]))
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
        c = self.fail(waste=log)
        self.assertIn("context", c["title"].lower())
        self.assertEqual(c["tag"], "possible")

    def test_a_compaction_makes_the_context_card_sure(self):
        log = WasteLog()
        log.calls = OrderedDict([("m1", [100.0, "claude-sonnet-5", 0, 960_000, 10, 100])])
        log.compactions = [{"at": 100.0, "trigger": "auto", "pre_tokens": 960_000, "post_tokens": 40_000,
                            "duration_s": 12.0}]
        self.assertEqual(self.fail(waste=log)["tag"], "sure")

    def test_two_agents_out_of_context_are_one_card(self):
        def full_log():
            log = WasteLog()
            log.calls = OrderedDict([("m1", [100.0, "claude-sonnet-5", 0, 960_000, 10, 100])])
            return log
        out = cards(run([agent("a1", status=C.FAILED, waste=full_log(), tokens={"output": 4_000}),
                         agent("a2", status=C.FAILED, waste=full_log(), tokens={"output": 4_000})]))
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0]["agents"]), ["a1", "a2"])
        self.assertTrue(out[0]["title"].startswith("2 agents"))
        self.assertEqual(out[0]["impact"]["seconds"], 1200.0)
        self.assertEqual(out[0]["impact"]["tokens"], 8_000)

    def test_a_loop(self):
        tc = calls(*[("Bash", "npm test", 100.0 + i, False) for i in range(C.LOOP_REPEATS)])
        c = self.fail(tool_calls=tc)
        self.assertIn("circles", c["title"])
        self.assertEqual(c["tag"], "possible")

    def test_two_agents_in_circles_are_one_card(self):
        tc = calls(*[("Bash", "npm test", 100.0 + i, False) for i in range(C.LOOP_REPEATS)])
        out = cards(run([agent("a1", status=C.FAILED, tool_calls=tc),
                         agent("a2", status=C.FAILED, tool_calls=tc)]))
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0]["agents"]), ["a1", "a2"])
        self.assertTrue(out[0]["title"].startswith("2 agents"))

    def test_cut_off_when_the_session_ended(self):
        c = cards(run([agent(status=C.ORPHANED)]))[0]
        self.assertEqual(c["fix"]["where"], "none")
        self.assertIn("session", c["title"].lower())

    def test_a_stalled_agent_mid_call_went_quiet(self):
        out = cards(run([agent(status=C.STALLED, end=None, tool_calls=calls(("Bash", "npm run e2e", 500.0, None)))]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Went quiet during a tool call")
        self.assertEqual(out[0]["fix"]["where"], "none")
        self.assertEqual(out[0]["agents"], ["a1"])

    def test_a_failed_agent_mid_call_still_stopped_with_the_session(self):
        c = self.fail(tool_calls=calls(("Bash", "npm test", 500.0, None)))
        self.assertEqual(c["title"], "Stopped when the session ended")

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


def unchecked(edit="src/app.py"):
    return {"state": "unchecked", "final": True, "checked_before": False, "last_check": None,
            "last_edit": {"tool": "Edit", "target": edit, "at": 50.0}}


class TestSureRules(unittest.TestCase):
    def test_unchecked_work_asks_for_the_check_seen_in_the_run(self):
        a = agent(verification=unchecked(), tokens={"input": 5000, "output": 2000})
        other = agent("a2", tool_calls=[ToolCall(name="Bash", target="pytest -q", timestamp=10.0, ok=True,
                                                 verify="check")])
        (c,) = rule(run([a, other]), "verify")
        self.assertIn("src/app.py", c["happened"])
        self.assertIn("run `pytest -q`", c["fix"]["snippet"])
        self.assertEqual(c["fix"]["where"], "instructions")

    def test_without_a_check_seen_it_names_the_project_tests(self):
        (c,) = rule(run([agent(verification=unchecked())]), "verify")
        self.assertIn("the project's tests", c["fix"]["snippet"])

    def test_a_failing_last_check(self):
        v = dict(unchecked(), state="failing", last_check={"tool": "Bash", "target": "npm test", "at": 60.0})
        (c,) = rule(run([agent(verification=v)]), "verify")
        self.assertIn("failing", c["title"])
        self.assertIn("npm test", c["happened"])

    def test_checked_or_unfinished_work_gets_nothing(self):
        done = dict(unchecked(), state="checked")
        running = dict(unchecked(), final=False)
        self.assertEqual(rule(run([agent(verification=done), agent("a2", verification=running)]), "verify"), [])

    def test_two_writers_of_one_file(self):
        r = run([agent("a1"), agent("a2")], write_conflicts=[WriteConflict(path="src/x.py", writer_ids=["a1", "a2"])])
        (c,) = rule(r, "worktree")
        self.assertEqual(c["fix"]["snippet"], "isolation: worktree")
        self.assertEqual(c["agents"], ["a1", "a2"])
        self.assertIn("src/x.py", c["happened"])

    def test_a_cache_rebuilt_after_a_long_break_is_priced(self):
        log = WasteLog()
        log.calls = OrderedDict([("m1", [0.0, "claude-sonnet-5", 50_000, 0, 10, 10]),
                                 ("m2", [4000.0, "claude-sonnet-5", 200_000, 0, 10, 10])])
        (c,) = rule(run([], main_waste=log), "compact", table=listprices.table())
        self.assertEqual(c["impact"]["tokens"], 200_000)
        self.assertAlmostEqual(c["impact"]["cost"], 200_000 * (4.0 - 0.2) / 1e6)
        self.assertIn("/compact", c["fix"]["text"])
        self.assertEqual(c["agents"], [""])

    def test_a_rebuild_after_a_model_switch_is_not_a_break(self):
        log = WasteLog()
        log.calls = OrderedDict([("m1", [0.0, "claude-sonnet-5", 50_000, 0, 10, 10]),
                                 ("m2", [4000.0, "claude-opus-5", 200_000, 0, 10, 10])])
        self.assertEqual(rule(run([], main_waste=log), "compact"), [])

    def test_an_agent_without_the_project_rules(self):
        r = run([agent("a1")], insights={"context": {"missing": [
            {"agent_id": "a1", "label": "Agent a1", "agent_type": "general-purpose", "status": "completed",
             "missing": ["E:/p/CLAUDE.md"]}]}})
        (c,) = rule(r, "rules_missing")
        self.assertIn("CLAUDE.md", c["happened"])
        self.assertIn("omitClaudeMd", c["fix"]["text"])

    def test_a_failed_or_cut_off_agent_gets_no_verify_card(self):
        out = cards(run([agent("a1", status=C.FAILED, verification=unchecked()),
                         agent("a2", status=C.ORPHANED, verification=unchecked())]))
        self.assertEqual([c for c in out if c["rule"] == "verify"], [])
        self.assertEqual(sorted(a for c in out if c["rule"] == "failure" for a in c["agents"]), ["a1", "a2"])

    def test_two_failing_checks_name_their_own_commands(self):
        a1 = agent("a1", verification=dict(unchecked(), state="failing",
                                           last_check={"tool": "Bash", "target": "npm test", "at": 60.0}))
        a2 = agent("a2", verification=dict(unchecked(), state="failing",
                                           last_check={"tool": "Bash", "target": "pytest -q", "at": 61.0}))
        out = rule(run([a1, a2]), "verify")
        self.assertEqual(len(out), 2)
        got = {c["fix"]["snippet"].split("`")[1]: c["agents"] for c in out}
        self.assertEqual(got, {"npm test": ["a1"], "pytest -q": ["a2"]})

    def test_a_command_in_a_snippet_is_its_first_line_only(self):
        other = agent("a2", tool_calls=[ToolCall(name="Bash", target="pytest -q\nmore", timestamp=10.0, ok=True,
                                                 verify="check")])
        (c,) = rule(run([agent(verification=unchecked()), other]), "verify")
        self.assertIn("run `pytest -q`", c["fix"]["snippet"])

    def test_a_cut_off_command_is_not_put_in_a_snippet(self):
        for cut in ("npm test ...", "npm test\u2026"):
            other = agent("a2", tool_calls=[ToolCall(name="Bash", target=cut, timestamp=10.0, ok=True,
                                                     verify="check")])
            (c,) = rule(run([agent(verification=unchecked()), other]), "verify")
            self.assertIn("the project's tests", c["fix"]["snippet"])
            self.assertNotIn("`", c["fix"]["snippet"])
        v = dict(unchecked(), state="failing", last_check={"tool": "Bash", "target": "npm test ...", "at": 60.0})
        (c,) = rule(run([agent(verification=v)]), "verify")
        self.assertIn("the project's tests", c["fix"]["snippet"])

    def test_a_table_that_prices_no_rebuilt_model_gives_no_cost(self):
        log = WasteLog()
        log.calls = OrderedDict([("m1", [0.0, "claude-unlisted-1", 50_000, 0, 10, 10]),
                                 ("m2", [4000.0, "claude-unlisted-1", 200_000, 0, 10, 10])])
        (c,) = rule(run([], main_waste=log), "compact", table=listprices.table())
        self.assertEqual(c["impact"]["tokens"], 200_000)
        self.assertIsNone(c["impact"]["cost"])


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

    def test_merged_titles_fill_n_without_format(self):
        first = advice._card("k", "r", "sure", "failure", "t", "h", ["a1"], "none", "x", title_many="a {x} {n} b")
        second = advice._card("k", "r", "sure", "failure", "t", "h", ["a2"], "none", "x", title_many="a {x} {n} b")
        self.assertEqual(advice._merge([first, second])[0]["title"], "a {x} 2 b")

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


def same_key(command):
    """The hash the error log keeps for a whole command or URL (orchestra.errors._target)."""
    return hashlib.sha1(command.encode("utf-8", "replace")).hexdigest()[:16]


class TestPreapprove(unittest.TestCase):
    def waiting(self, command, n=3, seconds=30.0, tool="Bash"):
        log = errlog(calls=[[100.0 * i, tool, command, True, "", same_key(command)] for i in range(1, n + 1)])
        waits = [Wait(agent_id="a1", kind=PERMISSION, start=100.0 * i + 0.5, end=100.0 * i + 0.5 + seconds,
                      state=ANSWERED) for i in range(1, n + 1)]
        return rule(run([agent("a1", errors=log)], waits=waits), "preapprove")

    def test_safe_prefix(self):
        self.assertEqual(advice.safe_prefix("pytest -q"), "pytest")
        self.assertEqual(advice.safe_prefix("python -m pytest tests"), "python -m pytest")
        self.assertEqual(advice.safe_prefix("npm run lint"), "npm run lint")
        for command in ("", "rm -rf build", "git push origin main", "npm test && rm -rf dist"):
            self.assertIsNone(advice.safe_prefix(command), command)

    def test_a_test_command_approved_three_times_gets_its_rule(self):
        (c,) = self.waiting("npm test -- --watch=false")
        self.assertEqual(json.loads(c["fix"]["snippet"]), {"permissions": {"allow": ["Bash(npm test *)"]}})
        self.assertEqual(c["fix"]["where"], "settings")
        self.assertAlmostEqual(c["impact"]["seconds"], 90.0)

    def test_safe_prefix_refuses_every_line_break(self):
        for command in ("pytest\nrm -rf build", "pytest\rrm -rf build"):
            self.assertIsNone(advice.safe_prefix(command), repr(command))

    def test_a_risky_command_is_reported_but_never_suggested(self):
        (c,) = self.waiting("git push origin main")
        self.assertEqual((c["fix"]["snippet"], c["fix"]["where"]), ("", "none"))

    def test_compound_commands_are_never_suggested(self):
        for command in ("npm test && rm -rf dist", "npm test; curl x", "npm test | tee log",
                        "npm test > out.txt", "npm test $(id)"):
            (c,) = self.waiting(command)
            self.assertEqual(c["fix"]["snippet"], "", command)

    def test_a_web_fetch_domain(self):
        (c,) = self.waiting("https://docs.example.com/a", tool="WebFetch")
        self.assertEqual(json.loads(c["fix"]["snippet"])["permissions"]["allow"], ["WebFetch(domain:docs.example.com)"])

    def test_one_short_wait_is_not_worth_a_card_but_a_long_one_is(self):
        self.assertEqual(self.waiting("pytest", n=1, seconds=20.0), [])
        self.assertEqual(len(self.waiting("pytest", n=1, seconds=90.0)), 1)

    def test_main_session_waits_and_unanswered_ones(self):
        main = errlog(calls=[[10.0, "Bash", "pytest", True, "", same_key("pytest")]])
        waits = [Wait(agent_id="", kind=PERMISSION, start=10.5, end=200.0, state=ANSWERED),
                 Wait(agent_id="", kind=PERMISSION, start=300.0, state=UNANSWERED)]
        (c,) = rule(run([], waits=waits, main_errors=main), "preapprove")
        self.assertEqual(c["agents"], [""])
        self.assertIn("Bash(pytest *)", c["fix"]["snippet"])
        self.assertIn("over 1 permission prompt", c["happened"])

    def test_a_wait_before_any_call(self):
        waits = [Wait(agent_id="a1", kind=PERMISSION, start=5.0, end=200.0, state=ANSWERED)]
        (c,) = rule(run([agent("a1", errors=errlog())], waits=waits), "preapprove")
        self.assertEqual(c["fix"]["snippet"], "")

    def test_a_command_whose_first_line_is_not_all_of_it_is_never_suggested(self):
        # The error log keeps only the first line (cut at 160 characters) and a hash of the whole command.
        for whole in ("pytest -q\nrm -rf build", "pytest\rrm -rf build", "pytest -q " + "x" * 200):
            first = whole.splitlines()[0][:160]
            log = errlog(calls=[[100.0, "Bash", first, True, "", same_key(whole)]])
            waits = [Wait(agent_id="a1", kind=PERMISSION, start=100.5, end=190.0, state=ANSWERED)]
            (c,) = rule(run([agent("a1", errors=log)], waits=waits), "preapprove")
            self.assertEqual((c["fix"]["snippet"], c["fix"]["where"]), ("", "none"), whole[:20])

    def test_a_web_fetch_address_that_is_not_a_plain_domain_is_never_suggested(self):
        for url in ("http://[", "https://a.com),Bash(rm:*"):
            (c,) = self.waiting(url, tool="WebFetch")
            self.assertEqual((c["fix"]["snippet"], c["fix"]["where"]), ("", "none"), url)

    def test_a_url_whose_host_is_not_plain_gets_no_rule(self):
        # Python's parser and Claude Code's can disagree about these; an address, a local name or userinfo
        # is not a domain a rule should name.
        for url in ("https://evil.com\\@attacker.net/x", "https://evil.com@attacker.net/x",
                    "https://evil.com/\\@attacker.net/x", "https://evil.com/x@attacker.net",
                    "http://169.254.169.254/latest", "http://localhost:8080/x", "https://-a-.com/x"):
            (c,) = self.waiting(url, tool="WebFetch")
            self.assertEqual((c["fix"]["snippet"], c["fix"]["where"]), ("", "none"), url)

    def test_a_host_that_runs_past_the_error_log_cut_gets_no_rule(self):
        # orchestra.errors keeps only the first 160 characters of a target: a host that runs past them is cut.
        probe = "https://" + "a" * 63 + "." + "b" * 63 + "." + "y" * 20 + ".com.attacker.net/x"
        self.assertEqual(len(probe), 175)
        (c,) = self.waiting(probe[:TARGET_CUT], tool="WebFetch")
        self.assertEqual((c["fix"]["snippet"], c["fix"]["where"]), ("", "none"))
        (short,) = self.waiting("https://docs.example.com/a", tool="WebFetch")
        self.assertEqual(json.loads(short["fix"]["snippet"])["permissions"]["allow"],
                         ["WebFetch(domain:docs.example.com)"])

    def fetched(self, params):
        """A WebFetch call stored the way orchestra.errors stores it, and the preapprove cards for it."""
        shown, same = error_module._target(params)
        log = errlog(calls=[[100.0, "WebFetch", shown, True, "", same]])
        waits = [Wait(agent_id="a1", kind=PERMISSION, start=100.5, end=190.0, state=ANSWERED)]
        return rule(run([agent("a1", errors=log)], waits=waits), "preapprove")

    def test_a_url_with_a_line_break_gets_no_rule(self):
        # The log keeps only the first line of this URL, so the host after the break must not be named.
        self.assertEqual(error_module._target({"url": "https://trusted.com\n@attacker.com/x"})[0], "https://trusted.com")
        (c,) = self.fetched({"url": "https://trusted.com\n@attacker.com/x"})
        self.assertEqual((c["fix"]["snippet"], c["fix"]["where"]), ("", "none"))

    def test_a_single_line_url_gets_its_rule_on_its_stored_hash(self):
        (c,) = self.fetched({"url": "https://docs.example.com/a"})
        self.assertEqual(json.loads(c["fix"]["snippet"])["permissions"]["allow"], ["WebFetch(domain:docs.example.com)"])

    def test_a_localhost_name_gets_no_rule(self):
        # Names under .localhost resolve to this machine, so a rule for one would approve a local server.
        for url in ("http://foo.localhost/x", "https://a.b.localhost:8080/x"):
            (c,) = self.waiting(url, tool="WebFetch")
            self.assertEqual((c["fix"]["snippet"], c["fix"]["where"]), ("", "none"), url)

    def test_a_report_only_card_names_what_it_would_allow(self):
        (bash,) = self.waiting("git push origin main")
        (web,) = self.waiting("https://evil.com\\@attacker.net/x", tool="WebFetch")
        self.assertIn("would also allow commands you have not seen", bash["fix"]["text"])
        self.assertIn("would also allow ones you have not seen", web["fix"]["text"])

    def test_a_web_fetch_host_keeps_its_rule_through_a_port_and_a_query(self):
        (c,) = self.waiting("https://docs.example.com:8443/a?b", tool="WebFetch")
        self.assertEqual(json.loads(c["fix"]["snippet"])["permissions"]["allow"], ["WebFetch(domain:docs.example.com)"])

    def test_three_short_prompts_still_get_their_rule(self):
        (c,) = self.waiting("pytest", n=3, seconds=5.0)
        self.assertEqual(json.loads(c["fix"]["snippet"])["permissions"]["allow"], ["Bash(pytest *)"])
        self.assertAlmostEqual(c["impact"]["seconds"], 15.0)

    def test_each_rule_card_says_how_far_it_reaches(self):
        (bash,) = self.waiting("pytest -q")
        (web,) = self.waiting("https://docs.example.com/a", tool="WebFetch")
        self.assertIn("allows any arguments", bash["fix"]["text"])
        self.assertIn("any page on that domain", web["fix"]["text"])

    def test_a_tool_name_is_scrubbed_before_it_is_shown(self):
        (c,) = self.waiting("git push", tool=fake.ANTHROPIC_KEY)
        self.assertNotIn(fake.ANTHROPIC_KEY, c["title"] + c["happened"])
        self.assertIn("redacted", c["title"] + c["happened"])


class TestCheaperModel(unittest.TestCase):
    TOKENS = {"input": 20_000, "output": 30_000, "cache_read": 2_000_000, "cache_create": 200_000}

    def reader(self, model="claude-opus-5-5", n=4, tokens=None, extra=(), **kw):
        tc = calls(*[("Read", "f%d.py" % i, float(i), True) for i in range(n)] + list(extra))
        return agent(model=model, tool_calls=tc, tokens_by_model={model: tokens or dict(self.TOKENS)}, **kw)

    def cheaper(self, a, table="list"):
        return rule(run([a]), "cheaper_model", table=listprices.table() if table == "list" else table)

    def test_an_opus_agent_that_only_read_could_use_sonnet(self):
        (c,) = self.cheaper(self.reader())
        self.assertEqual((c["tag"], c["fix"]["snippet"]), ("possible", "model: sonnet"))
        self.assertAlmostEqual(c["impact"]["cost"], 2.68 - 1.54)      # opus-5-5 vs sonnet-5-5 list prices

    def test_a_sonnet_agent_could_use_haiku(self):
        (c,) = self.cheaper(self.reader(model="claude-sonnet-5"))
        self.assertEqual(c["fix"]["snippet"], "model: haiku")

    def test_an_agent_that_edited_or_ran_commands_is_left_alone(self):
        self.assertEqual(self.cheaper(self.reader(extra=[("Edit", "a.py", 9.0, True)])), [])
        self.assertEqual(self.cheaper(self.reader(extra=[("Bash", "ls", 9.0, True)])), [])

    def test_too_few_calls_or_too_small_a_saving(self):
        self.assertEqual(self.cheaper(self.reader(n=2)), [])
        self.assertEqual(self.cheaper(self.reader(tokens={"input": 100, "output": 100})), [])

    def test_without_prices_or_with_a_table_lacking_the_target(self):
        self.assertEqual(self.cheaper(self.reader(), table=None), [])
        mine = PriceTable.parse({"models": {"claude-opus-*": {"input": 5, "output": 25}}})
        self.assertEqual(self.cheaper(self.reader(), table=mine), [])

    def test_haiku_and_failed_agents_are_left_alone(self):
        self.assertEqual(self.cheaper(self.reader(model="claude-haiku-4-5")), [])
        self.assertEqual([c for c in self.cheaper(self.reader(status=C.FAILED)) if c["rule"] == "cheaper_model"], [])

    def test_a_saving_under_a_third_of_the_cost_is_not_worth_a_card(self):
        # opus-5-5 costs 2.4 here and sonnet-5-5 would cost 2.2: a saving of 0.20 clears the $0.05 floor,
        # but not the 30% share.
        tokens = {"input": 100_000, "cache_read": 10_000_000}
        self.assertEqual(self.cheaper(self.reader(tokens=tokens)), [])

    def test_the_model_name_is_scrubbed_before_it_is_shown(self):
        (c,) = self.cheaper(self.reader(model="claude-opus-5-5-" + fake.ANTHROPIC_KEY))
        self.assertNotIn(fake.ANTHROPIC_KEY, c["happened"])
        self.assertNotIn(fake.ANTHROPIC_KEY, json.dumps(c))
        self.assertIn("redacted", c["happened"])


class TestParallel(unittest.TestCase):
    def seq(self, gap=10.0, first=None, second=None, **kw):
        a1 = agent("a1", start=0.0, end=200.0, **(first or {}))
        a2 = agent("a2", start=200.0 + gap, end=200.0 + gap + 300.0, **(second or {}))
        return rule(run([a1, a2], **kw), "parallel")

    def test_two_unlinked_agents_one_after_another(self):
        (c,) = self.seq()
        self.assertEqual(c["tag"], "possible")
        self.assertAlmostEqual(c["impact"]["seconds"], 200.0)
        self.assertEqual(c["agents"], ["a1", "a2"])

    def test_a_handoff_between_them(self):
        self.assertEqual(self.seq(edges=[Edge(src="a1", dst="a2", kind="handoff", confidence="inferred")]), [])

    def test_a_file_one_wrote_and_the_other_read(self):
        self.assertEqual(self.seq(first={"files_written": ["src/plan.md"]}, second={"files_read": ["src/plan.md"]}), [])

    def test_a_prompt_in_between_means_you_were_deciding(self):
        turns = TurnLog()
        turns.turns = [{"at": 205.0}]
        self.assertEqual(self.seq(main_turns=turns), [])

    def test_a_long_gap_is_not_a_chain(self):
        self.assertEqual(self.seq(gap=600.0), [])

    def test_launched_together_is_already_parallel(self):
        self.assertEqual(self.seq(batches=[Batch(agent_ids=["a1", "a2"])]), [])

    def test_a_short_saving_is_below_the_threshold(self):
        a1, a2 = agent("a1", start=0.0, end=30.0), agent("a2", start=40.0, end=80.0)
        self.assertEqual(rule(run([a1, a2]), "parallel"), [])

    def test_two_groups_of_agents_are_one_card(self):
        group = [agent("a1", start=0.0, end=200.0, parent_agent_id="p1"),
                 agent("a2", start=210.0, end=510.0, parent_agent_id="p1"),
                 agent("b1", start=1000.0, end=1200.0, parent_agent_id="p2"),
                 agent("b2", start=1210.0, end=1510.0, parent_agent_id="p2")]
        (c,) = rule(run(group), "parallel")
        self.assertEqual(sorted(c["agents"]), ["a1", "a2", "b1", "b2"])
        self.assertEqual(c["title"], "2 groups of agents could have run together")
        self.assertAlmostEqual(c["impact"]["seconds"], 400.0)


if __name__ == "__main__":
    unittest.main()
