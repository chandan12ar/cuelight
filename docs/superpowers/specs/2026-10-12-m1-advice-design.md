# M1: from dashboard to advisor — fix-it cards and "usual" cost per agent

**Date:** 2026-10-12
**Status:** Design approved in chat (three parts); this written spec awaits the owner's review
**Branch:** `feature/m1` (from `main` 4aa8b89, 0.21.3). Ships as **0.22.0**, one PR to `main`,
opened only after the directory review of 0.21.3 is decided and the owner says so.

---

## 1. Problem

People evaluating Cuelight ask: *is this only Claude's logs in a nice dashboard? Does it recommend
anything when an agent fails? Can it tell me what a subagent will cost before I launch it?*

The honest answer for 0.21.3: Cuelight answers "what happened, and do I need to step in?" well and
with evidence (attention, the run graph, loops, unverified work, write conflicts, missing rules,
cache waste, errors). It never answers **"so what should I change?"** and it knows nothing about
**what an agent usually costs**. Meanwhile "sessions, cost and subagent trees in a dashboard" is a
crowded category (claude-view, Agents Observe, ccboard, clorch, Claude Code's OpenTelemetry export),
and Claude Code's own `/insights` already gives prompting advice. The open ground that fits what
Cuelight has built: **advice about multi-agent runs specifically** — evidence-backed, worked out
locally by rules with no AI call, with what each problem cost in minutes and dollars, and a fix to copy.

## 2. Decisions made with the owner (2026-10-12)

| Question | Decision |
|---|---|
| What M1 delivers | Fix-it cards + a one-line run verdict + "usual" cost/time per kind of agent + the README "What problem it solves" section. Cross-run patterns and anything else wait for M2 |
| Where past-run data comes from | Scan the transcripts Claude Code already keeps (read-only), cache **numbers only** in Cuelight's temp state dir |
| Where it shows | Dashboard (new Advice tab, "vs usual" on agents, Health box) **and** Claude Code (`/cuelight:open advise`, `/cuelight:open estimate`) |
| Prices | Ship Anthropic's public list prices with the date checked, used only when the user has no price file; labelled "list price, as of <date>" |
| How advice is made | Approach C: deterministic rules in Cuelight; `advise` also prints a short fact sheet so the user can ask Claude follow-ups. Cuelight never applies a fix |
| Rules | All eight below, "possible" ones included, tagged as such |
| Shipping | `feature/m1`; README section in the first commit; one version (0.22.0) for all of M1 |

## 3. Goals

- When an agent fails, say **why** from what the record shows, and what to do.
- Turn each existing detection into a **card**: what happened, what it cost, the exact change, where it goes.
- Know what each kind of agent **usually** takes, costs and how often it fails, from the user's own runs.
- Put the advice and the usual numbers **where the decision is made**: in the dashboard and in Claude Code.
- Show dollars out of the box.

## 4. Non-goals (M1)

- Cuelight calling any AI model, or making any network request. (Claude may be asked follow-ups in the
  user's own session; that is Claude Code's call, made by the user.)
- Cuelight writing any config file (`settings.json`, agent definitions, `CLAUDE.md`) or applying a fix.
- Hooks that block, delay or inject anything. Observer hooks stay async and always exit 0.
- Dashboard approve/deny; other-agent adapters (owner decision, unchanged).
- Patterns across runs ("unverified in 7 of 10 runs"), run-level baselines, a `/cuelight:open advise`
  that edits files. M2 candidates.

## 5. Invariants that must not regress

Everything in `.claude/CLAUDE.md` "Invariants" holds: loopback + Host/Origin + token; no network egress
(`tests/test_static_assets.py`); never write under `~/.claude`; state dir `0700`, files `0600`; all output
through `redact.py`; observer hooks never block. Plus the directory lessons: no credential-shaped text in
any file (`TestNoSecretShapedText`), no credential reads in runtime code (`TestNoCredentialReads`).

## 6. Architecture

```
past transcripts (~/.claude/projects, read-only)
   └─ orchestra/baselines.py   background scan reusing RunBuilder; numbers only, cached per session
                               in the state dir; only new/changed sessions re-read
                               → Usual per (agent type, model family)

this session ── RunBuilder → Run
   └─ orchestra/advice.py      pure function (Run, Usual, price table, now) → verdict + cards

prices: user's file (ORCHESTRA_PRICES / per-user config)  else  orchestra/listprices.py (dated)
```

### New and changed files

| File | Role |
|---|---|
| `orchestra/listprices.py` | Built-in list prices: `AS_OF` date, `SOURCE` URL, a table in the existing `pricing.py` format |
| `orchestra/pricing.py` | Falls back to the list prices when no user file exists; `ORCHESTRA_PRICES=off` turns cost off; the table says which it is (`"yours"` / `"list"`) |
| `orchestra/baselines.py` | Scan, cache, aggregate; `Usual` lookup with fallback |
| `orchestra/advice.py` | Failure-cause ladder, the rules, thresholds, merging, ordering, verdict, fact sheet text |
| `orchestra/service.py`, `orchestra/build.py` | Payload gets an `advice` block; agents get a `usual` comparison |
| `orchestra/__main__.py` | `--advise`, `--estimate` |
| `commands/open.md` | `advise`, `estimate` actions; argument hint |
| `orchestra/static/advice.js` (new, loaded like `tabs.js`) | Renders the Advice tab |
| `orchestra/static/index.html`, `app.js`, `style.css` | Tab button, `A` key, badge, agent-panel line, Health item, styles |
| `orchestra/report.py` | Report includes verdict + cards, not the usual table |

## 7. Prices

- `listprices.py` holds Anthropic's **public list prices**, looked up at implementation time from
  Anthropic's pricing page (never from memory), with `AS_OF` (the date checked) and `SOURCE`.
  Patterns cover the current and recent Opus, Sonnet and Haiku generations seen in real transcripts.
- Precedence: the user's price file wins; else the list prices; `ORCHESTRA_PRICES=off` gives tokens only.
- Wherever a dollar figure comes from the list prices, the UI and CLI say "list price, as of <date>".
- Side effect to handle: cost now shows by default in Spend, Insights, the pill and exports. Tests that
  assume "no price file means tokens only" set `ORCHESTRA_PRICES=off`. Budgets are user-set; unchanged.

## 8. The advice engine (`advice.py`)

Pure, bounded, no I/O. Every transcript-derived string is scrubbed, like the rest of the payload.

### 8.1 Card

```
{ "id":       stable hash of rule + subject (agent ids / file / command), for per-browser "Hide"
  "rule":     "failure" | "preapprove" | "cheaper_model" | "verify" | "worktree" | "compact"
              | "rules_missing" | "parallel" | "over_usual"
  "tag":      "sure" | "possible"
  "severity": "failure" | "fix" | "info"
  "title":    plain sentence
  "happened": plain sentence(s) with the evidence
  "agents":   [agent ids]      (links to the agent panel)
  "impact":   { "seconds", "tokens", "cost", "currency", "priced_by": "yours" | "list" | null }
  "fix":      { "where": "settings" | "agent" | "claude_md" | "instructions" | "habit" | "none",
                "text": what to do, "snippet": text to copy or "" } }
```

### 8.2 Thresholds, merging, order

- A non-failure card appears only if its impact is at least **60 s, 10,000 tokens or $0.05**.
- The same rule and fix across several agents is **one card** listing them, impacts added.
- Order: failure cards first; then by cost (when priced), else seconds, else tokens. At most **12** cards.

### 8.3 Failure cards (agents `failed` or `orphaned`; `stalled` uses the same ladder, titled "went quiet")

First match wins:

| # | Cause in the record | Fix |
|---|---|---|
| 1 | An API error is the agent's last entry, or within its last 2 minutes | by kind: usage limit → wait for the reset or resume later; authentication → `/login`; server/overloaded → run it again; other → show the message |
| 2 | Its latest call failed `STUCK_AFTER` (3)+ times in a row | show the call and its error; instruction line: "If <tool> on <target> fails twice, stop and report what you tried" |
| 3 | Its last command ran past its timeout | run it in the background, or raise the Bash timeout (the `settings.json` `env` line) |
| 4 | Its context was compacted during the task, or ended at ≥ 95% of its window | split the task across two agents |
| 5 | A possible loop | add a stop condition to its instructions |
| 6 | It ended mid tool, or was orphaned when the session ended | nothing to fix; resume it |
| 7 | none of the above | "No cause in the record", with its last three tool calls |

### 8.4 Rules

| Rule | Fires when | Card | Tag |
|---|---|---|---|
| `preapprove` | The waits matched to the same command prefix total ≥ 60 s or ≥ 3 prompts. A wait is matched to the tool call open when it began (the latest call at or before its start in that agent) | the `permissions.allow` entry for that prefix. **Only** for an allowlist of read-only, test and build commands (e.g. `npm test`, `npm run lint/build/test`, `pytest`, `python -m pytest/unittest`, `go test`, `cargo test/check/build`, `make test`, `git status/diff/log`) and `WebFetch` domains. Anything else gets a report-only card ("you approved X n times, 9 min") with no snippet | sure |
| `cheaper_model` | A finished agent on an Opus model (→ Sonnet) or a Sonnet model (→ Haiku) made ≥ 3 tool calls, all read/search (`Read`, `Grep`, `Glob`, `LS`, `WebFetch`, `WebSearch`, `NotebookRead`, `TodoWrite`), and the same token mix at the cheaper model's prices saves ≥ $0.05 and ≥ 30% | "cost $2.40 on Opus; the same tokens on Sonnet ≈ $0.48"; snippets: `model: sonnet` for the agent's definition, and the `model` parameter when launching it | possible |
| `verify` | Verification is `unchecked` after code edits, or `failing` | instruction line: "Before you report done, run <the check command seen in this run, else the project's tests> and include the result" | sure |
| `worktree` | A write conflict: two agents wrote the same file | `isolation: worktree` for the agents' definitions | sure |
| `compact` | Cache rebuilds after idle (`idle_long`, `idle_short`) | what the rebuilds cost above the cache-read price; habit: `/compact` before a long break, `/clear` when the old context isn't needed | sure |
| `rules_missing` | An agent lacks instruction files the main session had (`context.coverage`) | name the missing files; "if it must follow them, name them in its definition, or check it ran in the project folder" | sure |
| `parallel` | Two or more agents of the same parent ran strictly one after another within one prompt's window, with **no** edge of any kind between them (exact or inferred) and no file one wrote that another read | minutes saved = sum of durations − the longest; habit: launch them in one message | possible |
| `over_usual` | A finished agent's time is over its usual 90th percentile **and** over 2× its typical; or the same for cost (tokens when unpriced). Needs a usual with ≥ 5 runs | "9 min, 3× usual for general-purpose on sonnet-5 (54 runs)"; points to the other cards on that agent, or says none explains it | sure |

The exact permission-rule syntax, the `isolation` and `model` frontmatter keys, and the Bash timeout
setting names are **checked against Claude Code's current docs during implementation**, and each
snippet has a test pinning its text.

### 8.5 Verdict

One line: `8 agents, 1 failed · 2 commits, 1 PR · tests passed · $3.10 (list price) · 3 things to fix`.
Parts with nothing to say are left out. Copyable.

### 8.6 Fact sheet (`--advise` only)

Plain text, ≤ 60 lines, scrubbed: wall time; per agent (short id, type, model, status, duration, fresh
tokens, cost, tool calls, files written, verification); top 5 tools; write conflicts; edges; outcomes.

## 9. Usual numbers (`baselines.py`)

- **Scan unit:** a session (its main transcript and its `subagents/` files). Every project under
  Claude Code's projects folder, read-only, in a background thread. The current session is excluded.
- **Row per finished agent, numbers only:** session id, project folder name (scrubbed), agent type,
  model family (date suffix removed), status, start time, duration, tokens by model and kind (so cost is
  priced at read time and a price change needs no rescan), tool-call count, edited (bool), verification
  state, loop (bool), waited seconds. No prompts, briefs, results, paths or commands.
- **Cache:** `<state dir>/baselines-v1.json`, `0600`, written atomically (temp file + rename). Keyed by
  session; valid while the fingerprint (size and mtime of the main transcript and every subagent file)
  matches. Sessions that disappear drop out. A corrupt or wrong-version cache is ignored and rebuilt.
- **Usual for (type, model):** this project's rows if ≥ 5; else all projects' if ≥ 5; else "not enough
  past runs yet (n)". Typical = median, slow case = 90th percentile, for duration, fresh tokens and cost;
  fail rate (`failed` + `orphaned`); skipped-tests rate (`unchecked` among agents that edited).
- **Live:** a running agent past its usual slow case **and** over 2× typical goes in the Health box.
- **Never breaks the dashboard:** any scan error is swallowed and surfaced as a note, like history.

## 10. Screens

- **Advice tab** after Insights; shortcut **A** (free today). Number keys 1–9, 0 unchanged. `A` follows the
  existing rules: ignored while typing, with a modifier, with the palette or help open, or when the event
  was already handled (`defaultPrevented`).
  Top: verdict + Copy. Then cards: tag, title, what happened (agent links open the panel), impact chips
  (with "list price, as of <date>" where it applies), the fix with its snippet and a Copy button, and
  **Hide** (per browser, `localStorage`, wrapped in try/catch). Below: "Your usual" table (type, model,
  runs, typical and slow-case time, typical cost, fail rate) and whether it is this project or all.
  Empty states: "Nothing to fix in this run." / "Learning from your past runs…".
- **Tab badge:** card count; red when any failure card exists.
- **Agent panel:** "Usual for <type> on <model>: 3 min, 76k tokens, fails 7% (54 runs). This one: 9 min, 3×."
- **Health box:** "slower than usual" for a running agent.
- **Static report:** verdict and cards; no "Your usual" table (it comes from other sessions).
- Phone width, light and dark, keyboard focus kept across live refreshes (`noteFocus`/`restoreFocus`).

## 11. Claude Code commands

`commands/open.md` gains two actions (argument hint `[stop | report [path] | advise | estimate]`):

- `advise` → `cuelight --advise`: verdict, cards (with snippets), fact sheet. The command file tells Claude
  to show them and **not change any file unless the user asks**.
- `estimate` → `cuelight --estimate`: the usual table for this project and for all projects, with run
  counts and the price label. Runs the scan in the foreground (cached after the first time).

## 12. Testing

- Each rule: fires at its trigger, stays quiet below the threshold, merges duplicates; `preapprove` never
  emits a snippet outside the allowlist; failure-ladder order (one test per step, plus "no cause").
- Baselines: grouping and model-family normalising, the project → all → "not enough" fallback, median and
  90th percentile, cache reuse, refresh on a changed fingerprint, drop on a vanished session, corrupt and
  wrong-version cache ignored, file mode `0600`, current session excluded.
- Prices: list prices only without a user file; `off`; the "as of" label.
- CLI: `--advise` and `--estimate` output; `commands/open.md` dispatch (`tests/test_packaging.py`).
- UI under the node DOM stub: cards render escaped, Copy, Hide, badge, the `A` key and its exceptions,
  focus kept across refreshes, report without the usual table.
- **Real transcripts:** run `--advise` and `--estimate` on this machine's sessions and check the numbers
  by hand (the fixtures-hid-a-bug lesson).
- **Real browser:** Advice tab in light, dark and phone width.
- Unchanged and passing: directory readiness (no credential-shaped text in snippets or tests), no egress,
  encoding hygiene, the full suite on Linux, macOS and Windows (CI runs on every push to `feature/**`).

## 13. Docs

README: "What problem it solves" near the top (first commit), then Advice and Estimate sections, the
list-price note in "Cost and budget", and "What Cuelight runs and touches" (the cache). Also PRIVACY.md
(the baseline cache: numbers only, temp folder, rebuilt after reboot), ARCHITECTURE.md, SUBMISSION.md
(new files; no new finding expected), CHANGELOG (0.22.0), ROADMAP, and RESUME / PROGRESS /
`.claude/CLAUDE.md` (features resume on `feature/m1` only; `main` frozen until the review is decided).

## 14. Build order

Each step: full suite → commit → push → read CI.

1. Spec + README "What problem it solves" (this commit)
2. Implementation plan (writing-plans), reviewed by the owner
3. List prices and the pricing fallback
4. `advice.py` + `--advise` + `advise` action
5. Advice tab, verdict, badge, report
6. `baselines.py` + `--estimate` + `estimate` action + agent-panel line + Health item + `over_usual`
7. Docs, version 0.22.0, real-transcript and real-browser checks

## 15. Risks

- **A wrong card costs trust.** Mitigations: "possible" tags, thresholds, evidence on every card, allowlist
  for pre-approval, honest "no cause in the record".
- **List prices go stale.** Dated label everywhere; the user's file wins; `off` exists.
- **Most agents are `general-purpose`** (real data: 121 of 123), so "usual" is keyed on type *and* model;
  custom agent types sharpen it.
- **Scan cost on big histories.** 57 sessions / 305 MB took 6.8 s here; background + per-session cache.
- **Directory review.** New files are plain Python/JS; no network, no binaries, no hooks change. Snippets
  are checked by the existing credential-shape guard.

## 16. As built (2026-10-13)

Sections 1 to 15 are left as they were written. Review rounds while building changed these details, and
where the two disagree, this section is right.

- **Cache.** The file is `baselines-v2.json`. Version 2 came when agents that were cut off (no end time)
  became rows too: they count toward the fail rate (failed and cut-off runs) but not toward time, tokens or
  cost. A session that still had an unfinished agent when it was read is read again at the next scan
  instead of being reused. A group needs at least 5 runs, one of them timed, to be quoted. The skipped-tests
  rate counts only `unchecked` among runs that edited code, not `failing`. Each row keeps a short hash of
  the project folder name (`baselines.project_key`), not the name that section 9 mentions, so no folder
  name is stored.
- **Prices.** Cache writes are priced at the one-hour rate and Claude Haiku 5.5 at its over-100K rate, so
  list-priced figures lean high. A price file that cannot be read is reported, not replaced by list prices.
  The "list price, as of" label is on the header total, the Spend tab, the Agents tab, the Advice tab and the
  two commands; not yet on the Insights numbers, the agent panel, the Spend by-agent and by-model tables,
  History totals and CSV/JSON exports (the JSON carries where its prices came from).
- **Failure cards.** An API error is the cause only when it was not recovered from. "Ran out of context" is
  `sure` when a compaction is recorded and `possible` when only the assumed window's fill reached 95%; loops
  are `possible`. A stalled agent with a call still open is "went quiet during a tool call". Cards with the
  same cause and fix in several agents merge.
- **Verify** cards are for completed agents only (a failed or cut-off agent has its failure card). A failing
  check is keyed by its own command, and the command in a snippet is its first line, never a cut-off one.
- **Worktree** cards appear only when the writers of a file were running at the same time. Agents that took
  turns on a file are a handoff.
- **Pre-approve.** Questions Claude asks on purpose (`AskUserQuestion`, `ExitPlanMode`) never count. Git
  `status`, `diff` and `log` are not on the list, because Claude Code never asks about them. A Bash rule needs
  the whole command (a single plain line, no `; & | > <`, backtick or `$(`); a WebFetch rule needs a strict
  host check (no backslash or `@`, a dotted name ending in letters, no IP address or `localhost`, a URL under
  160 characters on one line). Every suggested rule says how far it reaches, since it allows any arguments and
  runs project code. A card shows at 60 seconds or at 3 prompts. A command that is not on the allow-list
  (or a Bash command or URL whose whole text is not known) gets a report-only card with no snippet.
- **Could have run together.** Only agents launched within 2 minutes of the previous one ending, with no
  prompt, handoff or shared file between them; all groups share one card.
- **Far over usual.** Time, else cost, else tokens when unpriced, and only for completed agents. A card names
  only the cards that can explain it (a time overage: pre-approve; cost or tokens: cache rebuilt or
  pre-approve) or says none does, and agents that nothing explains share one card per kind. A running agent
  far over usual goes in the Health box as SLOWER THAN USUAL, not in a replay.
- **Order and counts.** Cards are ordered failure, then fix, then information, each by cost, time, tokens;
  at most 12. The verdict's "N things to fix" and the tab badge count only failures and cards with a real
  fix, so an information card is not counted; the badge is red when a failure card is shown.
- **Always shown.** Failure, unchecked-work, same-file and missing-rules cards show whatever their size, since
  their cost is a risk; so does a pre-approve card with 3 or more prompts.
- **Carried but not shown.** The skipped-tests rate is in the payload, not on screen.
- **Smaller shape changes.** A card's `impact` is `{seconds, tokens, cost, label}`; the currency and where
  the prices came from (`priced_by`, `as_of`) are on the advice block, not on each card. `estimate` prints one
  table for the project and all projects together, with a "from" column saying which a row used. The "Your
  usual" table on the Advice tab has a Tokens column as well as time and cost.
