"""Command palette logic and keyboard rules, executed under node."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

NODE = shutil.which("node")
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "orchestra", "static")

SETUP = """
global.document = {createElement: () => {
  let text = "";
  return {set textContent(v) { text = String(v); },
          get innerHTML() { return text.replace(/&/g, "&amp;").replace(/</g, "&lt;")
                                       .replace(/>/g, "&gt;"); }};
}};
const $ = () => null;
const state = {offline: false, live: true, run: null};
const calls = [];
const setView = (v) => calls.push(["view", v]);
const openDrawer = (id) => calls.push(["drawer", id]);
const toggleReplay = () => calls.push(["replay"]);
const cycleTheme = () => calls.push(["theme"]);
const copySummary = () => calls.push(["copy"]);
const downloadExport = (f) => calls.push(["export", f]);
const openHelp = () => calls.push(["help"]);
const closeHelp = () => {};
"""

FNS = ("esc", "fmtDuration", "fmtModelShort", "statusVar", "isMac", "viewAvailable", "matchTerms",
       "matchesAll", "highlight", "baseName", "paletteCommands", "agentItem", "buildPaletteItems",
       "localSearch", "isTyping", "helpOpen", "openPalette", "onGlobalKey")


def read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def fn(js, name):
    start = js.index("function {}(".format(name))
    return js[start:js.index("\n}\n", start) + 3]


def const(js, name):
    start = js.index("const {} =".format(name))
    end = js.index(";\n", start) + 2
    return js[start:end]


def run_js(body):
    js = read("app.js")
    prelude = "\n".join([SETUP, const(js, "PALETTE_VIEWS"), const(js, "palette"),
                         const(js, "SHORTCUTS")] + [fn(js, n) for n in FNS])
    path = os.path.join(tempfile.mkdtemp(), "p.js")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(prelude + "\n" + body)
    proc = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=30)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr[-1500:])
    return json.loads(proc.stdout)


def agent(aid, desc, status="completed", **extra):
    base = {"agent_id": aid, "description": desc, "status": status, "agent_type": "general-purpose",
            "model": "claude-sonnet-5-5", "duration_s": 60.0, "objective": "", "loop": None}
    base.update(extra)
    return base


AGENTS = [agent("a1", "Build the checkout UI", "running"),
          agent("a2", "Write the unit tests", "failed"),
          agent("a3", "Review security", "stalled"),
          agent("a4", "Plan the work", "completed"),
          agent("a5", "Fix tests", "running", loop={"kind": "repeat"})]


def items(query="", offline=False, remote=None):
    body = ("state.offline = %s; state.run = {agents: %s}; palette.query = %s; palette.remote = %s;"
            "console.log(JSON.stringify(buildPaletteItems().map((i) => [i.group, i.title])));"
            % (json.dumps(offline), json.dumps(AGENTS), json.dumps(query), json.dumps(remote)))
    return run_js(body)


@unittest.skipIf(NODE is None, "node is not on PATH")
class TestResults(unittest.TestCase):
    def test_empty_query_offers_views_attention_and_actions(self):
        out = items("")
        groups = [g for g, _ in out]
        self.assertEqual(groups[:7], ["Go to"] * 7)
        attention = [t for g, t in out if g == "Needs attention"]
        self.assertEqual(sorted(attention), sorted(["Write the unit tests", "Review security", "Fix tests"]))
        self.assertIn("Actions", groups)
        self.assertNotIn("Plan the work", attention)        # healthy agents are not attention

    def test_agent_search_is_instant_and_matches_every_term(self):
        self.assertEqual([t for g, t in items("unit tests") if g == "Agents"], ["Write the unit tests"])
        self.assertEqual([t for g, t in items("tests") if g == "Agents"], ["Write the unit tests", "Fix tests"])
        self.assertEqual([t for g, t in items("failed") if g == "Agents"], ["Write the unit tests"])

    def test_commands_match_by_name(self):
        self.assertIn(["Commands", "Replay the run"], items("replay"))
        self.assertIn(["Commands", "Insights"], items("insights"))

    def test_remote_results_show_only_for_the_same_query(self):
        remote = {"query": "checkout", "data": {
            "tools": [{"agent_id": "a1", "tool": "Edit", "target": "/p/Checkout.tsx", "description": "UI"}],
            "files": [{"path": "/p/Checkout.tsx", "writers": ["a1"], "readers": ["a2"]}]}}
        shown = items("checkout", remote=remote)
        self.assertIn(["Tool calls", "Edit  Checkout.tsx"], shown)
        self.assertIn(["Files", "Checkout.tsx"], shown)
        stale = items("checkout x", remote=remote)
        self.assertNotIn("Tool calls", [g for g, _ in stale])

    def test_a_static_report_hides_fleet_and_history(self):
        titles = [t for g, t in items("", offline=True) if g == "Go to"]
        self.assertNotIn("Fleet", titles)
        self.assertNotIn("History", titles)
        self.assertIn("Insights", titles)


@unittest.skipIf(NODE is None, "node is not on PATH")
class TestSafetyAndSearch(unittest.TestCase):
    def test_highlight_marks_terms_and_escapes_everything_else(self):
        out = run_js('console.log(JSON.stringify([highlight("a <b>Fix</b> tests", ["fix"]), '
                     'highlight("<script>x</script>", []), highlight("x", ["(", "["]), '
                     'highlight("a.b*c", [".", "*"])]));')
        self.assertEqual(out[0], "a &lt;b&gt;<mark>Fix</mark>&lt;/b&gt; tests")
        self.assertEqual(out[1], "&lt;script&gt;x&lt;/script&gt;")
        self.assertEqual(out[2], "x")                       # regex metacharacters are inert
        self.assertEqual(out[3], "a<mark>.</mark>b<mark>*</mark>c")

    def test_base_name(self):
        out = run_js('console.log(JSON.stringify(["/a/b/c.ts", "C:\\\\x\\\\y.md", "dir/", "solo", ""].map(baseName)));')
        self.assertEqual(out, ["c.ts", "y.md", "dir", "solo", ""])

    def test_local_search_over_report_details(self):
        details = {"a1": {"description": "UI", "tool_calls": [
            {"name": "Edit", "target": "/p/Checkout.tsx", "timestamp": 5},
            {"name": "Bash", "target": "npm test", "timestamp": 9}],
            "files_written": ["/p/Checkout.tsx"], "files_read": []},
            "a2": {"description": "Tests", "tool_calls": [], "files_written": [],
                   "files_read": ["/p/Checkout.tsx"]}}
        out = run_js("global.window = {ORCHESTRA_DETAILS: %s};"
                     "console.log(JSON.stringify(localSearch('checkout')));" % json.dumps(details))
        self.assertEqual([t["tool"] for t in out["tools"]], ["Edit"])
        self.assertEqual(out["files"][0]["writers"], ["a1"])
        self.assertEqual(out["files"][0]["readers"], ["a2"])


@unittest.skipIf(NODE is None, "node is not on PATH")
class TestKeyboard(unittest.TestCase):
    def keys(self, *events):
        body = ("global.window = {};"
                "const log = []; const origOpen = openPalette;"
                "const evs = %s;"
                "for (const e of evs) { e.preventDefault = () => log.push('prevent'); "
                "e.target = e.target || {tagName: 'BODY'}; onGlobalKey(e); }"
                "console.log(JSON.stringify({calls: calls, log: log, open: palette.open}));"
                % json.dumps(list(events)))
        return run_js(body)

    def test_number_keys_switch_views(self):
        out = self.keys({"key": "3"}, {"key": "5"}, {"key": "0"}, {"key": "1"})
        self.assertEqual(out["calls"], [["view", "agents"], ["view", "spend"], ["view", "history"],
                                        ["view", "timeline"]])

    def test_a_opens_advice(self):
        out = self.keys({"key": "a"}, {"key": "A"}, {"key": "a", "target": {"tagName": "INPUT"}})
        self.assertEqual(out["calls"], [["view", "advice"], ["view", "advice"]])

    def test_letters_run_their_commands(self):
        out = self.keys({"key": "t"}, {"key": "r"}, {"key": "?"})
        self.assertEqual(out["calls"], [["theme"], ["replay"], ["help"]])

    def test_typing_in_a_field_never_triggers_shortcuts(self):
        out = self.keys({"key": "3", "target": {"tagName": "INPUT"}},
                        {"key": "t", "target": {"tagName": "TEXTAREA"}},
                        {"key": "r", "target": {"tagName": "SELECT"}},
                        {"key": "x", "target": {"tagName": "DIV", "isContentEditable": True}})
        self.assertEqual(out["calls"], [])

    def test_modified_keys_are_left_to_the_browser(self):
        out = self.keys({"key": "3", "ctrlKey": True}, {"key": "r", "metaKey": True},
                        {"key": "t", "altKey": True})
        self.assertEqual(out["calls"], [])

    def test_a_static_report_ignores_fleet_and_history_keys(self):
        out = run_js("global.window = {}; state.offline = true;"
                     "onGlobalKey({key: '9', target: {tagName: 'BODY'}, preventDefault() {}});"
                     "onGlobalKey({key: '0', target: {tagName: 'BODY'}, preventDefault() {}});"
                     "console.log(JSON.stringify(calls));")
        self.assertEqual(out, [])

    def test_a_key_a_focused_control_already_used_is_left_alone(self):
        # The graph fits itself on 0 and calls preventDefault; 0 must not also jump to History.
        out = self.keys({"key": "0", "defaultPrevented": True}, {"key": "t", "defaultPrevented": True},
                        {"key": "2"})
        self.assertEqual(out["calls"], [["view", "graph"]])

    def test_ctrl_k_and_slash_are_intercepted(self):
        out = self.keys({"key": "k", "ctrlKey": True})
        self.assertIn("prevent", out["log"])


if __name__ == "__main__":
    unittest.main()
