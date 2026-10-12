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
import json
import re
from typing import Any, Dict, List, Optional

from orchestra import constants as C
from orchestra import listprices
from orchestra import livestate
from orchestra import pressure
from orchestra import verify
from orchestra import waste as waste_mod
from orchestra.edges import normalize_path
from orchestra.errors import TARGET_CUT
from orchestra.redact import scrub
from orchestra.runaway import detect_loop
from orchestra.spend import _short

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
            card["title"] = card["title_many"].replace("{n}", str(card["more"] + 1))
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
    """Merged, filtered, ordered and capped, then explained: what the reader sees. An over-usual card
    points only to cards that are shown, never to one filtered out or cut by the cap."""
    shown = sorted((c for c in _merge(cards) if _worth_showing(c)), key=_order)[:MAX_CARDS]
    _explain_over_usual(shown)
    return [_finish(c) for c in shown]


# ------------------------------------------------------------------ failures

API_FIX = {
    "rate_limit": ("a usage limit", "Wait for the limit to reset, then resume the agent."),
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
             snippet: str = "", merge: bool = False, tag: str = "sure") -> Dict[str, Any]:
        key = "failure:{}:{}".format(cause, "" if merge else agent.agent_id)
        return _card(key, "failure", tag, "failure", title, happened, [agent.agent_id], where, text,
                     snippet, title_many=title_many, always=True, **spent)

    last_api = log.api[-1] if log is not None and log.api else None
    if last_api is not None and last_api.get("resumed_at") is None and last_api.get("at") is not None \
            and last_api["at"] >= end - API_RECENT_S:
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
                                    "fills its context.", merge=True, tag="sure" if compactions else "possible")
    loop = detect_loop(agent.tool_calls, C.LOOP_REPEATS, C.LOOP_CYCLE_CALLS)
    if loop is not None:
        first = loop.calls[0]
        what = ("{} {} {} times in a row".format(first[0], scrub(first[1])[:120], loop.count)
                if loop.kind == "repeat" else "the same {} calls over and over".format(len(loop.calls)))
        return card("loop", "Went in circles", "{n} agents went in circles",
                    "{} repeated {} before it {}.".format(label, what, verb), "instructions",
                    "Give it a stop condition in its instructions:",
                    "If the same step fails or repeats three times, stop and report what you tried.",
                    merge=True, tag="possible")
    open_call = bool(agent.tool_calls) and agent.tool_calls[-1].ok is None
    if agent.status == C.STALLED and open_call:
        return card("quiet", "Went quiet during a tool call", "{n} agents went quiet during a tool call",
                    "{} went quiet while a tool call was still open.".format(label), "none",
                    "It may still be running a long command. Open the agent to check, or stop it and resume.")
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


# ------------------------------------------------------------------ sure rules

def _seen_check(run: Any) -> str:
    """The latest test, build or lint command any agent ran: the one to ask for."""
    best = None
    for agent in run.agents:
        for call in agent.tool_calls:
            if call.verify == verify.CHECK and call.timestamp is not None and \
                    (best is None or call.timestamp > best.timestamp):
                best = call
    return scrub(best.target)[:120] if best is not None else ""


def _exact_command(text: str) -> str:
    """A command as a snippet may show it: its first line, or "" when that line is cut off."""
    lines = text.strip().splitlines()
    line = lines[0].strip() if lines else ""
    return "" if line.endswith(("...", "\u2026")) else line


