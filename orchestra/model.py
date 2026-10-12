"""Dataclasses for an Orchestra run. No I/O, no parsing."""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from orchestra import constants as C
from orchestra import pressure
from orchestra.redact import scrub, scrub_obj


LIGHT_TEXT_CAP = 200
# The timeline draws a comb of ticks along each bar: tool calls per slice of the
# agent's life. Fixed width, so the payload stays small however long an agent runs.
ACTIVITY_BINS = 28
LAST_TOOL_CAP = 80


def _cap(text: Optional[str], limit: int = LIGHT_TEXT_CAP) -> str:
    """Trim a field carried in the 2-second poll payload.

    Applied AFTER scrub, never before, so a credential can never be split
    across the cut and survive half-redacted. The full text stays available
    on the per-agent detail endpoint.
    """
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


_GIST_MARK = re.compile(r"^(#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)")


def _gist(text: str) -> str:
    """A report's opening on one line, for the Agents table: markdown marks dropped,
    rules and table separators skipped, lines joined with " · ". Stops once past the cap."""
    lines: List[str] = []
    size = 0
    for raw in text.splitlines():
        line = _GIST_MARK.sub("", raw.strip()).replace("**", "").replace("__", "").replace("`", "").strip()
        if not line or not line.strip("-=*_|: "):
            continue
        lines.append(line)
        size += len(line) + 3
        if size > LIGHT_TEXT_CAP:
            break
    return " · ".join(lines)


@dataclass
class Extraction:
    """A value pulled out of a brief, plus which rule produced it."""
    text: str = ""
    source: str = ""


@dataclass
class Round:
    """One start-to-finish pass of an agent. An agent resumed via SendMessage has several."""
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    status: str = C.RUNNING
    result: str = ""


@dataclass
class ToolCall:
    name: str = ""
    target: str = ""
    timestamp: Optional[float] = None
    ok: Optional[bool] = None     # False when its result was an error; None until a result arrives
    verify: str = ""              # orchestra.verify.EDIT / CHECK / RUN, or ""
    ref: str = ""                 # the file an EDIT wrote or the scripts a RUN ran (orchestra.verify)


