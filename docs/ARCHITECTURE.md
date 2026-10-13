# Cuelight — Architecture & Feature Reference

This document explains how Cuelight works end to end: what problem it solves,
how the pieces fit together, the exact formulas and heuristics it uses to turn
raw transcript files into a dashboard, and what every feature on screen does
and why it exists. It assumes no prior knowledge of the codebase.

If you just want to *use* Cuelight, see [`README.md`](../README.md). This
document is for understanding — or extending — how it's built.

> **A note on the demo data.** Cuelight was itself built by a 22-agent Claude
> Code orchestration, following the 16-task plan in
> [`docs/superpowers/plans/2026-09-12-orchestra.md`](superpowers/plans/2026-09-12-orchestra.md).
> That build's own transcript (session `73b33781`) is what most of the
> screenshots and examples in this document — and in the dashboard's own
> development — are drawn from. Later features were validated the same way,
> one level further in: pointing a live Cuelight server at *its own
> currently-running development session* and dispatching real subagents to
> watch. That's exactly how the notification-parsing fix and the fork
> self-parent fix (both in §5) were found — not in a test, but by watching
> the dashboard lie about its own live agents in real time. Cuelight
> watches its own construction, live.

---

## 1. What problem this solves

When Claude Code runs subagents (via the `Task`/`Agent` tool), the only
record of what happened is scattered across several files:

- the **main session transcript** (`~/.claude/projects/<project>/<session>.jsonl`),
  which contains the *launch* of each subagent and the *result* it returned
- a **per-agent transcript** (`.../<session>/subagents/agent-<id>.jsonl`),
  which is that subagent's own turn-by-turn record — its tool calls, its
  token usage, its final message
- a **per-agent metadata file** (`agent-<id>.meta.json`) with a few fields
  the launch/result records don't carry (agent type, spawn depth, model)

None of this is a dashboard. There's no single place that says "22 agents
ran, here's who fed whom, here's who's stuck, here's what each one actually
did." Cuelight reads exactly these files — nothing else, no instrumentation,
no changes to how Claude Code runs — and reconstructs that picture.

It is deliberately **read-only** and **local-only**: it never writes under
`~/.claude`, never talks to the network, and only serves `127.0.0.1`.

---

## 2. How you run it — the `/cuelight:open` slash command

Cuelight ships as an installable Claude Code **plugin**
(`.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`), installed
with:

```
/plugin marketplace add <this repo>
/plugin install cuelight
```

That makes the `/cuelight:open` command (defined in
[`commands/open.md`](../commands/open.md)) available. It's a thin
dispatcher — the actual work is a plain Python CLI
([`orchestra/__main__.py`](../orchestra/__main__.py)), started through the
launcher [`bin/cuelight`](../bin/cuelight). Claude Code puts a plugin's `bin/`
on the Bash tool's PATH, so each action is one bare `cuelight` command, run from
the project directory. The launcher picks the first Python 3.9+ among `python3`
and `python` (skipping a stub such as the Windows Store alias) and runs the CLI.

Why it is shaped this way:

- The command file declares no `allowed-tools`, so Claude Code asks before
  running it (the directory holds a plugin that pre-approves broad shell access).
- Claude Code offers "don't ask again" as a rule on the command's first word.
  For `cuelight ...` that is `cuelight *`, which covers Cuelight only; for
  `python .../__main__.py` it was `python *`, which covers every Python command.
- The commands carry no shell variables, because Claude Code will not offer
  "don't ask again" for a command with one. The CLI reads
  `$CLAUDE_CODE_SESSION_ID` itself and takes the project from the directory it
  starts in.
- The command no longer names the plugin's versioned path, so the rule survives
  updates. On a Claude Code without `bin/` support the command file falls back
  to `python ${CLAUDE_PLUGIN_ROOT}/orchestra/__main__.py`.

| You type | The command runs | What happens |
|---|---|---|
| `/cuelight:open` | `cuelight` | Starts the dashboard server (if not already running for this session) and opens the URL |
| `/cuelight:open stop` | `cuelight --stop` | Kills the server process for this session |
| `/cuelight:open report` | `cuelight --report .` | Writes one self-contained `.html` file — no server needed to view it |
| `/cuelight:open advise` | `cuelight --advise` | Prints the run's verdict, its fix-it cards (with the text to copy) and a short fact sheet, then exits. `commands/open.md` tells Claude to show it and not to change any file unless the user asks |
| `/cuelight:open estimate` | `cuelight --estimate` | Prints what each kind of agent usually takes (time, tokens, cost, fail rate) from the user's past sessions, then exits |

### What `--session` actually does under the hood

1. **Port file reuse.** Each session gets a small JSON file in the OS temp
   directory (`orchestra_<session-id>.json`) recording the port, an
   auth token, and the server's PID. If that file exists and the server
   answers `/api/health`, the running server is reused instead of starting a
   second one.
