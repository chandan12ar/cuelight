import json
import os
import tempfile
import unittest

from orchestra import constants as C
from orchestra.build import RunBuilder
from orchestra.events import SESSION_END, Event, EventSpool
from orchestra.parent import parse_timestamp
from tests.fixtures import (agent_entry, build_nested_session, build_session, ts,
                            write_jsonl)

NOW_LIVE = parse_timestamp(ts(150))
NOW_DEAD = NOW_LIVE + C.SESSION_LIVE_THRESHOLD_S + 60


class BuildTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.paths = build_session(self.root)

    def build(self, now=NOW_LIVE, live=True):
        builder = RunBuilder(self.paths, now_fn=lambda: now)
        if not live:
            os.utime(self.paths.session_jsonl, (now - 99999, now - 99999))
        else:
            os.utime(self.paths.session_jsonl, (now, now))
        return builder.refresh()


class TestRunAssembly(BuildTestCase):
    def test_finds_all_three_agents(self):
        run = self.build()
        self.assertEqual({a.agent_id for a in run.agents}, {"a1", "a2", "a3"})

    def test_project_path_comes_from_the_transcript(self):
        self.assertEqual(self.build().project_path, r"E:\proj")

    def test_metadata_is_attached(self):
        a1 = self.build().agent("a1")
        self.assertEqual(a1.agent_type, "general-purpose")
        self.assertEqual(a1.description, "Plan the work")
        self.assertEqual(a1.launch_mode, "background")
        # The agent's own transcript reports the real, versioned model
        # ("claude-haiku-4-5-20251001"); that's the ground truth over the
        # short alias ("haiku") requested at spawn time in meta.json.
        self.assertEqual(a1.model, "claude-haiku-4-5-20251001")

    def test_brief_and_extraction(self):
        a1 = self.build().agent("a1")
        self.assertIn("You are planning", a1.brief)
        self.assertIn("A written plan at PLAN.md", a1.expected_output.text)
        self.assertIn("Deliverable", a1.expected_output.source)

    def test_statuses(self):
        run = self.build()
        self.assertEqual(run.agent("a1").status, C.COMPLETED)
        self.assertEqual(run.agent("a2").status, C.COMPLETED)
        self.assertEqual(run.agent("a3").status, C.RUNNING)

    def test_running_agent_becomes_orphaned_when_session_dies(self):
        run = self.build(now=NOW_DEAD, live=False)
        self.assertEqual(run.agent("a3").status, C.ORPHANED)

    def test_durations(self):
        a1 = self.build().agent("a1")
        self.assertEqual(a1.duration_s, 60.0)
        self.assertIsNone(self.build().agent("a3").duration_s)

    def test_tokens_are_summed_per_agent_and_per_run(self):
        run = self.build()
        self.assertEqual(run.agent("a1").tokens["input"], 100)
        self.assertEqual(run.totals()["tokens"]["input"], 510)

    def test_results_are_captured(self):
        run = self.build()
        self.assertIn("Wrote the plan", run.agent("a1").result)
        self.assertEqual(run.agent("a3").result, "")

    def test_files_written_and_read(self):
        run = self.build()
        self.assertEqual(len(run.agent("a1").files_written), 1)
        self.assertEqual(len(run.agent("a2").files_read), 1)


class TestBatchesAndEdges(BuildTestCase):
    def test_agents_launched_in_one_turn_form_a_batch(self):
        run = self.build()
        waves = {tuple(sorted(b.agent_ids)) for b in run.batches}
        self.assertIn(("a2", "a3"), waves)
        self.assertIn(("a1",), waves)

    def test_spawn_edges_come_from_the_orchestrator(self):
        run = self.build()
        spawn = {(e.src, e.dst) for e in run.edges if e.kind == "spawn"}
        self.assertEqual(spawn, {("main", "a1"), ("main", "a2"), ("main", "a3")})

    def test_artifact_edges_follow_the_plan_file(self):
        run = self.build()
        artifact = {(e.src, e.dst) for e in run.edges if e.kind == "artifact"}
        self.assertEqual(artifact, {("a1", "a2"), ("a1", "a3")})

    def test_handoff_folds_into_the_artifact_edge_for_a2(self):
        run = self.build()
        edge = [e for e in run.edges
                if (e.src, e.dst) == ("a1", "a2") and e.kind == "artifact"][0]
        self.assertIn("handoff", edge.evidence)

    def test_summary_dict_is_json_serializable(self):
        import json
        json.dumps(self.build().to_summary_dict())


