"""What to change next time: fix-it cards for a run, and a one-line verdict.

Rule-based and explainable, like orchestra.runaway and orchestra.verify. Every card says what
happened (the evidence, with the agents it concerns), what it cost, and one concrete change with
where it goes. "possible" marks a judgment the record cannot settle; "sure" is a fact in it.
Cuelight never applies a fix: a card's snippet is text to copy.

Pure function of a Run (plus the price table and the usual numbers the builder attached to each
agent): no I/O, bounded output, every transcript-derived string scrubbed. Cuelight's own text is
ASCII only, so the command line can print it on any console.
"""

import hashlib
from typing import Any, Dict, List, Optional

from orchestra import constants as C
from orchestra import pressure
from orchestra.redact import scrub
from orchestra.runaway import detect_loop

MIN_SECONDS = 60.0        # a card that cost less than all three of these is noise
MIN_TOKENS = 10_000
MIN_COST = 0.05
MAX_CARDS = 12
API_RECENT_S = 120.0      # an API error this close to an agent's end is why it ended
CONTEXT_FULL = 0.95       # a context this full (of its assumed window) ran out
DONE_BADLY = (C.FAILED, C.ORPHANED, C.STALLED)

WHERE = {"settings": "settings.json", "agent": "the agent's definition", "claude_md": "CLAUDE.md",
         "instructions": "the agent's instructions", "habit": "a habit", "none": "nothing to change"}


# ------------------------------------------------------------------ text

