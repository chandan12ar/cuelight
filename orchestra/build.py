"""Assembling a Run from a session's files.

Pure given the file contents: no clock of its own (now_fn is injected), no
network, no writes. Everything the dashboard shows is decided here.
"""

import glob
import os
import threading
import time
from typing import Callable, Dict, List, Optional

from orchestra import constants as C
from orchestra.agentlog import ActivityTimes, AgentDigest, TokenTally
from orchestra.edges import HandoffCache, infer_edges
from orchestra import livestate
from orchestra.events import Event, EventSpool
from orchestra.extract import extract_expected_output, extract_objective
from orchestra.locate import SessionPaths
from orchestra.model import Agent, Batch, Round, Run
from orchestra import insights
from orchestra.parent import ParentIndex, parse_timestamp
from orchestra.pricing import PriceSource, PriceTable
from orchestra.runaway import detect_loop
from orchestra.status import build_rounds, compute_status, fill_handbacks
from orchestra.transcript import IncrementalReader
from orchestra import verify
from orchestra.outcomes import OutcomeLog
from orchestra.context import ContextLog
from orchestra.sessionmeta import SessionMeta
from orchestra.waste import WasteLog
from orchestra.turns import TurnLog
from orchestra.errors import ErrorLog


MAX_EVENTS = 5000


