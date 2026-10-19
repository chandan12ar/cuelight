# Product tour video (about 90 seconds)

The storyboard for a narrated tour of Cuelight, recorded against `python -m orchestra --demo`
(sample data, labelled on screen as such). It is not part of the plugin: nothing here runs at
install time.

| File | What it is |
|---|---|
| `cuelight-promo.json` | the storyboard: one spoken sentence per beat, with the on-screen actions for that sentence |
| `cfg.js` | recorder settings (accent colour, when the page counts as loaded) |
| `brand.json` | colours and font for the title cards |

Story: a hook card (13 agents, which one needs you?), the problem, the solution, the attention
banners and Pulse, then one line each for Timeline, Graph, Agents, Insights, Spend, Prompts,
Activity, Work Floor and Fleet, and the install command. History is left out because it is empty
in the demo (`ORCHESTRA_HISTORY` is off).

## Rebuild

It is built with the `narrated-screen-video` skill's scripts (`build_video.py` and friends), which
are not kept in this repository. One fix is needed in that skill's `overlay.js` first: its spotlight
uses the classes `on` and `pulse`, and Cuelight's own `.pulse` (the Pulse strip) paints over the
spotlight, so every highlighted panel comes out as a blank white box. Rename them to `v-on` and
`v-pulse` in `overlay.js`.

```bash
python <skill>/scripts/build_video.py --script docs/video/cuelight-promo.json \
  --url "http://127.0.0.1:8799/?k=cuelightvideo&session=demo-checkout-v2" --ready-path /api/health \
  --serve-cmd "python -m orchestra --demo --no-open --port 8799 --token cuelightvideo" \
  --config-js docs/video/cfg.js --brand docs/video/brand.json \
  --engine kokoro --voice af_heart --rate=-12% --badge "DEMO RUN · SAMPLE DATA" \
  --out out/cuelight-tour.mp4
```

`--engine edge --voice en-US-AriaNeural` gives a Microsoft neural voice where that service is
reachable. The demo's numbers move while it records, so two builds never show exactly the same
values.
