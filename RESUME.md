# RESUME — read this first to continue the session

Repo: `chandan12ar/cuelight` (public) · default branch **`main`**, which changes only by a merged
PR (the owner clicks Merge on GitHub; `gh pr merge` from here is blocked). The product is
**Cuelight**; the internal package is still `orchestra`.

State (2026-10-13): **`main` is 0.21.3** (PR #38, the audit, merged; CI green) and **the directory is
reviewing it** (it passed the security scan; a person is reading it, as of 2026-10-11). `main` stays
frozen until that is decided, because every merge to `main` becomes the version under review. The owner
paused new features on `main` (2026-10-09): there, work is fixing what exists and keeping directory
findings from coming back.

**M1 (0.22.0) is built on `feature/m1`**, the one exception the owner made (2026-10-12): the Advice tab,
fix-it cards and verdict, why an agent failed, the usual numbers per kind of agent, `/cuelight:open advise`
and `estimate`, and dated list prices as the default. All ten tasks are done and pushed, and the docs and
the version (`plugin.json`, `CHANGELOG.md`) say 0.22.0. The final whole-branch review and its fix wave are
done (head `d0edfcf`, 1363 tests, CI green). **Next: five small fixes listed in `PROGRESS.md` "M1" ("Five
fixes left before the PR"). The pull request to `main` waits until the 0.21.3 directory review is decided
and the owner says so; do not open it before.** The last GitHub Release is **v0.17.0**; one for 0.21.x or 0.22.0 needs the owner's yes.
Details: `PROGRESS.md` (status by round), `.claude/CLAUDE.md` (rules + lessons), `CHANGELOG.md`
(per version), `docs/ARCHITECTURE.md` (how it works), `docs/superpowers/specs/2026-10-12-m1-advice-design.md`
(M1's design; section 16 says where the built version differs), `docs/FOLLOW-UPS.md` (older review items).

## 1. What the product is
A Claude Code plugin (`/cuelight:open`) serving a local dashboard of a session's subagent
orchestration. Python 3.9+ stdlib only, vanilla JS, no build step, no network egress,
loopback + token. Direction agreed with the owner: **observability + control plane for Claude
Code multi-agent work** = the record (transcripts) + the live truth (hooks), across all sessions.
Tabs: Timeline, Graph, Agents, Insights, Advice (M1, on `feature/m1`), Spend, Prompts, Activity, Work Floor,
Fleet, History.

## 2. Owner's rules (follow exactly)
1. One feature = one branch, one PR, one minor version (bump `plugin.json` + a CHANGELOG
   heading; installed copies only update when the version changes).
2. After each feature: full test suite -> commit -> push -> **read the PR's CI and say it is
   green before asking the owner to merge**.
3. If something fails: note it in the commit message or `PROGRESS.md`, push, and stop.
4. If the owner says "stop": commit + push everything, update the docs with where we stopped.
5. New feature work: show a short design first and wait for the go-ahead. Never force-push.
6. Out of scope by owner decision: dashboard approve/deny, Gemini/Cursor adapters.
7. Every merge to `main` becomes the version the Claude plugin directory reviews (it
   re-scans about every 6 hours), so keep `main` clean.

## 3. What is built
| Versions | Delivered |
|---|---|
| 0.2-0.5 | Live layer (async hooks -> spool -> SSE), Fleet, pill + sounds, cost/budget, loops, replay, export, history; Insights, Graph, Work Floor, palette, `--demo`; rename to Cuelight; Pulse strip; directory-readiness docs |
| 0.6-0.10 | Waiting on you; Did they check their work?; Review the changes (diffs); What the run produced (commits/PRs/tests); What each agent was told |
| 0.10.1-0.11 | Directory review fixes (no `allowed-tools`, removed an uninspectable image); `bin/cuelight` launcher so "don't ask again" covers Cuelight only |
| 0.12-0.16 | Catch me up (titles, recaps); Where tokens were wasted; Prompts tab; Context pressure; What went wrong (API stalls, failed calls, retries, timeouts) |
| 0.17 | Insights at a glance: chip strip (worst first) + foldable cards |
| 0.18-0.21 (merged) | Agents tab (+ SubagentHandback reports, deliverable phrasings); Spend tab (running cost, budget crossings; tab views moved to `static/tabs.js` for the 256 KiB limit); Activity search (`/api/calls`); graph edge routing |
| 0.21.1-0.21.3 | Keyboard focus kept across live refreshes; the directory block (fake Bearer token in a test) and wider secret guard; audit: graph `0` key, demo text, credential-read guard, docs refreshed |
| 0.22.0 (`feature/m1`, not on `main`) | M1, advice: fix-it cards and a verdict (`orchestra/advice.py`), why an agent failed, the Advice tab (`static/advice.js`, key `A`), usual numbers per agent type and model from past sessions (`orchestra/baselines.py`, cache `baselines-v2.json`, `/api/usual`), `/cuelight:open advise` and `estimate`, dated list prices as the default (`orchestra/listprices.py`, `ORCHESTRA_PRICES=off`) |

## 4. Still open
- **Owner's direction (2026-10-09): no new features on `main` for now.** Fix what exists, keep the docs
  current, and make sure directory findings do not come back. Every finding and what it means is in
  `docs/SUBMISSION.md` ("Findings in the portal"); `CONTRIBUTING.md` says what the scanner flags. M1 is
  the one exception (2026-10-12), and it lives only on `feature/m1`.
- **Next, in order:**
  1. **Five fixes on `feature/m1` before its PR** (the final whole-branch review is done; these came from
     its fix wave's re-review): resumed agents' false "same file" card and the docs that overclaim it; the
     "not its tool calls" wording for long commands; one known limit back in the CHANGELOG; the generic
     badge's dark-mode contrast; a check of prompts raised together. Details and suggested fixes:
     `PROGRESS.md` "M1". Decisions taken during the build: the git-ignored ledger
     `.superpowers/sdd/2026-10-12-m1-advice/progress.md` (if it is gone, `git log feature/m1` holds the work).
  2. Wait for the directory's decision on 0.21.3 (the owner reads the portal). If it asks for a change,
     fix it on `main` by its own small PR, then bring it into `feature/m1`.
  3. When the owner says so, open the pull request from `feature/m1` to `main` (one PR, 0.22.0), read its
     CI, say it is green, and ask the owner to merge. Then "Check for new commits" in the portal. No new
     portal finding is expected (`docs/SUBMISSION.md`, "What M1 (0.22.0) adds").
  4. GitHub Release v0.22.0 (notes from the CHANGELOG), owner's yes first.
  5. Parked until the owner lifts the pause: **name the pending call on a permission prompt**
     (real-data basis in `PROGRESS.md` "Resumed"; show a design first) and agent teams (needs a
     real team run; none on this machine as of 2026-10-09).
  - **M2 candidates (not started; each needs a design first):** patterns across runs ("unchecked in 7 of
    10 runs"); run-level baselines; an `advise` that applies a fix after the user approves it (that belongs
    in the separate opt-in approvals plugin with an audit log, not in this read-only one).
  - Done 2026-10-09: keyboard focus across live refreshes (#36, 0.21.1); the directory block (#37,
    0.21.2); the audit (#38, 0.21.3). Done 2026-10-13: M1 built on `feature/m1` (0.22.0).
- **Owner's calls:** the releases above; trademark search for "Cuelight"; checking the directory
  review result for 0.21.3 in the portal (every merge to main is re-scanned); when M1's PR is opened.
- **Unverified:** agent teams (teammates): no handling, and no team run in the local
  transcripts to test against; `bin/cuelight` launcher's permission prompt in a live Claude
  Code; the API-error hook fields `error_type`/`error_message` (docs only).
- **Known gaps:** the permission notification often carries no tool name (parked, item 3 above); history
  records only sessions that were viewed or scanned. (`agent_type` is empty on `agent_stop`, but
  nothing reads it: agent types come from the transcripts.)
- **Ideas not started:** Fleet ticker.
- **File size:** the plugin directory takes no non-image file over 256 KiB. `app.js` is 241,383 bytes
  (about 236 KiB) in a CRLF checkout at 0.22.0; put new tab-sized views in `static/tabs.js`, or in a file of
  their own loaded after it like `static/advice.js` (`test_directory_readiness` fails first if a file grows
  past the limit).

## 5. Practical notes
- Tests: `python -m unittest discover -s tests -t .` (Node 22+ needed for the UI tests).
- UI check: `python -m orchestra --demo --no-open --port 8766 --token demo`, then
  `node docs/evidence/capture.mjs` (uses its own headless Chrome/Edge profile; `ONLY=a,b` limits
  the shots). Check against a REAL session too: `python -m orchestra --session <id>`.
- Stop servers by port or PID, never by image name (it would close the owner's Chrome).
- Reloading the page in a browser check: a navigate that changes only the `#...` part does not reload,
  so old code keeps running. Change the query string (`&reload=1`). The server itself reads static
  files on every request.
- The directory scans tests and docs too: never write credential-shaped text, even fake (use
  `tests/fake_secrets.py`, or `<fake-...>` in docs), and never the literal names of credential
  sources in the demo. The guard tests in `test_directory_readiness` fail first.
- Windows: always `encoding="utf-8"`; set mtimes explicitly in tests; files check out CRLF
  (tests about stored content read `git show :path`); use Edit, not shell rewrites, for
  any text with backslashes; no `$` inside double-quoted commit messages.
- Real data beats fixtures: check every new number against a real transcript in
  `~/.claude/projects/...` (read-only; never write under `~/.claude`).
