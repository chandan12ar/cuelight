#!/usr/bin/env python3
"""Can this computer make a narrated video? Checks every piece and prints the exact command that fixes what is missing.

    python scripts/doctor.py             # full check, including a one-word test of the Microsoft voice (needs internet)
    python scripts/doctor.py --offline   # skip the online voice test

Exit 0: ready (ffmpeg, Playwright with a working Chromium, and a natural-sounding voice). Exit 1: something essential is
missing - run the "fix:" commands from the top, in order, then run doctor.py again. Fix commands use this exact Python
interpreter, so packages land where the scripts will look for them.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import env  # noqa: E402
import voicelab  # noqa: E402

OS = "windows" if env.WINDOWS else ("mac" if sys.platform == "darwin" else "linux")
PY = '"%s"' % sys.executable if " " in sys.executable else sys.executable
FIX = {
    "ffmpeg": {"windows": "winget install --id Gyan.FFmpeg -e", "mac": "brew install ffmpeg",
               "linux": "sudo apt-get install -y ffmpeg"}[OS],
    "playwright": "%s -m pip install playwright" % PY,
    "chromium": "%s -m playwright install %schromium" % (PY, "--with-deps " if OS == "linux" else ""),
    "edge": "%s -m pip install edge-tts" % PY,
    "kokoro": "%s -m pip install kokoro-onnx numpy  then  bash scripts/fetch_kokoro.sh ~/.cache/kokoro  (and set the two variables it prints)" % PY,
    "pillow": "%s -m pip install pillow" % PY,
    "whisper": "%s -m pip install faster-whisper" % PY,
    "chatterbox": "%s scripts/voicelab.py setup [--home <a drive with 6 GB free>]" % PY,
}
rows, missing = [], []


def row(state, what, detail="", fix=None, essential=False):
    rows.append((state, what, detail, fix))
    if state == "MISSING" and essential:
        missing.append(what)


def main():
    env.safe_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="do not test the online (Microsoft) voice")
    ap.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH"), help="a Chromium/Chrome binary to test instead of Playwright's")
    a = ap.parse_args()

    v = sys.version_info
    row("OK" if v >= (3, 9) else "MISSING", "python", "%d.%d.%d  %s" % (v[0], v[1], v[2], sys.executable),
        "install Python 3.9 or newer", essential=True)

    on_path = {t: shutil.which(t) for t in ("ffmpeg", "ffprobe")}
    found = env.ensure_tools()
    for t in ("ffmpeg", "ffprobe"):
        if not found[t]:
            row("MISSING", t, "", FIX["ffmpeg"] + ("   (then open a new terminal)" if OS == "windows" else ""), essential=True)
            continue
        ver = subprocess.run([found[t], "-version"], capture_output=True, text=True).stdout.split("\n")[0].split(" Copyright")[0]
        note = "" if on_path[t] else "   (not on this terminal's PATH yet - the scripts find it anyway; open a new terminal to use it by hand)"
        row("OK", t, "%s  %s%s" % (ver, found[t], note))

    browser_ok = False
    try:
        import playwright  # noqa: F401
        from importlib.metadata import version
        row("OK", "playwright", version("playwright"))
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                b = pw.chromium.launch(headless=True, **({"executable_path": a.chromium} if a.chromium else {}))
                pg = b.new_page()
                pg.set_content("<p id=ok>ok</p>")
                assert pg.text_content("#ok") == "ok"
                ver = b.version
                b.close()
            row("OK", "chromium", "%s launches headless" % ver)
            browser_ok = True
        except Exception as exc:  # noqa: BLE001
            row("MISSING", "chromium", (str(exc).strip().splitlines() or ["does not start"])[0][:110], FIX["chromium"], essential=True)
    except ImportError:
        row("MISSING", "playwright", "", FIX["playwright"], essential=True)
        row("MISSING", "chromium", "needs playwright first", FIX["chromium"], essential=True)

    natural = None
    if found["ffmpeg"]:
        import tts
        if tts._available("edge"):
            if a.offline:
                row("OK", "voice: edge", "installed (online test skipped)")
                natural = natural or "edge"
            else:
                ok, why = tts.edge_reachable()
                if ok:
                    row("OK", "voice: edge", "Microsoft neural voice works (best quality)")
                    natural = natural or "edge"
                else:
                    row("WARN", "voice: edge", "installed, but Microsoft's service is unreachable from here (%s)" % why,
                        "use kokoro offline (below), or run where the internet reaches speech.platform.bing.com")
        else:
            row("WARN", "voice: edge", "not installed (the best voice when online)", FIX["edge"])
        if tts._available("kokoro"):
            try:
                d = tempfile.mkdtemp(prefix="doctor-")
                tts.synth("Ready.", os.path.join(d, "k.wav"), "kokoro")
                shutil.rmtree(d, ignore_errors=True)
                row("OK", "voice: kokoro", "offline neural voice works")
                natural = natural or "kokoro"
            except Exception as exc:  # noqa: BLE001
                row("WARN", "voice: kokoro", "installed but failed: %s" % str(exc).splitlines()[0][:90], FIX["kokoro"])
        else:
            row("INFO", "voice: kokoro", "not set up (offline neural voice; only needed without internet)", FIX["kokoro"])
        lab = voicelab.ready()
        if lab:
            row("OK", "voice: chatterbox", "expressive voice (moods, cloning) ready: %s model, %.1fx real time here; lab in %s"
                % (lab["model"], lab["rtf"], voicelab.home()))
            natural = natural or "chatterbox"
        else:
            row("INFO", "voice: chatterbox", "not set up (optional: real emotion per sentence and your own cloned voice, offline; ~5 GB)",
                FIX["chatterbox"])
        for e, what in (("sapi", "Windows built-in voice"), ("say", "macOS built-in voice"), ("flite", "robotic voice")):
            if tts._available(e):
                row("INFO", "voice: " + e, "%s - never used for a video (sounds robotic/dated); --draft-voice for private previews" % what)
    if not natural:
        row("MISSING", "natural voice", "no edge, chatterbox or kokoro voice works - a video narrated by a robotic voice is "
            "worthless, so builds stop", FIX["edge"] + "   (or kokoro/chatterbox, above, without internet)", essential=True)

    try:
        import PIL  # noqa: F401
        row("OK", "pillow", "draws the final picture (window frame, camera, captions)")
    except ImportError:
        row("MISSING", "pillow", "the compose step (frame, camera, captions) needs it; --no-compose works without", FIX["pillow"], essential=True)
    try:
        import faster_whisper  # noqa: F401
        row("OK", "whisper", "narration check available (check_narration.py; first run downloads a ~140 MB model)")
    except ImportError:
        lab_on = voicelab.ready() is not None
        row("WARN" if lab_on else "INFO", "whisper",
            "needed to check chatterbox takes by listening (bad takes are re-made)" if lab_on else
            "optional: lets check_narration.py listen to the voice for wrong or swallowed words", FIX["whisper"])
    free = shutil.disk_usage(tempfile.gettempdir()).free / 1e9
    row("OK" if free > 3 else "WARN", "disk", "%.0f GB free in %s%s" % (free, tempfile.gettempdir(),
        "" if free > 3 else " - a long recording needs 1-3 GB of frames; pass --work on a bigger drive"))

    w = max(len(r[1]) for r in rows)
    for state, what, detail, fix in rows:
        print("  %-8s %-*s  %s" % (state, w, what, detail))
        if fix and state in ("MISSING", "WARN"):
            print("  %-8s %-*s  fix: %s" % ("", w, "", fix))
    print()
    if missing:
        print("NOT READY: %s. Run the fix commands above from the top, then run this again." % ", ".join(missing))
        return 1
    print("READY: this computer can make a narrated video (voice: %s%s). First time here? Run: %s scripts/selftest.py"
          % (natural, "" if browser_ok else ", browser untested", PY))
    return 0


if __name__ == "__main__":
    sys.exit(main())