2. **Detached launch.** Otherwise, `cmd_start` spawns
   `python -m orchestra --serve --session <id>` (from the plugin directory) as a **detached background
   process** (on Windows: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`; on
   POSIX: a new session) so the dashboard keeps running after the command
   that launched it returns, and polls the port file for up to 5 seconds
   waiting for it to come alive.
3. **Idle shutdown.** A background watchdog thread checks every 30 seconds
   whether the server has been silent for `IDLE_SHUTDOWN_S` (30 minutes) and
   shuts itself down — so a forgotten dashboard doesn't run forever.
4. **The URL.** `http://127.0.0.1:<port>/?k=<token>&session=<id>` — the token
   is required on every API call (see [§8 Security](#8-security--privacy-model)).

---

## 3. The core idea: reconstructing orchestration from transcripts alone

To read the rest of this document, it helps to know the vocabulary Claude
Code's own transcripts use — Cuelight's whole backend is built around
recognizing these patterns in the JSONL:

| Concept | What it looks like in the transcript | What Cuelight calls it |
|---|---|---|
| Spawning a subagent | A `tool_use` block named `Task` or `Agent` in the **parent's** transcript | a **launch** (`LaunchRecord`) |
| A subagent finishing inline | The matching `tool_result` block, same turn | a **result** (`ResultRecord`) |
| A subagent finishing in the background | The `tool_result` text contains `"Async agent launched"` and an `agentId: <hex>` | a **background launch mode** |
| A background agent's completion signal | A `<task-notification>` block appearing later in the parent's transcript | a **notification** |
| Waking a *finished* agent back up | `SendMessage` targeting a known agent id | a **resumed round** — the agent gets a second `Round` |
| Several agents launched in the same assistant turn | Same `uuid` on the parent message | a **batch** — a parallel "wave," shown as a shaded band in the Timeline |
| A subagent launching its own subagents | `isSidechain: true` entries with an `agentId` | **nested orchestration** — tracked via `parent_agent_id` / `spawn_depth` |

An agent's full lifecycle can therefore span **multiple `Round`s** (an initial
run, then zero or more resumptions via `SendMessage`), which is why the data
model has `Agent.rounds: List[Round]` rather than a single start/end pair.

---

## 4. Architecture: how a byte on disk becomes a pixel on screen

```mermaid
flowchart LR
    subgraph disk["On disk (~/.claude/projects/...)"]
        A["session.jsonl<br/>(parent transcript)"]
        B["subagents/agent-*.jsonl<br/>(per-agent transcripts)"]
        C["subagents/agent-*.meta.json"]
    end

    subgraph backend["orchestra/ (Python, stdlib only)"]
        D["transcript.py<br/>IncrementalReader"]
        E["parent.py<br/>ParentIndex"]
        F["agentlog.py<br/>AgentDigest"]
        G["status.py<br/>rounds + status"]
        H["extract.py<br/>objective / expected output"]
        I["edges.py<br/>spawn / artifact / message / handoff"]
        J["build.py<br/>RunBuilder.refresh()"]
        K["model.py<br/>Run / Agent / Edge dataclasses"]
        L["redact.py<br/>secret scrubbing"]
        M["http.py<br/>loopback JSON API"]
        N["report.py<br/>static HTML snapshot"]
    end

    subgraph frontend["orchestra/static/ (vanilla JS, no build step)"]
        O["app.js<br/>2s poll loop"]
        P["Timeline / Graph / Activity / Work Floor views"]
    end

    A --> D --> E
    B --> D --> F
    C --> J
    E --> J
    F --> J
    J --> G
    J --> H
    J --> I
    G & H & I --> K
    K -->|"to_summary_dict() / to_detail_dict()<br/>(scrubbed by redact.py)"| L --> M
    M -->|"/api/run, /api/agent/:id"| O --> P
    K -->|"or: baked in once, offline"| N
```

Two ways to view the data, one code path underneath:

- **Live server** ([`http.py`](../orchestra/http.py)): the browser polls
  `GET /api/run` every 2 seconds; `app.js` re-renders whichever tab is open.
- **Static report** ([`report.py`](../orchestra/report.py)): the *same*
  `app.js` and `style.css` are embedded verbatim into one HTML file, along
  with the run data baked in as `window.ORCHESTRA_RUN`/`ORCHESTRA_DETAILS`.
  A tiny shim replaces the network `api()` function with one that reads
  those baked-in objects instead, and turns polling off (`state.live =
  false`). This is why a bug fixed in one view is fixed in both — there is
  only one frontend, ever.

### `RunBuilder.refresh()` — the one function that decides everything shown

Every poll (or report generation) calls `RunBuilder.refresh()`
([`build.py`](../orchestra/build.py)), which:

1. Reads only the **newly appended bytes** of every relevant `.jsonl` since
   last time (`IncrementalReader` — see §5's `transcript.py` entry below). If
   a subagent's log file has shrunk since the last read (truncated or
   replaced), the reader restarts at byte 0 *and* `_scan_subagents` drops
   that agent's existing `AgentDigest` first (`IncrementalReader
   .consume_reset()`) — otherwise the re-read entries would sum their
   tokens and tool calls in on top of what was already tallied.
2. Folds new parent-transcript entries into `ParentIndex` (launches, results,
   notifications) and new per-agent entries into that agent's `AgentDigest`
   (tokens, tool calls, files touched).
3. Determines the full set of agent ids that have ever existed
   (`_agent_ids`), builds each one's `Round` history and `status`
   (`status.py`), extracts its objective/expected-output from its brief
   (`extract.py`), and assembles an `Agent` dataclass. One guard worth
   knowing about here: a **fork**-type agent's own transcript replays its
   full inherited history — including the very entry that launched it,
   tagged (like every entry in that agent's own transcript file) with
   `isSidechain: true` and that agent's own id. Read naively, that looks
   like the agent spawning itself. `_assemble` refuses to let an agent
   become its own `parent_agent_id`, since that can never legitimately
   happen — without the guard, a forked agent shows a self-loop spawn edge
   in the Graph instead of `main → agent`.
4. Infers `Edge`s and file relationships across the *whole* agent set
   (`edges.py`) — this has to happen after every agent is assembled because
   an edge is a relationship *between* two agents.
5. Groups agents into `Batch`es by the assistant-turn `uuid` that launched
   them (parallel waves).
6. Returns one immutable `Run` dataclass. `Run.to_summary_dict()` (light,
   polled every 2s) and `Agent.to_detail_dict()` (heavier, fetched only when
   a drawer opens) both run every string through `redact.py` before they
   leave the process.

This whole method holds a lock (`self._lock`) for its duration — the HTTP
server is multi-threaded, and `IncrementalReader`'s byte offsets and each
`AgentDigest`'s accumulating counts would double-count if two requests
refreshed the same builder concurrently.

---

## 5. Backend module reference

### `locate.py` — finding sessions on disk
The only module that knows Claude Code's on-disk layout
(`~/.claude/projects/<encoded-project-path>/<session-id>.jsonl`, honoring
`CLAUDE_CONFIG_DIR`). `find_session` locates one session by id across *every*
project directory (an id search, not a path guess), so passing a bare session
id always works regardless of which project it belongs to.

### `transcript.py` — incremental reading
`IncrementalReader` remembers, per file, the byte offset already consumed.
Because Claude Code appends to these files *while Cuelight is reading them*,
a partially-written final line is the normal case, not an error: the reader
finds the last `\n` in the chunk it read and only advances its offset past
that point, leaving a torn tail for the next poll to pick up whole. A file
that shrinks (truncated or replaced) resets the offset to 0 rather than
seeking into garbage, and records that fact so the caller can find out:
`consume_reset(path)` returns `True` exactly once for that reset, which
`build.py` uses to drop the affected agent's accumulated `AgentDigest`
before re-ingesting the file from scratch (see §4, step 1) — without that,
a truncated-and-replaced transcript would get its tokens and tool calls
counted twice.

### `parent.py` — the parent transcript's shapes
Deliberately isolated from `build.py` because this is the part most likely to
change between Claude Code versions: it defines `LaunchRecord`,
`ResultRecord`, and `Notification`, and every accessor here tolerates missing
or unexpected fields — a format change degrades the view instead of crashing
the server. Also home to `parse_timestamp`, which turns Claude Code's
ISO-8601 (`...Z`) timestamps into POSIX floats — every duration and every
x-axis position in the UI ultimately traces back to this one function.

That "most likely to change" warning turned out to be exactly right: running
Cuelight against a genuinely live session surfaced a build that delivers a
background agent's `<task-notification>` wrapped in a top-level
`queue-operation` entry (`operation: "enqueue"`) or an `attachment` entry,
neither of which has a `message` field at all. `_notification_text` now
checks both shapes (skipping a queue `"remove"`, which is the same
notification being dequeued, not a second one) before falling back to the
original message-based lookup. Without this, every background agent in such
a session eventually reads as `stalled` no matter how long ago it actually
finished — the completion text was sitting in the file the whole time,
just not where the parser was looking.

### `agentlog.py` — digesting a subagent's own transcript
`AgentDigest` accumulates, per agent: token usage split by type (`input`,
`output`, `cache_read`, `cache_create` — this split is what powers the
cache-hit feature, see the "Header totals" entry in §7), every tool call
(name + a short "target" string extracted from that tool's most relevant
argument — e.g. `file_path` for `Read`, `command` for `Bash`), and the file
paths read/written. It also tracks `ended_mid_tool`: whether the transcript's
last entry was a tool call with no matching result, which the status state
machine (below) uses to tell "process died while doing something" apart from
"process just went away."

**Usage is counted once per API message, not once per entry.** Claude Code
writes one transcript entry per content block (thinking, text, each tool call)
and every entry repeats the whole message's usage — in a real session, 271
assistant entries carried usage for 103 distinct messages, and summing per
entry overstated every token count by 2.6-3.1x. `apply_usage` keys on
`message.id`: a recurring id replaces its earlier usage instead of adding to it;
entries with no id (older formats) are still counted individually. Tokens are
also kept per model (`tokens_by_model`), because a session can switch models.

It also records `model` — the real, versioned model the agent's own
transcript reports (`"claude-sonnet-5"`), which is the ground truth over the
short alias requested at spawn time (`"sonnet"`, carried in `meta.json`);
`build.py` prefers this field whenever it's present (see below). One
wrinkle: Claude Code injects a synthetic wrap-up message on an interrupted
or errored turn with `model` literally set to the string `"<synthetic>"` —
`AgentDigest` ignores that value rather than letting it clobber the last
genuine model the agent actually ran on.

### `status.py` — the agent lifecycle state machine
Two functions, and this is the "formula" behind every colored dot in the UI:

**`build_rounds`** turns a launch + result + notification list into a
`List[Round]`:
- If there are `Notification`s (background-agent completions), one `Round`
  per notification, chained start-to-next-start; if activity was seen *after*
  the last notification, an extra open `Round` is appended — the agent was
  resumed via `SendMessage`.
- Otherwise, an inline result with a timestamp closes a single `Round`
  immediately.
- Otherwise, one open `Round` (still running), or none at all if there's no
  evidence the agent ever started.

**`compute_status`** — precedence, in order:
1. No rounds at all → `unknown`.
2. The last round already has an end → that round's own status
   (`completed`/`failed`) wins, full stop.
3. Otherwise the round is still open. If the **session itself** is no longer
   live (see below), the agent is `failed` if it died mid-tool-call
   (`ended_mid_tool`), else `orphaned` — dying while holding a tool call is
   *positive evidence* of failure; the session simply ending is not.
4. If the session is live but nothing has happened for more than
   `STALL_THRESHOLD_S` (**300 seconds**), the agent is `stalled`.
5. Otherwise, `running`.

A **session** itself counts as "live" (`RunBuilder._session_live`) if its
main transcript file was modified within `SESSION_LIVE_THRESHOLD_S`
(**600 seconds**) of now — this is a mtime check, nothing fancier, which is
exactly why a truly finished session (report generation) never counts as
live.

### `extract.py` — pulling intent out of free text, deterministically
Every subagent's `objective` and `expected_output` fields (shown in the
drawer) are extracted from its raw prompt with **zero LLM calls** — same
input always produces the same output, offline, free. The heuristic, in
priority order:
1. Look for a markdown heading matching a known vocabulary (`## Objective`,
   `## Expected output`, `## Deliverable`, `## Report Contract`, …) and take
   the body under it, stopping at the next heading of equal-or-shallower
   depth.
2. Failing that, look for the first line starting with a recognized
   imperative (`"You are..."`, `"Return..."`, `"Report..."`), plus up to two
   following lines.
3. Failing that, fall back to the agent's `description` field plus the
   brief's first paragraph — and if that fallback text for "expected output"
   turns out to be identical to the objective's fallback text, report
   `"not stated"` rather than showing the same paragraph twice under two
   different headings.

Every extraction is tagged with **where it came from** (`objective_source`,
e.g. `heading "Objective"` or `fallback`) — shown in the drawer as a small
italic caption — so a reader can judge how much to trust it.

### `edges.py` — the four ways one agent's work becomes another's input
This is the graph's evidence engine. All four kinds run over the *whole*
assembled agent list:

| Kind | Confidence | How it's found |
|---|---|---|
| **spawn** | exact | Every agent has exactly one: `parent_agent_id or "main"` → itself. Comes straight from the launch record. |
| **artifact** | exact | Agent A writes path P (via `Write`/`Edit`/`NotebookEdit`) at time *t₁*; Agent B reads P (via `Read`) at time *t₂* > *t₁*. One edge per (writer, reader) pair per file, carrying the write/read timestamps as evidence. Paths are normalized first (backslash→slash, lowercased, and a worktree path like `...\.claude\worktrees\wt\src\a.py` is collapsed onto `...\src\a.py`) so the same file is recognized across worktrees. |
| **message** | exact | A `SendMessage` tool call whose target is a known agent id. |
| **handoff** | **inferred** | No write/read or message connects A and B, but B's *brief* substantially reuses words from A's *result*. See the formula below. |

**Hub files.** A path read by more than `HUB_FILE_THRESHOLD` (**3**) agents
but written by none of them isn't a dependency — it's shared context (a plan
file, a spec) — so it's collapsed out of the graph entirely and listed
separately as a "shared context" note instead of drawn as a fan of edges.

**Write conflicts** (added after the initial build, alongside `hub_files`):
the same write-tracking map, read the other way — if **more than one agent**
wrote the same path, that's surfaced as a correctness flag, not a
dependency. See the "Write-conflict box" entry in §7.

**The handoff heuristic, precisely.** For candidate pair (A→B) where B
started after A ended: split both texts into lowercase words, take all
overlapping windows of `SHINGLE_SIZE` (**8**) consecutive words (a
"shingle") from each, and compute

```
containment = |shingles(A.result) ∩ shingles(B.brief)| / |shingles(A.result)|
```

plus the length of the single longest contiguous word run the two texts
share (`difflib.SequenceMatcher.find_longest_match`). The edge is drawn only
if `containment ≥ HANDOFF_CONTAINMENT` (**0.15**) **or** the longest run is
≥ `HANDOFF_RUN_WORDS` (**40**) words — and if an *exact* edge already
connects the same pair, the handoff evidence is folded into it instead of
drawn separately (so a pair never has to be second-guessed twice). Every
handoff edge ships its score and the matching snippet as evidence, shown
when you click the edge — the dashed line is a claim you're meant to be able
to check, not a fact.

### `redact.py` — the one chokepoint everything passes through
`scrub()`/`scrub_obj()` run on every string right before it's serialized —
never earlier, so a credential can't be half-stripped by an intermediate
truncation and survive the other half. It matches, in order (more specific
patterns first, so a generic one can't eat a more specific one's prefix):
private-key PEM blocks, JWTs, Anthropic/OpenAI/GitHub API key shapes, AWS
access-key ids, `Bearer <token>` headers, and `key = value` /
`key: "value"` assignments where the key name looks credential-shaped
(`password`, `secret`, `api_key`, `access_token`, …) — replacing only the
*value*, so `api_key=<redacted:secret>` stays readable as a shape without
leaking the content.

### `model.py` — the shapes everything else builds
Plain dataclasses, no behavior beyond a few computed properties
(`Agent.duration_s`, etc.) and two serialization methods per class:

- **`to_light_dict()`** — what the 2-second poll loop sends: enough for the
  Timeline and Graph (status, timing, token totals, a capped 200-character
  objective), nothing large (no full tool-call list, no full brief).
- **`to_detail_dict()`** — what a drawer fetch gets: everything, uncapped —
  full brief, full result, the complete tool-call list, every file path.

This light/heavy split is why opening a drawer makes a *second* network
request rather than the summary already containing everything.

### `http.py` — the server
A `ThreadingHTTPServer` bound to `127.0.0.1` only. See
[§8](#8-security--privacy-model) for the full security model. Three JSON
routes (`/api/run`, `/api/agent/<id>`, `/api/sessions`) plus static file
serving for the dashboard's own HTML/JS/CSS, with a `realpath`-based
containment check so a symlink planted under `static/` can't be used to read
files outside it.

### `report.py` — the static snapshot
Renders one self-contained `.html` file: the same page shell, the same
`style.css`, and `app.js` with a small string-replacement shim
(`_offline_shim`) that swaps its `api()` function for one reading
`window.ORCHESTRA_RUN`/`ORCHESTRA_DETAILS` (baked into the page as JSON) and
turns polling off. Every literal opening-angle-bracket in the JSON payload is
escaped to its six-character Unicode form (backslash, `u`, `0`, `0`, `3`,
`c`) before embedding, so a brief or result containing a literal closing
script tag can't break out of the inline `<script>` tag it's embedded in.
The Work Floor's sprite sheet travels the same way: a static report has no
server to fetch `agent-sprite.png` from, so `_agent_sprite_data_uri()`
base64-encodes it into `window.ORCHESTRA_AGENT_SPRITE` as a `data:` URI
instead.

### `__main__.py` — the CLI
`start` / `stop` / `report` / `--export` / `--advise` / `--estimate` / the hidden `--serve` (what the
detached child process actually runs). Covered in [§2](#2-how-you-run-it--the-cuelightopen-slash-command).
`--advise` builds the run once with the past-runs numbers (`cmd_advise`: a foreground scan, cached after
the first time) and prints `advice.render_text`; `--estimate` prints `baselines.render_estimate`. Both print
through `_print_safe`, which turns a character the console's code page lacks into `?` instead of failing.

---

## 6. Frontend architecture (`orchestra/static/`)

No build step, no framework, no dependency — `app.js` (plus `tabs.js`, below), one
`style.css`, one `index.html`, plain `fetch`/DOM/SVG APIs. The code is organized
around one `state` object and one `render()` dispatcher:

`tabs.js` holds the views that are tabs of their own (Prompts, Agents, Spend), and `advice.js`
the Advice tab (below). Each is a classic script loaded after `app.js`, so it shares `app.js`'s top-level globals
(`state`, `esc`, `insCard`, `openDrawer`...) and `render()` calls into it. It exists
because the plugin directory takes no non-image file over 256 KiB and `app.js` reached
that limit in 0.19.0. The static report inlines all three, in that order (`app.js`, `tabs.js`, `advice.js`);
`tests/test_insights_ui.run_js` reads all of them, so a test can extract a function from any file.

```js
state = {
  run,                 // the last /api/run payload
  view,                 // "timeline" | "graph" | "activity" | "workfloor"
  filterText, filterStatuses,   // the filter bar's current selection
  toolCache, ticker,    // the Activity tab's accumulated feed
  seenEdgeKeys, graphSeeded,     // which graph edges have already been drawn
  notifyEnabled, notifySeeded, knownFailedIds, lastSessionLive,  // desktop alerts
  floorActivity, floorSeeded,    // Work Floor: per-agent activity deltas since last render
  agentPrevStatus, agentCelebrateUntil,  // Work Floor: one-shot "just completed" jump burst
  agentSprites,         // Work Floor: agent_id -> mounted AgentSprite canvas animator
  generation,           // see below
  ...
}
```

**The polling loop** (`poll()`) fetches `/api/run` every `POLL_MS`
(2000ms) and calls `render()`, which re-renders the header, health box,
conflicts box, filter chips, and whichever of the four views is active.
Every loop iteration is tagged with a **generation number**
(`state.generation`); toggling "Live" off/on or switching sessions bumps it,
and any in-flight response whose generation has gone stale is discarded
instead of overwriting a newer view — without this, two poll loops (an old
one winding down, a new one starting) could race and flicker between two
sessions' data.

**Keyboard focus across redraws** (0.21.1). A view drawn with `innerHTML` replaces every
control on each poll, which drops keyboard focus to the page. Each such view (Prompts, Insights,
Agents, Spend, Work Floor, Fleet, History) calls `noteFocus(box)` before writing its markup and
`restoreFocus(box, spot)` after binding its handlers. `noteFocus` names the focused control by the
first of `FOCUS_NAMES` it carries (`data-agent`, `data-session`, `data-prompt`, `data-fold`,
`data-jump`, `data-view`, `data-sort`, `data-split`, `data-copy`, `data-hide`), a `<summary>` by its `<details>`, and a control
with none of them by its tag and first class (the Fold all button, the "needs a look" toggle, the Spend
chart), plus which of the matches it was (an agent can be named twice in one view). A control that is
gone after the redraw leaves focus where it fell rather than moving it somewhere unexpected.
`tests/test_focus_ui.py` redraws each view over a small parsing DOM stub and checks focus lands on the
same control.

**Keyboard shortcuts** (`onGlobalKey`, on `document`). Number keys `1`-`9`, `0` switch to the first ten views in
the order of `PALETTE_VIEWS` (not the visible tab order, where Advice sits fifth); `A` opens Advice;
letters run commands. None fire with a modifier, while typing in a field, with
the palette or help open, or for a key a focused control already handled (`event.defaultPrevented`): the
graph's own handler fits it on `0`, which before 0.21.3 also jumped to History.

**Text fitting.** SVG has no `text-overflow: ellipsis`. `fitText()` measures
real pixel width with an offscreen `<canvas>` 2D context
(`context.measureText`) and binary-searches for the longest prefix (plus an
ellipsis) that fits — never truncating by character count, which lies the
moment a label mixes wide and narrow glyphs. (It falls back to a rough
per-character estimate if no canvas context is available at all — the
project's own headless-Node render-check harness is one such environment —
so a constrained context degrades gracefully instead of throwing.)

**Motion.** The drawer is a real modal, not a page that happens to have a
panel on it: opening it shows a dimmed, blurred backdrop (`.scrim`) behind a
`transform: translateX` slide-in, and clicking the backdrop closes it exactly
like Escape does. A running agent's status dot/node border pulses with a
CSS `ping-ring` animation, and its open-ended bar breathes, so "in progress"
reads as motion, not just a color you have to notice. Every one of these is
guarded by `@media (prefers-reduced-motion: reduce)`.

**The four views** (`renderTimeline`, `renderGraph`, `renderTicker`,
`renderWorkfloor`) are detailed feature-by-feature in the next section.

---

## 7. Every feature, what it shows, and why it exists

### Header totals + cache-hit readout
Agent counts, total tokens, wall time, and:

```
cache_hit_ratio = cache_read / (input + cache_read + cache_create)
```

— the fraction of *context* tokens (output is never cacheable, so it's
excluded from the denominator) that were served from Anthropic's prompt
cache rather than paid for fresh. Shown session-wide in the header and
per-agent in the drawer (with the full input/cache-read/cache-write/output
breakdown). **Why it matters:** it's the single number that tells you
whether your prompt-caching setup is actually working on a multi-agent run —
previously invisible, even though the data was already being collected.

### Health box
Lists every agent whose status is `stalled`, `failed`, or `orphaned` —
click one to jump straight to its drawer. The dashboard's answer to "did
anything go wrong," without scanning 20+ rows to find out.

### Write-conflict box
Lists every file path written by **more than one** agent, and by which
agents. Unlike a hub file (read by many, written by none — informational),
a write conflict is a real correctness risk: two agents independently
editing the same file is exactly how a merge silently loses one side's
change. In the demo session, this genuinely catches later "fix" batches
touching files an earlier batch already wrote — which is expected in an
iterative build, but exactly the kind of overlap you'd want flagged if it
weren't.

### Filter bar
A pill-shaped search box (inline icon, a focus glow instead of the default
outline, a clear button) matching description, agent id, type, model, and
the capped objective, plus status chips built dynamically from whatever
statuses are actually present in the current run (no point showing an
"orphaned" chip when nothing is orphaned). A live "N of M shown" counter
appears next to the box the moment a filter actually narrows something, and
disappears at rest rather than duplicating the header's own agent count.
Filtering **dims** non-matches rather than removing them — in the Timeline,
dimmed rows keep their slot so the layout never jumps; in the Graph, dimmed
nodes stay in place so edges never have to be recomputed around a missing
node. The same filter state is honored by the Activity ticker too, so "show
me only what agent X did" works everywhere.

### Timeline
One row per agent: a status-colored dot, its description and
type/model, and a duration bar positioned on a shared time axis (with
gridlines every quarter of the session's wall-clock span). Batch bands
(translucent, labeled "batch 1", "batch 2", …) shade the rows launched in
the same assistant turn, so a wave of parallel work reads as one visual
block instead of N separate rows that happen to overlap. Rows alternate a
faint background tint (zebra striping) purely so your eye doesn't lose the
row on a wide screen, and hovering a row highlights its full-width band —
important because a short-duration agent's bar can be only a few pixels
wide, far too small a target to hover reliably on its own. A `running`
agent's dot pulses with a ping ring and its still-open bar breathes — a
plain blue dot doesn't read as "actively happening" the way motion does.

### Graph
One node per agent (plus a synthetic "orchestrator" root), laid out
left-to-right by dependency rank (computed as **longest path over exact
edges only** — an inferred handoff edge is never allowed to move a node,
so a bad guess can't rearrange the whole picture) and top-to-bottom by a
two-pass barycenter sweep (minimizing edge crossings, the same technique
Sugiyama-style graph layouts use). Solid edges are exact; dashed are
inferred handoffs (click one for its evidence — see the `edges.py` entry in §5).
Hovering a node dims every edge *not* touching it, so tracing one agent's
connections in a dense graph doesn't require following lines by eye.

**Critical path.** A second, duration-weighted longest-path pass (separate
from the layout rank above, which only counts hops) finds the actual chain
that determined the session's wall-clock time:

```
pathDuration(node) = node.duration + max(pathDuration(parent) for parent in structural-parents)
```

computed with the same bounded-relaxation loop the rank calculation uses (so
it's cycle-safe against the same artifact-edge cycles that can occur when
two agents each read what the other wrote). The node with the largest
`pathDuration` is walked back to its root, and that whole chain is drawn in
accent color. **Why it matters:** hop-count rank treats a 5-second agent and
a 5-minute agent as equally "one step" — duration-weighting answers the
actually useful question, "if I want this to finish faster, which agent do I
need to speed up?"

**Live packet flow.** Every edge key (`src>dst>kind`) already seen is
tracked across polls; the *first* render of a session seeds this set
silently (nothing on it "just happened" — it's all pre-existing history),
but any edge key that appears on a **later** poll is a real event — a file
handoff or message just detected between one 2-second poll and the next —
and gets a one-time dot animated along its exact path (SVG `animateMotion`,
about a second) before it settles into an ordinary static line. Switching
sessions reseeds for the same reason a fresh page load does. A `running`
node gets the same pulsing-ring treatment as a running Timeline row.
**Why it matters:** watching a real multi-agent session live, you see a dot
actually fly from one agent to the one that just picked up its output — the
graph reads as something happening, not a static diagram that occasionally
gets redrawn.

### Agent drawer
Every field extracted for one agent: status, type, a friendly model form
(`sonnet-5`) with the exact reported string alongside it (`claude-sonnet-5`)
when they differ, launch mode, duration, token totals + cache-hit breakdown,
round/tool-call counts, the extracted objective and expected output (each
with its `_source` caption), the returned result, the full brief and
tool-call list behind `<details>`, and every file read/written. Opening the
drawer shows a dimmed, blurred backdrop behind a slide-in panel — click the
backdrop or press Escape to close, both reversing the same animation
(disabled under `prefers-reduced-motion`).

**Tool mix.** A small stacked bar classifying that agent's tool calls into
four fixed buckets — **Read** (`Read`, `Grep`, `Glob`, `NotebookRead`,
`WebFetch`, `WebSearch`), **Edit** (`Edit`, `Write`, `NotebookEdit`),
**Bash**, **Task** (`Task`, `Agent`) — plus an **Other** catch-all for
anything else (`TodoWrite`, MCP tools, …). **Why it matters:** it's a
behavioral fingerprint at a glance — an agent that's almost all Bash with no
Read/Edit is doing something different from one that's mostly Edit, and
spotting a runaway agent (hundreds of one kind of call) doesn't require
reading the raw tool-call list.

### Activity tab
A merged, newest-first feed of tool calls across agents, colored by the same
four-bucket palette as the tool-mix bar, click-through to the source agent's
drawer. It actively polls (fetching full detail) only for agents currently
`running`, and only while the tab itself is open — so watching Timeline or
Graph never costs the extra per-agent requests. Opening any agent's drawer
also opportunistically folds that agent's history into the same feed
(deduplicated against what's already there), so browsing around
organically builds up a fuller picture even without leaving the tab open.
**Why it matters:** the Timeline tells you *when* something happened; the
Activity tab is for *watching it happen* — the difference between a
photograph and a live camera.

### Work Floor
Every agent as a small animated pixel-art sprite, grouped into sections by
`agent_type` (sorted alphabetically, "Ungrouped" always last since it's a
catch-all rather than a real role) with a group header showing agent count
and total tokens. Clicking a card opens the same drawer every other view
uses.

The sprite engine (`sliceSpriteSheet`, `AgentSprite`, the frame-rate table)
is a JS port of [ntd4996/agentpet](https://github.com/ntd4996/agentpet)
(MIT-licensed; the license text travels with the code in `app.js`), and
`static/agent-sprite.png` is that project's own bundled sample character
sheet, served locally — never fetched from their CDN — to keep the
no-egress guarantee. One character sheet stands in for a whole floor of
agents: each canvas is hue-rotated by a hash of its agent id
(`agentHue`), so a crowded floor still reads as individuals without
needing more art. Row + frame-rate is picked from status (`running`,
`completed`, `failed`, `stalled`, `orphaned`/`unknown` each map to a
sheet row), and a live transition *into* `completed` (not just already
being completed when the tab opens — same seeding rule as the graph's
packet flow and desktop notifications) fires a one-shot 3-second "jump"
burst before settling into the normal completed pose.

A card also **pulses** the moment its tool-call count or token total
increases since the last render (again, only after the first render has
seeded a baseline), and its clock ticks locally every second
(`tickAgentClocks`) rather than waiting on the next 2-second poll — the
same reasoning as the Timeline's breathing bar: a number that visibly
moves reads as "alive" in a way a value that jumps only every two seconds
doesn't. All of this is inert in the static report: `state.offline` freezes
the clock at the run's own `last_activity_at`, and the sprite animation
loop itself respects `prefers-reduced-motion` (holding the current frame,
which is still the right pose for the status, instead of cycling).
**Why it matters:** the other three views are analytical; Work Floor is
the "glance at a monitor across the room and know the run is healthy" view
— status, activity, and completion are all legible from motion alone,
without reading a single row.

### Deep links
Opening a drawer updates the URL to `#agent=<id>` (via `history.replaceState`,
so browsing from agent to agent doesn't spam back-history); loading a page
with that hash already set opens straight to that agent. A failed-agent
health-box entry, a write-conflict list item, and a ticker row are all now
pasteable links — into a PR description, a Slack message, wherever the
conversation about that specific agent is happening.

### Notifications
A "Notify" toggle next to Live requests `Notification` permission on first
click and remembers the choice in `localStorage`. Once enabled, the
dashboard alerts on two things happening **while the tab is open**: an
agent transitioning to `failed`, and the session ending. Both are seeded
the same way the graph's packet flow is — the first poll only records what
already exists, so opening the dashboard on a session that already has a
failure or has already ended never fires a burst of notifications for
history the Health box already shows at a glance. The static report hides
this toggle entirely: nothing new ever happens in a frozen snapshot.

### Copy summary
Builds a plain-markdown summary of the current run — the totals line, a
"Needs attention" section listing stalled/failed/orphaned agents, and a
"Write conflicts" section, each omitted when empty rather than printed as a
header with nothing under it — and copies it via the Clipboard API, with a
brief "Copied!"/"Copy failed" confirmation on the button itself. Visible in
*both* the live dashboard and the static report — unlike Live/Notify, a
frozen snapshot is exactly what you'd want to summarize for a PR
description or a status update, not something the feature is meaningless
for.

### Session picker + Live/Paused toggle
`/api/sessions` lists every session found in the current project directory
(newest first); switching one bumps the poll `generation` and restarts
against the new session. "Live"/"Paused" is a manual override on top of the
automatic 2-second poll — useful when you want to freeze the view to read
something without it re-rendering under you.

### Static HTML report
`/cuelight:open report` produces the same UI as a single file with everything
baked in — safe to email or attach to a PR, no server, no network call,
works from a `file://` URL in a real browser. (The one thing it can't do is
show *live* activity, since there's no running server to poll — the Activity
tab correctly shows its empty state rather than pretending to be live.)

---

## 8. Security & privacy model

- **Loopback-only.** `ThreadingHTTPServer` binds `127.0.0.1` explicitly, and
  every request additionally re-checks its `Host` header names a loopback
  address (`_host_is_loopback`) — defense in depth against DNS rebinding.
- **Origin-checked.** Any `Origin` header naming something other than
  loopback is refused (`_origin_is_allowed`), so a malicious page open in
  another tab can't read the dashboard's JSON even though it's on
  `localhost`.
- **Token-gated.** Every `/api/*` route except `/api/health` requires a
  token minted fresh at launch (`secrets.token_urlsafe(24)`), compared with
  `hmac.compare_digest` — not `==` — specifically because this token is the
  only gate standing between the outside world and every prompt and result
  the dashboard holds.
- **No egress, ever.** No CDN script, no web font, no analytics — every
  asset ships in the package (enforced by `tests/test_static_assets.py`,
  which greps for external `src`/`href`/`fetch` and fails the build if it
  finds one).
- **Never touches `~/.claude`.** Cuelight only *reads* transcripts; nothing
  it does writes back into Claude Code's own state. Since 0.22.0 it also reads
  past sessions in every project (read-only, `baselines.py`) and keeps numbers
  only, in its own state directory (`baselines-v2.json`, mode `0600`).
- **Advice never acts.** The fix-it cards (`advice.py`) are text to copy.
  Nothing in Cuelight writes a settings file, an agent definition or a
  `CLAUDE.md`, and `commands/open.md` tells Claude not to change a file
  because of `advise` unless the user asks.
- **Redacted at the one chokepoint** — see [`redact.py`](#redactpy--the-one-chokepoint-everything-passes-through)
  above.
- **Path-traversal guarded.** Static file serving resolves the real path
  (`os.path.realpath`, not a textual `..` check) and refuses anything that
  doesn't land back inside `static/` — a symlink planted there can't be used
  to read arbitrary files.

---

## 9. Testing

```bash
python -m unittest discover -s tests -t . -v
```

Standard library only (`unittest`), about 1,340 tests, no external dependencies.
Coverage spans: every backend module's pure logic (fixture-built fake
sessions, no real transcripts needed), the HTTP server's routing and auth
checks, the static-asset invariants (no network egress, every `$("id")`
app.js touches exists in *both* `index.html` and the report's embedded
shell, every interpolated value passes through `esc()`), and — critically —
`tests/test_report_renders.py`, which executes the actual generated report's
JavaScript under Node with a minimal DOM stub and asserts it renders content
rather than throwing. That last one exists because of exactly the failure
mode it's named for: a missing DOM id, or a browser-only API call, can make
`app.js` throw before it draws anything while every *text-based* assertion
about the page still passes.

The advice layer has its own files: `tests/test_advice.py` (every rung of the failure ladder and every
rule at its trigger, below its threshold and merged; the pre-approve safety checks; the verdict and
the order), `tests/test_baselines.py` (grouping, the project then all then not-enough fallback, median
and 90th percentile, cache reuse, refresh, drop, corrupt and old caches, the private file mode, numbers
only), `tests/test_listprices.py` (precedence, `off`, a broken file, the labels) and
`tests/test_advice_ui.py` (the tab under the Node DOM stub: escaping, Copy, Hide, the badge, the `A` key,
focus kept across a redraw, the usual table).

---

## 10. Live layer (added after the original build)

The original design reads transcripts only. Everything below adds a second,
*live* source and builds on it. Hooks are an enhancement: with them off
(`ORCHESTRA_EVENTS=off`) or absent, the dashboard is exactly the transcript-only
product above.

### Live events (`events.py`, `hook.py`, `hooks/hooks.json`, `statedir.py`)
A plugin can ship hooks, so Cuelight registers small **async** command hooks for
seven events (session start/end, subagent start/stop, notification, API failure,
turn end) — deliberately *not* per-tool events, which are already in the
transcripts and would cost a process spawn per call. `hook.py` normalizes the
payload to an agent-neutral `Event` (only whitelisted fields, redacted *before*
the length cap, never tool inputs or prompts) and appends one line to
`<state dir>/events/<session>.jsonl`. It always exits 0 and is silent. The spool
tolerates torn lines, rotates by inode (size alone misses a same-size
replacement), and prunes after 7 days. `statedir.py` makes the state dir
per-user, `0700`, and refuses a symlink or a dir owned by someone else.

### Ground-truth state (`livestate.py`)
A transcript can't say an agent is *waiting for you*. A hook can, but only that a
prompt *appeared* — nothing says it was answered. The transcript does: an
attention item is pending iff no transcript activity followed it (read from
entry timestamps, not mtime, which moves on bookkeeping lines). `SessionEnd`
makes a session non-live at once (was a 10-minute mtime guess); `SubagentStop`
closes an open round at the exact time; an agent blocked on a prompt is the new
`waiting` status instead of `stalled`.

### Live push (`/api/stream`)
Server-Sent Events carry only a change fingerprint (stat of the transcripts and
spool); the browser then fetches `/api/run` as before, so redaction stays in one
place. Capped at 8 streams, 1-hour lifetime, keep-alives. Polling remains as a
15 s safety net and as the fallback.

### Fleet, pill, sounds
`/api/fleet` lists sessions active in a window across *all* projects, most urgent
first, building at most `max_builders - 1` runs per request. The pill is a
Document Picture-in-Picture window (Chromium only) plus a tab-title count and a
canvas favicon; sounds are Web Audio tones (no audio files). Their decisions are
pure functions in `app.js`, tested under node.

### Money, loops, token accuracy (`pricing.py`, `runaway.py`, `agentlog.py`)
- **Usage is counted once per API message.** Claude Code writes one entry per
  content block, each repeating the message's usage (a real transcript: 271
  entries, 103 messages), so summing per entry inflated every token count
  2.6-3.1x. `apply_usage` keys on `message.id`; tokens are also kept per model.
- **Cost** uses the user's price file when there is one, else the built-in dated list prices
  (`listprices.py`, below); `ORCHESTRA_PRICES=off` turns it off. It
  covers the orchestrator's own transcript (`TokenTally`) and each agent priced
  per model; unpriced models mark the total *partial*; `ORCHESTRA_BUDGET` warns
  at 80 % and alerts when exceeded.
- **Possible loops**: an *open* agent whose last N calls are identical, or strictly
  alternate between two. Reported as *possible*, with evidence.

### Replay, export, history
- **Replay** (`deriveRunAt`) rebuilds a run at time *t* from round start/end
  times — works in a static report. It does not invent what it cannot know: an
  open round replays as `running`, and token/cost data are shown as unavailable.
- **Export** (`export.py`): CSV (one row per agent, stable columns, formula
  cells neutralised) and JSON, from the same scrubbed summary.
- **History** (`history.py`): opt-in sqlite, **metrics only**, `0600`, pruned by
  age and count; off creates nothing.

### Insights, search, demo (`insights.py`, `search.py`, `demo.py`)
- **Insights** is a pure function of the `Run`, filled in by `RunBuilder` and carried
  in the summary payload (so a static report and an export have it too). Parallelism
  is a sweep over round start/end events (ends sort before starts, so back-to-back
  agents do not read as overlap). The **critical path** is the duration-weighted
  longest chain over *exact* edges only (`spawn`, `artifact`, `message`), counting only
  the time an agent adds beyond its predecessor's end, on forward-in-time edges, so a
  cycle can never loop it. Every list is capped; every transcript string is scrubbed.
- **Search** (`GET /api/search?q=`) matches the **scrubbed** text, never the raw
  transcript: matching raw text and scrubbing only the output would let a caller
  probe a redacted secret one character at a time. Identical calls by one agent
  collapse into one counted row. A static report searches the details baked into it
  (`localSearch`).
- **Activity search** (`GET /api/calls?q=&failed=1`, `search.calls`) is the same matching
  over every tool call in the run (tool, scrubbed target, agent), or only the failed ones
  (`ToolCall.ok is False`; no query needed then). Identical calls by one agent are one row
  with `count`, `failed` and the latest one's time and `ok`; newest first; `matched` counts
  calls; at most `CALL_ROWS` (300) rows, 20,000 calls scanned. In `tabs.js`, `localCalls`
  applies the same rules to a static report's details (a test checks the two agree on a
  whole demo run), `markTerms` marks matches piece by piece, each piece escaped, and
  `renderTicker` hands the list over to `renderCalls` while `activitySearching()`. A live
  session asks again on every poll.
- **Demo** (`python -m orchestra --demo`) builds a fixed, seeded 13-agent scenario in a
  temp directory (own `CLAUDE_CONFIG_DIR`, state dir and price file), then a simulator
  thread appends tool calls to the running agents and re-asserts a pending permission
  prompt after each tick. Nothing is written outside the temp directory, which is
  removed on exit.

### Pulse (`insights.pulse`, `agentlog.token_events`)
- The live strip's series are part of the Insights payload, so a static report has them too.
  `AgentDigest` now records `(timestamp, fresh tokens added)` per API message (once per
  `message.id`, like the totals; capped at 4,000 events per agent, past which new tokens
  fold into the last entry). `insights._pulse` buckets tool calls and cumulative fresh
  tokens into 48 slices of one shared window (first agent start to *now* while the session
  is live, so the right edge moves), reuses the parallelism step series for "agents
  running", derives per-minute rates from the last 60 s against the 60 s before, and keeps
  the latest 40 start/finish/failure/stall markers.
- Front end (`pulseModel`, `renderPulse`): pure geometry functions turn each series into
  an SVG path with a zero baseline (a flat or empty series cannot divide by zero, and a
  small wobble never looks like a crash). The strip is built once and then updated in
  place on every poll, so the live dot keeps pulsing; ages on the tape tick locally every
  500 ms. It hides itself during a replay (the insights are for the live run). Colour
  stays on the data: only the running series and the problem pills use status colours.

### Waiting on you (`livestate.waits`, `insights.waits`, `agentlog.ActivityTimes`)
- A permission or input prompt (not the idle "waiting for your input" notice, and not an
  API error) opens a wait for its agent; no agent id means the main session. The wait ends
  at that agent's first transcript entry more than `GRACE_S` after the prompt (the main
  session: anyone's), the same rule that clears the `waiting` status. A prompt raised again
  before that entry extends the wait instead of starting another.
- To find "first entry after t", every transcript keeps its entry timestamps sorted
  (`ActivityTimes`, capped at 50,000 per transcript) and is searched with `bisect`.
- No later entry: the wait is **open** while the session is live (it counts up to now) and
  **unanswered** once it is not. Unanswered waits are counted but given no length, since
  nothing says how long they would have taken.
- Known limit: after you approve a command the agent's next entry is the command's result,
  so the wait includes the command's run time. Real transcripts record no duration for Bash
  results (checked), so it cannot be subtracted; the card says so.
- `insights.waits`: your time is the union of the wait intervals (overlaps once), agent
  time is their sum, plus the longest wait, a per-agent ranking (top 8) and the 12 newest
  waits with their scrubbed prompt text. Agents carry `waited_s`, `wait_count` and
  `wait_open_since` for the panel. Open durations tick locally every 500 ms
  (`tickWaits`, any element with `data-wait-since`); a static report shows them frozen at
  the time it was written.

### Did they check their work? (`verify.py`)
- Each tool call is classified when it is read (`verify.classify`, on the full input, since the
  stored target is cut at 120 characters): **edit** (Write/Edit/NotebookEdit of a code or config
  file, not in a `scratchpad`/`scratch`/`tmp`/`temp` folder), **check** (a Bash/PowerShell command
  matching the built-in list of test, build, type-check and lint commands, or
  `ORCHESTRA_VERIFY_PATTERN`), or **run** (an interpreter followed by code files; `ref` keeps their
  names). Here-document bodies are removed first, so a commit message that mentions `npm test` is
  not a check, and names must stand alone (`vitest.config.ts` is not `vitest`).
- The tool result's `is_error` sets `ToolCall.ok` (real transcripts mark a non-zero exit this way).
- `verify.assess`: after the last edit, the last check (or a run of the file edited last) decides
  **checked** or **failing**; none is **unchecked** (with `checked_before` when one ran earlier).
  `final` is set once the agent has finished, and only then does the Health box list it.
- Measured on the owner's real transcripts before release: 172 agents, 58 edited code and all 58
  checked. The first draft flagged 11, all throwaway scripts in scratch folders; that is why scratch
  files are excluded and running the edited file counts.

### What changed (`changes.py`)
- When an edit succeeds, Claude Code writes the patch it applied beside the result
  (`toolUseResult.structuredPatch`: hunks with `oldStart`/`newStart` and ' ', '-', '+' lines) and, for
  a new file, `type: "create"` and its content. `AgentDigest` keeps each edit's input until its result
  arrives, then `changes.extract` builds `{path, created, hunks}` from that record, or from the input
  (Edit's `old_string`/`new_string`, MultiEdit's edits, Write's content) when there is none. A result
  with `is_error` is skipped. An entry that carries several tool results has one `toolUseResult` that
  cannot be matched, so it is not trusted for any of them.
- `ChangeLog` groups by file in first-touched order, keeps at most 1,500 diff lines per agent (counts
  stay complete; `truncated` marks the cut) and 200 files. The builder hands each `Agent` a snapshot, so
  a later read never changes a run that is being serialised.
- Output: the light payload carries totals (project files only); the detail payload carries
  `change_files` with the hunks, each hunk scrubbed as one text (a PEM block spanning lines is caught)
  and long lines cut after scrubbing. Static reports embed every agent's detail, diffs included.
- `insights.changes`: totals, agents ranked by lines changed, and the most changed files, keyed by
  `edges.normalize_path` so worktree copies of a file are one file. Scratch/temp files
  (`verify.is_scratch`) are listed in the panel but never counted.

### What the run produced (`outcomes.py`)
- Claude Code records git results on a Bash call's result: `toolUseResult.gitOperation` with `commit`
  (`sha`, `kind`, `branch`), `push` (`branch`), `pr` (`number`, `url`, `action`) or `branch` (`ref`,
  `action`), and writes `pr-link` entries (`prNumber`, `prUrl`, `prRepository`) to the main transcript.
  Checked on real transcripts. `OutcomeLog.ingest` pairs each Bash/PowerShell call with its result
  (incrementally, like the digests) and records those; one log per agent and one for the main session.
- The commit id comes from git's own output, so `git commit -q` gets no `commit` record. A successful
  command that runs `git commit` on its command line (here-document text excluded, `--dry-run`
  excluded) still counts, keyed by its tool call id, with no sha. The first message line is read from
  `-m "..."`, `-m '...'` or a here-document, for display only.
- Test runs reuse `verify.classify`: a check command's result error flag says passed or failed.
- `outcomes.summary` merges every log, removing duplicates by commit key and pull request URL (a forked
  agent replays its parent's history) and attributing each to the first transcript that recorded it;
  cost and fresh tokens per commit and per pull request divide the whole run's figures. Only `https://`
  URLs are kept, and the page links them with `rel="noopener noreferrer"`.

### What each agent was told (`context.py`)
- Every transcript, subagents' included, carries attachment entries: `instructions` when a context
  starts (`files`: `path`, `type` such as User, Project, Local or AutoMem, `content`), `nested_memory`
  when a CLAUDE.md is loaded because the agent touched a file under it (`path`, and `content` holding
  the same dict, sometimes as its Python repr), and `skill_listing` (`names`, `skillCount`). Checked on
  real transcripts. This was planned as an `InstructionsLoaded` hook (Claude Code 2.1.288 added
  `agent_id` to it); the transcripts already hold the same facts, need no install step and cover past
  sessions, so no hook was added.
- `ContextLog` keeps path, type, size, time and how each file arrived; never the content. Paths are
  keyed by `edges.normalize_path`, so worktree copies are one file.
- `context.coverage`: the main session's Project and Local files are "your project instructions";
  each agent missing any of them is listed with its agent type (some types may be meant to run without
  them, so the card states what happened, not a verdict). User and AutoMem files are not required.
- Regression found in a real browser before release: the main session's log was reset on every
  refresh. `test_what_the_main_session_recorded_survives_later_reads` refreshes again after the
  transcript grows, for the instruction files and the outcomes alike.

### Catch me up (`sessionmeta.py`)
- Claude Code writes, into the main transcript: `ai-title` (`aiTitle`, generated, repeated through
  the file), `custom-title` (`customTitle`, from `/rename`, also repeated), a `system` entry with
  subtype `away_summary` (`content`, `timestamp`: the "while you were away" recap) and `last-prompt`
  (`lastPrompt`, the start of your last prompt; no timestamp). Checked on 57 real sessions: 40 titled,
  35 with a recap (at most ~340 characters), none in subagent files.
- `SessionMeta` keeps the latest of each on one line, scrubbed before it is cut (title 80, recap 400,
  prompt 200 characters). A title you set wins over Claude's. A recap is `recap_stale` once a user or
  assistant entry arrives more than `STALE_AFTER_S` (60 s) after it, because it then no longer
  describes the session. The builder resets it only with the other main-session logs.
- `Run.meta` becomes the summary's `session` block; the fleet entry copies it. Lists that must not
  build a run per session (the session picker, fleet rows beyond the build budget) use
  `peek_title`: it reads the last 256 KB of the transcript, then the first 256 KB if the end has no
  title, and caches by (path, size, mtime). On real data it matched a full read for all 40 titled
  sessions and took 26 ms cold for a 19-session, 19 MB project.
- UI: `fleetRow` / `fleetSub` / `fleetStatus` (what needs you, else the recap with its age, else the
  last prompt, else the status, which then moves to the meta line), `sessionLabel` for the picker
  (plain text, set through `textContent`), `recapHtml` / `renderRecap` for the bar under the header
  (one line; `aria-expanded` toggles the rest), and the title in `tabTitle`, the pill and fleet alerts.
  Static reports carry it too (`<title>` is HTML-escaped).
- `demo.write_side_sessions` gives the demo's Fleet four neighbours (titled with a fresh recap,
  renamed with a stale one, last prompt only, untitled), written in the real format and read by the
  real code; only `--demo` writes them.

### Where tokens were wasted (`waste.py`)
- `WasteLog` (one per transcript: the main session's, and each agent's digest) keeps each API call
  once per `message.id`, latest usage winning (Claude Code repeats a message's usage on every content
  block): first-seen time, model, `cache_creation_input_tokens`, `cache_read_input_tokens`. It also
  keeps `compact_boundary` times, the largest tool results (text length, tool, scrubbed target) and
  Read results whose content fingerprint matches the previous read of the same file and range (errors
  are not re-reads).
- A rebuild is a call after the first that writes at least `REBUILD_MIN_TOKENS` (10k) to the cache
  while reading back under `REBUILD_READ_SHARE` (a fifth) of that. Its cause, in order: a compaction
  between it and the previous call, a model change, a gap of an hour or more, of five minutes or more,
  else unknown. On 57 real sessions: 31 rebuilds, 9.24M tokens, a third of all cache writes; 20 after
  more than an hour. A few came 8 to 98 seconds after the previous call with no recorded reason; those
  say so rather than guess.
- `waste.summary` (via `insights._waste`) prices each rebuild at `cache_create - cache_read` for its
  model, says when a model has no price, and attaches the same agent's prompt (`run.waits`) that
  overlapped the idle stretch most. Result sizes are characters / 4, labelled "about".
- Unused plugin and MCP overhead was dropped from the design: tool definitions are deferred now and
  their size is not in the transcript.
- UI: `insWaste` (metrics, rebuilds with cause and wait, biggest results, re-read note) after
  "Waiting on you"; `wasteRow` in the agent panel; `wasteCause` words a cause.

### Your prompts (`turns.py`)
- `prompt_of` decides what you sent: a `user` entry that is not `isMeta`, not a sidechain, holds no
  `tool_result`, and does not start with an injected marker (`<local-command…>`, `<system-reminder>`,
  `<task-notification>`, bash input and output, the "Caveat:" preamble). A `<command-name>` entry becomes
  the slash command as typed. Real prompts also carry `promptId`, `promptSource` (typed, sdk,
  suggestion_accepted, queued) and `origin`; only `promptSource` is kept.
- `TurnLog` splits the main transcript at each prompt and keeps, per turn: its API usage (once per
  `message.id`, staying with the turn where the message first appeared), tool-call spans, successful
  edits (normalized paths), the latest entry time, the `turn_duration` record time, and whether a
  `local_command` system entry marked it. Entries before the first prompt are not attributed.
- `turns.summary` (via `insights.compute`, key `prompts`): a turn's window runs to the next prompt; its
  agents are those that started in the window; its end is the latest of its own entries, its record and
  its agents' ends, clipped to the next prompt, or now while the last prompt is still running. The split
  is interval arithmetic: waits (all agents) clipped to the window, then agent spans and main tool spans
  minus the waits, then the rest. Cost prices the main usage plus the agents' `tokens_by_model`. Files
  are the main edits plus the agents' non-scratch changed files. A turn with nothing in it and either a
  `local_command` mark or no live work is a local command and is skipped; a session of only those has no
  card (this was a crash on real data before release). On 40 real sessions: 284 prompts, every split
  adding up to its duration.
- Considered and dropped: Claude Code's `cost-state` record (cumulative cost and API, retry and tool
  time). It restarts on resume and its scope differs from the transcripts (1.0M tokens against 78M in a
  32-agent session), so it is not shown.
- UI: its own view, `prompts` (tab after Insights, key 4, `#view=prompts`; not an agent view, so it
  shows for a session without agents). `renderPrompts` / `promptsHtml`: metrics, a legend, then one
  `<details>` per prompt. The summary row has a three-part bar (`split-claude`, `split-work`,
  `split-you`, widths inline as elsewhere) whose words are in its `aria-label`; the body has the whole
  prompt (up to 500 characters), the time in words, the agents (`data-agent`, opening the drawer), file
  names (up to 20, `fileLabel`) and commits (up to 5). `state.openPrompts` keeps open rows open across
  polls. Rows stack on phones. Static reports keep the tab (the data is in the summary).

### Context pressure (`pressure.py`)
- Reads what `WasteLog` already keeps: each API call's `input_tokens`, `cache_read_input_tokens` and
  `cache_creation_input_tokens` (once per `message.id`; their sum is the context the model saw) and each
  `compact_boundary` with its `compactMetadata` (trigger `manual`/`auto`, `preTokens`, `postTokens`,
  `durationMs`). A boundary is kept once per `uuid` (Claude Code can write the same one twice); values
  that are not non-negative integers are dropped.
- `series` is the calls plus, at each compaction, its `preTokens` (a `/compact` after a quiet spell can
  come long after the last call). `peak_of` takes the highest point; "now" is the latest call, never a
  compaction point. `summary` (via `insights.compute`, key `pressure`): the main session's peak, fill and
  curve (thinned to 240 points, each bucket keeping its highest), its compactions (a missing `postTokens`
  falls back to the next call's context), agents ranked by peak (top 10, plus a count), and `near`: while
  the session is live, the main session or a running, waiting or stalled agent whose latest call is past
  `NEAR` (0.8) of its window.
- Windows are not in the transcript. `limit_for` matches `ORCHESTRA_CONTEXT_LIMITS` (case-insensitive
  model-name parts) before the defaults: 200k for Haiku, else 1M. On real sessions main contexts reached
  885k (Sonnet) and 726k (Opus) between compactions, agents 235k, Haiku agents 90k; all 5 compactions were
  manual. A fill past 1 means the assumed window is wrong, and the card says so.
- Each agent's light dict has `context_peak` (tokens, limit, fill, now, now fill, model, compactions).
- UI: `insPressure` after "Where tokens were wasted": metrics, notes (near, agent compactions, past the
  window), `insPressureChart` (area of the curve, dashed 80% line only when it is inside the y range and
  then explained in a caption, a labelled marker per compaction with labels closer than 60px skipped),
  the compactions list (`compactionCause`), and agents through `insRank` (amber past 80%).
  `pressureRow` is the agent panel's "context" line. `renderHealth` adds a `context` item per `near`
  entry; the main session's opens Insights at the card (`openCard`, offset by the top bar only where
  it is sticky), and the headline names the main session separately from agents.

### What went wrong (`errors.py`)
- `ErrorLog` (one per transcript: the main session's, and each agent's digest) keeps:
  - **API errors**: entries with `isApiErrorMessage` (model `<synthetic>`), their `error` type, `apiErrorStatus`
    and first line (`_api_text` turns Anthropic's JSON error body into "API Error: 529 Overloaded"). Each is
    kept once per `uuid`; the same type again before any reply adds to `repeats` instead of a new row.
    `resumed_at` is the next assistant entry with a real model.
  - **calls**: `[at, tool, shown target, ok, error line, match key]` per `tool_use`, `ok` set from its
    `tool_result` (`is_error`). The error line drops `<tool_use_error>` tags and terminal colours and joins a
    bare "Exit code N" to the line after it. The match key is a hash of the whole command for commands and of
    the target for everything else, so two heredoc scripts are not one call but two edits of one file are.
  - **timeouts**: results whose `toolUseResult.timedOutAfterMs` is set, with whether a `backgroundTaskId`
    says Claude Code moved the command to the background (current versions) rather than stopping it.
- `retries()`: from each failed call, the next call with the same tool and key within `RETRY_GAP` (3)
  other calls, repeated until one works. `stuck()`: the latest chain that is still failing, has
  `STUCK_AFTER` (3) failures in a row, ended within the last `STUCK_RECENT` (10) calls, and was not tried
  again later. On 202 real transcripts: 49 chains, 47 worked, none stuck; most had one call (the fix)
  between attempts.
- `errors.summary` (via `insights._errors`, key `errors`): API rows with `lost_s` (to the reply, to now
  while live, else none) and per-kind and total lost time as the union of stall spans per transcript;
  failed calls by tool (with the most common error) and by agent; retries (most attempts first);
  timeouts; and `stuck` for the live main session and running, waiting or stalled agents. Each agent's
  light dict has `errors` (`ErrorLog.totals`).
- UI: `insErrors` after "Did they check their work?" (metrics, a "Stuck now" note, API errors, tried
  again, failed calls by tool and by agent, past the timeout; rows reuse `.check-list`, with `.check-ok`
  for a retry that worked). `errorsRow` is the agent panel's "errors" line. `renderHealth` adds a
  `retrying` item for a stuck agent, or "failing every time" on its POSSIBLE LOOP line, and one for a
  stuck main session that opens the card (`openCard("what-went-wrong")`).
- Dropped from the design: background commands "left running". 54 commands went to the background
  and only 9 were followed by an output or stop call, but Claude Code reports completion in task
  notifications that do not reliably tie back, so it could not be told apart from forgotten.

### Insights at a glance (front end only)
- Every card from `insCard` has `data-card`, its title as a slug (`cardKey`). `renderInsights` sets
  `state.cardsFold` while it draws, so only Insights cards fold; the same helper elsewhere (the Prompts
  tab) draws a plain `<h3>`. In Insights the title is `<h3><button class="card-fold" data-fold=… aria-expanded>`.
- `insHeadlines(ins, run)` makes one `{key, text, tone}` per card with something to say, from the same
  data the cards use: tone `bad` (failing check, stuck agent, over budget, a context near its window),
  `warn` (unchecked, an API stall, cache rebuilds, time waiting on you while a wait is open, agents
  without the project instructions, a file with two writers, a budget past its warning), else `info`.
  Sorted by tone, then card order. Cards with nothing to report (Tool use, Longest-running agents) get
  none.
- `insGlance` draws the strip (`nav.glance`): a button per headline with `data-jump`, and
  `data-fold-all`. A folded card is drawn as its title button plus `card-headline` (no body). The
  folded keys are `state.folded`, read once from `localStorage["cuelight-folded"]` (a JSON array of
  strings; anything else is ignored) and written on every change, inside try/catch so a blocked store
  still folds for the visit. `toggleFold` redraws and puts focus back on the redrawn toggle;
  `foldAll(fold)` folds every `[data-card]` or clears the set; `openCard(key)` unfolds the card before
  switching to Insights and scrolling to it, so chips and Health box items always land on an open card.

### Agents tab, and reports handed back (`status.fill_handbacks`, `model._gist`, `extract.py`)
- **Reports handed back.** Since Claude Code 2.1.277 a subagent ends by calling `SubagentHandback({message})`;
  the parent's tool result or `<task-notification>` `<result>` then holds only a pointer ("This agent's report
  was delivered to you as a message from "…" (its SubagentHandback call)…"). `AgentDigest.handbacks` keeps
  `(when, message)` for each call, and `AgentDigest.report` is the latest one, else the last text block.
  `fill_handbacks(rounds, handbacks)` replaces each round's pointer (`is_handback_pointer`: the pointer shape,
  under 400 characters, so a real report that mentions the tool is kept) with the latest report handed back
  by that round's end plus 5 s of clock skew. Not "inside the round": on real sessions the call comes 8-14 s
  before the parent hears of it, and the same stop is sometimes notified twice 1 ms apart, which makes a
  round that starts after its report. Every later consumer (the result, handoff edges, exports) sees the
  real report.
- **Deliverables.** `extract_expected_output` also recognizes, from a census of real briefs: headings that
  start "When you are done", "Report back", "Final message" and the like (`_OUTPUT_HEADING_STARTS`), and
  lines that start "Final message", "Write your (full) report", "Reply with", "Report back", "When you are
  done", which may sit behind a list marker. A bare "Return"/"Output" step behind a list marker is still an
  instruction, not a deliverable.
- **Payload.** The light agent dict adds `expected_output` and `result_gist`, both scrubbed, then capped at
  `LIGHT_TEXT_CAP`. `_gist` drops markdown marks (headings, bullets, emphasis, backticks), skips rules
  and table separators, and joins lines with " · ", stopping once past the cap.
- **Front end.** `agentFlags(agent)` gives `{text, tone}` reasons a row needs a look (status failed /
  orphaned / stalled / waiting; checks failing, or unchecked once finished; completed with no report; a
  possible loop, else stuck retrying). `agentsRows(run)` applies the filter bar and "only the ones that need
  a look" (`state.agentsFlagged`), then sorts by `state.agentsSort` (`{key, dir}`; `AGENT_SORTS` holds each
  column's first-click direction; ties keep launch order). `agentSortValue` ranks status worst first (more
  flags first within a status) and checks failing-unchecked-checked-none. `agentSpend` is money when the
  run is priced, else fresh tokens. `renderAgents` redraws on every poll and puts keyboard focus back on
  the row (`data-agent`), heading (`data-sort`) or toggle it was on (`noteFocus`/`restoreFocus`, section 6).

### Spend tab (`spend.py`, `tabs.js`)
- **Calls.** `WasteLog.calls` (one row per API message id, latest usage winning) now also keeps
  output tokens: `[at, model, cache_create, cache_read, input, output]`. `spend.summary` prices each
  call by its model (`PriceTable.price_for`; an unpriced model adds nothing and is listed), or without
  a table counts fresh tokens (input + output + cache writes), as the rest of the dashboard does. On
  51 real sessions the curve's total equals `cost.total` to float rounding.
- **Series.** 120 bins from the first call to now (live) or the last call (ended): running totals for
  the main session, the agents, and the top four models plus "other". The budget (`ORCHESTRA_BUDGET`,
  money only) records the call that passed `warn_ratio × limit` and the one that passed the limit.
  `rate_now` is the last five minutes per minute (live only); `peak` is the costliest five-minute
  window starting at a call, with its top three spenders.
- **Front end.** `spendSeries` stacks main then agents, or models by name ("other" last) so a model
  keeps its colour while rankings change. Colours are `--series-1..5`: the reference categorical
  palette (validated light and dark with the dataviz validator; light needs visible labels, so the
  legend carries values and a table lists every series). `spendChart` draws bands (a 14% wash) with
  2px lines, the budget line, the warning/limit crossings (names on opposite sides when close),
  clock ticks, and direct end labels where a band is tall enough. The crosshair (`spendReadout`)
  follows the pointer or the arrow keys (Home/End, PageUp/PageDown); a live redraw puts the reading
  and focus back. The Insights Spend card moved here; its glance chip carries `view: "spend"`.

### List prices (`listprices.py`, `pricing.py`)
- **Precedence.** `pricing.default_source()` is a `PriceSource` whose fallback is `listprices.table()`.
  `PriceSource.get()` returns, in order: nothing when `ORCHESTRA_PRICES=off` (tokens only, no list
  prices); the user's file when it exists and parses; no table and an `error` string when it exists
  but is broken (a broken file is reported, never quietly replaced by list prices); else the list
  prices. `PriceTable` now carries `source` (`"yours"` or `"list"`) and `as_of`; the cost block in
  the payload carries both, so a client can label a figure.
- **The table.** `listprices.MODELS` (first match wins, so a model's own row comes before its family's)
  holds Anthropic's public list prices per million tokens, checked on `AS_OF` (2026-10-12) against
  `SOURCE`, never from memory. Two choices keep it from understating: cache writes are priced at the
  one-hour rate (2x input), because most Claude Code cache writes are one-hour writes (72% in the
  transcripts it was checked against), and Claude Haiku 5.5 at its over-100K-prompt rate, five times its
  short-prompt rate. Models without a row (Opus 4.5 and earlier, Sonnet 4.5 and earlier) stay unpriced
  and are listed as such. `CHEAPER` maps a family to the cheaper model the advice suggests (Opus to
  Sonnet, Sonnet to Haiku) and the model id it is priced as.
- **Labels.** In the dashboard, `priceNote` in `app.js` gives "list price, as of <date>", used by the Spend
  tab and the Advice tab; the header total says " (list price)" with that text in its tooltip, and the Agents
  tab's cost column header says "Cost (list price)" with it in its tooltip. The command line does not use
  `priceNote`: `advice.render_text` prints "Dollar figures use Anthropic list prices as of <date>.",
  `baselines.render_estimate` prints "Costs use Anthropic list prices as of <date>.", and the verdict line
  marks the total " (list price)" with no date. Not labelled yet: the Insights numbers, the agent
  panel's cost row, the Spend by-agent and by-model tables, CSV/JSON exports (the JSON carries
  `cost.source` and `cost.as_of`) and History totals.

### Advice (`advice.py`, `advice.js`)
`advice.compute(run, now, table)` is a pure function of a `Run` (no I/O, bounded output, every
transcript-derived string passed through `scrub`). `RunBuilder.refresh` calls it last and puts the result
on `run.advice`: `{parts, cards, slow, failures, priced_by, as_of}`. Cuelight never applies a fix; a
card's snippet is text to copy. Its own text is ASCII, so the command line prints on any console.

- **A card** is `{id, rule, tag, severity, title, happened, agents, impact, fix}`. `id` is a hash of the
  card's merge key (what the per-browser Hide remembers); `tag` is `sure` (a fact in the record) or
  `possible` (a judgment the record cannot settle); `severity` is `failure`, `fix`, or `info` (it reports
  something and has nothing to change); `impact` is `{seconds, tokens, cost, label}`; `fix` is
  `{where, text, snippet}` with `where` one of settings, agent, claude_md, instructions, habit, none.
- **Failure ladder** (`_failure`: one card per agent that ended `failed`, `orphaned` or `stalled`). The
  first match wins: (1) an API error as the agent's last entry, or within 2 minutes of its end, and not
  recovered from (usage limit, expired login, server error, overloaded); (2) its latest call failed
  `STUCK_AFTER` (3) times in a row; (3) its last command ran past its timeout and was not moved to the
  background; (4) it ran out of context: `sure` when a compaction is recorded, `possible` when only the
  assumed window's fill reached `CONTEXT_FULL` (95%); (5) a possible loop (`detect_loop`); (6) it was
  cut off when the session ended or was still in a tool call, or, for a stalled agent with a call open,
  "went quiet during a tool call"; (7) "No cause in the record", with its last three calls. Cards with
  the same cause and fix in several agents (the API error kind, context, loop, session ended) merge.
- **Rules** (each `fn(run, now, table) -> [card]`, listed in `RULES`):

  | Rule | Fires when | Tag |
  |---|---|---|
  | `verify` | A **completed** agent's checks are `unchecked` after code edits, or `failing`. A failing card is keyed by its own command; the command in a snippet is its first line only, never a cut-off one | sure |
  | `worktree` | One file was written by agents that **overlapped in time** (`_overlapped`: an agent still running runs until now; one with no known start counts as overlapping). Writers that took turns are a handoff, so no card | sure |
  | `compact` | Cache rebuilds after an idle gap (`idle_long`, `idle_short`), main session and agents, with the extra cost over the cache-read price | sure |
  | `rules_missing` | An agent lacks instruction files the main session had; the text mentions `omitClaudeMd` | sure |
  | `preapprove` | Permission waits grouped by the command prefix or web domain they were about (below) | sure |
  | `cheaper_model` | A completed agent on Opus or Sonnet made 3 or more calls, all read/search tools, and the same tokens at the cheaper model's prices save at least $0.05 and 30% | possible |
  | `parallel` | Agents of one parent ran one after another: each launched within `PARALLEL_GAP_S` (120 s) of the previous one ending, with no prompt between, not in one launch batch, and no edge or shared file between them. All groups merge into one card; the saving is the sum of durations minus the longest | possible |
  | `over_usual` | A completed agent over its usual 90th percentile **and** over 2x its typical value (`OVER_RATIO`): time first, else cost, else fresh tokens (used when the agent or its usual has no cost). Needs a usual with enough runs | sure |

- **Pre-approve** (`_preapprove_cards`). A wait is matched to the tool call open when it began (the latest
  call at or before its start). Questions Claude asks on purpose (`AskUserQuestion`, `ExitPlanMode`) never
  count. A rule is suggested only for a Bash command whose **whole** text is known (the error log keeps a
  hash of the full value; `_is_whole`) that starts with a prefix in `SAFE_PREFIXES` (npm, pnpm and yarn
  test, lint and build scripts, pytest, `python -m pytest` and `unittest`, go test/build/vet, cargo
  test/check/build/clippy, make test/check/lint, ruff check, mypy, tsc, eslint) and contains none of
  `; & | > <`, a backtick, `$(` or a line break; and for a WebFetch URL that passes a strict host check
  (`_host`: no backslash or `@`, a dotted name whose last label is alphabetic, no IP literal, no
  `localhost`, under `TARGET_CUT` (160) characters and on one line, since a longer URL was cut by the log).
  The card says how far the rule reaches (`SCOPE`: "allows any arguments, and runs code from your
  project", or "any page on that domain"). Anything else gets a report-only `info` card with no snippet.
  `git status`, `diff` and `log` are not on the list: Claude Code never asks about them.
- **Thresholds, merge, order** (`_select`). Over-usual cards that no raw card can explain merge by kind
  (`_group_unexplained`); then cards with the same `key` merge (agents joined, impacts added, "and N more
  like it"). A card is kept when its impact is at least `MIN_SECONDS` (60), `MIN_TOKENS` (10,000) or
  `MIN_COST` ($0.05), or when it is a risk that always shows (`always`: failure, verify, worktree and
  rules_missing cards, and a pre-approve card with `PROMPTS_ENOUGH` (3) or more prompts). Order is severity
  (failure, then fix, then info), then cost, seconds and tokens; at most `MAX_CARDS` (12). Only then does
  `_explain_over_usual` write each over-usual card's text, so it names only cards that are shown and can
  explain it (a time overage: a pre-approve card, time spent waiting on you; a cost or tokens overage: a
  rebuilt-cache or pre-approve card), or says that no other card explains it.
- **Verdict** (`_verdict_parts`): agents (and how many failed or were cut off), commits and PRs, tests
  passed or "N of M test runs failed", the cost with " (list price)" when it is one, and "N things to
  fix". That count is the failure cards plus the cards with a real fix (`fix.where != "none"`), so an
  information card is not counted. `advice.js` counts the tab badge the same way (minus hidden cards) and
  makes it red when a failure card is shown.
- **Slow** (`_slow`): running or waiting agents past their kind's slow case and over 2x typical, for the
  Health box's SLOWER THAN USUAL. Not shown while replaying.
- **`--advise` text** (`render_text`): the verdict, a note when dollars are list prices, each card (title,
  tag, what happened, what it cost, where the fix goes, the snippet) and `fact_sheet` (at most 60 lines:
  wall time, one line per agent, top tools, conflicts, links, commits and PRs).
- **The tab** (`advice.js`, loaded after `tabs.js`). `renderAdvice` draws the verdict with a Copy button,
  the cards (escaped; agent chips open the agent panel; Copy puts the snippet on the clipboard and says
  "Copied!" or "Copy failed") and "Your usual". Hide keeps card ids in `localStorage`
  (`cuelight-advice-hidden`, in try/catch, capped at 200); a button under the cards shows or re-hides them.
  A replay shows a note instead, since advice describes the whole session. Keyboard focus survives a
  redraw (`noteFocus`/`restoreFocus`, with `data-copy` and `data-hide` in `FOCUS_NAMES`).

### Usual numbers (`baselines.py`, `/api/usual`)
- **Scan.** `Baselines.scan` walks every project under Claude Code's projects folder, read-only, and
  builds each past session with the same `RunBuilder`, keeping a row per finished agent (`completed`,
  `failed` or `orphaned`). `Baselines.start` runs it in a daemon thread when the dashboard starts, so the
  page opens at once (about 7 s for 57 sessions and 305 MB the first time; then cached); `start_if_stale`
  rescans every 10 minutes, after an answer has been sent. `--advise` and `--estimate` scan in the
  foreground. Errors are swallowed: a session that fails to read is skipped and tried again next scan, and a
  failing scan keeps the numbers already read and reports `state: "error"`.
- **A row is numbers only:** session id, a 12-character hash of the project folder name, agent type, model
  family, status, start, duration (`None` for an agent cut off before it ended, which still counts toward
  the fail rate), tokens by model and kind (so cost is priced when read and a price change needs no
  rescan), tool-call count, edited, check state, loop, seconds waited. No prompts, briefs, results, paths or
  commands (`test_rows_are_numbers_only_for_finished_agents` pins the shape).
- **Cache.** `<state dir>/baselines-v2.json` (`VERSION = 2`, mode `0600`, a temp file then `os.replace`).
  One entry per session, with a fingerprint (name, size and mtime of the main transcript and every
  `agent-*` file). A session whose fingerprint matches is reused; a changed one is read again; one that has
  disappeared drops out. A session that still had an unfinished agent (running, waiting or stalled) when
  it was read is stored with no fingerprint, so the next scan reads it again: whether an agent is running
  or cut off depends on the clock, and a dead session's files never change. A corrupt, old or wrongly
  shaped cache is ignored. The version went to 2 when cut-off agents became rows.
- **Usual** for an agent type and model family (`usual_for`): this project's finished runs when there are
  at least `MIN_RUNS` (5) and one of them timed, else all projects' runs when there are, else
  `enough: false` ("not enough past runs yet (n)"). Typical is the median and slow case the 90th
  percentile of time, fresh tokens and cost (from the runs that ended; a run on an unpriced model is left
  out of the cost figures, not counted as free). `fail_rate` is the share of failed and cut-off runs;
  `unchecked_rate` is the share of runs that edited code and were `unchecked` (carried in the payload, not
  shown on screen). The current session is left out. `RunBuilder` asks once per type and model and sets
  `agent.usual`.
- **Where it shows.** The agent panel's "usual" row (`usualText`), the Health box, the Advice tab's "Your
  usual" table (not in a static report, which has no other sessions to learn from), `render_estimate`
  (`--estimate`), and `GET /api/usual` (`service.usual`, behind the same Host, Origin and token checks as
  every route): `{state, error, sessions, agents, groups, source, as_of, currency}` for this session's
  project. The Advice tab asks for it when opened, and again every 3 seconds while `state` is `scanning`;
  an answer for a session that is no longer the current one is dropped.

### Graph and Work Floor
- **Graph layout** (`layoutGraph`) is a pure function split into `graphRankColumns`
  (longest path over exact edges; columns indexed, never raw rank), `graphOrderColumns`
  (alternating down/up barycentre sweeps on per-column normalised positions, best
  crossing count kept), `graphPlaceRows` (exact isotonic regression by pooling adjacent
  violators: each node level with its neighbours, no overlap, order kept) and
  `graphCriticalPath`. The orchestrator's spawn edge to an agent is hidden whenever that
  agent has another incoming exact edge. Pan/zoom is a transform on one viewport group.
- **Edge routing** (`graphRoute`, `graphPath`, 0.21.0). An edge between neighbouring columns is
  the same single curve as before. A longer one keeps its single curve unless a sample of
  that curve lands inside a node it skips; then it crosses each skipped column level, through
  the free lane (above, between or below that column's nodes, `GRAPH_LANE_PAD` clear) nearest
  the straight line between its ends, and bends only in the gaps between columns, where there
  are no nodes. An edge to an earlier column (an inferred handoff can point back) leaves its
  source's left side and enters its target's right side, routed the same way. `graphPath`
  draws any list of points as level-in, level-out cubic steps. `tests/test_graph_layout`
  samples every drawn segment of the demo graph and fails if one passes through a node other
  than its own two (before this change, 8 of the demo's 27 edges did, 16 crossings in all).
- **Work Floor** cards show `last_tool` and the `activity` bins from the light payload.
  The floor records a signature of everything it draws and skips the rebuild when it is
  unchanged, so running sprites are not restarted every poll.

### Front-end design system
One stylesheet of tokens (`style.css`): a monochrome interface where **status is the
only chroma**, designed for light and dark together (`data-theme` or the system
setting, applied before first paint). Colour is never the only signal. The
`tests/test_contrast.py` suite reads the real tokens and enforces WCAG 4.5:1 for
text and 3:1 for marks in both themes. The report page shell is derived from
`index.html` at import (`report._build_shell`), with every substitution asserted, so
there is one shell. Deep links are `#view=<tab>&agent=<id>`.

### Security notes for the live layer
Hook text and spool contents are untrusted: `Event.from_dict` validates strictly,
event text reaches the page via `textContent`, and `esc()` escapes quotes so a
value cannot end an HTML attribute. The stream/export/history routes sit behind
the same Host/Origin/token checks as every other route. Dashboard approve/deny is
deliberately **not** built (see `docs/ROADMAP.md`).

---

## 11. Directory map

```
orchestra/
  __main__.py       CLI: start / stop / report / --serve
  locate.py         find sessions on disk
  transcript.py     incremental JSONL reading
  parent.py         parse the parent transcript's launches/results/notifications
  agentlog.py       digest a subagent's own transcript
  status.py         rounds + the status state machine
  extract.py        objective / expected-output extraction from a brief
  edges.py          spawn / artifact / message / handoff edge inference
  redact.py         secret scrubbing
  model.py          Agent / Run / Edge / Batch / HubFile / WriteConflict dataclasses
  build.py          RunBuilder: assembles one Run from a session's files
  service.py        the API's business logic (no sockets)
  http.py           the loopback HTTP server
  report.py         self-contained static HTML snapshot
  constants.py      every tunable threshold (ORCHESTRA_* env vars), in one place
  statedir.py       the per-user, 0700 runtime directory
  events.py         agent-neutral Event schema + the on-disk spool
  hook.py           the async Claude Code hook entrypoint
  livestate.py      ground-truth session/agent state from events + transcripts
  insights.py       run-level analytics: parallelism, critical path, tools, tokens, files
  search.py         agents / tool calls / files search over the scrubbed run
  demo.py           the synthetic 13-agent run behind --demo
  pricing.py        price table (your file, else the list prices) -> cost
  listprices.py     Anthropic's public list prices, dated
  advice.py         fix-it cards, failure causes, verdict, the --advise text
  baselines.py      what each kind of agent usually takes, from past sessions
  runaway.py        possible-loop detection
  export.py         CSV / JSON export
  history.py        opt-in run-metrics history (sqlite)
  static/
    index.html      page shell (live server)
    app.js          the frontend: polling, rendering, interaction
    tabs.js         the Prompts, Agents and Spend tabs
    advice.js       the Advice tab
    style.css       theming (light/dark), layout, all visual language
    agent-sprite.png  Work Floor's character sheet (MIT-licensed, from
                      ntd4996/agentpet — see §7's "Work Floor" entry)
commands/
  open.md           the /cuelight:open slash command definition
hooks/
  hooks.json        the async live-event hooks the plugin ships
.claude-plugin/
  plugin.json       plugin manifest
  marketplace.json  marketplace manifest
tests/              one test file per backend module, plus static-asset
                    and rendered-report checks
docs/
  ARCHITECTURE.md   this document
  superpowers/plans/2026-09-12-orchestra.md   the original 16-task build plan
```
