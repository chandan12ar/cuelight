"""What went wrong: API errors and the time each cost, failed tool calls, the same call tried
again after a failure, and commands that ran past their timeout.

Claude Code records an API failure as an assistant entry with ``isApiErrorMessage`` (model
``<synthetic>``), an ``error`` type (``rate_limit``, ``authentication_failed``, ``server_error``),
usually an ``apiErrorStatus`` and the message it showed you. Nothing happens until the next real
reply, so the time to it is the time the error cost; on real sessions that was 98 seconds to over
four hours, almost all of it session limits. A tool result with ``is_error`` is a failed call. A
Bash command past its timeout carries ``timedOutAfterMs``; current versions move it to the
background (``backgroundTaskId``) instead of stopping it.

A retry is the same tool on the same target again within RETRY_GAP other calls of a failure (on
real sessions usually after one call: the fix, then the rerun). Most retries work; an agent that
is still running with STUCK_AFTER failed attempts in a row of its latest call goes in the Health box.
"""

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from orchestra.parent import parse_timestamp
from orchestra.redact import scrub

RETRY_GAP = 3             # at most this many other calls between a failure and its retry
STUCK_AFTER = 3           # failed attempts in a row that make a running agent "stuck"
STUCK_RECENT = 10         # ...when the last of them is among its latest this-many calls
MAX_CALLS = 20_000
MAX_API = 200
MAX_TIMEOUTS = 200
MAX_TEXT = 200
TARGET_CUT = 160          # a stored target (a command, a path, a URL) keeps this many characters of its first line
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")     # terminal colours in command output


def _target(params: Any) -> Tuple[str, str]:
    """(what to show: the first line of the target, what makes two calls the same call). A file
    tool is the same call on the same file, whatever the edit; a command or a URL is the same call
    only when the whole value matches, so two different ``python - <<'EOF'`` scripts are not, and a URL
    with a line break is not the URL its first line shows."""
    if not isinstance(params, dict):
        return "", ""
    for key in ("file_path", "notebook_path", "command", "path", "url", "pattern", "query", "description"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            shown = scrub(value.strip().splitlines()[0])[:TARGET_CUT]
            whole = value.strip() if key in ("command", "url") else shown
            return shown, hashlib.sha1(whole.encode("utf-8", "replace")).hexdigest()[:16]
    return "", ""


def _first_line(content: Any) -> str:
    """The first non-empty line of a result or message, without Claude Code's error tags. A bare
    "Exit code 1" says nothing, so the line after it is joined on."""
    if isinstance(content, list):
        content = "\n".join(c.get("text") or "" for c in content if isinstance(c, dict) and c.get("type") == "text")
    if not isinstance(content, str):
        return ""
    content = _ANSI.sub("", content.replace("<tool_use_error>", "").replace("</tool_use_error>", ""))
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0]
    if re.fullmatch(r"Exit code -?\d+", first) and len(lines) > 1:
        first += ": " + lines[1]
    return scrub(first)[:MAX_TEXT]


def _api_text(content: Any) -> str:
    """What Claude Code said about an API error. 'API Error: 529 {"type":"error","error":{"type":
    "overloaded_error","message":"Overloaded"}}' reads as 'API Error: 529 Overloaded'."""
    text = _first_line(content)
    match = re.match(r"(API Error: \d+) (\{.*\})$", text)
    if match:
        try:
            body = json.loads(match.group(2))
        except ValueError:
            return text
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if isinstance(message, str) and message.strip():
            return scrub(match.group(1) + " " + message.strip())[:MAX_TEXT]
    return text


