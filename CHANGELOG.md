# Changelog

All notable changes to Cuelight (called Workflow before 0.4.0). Format: [Keep a Changelog](https://keepachangelog.com/);
versions follow [SemVer](https://semver.org/) (the `version` in
`.claude-plugin/plugin.json` pins installed users until it changes).

## [Unreleased]

## [0.22.0] - 2026-10-13

M1: from a dashboard to an advisor. Cuelight now says what to change, not only what happened, and what an
agent usually costs. All of it is worked out on your machine by rules, with no AI call and no network.

### Added
- **Fix-it cards and a verdict** (new **Advice** tab, key `A`, with a count on the tab that is red when an
  agent failed). The verdict is one copyable line (`13 agents, 1 failed · 2 commits, 1 PR · 2 of 11 test
  runs failed · $7.32 · 5 things to fix`). Each card says what happened (with the agents it concerns), what
  it cost in minutes, tokens and dollars, and one change to make, with the text to copy. A card is tagged
  **sure** (a fact in the record) or **possible** (a judgment). **Hide** puts a card away in this browser.
  Cuelight never applies a fix.
  - **Why an agent failed**, from the record: a usage limit, an expired login, a server error or an
    overloaded API; a call that failed three times in a row; a command past its timeout; a context that
    filled up; a loop; cut off when the session ended; or, when the record shows nothing, "No cause in the
    record" with the last three calls.
  - **Rules:** work nobody checked (or checks that failed); two agents writing one file at the same time
    (`isolation: worktree`); a prompt cache rebuilt after a break; an agent that ran without your project
    rules; a command you kept approving, as a `permissions.allow` entry (only test, build and lint commands
    and web domains, and the card says how far the rule reaches); a cheaper model that would likely have
    done; agents that could have run together; an agent far over what its kind usually takes. Cards need a
    minute, 10,000 tokens or five cents to show, except risks (failures, unchecked work, shared files,
    missing rules), and at most 12 show, failures first.
- **What an agent usually costs.** From your past sessions (read-only, in the background, about 7 seconds
  for 57 sessions and 305 MB the first time), Cuelight learns for each agent type and model how long it
  usually takes, how many tokens and how much money, and how often it fails. It shows in an agent's panel
  (a "usual" row), in the Health box (**SLOWER THAN USUAL** for a running agent far past its slow case), in
  a "Your usual" table on the Advice tab, and in `GET /api/usual`. It keeps numbers only, in
  `baselines-v2.json` in the state directory (never under `~/.claude`).
- **`/cuelight:open advise`** (`cuelight --advise`) prints the verdict, the cards and a short fact sheet that
  Claude can answer follow-up questions from; **`/cuelight:open estimate`** (`cuelight --estimate`) prints the
  usual numbers before you launch more agents. The command file tells Claude to show them and not to change
  any file unless you ask.
- **Dollars out of the box.** Anthropic's public list prices are built in (`orchestra/listprices.py`, dated
  2026-10-12) and used when you have no price file of your own. Figures priced from them say "list price, as of
  2026-10-12" in the header, the Spend tab, the Agents tab, the Advice tab and the two commands.
  `ORCHESTRA_PRICES=off` shows tokens only.

### Changed
- Your own price file still wins, and a price file that cannot be read is reported, not replaced by list
  prices. Cache writes are priced at the one-hour rate and Claude Haiku 5.5 at its over-100K-token rate, so
  list-priced figures lean high, never low; Opus 4.5 and earlier and Sonnet 4.5 and earlier stay unpriced.
- Without a price file the dashboard now shows list-priced dollars instead of tokens only. Tests that assume
  "no price file means tokens only" set `ORCHESTRA_PRICES=off`.
- The error log keeps a hash of a whole URL as it did of a whole command, so a URL with a line break is not
  mistaken for the line it starts with.
- The static report includes the verdict and the cards (not the "Your usual" table, which comes from other
  sessions).
- README: "What problem it solves", an Advice section, two more example use cases, the list prices and what
  the past-runs scan reads and writes; PRIVACY.md lists the new cache; ARCHITECTURE.md, SUBMISSION.md,
  CONTRIBUTING.md and the roadmap describe the new files. The design's "As built" section lists where the
  built version differs from the design.

### Known limits
- The "list price" label is not yet on the Insights numbers, the agent panel's cost row, the Spend by-agent and
  by-model tables, History totals or the CSV/JSON exports (the JSON says where its prices came from).
