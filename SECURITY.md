# Security

## Threat model

Cuelight is a **local, read-only** viewer. It assumes other processes on the
same machine, and other web pages open in the user's browser, are hostile.

| Property | Mechanism |
|---|---|
| Reachable only from this machine | Binds `127.0.0.1`; every request re-checks the `Host` header (DNS rebinding) |
| Not readable by other web pages | `Origin` must be loopback or absent |
| Not readable by other local users | Every `/api/*` route except `/api/health` needs a per-launch token (`secrets.token_urlsafe`, compared with `hmac.compare_digest`); the port file holding it is mode `0600` |
| No data leaves the machine | No CDN, fonts, telemetry or analytics; `tests/test_static_assets.py` fails the build on any external reference |
| Cannot alter Claude Code | Never writes under `~/.claude` |
| Secrets in transcripts are not displayed | Everything is passed through `redact.py` at one chokepoint before serialization |
| Cannot be used to read arbitrary files | Static serving resolves `realpath` and refuses anything outside `static/` |

Redaction is pattern-based and best-effort: it covers common key shapes
(Anthropic/OpenAI/GitHub/AWS keys, JWTs, PEM blocks, `Bearer` tokens, and
credential-named assignments). It cannot recognise an arbitrary secret with no
tell-tale shape. Treat a static report like the transcripts it summarises.

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private vulnerability
reporting on this repository (Security tab -> Report a vulnerability). Include
steps to reproduce and the affected version.

## Out of scope by design

Cuelight does not approve, deny or otherwise control Claude Code. If that is
ever added it will be a separate opt-in plugin with its own threat model and an
audit log, and this document will be updated first.

Since 0.22.0 Cuelight also suggests changes (the Advice tab, `/cuelight:open advise`), but only as text
you copy: it never writes a settings file, an agent definition or a `CLAUDE.md`. A suggested
`permissions.allow` rule is made only for a whole, plain test, build or lint command or a plain web domain,
and says how far it reaches. Its scan of your past sessions is read-only and keeps numbers only.