@dataclass
class Agent:
    agent_id: str
    tool_use_id: str = ""
    parent_agent_id: Optional[str] = None
    spawn_depth: int = 1
    agent_type: str = ""
    description: str = ""
    model: str = ""
    launch_mode: str = "inline"
    brief: str = ""
    objective: Extraction = field(default_factory=Extraction)
    expected_output: Extraction = field(default_factory=Extraction)
    status: str = C.UNKNOWN
    rounds: List[Round] = field(default_factory=list)
    last_activity_at: Optional[float] = None
    result: str = ""
    tokens: Dict[str, int] = field(default_factory=dict)
    tokens_by_model: Dict[str, Dict[str, int]] = field(default_factory=dict)
    cost: Optional[float] = None     # None = no price table (or nothing priced)
    loop: Optional[Dict[str, Any]] = None   # a POSSIBLE loop (orchestra.runaway)
    tool_calls: List[ToolCall] = field(default_factory=list)
    token_events: List[Tuple[float, int]] = field(default_factory=list)   # (when, fresh tokens)
    files_written: List[str] = field(default_factory=list)
    files_read: List[str] = field(default_factory=list)
    transcript_path: str = ""
    # Time spent on prompts only the user could answer (orchestra.livestate.waits):
    # answered waits in seconds, how many waits, and when the open one began.
    waited_s: float = 0.0
    wait_count: int = 0
    wait_open_since: Optional[float] = None
    # Whether it checked its work after its last code edit (orchestra.verify), or None.
    verification: Optional[Dict[str, Any]] = None
    # What it changed (orchestra.changes.ChangeLog): totals in the light payload, diffs in the detail.
    changes: Any = None
    # What it produced (orchestra.outcomes.OutcomeLog): commits, pushes, pull requests, test runs.
    outcomes: Any = None
    # What it was told (orchestra.context.ContextLog): instruction files and skills.
    context: Any = None
    # Where its tokens went to waste (orchestra.waste.WasteLog).
    waste: Any = None
    # What went wrong: API errors, failed calls, retries, timeouts (orchestra.errors.ErrorLog).
    errors: Any = None

    @property
    def started_at(self) -> Optional[float]:
        starts = [r.started_at for r in self.rounds if r.started_at is not None]
        return min(starts) if starts else None

    @property
    def ended_at(self) -> Optional[float]:
        """None while any round is still open — an agent mid-resume has no end."""
        if not self.rounds or any(r.ended_at is None for r in self.rounds):
            return None
        return max(r.ended_at for r in self.rounds)

    @property
    def duration_s(self) -> Optional[float]:
        if self.started_at is None or self.ended_at is None:
            return None
        return self.ended_at - self.started_at

    def _activity(self) -> List[int]:
        """Tool calls per equal slice of this agent's span; [] when there is nothing to show."""
        start = self.started_at
        end = self.ended_at if self.ended_at is not None else self.last_activity_at
        if start is None or end is None or end <= start:
            return []
        span = end - start
        bins = [0] * ACTIVITY_BINS
        for call in self.tool_calls:
            at = call.timestamp
            if at is None or at < start or at > end:
                continue
            bins[min(ACTIVITY_BINS - 1, int((at - start) / span * ACTIVITY_BINS))] += 1
        return bins if any(bins) else []

    def _last_tool(self) -> Optional[Dict[str, str]]:
        """What the agent did most recently: the Work Floor's "doing now" line."""
        if not self.tool_calls:
            return None
        call = self.tool_calls[-1]
        return {"name": call.name, "target": _cap(scrub(call.target), LAST_TOOL_CAP)}

    def _loop_dict(self) -> Optional[Dict[str, Any]]:
        if not self.loop:
            return None
        return {"kind": self.loop["kind"], "count": self.loop["count"],
                "tool": self.loop["tool"], "target": scrub(self.loop["target"]),
                "calls": [{"tool": c["tool"], "target": scrub(c["target"])}
                          for c in self.loop["calls"]]}

    def to_light_dict(self) -> Dict[str, Any]:
        """Everything the timeline and graph need; nothing large."""
        return {
            "agent_id": self.agent_id,
            "parent_agent_id": self.parent_agent_id,
            "spawn_depth": self.spawn_depth,
            "agent_type": self.agent_type,
            "description": scrub(self.description),
            "model": self.model,
            "launch_mode": self.launch_mode,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": self.duration_s,
            "last_activity_at": self.last_activity_at,
            "tokens": dict(self.tokens),
            "cost": self.cost,
            "loop": self._loop_dict(),
            "rounds": [{"started_at": r.started_at, "ended_at": r.ended_at,
                        "status": r.status} for r in self.rounds],
            "objective": _cap(scrub(self.objective.text)),
            "expected_output": _cap(scrub(self.expected_output.text)),
            "result_gist": _cap(_gist(scrub(self.result))),
            "files_written_count": len(self.files_written),
            "tool_call_count": len(self.tool_calls),
            "activity": self._activity(),
            "last_tool": self._last_tool(),
            "waited_s": self.waited_s,
            "wait_count": self.wait_count,
            "wait_open_since": self.wait_open_since,
            "verification": self.verification,
            "changes": self.changes.totals() if self.changes is not None else None,
            "produced": ({"commits": len(self.outcomes.commits), "prs": len(self.outcomes.prs),
                          "pushes": self.outcomes.pushes}
                         if self.outcomes is not None and not self.outcomes.empty() else None),
            "waste": self._waste_totals(),
            "context_peak": self._context_peak(),
            "errors": self.errors.totals() if self.errors is not None else None,
        }

    def _waste_totals(self) -> Optional[Dict[str, int]]:
        if self.waste is None:
            return None
        rebuilds = self.waste.rebuilds()
        return {"rebuilds": len(rebuilds), "rebuilt_tokens": sum(r["tokens"] for r in rebuilds)}

    def _context_peak(self) -> Optional[Dict[str, Any]]:
        """How full its context got (orchestra.pressure), and how often it was compacted."""
        peak = pressure.peak_of(self.waste)
        if peak is None:
            return None
        return {"tokens": peak["tokens"], "limit": peak["limit"], "fill": peak["fill"],
                "now": peak["now"], "now_fill": peak["now_fill"], "model": peak["model"],
                "compactions": len(self.waste.compactions)}

    def to_detail_dict(self) -> Dict[str, Any]:
        d = self.to_light_dict()
        d.update({
            "brief": scrub(self.brief),
            "result": scrub(self.result),
            # Overrides the capped copy inherited from to_light_dict: the
            # detail endpoint is exactly where the full text belongs.
            "objective": scrub(self.objective.text),
            "objective_source": self.objective.source,
            "expected_output": scrub(self.expected_output.text),
            "expected_output_source": self.expected_output.source,
            "tool_calls": [{"name": t.name, "target": scrub(t.target),
                            "timestamp": t.timestamp, "ok": t.ok} for t in self.tool_calls],
            # These are transcript-derived paths too: the same string is
            # scrubbed in tool_calls[].target, so leaving it raw here would
            # be a hole in the single chokepoint spec section 11 promises.
            "files_written": [scrub(p) for p in self.files_written],
            "files_read": [scrub(p) for p in self.files_read],
            "transcript_path": scrub(self.transcript_path),
            "change_files": self.changes.to_dicts() if self.changes is not None else [],
            "outcomes": self.outcomes.to_dict() if self.outcomes is not None else None,
            "context": self.context.to_dict() if self.context is not None else None,
            "waste": self.waste.to_dict() if self.waste is not None else None,
        })
        return d