- The skipped-tests rate of each kind of agent is worked out and in the payload, but not shown on screen.
- A subagent that works for more than ten minutes while its session's own transcript stays quiet is read by the
  existing status rules as failed or cut off (unchanged from 0.21), and then gets a failure card.

## [0.21.3] - 2026-10-09

An audit after the plugin directory's review of 0.21.0. No new features.

### Fixed
- **`0` on the Graph fits it and stays there.** The graph fits itself on `0`, and the same key press then
  went on to the page's view shortcuts, which since the tenth tab send `0` to History. A key a focused
  control has already used now never also runs a shortcut.
- **The demo no longer looks like it reads a credential.** Its made-up security review searched the code
  for environment variables, which the directory listed under "Uses a credential from the user's machine".
  It now searches for something else. Cuelight itself reads no credential; a new test keeps every runtime
  file free of anything that does.

### Changed
- Documents brought up to date: the README (the hooks' start-up cost as measured on Windows too, keyboard
  behaviour, how to check the plugin before pushing), PRIVACY.md (the dashboard's error log and exports are
  now listed), docs/SUBMISSION.md (every finding the portal lists, what it is here and what to do about it),
  CONTRIBUTING.md (what the directory's scanner flags), and the workflow, roadmap and follow-up notes.
- Checked, with nothing to fix: every real session on the maintainer's machine (59 sessions, 144 agents)
  through every API call and the static report; every tab and agent panel of the largest one in a real
  browser; the hook with empty, malformed, hostile and 3 MB input (always exit 0); both strict plugin
  validators.

## [0.21.2] - 2026-10-09

### Fixed
- **No credential-shaped text in any shipped file (again).** The plugin directory held 0.21.0 for
  "Secret in a shipped file": a loop-detection test (`tests/test_runaway.py`) had a fake
  `Authorization: Bearer …` header written out in a curl command. It is now built at run time from
  `tests/fake_secrets.py` like every other fake credential, and an old planning document's fake Bearer
  token and password are now obvious placeholders. The check that keeps such text out of the repository
  (`test_directory_readiness`) now also looks for Bearer and Basic values, password and AWS secret
  assignments, quoted keys and tokens, and passwords inside URLs, so this fails the tests first next time.

## [0.21.1] - 2026-10-09

### Fixed
- **Keyboard focus survives a live refresh in every view that redraws.** A live session redraws the
  open tab every few seconds, and that used to throw a keyboard user out of the Prompts tab, the
  Insights fold buttons and chips (Fold all too), the Spend split toggle and agent links, the Work Floor
  cards, the Fleet rows and the History rows. Focus now stays on the same control, so Tab carries on
  from where you were. The Agents tab and the Spend chart already did this; all of them now share one
  way of doing it. If the control is gone after the refresh, focus is left alone.

## [0.21.0] - 2026-10-09

### Changed
- **Graph edges go around the agents they skip.** An edge between neighbouring columns is drawn as
  before. A longer one used to cut straight through any agent in the columns between. Now, when its
  curve would pass through one, it crosses each of those columns through the nearest free gap and bends
  only between columns. In the demo, 8 of 27 edges passed through agents (16 times); now none does.
- An edge back to an earlier column (an inferred handoff can point back) now leaves its source's left
  side and enters its target's right side. It used to loop back across the whole graph from the
  source's right side.

## [0.20.0] - 2026-10-09

### Added
- **Activity search**: a search box on the Activity tab that looks through every tool call the run made,
  not only the live tail of running agents, so it works on finished sessions and static reports too.
  - It matches the tool, the file or command (scrubbed, as everywhere) and the agent. "Failed only"
    lists the calls whose result was an error, with or without words.
  - Repeats of one call by one agent are one row with how often and how many failed. Matched text is
    marked, the newest come first, and the count says how many calls matched (at most 300 rows are
    shown, and it says so).
  - Enter steps to the next match (Shift+Enter back) and Escape clears; a row opens its agent's panel.
  - New endpoint `GET /api/calls?q=…&failed=1` (token required). A static report runs the same rules
    over its baked-in details, and a test checks both give the same rows on a whole run.
- When nothing is running, the Activity tab says how to look back through the run instead of only
  "no live activity".

### Changed
- On a phone, an Activity row wraps: time, agent and tool on the first line, the whole command under it.

### Not included
- Result text: Cuelight does not keep each call's output (only whether it failed), so it cannot be
  searched.

## [0.19.0] - 2026-10-09

### Added
- **Spend**: a tab of its own (key 5; Prompts and the views after it move up one number, History is
  key 0). It shows what the session cost as it ran, as a running total from every API call:
  - the main session and its agents stacked, or each model (top four, then "other");
  - your budget as a line, with where its warning (`ORCHESTRA_BUDGET_WARN_RATIO`, 80%) and its limit
    were passed;
  - a crosshair that reads every series at a moment, by pointer or by arrow keys on the focused
    chart. A legend with values and a table carry every number without it.

  It leads with the total, the main session's and the agents' shares, the average pace and the pace over
  the last five minutes. With a budget set, it shows how much of it is used, and either how far over it
  is or when it runs out at the current pace. Below the chart are the most expensive five minutes (and
  who spent them) and the most expensive agents. Without a price file it counts fresh tokens, and says
  where to put prices. On 51 real sessions the curve ends exactly at the cost the header shows.

### Changed
- The Insights Spend card moved to the Spend tab; its chip in the Insights strip opens the tab.
- The views that are tabs of their own (Prompts, Agents, Spend) moved from `app.js` to a second script,
  `static/tabs.js`. `app.js` had reached the plugin directory's 256 KiB limit for a file; a static
  report still inlines everything.
- Each API call's record keeps its output tokens too, so a call can be priced on its own.

## [0.18.0] - 2026-10-09

### Added
- **Agents**: a tab of its own (key 3; Insights and the views after it move up one number), with one
  row per agent. Each row shows what the agent was asked, what its brief said to deliver, and the start
  of what it reported back, side by side. It also shows the agent's status, whether it checked its work,
  its time (counting up while it runs), its cost (or fresh tokens without prices) and its failed calls.
  - Rows that need a look say why: failed or orphaned, stalled, waiting on you, checks failing,
    unchecked, finished with no report, a possible loop, or stuck retrying. "Only the ones that need a
    look" shows just those.
  - The tab leads with how many agents there are, how many need a look, and how many briefs said what
    to deliver.
  - Status, Checked, Time, Cost and Errors sort (worst or biggest first), and the Agent column sorts by
    launch order. The filter bar applies, and a row opens the agent's panel (Enter on a focused row too).
  - A live refresh keeps keyboard focus on the row or heading it was on.
  - The poll payload carries each agent's expected output and the first line or two of its report, both
    capped at 200 characters; the panel still has the whole text.
- The demo's briefs each name their own deliverable.

### Fixed
- **An agent's report was missing on Claude Code 2.1.277 and later.** Agents there hand their report back
  through a `SubagentHandback` tool call, and the parent session only gets a pointer ("This agent's report
  was delivered to you as a message from …"). Cuelight showed that pointer as the agent's result,
  everywhere. It now reads the report from the agent's own transcript. A pointer takes the latest report
  handed back by the end of its round (the call comes 8 to 14 seconds before the parent hears of it, and
  the same stop can be notified twice). On local sessions: 88 of 88 such agents had only the pointer,
  and now all 88 have their report. Handoff edges in those sessions went from 0 to 13, because a brief
  that quotes an earlier agent's report is now recognized.
- **Deliverables that real briefs state were read as "not stated".** Briefs often say what to hand back
  as "Final message: …", "Write your full report to …", a "When you are done, report back with" heading,
  or a numbered "Report back …" step. Those are recognized now. On local sessions, briefs with no
  deliverable found went from 41 to 10; the rest only mention it mid-sentence.

## [0.17.0] - 2026-10-08

### Added
- **Insights at a glance**: a strip at the top of Insights with one chip per card that has something
  to say, in a few words ("1 agent stuck retrying, 1 API stall (2m 20s)", "58.6k tokens rewritten to the
  cache", "context peaked at 19%"). Worst first: red for something wrong (a failing check, a stuck agent,
  over budget, a context near its window), amber for worth a look (unchecked work, cache rebuilds, time
  waiting on you, agents without your instructions, a file with two writers), grey for plain facts; then
  card order. Each chip jumps to its card. Read from data the cards already have.
- **Cards fold**: an Insights card's title is a toggle; a folded card keeps its title and its chip's line.
  "Fold all" / "Unfold all" sits at the end of the strip. Folding is remembered in this browser (and lasts
  for the visit where storage is blocked); jumping to a card from a chip or the Health box opens it.
  Everything starts unfolded.

### Changed
- Cards carry `data-card` (their title as a slug) and `openCard` takes that key, so a Health box item
  opens the right card even when it was folded.

## [0.16.0] - 2026-10-08

### Added
- **What went wrong**: an Insights card after "Did they check their work?". It shows:
  - each API error (`isApiErrorMessage`, with its `error` type and `apiErrorStatus`): usage or rate limit,
    failed login, server error, with how long until the next real reply (or that the session stopped there,
    or that it is still waiting). The same error again before any reply is one stall with a count, and
    overlapping stalls count their time once. Anthropic's JSON error body reads as words
    ("API Error: 529 Overloaded");
  - tool calls whose result was an error, by tool (with the most common error's first line; a bare
    "Exit code 1" gets the next line, terminal colours removed) and by agent;
  - **tried again**: the same call within three calls of a failure, until it worked or the tries stopped. A
    command matches only in full, so two different heredoc scripts are not one call; a file tool matches by
    file;
  - commands that ran past their timeout (`timedOutAfterMs`), and whether Claude Code moved them to the
    background.

  While a session is live, a running, waiting or stalled agent (or the main session) that failed its latest
  call three times in a row and has not tried it since goes in the Health box as RETRYING; an agent already
  flagged as a possible loop gets "failing every time" on that line instead. The main session's item opens
  this card. The agent panel gains an "errors" line. Real transcripts: 16 API stalls (13 session limits,
  about 18 hours in all), 333 of 8,915 calls failed, 49 retries of which 47 worked, 16 commands moved to
  the background.
- The demo has an overloaded API while the design agent works, a security review stuck on a failing
  `npm audit`, and an end-to-end run moved to the background after 2 minutes.

### Changed
- `openCard(name)` replaces `openPressure()`: a Health box item for the main session opens its card.

## [0.15.0] - 2026-10-08

### Added
- **How full each context got**: an Insights card after "Where tokens were wasted". It shows:
  - the main session's context on every API call (input plus cache read plus cache write, once per API
    message), with a mark at each compaction and the size Claude Code recorded just before it;
  - each compaction: its size before and after, whether you ran `/compact` or Claude Code did, and how
    long it took (the same boundary written twice counts once; when the size after is not recorded, the
    next call's context stands in);
  - agents ranked by their fullest call, with how often each was compacted.

  While a session is live, the main session or a running agent past 80% of its window goes in the Health
  box ("CONTEXT 86% FULL"); the main session's item opens this card. The agent panel gains a "context"
  line. Transcripts do not record a model's window, so it is assumed: 1M, and 200k for Haiku, set with
  `ORCHESTRA_CONTEXT_LIMITS` (`haiku=200000,opus=1000000`). A context past its assumed window says the
  window is too small. On real sessions main contexts reached 885k and agents 235k, and every compaction
  was a manual `/compact`.
- The demo's orchestrator context now grows with each wave (24k to 186k) before its compaction to 61k,
  and its live ticks continue from there.

## [0.14.0] - 2026-10-08

### Added
- **Prompts**: a tab of its own (key 4; Activity and the views after it move up one number), with one row
  per message you sent. Each row shows:
  - when, and what you asked (slash commands as typed);
  - how long the work it started took, counting up while it runs;
  - a bar splitting that time into waiting on you, agents and tools running, and Claude itself, each
    moment counted once in that order;
  - the agents it launched, files edited, commits, and its cost (or fresh tokens without prices).

  A row opens to the whole prompt, the time in words, the agents it launched (each opens its panel), the
  files it edited and its commits; open rows stay open as the page refreshes. The tab leads with the number of prompts, the typical time per prompt, and the most expensive prompt's
  share of the session. Read from the transcript: prompts are `user` entries that are not tool results,
  reminders or agent notifications; Claude Code's `turn_duration` records close turns, and local commands
  such as `/plugin` (marked `local_command`) start none. The main session's usage is counted once per API
  message; agents, commits and waits are matched by time.

  Claude Code's own `cost-state` record was considered for a cost cross-check and a retry-time split, and
  left out: it restarts when a session is resumed, and in a real 32-agent session it held 1.0M tokens
  against 78M in the transcripts.
- The demo has three prompts (plan, build, ship) with turn records.

### Fixed
- Found on a real session before release: a session whose prompts were all local commands made the new
  card's summary fail (an empty list), which would have emptied that session's Insights tab.

## [0.13.0] - 2026-10-08

### Added
- **Where tokens were wasted**: an Insights card and a line in each agent's panel.
  - **Cache rebuilds.** Each time the prompt cache was written again from scratch, for the main session
    and every agent: how many tokens, the likely cause (idle over five minutes, idle over an hour, the
    model changed, after a compaction, or not recorded), the permission or input prompt you were
    answering meanwhile, and what it cost above the cache-read price when prices are set. A rebuild is a
    call that writes at least 10,000 tokens to the cache while reading back under a fifth of that; usage
    is counted once per API message.
  - **Biggest results.** The five largest tool results pulled into context (estimated at four characters
    a token), with how many later calls carried them.
  - **Re-reads.** Reads of a file whose content came back identical to the previous read of the same
    range.

  Read from the transcripts, so no new hook, and it works on past sessions. On 57 real sessions it found
  31 rebuilds, 9.2M tokens: a third of everything written to the cache, most after more than an hour
  idle. Measuring unused plugin or MCP overhead was considered and left out: Claude Code now loads tool
  definitions on demand, and transcripts do not record their size.
- The demo's orchestrator is compacted during the verify wave and rebuilds its cache, and its design
  agent reads one long file.

## [0.12.0] - 2026-10-08

### Added
- **Catch me up**: sessions are named by their title (the one you gave with `/rename`, else the one
  Claude Code generated) in the Fleet view, the session picker, the browser tab, the pill, alerts for
  other sessions and static reports. Each Fleet row's second line is what the session needs from you,
  else Claude Code's own "while you were away" recap with its age (two lines), else the last thing you
  asked, else its status, which then moves to the meta line. The recap of the session on screen sits
  under the header, one line until clicked, and says "older than the latest activity" once the session
  has worked since. Read from the `ai-title`, `custom-title`, `away_summary` and `last-prompt` records
  Claude Code already writes, so it works on past sessions and needs no hook. Lists that cannot build a
  whole run per session read only the end of each transcript for its title (cached).
- The demo's Fleet has four neighbouring sessions showing each kind of row.

### Fixed
- Fleet rows on a phone: the counts line no longer squeezes the title to nothing; it wraps under it.
- CI: the launcher line-ending test read the checkout, which Git for Windows writes with CRLF; it reads
  the stored file now (main was red on the Windows jobs after 0.11.0).

## [0.11.0] - 2026-10-08

### Changed
- `/cuelight:open` runs a `cuelight` command. The plugin now ships `bin/cuelight`, a short shell script that
  starts the dashboard with the first Python 3.9+ it finds, and Claude Code puts a plugin's `bin/` on the
  Bash tool's PATH. When Claude Code asks before running it, "don't ask again" now offers `cuelight *`,
  which covers Cuelight only, instead of `python *`, which covered every Python command. The rule also
  outlasts plugin updates, since the command no longer names the versioned plugin folder. On a Claude Code
  without `bin/` support, the command falls back to running `orchestra/__main__.py` with Python.

## [0.10.3] - 2026-10-08

### Removed
- `docs/assets/pill-states.png`, a still of the pill's states that nothing used any more (the README shows
  the animated pill). The plugin directory held 0.10.2 for review on this one file, under "Files or
  downloads the validator couldn't inspect".

## [0.10.2] - 2026-10-04

### Fixed
- `/cuelight:open` asked for permission every time, with no "don't ask again" option. Its commands passed
  `$CLAUDE_CODE_SESSION_ID` and `$PWD`, and Claude Code will not let you allow a command for good when it
  cannot check a variable in it. The commands now have no variables: Cuelight reads the session id from
  its environment, as it already could, and when there is none it looks in the directory it was started
  in (before, only in `--cwd`). Allowing it once now lasts until the next plugin update.

## [0.10.1] - 2026-10-04

### Changed
- `/cuelight:open` no longer pre-approves shell access. Its command file declared `allowed-tools: Bash`,
  which allowed any shell command while it ran, and the plugin directory holds a plugin for that. Each
  action is now one command that runs `orchestra/__main__.py` by its full path, with no `cd`, so Claude
  Code asks once and you can allow that command for good.
- The README shows the pill with Markdown image syntax, as the directory asks, instead of an HTML
  `<picture>` element. It shows the light version in both themes.

## [0.10.0] - 2026-10-03

### Added
- **What each agent was told**: the instruction files (CLAUDE.md, rules, memory) and skills each agent
  had, and which agents ran without the project instructions the main session had. An Insights card
  shows how many agents loaded your project instructions, names the ones that did not (with their agent
  type, since some types may be meant to run without them), and lists every instruction file with its
  type, size and how many agents loaded it; the agent panel lists that agent's files. Read from the
  `instructions`, `nested_memory` and `skill_listing` records Claude Code writes in every transcript,
  subagents' included, so it needs no new hook and works on past sessions. Only paths, types and sizes
  are kept, never the files' content. Worktree copies of a file count as the same file.
- The demo's read-only audit agent runs without the project instructions; the payment agent picks up a
  nested CLAUDE.md when it touches `src/payments`.

### Fixed
- Found while checking this release in a real browser: the main session's instruction files were
  cleared on every refresh after the first (a misplaced line in the builder), so a live dashboard lost
  them within seconds. Fixed before release, with a test that refreshes after the transcript grows.

## [0.9.0] - 2026-10-03

### Added
- **What the run produced**: commits, pushes, pull requests, branch merges and test runs, from the main
  session and every agent. An Insights card shows the counts, how many test, build and lint runs passed,
  the whole run's cost (or fresh tokens) per commit and per pull request, the pull requests as links
  (https only, opened in a new tab without a referrer) and the latest commits with their first message
  line, branch, author agent and time. The agent panel says what that agent produced. Read from the git
  results Claude Code records on a Bash call (`toolUseResult.gitOperation`) and its `pr-link` entries.
  A `git commit -q` prints no commit id, so Claude Code records none: it is still counted, without a sha.
  A forked agent replays its parent's history, so commits and pull requests are counted once.
- The demo's orchestrator commits, pushes and opens a pull request; one agent commits its own work.

## [0.8.0] - 2026-10-03

### Added
- **Review the changes**: every agent's file changes as real diffs. The agent panel has a Changes
  section: one folded entry per file with lines added and removed, "new" for created files, and the
  diff with old and new line numbers and the time of each edit. An Insights card "What changed" totals
  files, lines added and removed and new files, ranks agents by lines changed, and lists the most
  changed files (worktree copies of a file count as one). Diffs come from the patch Claude Code records
  for each successful edit (`toolUseResult.structuredPatch`, or a new file's content), so nothing is
  re-diffed; transcripts without it fall back to the edit's own old and new text. Failed edits are not
  counted. Bounded at 1,500 diff lines per agent (counts stay complete) and redacted as one text per
  hunk, so a key spread over several lines is caught. Files in scratch or temp folders are shown but not
  counted. Static reports carry the diffs too.
- The demo records realistic patches for its new files and edits.

## [0.7.0] - 2026-10-03

### Added
- **Did they check their work?** For every agent that edited code, whether it ran a test, build,
  type check or lint (or ran the very file it edited) after its last edit, and whether that check
  passed. Finished agents that are unchecked or whose last check failed are listed in the Health box;
  an Insights card counts checked, failing and unchecked agents and lists the ones worth a look with
  the evidence (last file edited, last check command); the agent panel says it in one line. Docs and
  files in scratch or temp folders do not count as code. Rule-based (`orchestra/verify.py`), no model
  calls. Add your own check commands with `ORCHESTRA_VERIFY_PATTERN` (a regular expression; an
  invalid one is ignored and the card says so).
- Tool calls now record whether their result was an error (`ok` in the agent detail payload).
- The demo has a failing test run, an agent that edited after its last type check, and checked ones.

## [0.6.0] - 2026-10-03

### Added
- **Waiting on you**: how long agents sat on permission and input prompts only you could answer.
  A new Insights card shows your time (overlapping waits counted once), agent time lost (every
  wait added), the number of waits, the longest, a per-agent ranking and the latest prompts with
  their text. A wait still open counts up live, and the agent panel says how long that agent
  waited on you. A wait ends at the agent's next transcript entry, so an approved command's own
  run time is included (transcripts record no separate time for it). A prompt still up when the
  session ended is listed as never answered and given no length. Backed by `livestate.waits`,
  `insights.waits` and per-transcript activity times; no new hook.
- The demo has answered prompts (two of them overlapping) and one agent waiting on you now.
- README section "Example use cases" (Anthropic's directory policy asks for at least three working examples).

### Fixed
- The directory's validation blocked the plugin for "secret in a shipped file": the redaction tests
  held fake keys as text. They are now built from pieces at run time (`tests/fake_secrets.py`), the old
  planning document was cleaned the same way, and a test keeps secret-shaped text out of every file.

## [0.5.0] - 2026-10-03

### Added
- Plugin metadata for Anthropic's directory listing: `displayName`, `homepage`, `repository`, `license`,
  `icon` (new `assets/icon.png`), `documentationUrl`, `supportUrl` and `privacyPolicyUrl`.
- `PRIVACY.md` (what it reads, writes and never sends), `CONTRIBUTING.md`, issue and pull request templates.
- `docs/WORKFLOW.md` (the whole flow on one page) and `docs/SUBMISSION.md` (checklist status and the
  answers for the submission form).
- README section "What Cuelight runs and touches".
- `tests/test_directory_readiness.py`: Anthropic's mechanical pre-submission checks, run on every build.
- **Pulse**: a live strip above every view with charts for agents running, tool calls per
  minute and fresh tokens spent, change arrows against the previous minute, a "needs you"
  tile and a tape of recent starts, finishes, failures and stalls (click one to open the
  agent). Backed by a new `insights.pulse` block and per-message token timestamps. Charts
  update in place, hover shows exact values, it can be collapsed, and it stops moving under
  reduced motion.

### Changed
- The project's own `CLAUDE.md` moved to `.claude/CLAUDE.md` (a `CLAUDE.md` at the plugin root is not
  loaded and draws a validator warning). CI now runs `claude plugin validate --strict`.

## [0.4.1] - 2026-10-03

### Changed
- The always-on-top pill has a new look: a small cue-light mascot whose face and motion show the
  state (asleep when quiet, bobbing with an orbiting ring when agents run, hopping with ripples when
  something needs you, shaking on an error), a count badge when several sessions wait, and a
  segmented bar for the agents. It is drawn in the window itself (no images, no network), follows
  the light/dark setting and stops moving when reduced motion is requested. The window is now built
  once and updated in place, so the animation no longer restarts on every poll.
- The browser tab icon now shows the same cue-light face for the current state, with the count of
  waiting sessions in a red badge, so the state is visible while the dashboard tab is in the background.

### Fixed
- The marketplace entry still showed the old description; the README install line now names
  the marketplace (`/plugin install cuelight@cuelight-marketplace`).

## [0.4.0] - 2026-10-03

### Changed
- **Renamed from Workflow to Cuelight.** A cue light is the lamp that tells a performer
  "now", which is what the dashboard is for. What changes for you:
  - The command is `/cuelight:open` (was `/workflow:open`), and the plugin is `cuelight`:
    `/plugin marketplace add chandan12ar/cuelight`, then `/plugin install cuelight`.
  - Downloaded exports are named `cuelight-<session>.csv|json` (were `workflow-...`).
  - New config and history live under a `cuelight` directory
    (`~/.config/cuelight/prices.json`, `~/.local/share/cuelight/history.sqlite`; Windows:
    `%APPDATA%\cuelight`, `%LOCALAPPDATA%\cuelight`). A price file or history already
    saved under the old `workflow` directory is still found and used until a `cuelight`
    one exists, so nothing is lost. `ORCHESTRA_PRICES` and `ORCHESTRA_HISTORY_DB` still
    override both.
  - The GitHub repository is now `chandan12ar/cuelight` (the old address redirects).
  - Unchanged: the internal Python package (`orchestra`), every `ORCHESTRA_*` environment
    variable, the API and the on-disk event format.

## [0.3.0] - 2026-10-03

### Added
- Insights tab: parallelism over time, the critical path (the exact-evidence chain
  of agents that set the run's length, overlap counted once), tool use, prompt-cache
  hit rate, fresh-token share by agent and model, contended and widely-read files,
  longest-running agents, and a Spend card with burn rate and a budget forecast.
  Computed server-side (`orchestra/insights.py`) and carried in the summary payload,
  so reports and exports include it.
- Command palette (`Ctrl/Cmd+K` or `/`) with instant agent and command results and
  `GET /api/search` for tool calls and files. The search matches the redacted text
  only (it cannot be used to probe a secret); repeated identical calls collapse into
  one row with a count, so a loop reads as x10. Static reports search the details
  baked into them.
- Keyboard shortcuts (`1`-`7` views, `L`, `R`, `T`, `F`, `?`, `Esc`) with a help
  dialog; they never fire while typing or with a modifier key. Tabs support arrow
  keys.
- Timeline: a comb of ticks on each bar showing when the agent was calling tools,
  an outline on the critical path, durations inside bars and a "now" marker.
- `python -m orchestra --demo`: a scripted 13-agent run served from a throwaway
  directory, with a simulator that keeps the running agents moving.
- Theme: automatic, light or dark, remembered per browser, applied before first
  paint. Deep links now carry the view (`#view=insights&agent=<id>`).
- Empty and loading states, and a visible "can't reach the dashboard" message.

- Graph: a layered layout (ranks by exact edges, up/down ordering sweeps that keep the
  best crossing count, exact vertical alignment with neighbours), pan and zoom (drag,
  Ctrl/Cmd+wheel, buttons, `+`/`-`/`0`), hover focus that fades unrelated agents, a
  duration on every card, and the orchestrator's launch fan drawn only where it is the
  explanation (hover the orchestrator for the rest).
- Work Floor: each card shows what the agent is doing right now (its last tool call)
  and an activity sparkline; group by role or by status, attention first; the floor is
  no longer rebuilt (restarting every sprite) when nothing visible changed.

### Changed
- A new design system: a monochrome interface where status is the only colour, a
  transport-style header (live clock, stat strip, budget meter), alerts as status
  chips, a redesigned agent drawer, and a responsive layout down to phone width.
  Text and status colours are held to WCAG 4.5:1 by `tests/test_contrast.py`.
- The static report's page shell is now derived from `index.html` instead of being
  a hand-kept copy, so the two cannot drift apart.

### Fixed
- `fmtDuration` shows hours and never rounds to "1m 60s"; the main-transcript
  reader drops launches that a truncated file no longer contains; the page declares
  its own icon (no `/favicon.ico` request).

## [0.2.0] - 2026-10-03

### Added
- Live events: async hooks (session, subagent, notification, API-failure, turn
  events) feed ground-truth state: what the session is waiting on (permission /
  input / idle / API error type), exact session end, exact subagent stop, and a
  new `waiting` agent status. `ORCHESTRA_EVENTS=off` disables recording.
- Run history (opt-in, `ORCHESTRA_HISTORY=on`): a History tab to list past runs and
  compare any two (agents, failures, wall time, tokens, cost, loops). Stores
  metrics only - never prompts, results, or file paths - in a per-user 0600 database
  pruned by age and count; off by default.
- Replay: scrub or play back the run to see who had launched, who was running and
  who had finished at any moment - in the live dashboard and in a static report,
  so a post-mortem needs no server. Statuses are reconstructed from start/end
  times; token totals are shown as unavailable rather than wrong.
- Export: `Export` menu (CSV one-row-per-agent, JSON everything), `GET /api/export`,
  and `python -m orchestra --export csv|json [--out PATH]`. CSV cells that a
  spreadsheet would run as a formula are neutralised; output is the same scrubbed
  data the dashboard shows.
- Possible-loop detection: an agent that is still running and has repeated the
  same tool call (or strictly alternated between two) is listed as a *possible*
  loop with the evidence; thresholds configurable. Never a verdict.
- Cost and budget: with a user-supplied price file, the header shows what the run
  cost - the orchestrator's own usage included, priced per model - with a partial
  flag for unpriced models and a budget (`ORCHESTRA_BUDGET`) that warns at 80 %
  and alerts when exceeded. No prices are built in.
- Sounds (off by default, synthesized with Web Audio, no audio files): a distinct
  tone when Claude needs you, when something fails, and when everything finishes
  cleanly - for the open session and for other sessions in the fleet. An *Options*
  panel mutes each event separately and sets quiet hours (overnight spans work).
- Pill: a small always-on-top window (Document Picture-in-Picture, Chromium) showing
  what needs you across all sessions, plus a tab-title count and a favicon badge
  that work in every browser.
- Fleet view: every recently active session across all projects, most urgent
  first, with a badge and notifications for sessions you are not viewing.
- Live push (Server-Sent Events): the dashboard updates when something changes
  instead of every 2 s; polling remains as a 15 s safety net and as the fallback.
- `--cwd` flag: base for relative report paths; fallback session discovery.
- `ORCHESTRA_*` environment variables for every threshold (see README).
- CI (Linux/macOS/Windows x Python 3.9/3.12/3.13), `SECURITY.md`, this file.

### Changed
- Handoff-edge inference is cached across polls and skips provably-failing
  searches: 96 agents went from 5.2 s to 0.22 s cold, 0.024 s warm.
- Session builders are held in a bounded LRU (default 8) instead of forever.

### Fixed
- **Security:** the page's `esc()` helper did not escape quotes, so a value containing
  one could break out of a quoted HTML attribute and inject another. It now does.
- **Token counts were inflated 2.6-3.1x on real transcripts.** Claude Code writes
  one entry per content block, each repeating the message's usage; usage is now
  counted once per API message id (and tracked per model).
- `/workflow:open report` wrote into the plugin directory; it now writes into
  the project.
- A leaked log file handle in `cmd_start`.

### Removed
- Dead code: `IncrementalReader.reset`, `parent.content_text`,
  `SessionInfo.size_bytes`.

## [0.1.0]
- Initial release: timeline, graph, activity, Work Floor, drawer, filters,
  health and write-conflict boxes, deep links, notifications, copy summary,
  static report.
