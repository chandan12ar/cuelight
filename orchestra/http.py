"""The local server. Loopback only, token-gated, no egress."""

import hmac
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from orchestra import constants as C
from orchestra.service import NotFound, OrchestraService

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                  ".js": "text/javascript; charset=utf-8",
                  ".css": "text/css; charset=utf-8",
                  ".png": "image/png"}

_LOOPBACK = ("127.0.0.1", "localhost", "::1")

# Live push (Server-Sent Events).
STREAM_POLL_S = 0.4         # how often a stream checks for changes
STREAM_HEARTBEAT_S = 15.0   # comment line so proxies/clients see the link alive
STREAM_MAX_S = 3600.0       # then close; EventSource reconnects. Reaps vanished clients.
MAX_STREAMS = 8


def _hostname_of(value: str, is_url: bool) -> Optional[str]:
    """The real hostname, per URL rules — lowercased, unbracketed, no userinfo.

    Hand-rolled colon splitting gets this wrong in both directions: it reads
    "127.0.0.1:8080@evil.com" as loopback (the userinfo hides the real host),
    and it rejects "[::1]:1234" because IPv6 has more than one colon.
    A Host header is not a URL, so it is prefixed to make one.
    """
    try:
        return urlsplit(value if is_url else "//" + value).hostname
    except ValueError:
        return None


def _host_is_loopback(header: Optional[str]) -> bool:
    if not header:
        return False
    return _hostname_of(header, is_url=False) in _LOOPBACK


def _origin_is_allowed(header: Optional[str]) -> bool:
    """A cross-site page must not be able to read the dashboard's JSON.

    No Origin at all is fine: that is a same-origin navigation or a direct
    fetch. An Origin naming somewhere other than loopback is a cross-site
    read attempt and is refused.
    """
    if not header or header == "null":
        return True
    return _hostname_of(header, is_url=True) in _LOOPBACK


