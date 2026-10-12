import os
import re
import unittest

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "orchestra", "static")

EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", re.IGNORECASE)
FETCH_ABSOLUTE = re.compile(r"""fetch\(\s*["'](?:https?:)?//""", re.IGNORECASE)
LOCAL_REF = re.compile(r"""(?:src|href)\s*=\s*["']([^"'#]+)["']""")


def read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


class TestNoNetworkEgress(unittest.TestCase):
    """The local-only guarantee is a hard constraint; this is its enforcement."""

    def test_no_external_src_or_href(self):
        for name in ("index.html", "app.js", "tabs.js", "advice.js", "style.css"):
            self.assertIsNone(EXTERNAL.search(read(name)),
                              "{} reaches an external host".format(name))

    def test_no_absolute_fetch(self):
        for name in ("app.js", "tabs.js", "advice.js"):
            self.assertIsNone(FETCH_ABSOLUTE.search(read(name)), name)

    def test_every_script_the_page_loads_is_checked_here(self):
        scripts = __import__("re").findall(r'<script src="([^"]+)"', read("index.html"))
        self.assertEqual(scripts, ["app.js", "tabs.js", "advice.js"])

    def test_no_font_imports(self):
        self.assertNotIn("@import", read("style.css"))


class TestLocalReferencesResolve(unittest.TestCase):
    def test_every_referenced_file_exists(self):
        for ref in LOCAL_REF.findall(read("index.html")):
            if ref.startswith("data:"):     # inline (the favicon), not a file
                continue
            self.assertTrue(os.path.isfile(os.path.join(STATIC, ref)),
                            "missing asset: {}".format(ref))


class TestPageStructure(unittest.TestCase):
    def test_required_mount_points_exist(self):
        html = read("index.html")
        for element_id in ("totals", "health", "timeline", "graph", "drawer",
                           "session-picker", "diagnostics", "ticker",
                           "view-activity", "filter-text", "filter-status",
                           "conflicts", "scrim", "filter-clear", "filter-count",
                           "notify-toggle", "copy-summary"):
            self.assertIn('id="{}"'.format(element_id), html)

    def test_theme_is_defined_for_light_and_dark(self):
        css = read("style.css")
        self.assertIn(":root", css)
        self.assertIn("prefers-color-scheme: dark", css)

    def test_status_classes_exist_for_every_status(self):
        css = read("style.css")
        for status in ("running", "completed", "failed", "stalled",
                       "orphaned", "unknown"):
            self.assertIn(".s-{}".format(status), css)


class TestGraphAndDrawerPresent(unittest.TestCase):
    def test_functions_task_twelve_referenced_are_defined(self):
        js = read("app.js")
        for name in ("function renderGraph", "function openDrawer",
                     "function layoutGraph"):
            self.assertIn(name, js, "{} is missing".format(name))

    def test_inferred_edges_are_styled_differently(self):
        self.assertIn("edge-inferred", read("app.js"))
        self.assertIn(".edge-inferred", read("style.css"))

    def test_drawer_shows_the_extraction_source(self):
        self.assertIn("expected_output_source", read("app.js"))


class TestNoUnescapedInterpolation(unittest.TestCase):
    """Transcript text reaches innerHTML; every value must pass through esc().

    A tool call's file_path is taken verbatim from the transcript, and `<`,
    `>` and `"` are all legal in a filename — so an unescaped path is live
    HTML in a page that holds the dashboard's API token.
    """

    def _body(self, name):
        js = read("app.js")
        start = js.index("function {}(".format(name))
        return js[start:js.index("\n}", start)]

    def test_show_evidence_escapes_every_interpolated_value(self):
        body = self._body("showEvidence")
        for raw in ("+ e.path", "+ edge.kind", "+ edge.src", "+ edge.dst",
                    "+ e.snippet", "+ e.score", "+ e.run_words"):
            self.assertNotIn(raw, body,
                             "unescaped interpolation in showEvidence: " + raw)
        self.assertIn("esc(e.path)", body)
        self.assertIn("esc(e.snippet)", body)

    def test_snippet_is_escaped_not_stripped(self):
        # The old code deleted < > & from snippets, which mutilates legitimate
        # content instead of rendering it.
        self.assertNotIn('replace(/[<>&]/g, "")', read("app.js"))


class TestGraphLayoutUsesColumnIndex(unittest.TestCase):
    def test_nodes_are_placed_by_column_not_raw_rank(self):
        # Artifact edges can form a cycle, which inflates a raw rank far past
        # the number of occupied columns and pushes nodes outside the viewBox.
        js = read("app.js")
        self.assertNotIn("node.x = 20 + r * (NODE_W + COL_GAP)", js)
        self.assertIn("node.x = 20 + column * (NODE_W + COL_GAP)", js)


class TestPollingCannotOverlap(unittest.TestCase):
    def test_polling_is_generation_guarded(self):
        js = read("app.js")
        self.assertIn("function startPolling()", js)
        self.assertIn("state.generation", js)
        # Every entry point must go through startPolling, never poll() directly.
        self.assertNotIn("  poll();", js)
        self.assertNotIn("if (state.live) poll();", js)


if __name__ == "__main__":
    unittest.main()
