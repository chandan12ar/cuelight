---
description: Open the Cuelight dashboard for this session's subagents
argument-hint: "[stop | report [path] | advise]"
---

Run Cuelight for the current Claude Code session.

Each action is one `cuelight` command. Claude Code puts the plugin's `bin/`
directory on the Bash tool's PATH, so `cuelight` runs as a bare command. Run it
exactly as written, with the Bash tool, from the current directory: no `cd`, no
extra flags and no shell variables. Cuelight finds the session and the project by
itself.

Dispatch on `$ARGUMENTS`:

- **empty** — start the dashboard and print its URL:
  ```bash
  cuelight
  ```
- **`stop`** — shut the server down:
  ```bash
  cuelight --stop
  ```
- **`report`** or **`report <path>`** — write a self-contained HTML snapshot:
  ```bash
  cuelight --report "<path or .>"
  ```
- **`advise`** — what to change next time: the run's verdict, its fix-it cards and a
  short fact sheet:
  ```bash
  cuelight --advise
  ```

If the shell says `cuelight` is not found (an older Claude Code), run the same
action as `python "${CLAUDE_PLUGIN_ROOT}/orchestra/__main__.py"` followed by the
same flags, using `python3` if `python` is not found.

Report back to the user exactly what the command printed — the URL, the report
path, or the failure text. Do not paraphrase a failure as success, and do not
retry a failed start more than once.

For `advise`, show the user what it printed, keeping each fix's snippet exactly as written.
Do not change any file because of it unless the user asks you to; if they do, make the change
with your normal tools so they approve it as usual.

If the command prints "no session id", tell the user Cuelight could not detect
the session and they can pass one explicitly with `--session`.