class TestBackgroundSubagentKeepsTheSessionLive(BuildTestCase):
    """The main transcript can sit untouched while a background subagent works; its entries are activity."""

    def quiet_main_session(self, agent_entry_at, spool=None, main_touched_at=None):
        """a3 writes an entry at `agent_entry_at` seconds; the main transcript was last touched long before
        (or at `main_touched_at`), and `now` is a minute after a3's entry."""
        path = os.path.join(self.paths.subagents_dir, "agent-a3.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(agent_entry([{"type": "text", "text": "still working"}], agent_entry_at)) + "\n")
        now = parse_timestamp(ts(agent_entry_at)) + 60
        touched = main_touched_at if main_touched_at is not None else now - 3 * C.SESSION_LIVE_THRESHOLD_S
        os.utime(self.paths.session_jsonl, (touched, touched))
        return RunBuilder(self.paths, now_fn=lambda: now, spool=spool).refresh()

    def test_a_subagent_active_a_minute_ago_is_running_not_cut_off(self):
        run = self.quiet_main_session(900)
        self.assertTrue(run.session_live)
        self.assertEqual(run.agent("a3").status, C.RUNNING)
        self.assertEqual([c for c in run.advice["cards"] if c["rule"] == "failure"], [])

    def test_a_session_nobody_touched_for_ten_minutes_is_still_over(self):
        run = self.build(now=NOW_DEAD, live=False)         # every entry in the fixture is older than that
        self.assertFalse(run.session_live)
        self.assertEqual(run.agent("a3").status, C.ORPHANED)
        self.assertEqual([c["title"] for c in run.advice["cards"] if c["rule"] == "failure"],
                         ["Stopped when the session ended"])

    def test_session_end_is_a_fact_whatever_a_subagent_wrote_after_it(self):
        spool = EventSpool(tempfile.mkdtemp())
        ended = parse_timestamp(ts(850))
        spool.append(Event(kind=SESSION_END, session_id=self.paths.session_id, ts=ended, detail={"reason": "other"}))
        run = self.quiet_main_session(900, spool=spool, main_touched_at=ended - 5)
        self.assertFalse(run.session_live)


class TestIncrementalRefresh(BuildTestCase):
    def test_second_refresh_picks_up_new_activity(self):
        builder = RunBuilder(self.paths, now_fn=lambda: NOW_LIVE)
        os.utime(self.paths.session_jsonl, (NOW_LIVE, NOW_LIVE))
        first = builder.refresh()
        self.assertEqual(first.agent("a3").tokens.get("output", 0), 0)

        path = os.path.join(self.paths.subagents_dir, "agent-a3.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            import json
            fh.write(json.dumps(agent_entry(
                [{"type": "text", "text": "Implemented part two."}], 145,
                usage={"output_tokens": 33})) + "\n")

        second = builder.refresh()
        self.assertEqual(second.agent("a3").tokens["output"], 33)

    def test_truncated_subagent_log_does_not_double_count_tokens(self):
        import json

        builder = RunBuilder(self.paths, now_fn=lambda: NOW_LIVE)
        os.utime(self.paths.session_jsonl, (NOW_LIVE, NOW_LIVE))
        path = os.path.join(self.paths.subagents_dir, "agent-a3.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(agent_entry(
                [{"type": "text", "text": "Implemented part two."}], 145,
                usage={"output_tokens": 33})) + "\n")
        first = builder.refresh()
        self.assertEqual(first.agent("a3").tokens["output"], 33)

        # The file gets replaced with a shorter one carrying the same entry
        # (e.g. Claude Code rewriting/compacting it) — the digest must be
        # rebuilt from scratch, not have the old total added on top.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(agent_entry(
                [{"type": "text", "text": "Implemented part two."}], 145,
                usage={"output_tokens": 33})) + "\n")

        second = builder.refresh()
        self.assertEqual(second.agent("a3").tokens["output"], 33)

    def test_replaced_main_transcript_drops_stale_launches(self):
        import json

        builder = RunBuilder(self.paths, now_fn=lambda: NOW_LIVE)
        os.utime(self.paths.session_jsonl, (NOW_LIVE, NOW_LIVE))
        builder.refresh()
        self.assertTrue(builder._parent.launches)

        # The main transcript is replaced by a shorter one that launches nothing.
        # Launches/results from the old file must not outlive it.
        with open(self.paths.session_jsonl, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "cwd": r"E:\other",
                                 "timestamp": ts(1)}) + "\n")
        builder.refresh()
        self.assertEqual(builder._parent.launches, {})
        self.assertEqual(builder._parent.results, {})
        self.assertEqual(builder._parent.cwd, r"E:\other")

    def test_diagnostics_report_unparsable_lines(self):
        path = os.path.join(self.paths.subagents_dir, "agent-a3.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        run = self.build()
        self.assertEqual(run.diagnostics["unparsable_lines"], 1)


class TestNestedAgents(unittest.TestCase):
    """spawnDepth > 1 has no real-world sample yet; this is its only coverage."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.paths = build_nested_session(self.root)

    def build(self):
        os.utime(self.paths.session_jsonl, (NOW_LIVE, NOW_LIVE))
        return RunBuilder(self.paths, now_fn=lambda: NOW_LIVE).refresh()

    def test_both_agents_are_found(self):
        self.assertEqual({a.agent_id for a in self.build().agents}, {"a1", "a1b"})

    def test_nested_agent_records_its_launcher(self):
        run = self.build()
        self.assertIsNone(run.agent("a1").parent_agent_id)
        self.assertEqual(run.agent("a1b").parent_agent_id, "a1")

    def test_spawn_depth_is_preserved(self):
        self.assertEqual(self.build().agent("a1b").spawn_depth, 2)

    def test_spawn_edge_points_from_the_parent_agent(self):
        spawn = {(e.src, e.dst) for e in self.build().edges if e.kind == "spawn"}
        self.assertEqual(spawn, {("main", "a1"), ("a1", "a1b")})

    def test_nested_agent_brief_comes_from_the_inner_launch(self):
        self.assertIn("inner part", self.build().agent("a1b").brief.lower())


class TestQuotedAgentIdsDoNotBecomeAgents(BuildTestCase):
    """Found by running Orchestra against its own session.

    Agent ids are scraped from tool_result TEXT. A transcript that merely
    quotes another session's launch output — which happens whenever anyone
    inspects transcripts, or a subagent reads one — must not invent an agent.
    """

    def test_a_quoted_launch_result_does_not_create_a_phantom_agent(self):
        import json
        quoted = {
            "uuid": "quote-1", "timestamp": ts(90), "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_bash_99",
                 "content": [{"type": "text", "text":
                              "Async agent launched successfully.\n"
                              "agentId: deadbeefcafe1234 (internal ID)\n"}]}]}}
        with open(self.paths.session_jsonl, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(quoted) + "\n")

        run = self.build()
        self.assertEqual({a.agent_id for a in run.agents}, {"a1", "a2", "a3"})
        self.assertIsNone(run.agent("deadbeefcafe1234"))

    def test_a_real_launch_result_still_creates_its_agent(self):
        # The guard must not suppress agents whose meta file has not appeared
        # yet: the launch record is what makes them real.
        run = self.build()
        self.assertIsNotNone(run.agent("a3"))


class TestAForkCannotBecomeItsOwnParent(BuildTestCase):
    """Found by running Orchestra against its own session: a forked agent's
    transcript replays its full inherited history, including the very entry
    that launched it -- tagged, like every entry in that file, with
    isSidechain=True and its own agentId. Read naively, that looks like the
    agent spawning itself, producing a self-loop spawn edge.
    """

    def test_a_replayed_self_launch_does_not_self_parent(self):
        import json
        # a1's own transcript replays the entry that launched a1 in the first
        # place -- exactly as a forked agent's transcript would -- reusing the
        # same tool_use_id ("toolu_1") so it overwrites the real launch record.
        replay = {
            "isSidechain": True, "agentId": "a1", "uuid": "replay-1",
            "timestamp": ts(5), "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Agent",
                 "input": {"description": "Plan the work", "prompt": "..."}}]}}
        path = os.path.join(self.paths.subagents_dir, "agent-a1.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(replay) + "\n")

        run = self.build()
        self.assertIsNone(run.agent("a1").parent_agent_id)
        spawn = {(e.src, e.dst) for e in run.edges if e.kind == "spawn"}
        self.assertIn(("main", "a1"), spawn)
        self.assertNotIn(("a1", "a1"), spawn)


if __name__ == "__main__":
    unittest.main()


class TestConcurrentRefreshDoesNotDoubleCount(BuildTestCase):
    """ThreadingHTTPServer calls refresh() from many threads at once.

    refresh mutates the reader's byte offsets and the per-agent digests, and
    digests accumulate with += and append. Two interleaved refreshes over the
    same new bytes ingest them twice and the builder stays permanently
    poisoned: every later poll reports the inflated figure.
    """

    def _fatten(self, agent_id, entries):
        import json
        path = os.path.join(self.paths.subagents_dir,
                            "agent-{}.jsonl".format(agent_id))
        with open(path, "a", encoding="utf-8") as fh:
            for i in range(entries):
                fh.write(json.dumps(agent_entry(
                    [{"type": "tool_use", "id": "t{}".format(i), "name": "Read",
                      "input": {"file_path": "src/f{}.py".format(i)}}],
                    100 + (i % 40),
                    usage={"input_tokens": 1, "output_tokens": 1})) + "\n")

    def test_parallel_refresh_matches_a_single_threaded_build(self):
        import threading

        self._fatten("a3", 400)

        expected = RunBuilder(self.paths, now_fn=lambda: NOW_LIVE).refresh()
        expected_tokens = expected.totals()["tokens"]
        expected_calls = len(expected.agent("a3").tool_calls)

        shared = RunBuilder(self.paths, now_fn=lambda: NOW_LIVE)
        errors = []

        def hammer():
            try:
                for _ in range(4):
                    shared.refresh()
            except Exception as exc:  # noqa: BLE001 - surfaced by the assert
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [])
        final = shared.refresh()
        self.assertEqual(final.totals()["tokens"], expected_tokens)
        self.assertEqual(len(final.agent("a3").tool_calls), expected_calls)


def _pointer(agent_id):
    """What Claude Code (2.1.277+) puts where the report used to be."""
    return ('This agent\'s report was delivered to you as a message from "{}" '
            '(its SubagentHandback call). Read it there; it is not repeated here.').format(agent_id)


def _handback(message, at, use_id):
    return agent_entry([{"type": "tool_use", "id": use_id, "name": "SubagentHandback",
                         "input": {"message": message}}], at)


class TestSubagentHandback(unittest.TestCase):
    """An agent that reports through SubagentHandback: the parent only gets a pointer.

    aaaa0001  background, reports at 58s, resumed, reports again at 198s
    aaaa0002  launched after aaaa0001, its brief quotes aaaa0001's second report (a handoff)
    aaaa0003  a real report that merely mentions SubagentHandback stays as it is
    """

    REPORT = ("Second pass done. The importer now keeps every row whose match is null in the "
              "errors log instead of the flagged log, the column mapping is resolved per row, "
              "and the workbook writer preserves the original sheet order. All five new tests "
              "pass and the full suite is green on the branch, so the next task can start from "
              "the merged importer without any further changes to the mapping code.")

    def setUp(self):
        self.root = tempfile.mkdtemp()
        project = os.path.join(self.root, "projects", "E--proj")
        sub = os.path.join(project, "h", "subagents")
        session = os.path.join(project, "h.jsonl")
        from tests.fixtures import background_result, launch, notification
        mention = "Documented how SubagentHandback replaces the plain final message, with examples."
        write_jsonl(session, [
            launch("toolu_h1", "Import rows", "Import the rows.\n", 0),
            background_result("toolu_h1", "aaaa0001", 1),
            notification("aaaa0001", "toolu_h1", _pointer("aaaa0001"), 60),
            notification("aaaa0001", "toolu_h1", _pointer("aaaa0001"), 200),
            launch("toolu_h2", "Next task", "Start from this:\n\n" + self.REPORT + "\n", 210,
                   turn="turn-2"),
            background_result("toolu_h2", "aaaa0002", 211),
            launch("toolu_h3", "Write docs", "Write the docs.\n", 0, turn="turn-3"),
            background_result("toolu_h3", "aaaa0003", 1),
            notification("aaaa0003", "toolu_h3", mention, 50),
        ])
        write_jsonl(os.path.join(sub, "agent-aaaa0001.jsonl"), [
            _handback("First pass: imported 40 rows, 3 flagged.", 58, "hb1"),
            {"isSidechain": True, "type": "user", "timestamp": ts(120),
             "message": {"role": "user", "content": "Now handle the null matches."}},
            _handback(self.REPORT, 198, "hb2"),
        ])
        write_jsonl(os.path.join(sub, "agent-aaaa0002.jsonl"), [
            agent_entry([{"type": "text", "text": "Starting."}], 215)])
        write_jsonl(os.path.join(sub, "agent-aaaa0003.jsonl"), [
            _handback("A different message that must not replace the real report.", 49, "hb3")])
        from orchestra.locate import SessionPaths
        paths = SessionPaths(session_id="h", session_jsonl=session, subagents_dir=sub,
                             project_dir=project)
        now = parse_timestamp(ts(230))
        os.utime(session, (now, now))
        self.run_ = RunBuilder(paths, now_fn=lambda: now).refresh()

    def test_the_report_replaces_the_pointer(self):
        first = self.run_.agent("aaaa0001")
        self.assertEqual(first.result, self.REPORT)
        self.assertNotIn("SubagentHandback", first.result)

    def test_each_round_gets_its_own_report(self):
        rounds = self.run_.agent("aaaa0001").rounds
        self.assertEqual([r.result for r in rounds[:2]],
                         ["First pass: imported 40 rows, 3 flagged.", self.REPORT])

    def test_a_repeated_notification_gets_the_same_report(self):
        # Seen on real sessions: the same stop notified twice, 1 ms apart, so the
        # second "round" starts 8 s after the report was handed back.
        from orchestra.model import Round
        from orchestra.status import fill_handbacks
        pointer = _pointer("aaaa0001")
        rounds = [Round(started_at=100.0, ended_at=163.454, result=pointer),
                  Round(started_at=163.454, ended_at=163.455, result=pointer)]
        fill_handbacks(rounds, [(155.4, "The report.")])
        self.assertEqual([r.result for r in rounds], ["The report.", "The report."])

    def test_a_real_report_that_mentions_handback_is_kept(self):
        self.assertTrue(self.run_.agent("aaaa0003").result.startswith("Documented how SubagentHandback"))

    def test_a_handed_back_report_quoted_in_a_brief_is_a_handoff(self):
        kinds = {(e.src, e.dst): e.kind for e in self.run_.edges}
        self.assertIn(("aaaa0001", "aaaa0002"), kinds)
