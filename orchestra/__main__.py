"""The /orchestra command line."""

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import webbrowser
from typing import Any, Dict, List, Optional

# /cuelight:open runs this file by its full path, so the command needs no `cd` and
# no blanket shell permission. Run that way, Python puts orchestra/ itself first on
# sys.path, where orchestra/http.py would shadow the standard library's http; put
# the plugin root there instead so `orchestra` imports as a package.
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not __package__:
    sys.path[0] = PLUGIN_ROOT

from orchestra import constants as C
from orchestra.statedir import state_dir

MIN_PYTHON = (3, 9)


def _state_dir() -> str:
    return state_dir()


def portfile_path(session_id: str) -> str:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    return os.path.join(_state_dir(), "{}.json".format(safe or "default"))


def logfile_path(session_id: str) -> str:
    return portfile_path(session_id)[: -len(".json")] + ".log"


def write_portfile(session_id: str, port: int, token: str, pid: int) -> None:
    path = portfile_path(session_id)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"port": port, "token": token, "pid": pid,
                   "session": session_id}, fh)
    # This file holds the API token, and the server's whole threat model is
    # that other local processes are hostile. On POSIX the system temp dir is
    # world-readable (1777) and the default umask would leave this 0644.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_portfile(session_id: str) -> Optional[Dict[str, Any]]:
    try:
        with open(portfile_path(session_id), encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and "port" in info else None


def remove_portfile(session_id: str) -> None:
    try:
        os.remove(portfile_path(session_id))
    except OSError:
        pass


def url_for(info: Dict[str, Any]) -> str:
    return "http://127.0.0.1:{}/?k={}&session={}".format(
        info["port"], info.get("token", ""), info.get("session", ""))


def server_is_alive(info: Dict[str, Any]) -> bool:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:{}/api/health".format(info["port"]), timeout=1):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _kill(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass


# -- commands -------------------------------------------------------------

def _make_spool():
    """A fresh event spool, or None when events are unavailable.

    Hook events are an enhancement. A state directory we cannot safely use must
    degrade the dashboard to transcript-only, not stop it starting.
    """
    from orchestra.events import EventSpool
    try:
        return EventSpool()
    except OSError:
        return None


def _latest_session_in(cwd: str) -> str:
    """Newest session of the project that `cwd` belongs to, or ""."""
    from orchestra.locate import find_project_dir, list_sessions
    project = find_project_dir(cwd)
    sessions = list_sessions(project) if project else []
    return sessions[0].session_id if sessions else ""


def _resolve_session(args) -> str:
    """--session, else $CLAUDE_CODE_SESSION_ID, else the newest in the project.

    The project is --cwd, else the directory this was started in: /cuelight:open
    passes neither flag, so its command has no shell variable in it and Claude
    Code can offer to allow it for good.
    """
    explicit = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if explicit:
        return explicit
    return _latest_session_in(getattr(args, "cwd", "") or os.getcwd())


def _print_safe(text: str) -> None:
    """Print on any console: a character the console's encoding lacks (a Windows code page and an
    agent description with an arrow in it) becomes "?" instead of stopping the command."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, "replace").decode(encoding, "replace"))


def cmd_advise(args) -> int:
    from orchestra import advice
    from orchestra.build import RunBuilder
    from orchestra.locate import find_session
    from orchestra.pricing import default_source

    session_id = _resolve_session(args)
    paths = find_session(session_id) if session_id else None
    if paths is None:
        print("no transcript found for session: {}".format(session_id or "(none)"))
        return 2
    run = RunBuilder(paths, spool=_make_spool(), prices=default_source()).refresh()
    _print_safe(advice.render_text(run))
    return 0


def cmd_serve(args) -> int:
    """Foreground server. This is what the detached child process runs."""
    from orchestra.http import make_server, start_idle_watchdog
    from orchestra.service import OrchestraService

    session_id = _resolve_session(args)
    token = args.token or secrets.token_urlsafe(24)
    from orchestra import history as history_mod
    from orchestra.pricing import default_source
    # History persists data after the session is gone, so it is opt-in only.
    store = history_mod.HistoryStore() if history_mod.enabled() else None
    service = OrchestraService(token=token, default_session=session_id,
                               spool_factory=_make_spool, prices=default_source(),
                               history=store)

    port = args.port
    server = None
    for attempt in range(20):
        try:
            server = make_server(service, port if port else 0)
            break
        except OSError:
            port = (port or C.DEFAULT_PORT) + 1
    if server is None:
        print("could not bind a port", file=sys.stderr)
        return 1

    write_portfile(session_id, server.server_port, token, os.getpid())
    start_idle_watchdog(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        remove_portfile(session_id)
    return 0


def cmd_start(args) -> int:
    session_id = _resolve_session(args)
    if not session_id:
        print("no session id: pass --session or run inside Claude Code")
        return 2

    info = read_portfile(session_id)
    if info and server_is_alive(info):
        print(url_for(info))
        if not args.no_open:
            webbrowser.open(url_for(info))
        return 0
    if info:
        remove_portfile(session_id)

    command = [sys.executable, "-m", "orchestra", "--serve",
               "--session", session_id]
    if args.port:
        command += ["--port", str(args.port)]

    log = open(logfile_path(session_id), "wb")
    # cwd: `-m orchestra` must resolve however this command was started.
    kwargs: Dict[str, Any] = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "cwd": PLUGIN_ROOT}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008) |
                                   getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    finally:
        # The child inherited its own copy of the descriptor; this one only
        # leaked in the parent for the life of the command.
        log.close()

    deadline = time.time() + 5.0
    while time.time() < deadline:
        info = read_portfile(session_id)
        if info and server_is_alive(info):
            print(url_for(info))
            if not args.no_open:
                webbrowser.open(url_for(info))
            return 0
        time.sleep(0.15)

    # A silent non-start is the worst failure for a visibility tool: say why.
    print("orchestra failed to start within 5s")
    try:
        with open(logfile_path(session_id), encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-2000:]
        if tail.strip():
            print(tail)
    except OSError:
        pass
    return 1


def cmd_demo(args) -> int:
    """Serve a synthetic multi-agent session from a throwaway directory.

    Everything lives under one temp directory and is removed on exit; the real
    ~/.claude and the real state directory are never touched.
    """
    import shutil
    import tempfile
    import threading
    from orchestra import demo

    root = tempfile.mkdtemp(prefix="orchestra-demo-")
    os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(root, "claude")
    os.environ["ORCHESTRA_STATE_DIR"] = os.path.join(root, "state")
    os.environ["ORCHESTRA_PRICES"] = os.path.join(root, "prices.json")
    demo.write_prices(os.environ["ORCHESTRA_PRICES"])
    if C.BUDGET <= 0:
        C.BUDGET = 8.0               # so the budget meter has something to show
    paths, agents = demo.build_demo(os.environ["CLAUDE_CONFIG_DIR"])
    demo.write_side_sessions(os.environ["CLAUDE_CONFIG_DIR"])
    spool = _make_spool()
    if spool is not None:
        demo.write_events(spool, paths.session_id)
    simulator = demo.Simulator(paths, agents)
    simulator.start()

    args.session = paths.session_id
    args.token = args.token or secrets.token_urlsafe(24)

    def open_when_ready() -> None:
        deadline = time.time() + 10.0
        while time.time() < deadline:
            info = read_portfile(paths.session_id)
            if info and server_is_alive(info):
                print("demo: " + url_for(info), flush=True)
                if not args.no_open:
                    webbrowser.open(url_for(info))
                return
            time.sleep(0.2)

    threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        return cmd_serve(args)
    finally:
        simulator.stop()
        shutil.rmtree(root, ignore_errors=True)


def cmd_stop(args) -> int:
    session_id = _resolve_session(args)
    info = read_portfile(session_id)
    if not info:
        print("orchestra is not running for this session")
        return 0
    _kill(int(info.get("pid", 0)))
    remove_portfile(session_id)
    print("orchestra stopped")
    return 0


def cmd_report(args) -> int:
    from orchestra.build import RunBuilder
    from orchestra.locate import find_session
    from orchestra.pricing import default_source
    from orchestra.report import write_report

    session_id = _resolve_session(args)
    paths = find_session(session_id)
    if paths is None:
        print("no transcript found for session: {}".format(session_id or "(none)"))
        return 2
    target = args.report or ""
    base = args.cwd or os.getcwd()
    # A relative path means "relative to the project", not to wherever this
    # process happens to be: the slash command runs from the plugin directory.
    if not os.path.isabs(target):
        target = os.path.join(base, target) if target else base + os.sep
    written = write_report(RunBuilder(paths, prices=default_source()), target)
    print(written)
    return 0


def cmd_export(args) -> int:
    from orchestra import export as export_mod
    from orchestra.build import RunBuilder
    from orchestra.locate import find_session
    from orchestra.pricing import default_source

    session_id = _resolve_session(args)
    paths = find_session(session_id)
    if paths is None:
        print("no transcript found for session: {}".format(session_id or "(none)"))
        return 2
    summary = RunBuilder(paths, spool=_make_spool(),
                         prices=default_source()).refresh().to_summary_dict()
    content_type, body, name = export_mod.render(summary, args.export)
    target = args.out or os.path.join(args.cwd or os.getcwd(), name)
    if os.path.isdir(target):
        target = os.path.join(target, name)
    # newline="" so the CSV's own \r\n is written as-is on every platform.
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    print(target)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    if sys.version_info < MIN_PYTHON:
        print("orchestra needs Python {}.{} or newer; this is {}.{}".format(
            MIN_PYTHON[0], MIN_PYTHON[1], sys.version_info[0], sys.version_info[1]))
        return 2

    parser = argparse.ArgumentParser(prog="orchestra", add_help=True)
    parser.add_argument("--session", default="")
    parser.add_argument("--cwd", default="",
                        help="project directory: base for relative report "
                             "paths, and where to look for a session when "
                             "none is given")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", default="")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--demo", action="store_true",
                        help="serve a synthetic multi-agent session to explore the dashboard")
    parser.add_argument("--report", nargs="?", const="", default=None)
    parser.add_argument("--export", choices=("csv", "json"), default=None,
                        help="write the run as CSV (one row per agent) or JSON")
    parser.add_argument("--out", default="", help="file or directory for --export")
    parser.add_argument("--advise", action="store_true",
                        help="print what to change next time: the run's verdict, fix-it cards and a fact sheet")
    args = parser.parse_args(argv)

    if args.demo:
        return cmd_demo(args)
    if args.serve:
        return cmd_serve(args)
    if args.stop:
        return cmd_stop(args)
    if args.advise:
        return cmd_advise(args)
    if args.report is not None:
        return cmd_report(args)
    if args.export:
        return cmd_export(args)
    return cmd_start(args)


if __name__ == "__main__":
    sys.exit(main())
