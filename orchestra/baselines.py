"""What each kind of agent usually takes: time, tokens, cost and how often it fails, learned from
your own past sessions.

Claude Code keeps every session's transcripts (about 30 days by default). This reads them, read-only,
with the same RunBuilder the dashboard uses, and keeps NUMBERS ONLY per finished agent: no prompts,
briefs, results, paths or commands; the project is a short hash of its folder name. The rows are
cached in Cuelight's state directory (never ~/.claude), one entry per session, valid while the
session's files are unchanged, so after the first scan only new or changed sessions are read. The
cache is a convenience: deleting it costs one rescan, and a corrupt or old one is ignored.

"Usual" is per agent type and model family (most agents are general-purpose, so the model is what
tells them apart): this project's runs when there are at least MIN_RUNS, else every project's,
else "not enough past runs yet". Typical is the median, slow case the 90th percentile.
"""

import glob
import hashlib
import json
import math
import os
import statistics
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from orchestra import constants as C
from orchestra import locate
from orchestra.advice import fmt_count, fmt_duration, fmt_money
from orchestra.build import RunBuilder
from orchestra.runaway import detect_loop
from orchestra.spend import _short
from orchestra.redact import scrub
from orchestra.statedir import state_dir

VERSION = 1
CACHE_NAME = "baselines-v1.json"
MIN_RUNS = 5
RESCAN_S = 600.0          # a dashboard left open picks up new sessions this often
MAX_GROUPS = 40
FINISHED = (C.COMPLETED, C.FAILED, C.ORPHANED)


def _name(text: str) -> str:
    """A type or model name as the cache keeps it: scrubbed like all text that leaves the process, and short."""
    return (scrub(text or "") or "")[:60]


def project_key(project_dir: str) -> str:
    """A project by a short hash of its folder name: matching needs no path kept."""
    name = os.path.basename((project_dir or "").rstrip("/\\"))
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12] if name else ""


ROW_KEYS = frozenset({"session", "project", "type", "model", "status", "start", "seconds", "tokens", "calls",
                      "edited", "checked", "loop", "waited"})


def _entry_ok(entry: Any) -> bool:
    """A cached session that has the shape scan() and the readers rely on; anything else is re-read."""
    return (isinstance(entry, dict) and isinstance(entry.get("fp"), list) and isinstance(entry.get("rows"), list)
            and all(isinstance(r, dict) and ROW_KEYS.issubset(r) for r in entry["rows"]))


def _fresh(tokens_by_model: Dict[str, Dict[str, int]]) -> int:
    return sum(v for tokens in tokens_by_model.values() for k, v in tokens.items() if k != "cache_read")


def _fingerprint(paths: locate.SessionPaths) -> List[List[Any]]:
    files = [paths.session_jsonl] + sorted(glob.glob(os.path.join(paths.subagents_dir, "agent-*")))
    out = []
    for path in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append([os.path.basename(path), st.st_size, st.st_mtime_ns])
    return out


def rows_for(paths: locate.SessionPaths) -> List[Dict[str, Any]]:
    """Numbers for every finished agent of one session."""
    run = RunBuilder(paths).refresh()
    project = project_key(paths.project_dir)
    out = []
    for a in run.agents:
        if a.status not in FINISHED or a.duration_s is None:
            continue
        out.append({"session": paths.session_id, "project": project, "type": _name(a.agent_type) or "?",
                    "model": _name(_short(a.model)), "status": a.status, "start": a.started_at,
                    "seconds": a.duration_s, "tokens": {_name(m): dict(t) for m, t in a.tokens_by_model.items()},
                    "calls": len(a.tool_calls), "edited": a.verification is not None,
                    "checked": (a.verification or {}).get("state", ""),
                    "loop": detect_loop(a.tool_calls, C.LOOP_REPEATS, C.LOOP_CYCLE_CALLS) is not None,
                    "waited": a.waited_s})
    return out