def make_handler(service: OrchestraService, state: Dict[str, Any]):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Cuelight"

        def log_message(self, fmt, *args):  # silence the default stderr spam
            pass

        # -- helpers ----------------------------------------------------
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: Dict[str, Any]) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, code: int, message: str) -> None:
            self._json(code, {"error": message})

        def _authorized(self, query: Dict[str, list]) -> bool:
            supplied = (query.get("k", [""])[0]
                        or self.headers.get("X-Orchestra-Token", ""))
            if not service.token:
                return False
            # compare_digest, not ==: this is the only gate on the user's
            # prompts and results.
            return hmac.compare_digest(supplied, service.token)

        # -- routing ----------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            state["last_request"] = time.time()
            if not _host_is_loopback(self.headers.get("Host")):
                self._error(403, "non-loopback host")
                return
            if not _origin_is_allowed(self.headers.get("Origin")):
                self._error(403, "cross-site origin")
                return
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            # A handler that raises sends NOTHING — no status line, no headers —
            # and the client just sees a dropped connection, which under
            # keep-alive can hang it. A corrupt transcript must not do that.
            try:
                if path.startswith("/api/"):
                    self._api(path, query)
                else:
                    self._static(path)
            except Exception:  # noqa: BLE001 - deliberate catch-all
                # stderr only: the body must never echo transcript content.
                traceback.print_exc(file=sys.stderr)
                try:
                    self._error(500, "internal error")
                except Exception:  # noqa: BLE001 - client already gone
                    pass

        def _api(self, path: str, query: Dict[str, list]) -> None:
            if path == "/api/health":
                self._json(200, {"ok": True})
                return
            if not self._authorized(query):
                self._error(403, "bad or missing token")
                return
            session = query.get("session", [""])[0]
            try:
                if path == "/api/run":
                    self._json(200, service.run_summary(session))
                elif path.startswith("/api/agent/"):
                    agent_id = path[len("/api/agent/"):]
                    self._json(200, service.agent_detail(agent_id, session))
                elif path == "/api/sessions":
                    self._json(200, service.session_list(session))
                elif path == "/api/history":
                    try:
                        limit = int(query.get("limit", ["50"])[0])
                    except ValueError:
                        limit = 50
                    self._json(200, service.history_list(limit))
                elif path == "/api/history/compare":
                    result = service.history_compare(query.get("a", [""])[0],
                                                     query.get("b", [""])[0])
                    if result is None:
                        self._error(404, "both runs must be in the history")
                    else:
                        self._json(200, result)
                elif path == "/api/export":
                    self._export(session, query.get("format", ["csv"])[0])
                elif path == "/api/search":
                    self._json(200, service.search(query.get("q", [""])[0], session))
                elif path == "/api/calls":
                    self._json(200, service.calls(query.get("q", [""])[0],
                                                  query.get("failed", [""])[0] == "1", session))
                elif path == "/api/fleet":
                    self._json(200, service.fleet())
                elif path == "/api/usual":
                    self._json(200, service.usual(session))
                elif path == "/api/stream":
                    self._stream(session)
                else:
                    self._error(404, "no such route")
            except NotFound as exc:
                self._error(404, str(exc))

        def _export(self, session: str, fmt: str) -> None:
            result = service.export(fmt, session)
            if result is None:
                self._error(400, "format must be csv or json")
                return
            content_type, body, filename = result
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            # filename is built from alphanumerics, '-' and '_' only (export.filename).
            self.send_header("Content-Disposition",
                             'attachment; filename="{}"'.format(filename))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _stream(self, session: str) -> None:
            """Tell the browser *when* something changed; it fetches the data.

            The stream carries no run data, only a change fingerprint, so the
            token-gated JSON endpoints stay the single place anything is
            redacted and served.
            """
            service.change_token(session)      # unknown session -> 404, pre-headers
            with state["lock"]:
                if state["streams"] >= MAX_STREAMS:
                    self._error(429, "too many streams")
                    return
                state["streams"] += 1
            try:
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()
                last = service.change_token(session)
                self._emit("hello", {"ok": True})
                started = last_beat = time.time()
                while time.time() - started < STREAM_MAX_S:
                    time.sleep(STREAM_POLL_S)
                    state["last_request"] = time.time()   # an open tab is use
                    current = service.change_token(session)
                    if current != last:
                        last = current
                        self._emit("tick", {})
                        last_beat = time.time()
                    elif time.time() - last_beat >= STREAM_HEARTBEAT_S:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        last_beat = time.time()
            except (BrokenPipeError, ConnectionError, OSError, NotFound):
                pass          # the client went away; that is the normal ending
            finally:
                with state["lock"]:
                    state["streams"] -= 1

        def _emit(self, event: str, data: Dict[str, Any]) -> None:
            self.wfile.write("event: {}\ndata: {}\n\n".format(
                event, json.dumps(data)).encode("utf-8"))
            self.wfile.flush()

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            # realpath, not normpath: a symlink planted inside static/ would
            # satisfy a purely textual prefix check and be served.
            target = os.path.realpath(os.path.join(STATIC_DIR, name))
            root = os.path.realpath(STATIC_DIR)
            if not target.startswith(root + os.sep) or not os.path.isfile(target):
                self._error(404, "not found")
                return
            extension = os.path.splitext(target)[1]
            if extension not in _CONTENT_TYPES:
                self._error(403, "unsupported asset")
                return
            with open(target, "rb") as fh:
                self._send(200, fh.read(), _CONTENT_TYPES[extension])

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # A browser closing a tab or an EventSource reconnecting is not an
        # error worth a stack trace in the log; anything else still is.
        if isinstance(sys.exc_info()[1], (ConnectionError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def make_server(service: OrchestraService, port: int) -> ThreadingHTTPServer:
    state: Dict[str, Any] = {"last_request": time.time(),
                             "streams": 0, "lock": threading.Lock()}
    server = _Server(("127.0.0.1", port), make_handler(service, state))
    server.orchestra_state = state  # type: ignore[attr-defined]
    return server


def serve(service: OrchestraService, port: int = C.DEFAULT_PORT
          ) -> Tuple[ThreadingHTTPServer, threading.Thread]:
    """Start on a background thread. Returns (server, thread)."""
    server = make_server(service, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def start_idle_watchdog(server: ThreadingHTTPServer,
                        idle_seconds: int = C.IDLE_SHUTDOWN_S) -> threading.Thread:
    """Shut down after a long silence so no server is left running for days."""
    state = server.orchestra_state  # type: ignore[attr-defined]

    def watch() -> None:
        while True:
            time.sleep(30)
            if time.time() - state["last_request"] > idle_seconds:
                server.shutdown()
                return

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread
