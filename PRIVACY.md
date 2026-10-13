# Privacy policy

Cuelight is a Claude Code plugin that runs **entirely on your computer**. This page says exactly what
it reads, what it writes, and what leaves your machine (nothing).

*Last updated: 2026-10-13, for version 0.22.0 (checked against the code; new: Cuelight now also reads your
past sessions in every project to learn what each kind of agent usually takes, and keeps numbers only,
listed below. Nothing else changed since 0.21.3).*

## Short version

- **Nothing is sent anywhere.** Cuelight makes no outside network connections, has no analytics or
  telemetry, and has no account or sign-in. The author does not receive any data from your use of it.
- **It only watches.** It reads files Claude Code already writes and cannot approve, deny or change anything.
- **Secrets are redacted** before anything is shown in the dashboard.

## What it reads

| What | Where | Why |
|---|---|---|
| Claude Code session transcripts | `~/.claude/projects/...` | To rebuild the run: which agents ran, what each was asked, what each produced, and what the session is called and Claude Code's last recap of it, and what went wrong (the first line of API errors and failed tool results, redacted). Since 0.22.0 also your **past** sessions in every project, read-only and in the background, to work out what each kind of agent usually takes (see the cache below) |
| Hook events written by its own hooks | `<state dir>/events/<session>.jsonl` | To know when a session is waiting for your permission or input |
| An optional price file you supply | the path in `ORCHESTRA_PRICES`, else Cuelight's per-user config folder | To show an estimated cost. Without one, Cuelight uses Anthropic's public list prices, which are built into the plugin (dated, never fetched from the internet); `ORCHESTRA_PRICES=off` turns cost off |

Transcripts can contain anything you and Claude discussed, including personal or confidential text.
Cuelight reads them **locally** and shows them to **you** in a browser tab on the same computer. It does
not copy them anywhere else.

The scan of your past sessions (since 0.22.0) has **no off switch yet**: `ORCHESTRA_EVENTS=off` below turns off
the hooks only. It reads files Claude Code already keeps, read-only, and stores numbers only (the past-runs
cache below). Deleting that cache file costs one rescan.

## What it writes

| What | Where | Kept for |
|---|---|---|
| One line per hook event: the event name, session and agent ids, working directory and a short **redacted** message. Never tool inputs, prompts or file contents | `<state dir>/events/` | 7 days, then deleted |
| The dashboard's port and access token | `<state dir>` (mode `0600`) | Until the dashboard stops |
| The dashboard's error log: Python error tracebacks only; requests and URLs are never logged (normally empty) | `<state dir>/<session>.log` | Until you delete it or the system clears its temp folder |
| The **past-runs cache**: for each finished agent in your past sessions, numbers only: the session id, a short hash of the project folder name (not the name or path), the agent type, the model, its status, when it started, how long it ran, its tokens by kind, its tool-call count, how long it waited on you, and whether it edited code, was checked or looped. Per session it also keeps a fingerprint to tell whether the session changed: the names, sizes and modified times of its transcript files (no content). **Never prompts, briefs, results, file paths or commands** | `<state dir>/baselines-v2.json` (mode `0600`, written atomically) | Until the system clears its temp folder or you delete it (the next scan rebuilds it); a session's numbers drop out when Claude Code deletes that session's transcript |
| An export (CSV or JSON), **only when you ask** (the Export menu or `--export`) | your browser's downloads, or the path you choose | Until you delete it |
| Optional run history (**off by default**; you turn it on with `ORCHESTRA_HISTORY=on`). **Metrics only**: counts, durations, tokens, cost and the project folder, with no prompts, task text, results or file paths | a small local SQLite file in Cuelight's per-user data folder | 90 days (adjustable), then pruned |
| A static HTML report, **only when you ask** (`/cuelight:open report`) | the path you choose | Until you delete it |

`<state dir>` is `ORCHESTRA_STATE_DIR` if set, otherwise a per-user folder (mode `0700`) in the system temp
directory. Cuelight **never writes under `~/.claude`**.

You can turn hook recording off completely by setting `ORCHESTRA_EVENTS=off` in the environment Claude Code
runs in. The dashboard then works from transcripts alone.

## What leaves your computer

**Nothing.** The dashboard server listens on `127.0.0.1` only (not reachable from other devices), requires a
random access token, and checks the `Host` and `Origin` of every request. The page loads no fonts, scripts or
images from the internet, and an automated test fails the build if any external reference is added.

Using Claude Code itself sends your prompts to Anthropic as it always does. That traffic belongs to Claude Code
and is governed by Anthropic's terms and privacy policy; Cuelight does not add to it or change it.

## Children

Cuelight is a developer tool and is not directed at anyone under 18.

## Changes and contact

If this policy changes, the date above changes and the change is listed in [CHANGELOG.md](CHANGELOG.md).
Questions: open an issue at <https://github.com/chandan12ar/cuelight/issues>. To report a security problem
privately, see [SECURITY.md](SECURITY.md).