@dataclass
class Edge:
    src: str
    dst: str
    kind: str
    confidence: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"src": self.src, "dst": self.dst, "kind": self.kind,
                "confidence": self.confidence, "evidence": scrub_obj(self.evidence)}


@dataclass
class Batch:
    """Agents launched in the same assistant turn — one parallel wave."""
    turn_uuid: str = ""
    agent_ids: List[str] = field(default_factory=list)
    launched_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"turn_uuid": self.turn_uuid, "agent_ids": list(self.agent_ids),
                "launched_at": self.launched_at}


@dataclass
class HubFile:
    """A file read by many agents but written by none — collapsed out of the graph."""
    path: str = ""
    reader_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"path": scrub(self.path), "reader_ids": list(self.reader_ids)}


@dataclass
class WriteConflict:
    """A file written by more than one agent — a real correctness risk, not
    just informational the way a shared-read hub file is."""
    path: str = ""
    writer_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"path": scrub(self.path), "writer_ids": list(self.writer_ids)}


@dataclass
class Run:
    session_id: str
    project_path: str = ""
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    session_live: bool = False
    agents: List[Agent] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)
    batches: List[Batch] = field(default_factory=list)
    hub_files: List[HubFile] = field(default_factory=list)
    write_conflicts: List[WriteConflict] = field(default_factory=list)
    diagnostics: Dict[str, int] = field(default_factory=dict)
    # Hook-event ground truth (orchestra.livestate.LiveState.to_dict), or None
    # when no hook has ever reported for this session.
    live: Optional[Dict[str, Any]] = None
    # The orchestrator's own usage (the main transcript), which no per-agent
    # figure includes: {"tokens", "model", "cost"}.
    orchestrator: Optional[Dict[str, Any]] = None
    # Money, when a price table is configured; see RunBuilder._cost_block.
    cost: Optional[Dict[str, Any]] = None
    # Run-level analytics (orchestra.insights.compute): parallelism, critical path,
    # tool mix, token efficiency, file hotspots. None until the builder fills it.
    insights: Optional[Dict[str, Any]] = None
    # Blocking prompts as intervals (orchestra.livestate.Wait); summarised by insights.
    waits: List[Any] = field(default_factory=list)
    # What the main session itself produced (orchestra.outcomes.OutcomeLog), beside the agents'.
    main_outcomes: Any = None
    # The main session's instruction files and skills (orchestra.context.ContextLog).
    main_context: Any = None
    # What Claude Code calls the session and last said about it (orchestra.sessionmeta.SessionMeta).
    meta: Any = None
    # The main session's own cache rebuilds and big results (orchestra.waste.WasteLog).
    main_waste: Any = None
    # The main session split at each of your prompts (orchestra.turns.TurnLog).
    main_turns: Any = None
    # The main session's own API errors, failed calls and retries (orchestra.errors.ErrorLog).
    main_errors: Any = None
    # What to change next time (orchestra.advice.compute): verdict and fix-it cards.
    advice: Optional[Dict[str, Any]] = None

    def agent(self, agent_id: str) -> Optional[Agent]:
        for a in self.agents:
            if a.agent_id == agent_id:
                return a
        return None

    def totals(self) -> Dict[str, Any]:
        counts = {"agents": len(self.agents)}
        for status in (C.RUNNING, C.WAITING, C.COMPLETED, C.FAILED, C.STALLED,
                       C.ORPHANED, C.UNKNOWN):
            counts[status] = sum(1 for a in self.agents if a.status == status)
        tokens = {}
        for a in self.agents:
            for k, v in a.tokens.items():
                tokens[k] = tokens.get(k, 0) + v
        counts["tokens"] = tokens
        ends = [a.ended_at for a in self.agents if a.ended_at is not None]
        starts = [a.started_at for a in self.agents if a.started_at is not None]
        counts["wall_time_s"] = (max(ends) - min(starts)) if ends and starts else None
        return counts

    def to_summary_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "project_path": self.project_path,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "session_live": self.session_live,
            "totals": self.totals(),
            "agents": [a.to_light_dict() for a in self.agents],
            "edges": [e.to_dict() for e in self.edges],
            "batches": [b.to_dict() for b in self.batches],
            "hub_files": [h.to_dict() for h in self.hub_files],
            "write_conflicts": [c.to_dict() for c in self.write_conflicts],
            "diagnostics": dict(self.diagnostics),
            "live": self.live,
            "orchestrator": self.orchestrator,
            "cost": self.cost,
            "insights": self.insights,
            "advice": self.advice,
            "session": self.meta.to_dict() if self.meta is not None else None,
        }
