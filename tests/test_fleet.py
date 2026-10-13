import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request

from orchestra import events as E
from orchestra.events import Event, EventSpool
from orchestra.http import serve
from orchestra.locate import list_recent_sessions
from orchestra.parent import parse_timestamp
from orchestra.service import OrchestraService
from tests.fixtures import build_session, ts

T = parse_timestamp(ts(0))
NOW = T + 150
TOKEN = "tok-fleet"


def at(offset):
    return T + offset


class FleetCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.spool_dir = os.path.join(tempfile.mkdtemp(), "events")
        self.spool = EventSpool(self.spool_dir)

    def session(self, session_id, project="E--proj", mtime=NOW):
        """build_session always writes under E--proj; relocate for other projects."""
        scratch = tempfile.mkdtemp()
        build_session(scratch, session_id)
        target = os.path.join(self.root, "projects", project)
        os.makedirs(target, exist_ok=True)
        os.rename(os.path.join(scratch, "projects", "E--proj", session_id + ".jsonl"),
                  os.path.join(target, session_id + ".jsonl"))
        os.rename(os.path.join(scratch, "projects", "E--proj", session_id),
                  os.path.join(target, session_id))
        path = os.path.join(target, session_id + ".jsonl")
        os.utime(path, (mtime, mtime))
        return path

    def service(self, now=NOW, **kw):
        return OrchestraService(
            root=self.root, token=TOKEN, default_session=kw.pop("default", "s1"),
            now_fn=lambda: now,
            spool_factory=lambda: EventSpool(self.spool_dir), **kw)

    def permission(self, session_id, offset=145):
        self.spool.append(Event(
            kind=E.NOTIFICATION, session_id=session_id, ts=at(offset),
            detail={"notification_type": "permission_prompt",
                    "message": "needs permission"}))


class TestListRecentSessions(FleetCase):
    def test_spans_projects_newest_first(self):
        self.session("a", "E--one", mtime=NOW - 100)
        self.session("b", "E--two", mtime=NOW - 10)
        got = list_recent_sessions(self.root, 3600, NOW)
        self.assertEqual([s.session_id for s in got], ["b", "a"])

    def test_old_sessions_are_excluded(self):
        self.session("old", mtime=NOW - 99999)
        self.assertEqual(list_recent_sessions(self.root, 3600, NOW), [])

    def test_limit_is_honoured(self):
        for i in range(5):
            self.session("s%d" % i, mtime=NOW - i)
        self.assertEqual(len(list_recent_sessions(self.root, 3600, NOW, limit=2)), 2)

    def test_missing_root_is_empty(self):
        self.assertEqual(list_recent_sessions(os.path.join(self.root, "x"), 3600, NOW), [])


class TestFleet(FleetCase):
    def test_lists_every_recent_session_across_projects(self):
        self.session("s1", "E--one")
        self.session("s2", "E--two")
        fleet = self.service().fleet()
        self.assertEqual({s["session_id"] for s in fleet["sessions"]}, {"s1", "s2"})

    def test_a_session_waiting_on_permission_sorts_first_and_is_counted(self):
        self.session("s1", mtime=NOW)
        self.session("s2", mtime=NOW - 5)
        self.permission("s2")                     # older session, but blocked
        fleet = self.service().fleet()
        self.assertEqual(fleet["sessions"][0]["session_id"], "s2")
        self.assertEqual(fleet["sessions"][0]["attention"]["kind"], "permission")
        self.assertEqual(fleet["attention_count"], 1)

    def test_error_outranks_input_and_idle_is_not_urgent(self):
        for sid in ("a", "b", "c"):
            self.session(sid)
        self.spool.append(Event(kind=E.ERROR, session_id="a", ts=at(146),
                                detail={"error_type": "rate_limit"}))
        self.spool.append(Event(kind=E.NOTIFICATION, session_id="b", ts=at(146),
                                detail={"notification_type": "agent_needs_input"}))
        self.spool.append(Event(kind=E.NOTIFICATION, session_id="c", ts=at(146),
                                detail={"notification_type": "idle_prompt"}))
        fleet = self.service(default="a").fleet()
        self.assertEqual([s["session_id"] for s in fleet["sessions"][:2]], ["a", "b"])
        self.assertEqual(fleet["attention_count"], 2)

    def test_project_name_comes_from_the_working_directory(self):
        self.session("s1")
        entry = self.service().fleet()["sessions"][0]
        self.assertEqual(entry["project_path"], r"E:\proj")
        self.assertEqual(entry["project_name"], "proj")

    def test_totals_are_summarised(self):
        self.session("s1")
        totals = self.service().fleet()["sessions"][0]["totals"]
        self.assertEqual((totals["agents"], totals["completed"], totals["running"]),
                         (3, 2, 1))

    def test_an_ended_session_is_never_urgent(self):
        self.session("s1", mtime=NOW - 99999 + 86400 * 0)   # touched long ago...
        self.permission("s1")
        # ...but inside the fleet window, so it is listed -- and not live. Every agent's latest entry counts
        # as activity too (a background agent works while the main thread is idle), so the file's time is
        # not enough here: it is the clock that has moved on, past the newest entry of any agent.
        from orchestra import constants as C
        os.utime(os.path.join(self.root, "projects", "E--proj", "s1.jsonl"),
                 (NOW - C.SESSION_LIVE_THRESHOLD_S - 60,) * 2)
        entry = self.service(now=NOW + C.SESSION_LIVE_THRESHOLD_S + 60).fleet()["sessions"][0]
        self.assertFalse(entry["session_live"])
        self.assertEqual(entry["urgency"], 0)

    def test_sessions_outside_the_window_are_not_listed(self):
        self.session("s1", mtime=NOW - 10 * 3600)
        self.assertEqual(self.service().fleet()["sessions"], [])

    def test_builds_only_within_budget_and_never_evicts_the_viewed_session(self):
        for i in range(4):
            self.session("s%d" % i, mtime=NOW - i)
        service = self.service(default="s3", max_builders=3)
        service.run_summary("s3")                 # the user is viewing s3
        fleet = service.fleet()
        built = [s for s in fleet["sessions"] if s["totals"] is not None]
        self.assertEqual(len(built), 2)           # budget = max_builders - 1
        self.assertIn("s3", service._builders)
        self.assertLessEqual(len(service._builders), 3)

    def test_unbuilt_entries_still_carry_identity_and_liveness(self):
        for i in range(3):
            self.session("s%d" % i, mtime=NOW - i)
        fleet = self.service(max_builders=2).fleet()
        lone = [s for s in fleet["sessions"] if s["totals"] is None][0]
        self.assertTrue(lone["session_id"])
        self.assertTrue(lone["session_live"])

    def test_event_text_is_scrubbed(self):
        self.session("s1")
        self.spool.append(Event(
            kind=E.NOTIFICATION, session_id="s1", ts=at(146),
            detail={"notification_type": "permission_prompt",
                    "message": "token sk-ant-api03-" + "Q" * 40}))
        entry = self.service().fleet()["sessions"][0]
        self.assertNotIn("sk-ant-api03", json.dumps(entry))