class ErrorLog:
    def __init__(self) -> None:
        # API failures: at, kind, HTTP status, what Claude Code said, and when the next real reply came.
        self.api: List[Dict[str, Any]] = []
        # Tool calls in order: [at, tool, target, ok (None until its result), first line of the error,
        # what makes two calls the same call].
        self.calls: List[List[Any]] = []
        # Commands past their timeout: at, tool, target, the timeout, moved to the background or not.
        self.timeouts: List[Dict[str, Any]] = []
        self._open: Dict[str, int] = {}
        self._seen: set = set()

    def ingest(self, entries: List[Any]) -> None:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            at = parse_timestamp(entry.get("timestamp"))
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            if entry.get("isApiErrorMessage"):
                self._api(entry, message, at)
                continue
            model = message.get("model")
            if entry.get("type") == "assistant" and isinstance(model, str) and model and model != "<synthetic>" \
                    and at is not None:
                for e in self.api:
                    if e["resumed_at"] is None and e["at"] is not None and at > e["at"]:
                        e["resumed_at"] = at
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    if len(self.calls) < MAX_CALLS:
                        self._open[block["id"]] = len(self.calls)
                        shown, same = _target(block.get("input"))
                        self.calls.append([at, str(block.get("name") or "tool"), shown, None, "", same])
                elif block.get("type") == "tool_result":
                    self._result(entry, block, at)

    def _api(self, entry: Dict[str, Any], message: Dict[str, Any], at: Optional[float]) -> None:
        key = entry.get("uuid") or message.get("id") or "{}|{}".format(at, entry.get("error"))
        if key in self._seen or len(self.api) >= MAX_API:
            return
        self._seen.add(key)
        kind, status = entry.get("error"), entry.get("apiErrorStatus")
        kind = scrub(kind)[:40] if isinstance(kind, str) else ""
        last = self.api[-1] if self.api else None
        if last is not None and last["resumed_at"] is None and last["kind"] == kind:
            last["repeats"] += 1          # the same error again before any reply: one stall
            return
        self.api.append({"at": at, "kind": kind,
                         "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
                         "text": _api_text(message.get("content")), "resumed_at": None, "repeats": 1})

    def _result(self, entry: Dict[str, Any], block: Dict[str, Any], at: Optional[float]) -> None:
        index = self._open.pop(block.get("tool_use_id"), None)
        if index is None:
            return
        call = self.calls[index]
        call[3] = not block.get("is_error")
        if block.get("is_error"):
            call[4] = _first_line(block.get("content"))
        result = entry.get("toolUseResult")
        timeout = result.get("timedOutAfterMs") if isinstance(result, dict) else None
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0 \
                and len(self.timeouts) < MAX_TIMEOUTS:
            self.timeouts.append({"at": call[0], "tool": call[1], "target": call[2], "after_s": timeout / 1000.0,
                                  "background": bool(result.get("backgroundTaskId"))})

    # -- reading it back --------------------------------------------------

    def done_calls(self) -> List[List[Any]]:
        return [c for c in self.calls if c[3] is not None]

    def failed(self) -> List[List[Any]]:
        return [c for c in self.calls if c[3] is False]

    def retries(self) -> List[Dict[str, Any]]:
        """Each failed call that was tried again: attempts until it worked or the tries stopped."""
        calls = self.done_calls()
        out, used = [], set()
        for i, call in enumerate(calls):
            if call[3] or i in used or not call[5]:
                continue
            attempts, j = [i], i
            while True:
                nxt = next((k for k in range(j + 1, min(len(calls), j + 2 + RETRY_GAP))
                            if calls[k][1] == call[1] and calls[k][5] == call[5]), None)
                if nxt is None:
                    break
                attempts.append(nxt)
                used.add(nxt)
                j = nxt
                if calls[nxt][3]:
                    break
            if len(attempts) > 1:
                last = calls[attempts[-1]]
                out.append({"at": call[0], "last_at": last[0], "tool": call[1], "target": call[2], "same": call[5],
                            "attempts": len(attempts), "ok": bool(last[3]), "error": call[4] or last[4],
                            "latest": attempts[-1] >= len(calls) - STUCK_RECENT,
                            "failed_in_a_row": len(attempts) - (1 if last[3] else 0)})
        return out

    def stuck(self) -> Optional[Dict[str, Any]]:
        """The latest call failing again and again: STUCK_AFTER or more failures in a row, the last
        of them recent, and no later attempt of it."""
        for r in reversed(self.retries()):
            if not r["ok"] and r["latest"] and r["failed_in_a_row"] >= STUCK_AFTER:
                later = [c for c in self.calls if c[0] is not None and r["last_at"] is not None and c[0] > r["last_at"]
                         and c[1] == r["tool"] and c[5] == r["same"]]
                if not later:
                    return r
        return None

    def empty(self) -> bool:
        return not self.api and not self.timeouts and not any(c[3] is False for c in self.calls)

    def totals(self) -> Optional[Dict[str, Any]]:
        """The agent panel's line, or None when nothing went wrong."""
        if self.empty():
            return None
        retries = self.retries()
        stuck = self.stuck()
        return {"api": len(self.api), "api_kinds": sorted({e["kind"] or "error" for e in self.api}),
                "failed": len(self.failed()), "calls": len(self.done_calls()),
                "retries": len(retries), "retries_ok": sum(1 for r in retries if r["ok"]),
                "timeouts": len(self.timeouts),
                "stuck": {"tool": stuck["tool"], "target": stuck["target"],
                          "failed_in_a_row": stuck["failed_in_a_row"]} if stuck else None}

    def snapshot(self) -> "ErrorLog":
        copy = ErrorLog()
        copy.api = [dict(e) for e in self.api]
        copy.calls = [list(c) for c in self.calls]
        copy.timeouts = [dict(t) for t in self.timeouts]
        copy._open = dict(self._open)
        copy._seen = set(self._seen)
        return copy


