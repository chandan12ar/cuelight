"""Usual numbers per kind of agent, learned from past sessions (orchestra.baselines)."""

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from orchestra import baselines, listprices, locate
from tests import fake_secrets as fake
from tests.fixtures import build_session


def row(type_="general-purpose", model="sonnet-5", seconds=60.0, status="completed", project="p",
        session="sX", edited=False, checked="", tokens=None):
    return {"session": session, "project": baselines.project_key(project), "type": type_, "model": model,
            "status": status, "start": 0.0, "seconds": seconds,
            "tokens": tokens or {"claude-sonnet-5": {"input": 1000, "output": 1000}},
            "calls": 5, "edited": edited, "checked": checked, "loop": False, "waited": 0.0}


def store_with(rows):
    s = baselines.Baselines(root="unused", cache_path="unused")
    s._install({"x": {"fp": [], "rows": rows}})
    return s


class TestScan(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.cache = os.path.join(tempfile.mkdtemp(), "baselines-v1.json")

    def store(self):
        return baselines.Baselines(root=self.root, cache_path=self.cache)

    def test_rows_are_numbers_only_for_finished_agents(self):
        build_session(self.root, "s1")              # a1, a2 finished; a3 still running
        s = self.store()
        s.scan()
        self.assertEqual(s.state, "ready")
        rows = s.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows[0]), {"session", "project", "type", "model", "status", "start", "seconds",
                                        "tokens", "calls", "edited", "checked", "loop", "waited"})
        # Values only: the key "project" contains "proj", and the key set is asserted above.
        text = json.dumps([list(r.values()) for r in rows])
        for leak in ("PLAN.md", "Plan the work", "proj", "Implement"):
            self.assertNotIn(leak, text)

    def test_the_cache_is_reused_until_a_session_changes(self):
        build_session(self.root, "s1")
        self.store().scan()
        with mock.patch.object(baselines, "rows_for", side_effect=AssertionError("read again")):
            again = self.store()
            again.scan()
        self.assertEqual(len(again.rows()), 2)
        path = os.path.join(self.root, "projects", "E--proj", "s1.jsonl")
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        with mock.patch.object(baselines, "rows_for", return_value=[]) as reread:
            self.store().scan()
        reread.assert_called_once()

    def test_a_vanished_session_drops_out(self):
        build_session(self.root, "s1")
        self.store().scan()
        shutil.rmtree(os.path.join(self.root, "projects"))
        s = self.store()
        s.scan()
        self.assertEqual(s.rows(), [])

    def test_a_corrupt_or_old_cache_is_ignored(self):
        build_session(self.root, "s1")
        for content in ("{not json", json.dumps({"version": 0, "sessions": {}}), "[]"):
            with open(self.cache, "w", encoding="utf-8") as fh:
                fh.write(content)
            s = self.store()
            s.scan()
            self.assertEqual(len(s.rows()), 2, content)

    @unittest.skipUnless(os.name == "posix", "file modes are POSIX")
    def test_the_cache_file_is_private(self):
        build_session(self.root, "s1")
        self.store().scan()
        self.assertEqual(os.stat(self.cache).st_mode & 0o777, 0o600)

    def test_missing_projects_folder(self):
        s = self.store()
        s.scan()
        self.assertEqual((s.state, s.rows()), ("ready", []))
        self.assertEqual(s.summary("", "")["groups"], [])
        self.assertIn("No past runs yet", baselines.render_estimate(s.summary("", "")))

    def test_one_unreadable_session_does_not_stop_the_scan(self):
        build_session(self.root, "s1")
        build_session(self.root, "s2")
        real = baselines.rows_for

        def flaky(paths):
            if paths.session_id == "s1":
                raise ValueError("unreadable")
            return real(paths)
        with mock.patch.object(baselines, "rows_for", side_effect=flaky):
            s = self.store()
            s.scan()
        self.assertEqual({r["session"] for r in s.rows()}, {"s2"})

    def test_an_unusable_state_directory_does_not_stop_the_scan(self):
        build_session(self.root, "s1")
        with mock.patch.object(baselines, "state_dir", side_effect=OSError("no state dir")):
            s = baselines.Baselines(root=self.root)
            s.scan()
        self.assertEqual((s.state, len(s.rows())), ("ready", 2))

    def test_a_failed_scan_waits_before_the_next_attempt(self):
        build_session(self.root, "s1")
        s = self.store()
        with mock.patch.object(baselines.os, "listdir", side_effect=PermissionError("denied")):
            s.scan()
        self.assertEqual(s.state, "error")
        with mock.patch.object(s, "start") as start:
            s.start_if_stale()
        start.assert_not_called()

    def test_a_session_with_an_unfinished_agent_is_read_again(self):
        build_session(self.root, "s1")                 # a3 is still unfinished when it is first read
        self.store().scan()
        with open(self.cache, encoding="utf-8") as fh:
            self.assertIsNone(json.load(fh)["sessions"]["s1"]["fp"])
        two_hours_on = time.time() + 2 * 3600          # no file changed, but a3 is now cut off
        real = baselines.rows_for
        with mock.patch.object(baselines, "rows_for",
                               side_effect=lambda paths: real(paths, now_fn=lambda: two_hours_on)) as reread:
            s = self.store()
            s.scan()
        reread.assert_called_once()
        self.assertEqual(len(s.rows()), 3)
        self.assertEqual([(r["status"], r["seconds"]) for r in s.rows() if r["seconds"] is None],
                         [("orphaned", None)])

    def test_a_cut_off_agent_is_kept_with_no_time(self):
        paths = build_session(self.root, "s1")
        two_hours_on = time.time() + 2 * 3600          # the session is no longer live: a3 is cut off
        rows = baselines.rows_for(paths, now_fn=lambda: two_hours_on)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted((r["status"], r["seconds"]) for r in rows if r["seconds"] is None),
                         [("orphaned", None)])

    def test_a_later_scan_error_keeps_the_numbers(self):
        build_session(self.root, "s1")
        s = self.store()
        s.scan()
        with mock.patch.object(baselines.os, "listdir", side_effect=PermissionError("denied")):
            s.scan()
        self.assertEqual(s.state, "error")
        u = s.usual_for("general-purpose", "haiku-4-5", "", "")
        self.assertEqual((u is not None, u["n"]), (True, 2))
        self.assertEqual(len(s.summary("", "")["groups"]), 1)

    def test_a_session_that_failed_is_read_again_next_time(self):
        build_session(self.root, "s1")
        with mock.patch.object(baselines, "rows_for", side_effect=ValueError("unreadable")):
            self.store().scan()
        s = self.store()
        s.scan()
        self.assertEqual(len(s.rows()), 2)

    def test_a_cache_entry_with_bad_rows_is_read_again(self):
        build_session(self.root, "s1")
        paths = locate._paths_for(os.path.join(self.root, "projects", "E--proj", "s1.jsonl"), "s1")
        with open(self.cache, "w", encoding="utf-8") as fh:
            json.dump({"version": baselines.VERSION,
                       "sessions": {"s1": {"fp": baselines._fingerprint(paths), "rows": [{"session": "s1"}]}}}, fh)
        s = self.store()
        s.scan()
        self.assertEqual(len(s.rows()), 2)

    def test_names_from_the_transcripts_are_scrubbed_before_they_are_kept(self):
        paths = build_session(self.root, "s1")
        meta = os.path.join(paths.subagents_dir, "agent-a1.meta.json")
        with open(meta, encoding="utf-8") as fh:
            data = json.load(fh)
        data["agentType"] = "reviewer " + fake.ANTHROPIC_KEY
        with open(meta, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        s = self.store()
        s.scan()
        self.assertNotIn(fake.ANTHROPIC_KEY, json.dumps(s.rows()))
        self.assertIn("reviewer", json.dumps(s.rows()))


class TestUsual(unittest.TestCase):
    def test_median_and_90th_percentile(self):
        rows = [row(seconds=float(v)) for v in (10, 20, 30, 40, 50, 60, 70, 80, 90, 1000)]
        u = store_with(rows).usual_for("general-purpose", "claude-sonnet-5", "/x/p", "")
        self.assertEqual((u["typical_s"], u["slow_s"], u["n"], u["scope"], u["enough"]),
                         (55.0, 90.0, 10, "project", True))

    def test_model_dates_are_dropped_and_both_type_and_model_count(self):
        s = store_with([row(model="haiku-4-5", project="other") for _ in range(5)])
        u = s.usual_for("general-purpose", "claude-haiku-4-5-20251001", "/x/p", "")
        self.assertEqual((u["scope"], u["n"]), ("all", 5))
        self.assertFalse(s.usual_for("Explore", "claude-haiku-4-5", "/x/p", "")["enough"])

    def test_this_project_first_then_all_then_not_enough(self):
        mine, theirs = [row(project="p") for _ in range(5)], [row(project="q") for _ in range(3)]
        self.assertEqual(store_with(mine + theirs).usual_for("general-purpose", "sonnet-5", "/x/p", "")["scope"],
                         "project")
        u = store_with(mine[:3] + theirs).usual_for("general-purpose", "sonnet-5", "/x/p", "")
        self.assertEqual((u["scope"], u["n"]), ("all", 6))
        u = store_with(mine[:2] + theirs[:2]).usual_for("general-purpose", "sonnet-5", "/x/p", "")
        self.assertEqual((u["enough"], u["n"]), (False, 4))

    def test_the_current_session_is_left_out(self):
        s = store_with([row(session="s1") for _ in range(5)])
        self.assertFalse(s.usual_for("general-purpose", "sonnet-5", "/x/p", "s1")["enough"])

    def test_fail_and_skipped_test_rates(self):
        rows = [row() for _ in range(3)] + [row(status="failed"), row(status="orphaned")]
        rows[0].update(edited=True, checked="checked")
        rows[1].update(edited=True, checked="unchecked")
        u = store_with(rows).usual_for("general-purpose", "sonnet-5", "/x/p", "")
        self.assertEqual((u["fail_rate"], u["unchecked_rate"]), (0.4, 0.5))

    def test_cut_off_runs_count_toward_n_and_fail_rate_but_not_time(self):
        rows = [row(seconds=float(v)) for v in (10, 20, 30, 40)] + [row(seconds=None, status="orphaned")]
        u = store_with(rows).usual_for("general-purpose", "sonnet-5", "/x/p", "")
        self.assertEqual((u["n"], u["fail_rate"], u["enough"]), (5, 0.2, True))
        self.assertEqual((u["typical_s"], u["slow_s"]), (25.0, 40.0))

    def test_a_group_with_no_timed_run_is_not_enough(self):
        u = store_with([row(seconds=None, status="failed") for _ in range(5)]).usual_for(
            "general-purpose", "sonnet-5", "/x/p", "")
        self.assertEqual((u["enough"], u["n"]), (False, 5))

    def test_a_failing_run_is_not_counted_as_unchecked(self):
        rows = [row(edited=True, checked="checked"), row(edited=True, checked="failing"),
                row(edited=True, checked="failing"), row(edited=True, checked="unchecked"), row()]
        u = store_with(rows).usual_for("general-purpose", "sonnet-5", "/x/p", "")
        self.assertEqual(u["unchecked_rate"], 0.25)

    def test_cost_is_priced_when_asked(self):
        u = store_with([row() for _ in range(5)]).usual_for("general-purpose", "sonnet-5", "/x/p", "",
                                                            listprices.table())
        self.assertAlmostEqual(u["typical_cost"], (1000 * 2.0 + 1000 * 10.0) / 1e6)

    def test_an_unpriced_run_is_not_counted_as_free(self):
        unpriced = [row(model="opus-4-1", tokens={"claude-opus-4-1": {"input": 1000, "output": 1000}})
                    for _ in range(5)]
        u = store_with(unpriced).usual_for("general-purpose", "opus-4-1", "/x/p", "", listprices.table())
        self.assertEqual((u["n"], u["typical_cost"], u["slow_cost"]), (5, None, None))
        mixed = [row() for _ in range(5)] + [row(tokens={"claude-mystery-1": {"input": 1000, "output": 1000}})
                                             for _ in range(6)]
        u = store_with(mixed).usual_for("general-purpose", "sonnet-5", "/x/p", "", listprices.table())
        self.assertAlmostEqual(u["typical_cost"], (1000 * 2.0 + 1000 * 10.0) / 1e6)

    def test_nothing_before_the_first_scan(self):
        self.assertIsNone(baselines.Baselines(root="unused", cache_path="unused")
                          .usual_for("general-purpose", "sonnet-5", "", ""))

    def test_summary_and_estimate_text(self):
        s = store_with([row() for _ in range(5)] + [row(type_="Explore", model="haiku-4-5")])
        data = s.summary("/x/p", "", listprices.table())
        self.assertEqual([g["enough"] for g in data["groups"]], [True, False])
        self.assertEqual((data["source"], data["as_of"]), ("list", listprices.AS_OF))
        text = baselines.render_estimate(data)
        self.assertIn("general-purpose", text)
        self.assertIn("Not enough past runs yet", text)
        self.assertIn("list prices as of " + listprices.AS_OF, text)
        text.encode("ascii")


if __name__ == "__main__":
    unittest.main()