def _verify_cards(run: Any, now: float, table: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: Optional[str] = None
    for agent in run.agents:
        v = agent.verification
        if agent.status != C.COMPLETED or not v or not v.get("final") \
                or v.get("state") not in (verify.UNCHECKED, verify.FAILING):
            continue
        if seen is None:
            seen = _seen_check(run)
        label = _label(agent)
        if v["state"] == verify.FAILING:
            last = scrub((v.get("last_check") or {}).get("target") or "")[:120]
            title, many = "Finished with its checks failing", "{n} agents finished with their checks failing"
            happened = "{}'s last check, {}, failed.".format(label, last or "a check")
            command = _exact_command(last or seen)
            key = "verify:failing:" + command
        else:
            edited = scrub((v.get("last_edit") or {}).get("target") or "")[:120] or "code"
            title, many = ("Reported done without running the tests",
                           "{n} agents reported done without running the tests")
            happened = "{} edited {} and ran no test, build or lint after it.".format(label, edited)
            command = _exact_command(seen)
            key = "verify:" + v["state"]
        snippet = "Before you report done, run {} and include its result. If it fails, fix it or say so.".format(
            "`" + command + "`" if command else "the project's tests")
        out.append(_card(key, "verify", "sure", "fix", title, happened, [agent.agent_id],
                         "instructions", "Add this to the agent's instructions (its brief, or its definition "
                                         "in .claude/agents/):", snippet,
                         seconds=agent.duration_s or 0.0, tokens=_fresh(agent.tokens), cost=agent.cost,
                         label="work not checked", title_many=many, always=True))
    return out


def _overlapped(run: Any, writers: List[str], now: float) -> bool:
    """True when at least two of the writers were running at the same time. An agent still running
    runs until `now`; one with no known start (or not in the run) counts as overlapping, so it is
    never silently dropped. Writers that took turns are a handoff, not a conflict."""
    spans = []
    for writer in writers:
        agent = run.agent(writer)
        start = agent.started_at if agent is not None else None
        if start is None:
            spans.append(None)
        else:
            spans.append((start, agent.ended_at if agent.ended_at is not None else now))
    for i, a in enumerate(spans):
        for b in spans[i + 1:]:
            if a is None or b is None or (a[0] < b[1] and b[0] < a[1]):
                return True
    return False


def _worktree_cards(run: Any, now: float, table: Any) -> List[Dict[str, Any]]:
    names = {a.agent_id: _label(a) for a in run.agents}
    out = []
    for conflict in run.write_conflicts:
        writers = list(conflict.writer_ids)
        if not _overlapped(run, writers, now):
            continue
        who = ", ".join(names.get(w, w[:12]) for w in writers[:4])
        out.append(_card("worktree", "worktree", "sure", "fix", "Agents wrote the same file",
                         "{} all wrote {}.".format(who, scrub(conflict.path)[:160]), writers, "agent",
                         "Give agents that edit in parallel their own copy of the repository: add this to each "
                         "one's definition in .claude/agents/:", "isolation: worktree",
                         title_many="Agents wrote the same files ({n} files)", always=True))
    return out


def _compact_cards(run: Any, now: float, table: Any) -> List[Dict[str, Any]]:
    sources = [("", run.main_waste)] + [(a.agent_id, a.waste) for a in run.agents]
    count = tokens = 0
    longest, extra = 0.0, 0.0
    priced = False
    agents: List[str] = []
    for agent_id, log in sources:
        if log is None:
            continue
        for r in log.rebuilds():
            if r["cause"] not in (waste_mod.IDLE_LONG, waste_mod.IDLE_SHORT):
                continue
            count += 1
            tokens += r["tokens"]
            longest = max(longest, r["gap_s"])
            if agent_id not in agents:
                agents.append(agent_id)
            prices = table.price_for(r["model"]) if table is not None else None
            if prices is not None:
                extra += r["tokens"] * max(0.0, prices["cache_create"] - prices["cache_read"]) / 1_000_000
                priced = True
    if not count:
        return []
    return [_card("compact", "compact", "sure", "fix", "The cache was rebuilt after a break",
                  "After {} (the longest {}), Claude Code wrote {} tokens of context to the prompt cache "
                  "again.".format(_plural(count, "break"), fmt_duration(longest), fmt_count(tokens)),
                  agents, "habit", "Before a break of more than a few minutes, run /compact so less has to be "
                                   "written again; if you won't need the old context, /clear instead.",
                  tokens=tokens, cost=extra if priced else None, label="extra cache writes")]


def _rules_missing_cards(run: Any, now: float, table: Any) -> List[Dict[str, Any]]:
    context = (run.insights or {}).get("context") or {}
    out = []
    for row in context.get("missing") or []:
        files = ", ".join(scrub(p)[:120] for p in (row.get("missing") or [])[:3])
        out.append(_card("rules_missing", "rules_missing", "sure", "fix", "An agent ran without your project rules",
                         "{} did not load {}, which the main session had.".format(
                             scrub(row.get("label") or "")[:60] or row["agent_id"][:12], files),
                         [row["agent_id"]], "agent",
                         "If it must follow them, name them in its instructions or definition. If its definition "
                         "sets omitClaudeMd: true this is expected; otherwise check it ran in the project folder.",
                         title_many="{n} agents ran without your project rules", always=True))
    return out


# Commands worth pre-approving: tests, builds, linters. Anything else is reported, never suggested:
# allowing it for good would also allow commands the user has not seen.
SAFE_PREFIXES = tuple(tuple(p.split()) for p in (
    "npm test", "npm run test", "npm run lint", "npm run build", "npm run typecheck",
    "pnpm test", "pnpm run test", "pnpm run lint", "pnpm run build", "yarn test", "yarn lint", "yarn build",
    "pytest", "python -m pytest", "python -m unittest", "python3 -m pytest", "python3 -m unittest",
    "go test", "go build", "go vet", "cargo test", "cargo check", "cargo build", "cargo clippy",
    "make test", "make check", "make lint", "ruff check", "mypy", "tsc", "eslint"))
UNSAFE_MARKS = (";", "&", "|", ">", "<", "`", "$(", "\n", "\r")
URL_HOST = re.compile(r"^https?://(?P<host>[a-z0-9.-]+)(?::[0-9]+)?(?:[/?#]|$)")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")   # 1 to 63 characters, no dash at either end
TOP_LABEL = re.compile(r"[a-z]+")                              # a domain's last label is a word, never a number
# What an allow rule for each tool says it reaches, shown on the card so no one reads it as narrower.
SCOPE = {"Bash": "This allows any arguments, and runs code from your project (its tests or build scripts).",
         "WebFetch": "This allows any page on that domain."}
PROMPTS_ENOUGH = 3
# Tools that ask you something by design: answering is the point, and no rule could answer for you.
ASKS_THE_USER = frozenset(("AskUserQuestion", "ExitPlanMode"))


def safe_prefix(command: str) -> Optional[str]:
    """The allow-listed prefix a command starts with, or None. A compound command, a redirect or a
    substitution never gets one: a prefix rule for it would approve more than it says."""
    text = (command or "").strip()
    if not text or any(mark in text for mark in UNSAFE_MARKS):
        return None
    words = tuple(text.split())
    for prefix in sorted(SAFE_PREFIXES, key=len, reverse=True):
        if words[:len(prefix)] == prefix:
            return " ".join(prefix)
    return None


def _is_whole(text: str, same: Any) -> bool:
    """True when `text` is the whole command or URL. The error log keeps the first line of a command or
    a URL (cut at TARGET_CUT characters) and a hash of the whole value, so the line is the whole only when
    that hash is its own. A second line, a carriage return, a cut or a scrub that changed the text never
    matches. Before the log hashed a URL's whole value it hashed the shown line, so this proved nothing
    for URLs; now it holds for both."""
    return bool(text) and hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16] == same


