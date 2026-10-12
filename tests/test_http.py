import json
import tempfile
import unittest
import urllib.error
import urllib.request

from orchestra.http import _host_is_loopback, _origin_is_allowed, serve
from orchestra.parent import parse_timestamp
from orchestra.service import OrchestraService
from tests.fixtures import build_session, ts

TOKEN = "test-token-123"


class HttpTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        build_session(self.root, "s1")
        self.service = OrchestraService(root=self.root, token=TOKEN,
                                        default_session="s1",
                                        now_fn=lambda: parse_timestamp(ts(150)))
        self.server, self.thread = serve(self.service, port=0)
        self.base = "http://127.0.0.1:{}".format(self.server.server_port)
        self.addCleanup(self.server.shutdown)

    def get(self, path, token=TOKEN, host=None):
        url = self.base + path
        if token is not None:
            url += ("&" if "?" in path else "?") + "k=" + token
        request = urllib.request.Request(url)
        if host:
            request.add_header("Host", host)
        return urllib.request.urlopen(request, timeout=5)

    def get_json(self, path, **kw):
        return json.loads(self.get(path, **kw).read().decode("utf-8"))


class TestAuth(HttpTestCase):
    def test_missing_token_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/run", token=None)
        self.assertEqual(ctx.exception.code, 403)

    def test_wrong_token_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/run", token="nope")
        self.assertEqual(ctx.exception.code, 403)

    def test_non_loopback_host_header_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/run", host="evil.example.com")
        self.assertEqual(ctx.exception.code, 403)

    def test_cross_site_origin_is_rejected(self):
        request = urllib.request.Request(
            self.base + "/api/run?k=" + TOKEN,
            headers={"Origin": "https://evil.example.com"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_loopback_origin_is_allowed(self):
        request = urllib.request.Request(
            self.base + "/api/run?k=" + TOKEN,
            headers={"Origin": self.base})
        self.assertEqual(urllib.request.urlopen(request, timeout=5).status, 200)

    def test_static_page_does_not_require_a_token(self):
        self.assertEqual(self.get("/", token=None).status, 200)


class TestApi(HttpTestCase):
    def test_run_returns_the_default_session(self):
        data = self.get_json("/api/run")
        self.assertEqual(data["session_id"], "s1")
        self.assertEqual(data["totals"]["agents"], 3)
        self.assertEqual(len(data["agents"]), 3)

    def test_run_agents_are_light(self):
        agent = self.get_json("/api/run")["agents"][0]
        self.assertNotIn("brief", agent)

    def test_agent_detail_is_heavy(self):
        detail = self.get_json("/api/agent/a1")
        self.assertIn("You are planning", detail["brief"])
        self.assertIn("PLAN.md", detail["expected_output"])
        self.assertIn("tool_calls", detail)

    def test_unknown_agent_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/agent/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_unknown_session_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/run?session=missing")
        self.assertEqual(ctx.exception.code, 404)

    def test_sessions_list(self):
        data = self.get_json("/api/sessions")
        self.assertEqual(data["sessions"][0]["session_id"], "s1")
        self.assertEqual(data["sessions"][0]["agent_count"], 3)

    def test_calls_searches_every_tool_call(self):
        data = self.get_json("/api/calls?q=plan.md")
        self.assertGreater(data["matched"], 0)
        self.assertTrue(all("plan.md" in r["target"].lower() for r in data["rows"]))
        self.assertEqual(self.get_json("/api/calls?failed=1")["failed_only"], True)
        self.assertEqual(self.get_json("/api/calls?q=x")["rows"], [])

    def test_calls_needs_the_token(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/api/calls?q=plan", token=None)

    def test_health_needs_no_token(self):
        self.assertEqual(self.get("/api/health", token=None).status, 200)

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nothing")
        self.assertEqual(ctx.exception.code, 404)


class TestStatic(HttpTestCase):
    def test_index_is_html(self):
        response = self.get("/", token=None)
        self.assertIn("text/html", response.headers["Content-Type"])

    def test_path_traversal_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/../../../etc/passwd", token=None)
        self.assertIn(ctx.exception.code, (403, 404))


class TestBuilderReuse(unittest.TestCase):
    def test_same_session_reuses_one_builder(self):
        root = tempfile.mkdtemp()
        build_session(root, "s1")
        service = OrchestraService(root=root, token=TOKEN, default_session="s1",
                                   now_fn=lambda: parse_timestamp(ts(150)))
        service.run_summary("s1")
        first = service._builders["s1"]
        service.run_summary("s1")
        self.assertIs(service._builders["s1"], first)


class TestBuilderEviction(unittest.TestCase):
    def _service(self, sessions, max_builders, default="s0"):
        root = tempfile.mkdtemp()
        for name in sessions:
            build_session(root, name)
        return OrchestraService(root=root, token=TOKEN, default_session=default,
                                max_builders=max_builders,
                                now_fn=lambda: parse_timestamp(ts(150)))

    def test_least_recently_used_builder_is_evicted(self):
        service = self._service(["s0", "s1", "s2", "s3"], max_builders=3)
        for name in ("s0", "s1", "s2"):
            service.run_summary(name)
        service.run_summary("s0")            # s1 is now the oldest
        service.run_summary("s3")
        self.assertEqual(set(service._builders), {"s0", "s2", "s3"})

    def test_default_session_is_never_evicted(self):
        service = self._service(["s0", "s1", "s2", "s3"], max_builders=2)
        for name in ("s0", "s1", "s2", "s3"):
            service.run_summary(name)
        self.assertIn("s0", service._builders)
        self.assertLessEqual(len(service._builders), 2)

    def test_an_evicted_session_is_rebuilt_with_the_same_answer(self):
        service = self._service(["s0", "s1", "s2"], max_builders=1)
        before = service.run_summary("s1")["totals"]
        service.run_summary("s2")
        self.assertNotIn("s1", service._builders)
        self.assertEqual(service.run_summary("s1")["totals"], before)


class TestHostnameParsing(unittest.TestCase):
    """Hand-rolled colon splitting got this wrong in both directions."""

    def test_userinfo_cannot_disguise_a_foreign_host(self):
        # The real host here is evil.com; the loopback part is userinfo.
        self.assertFalse(_host_is_loopback("127.0.0.1:8080@evil.com"))
        self.assertFalse(_origin_is_allowed("http://127.0.0.1:1234@evil.com"))

    def test_ipv6_loopback_with_a_port_is_accepted(self):
        self.assertTrue(_host_is_loopback("[::1]:1234"))
        self.assertTrue(_host_is_loopback("[::1]"))

    def test_ipv4_loopback_with_and_without_port(self):
        self.assertTrue(_host_is_loopback("127.0.0.1"))
        self.assertTrue(_host_is_loopback("127.0.0.1:7717"))
        self.assertTrue(_host_is_loopback("localhost:7717"))

    def test_lookalike_domains_are_rejected(self):
        self.assertFalse(_host_is_loopback("localhost.evil.com"))
        self.assertFalse(_host_is_loopback("127.0.0.1.evil.com"))
        self.assertFalse(_host_is_loopback("evil.com"))
        self.assertFalse(_host_is_loopback(""))
        self.assertFalse(_host_is_loopback(None))

    def test_malformed_host_is_not_loopback(self):
        self.assertFalse(_host_is_loopback("[::1"))
        self.assertFalse(_host_is_loopback("http://[oops"))

    def test_absent_or_null_origin_is_allowed(self):
        self.assertTrue(_origin_is_allowed(None))
        self.assertTrue(_origin_is_allowed(""))
        self.assertTrue(_origin_is_allowed("null"))
        self.assertTrue(_origin_is_allowed("http://127.0.0.1:7717"))


class TestHostOriginOverHttp(HttpTestCase):
    def test_userinfo_host_is_refused_by_the_server(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/run", host="127.0.0.1:8080@evil.com")
        self.assertEqual(ctx.exception.code, 403)


class TestUnexpectedErrorsStillAnswer(HttpTestCase):
    def test_an_unexpected_exception_returns_500_not_an_empty_response(self):
        def boom(*args, **kwargs):
            raise RuntimeError("simulated corrupt transcript")

        self.service.run_summary = boom
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/run")
        self.assertEqual(ctx.exception.code, 500)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertIn("error", body)
        # The body must never echo the exception text: it can carry
        # transcript content.
        self.assertNotIn("corrupt transcript", json.dumps(body))

    def test_the_server_still_serves_after_an_error(self):
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        original = self.service.run_summary
        self.service.run_summary = boom
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/run")
        ctx.exception.close()
        self.service.run_summary = original
        response = self.get("/api/health", token=None)
        self.assertEqual(response.status, 200)
        response.close()


class TestUsualRoute(HttpTestCase):
    def test_usual_without_baselines_says_off(self):
        self.assertEqual(self.get_json("/api/usual")["state"], "off")


if __name__ == "__main__":
    unittest.main()
