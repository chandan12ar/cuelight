# Contributing to Cuelight

Thanks for helping. Cuelight is small on purpose: Python standard library only, a vanilla JavaScript front end,
no build step and no network access. Please keep it that way.

## Set up

```bash
git clone https://github.com/chandan12ar/cuelight
cd cuelight
python -m unittest discover -s tests -t . -v      # the whole suite; needs Python 3.9+ and, optionally, Node
python -m orchestra --demo                        # a scripted run to look at, no real session needed
```

Node is optional: tests that render the page under Node skip themselves without it. To check the plugin files
the way the directory does, install Claude Code and run
`claude plugin validate --strict .claude-plugin/plugin.json` (and the same for `marketplace.json`).

## Rules that are not negotiable

These are enforced by tests, so a change that breaks one fails CI. The reasons are in [SECURITY.md](SECURITY.md).

- The server binds `127.0.0.1` only and checks `Host`, `Origin` and the token.
- No outside network access from the program or the page (no CDN, fonts, analytics).
- Nothing is ever written under `~/.claude`.
- Everything leaving the process passes through `orchestra/redact.py`.
- Hooks never block or fail Claude Code: async, always exit 0.
- Cuelight observes only. Approving or denying from the dashboard is out of scope for this plugin.

## What the plugin directory's scanner flags

Every merge to `main` is scanned by Anthropic's plugin directory, which reads every file in the repository,
tests and docs included. These have held or blocked a version before; `tests/test_directory_readiness.py`
catches each one first (details in [docs/SUBMISSION.md](docs/SUBMISSION.md)):

- **Credential-shaped text, even fake.** Never write a key, token, `Authorization: Bearer ...` value or
  `password=...` value out in a file. Tests build their fakes from pieces in `tests/fake_secrets.py`; docs use
  obvious placeholders such as `<fake-token>`.
- **Reading a credential.** Runtime code (`orchestra/`, `hooks/`, `bin/`, `commands/`) reads no tokens from the
  environment or credential files, and the demo's made-up tool calls do not look like it either.
- **Images.** Show images in the README with Markdown syntax only; do not add an image nothing uses; keep
  scripts from naming bundled images where you can.
- **File size.** No file over 256 KiB (images aside). `orchestra/static/app.js` is close: put a new tab-sized
  view in `orchestra/static/tabs.js`, or, if it is a tab with its own state and data, in a file of its own
  loaded after `tabs.js` the way `orchestra/static/advice.js` is (add it to `index.html`, to `report.py` and to
  the test helpers that read the front end).

## Making a change

1. Branch from `main` (`feature/...` or `fix/...`).
2. Write the test first when you can, then the change.
3. Run the full suite. Check the page in a real browser if you touched `orchestra/static/`; the stubbed DOM in
   some tests cannot show layout or contrast problems. After editing a static file, reload the page for real
   (the server reads the files on every request): a change to the `#...` part of the address alone does not
   reload it.
4. Update [CHANGELOG.md](CHANGELOG.md) under `Unreleased`.
   Advice and "usual" numbers have three test files to extend: `tests/test_advice.py` (a new rule or
   failure cause: its trigger, a case just below its threshold, merging, and what its snippet says),
   `tests/test_baselines.py` (anything about past runs or the cache) and `tests/test_advice_ui.py` (the tab).
   A card's snippet is text people paste into their settings, so a test pins its exact text, and nothing is
   suggested unless the record proves the whole of it.
5. Open a pull request. CI runs Linux, macOS and Windows on Python 3.9, 3.12 and 3.13, and the plugin
   validator. `main` only changes by merged pull request, and only once CI is green on every job.

## Releasing (maintainers)

Installed copies stay on the `version` in `.claude-plugin/plugin.json` until it changes, so any user-visible
change needs a bump (patch for fixes and look-and-feel, minor for features). Move the `Unreleased` notes under a
dated version heading, merge, then tag `vX.Y.Z` and publish a GitHub Release with the same notes.
Never rename the plugin: installs are recorded under its name. Use `displayName` for a different label.

## Reporting problems

Bugs and ideas: open an issue. Security problems: do not open a public issue; see [SECURITY.md](SECURITY.md).
