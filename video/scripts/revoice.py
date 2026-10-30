#!/usr/bin/env python3
"""Re-voice an already recorded walkthrough without recording the screen again.

build_video.py keeps its working folder (frames + rec.json with every beat's timing). This script
synthesizes the narration again with another engine or voice - or with the storyboard's new cast and moods - fits each
sentence into the slot the original sentence had (speaking it slightly faster if needed), and rebuilds the MP4. It takes
minutes, not a full re-recording, and the picture is identical.

    python revoice.py --script script.json --work <build_video --work dir> --engine edge --voice en-US-AvaMultilingualNeural --out out/new.mp4
    python revoice.py --script script.json --work <dir> --out out/new.mp4      # voices from the storyboard's "voice"/"cast"

The words must stay the same (the captions are in the timeline); change who says them, how, or with which voice.
"""
import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import env  # noqa: E402
import narrate  # noqa: E402
import tts  # noqa: E402
import voices  # noqa: E402
from build_video import FPS, frames_to_list, mix_narration, srt_time, write_voice_json  # noqa: E402
from record import LEAD  # noqa: E402

MAX_SPEEDUP = 18     # percent: beyond this a voice starts to sound rushed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="working folder of a previous build_video.py run")
    ap.add_argument("--engine", default="auto", choices=("auto",) + tts.ENGINES)
    ap.add_argument("--voice", default=None)
    ap.add_argument("--rate", default=None, help="speaking rate, e.g. -6%% (default: per engine and tone)")
    ap.add_argument("--script", required=True, help="the same storyboard JSON the recording used")
    ap.add_argument("--lexicon", default=None, help="extra pronunciation fixes JSON merged over lexicon.default.json")
    ap.add_argument("--finish", default=None, choices=tuple(voices.FINISH), help="narration finish (default: the storyboard's, else warm)")
    ap.add_argument("--voice-check", default="auto", choices=("auto", "warn", "off"))
    ap.add_argument("--draft-voice", action="store_true", help="allow a robotic/dated voice for a private timing preview")
    ap.add_argument("--jobs", type=int, default=2, help="(kept for older commands; voices are made one after another)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    env.safe_console()
    env.require_tools()

    with open(a.script, encoding="utf-8") as f:
        script = json.load(f)
    with open(os.path.join(a.work, "rec.json"), encoding="utf-8") as f:
        rec = json.load(f)
    lex = tts.load_lexicon()
    if a.lexicon:
        lex.update(tts.load_lexicon(a.lexicon))
    recorded = {bt["i"] for bt in rec["beats"]}
    script["beats"] = script["beats"][:max(recorded) + 1]          # a recording made with --only N has N beats
    plan = voices.plan(script, a.script, a.engine, a.voice, a.rate, lex, draft_ok=a.draft_voice)
    finish = a.finish or plan["finish"]
    plan["lines"] = [ln for ln in plan["lines"] if ln["i"] in recorded]
    for line in voices.describe(plan):
        print("voice: " + line)
    tag = hashlib.sha1(json.dumps([[s["engine"], s["voice"], s["sample_sha"]] for s in plan["speakers"].values()]).encode()).hexdigest()[:8]
    wdir = os.path.join(a.work, "wav-" + tag)
    slot = {bt["i"]: bt["dur"] - LEAD - 0.12 for bt in rec["beats"]}

    res = narrate.voice_all(plan, wdir, check=a.voice_check)
    if res["stopped"] and a.voice_check == "auto":
        print("\n!! sentence(s) that never came out right:\n   - " + "\n   - ".join(res["stopped"]))
        raise SystemExit(5)
    final = {ln["i"]: w for ln, w in zip(plan["lines"], res["wavs"])}
    redo = []
    for ln, d in zip(plan["lines"], res["durations"]):
        if d > slot[ln["i"]]:
            redo.append(voices.faster(ln, min(MAX_SPEEDUP, int((d / slot[ln["i"]] - 1) * 100) + 2)))
    if redo:
        print("  %d line(s) run long; re-speaking them slightly faster" % len(redo))
        sub = dict(plan, lines=redo)
        res2 = narrate.voice_all(sub, wdir, check=a.voice_check)
        for ln, w, d in zip(redo, res2["wavs"], res2["durations"]):
            final[ln["i"]] = w
            if d > slot[ln["i"]]:
                print("  ! line %d still %.1f s long; it overlaps the next sentence's lead-in" % (ln["i"] + 1, d - slot[ln["i"]]))

    list_path = os.path.join(a.work, "frames.txt")
    t0, total = frames_to_list(rec, list_path)
    starts = {bt["i"]: max(0.0, bt["start"] - t0 + LEAD) for bt in rec["beats"]}
    order = sorted(final)
    narr = os.path.join(a.work, "narration-%s.wav" % tag)
    by_i = {ln["i"]: ln for ln in plan["lines"]}
    mix_narration(narr, total + 0.5, [(starts[i], final[i]) for i in order], [by_i[i]["who"] for i in order])
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    write_voice_json(os.path.join(a.work, "voice-%s.json" % tag), dict(plan, lines=[by_i[i] for i in order]),
                     {i: final[i] for i in order}, res["verdicts"])
    if rec.get("compose"):
        # captions are drawn by compose.py: re-time every highlighted word to the new voice, then draw the video again
        import compose
        import words
        rec2 = copy.deepcopy(rec)
        for c in rec2["timeline"]["captions"]:
            ln = by_i.get(c["beat"])
            if ln is None:
                continue
            if c.get("words"):
                c["words"] = words.word_timings(c["text"], final[c["beat"]])
            if plan["multi"]:                         # the cast may have changed: label captions with the new speakers
                sp = plan["speakers"][ln["who"]]
                c["who"], c["speaker"], c["color"] = ln["name"], sp["index"], sp["color"]
            else:
                for k in ("who", "speaker", "color"):
                    c.pop(k, None)
        opts = dict(rec2.get("compose_opts") or {}, narration=narr, finish=finish)
        if opts.get("out_size"):
            opts["out_size"] = tuple(opts["out_size"])
        compose.compose(a.work, a.out, opts, rec=rec2)
    else:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                        "-i", narr, "-vf", "fps=%d,format=yuv420p" % FPS, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                        "-af", voices.audio_filter(finish), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                        "-shortest", a.out], check=True)
    with open(os.path.splitext(a.out)[0] + ".srt", "w", encoding="utf-8") as f:
        for n, i in enumerate(order, 1):
            ln = by_i[i]
            f.write("%d\n%s --> %s\n%s%s\n\n" % (n, srt_time(starts[i]), srt_time(starts[i] + tts.duration(final[i])),
                                                 "[%s] " % ln["name"] if plan["multi"] else "", ln["caption"]))
    print("Done: %s (%.1f MB)   narration check: python scripts/check_narration.py --work %s --voice-file %s"
          % (a.out, os.path.getsize(a.out) / 1e6, a.work, os.path.join(a.work, "voice-%s.json" % tag)))


if __name__ == "__main__":
    main()
