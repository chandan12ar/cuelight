"""A single-file HTML snapshot: no server, no network, safe to email."""

import base64
import html
import json
import os
from typing import Any, Dict

from orchestra.build import RunBuilder
from orchestra.http import STATIC_DIR
from orchestra.model import Run


def _build_shell() -> str:
    """The report page: index.html itself, with the server-only parts switched off.

    The report used to carry its own copy of the page shell, and the two drifted
    (a missing id once shipped a blank report). Deriving it from index.html means
    there is one shell; every substitution below is asserted, so a change to
    index.html that this function no longer understands fails loudly at import.
    """
    path = os.path.join(STATIC_DIR, "index.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    # The result is str.format()ed, so literal braces (the pre-paint theme
    # script) must be doubled before the real placeholders go in.
    html = html.replace("{", "{{").replace("}", "}}")

    def swap(old: str, new: str) -> None:
        nonlocal html
        if old not in html:
            raise RuntimeError("report shell: index.html no longer contains " + old[:60])
        html = html.replace(old, new, 1)

    swap("<title>Cuelight</title>", "<title>Cuelight report — {title}</title>")
    swap('<link rel="stylesheet" href="style.css">', "<style>\n{css}\n</style>")
    swap('<script src="app.js"></script>\n<script src="tabs.js"></script>\n<script src="advice.js"></script>',
         "<script>\nwindow.ORCHESTRA_RUN = {run_json};\n"
         "window.ORCHESTRA_DETAILS = {details_json};\n"
         "window.ORCHESTRA_AGENT_SPRITE = {agent_sprite_json};\n</script>\n"
         "<script>\n{js}\n</script>")
    # id is load-bearing: app.js sets $("conn").textContent inside poll(), before
    # render(). Without it the whole page throws and stays blank.
    swap('<span id="conn" class="conn"></span>',
         '<span id="conn" class="conn conn-static">static report · session {session}</span>')
    # Nothing to pick, pause or be notified about in a frozen snapshot.
    for tag in ('<select id="session-picker"', '<button id="live-toggle"',
                '<button id="notify-toggle"'):
        swap(tag, tag + " hidden")
    for view in ("fleet", "history"):
        swap('<button type="button" class="tab" data-view="' + view + '"',
             '<button type="button" hidden class="tab" data-view="' + view + '"')
    return html


_SHELL = _build_shell()


def _script_safe(payload: Any) -> str:
    """JSON for embedding inside an inline <script>.

    json.dumps does not escape "<", so a brief containing "</script>" would
    close the script element early: a benign case (an agent discussing HTML)
    truncates the payload into a syntax error and kills the report, and a
    hostile one executes. Escaping "<" is enough, and leaves the JSON valid.
    """
    return json.dumps(payload).replace("<", "\\u003c")


def _read_static(name: str) -> str:
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def _agent_sprite_data_uri() -> str:
    """The character sheet as a data: URI — a report is one file with no
    server to fetch a sibling image from, so the sprite has to travel inside
    it."""
    with open(os.path.join(STATIC_DIR, "agent-sprite.png"), "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    return "data:image/png;base64," + encoded


def _offline_shim(js: str) -> str:
    """Serve the baked-in payload instead of polling, and never schedule a poll."""
    shim = """
api = function (path) {
  if (path.indexOf("/api/agent/") === 0) {
    var id = decodeURIComponent(path.slice("/api/agent/".length));
    var detail = window.ORCHESTRA_DETAILS[id];
    return detail ? Promise.resolve(detail) : Promise.reject(new Error("no detail"));
  }
  if (path.indexOf("/api/run") === 0) return Promise.resolve(window.ORCHESTRA_RUN);
  return Promise.reject(new Error("offline"));
};
state.live = false;
state.offline = true;
"""
    # `api` and `state` are declared with const/let in app.js; rebind via window
    # after the definitions rather than before them.
    return js.replace("const TOKEN =", "var TOKEN =") \
             .replace("function api(path) {", "var api = function (path) {") \
             .replace("\n// ---------------------------------------------------------------- header",
                      "\n" + shim +
                      "\n// ---------------------------------------------------------------- header")


def render_report(run: Run, details: Dict[str, Dict[str, Any]]) -> str:
    return _SHELL.format(
        session=run.session_id,
        # The session's title when Claude Code (or you) gave it one; the id otherwise.
        title=html.escape(run.meta.title if run.meta is not None and run.meta.title else run.session_id),
        css=_read_static("style.css"),
        js=_offline_shim(_read_static("app.js")) + "\n" + _read_static("tabs.js") + "\n" + _read_static("advice.js"),
        run_json=_script_safe(run.to_summary_dict()),
        details_json=_script_safe(details),
        agent_sprite_json=_script_safe(_agent_sprite_data_uri()),
    )


def write_report(builder: RunBuilder, path: str) -> str:
    """Render and write. An empty or directory path gets a session-named default."""
    run = builder.refresh()
    details = {a.agent_id: a.to_detail_dict() for a in run.agents}
    if not path or os.path.isdir(path) or path.endswith((os.sep, "/")):
        directory = path or os.getcwd()
        path = os.path.join(directory,
                            "orchestra-report-{}.html".format(run.session_id))
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_report(run, details))
    return path