def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    total = int(round(max(0.0, seconds)))
    if total < 60:
        return "{}s".format(total)
    if total < 3600:
        return "{}m {:02d}s".format(total // 60, total % 60)
    return "{}h {:02d}m".format(total // 3600, (total % 3600) // 60)


def fmt_money(amount: Optional[float], currency: Optional[str] = None) -> str:
    if amount is None:
        return "-"
    symbol = "$" if not currency or currency == "USD" else currency + " "
    if 0 < amount < 0.01:
        return "<" + symbol + "0.01"
    return symbol + ("{:.0f}".format(amount) if amount >= 100 else "{:.2f}".format(amount))


def fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return "{:.1f}M".format(n / 1_000_000)
    if n >= 1000:
        return "{:.1f}k".format(n / 1000)
    return str(n)


def _plural(n: int, one: str, many: str = "") -> str:
    return "{} {}".format(n, one if n == 1 else (many or one + "s"))


def _label(agent: Any) -> str:
    return scrub(agent.description)[:60] or agent.agent_id[:12]


def _fresh(tokens: Optional[Dict[str, int]]) -> int:
    return sum(v for k, v in (tokens or {}).items() if k != "cache_read")


# ------------------------------------------------------------------ cards

def _card(key: str, rule: str, tag: str, severity: str, title: str, happened: str, agents: List[str],
          where: str, text: str, snippet: str = "", seconds: float = 0.0, tokens: int = 0,
          cost: Optional[float] = None, label: str = "", title_many: str = "",
          always: bool = False) -> Dict[str, Any]:
    """One card before merging. `key` decides what merges; `title_many` ("{n} agents ...") names a
    merged card; `always` shows it whatever its size (its cost is a risk, not time or money)."""
    return {"key": key, "rule": rule, "tag": tag, "severity": severity, "title": title,
            "title_many": title_many or title, "happened": happened, "agents": list(agents),
            "impact": {"seconds": seconds, "tokens": tokens, "cost": cost, "label": label},
            "fix": {"where": where, "text": text, "snippet": snippet}, "always": always, "more": 0}


def _merge(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cards with the same key become one: agents joined, impacts added."""
    out: Dict[str, Dict[str, Any]] = {}
    for card in cards:
        have = out.get(card["key"])
        if have is None:
            out[card["key"]] = dict(card, agents=list(card["agents"]), impact=dict(card["impact"]),
                                    fix=dict(card["fix"]))
            continue
        have["agents"] += [a for a in card["agents"] if a not in have["agents"]]
        have["more"] += 1
        have["always"] = have["always"] or card["always"]
        imp, add = have["impact"], card["impact"]
        imp["seconds"] += add["seconds"]
        imp["tokens"] += add["tokens"]
        if add["cost"] is not None:
            imp["cost"] = (imp["cost"] or 0.0) + add["cost"]
    for card in out.values():
        if card["more"]:
            card["title"] = card["title_many"].format(n=card["more"] + 1)
            card["happened"] += " (and {} more like it)".format(card["more"])
    return list(out.values())


def _worth_showing(card: Dict[str, Any]) -> bool:
    if card["always"]:
        return True
    imp = card["impact"]
    return imp["seconds"] >= MIN_SECONDS or imp["tokens"] >= MIN_TOKENS or (imp["cost"] or 0.0) >= MIN_COST


def _order(card: Dict[str, Any]):
    imp = card["impact"]
    return (card["severity"] != "failure", -(imp["cost"] or 0.0), -imp["seconds"], -imp["tokens"], card["key"])


def _finish(card: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": hashlib.sha1(card["key"].encode("utf-8")).hexdigest()[:12], "rule": card["rule"],
            "tag": card["tag"], "severity": card["severity"], "title": card["title"],
            "happened": card["happened"], "agents": card["agents"], "impact": card["impact"], "fix": card["fix"]}


def _select(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merged, filtered, ordered and capped: what the reader sees."""
    merged = _merge(cards)
    return [_finish(c) for c in sorted((c for c in merged if _worth_showing(c)), key=_order)[:MAX_CARDS]]


# ------------------------------------------------------------------ failures

API_FIX = {
    "rate_limit": ("a usage limit", "Wait for the limit to reset, then resume the agent. Runs started just "
                                    "before a reset are the ones that hit it."),
    "authentication_failed": ("an expired login", "Run /login, then resume the agent."),
    "server_error": ("a server error", "The API failed on Anthropic's side. Run the agent again."),
    "overloaded": ("an overloaded API", "The API was overloaded. Run the agent again in a few minutes."),
}


def _failure(agent: Any, now: float) -> Dict[str, Any]:
    """The first cause the record shows for an agent that failed, was cut off or went quiet."""
    label = _label(agent)
    verb = {C.STALLED: "went quiet", C.ORPHANED: "was cut off"}.get(agent.status, "failed")
    end = agent.ended_at or agent.last_activity_at or now
    log = agent.errors
    spent = {"seconds": agent.duration_s or max(0.0, end - (agent.started_at or end)),
             "tokens": _fresh(agent.tokens), "cost": agent.cost, "label": "spent before it stopped"}

    def card(cause: str, title: str, title_many: str, happened: str, where: str, text: str,
             snippet: str = "", merge: bool = False) -> Dict[str, Any]:
        key = "failure:{}:{}".format(cause, "" if merge else agent.agent_id)
        return _card(key, "failure", "sure", "failure", title, happened, [agent.agent_id], where, text,
                     snippet, title_many=title_many, always=True, **spent)

    last_api = log.api[-1] if log is not None and log.api else None
    if last_api is not None and last_api.get("at") is not None and last_api["at"] >= end - API_RECENT_S:
        kind = last_api.get("kind") or "error"
        what, fix = API_FIX.get(kind, ("an API error", "Run the agent again. If it happens again, read the "
                                                       "error above."))
        message = scrub(last_api.get("text") or "")[:160]
        return card("api-" + kind, "Stopped by " + what, "{n} agents stopped by " + what,
                    "{} {} right after the API returned {}{}.".format(label, verb, what,
                                                                     ": " + message if message else ""),
                    "habit", fix, merge=True)
    stuck = log.stuck() if log is not None else None
    if stuck is not None:
        target = scrub(stuck["target"])[:120]
        error = scrub(stuck.get("error") or "")[:160]
        return card("stuck", "Stuck on a failing call", "{n} agents stuck on a failing call",
                    "{} ran {} {} {} times in a row and it failed every time{}.".format(
                        label, stuck["tool"], target, stuck["failed_in_a_row"], ": " + error if error else ""),
                    "instructions", "Add a line to this agent's instructions (its brief, or its definition in "
                                    ".claude/agents/) so it stops instead of retrying:",
                    "If {} on {} fails twice, stop and report the error and what you tried.".format(stuck["tool"],
                                                                                                  target))
    done = log.done_calls() if log is not None else []
    if done:
        timeout = next((t for t in reversed(log.timeouts)
                        if not t.get("background") and t.get("at") == done[-1][0]), None)
        if timeout is not None:
            limit_ms = int(max(timeout["after_s"] * 2, 300) * 1000)
            return card("timeout", "Its last command ran past its timeout",
                        "{n} agents' last command ran past its timeout",
                        "{}'s last command, {}, was stopped after {}.".format(
                            label, scrub(timeout["target"])[:120], fmt_duration(timeout["after_s"])),
                        "settings", "Ask for long commands to run in the background, or raise the Bash timeout "
                                    "for this project in .claude/settings.json:",
                        '{"env": {"BASH_DEFAULT_TIMEOUT_MS": "%d"}}' % limit_ms)
    peak = pressure.peak_of(agent.waste)
    compactions = len(agent.waste.compactions) if agent.waste is not None else 0
    if compactions or (peak is not None and peak["fill"] >= CONTEXT_FULL):
        fill = "{:.0f}%".format(100 * peak["fill"]) if peak is not None else "most"
        return card("context", "Ran out of context", "{n} agents ran out of context",
                    "{}'s context reached {} of its window{}.".format(
                        label, fill, " and was compacted " + _plural(compactions, "time") if compactions else ""),
                    "instructions", "Split this task across two agents, each with a smaller part, so neither "
                                    "fills its context.")
    loop = detect_loop(agent.tool_calls, C.LOOP_REPEATS, C.LOOP_CYCLE_CALLS)
    if loop is not None:
        first = loop.calls[0]
        what = ("{} {} {} times in a row".format(first[0], scrub(first[1])[:120], loop.count)
                if loop.kind == "repeat" else "the same {} calls over and over".format(len(loop.calls)))
        return card("loop", "Went in circles", "{n} agents went in circles",
                    "{} repeated {} before it {}.".format(label, what, verb), "instructions",
                    "Give it a stop condition in its instructions:",
                    "If the same step fails or repeats three times, stop and report what you tried.")
    open_call = bool(agent.tool_calls) and agent.tool_calls[-1].ok is None
    if agent.status == C.ORPHANED or open_call:
        return card("closed", "Stopped when the session ended", "{n} agents stopped when the session ended",
                    "{} {} while {}.".format(label, verb, "a tool call was still open" if open_call
                                             else "its session ended"),
                    "none", "Nothing to fix in the setup. Resume it if its work is still needed.", merge=True)
    recent = "; ".join("{} {}".format(c.name, scrub(c.target)[:60]).strip() for c in agent.tool_calls[-3:])
    said = next((line.strip() for line in scrub(agent.result or "").splitlines() if line.strip()), "")[:160]
    return card("unknown", "No cause in the record", "{n} agents stopped with no cause in the record",
                "{} {}. Its last calls: {}.{}".format(label, verb, recent or "none",
                                                     " Its last message: " + said if said else ""),
                "none", "Open the agent to read what it did last.")


def _failure_cards(run: Any, now: float, table: Any) -> List[Dict[str, Any]]:
    return [_failure(a, now) for a in run.agents if a.status in DONE_BADLY]


# Every rule: fn(run, now, table) -> cards. Later rules are added to this tuple.
RULES = (_failure_cards,)


# ------------------------------------------------------------------ verdict

def _verdict_parts(run: Any, cards: List[Dict[str, Any]]) -> List[str]:
    totals = run.totals()
    n, failed = totals["agents"], totals.get(C.FAILED, 0) + totals.get(C.ORPHANED, 0)
    parts = [(_plural(n, "agent") + (", {} failed".format(failed) if failed else "")) if n else "No agents"]
    out = (run.insights or {}).get("outcomes") or {}
    made = [_plural(out[k], word) for k, word in (("commits", "commit"), ("prs", "PR")) if out.get(k)]
    if made:
        parts.append(", ".join(made))
    checks = out.get("checks") or {}
    passed, bad = checks.get("passed", 0), checks.get("failed", 0)
    if bad:
        parts.append("{} of {} test runs failed".format(bad, passed + bad))
    elif passed:
        parts.append("tests passed")
    cost = run.cost or {}
    if cost.get("enabled") and cost.get("total") is not None:
        parts.append(fmt_money(cost["total"], cost.get("currency")) +
                     (" (list price)" if cost.get("source") == "list" else ""))
    parts.append(_plural(len(cards), "thing") + " to fix" if cards else "nothing to fix")
    return parts


def compute(run: Any, now: float, table: Any = None) -> Dict[str, Any]:
    """The Advice payload for a run. `table` is the PriceTable in use, or None."""
    raw: List[Dict[str, Any]] = []
    for rule_fn in RULES:
        raw.extend(rule_fn(run, now, table))
    cards = _select(raw)
    cost = run.cost or {}
    enabled = bool(cost.get("enabled"))
    return {"parts": _verdict_parts(run, cards), "cards": cards, "slow": [],
            "failures": sum(1 for c in cards if c["severity"] == "failure"),
            "priced_by": cost.get("source") if enabled else None,
            "as_of": cost.get("as_of") if enabled and cost.get("source") == "list" else None}