def _host(url: str) -> str:
    """The domain a WebFetch rule can name, or "". Read as one strict pattern, not as a general parser:
    a backslash or an "@" anywhere could make Python's parser and the fetch's disagree about the host, so
    no host is taken. An address, a single-label name, a .localhost name or a malformed label is not a
    domain to approve, and a URL the error log cut short may have lost the end of its host."""
    if len(url) >= TARGET_CUT:
        return ""
    text = url.lower()
    m = None if ("\\" in text or "@" in text) else URL_HOST.match(text)
    labels = m.group("host").split(".") if m else []
    if len(labels) < 2 or labels[-1] == "localhost" or not TOP_LABEL.fullmatch(labels[-1]) \
            or not all(LABEL.fullmatch(x) for x in labels):
        return ""
    return m.group("host")


def _waited_on(run: Any, agent_id: str, start: float) -> Optional[List[Any]]:
    """The call a prompt was about: the latest one the agent made at or before it."""
    if agent_id:
        owner = run.agent(agent_id)
        log = owner.errors if owner is not None else None
    else:
        log = run.main_errors
    best = None
    for call in (log.calls if log is not None else []):
        if call[0] is not None and call[0] <= start + 1.0 and (best is None or call[0] >= best[0]):
            best = call
    return best


def _preapprove_cards(run: Any, now: float, table: Any) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for wait in run.waits:
        if wait.kind != livestate.PERMISSION or wait.state == livestate.UNANSWERED:
            continue
        call = _waited_on(run, wait.agent_id, wait.start)
        tool, target = (scrub(call[1])[:60], call[2] or "") if call else ("a tool", "")
        if tool in ASKS_THE_USER:
            continue
        whole = call is not None and _is_whole(target, call[5])
        shown, allow = "", None
        if tool == "Bash":
            prefix = safe_prefix(target) if whole else None
            shown = prefix or (target.split()[0] if target.split() else "a command")
            key = "preapprove:Bash:" + (prefix or "other:" + shown)
            allow = "Bash({} *)".format(prefix) if prefix else None
        elif tool == "WebFetch":
            host = _host(target)
            shown = host
            key = "preapprove:WebFetch:" + host
            allow = "WebFetch(domain:{})".format(host) if whole and host else None
        else:
            key = "preapprove:" + tool
        g = groups.setdefault(key, {"tool": tool, "shown": shown, "allow": allow, "seconds": 0.0, "prompts": 0,
                                    "agents": []})
        g["seconds"] += wait.seconds(now)
        g["prompts"] += wait.prompts
        if wait.agent_id not in g["agents"]:
            g["agents"].append(wait.agent_id)
    out = []
    for key, g in groups.items():
        shown = scrub(g["shown"])[:80]
        what = "`{}`".format(shown) if g["tool"] == "Bash" else (g["tool"] + " " + shown).strip()
        happened = "Waited {} over {} for {}.".format(fmt_duration(g["seconds"]),
                                                      _plural(g["prompts"], "permission prompt"), what)
        enough = g["prompts"] >= PROMPTS_ENOUGH
        if g["allow"]:
            out.append(_card(key, "preapprove", "sure", "fix", "Pre-approve a command you kept approving", happened,
                             g["agents"], "settings",
                             "Add this to .claude/settings.json (everyone on the project) or "
                             ".claude/settings.local.json (just you), merged with any rules already there: "
                             + SCOPE[g["tool"]],
                             json.dumps({"permissions": {"allow": [g["allow"]]}}, indent=2),
                             seconds=g["seconds"], label="waiting on you", always=enough))
        else:
            out.append(_card(key, "preapprove", "sure", "info", "You kept approving " + what, happened, g["agents"],
                             "none", "Not suggested for pre-approval: allowing it for good would also allow {} "
                                     "you have not seen. Consider whether the agent needs it.".format(
                                         "commands" if g["tool"] == "Bash" else "ones"),
                             seconds=g["seconds"], label="waiting on you", always=enough))
    return out