class TestFleetEndpoint(FleetCase):
    def setUp(self):
        super().setUp()
        self.session("s1")
        self.server, _ = serve(self.service(), port=0)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def test_requires_the_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/fleet", timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_returns_the_fleet_as_json(self):
        body = urllib.request.urlopen(
            self.base + "/api/fleet?k=" + TOKEN, timeout=5).read()
        data = json.loads(body)
        self.assertEqual(data["sessions"][0]["session_id"], "s1")
        self.assertIn("attention_count", data)


if __name__ == "__main__":
    unittest.main()


class TestFleetUiWiring(unittest.TestCase):
    STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "orchestra", "static")

    def read(self, name):
        with open(os.path.join(self.STATIC, name), encoding="utf-8") as fh:
            return fh.read()

    def test_page_has_the_tab_badge_and_view(self):
        html = self.read("index.html")
        for needle in ('data-view="fleet"', 'id="fleet-badge"',
                       'id="view-fleet"', 'id="fleet"'):
            self.assertIn(needle, html)

    def test_report_shell_has_the_mount_points_but_hides_the_tab(self):
        from orchestra import report
        for needle in ('id="fleet-badge"', 'id="view-fleet"', 'id="fleet"'):
            self.assertIn(needle, report._SHELL)
        tab = report._SHELL[report._SHELL.index('data-view="fleet"') - 60:
                            report._SHELL.index('data-view="fleet"')]
        self.assertIn("hidden", tab)     # a frozen snapshot has no other sessions

    def test_session_picker_and_fleet_share_one_switch_function(self):
        js = self.read("app.js")
        self.assertIn("function switchSession(", js)
        self.assertIn('onchange = (event) => switchSession(event.target.value)', js)
        self.assertIn("switchSession(row.dataset.session)", js)

    def test_switching_reseeds_every_history_baseline(self):
        js = self.read("app.js")
        body = js[js.index("function switchSession("):]
        body = body[:body.index("\n}\n")]
        for reset in ("seenEdgeKeys", "graphSeeded", "notifySeeded",
                      "knownFailedIds", "knownAttention", "floorSeeded",
                      "agentPrevStatus", "startPolling()", "startStream()"):
            self.assertIn(reset, body)

    def test_fleet_text_is_escaped(self):
        # renderFleet joins rows built by fleetRow; every transcript-derived value there is escaped.
        js = self.read("app.js")
        self.assertIn("data.sessions.map((s) => fleetRow(s, current))", js)
        body = js[js.index("function fleetRow("):]
        body = body[:body.index("\n}\n")]
        for raw in ("+ s.project_name", "+ s.session_id", "+ s.title", "+ sub.text", "+ meta +"):
            self.assertNotIn(raw, body)
        for escaped in ("esc(s.project_name", "esc(s.title)", "esc(sub.text)", "esc(meta)"):
            self.assertIn(escaped, body)

    def test_fleet_polling_follows_the_live_toggle_and_skips_reports(self):
        js = self.read("app.js")
        body = js[js.index("async function pollFleet("):]
        body = body[:body.index("\n}\n")]
        self.assertIn("state.live && !state.offline", body)
