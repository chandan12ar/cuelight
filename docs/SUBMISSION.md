# Submitting Cuelight to Anthropic's directory

Status of Cuelight against Anthropic's published plugin requirements, plus ready-to-paste answers for the
submission form. Sources: the [pre-submission checklist](https://claude.com/docs/plugins/pre-submission-checklist),
[Submit your plugin](https://claude.com/docs/plugins/submit) and the
[plugin manifest reference](https://code.claude.com/docs/en/plugins-reference), read on 2026-10-03 and again on 2026-10-04;
the portal's findings were read on 2026-10-09 (below). Version 0.22.0 (M1, advice) adds no new kind of file; see "What M1 (0.22.0) adds".
Anthropic's rules change; re-check them before each submission.

## Where to submit

The developer portal at <https://claude.ai/directory/manage> (**Submit new**, then **Plugin bundle**).
The older Claude Console form at `platform.claude.com/plugins/submit` is no longer supported.

You need a paid claude.ai plan (Pro, Max, Team or Enterprise) and your GitHub account connected on claude.ai.
The repository is already public, which the listing requires before it goes live.

## Checklist

`tests/test_directory_readiness.py` checks the rows marked **test** on every run, so they cannot regress.

| Requirement | Status | How it is checked |
|---|---|---|
| `.claude-plugin/plugin.json` at the plugin root, valid | Done | `claude plugin validate --strict` in CI, and **test** |
| `name` is lowercase kebab-case, at most 64 characters, not reserved | Done (`cuelight`) | **test** |
| `description`, `author`, `version` set | Done | **test** |
| `license` set and a `LICENSE` file present | Done (MIT) | **test** |
| README of at least 40 words, outside code blocks | Done | **test** |
| Directory listing fields: `icon`, `documentationUrl`, `supportUrl`, `privacyPolicyUrl` | Done; URLs are `https://` and the icon is a PNG in the plugin | **test** |
| `hooks/hooks.json` valid, with a top-level `hooks` object and only real hook events | Done | **test** |
| Hook commands name each path in full from `${CLAUDE_PLUGIN_ROOT}` | Done | **test** |
| No `npx`, `uvx` or other package launchers, and no package install | Done (standard library only) | **test** |
| No file over 256 KiB (images and fonts aside) | Done | **test** |
| At most 512 files | Done | **test** |
| Only text, PNG, JPEG, GIF, WebP and font files | Done | **test** |
| No system files (`.DS_Store`, `Thumbs.db`, `desktop.ini`, `__MACOSX`) | Done | **test** |
| No symlinks, submodules or Git LFS | Done | **test** |
| No credential-shaped text in any file, **fake ones included** (tests and docs too) | Done (0.21.2; 0.21.0 was blocked for a fake `Authorization: Bearer` header in a test) | **test** (`TestNoSecretShapedText`: key formats, Bearer/Basic values, password and AWS secret assignments, quoted keys and tokens, passwords in URLs). Fakes are built from pieces in `tests/fake_secrets.py`; docs use `<fake-...>` placeholders |
| No runtime file reads a credential from the user's machine (env tokens, credential files), the demo's made-up tool calls included | Done (0.21.3) | **test** (`TestNoCredentialReads`) |
| Commit readable source, not minified or packed code | Done | by construction (no build step) |
| Describe in the README everything the plugin runs, sends or fetches | Done | README section "What Cuelight runs and touches" |
| Component files use the exact names Claude Code expects | Done | `claude plugin validate` |
| The slash command pre-approves no tools (no `allowed-tools`): broad shell access is held for a reviewer | Done (0.10.1) | **test** |
| README shows bundled images with Markdown image syntax only, no `<img>` or `<picture>` | Done (0.10.1) | **test** |
| No image the directory reads as carrying long embedded text (held for a reviewer) | Done (0.10.3: removed the unused `docs/assets/pill-states.png`, the one file it named) | the version's history in the portal |

## Findings in the portal, and what each one means for Cuelight

The portal lists every finding on a version (Review tab; expand a finding for its files). Only **Needs you**
items block. The rest are warnings a reviewer reads, so Cuelight is always held for a person: it ships hooks,
`bin/cuelight` and images its code uses. Read on v0.21.0 (2026-10-09):

| Finding | Files | What it is here | Action |
|---|---|---|---|
| **Secret in a shipped file** (blocking, `SECRET_IN_SCRIPT`) | `tests/test_runaway.py` | A made-up `Authorization: Bearer ...` header in a loop test. The scanner flags fakes too | Fixed in 0.21.2; the guard test now catches this shape |
| Image or font file that the plugin's code could run (`UNREAD_ASSET_REFERENCED`), 9 | `plugin.json`, `orchestra/static/app.js`, `orchestra/report.py`, two scripts and four documents under `docs/` | `plugin.json` names the listing icon; `app.js` draws the Work Floor from `agent-sprite.png`; `report.py` inlines it into a report; `docs/evidence/capture-icon.mjs` and `make_pill_gif.py` write the README's images; the documents mention image names. Nothing runs an image | None: a reviewer confirms |
| Uses a credential from the user's machine (`MCP_FORWARDS_CREDENTIAL_ENV`), 5 | `CHANGELOG.md`, an old plan in `docs/superpowers/`, `orchestra/demo.py`, `orchestra/static/app.js`, `plugin.json` | Cuelight reads no credential. `app.js` sends the dashboard's own token (made at launch, in the page's own URL) back to its own `127.0.0.1` server; the demo had a fake agent search the code for environment variables; the documents talk about tokens | Demo text changed in 0.21.3; `TestNoCredentialReads` keeps runtime code free of credential reads. The rest is for the reviewer |
| Files or downloads the validator couldn't inspect (`BINARIES_NOT_INSPECTED`) | `orchestra/static/agent-sprite.png` | A 1.46 MB, 1024x1248 full-colour PNG with no text chunks (only IHDR, IDAT, IEND). Its size is the likely reason | None: shrinking it would change how the sprites look |
| Unrecognized field in plugin.json, 3 | `documentationUrl`, `privacyPolicyUrl`, `supportUrl` | Listing fields the directory reads; Claude Code ignores them | None (the portal says so) |
| Field from another tool's manifest | `plugin.json` `icon` | The listing icon | None (the portal says so) |
| Contains a download-and-run command | `docs/evidence/screenshots/52-phone-agents-dark.png` | Text read out of a documentation screenshot | None (documentation only) |
| Uses hooks | `hooks/hooks.json` | The seven async observer hooks | None (information) |
| Ships executable files (`BUNDLES_BINARIES`) | `bin/cuelight` | A short readable POSIX shell script that starts `orchestra/__main__.py`, so "don't ask again" covers only `cuelight` (0.11.0) | None: a reviewer reads it |

After merging a fix, **Check for new commits** in the portal rescans at once instead of within about 6 hours.

## What M1 (0.22.0) adds

M1 is built on `feature/m1` and merges only after the 0.21.3 review is decided. It adds plain Python and
JavaScript, in the same shape as the rest of the plugin, and nothing the scanner has flagged before:

- **New files:** `orchestra/advice.py`, `orchestra/baselines.py`, `orchestra/listprices.py`,
  `orchestra/static/advice.js`, and tests (`tests/test_advice.py`, `test_advice_ui.py`, `test_baselines.py`,
  `test_listprices.py`). Two actions in `commands/open.md` (`advise`, `estimate`) that run the same
  `cuelight` launcher as the others.
- **Unchanged:** no new hook (still the seven async observer hooks), no binary or image, no network access
  (`tests/test_static_assets.py`), no `allowed-tools`, no package launcher, no file over 256 KiB
  (`app.js` is about 236 KiB in a CRLF checkout), no credential-shaped text (the card snippets are
  a `permissions.allow` entry, an `env` timeout line, `isolation: worktree`, `model: sonnet` and instruction
  sentences; `TestNoSecretShapedText` and `TestNoCredentialReads` pass).
- **New behaviour a reviewer may ask about:** the dashboard reads the user's past sessions under
  `~/.claude/projects`, read-only and in the background, and writes one numbers-only cache file
  (`baselines-v2.json`) in its own state directory, never under `~/.claude`. PRIVACY.md and the README's
  "What Cuelight runs and touches" say so. The built-in list prices are numbers in a Python file, not a
  fetch.
- **No new portal finding is expected.** If the portal shows one after the merge, add it to the findings
  table above with its files and what it means here.

## What to expect from review

- **Held for a reviewer (not a rejection).** Cuelight installs hooks and starts a local server, so a person
  will read it. Everything it does is in the README and PRIVACY.md, and the code is plain Python.
- **Name check.** `cuelight` was checked only informally. The closest name found is Cuelux (stage-lighting
  software). A look-alike can be held for a reviewer; an exact clash blocks. Run a proper trademark search first.
- **Claude Code only.** The directory shows the surfaces a plugin supports. Cuelight needs a local Python and
  hooks, so it is meant for Claude Code. Since 0.11.0 it also ships `bin/cuelight`, and claude.ai and Cowork
  do not install a plugin with a `bin/` directory. Hooks are ignored in claude.ai chat, and the local server cannot run there.

## Answers for the form

**Repository:** `https://github.com/chandan12ar/cuelight` (plugin path: leave empty; tracked branch: `main`).

**Short description:** Know the moment your Claude Code agents need you: a local, read-only dashboard of subagent runs.

**Example use cases:**
1. You start five subagents and switch to another window. A small pill shows when one is waiting for your
   permission, so you answer it instead of finding out ten minutes later.
2. A long run is slower than expected. The timeline, critical path and cost view show which agent held everything up.
3. Several sessions across different projects are open. The Fleet view lists which one needs you first.

**Data handling:**
- *Does it read or store personal data?* It reads Claude Code transcripts on the user's own machine, which may
  contain whatever the user discussed. It shows them only to the user, in a local browser tab. It stores only
  redacted hook events (7 days), a cache of numbers per finished agent in past sessions (type, model, time,
  tokens; no prompts, results, paths or commands; from 0.22.0) and, if the user opts in, metrics-only run
  history (90 days).
- *Does it send data to services other than its declared connectors?* No. It makes no outside network
  connections and has no telemetry.
- *How long is data kept?* Hook events 7 days; the past-runs cache until the system clears its temp folder;
  optional history 90 days; reports until the user deletes them.
- *Intended for people under 18?* No.

## After it is listed

- New versions arrive from the tracked branch: merge to `main`, and the directory scans and publishes it.
- Raise `version` in `.claude-plugin/plugin.json` with every release (see [../CONTRIBUTING.md](../CONTRIBUTING.md)).
- Do not rename the plugin. Use `displayName` for a different label.