READ_ONLY_TOOLS = frozenset(("Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "NotebookRead", "TodoWrite"))
MIN_READ_CALLS = 3
CHEAPER_SHARE = 0.30      # suggest a cheaper model only when it saves at least this share
PARALLEL_GAP_S = 120.0    # launched this soon after the previous one ended: it was waiting on it


def _family(model: str) -> str:
    name = (model or "").lower()
    return next((f for f in listprices.CHEAPER if f in name), "")


def _cheaper_cards(run: Any, now: float, table: Any) -> List[Dict[str, Any]]:
    if table is None:
        return []
    out = []
    for agent in run.agents:
        if agent.status != C.COMPLETED or len(agent.tool_calls) < MIN_READ_CALLS:
            continue
        if any(call.name not in READ_ONLY_TOOLS for call in agent.tool_calls):
            continue
        family = _family(agent.model)
        if not family:
            continue
        alias, target = listprices.CHEAPER[family]
        prices = table.price_for(target)
        cost, unpriced = table.cost(agent.tokens_by_model)
        if prices is None or unpriced or cost <= 0:
            continue
        alt = sum(count * prices.get(kind, 0.0) for tokens in agent.tokens_by_model.values()
                  for kind, count in tokens.items()) / 1_000_000
        saving = cost - alt
        if saving < MIN_COST or saving < cost * CHEAPER_SHARE:
            continue
        out.append(_card("cheaper_model:{}:{}".format(scrub(agent.agent_type) or "?", family), "cheaper_model",
                         "possible", "fix", "A cheaper model would likely have done",
                         "{} only read and searched ({} calls, no edits or commands) on {}. It cost {}; the same "
                         "tokens on {} would cost about {}.".format(
                             _label(agent), len(agent.tool_calls), scrub(_short(agent.model)),
                             fmt_money(cost, table.currency), alias, fmt_money(alt, table.currency)),
                         [agent.agent_id], "agent",
                         "If this kind of agent has a definition in .claude/agents/, set its model there; otherwise "
                         "ask Claude to launch it with model {}. Check that its answers still hold up.".format(alias),
                         "model: {}".format(alias), cost=saving, label="could save",
                         title_many="{n} read-only agents ran on an expensive model"))
    return out


def _linked(a: Any, b: Any, edges: set) -> bool:
    """Whether one agent could have needed the other: an edge, or a file one wrote that the other used."""
    if frozenset((a.agent_id, b.agent_id)) in edges:
        return True
    wa, ra = {normalize_path(p) for p in a.files_written}, {normalize_path(p) for p in a.files_read}
    wb, rb = {normalize_path(p) for p in b.files_written}, {normalize_path(p) for p in b.files_read}
    return bool(wa & rb or wb & ra or wa & wb)


def _chain_card(chain: List[Any]) -> List[Dict[str, Any]]:
    if len(chain) < 2:
        return []
    durations = [a.duration_s or 0.0 for a in chain]
    names = ", ".join(_label(a) for a in chain[:4]) + (" and {} more".format(len(chain) - 4) if len(chain) > 4 else "")
    return [_card("parallel", "parallel", "possible", "fix",
                  "These agents could have run together",
                  "{} agents ran one after another ({}) and none used another's work: no handoff, no shared "
                  "file.".format(len(chain), names),
                  [a.agent_id for a in chain], "habit",
                  "When agents don't depend on each other, ask Claude to launch them in one message so they run "
                  "at the same time.", seconds=sum(durations) - max(durations), label="could save",
                  title_many="{n} groups of agents could have run together")]


def _parallel_cards(run: Any, now: float, table: Any) -> List[Dict[str, Any]]:
    turns = run.main_turns.turns if run.main_turns is not None else []
    prompts = sorted(t["at"] for t in turns if t.get("at") is not None)
    edges = {frozenset((e.src, e.dst)) for e in run.edges}
    batch = {aid: i for i, b in enumerate(run.batches) for aid in b.agent_ids}
    groups: Dict[str, List[Any]] = {}
    for a in run.agents:
        if a.status == C.COMPLETED and a.started_at is not None and a.ended_at is not None:
            groups.setdefault(a.parent_agent_id or "", []).append(a)
    out: List[Dict[str, Any]] = []
    for agents in groups.values():
        agents.sort(key=lambda a: a.started_at)
        chain: List[Any] = []
        for agent in agents:
            prev = chain[-1] if chain else None
            joins = (prev is not None and agent.started_at >= prev.ended_at
                     and agent.started_at - prev.ended_at <= PARALLEL_GAP_S
                     and not any(prev.ended_at < t <= agent.started_at for t in prompts)
                     and (batch.get(agent.agent_id) is None or batch.get(agent.agent_id) != batch.get(prev.agent_id))
                     and not any(_linked(m, agent, edges) for m in chain))
            if not joins:
                out += _chain_card(chain)
                chain = []
            chain.append(agent)
        out += _chain_card(chain)
    return out


OVER_RATIO = 2.0          # far over usual: past the slow case AND more than this many times typical


def _usual_line(u: Dict[str, Any]) -> str:
    return "{} on {} ({} runs, {})".format(scrub(u["type"]), scrub(u["model"]), u["n"],
                                          "this project" if u["scope"] == "project" else "your projects")


def _over_usual_cards(run: Any, now: float, table: Any) -> List[Dict[str, Any]]:
    out = []
    for agent in run.agents:
        u = agent.usual
        d = agent.duration_s
        if not u or not u.get("enough") or agent.status != C.COMPLETED or d is None:
            continue
        long = u["typical_s"] > 0 and d > u["slow_s"] and d > OVER_RATIO * u["typical_s"]   # no ratio against 0 s
        dear = (agent.cost is not None and u.get("typical_cost") and u.get("slow_cost") is not None
                and agent.cost > u["slow_cost"] and agent.cost > OVER_RATIO * u["typical_cost"])
        if not (long or dear):
            continue
        if long:
            happened = "{} took {}, {:.1f}x the usual {} for {}.".format(
                _label(agent), fmt_duration(d), d / u["typical_s"], fmt_duration(u["typical_s"]), _usual_line(u))
        else:
            happened = "{} cost {}, {:.1f}x the usual {} for {}.".format(
                _label(agent), fmt_money(agent.cost), agent.cost / u["typical_cost"], fmt_money(u["typical_cost"]),
                _usual_line(u))
        out.append(_card("over_usual:" + agent.agent_id, "over_usual", "sure", "info",
                         "Took far longer than usual" if long else "Cost far more than usual", happened,
                         [agent.agent_id], "none", "",
                         seconds=max(0.0, d - u["typical_s"]) if long else 0.0,
                         cost=(agent.cost - u["typical_cost"]) if dear else None, label="over usual"))
    return out


def _explain_over_usual(cards: List[Dict[str, Any]]) -> None:
    """An over-usual card points to the other cards on the same agent, which say why."""
    for card in cards:
        if card["rule"] != "over_usual":
            continue
        others = [c["title"] for c in cards if c is not card and set(c["agents"]) & set(card["agents"])]
        card["fix"]["text"] = ("Other cards here explain part of it: " + "; ".join(others) + "." if others else
                               "No other card explains it. Open the agent and look at its longest tool calls.")


def _slow(run: Any, now: float) -> List[Dict[str, Any]]:
    """Running agents far past what their kind usually takes: the Health box's SLOWER THAN USUAL."""
    out = []
    for agent in run.agents:
        u = agent.usual
        if not u or not u.get("enough") or agent.status not in (C.RUNNING, C.WAITING) or agent.started_at is None:
            continue
        elapsed = now - agent.started_at
        if elapsed > u["slow_s"] and elapsed > OVER_RATIO * u["typical_s"]:
            out.append({"agent_id": agent.agent_id, "elapsed_s": elapsed, "typical_s": u["typical_s"],
                        "slow_s": u["slow_s"], "n": u["n"],
                        "kind": "{} on {}".format(scrub(u["type"]), scrub(u["model"]))})
    return out


# Every rule: fn(run, now, table) -> cards. Later rules are added to this tuple.
RULES = (_failure_cards, _verify_cards, _worktree_cards, _compact_cards, _rules_missing_cards,
         _preapprove_cards, _cheaper_cards, _parallel_cards, _over_usual_cards)


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
    return {"parts": _verdict_parts(run, cards), "cards": cards, "slow": _slow(run, now),
            "failures": sum(1 for c in cards if c["severity"] == "failure"),
            "priced_by": cost.get("source") if enabled else None,
            "as_of": cost.get("as_of") if enabled and cost.get("source") == "list" else None}


# ------------------------------------------------------------------ the command line

def _impact_text(impact: Dict[str, Any], currency: Optional[str]) -> str:
    parts = []
    if impact.get("cost"):
        parts.append(fmt_money(impact["cost"], currency))
    if (impact.get("seconds") or 0) >= 1:
        parts.append(fmt_duration(impact["seconds"]))
    if impact.get("tokens"):
        parts.append(fmt_count(impact["tokens"]) + " tokens")
    return (impact["label"] + ": " if parts and impact.get("label") else "") + ", ".join(parts)


def fact_sheet(run: Any) -> List[str]:
    """A short, scrubbed account of the run, so Claude can answer follow-up questions from facts."""
    totals = run.totals()
    currency = (run.cost or {}).get("currency")
    lines = ["wall time: {}".format(fmt_duration(totals["wall_time_s"])), "agents: {}".format(totals["agents"])]
    for agent in run.agents[:40]:
        lines.append("- {} | {} | {} | {} | {} | {} | {} tokens{} | {} calls | {} files written | checks: {}".format(
            agent.agent_id[:10], _label(agent), scrub(agent.agent_type) or "?", _short(agent.model), agent.status,
            fmt_duration(agent.duration_s), fmt_count(_fresh(agent.tokens)),
            " | " + fmt_money(agent.cost, currency) if agent.cost is not None else "",
            len(agent.tool_calls), len(agent.files_written), (agent.verification or {}).get("state") or "-"))
    if len(run.agents) > 40:
        lines.append("- ... and {} more agents".format(len(run.agents) - 40))
    tools: Dict[str, int] = {}
    for agent in run.agents:
        for call in agent.tool_calls:
            tools[call.name] = tools.get(call.name, 0) + 1
    top = sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    if top:
        lines.append("top tools: " + ", ".join("{} x{}".format(name, count) for name, count in top))
    out = (run.insights or {}).get("outcomes") or {}
    lines.append("write conflicts: {} | links between agents: {} | commits: {} | pull requests: {}".format(
        len(run.write_conflicts), len(run.edges), out.get("commits", 0), out.get("prs", 0)))
    return lines[:60]


def render_text(run: Any) -> str:
    """What /cuelight:open advise prints: the verdict, every card with its fix, then the fact sheet."""
    a = run.advice or {"parts": [], "cards": [], "as_of": None}
    currency = (run.cost or {}).get("currency")
    lines = ["Cuelight advice", "", " | ".join(a["parts"])]
    if a.get("as_of"):
        lines.append("Dollar figures use Anthropic list prices as of {}.".format(a["as_of"]))
    lines.append("")
    if not a["cards"]:
        lines += ["Nothing to fix in this run.", ""]
    for i, card in enumerate(a["cards"], 1):
        lines.append("{}. {} [{}]".format(i, card["title"], card["tag"]))
        lines.append("   What happened: " + card["happened"])
        cost = _impact_text(card["impact"], currency)
        if cost:
            lines.append("   What it cost: " + cost)
        fix = card["fix"]
        lines.append("   Fix ({}): {}".format(WHERE.get(fix["where"], fix["where"]), fix["text"]))
        lines += ["      " + line for line in fix["snippet"].splitlines()]
        lines.append("")
    lines.append("Run facts, for follow-up questions:")
    lines += fact_sheet(run)
    return "\n".join(lines)