def _p90(values: List[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def _summarize(rows: List[Dict[str, Any]], scope: str, table: Any) -> Dict[str, Any]:
    seconds = [r["seconds"] for r in rows]
    fresh = [_fresh(r["tokens"]) for r in rows]
    # A run whose model has no price is left out of the cost figures, not counted as free.
    priced = [table.cost(r["tokens"]) for r in rows] if table is not None else []
    costs = [cost for cost, unpriced in priced if not unpriced]
    edited = [r for r in rows if r["edited"]]
    return {"type": rows[0]["type"], "model": rows[0]["model"], "scope": scope, "n": len(rows), "enough": True,
            "typical_s": statistics.median(seconds), "slow_s": _p90(seconds),
            "typical_tokens": int(statistics.median(fresh)), "slow_tokens": int(_p90(fresh)),
            "typical_cost": statistics.median(costs) if costs else None,
            "slow_cost": _p90(costs) if costs else None,
            "fail_rate": sum(1 for r in rows if r["status"] != C.COMPLETED) / len(rows),
            "unchecked_rate": (sum(1 for r in edited if r["checked"] != "checked") / len(edited)) if edited else None}


class Baselines:
    """The past-runs store. One per server; every builder asks it, so reads are under a lock and a
    scan swaps in its result at the end."""

    def __init__(self, root: Optional[str] = None, cache_path: Optional[str] = None) -> None:
        self.root = root
        self._cache_path = cache_path
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._rows: List[Dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None
        self._scanned_at = 0.0
        self.state = "idle"       # idle | scanning | ready | error
        self.error = ""

    # -- scanning ------------------------------------------------------------

    def cache_path(self) -> str:
        return self._cache_path or os.path.join(state_dir(), CACHE_NAME)

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.cache_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("sessions"), dict):
            return {}
        return {sid: entry for sid, entry in data["sessions"].items() if _entry_ok(entry)}

    def _save(self, sessions: Dict[str, Dict[str, Any]]) -> None:
        try:
            path = self.cache_path()
            fd, tmp = tempfile.mkstemp(prefix=".baselines-", dir=os.path.dirname(path) or ".")
        except OSError:
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": VERSION, "sessions": sessions}, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _install(self, sessions: Dict[str, Dict[str, Any]]) -> None:
        rows = [r for entry in sessions.values() for r in entry.get("rows", [])]
        with self._lock:
            self._sessions, self._rows = sessions, rows
            self.state, self.error = "ready", ""
            self._scanned_at = time.time()

    def scan(self) -> None:
        """Read every session whose files changed since the cache was written. Never raises."""
        with self._lock:
            if self.state != "ready":
                self.state = "scanning"
        cached = self._load()
        fresh: Dict[str, Dict[str, Any]] = {}
        projects = os.path.join(self.root if self.root is not None else locate.claude_root(), "projects")
        try:
            names = sorted(os.listdir(projects)) if os.path.isdir(projects) else []
            for name in names:
                project_dir = os.path.join(projects, name)
                for info in locate.list_sessions(project_dir):
                    paths = locate._paths_for(os.path.join(project_dir, info.session_id + ".jsonl"), info.session_id)
                    fp = _fingerprint(paths)
                    old = cached.get(info.session_id)
                    if isinstance(old, dict) and old.get("fp") == fp:
                        fresh[info.session_id] = old
                        continue
                    try:
                        rows = rows_for(paths)
                    except Exception:      # noqa: BLE001 - one unreadable session must not stop the scan
                        continue           # not cached: a session that failed is read again next scan
                    fresh[info.session_id] = {"fp": fp, "rows": rows}
        except OSError as exc:
            with self._lock:
                self.state, self.error = "error", "past runs unreadable: {}".format(exc)
                self._scanned_at = time.time()     # the next attempt waits RESCAN_S, not one per request
            return
        self._install(fresh)
        self._save(fresh)

    def start(self) -> None:
        """Scan in a background thread; a call while one is running does nothing."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self.scan, name="cuelight-baselines", daemon=True)
            self._thread.start()

    def start_if_stale(self) -> None:
        if self.state != "scanning" and time.time() - self._scanned_at >= RESCAN_S:
            self.start()

    # -- reading -------------------------------------------------------------

    def rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._rows)

    def _group(self, rows: List[Dict[str, Any]], project: str, table: Any) -> Dict[str, Any]:
        mine = [r for r in rows if r["project"] == project] if project else []
        if len(mine) >= MIN_RUNS:
            return _summarize(mine, "project", table)
        if len(rows) >= MIN_RUNS:
            return _summarize(rows, "all", table)
        first = rows[0] if rows else {}
        return {"type": first.get("type", ""), "model": first.get("model", ""), "scope": "none", "n": len(rows),
                "enough": False}

    def usual_for(self, agent_type: str, model: str, project_dir: str, exclude_session: str,
                  table: Any = None) -> Optional[Dict[str, Any]]:
        """What this kind of agent usually takes, or None before the first scan has finished."""
        if self.state != "ready":
            return None
        kind, family = _name(agent_type) or "?", _name(_short(model))
        same = [r for r in self.rows() if r["type"] == kind and r["model"] == family and r["session"] != exclude_session]
        out = self._group(same, project_key(project_dir), table)
        out.update(type=kind, model=family)
        return out

    def summary(self, project_dir: str, exclude_session: str, table: Any = None) -> Dict[str, Any]:
        """Every (type, model) seen, for the Advice tab's table and /cuelight:open estimate."""
        rows = [r for r in self.rows() if r["session"] != exclude_session]
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault((r["type"], r["model"]), []).append(r)
        project = project_key(project_dir)
        out = []
        for (kind, family), group in groups.items():
            entry = self._group(group, project, table)
            entry.update(type=kind, model=family)
            out.append(entry)
        out.sort(key=lambda g: (not g["enough"], -g["n"], g["type"], g["model"]))
        return {"state": self.state, "error": self.error, "sessions": len({r["session"] for r in rows}),
                "agents": len(rows), "groups": out[:MAX_GROUPS],
                "source": table.source if table is not None else None,
                "as_of": table.as_of if table is not None else "",
                "currency": table.currency if table is not None else None}


def render_estimate(data: Dict[str, Any]) -> str:
    """What /cuelight:open estimate prints."""
    lines = ["Cuelight: what each kind of agent usually takes, from {} past sessions ({} finished agents).".format(
        data["sessions"], data["agents"]), ""]
    if data.get("error"):
        lines += [data["error"], ""]
    if data.get("source") == "list":
        lines.append("Costs use Anthropic list prices as of {}.".format(data["as_of"]))
    elif data.get("source") == "yours":
        lines.append("Costs use your price file.")
    else:
        lines.append("No prices: costs are left out.")
    groups = data.get("groups") or []
    if not groups:
        lines += ["", "No past runs yet. Cuelight learns what each kind of agent usually takes from your finished "
                      "sessions."]
        return "\n".join(lines)
    enough = [g for g in groups if g["enough"]]
    if enough:
        header = ("type", "model", "runs", "typical", "slow case", "tokens", "cost", "fails", "from")
        table = [header] + [(g["type"][:28], g["model"], str(g["n"]), fmt_duration(g["typical_s"]),
                             fmt_duration(g["slow_s"]), fmt_count(g["typical_tokens"]),
                             fmt_money(g["typical_cost"], data.get("currency")),
                             "{:.0f}%".format(100 * g["fail_rate"]),
                             "this project" if g["scope"] == "project" else "all projects") for g in enough]
        widths = [max(len(r[i]) for r in table) for i in range(len(header))]
        lines.append("")
        lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in table]
    few = [g for g in groups if not g["enough"]]
    if few:
        lines += ["", "Not enough past runs yet (fewer than {}): ".format(MIN_RUNS) +
                  ", ".join("{} on {} ({})".format(g["type"], g["model"], g["n"]) for g in few[:12])]
    lines += ["", "Typical is the middle run; slow case is the 90th percentile. Use this to pick an agent type and "
                  "model before you launch one."]
    return "\n".join(lines)
