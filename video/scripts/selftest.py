#!/usr/bin/env python3
"""Prove this computer can make a video in 1-2 minutes, before spending 20 on a real one. Windows, macOS and Linux.

    python scripts/selftest.py [--engine auto|edge|chatterbox|kokoro] [--voice NAME] [--out DIR]

Runs doctor.py, serves the bundled demo app, drafts a storyboard and reads the brand from it, then builds the bundled
storyboard (storyboard check, title-card preflight, blur, typing, callouts, word-highlighted captions, strict targets,
then the compose step: window frame, auto-zoom camera), checks loudness, sync and pacing, writes the per-sentence
review sheets, makes the extras and, when Whisper is installed, listens to the narration. The bundled storyboard is a
two-voice conversation (host and expert, with moods), so speaker captions and levelling are tested too. When the voice
lab is set up, Chatterbox voices one excited line and Whisper checks it. Afterwards LOOK at review/sheet-*.jpg,
contact.jpg and work/preflight/preflight.jpg.
"""
import argparse
import functools
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import env  # noqa: E402
import narrate  # noqa: E402
import voicelab  # noqa: E402

DEMO = os.path.join(HERE, "..", "assets", "demo-site")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def step(n, title, args):
    print("\n== %s %s" % (n, title), flush=True)
    t = time.time()
    r = subprocess.run([sys.executable] + args, env=dict(os.environ, PYTHONUNBUFFERED="1"))   # progress shows as it happens
    if r.returncode != 0:
        print("\nSELFTEST FAILED at step %s (%s), exit %d. The lines above say why." % (n, title, r.returncode))
        sys.exit(r.returncode)
    print("   (%.0f s)" % (time.time() - t), flush=True)


def main():
    env.safe_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default="auto")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--out", default=None, help="folder for the results (default: a new temp folder)")
    ap.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH"))
    a = ap.parse_args()
    out = os.path.abspath(a.out or tempfile.mkdtemp(prefix="narrated-selftest-"))
    os.makedirs(out, exist_ok=True)
    sh = lambda name: os.path.join(HERE, name)
    chromium = ["--chromium", a.chromium] if a.chromium else []
    voice = ["--engine", a.engine] + (["--voice", a.voice] if a.voice else [])

    step("1/8", "can this computer make a video?", [sh("doctor.py")] + (["--offline"] if a.engine not in ("auto", "edge") else []))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=os.path.abspath(DEMO)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    script = os.path.join(DEMO, "script.json")
    try:
        step("2/8", "storyboard check on the bundled storyboard", [sh("lint_storyboard.py"), "--script", script, "--source", DEMO])
        step("3/8", "draft a storyboard + read the brand from the app",
             [sh("draft_storyboard.py"), "--url", url, "--out", os.path.join(out, "draft.json"), "--shots", os.path.join(out, "shots"),
              "--nav", "#nav button", "--name", "Acme Reports"] + chromium)
        subprocess.run([sys.executable, sh("extract_brand.py"), "--url", url, "--out", os.path.join(out, "brand.json")] + chromium, check=True)
        step("4/8", "build the bundled storyboard (check, preflight, voice, record, compose)",
             [sh("build_video.py"), "--script", script, "--url", url, "--config-js", os.path.join(DEMO, "cfg.js"), "--blur-auto",
              "--brand", os.path.join(out, "brand.json"), "--source", DEMO, "--size", "1280x720", "--out", os.path.join(out, "selftest.mp4"),
              "--work", os.path.join(out, "work"), "--no-gates"] + voice + chromium)
        # the gates must hold: without --no-gates the same build has to stop before voicing (7 pronunciation, 8 voice sample)
        print("\n== 4b the gates stop an unchecked build", flush=True)
        r = subprocess.run([sys.executable, sh("build_video.py"), "--script", script, "--url", url, "--config-js", os.path.join(DEMO, "cfg.js"),
                            "--source", DEMO, "--size", "1280x720", "--out", os.path.join(out, "gate-test.mp4"),
                            "--work", os.path.join(out, "gate-work")] + voice + chromium, env=dict(os.environ, PYTHONUNBUFFERED="1"),
                           capture_output=True, text=True)
        if r.returncode not in (7, 8):
            print(r.stdout[-1500:], r.stderr[-800:])
            print("\nSELFTEST FAILED at step 4b: the build was not stopped by a gate (exit %d)" % r.returncode)
            sys.exit(1)
        print("   gate held: exit %d (%s)" % (r.returncode, "pronunciation" if r.returncode == 7 else "voice sample"), flush=True)
        step("5/8", "check the video like an editor (+ per-sentence review sheets)",
             [sh("check_video.py"), os.path.join(out, "selftest.mp4"), "--work", os.path.join(out, "work"), "--script", script,
              "--sheet", os.path.join(out, "contact.jpg"), "--frames", "12", "--review", os.path.join(out, "review")])
        step("6/8", "extras (poster, teaser, vertical, gif)",
             [sh("make_extras.py"), os.path.join(out, "selftest.mp4"), "--work", os.path.join(out, "work"), "--script", script,
              "--out-dir", os.path.join(out, "extras"), "--teaser-len", "20"])
        step("7/8", "listen to the narration (skipped when Whisper is not installed)",
             [sh("check_narration.py"), "--work", os.path.join(out, "work"), "--model", "tiny.en"])
        short = voicelab.ready() and narrate.too_little_memory()
        if voicelab.ready() and not short:
            step("8/8", "the expressive voice: Chatterbox says one excited line, Whisper checks it",
                 [sh("voicelab.py"), "say", "This is it, the whole report in one click!", "--mood", "excited", "--check",
                  "--out", os.path.join(out, "chatterbox-excited.wav")])
        elif short:
            print("\n== 8/8 expressive voice: skipped - %s Close big programs and run this again to test it." % short)
        else:
            print("\n== 8/8 expressive voice: skipped (the voice lab is not set up: python scripts/voicelab.py setup)")
    finally:
        srv.shutdown()
    print("\nSELFTEST OK. Results in %s\n  look at: review/sheet-*.jpg, contact.jpg, work/preflight/preflight.jpg, selftest.mp4, "
          "selftest.description.md" % out)


if __name__ == "__main__":
    main()