class RunBuilder:
    """Holds the incremental state between polls. One per session."""

    def __init__(self, paths: SessionPaths,
                 now_fn: Callable[[], float] = time.time,
                 spool: Optional[EventSpool] = None,
                 prices: Optional[PriceSource] = None) -> None:
        self.paths = paths
        self.now_fn = now_fn
        # Hook events, when a spool is given. Without one the run is built from
        # transcripts alone, exactly as before.
        self._spool = spool
        self._events: List[Event] = []
        self._prices = prices
        self._table: Optional[PriceTable] = None
        self._main_tally = TokenTally()
        self._main_activity: Optional[float] = None
        self._main_times = ActivityTimes()
        self._main_outcomes = OutcomeLog()
        self._main_context = ContextLog()
        self._main_meta = SessionMeta()
        self._main_waste = WasteLog()
        self._main_turns = TurnLog()
        self._main_errors = ErrorLog()
        # ThreadingHTTPServer runs a thread per connection, and every one of
        # them calls refresh() on this same builder. refresh mutates the
        # reader's byte offsets and the per-agent digests, which accumulate
        # with += and append -- so two interleaved refreshes consume the same
        # bytes twice and the counts stay permanently doubled.
        self._lock = threading.Lock()
        self._reader = IncrementalReader()
        self._parent = ParentIndex()
        self._digests: Dict[str, AgentDigest] = {}
        self._metas: Dict[str, dict] = {}
        self._handoffs = HandoffCache()

    def refresh(self) -> Run:
        with self._lock:
            return self._refresh_locked()

    def _refresh_locked(self) -> Run:
        now = self.now_fn()
        main_entries = self._reader.read_new(self.paths.session_jsonl)
        if self._reader.consume_reset(self.paths.session_jsonl):
            self._main_tally.reset()          # re-read from byte 0: don't double count
            self._parent = ParentIndex()      # and don't keep launches the new file lacks
            self._main_times.clear()
            self._main_outcomes = OutcomeLog()
            self._main_context = ContextLog()
            self._main_meta = SessionMeta()
            self._main_waste = WasteLog()
            self._main_turns = TurnLog()
            self._main_errors = ErrorLog()
        self._main_tally.ingest(main_entries)
        self._parent.ingest(main_entries)
        self._note_main_activity(main_entries)
        self._main_outcomes.ingest(main_entries)
        self._main_context.ingest(main_entries)
        self._main_meta.ingest(main_entries)
        self._main_waste.ingest(main_entries)
        self._main_turns.ingest(main_entries)
        self._main_errors.ingest(main_entries)
        self._scan_subagents()
        if self._spool is not None:
            self._events.extend(self._spool.read_new(self.paths.session_id))
            del self._events[:-MAX_EVENTS]
        live = livestate.derive(self._events, self._activity_at(),
                                self._agent_activity())
        session_live = self._session_live(now, live)
        self._table = self._prices.get() if self._prices else None

        waits = livestate.waits(self._events, self._first_activity_after, session_live)
        agents = [self._assemble(agent_id, now, session_live, live)
                  for agent_id in sorted(self._agent_ids())]
        self._apply_waits(agents, waits)
        agents.sort(key=lambda a: (a.started_at is None, a.started_at or 0))

        edges, hubs, conflicts = infer_edges(agents, self._handoffs)
        starts = [a.started_at for a in agents if a.started_at is not None]
        ends = [a.ended_at for a in agents if a.ended_at is not None]

        run = Run(
            session_id=self.paths.session_id,
            project_path=self._parent.cwd,
            started_at=min(starts) if starts else None,
            ended_at=max(ends) if ends and not session_live else None,
            session_live=session_live,
            agents=agents,
            edges=edges,
            batches=self._batches(agents),
            hub_files=hubs,
            write_conflicts=conflicts,
            diagnostics=dict(self._reader.diagnostics),
            live=live.to_dict() if live.has_events else None,
            orchestrator=self._orchestrator_block(),
            cost=self._cost_block(agents),
        )
        run.waits = waits
        run.main_outcomes = self._main_outcomes.snapshot()
        run.main_context = self._main_context.snapshot()
        run.meta = self._main_meta.snapshot()
        run.main_waste = self._main_waste.snapshot()
        run.main_turns = self._main_turns.snapshot()
        run.main_errors = self._main_errors.snapshot()
        run.insights = insights.compute(run, now, self._table)
        return run

    # -- internals ---------------------------------------------------------

    def _session_live(self, now: float, live: "livestate.LiveState") -> bool:
        try:
            mtime = os.path.getmtime(self.paths.session_jsonl)
        except OSError:
            return False
        if (live.ended_at is not None
                and mtime <= live.ended_at + livestate.GRACE_S):
            # SessionEnd is a fact; the mtime window is only a guess. Don't wait
            # out 10 minutes to learn what the hook already told us. Transcript
            # writes after the end mean the session was resumed.
            return False
        return (now - mtime) <= C.SESSION_LIVE_THRESHOLD_S

    def _note_main_activity(self, entries: List[dict]) -> None:
        for entry in entries:
            at = parse_timestamp(entry.get("timestamp")) \
                if isinstance(entry, dict) else None
            if at is not None and (self._main_activity is None
                                   or at > self._main_activity):
                self._main_activity = at
            if at is not None:
                self._main_times.add(at)

    def _first_activity_after(self, agent_id: str, t: float) -> Optional[float]:
        """When the transcript moved after t: that agent's own, or (no agent) anyone's."""
        if agent_id:
            digest = self._digests.get(agent_id)
            return digest.activity.first_after(t) if digest else None
        found = [x for x in [self._main_times.first_after(t)]
                 + [d.activity.first_after(t) for d in self._digests.values()]
                 if x is not None]
        return min(found) if found else None

    @staticmethod
    def _apply_waits(agents: List[Agent], waits: List["livestate.Wait"]) -> None:
        by_id = {a.agent_id: a for a in agents}
        for wait in waits:
            agent = by_id.get(wait.agent_id)
            if agent is None or wait.state == livestate.UNANSWERED:
                continue
            agent.wait_count += 1
            if wait.state == livestate.OPEN:
                agent.wait_open_since = wait.start
            else:
                agent.waited_s += wait.seconds(0.0)

    def _agent_activity(self) -> Dict[str, float]:
        return {agent_id: d.last_activity_at
                for agent_id, d in self._digests.items()
                if d.last_activity_at is not None}

    def _activity_at(self) -> Optional[float]:
        """Latest transcript activity anywhere in the session.

        Entry timestamps, not file mtime: Claude Code appends bookkeeping lines
        with no timestamp, and mtime would read each as activity and wrongly
        clear a prompt that is still waiting.
        """
        times = list(self._agent_activity().values())
        if self._main_activity is not None:
            times.append(self._main_activity)
        return max(times) if times else None

    def _scan_subagents(self) -> None:
        directory = self.paths.subagents_dir
        if not os.path.isdir(directory):
            return
        for meta_path in glob.glob(os.path.join(directory, "agent-*.meta.json")):
            agent_id = os.path.basename(meta_path)[len("agent-"):-len(".meta.json")]
            meta = self._reader.read_json(meta_path)
            if meta:
                self._metas[agent_id] = meta
        for log_path in glob.glob(os.path.join(directory, "agent-*.jsonl")):
            agent_id = os.path.basename(log_path)[len("agent-"):-len(".jsonl")]
            entries = self._reader.read_new(log_path)
            if self._reader.consume_reset(log_path):
                # The file was re-read from byte 0 — drop whatever this
                # agent's digest already tallied, or the truncated entries
                # (tokens, tool calls, files) get counted twice.
                self._digests.pop(agent_id, None)
            if not entries:
                continue
            digest = self._digests.setdefault(agent_id, AgentDigest())
            digest.ingest(entries)
            # A nested agent's launches live in its own transcript.
            self._parent.ingest(entries)

    def _agent_ids(self) -> List[str]:
        ids = set(self._metas) | set(self._digests)
        for result in self._parent.results.values():
            # An agent id is only real if its tool_result answers an actual
            # Agent/Task launch. The id is scraped from result TEXT, so a
            # transcript that merely QUOTES another session's launch output —
            # which happens whenever anyone inspects transcripts — would
            # otherwise invent a phantom agent with no meta, no transcript and
            # no launch, showing up as an empty "unknown" row.
            if result.tool_use_id not in self._parent.launches:
                continue
            if result.agent_id:
                ids.add(result.agent_id)
        return sorted(ids)

    def _tool_use_id_for(self, agent_id: str) -> str:
        meta = self._metas.get(agent_id) or {}
        if meta.get("toolUseId"):
            return str(meta["toolUseId"])
        for result in self._parent.results.values():
            if result.agent_id == agent_id:
                return result.tool_use_id
        for notes in self._parent.notifications.get(agent_id, []):
            if notes.tool_use_id:
                return notes.tool_use_id
        return ""

    def _assemble(self, agent_id: str, now: float, session_live: bool,
                  live: "livestate.LiveState") -> Agent:
        meta = self._metas.get(agent_id) or {}
        digest = self._digests.get(agent_id) or AgentDigest()
        tool_use_id = self._tool_use_id_for(agent_id)
        launch = self._parent.launches.get(tool_use_id)
        result = self._parent.results.get(tool_use_id)
        notifications = self._parent.notifications.get(agent_id, [])

        rounds = build_rounds(launch, result, notifications, digest)
        self._apply_stop_event(rounds, digest, live.agent_stops.get(agent_id))
        fill_handbacks(rounds, digest.handbacks)
        status = compute_status(rounds, digest, now, session_live)
        if status in (C.RUNNING, C.STALLED) and agent_id in live.agent_waiting:
            status = C.WAITING
        # Only an agent that is still going can be "in a loop right now".
        loop = None
        if status in (C.RUNNING, C.STALLED, C.WAITING):
            found = detect_loop(digest.tool_calls, C.LOOP_REPEATS, C.LOOP_CYCLE_CALLS)
            loop = found.to_dict() if found else None

        checked = verify.assess(digest.tool_calls,
                                final=status in (C.COMPLETED, C.FAILED, C.ORPHANED))

        brief = launch.prompt if launch else ""
        description = str(meta.get("description") or (launch.description if launch else ""))
        final_result = ""
        for round_ in reversed(rounds):
            if round_.result:
                final_result = round_.result
                break
        if not final_result and rounds and rounds[-1].ended_at is not None:
            final_result = digest.report

        shape = meta.get("requestShape")
        launch_mode = "background" if shape == "background" else (
            result.launch_mode if result else "inline")

        log_path = os.path.join(self.paths.subagents_dir,
                                "agent-{}.jsonl".format(agent_id))
        return Agent(
            agent_id=agent_id,
            tool_use_id=tool_use_id,
            # A fork's own transcript replays its full inherited history —
            # including the very entry that launched it, tagged (like every
            # entry in that file) with isSidechain=True and its own agentId.
            # Read naively, that looks like the agent spawning itself. An
            # agent can never legitimately be its own parent, so that specific
            # shape is treated as the top-level launch it actually was.
            parent_agent_id=(launch.launcher_agent_id
                             if launch and launch.launcher_agent_id != agent_id else None),
            spawn_depth=int(meta.get("spawnDepth") or 1),
            agent_type=str(meta.get("agentType") or ""),
            description=description,
            # digest.model is what the agent's own transcript actually reports
            # (e.g. "claude-sonnet-5") — the ground truth. meta/launch only
            # carry the short alias *requested* at spawn time ("sonnet"), which
            # is a worse answer to "which model ran this" whenever the real
            # one is known.
            model=str(digest.model or meta.get("model")
                      or (launch.model if launch else "")),
            launch_mode=launch_mode,
            brief=brief,
            objective=extract_objective(brief, description),
            expected_output=extract_expected_output(brief, description),
            status=status,
            rounds=rounds,
            last_activity_at=digest.last_activity_at,
            result=final_result,
            loop=loop,
            tokens=dict(digest.tokens),
            tokens_by_model={m: dict(t) for m, t in digest.tokens_by_model.items()},
            cost=(self._table.cost(digest.tokens_by_model)[0]
                  if self._table is not None else None),
            tool_calls=list(digest.tool_calls),
            token_events=list(digest.token_events),
            files_written=list(digest.files_written),
            files_read=list(digest.files_read),
            transcript_path=log_path,
            verification=checked.to_dict() if checked else None,
            changes=digest.changes.snapshot(),
            outcomes=digest.outcomes.snapshot(),
            context=digest.context.snapshot(),
            waste=digest.waste.snapshot(),
            errors=digest.errors.snapshot(),
        )

    def _orchestrator_block(self) -> Optional[Dict[str, object]]:
        tally = self._main_tally
        if not tally.tokens:
            return None
        cost = (self._table.cost(tally.by_model)[0]
                if self._table is not None else None)
        return {"tokens": dict(tally.tokens), "model": tally.model, "cost": cost}

    def _cost_block(self, agents: List[Agent]) -> Dict[str, object]:
        """Money for the whole run, or why there is none."""
        if self._table is None:
            block: Dict[str, object] = {"enabled": False}
            if self._prices is not None and self._prices.error:
                block["error"] = self._prices.error
            return block
        agents_total = sum(a.cost or 0.0 for a in agents)
        main_cost, main_unpriced = self._table.cost(self._main_tally.by_model)
        unpriced = set(main_unpriced)
        for a in agents:
            unpriced.update(self._table.cost(a.tokens_by_model)[1])
        total = agents_total + main_cost
        block = {"enabled": True, "currency": self._table.currency,
                 "source": self._table.source, "as_of": self._table.as_of,
                 "total": total, "agents": agents_total,
                 "orchestrator": main_cost,
                 "partial": bool(unpriced), "unpriced_models": sorted(unpriced),
                 "budget": None}
        if C.BUDGET > 0:
            ratio = total / C.BUDGET
            block["budget"] = {
                "limit": C.BUDGET, "spent": total, "ratio": ratio,
                "state": ("exceeded" if ratio >= 1.0 else
                          "warn" if ratio >= C.BUDGET_WARN_RATIO else "ok")}
        return block

    @staticmethod
    def _apply_stop_event(rounds: List[Round], digest: AgentDigest,
                          stopped_at: Optional[float]) -> None:
        """A SubagentStop hook closes an open round at the exact stop time.

        The event does not say whether the agent succeeded. Reuse the transcript
        rule: dying while holding an unanswered tool call is a failure.
        """
        if stopped_at is None or not rounds or rounds[-1].ended_at is not None:
            return
        last = rounds[-1]
        if last.started_at is not None and stopped_at < last.started_at:
            return
        last.ended_at = stopped_at
        last.status = C.FAILED if digest.ended_mid_tool else C.COMPLETED
        if not last.result:
            last.result = digest.report

    def _batches(self, agents: List[Agent]) -> List[Batch]:
        by_tool_use = {a.tool_use_id: a.agent_id for a in agents if a.tool_use_id}
        grouped: Dict[str, Batch] = {}
        for launch in self._parent.launches.values():
            agent_id = by_tool_use.get(launch.tool_use_id)
            if not agent_id:
                continue
            batch = grouped.setdefault(
                launch.turn_uuid,
                Batch(turn_uuid=launch.turn_uuid, launched_at=launch.launched_at))
            batch.agent_ids.append(agent_id)
            if launch.launched_at is not None:
                if batch.launched_at is None or launch.launched_at < batch.launched_at:
                    batch.launched_at = launch.launched_at
        batches = list(grouped.values())
        for batch in batches:
            batch.agent_ids.sort()
        batches.sort(key=lambda b: (b.launched_at is None, b.launched_at or 0))
        return batches
