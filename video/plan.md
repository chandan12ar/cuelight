# Cuelight video - plan

## Brief
- Purpose: a video Chandan attaches to the reply to Joeri Brons (Agensi.io), so a marketplace
  partner understands Cuelight completely: the problem, why it matters, and every view.
- Length: at least 3 minutes (owner). Script estimate ~5 min, tone `explainer`.
- Voice: Maya alone (Kokoro `af_heart`, offline neural voice; edge is unreachable from this sandbox), finish `warm`.
- Look: framed window over the blurred app (`frame: window`, `background: blur`, `cards: auto`).
- Highlights (owner's words): start with the main problem it solves and how it helps; then narrate
  the dashboard and every feature. Plus the roadmap: Claude Code today, GitHub Copilot CLI next,
  then VS Code and Codex (owner's statement of direction).
- Data: the built-in `--demo` run (synthetic). Badged "Demo data" on screen.

## Hook
"You start five Claude Code agents, switch windows, and ten minutes later find one was waiting on
your permission the whole time." (README use case 1.)

## Flow
Problem cards -> live run: header, permission bar, attention box, clashing files, Pulse, Pill ->
Ctrl+K search (product working) -> Timeline -> Graph -> Agents (+ agent panel, needs-a-look filter)
-> Insights (chips, critical path, produced, checks, what went wrong/waste) -> Spend -> Prompts ->
Activity search -> Work Floor -> Fleet -> Replay/summary/export, theme -> privacy card -> roadmap
card -> install.

## Claims and sources
| Claim | Source |
|---|---|
| Reads transcripts Claude Code already writes | README "It reads the transcripts..." |
| Local, read-only, 127.0.0.1, token, no egress, never writes ~/.claude, redaction | .claude/CLAUDE.md invariants, PRIVACY.md |
| Python only, no build | README Install ("Nothing else - no pip install, no npm, no build") |
| Waiting-for-permission bar, attention box reasons, clashing files | demo app, README use cases 1, 4, 5 |
| Pulse series are real series | README "What you get - Pulse" |
| Pill sleeps / hops | README top section |
| Ctrl+K search, Activity search | app (palette, #calls-q) |
| Timeline/critical path, Graph solid/dashed edges + evidence | app text on those views |
| Agents: asked / expected / came back, panel, needs-a-look filter | app (Agents tab text, drawer) |
| Insights cards (critical path, produced, checks, what went wrong, waste) | app Insights tab |
| Spend: stacked, budget line, most expensive five minutes/agents | app Spend tab |
| Prompts: per-message duration and cost share | app Prompts tab |
| Fleet: all sessions, recap | app Fleet tab, README use case 12 |
| Replay, copy summary, export, redacted | app header buttons, README "Use" |
| Copilot CLI, VS Code, Codex next | owner, 2026-10-30 (roadmap statement, not shipped) |
| Install command | README Install |

## Share caption
See `share` in script.json.