def _union(spans: List[Tuple[float, float]]) -> float:
    """Seconds covered by the spans, overlaps counted once."""
    total, start, end = 0.0, None, None
    for s, e in sorted(spans):
        if end is None or s > end:
            if end is not None:
                total += end - start
            start, end = s, e
        else:
            end = max(end, e)
    return total + (end - start if end is not None else 0.0)


def summary(sources: List[Tuple[str, str, str, Optional[ErrorLog]]], live: bool,
            now: float) -> Optional[Dict[str, Any]]:
    """API errors with the time each cost, failed calls by tool and by agent, retries, timeouts,
    and who is stuck retrying right now. ``sources`` are (agent id, label, status, log), the main
    session first with an empty id. None when nothing went wrong anywhere."""
    api, timeouts, retries, stuck = [], [], [], []
    by_tool: Dict[str, Dict[str, Any]] = {}
    by_agent = []
    failed_total = calls_total = 0
    lost_total = 0.0
    kinds: Dict[str, Dict[str, Any]] = {}
    for agent_id, label, status, log in sources:
        if log is None:
            continue
        done = log.done_calls()
        failed = [c for c in done if c[3] is False]
        calls_total += len(done)
        failed_total += len(failed)
        for c in done:
            row = by_tool.setdefault(c[1], {"tool": c[1], "failed": 0, "calls": 0, "errors": {}})
            row["calls"] += 1
            if c[3] is False:
                row["failed"] += 1
                if c[4]:
                    row["errors"][c[4]] = row["errors"].get(c[4], 0) + 1
        if failed:
            by_agent.append({"agent_id": agent_id, "label": label, "failed": len(failed), "calls": len(done)})
        spans: Dict[str, List[Tuple[float, float]]] = {}
        for e in log.api:
            end = e["resumed_at"] if e["resumed_at"] is not None else (now if live else None)
            lost = max(0.0, end - e["at"]) if end is not None and e["at"] is not None else None
            api.append(dict(e, agent_id=agent_id, label=label, lost_s=lost, ongoing=e["resumed_at"] is None and live))
            k = kinds.setdefault(e["kind"] or "error", {"count": 0, "lost_s": 0.0})
            k["count"] += 1
            if lost is not None:
                spans.setdefault(e["kind"] or "error", []).append((e["at"], e["at"] + lost))
        for kind, kind_spans in spans.items():
            kinds[kind]["lost_s"] += _union(kind_spans)
        lost_total += _union([s for kind_spans in spans.values() for s in kind_spans])
        timeouts += [dict(t, agent_id=agent_id, label=label) for t in log.timeouts]
        retries += [dict(r, agent_id=agent_id, label=label) for r in log.retries()]
        if live and status in ("running", "waiting", "stalled"):
            s = log.stuck()
            if s is not None:
                stuck.append(dict(s, agent_id=agent_id, label=label))
    if not api and not failed_total and not timeouts:
        return None
    tools = []
    for row in by_tool.values():
        if row["failed"]:
            common = max(row["errors"].items(), key=lambda kv: kv[1])[0] if row["errors"] else ""
            tools.append({"tool": row["tool"], "failed": row["failed"], "calls": row["calls"], "example": common})
    tools.sort(key=lambda r: (-r["failed"], r["tool"]))
    by_agent.sort(key=lambda r: -r["failed"])
    api.sort(key=lambda e: e["at"] or 0)
    retries.sort(key=lambda r: (-r["attempts"], r["ok"], -(r["at"] or 0)))
    timeouts.sort(key=lambda t: -(t["at"] or 0))
    for r in retries + stuck:
        r.pop("same", None)        # a hash for matching calls, nothing to show
    return {"api": api[-12:], "api_count": len(api), "api_kinds": kinds, "api_lost_s": lost_total,
            "failed": failed_total, "calls": calls_total, "tools": tools[:8], "agents": by_agent[:10],
            "retries": retries[:12], "retry_count": len(retries),
            "retries_ok": sum(1 for r in retries if r["ok"]),
            "timeouts": timeouts[:10], "timeout_count": len(timeouts), "stuck": stuck,
            "stuck_after": STUCK_AFTER}
