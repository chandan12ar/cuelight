# Examples

- `cli-demo.json` - a terminal session recorded with `assets/terminal-replay.html` (the first two commands print real output of the machine that built it; the `run-checks` line is a stand-in to show colour).
  Run: `python3 -m http.server 8790 -d assets &` then
  `python scripts/build_video.py --script assets/examples/cli-demo.json --url http://127.0.0.1:8790/terminal-replay.html --config-js assets/examples/terminal-cfg.js --out out/cli.mp4`
- `../demo-site/` - a three-tab demo web app with a storyboard; `scripts/selftest.py` records it (typing included).
- `../templates/` - storyboard skeletons by product type (dashboard, SaaS onboarding, API docs, CLI, release notes, launch).
