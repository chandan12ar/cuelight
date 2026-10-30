#!/usr/bin/env python3
"""Record a narrated screen video of a web app from a storyboard.

    python build_video.py --script script.json --url http://localhost:8000/ --out out/demo.mp4
    python build_video.py --script script.json --serve-cmd "python app.py --port 8799" --url http://127.0.0.1:8799/

What it does: synthesizes the narration for every sentence in the storyboard, drives a headless
browser through it (cursor, scrolling, spotlight ring, captions, title cards) while capturing frames
with exact timestamps, and joins picture and voice into an MP4 with matching SRT captions and chapters.

One-time setup:  pip install playwright && playwright install chromium   (+ a TTS engine, see tts.py)
                 and ffmpeg/ffprobe on your PATH.
"""
import argparse
import array
import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import env  # noqa: E402
import lint_storyboard  # noqa: E402
import narrate  # noqa: E402
import tts  # noqa: E402
import voices  # noqa: E402
import words  # noqa: E402
from record import LEAD, record  # noqa: E402

FPS = 30


def write_description(path, script, rec, beats, durations, plan, badge, t0):
    """The post that goes with the video: title, the storyboard's share caption, how to start, chapters, honesty notes."""
    outro = (script.get("stages") or {}).get("outro") or {}
    lines = ["# %s" % script.get("title", "Walkthrough"), "",
             script.get("share") or "(add a 1-3 sentence \"share\" caption to the storyboard and rebuild)", ""]
    lines += [x for x in (outro.get("cmd"), outro.get("url")) if x] + ([""] if outro.get("cmd") or outro.get("url") else [])
    chapters = []
    for bt in rec["beats"]:
        b = beats[bt["i"]]
        if b.get("chapter"):
            s = max(0.0, bt["start"] - t0 + LEAD) if chapters else 0.0     # YouTube wants the first chapter at 0:00
            chapters.append("%d:%02d %s" % (s // 60, s % 60, b["chapter"]))
    if len(chapters) >= 3:
        lines += ["Chapters", ""] + chapters + [""]
    lines += ["---"]
    for sid in plan["order"]:
        s = plan["speakers"][sid]
        who = ("%s: " % s["name"]) if plan["multi"] else "Narration: "
        if s["sample"]:
            lines.append("%sAI voice cloned (with consent) from a recording of %s, made with Chatterbox." % (who, voices.consent_of(s["sample"])))
        else:
            lines.append("%sAI voice, %s %s." % (who, s["engine"], s["voice"]))
    if badge:
        lines.append("Shows sample data (labelled \"%s\" in the video), not real usage." % badge)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def start_server(cmd, url, ready_path):
    """Start the app (if --serve-cmd was given) and wait until `url + ready_path` answers."""
    p = None
    if cmd:
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(150):
        if p is not None and p.poll() is not None:
            raise SystemExit("app server exited early:\n" + (p.stdout.read() or ""))
        try:
            urllib.request.urlopen(url if not ready_path else url.rstrip("/") + "/" + ready_path.lstrip("/"), timeout=2).read(64)
            return p
        except Exception:
            time.sleep(0.3)
    if p is not None:
        p.kill()
    raise SystemExit("app did not answer at %s" % url)


def srt_time(s):
    ms = int(round(s * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def speech_level(pcm):
    """RMS of the voiced part of a clip (20 ms frames within 20 dB of the loudest), so pauses do not count."""
    hop = 480
    frames = [sum(x * x for x in pcm[i:i + hop]) / hop for i in range(0, len(pcm) - hop, hop)]
    if not frames:
        return 0.0
    top = max(frames)
    act = [f for f in frames if f > top * 0.01]
    return math.sqrt(sum(act) / len(act))


def mix_narration(path, total_s, clips, speakers=None):
    """clips: [(offset_s, wav_path)] -> one 24 kHz mono wav, `total_s` long.
    `speakers` (one id per clip): two voices, often from different engines, rarely come out equally loud. Each speaker
    is brought to the first speaker's level (one gain per speaker, so a speaker's own louder and softer lines stay as
    they are); the narration's loudness is set at the very end (loudnorm)."""
    rate = 24000
    buf = bytearray(int(total_s * rate) * 2)
    data, gain = [], {}
    for off, wp in clips:
        with wave.open(wp, "rb") as w:
            assert w.getframerate() == rate and w.getnchannels() == 1 and w.getsampwidth() == 2, wp
            data.append(w.readframes(w.getnframes()))
    if speakers and len(set(speakers)) > 1:
        levels = {}
        for sid, d in zip(speakers, data):
            pcm = array.array("h"); pcm.frombytes(d)
            levels.setdefault(sid, []).append(speech_level(pcm))
        med = {sid: sorted(v)[len(v) // 2] for sid, v in levels.items()}
        ref = med[speakers[0]]
        gain = {sid: min(2.5, max(0.4, ref / m)) if m > 0 else 1.0 for sid, m in med.items()}
    for n, ((off, wp), d) in enumerate(zip(clips, data)):
        g = gain.get(speakers[n], 1.0) if speakers else 1.0
        if abs(g - 1.0) > 0.02:
            pcm = array.array("h"); pcm.frombytes(d)
            peak = max(1, max(abs(x) for x in pcm))
            g = min(g, 31000.0 / peak)             # never push a peak into clipping
            pcm = array.array("h", (int(x * g) for x in pcm))
            d = pcm.tobytes()
        i = int(off * rate) * 2
        buf[i:i + len(d)] = d[:max(0, len(buf) - i)]
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(bytes(buf))
    return gain


def write_voice_json(path, plan, wavs, verdicts):
    """What every clip was meant to say, and who said it how: check_narration.py listens against this."""
    sp = plan["speakers"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"engine": "/".join(sorted({s["engine"] for s in sp.values()})),
                   "voice": " + ".join(sp[s]["voice"] for s in plan["order"]),
                   "speakers": {sid: {k: s[k] for k in ("name", "engine", "voice", "rate")} for sid, s in sp.items()},
                   "beats": [{"i": ln["i"], "wav": wavs[ln["i"]], "spoken": ln["text"], "caption": ln["caption"],
                              "who": ln["name"], "mood": ln["mood"], "engine": ln["engine"],
                              "take": (verdicts.get(ln["i"]) or {}).get("take")} for ln in plan["lines"]]}, f, indent=1)


def frames_to_list(rec, list_path):
    frames = rec["frames"]
    t0 = frames[0][0]
    with open(list_path, "w") as f:
        f.write("ffconcat version 1.0\n")
        for i, (ts, path) in enumerate(frames):
            nxt = frames[i + 1][0] if i + 1 < len(frames) else rec["t_end"]
            d = nxt - ts
            if d <= 0.0005:
                continue
            f.write("file '%s'\nduration %.4f\n" % (path.replace("\\", "/"), d))
        f.write("file '%s'\n" % frames[-1][1].replace("\\", "/"))
    return t0, rec["t_end"] - t0


def set_card_backdrops(script, mode="auto"):
    """Give every title card without its own "backdrop" one: plain (the gradient) or blur (the live app behind it, blurred).
    auto: the first and last card used are plain - the opening and closing read best on a clean background - and the cards in
    between blur the app, so the product stays visible behind the explanation; blur / plain: all of them."""
    stages = script.get("stages") or {}
    used = []
    for b in script.get("beats", []):
        for act in b.get("do", []) if isinstance(b, dict) else []:
            if isinstance(act, dict) and act.get("stage") and act["stage"] in stages and act["stage"] not in used:
                used.append(act["stage"])
    if "intro" in stages and "intro" not in used:
        used.insert(0, "intro")                   # the recorder opens on the intro card before the first beat
    for name, spec in stages.items():
        if isinstance(spec, dict) and "backdrop" not in spec:
            plain = mode == "plain" or (mode == "auto" and (name == used[0] or name == used[-1] or name not in used))
            spec["backdrop"] = "plain" if plain else "blur"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--script", required=True, help="storyboard JSON (see assets/script.example.json)")
    ap.add_argument("--url", required=True, help="page to record, e.g. http://127.0.0.1:8799/")
    ap.add_argument("--serve-cmd", default=None, help="shell command that starts the app (omit if it is already running)")
    ap.add_argument("--ready-path", default="", help="URL path that answers once the app is up (default: the page itself)")
    ap.add_argument("--init-js", default=None, help="JS file run before the page's own scripts (seed localStorage, flags)")
    ap.add_argument("--config-js", default=None, help="JS file that sets window.V_CONFIG (target resolvers, accent colour)")
    ap.add_argument("--sidebar-px", type=int, default=0, help="width of a fixed left sidebar; the cursor lands exactly there")
    ap.add_argument("--scheme", default="light", choices=("light", "dark"), help="browser colour scheme")
    ap.add_argument("--badge", default="", help="text for a small corner badge when a beat says {BADGE} (e.g. SAMPLE DATA)")
    ap.add_argument("--storage-state", default=None, help="saved login (scripts/save_login.py) for apps behind a sign-in")
    ap.add_argument("--basic-auth", default=None, help="user:password for HTTP basic auth")
    ap.add_argument("--brand", default=None, help="brand.json from extract_brand.py (title cards use the app's colours/font)")
    ap.add_argument("--blur", default="", help="comma-separated CSS selectors to blur (emails, keys, names)")
    ap.add_argument("--blur-auto", action="store_true", help="also blur emails, API keys/tokens and IP addresses found in the page text")
    ap.add_argument("--lexicon", default=None, help="extra pronunciation fixes JSON {\"API\": \"A P I\"} merged over lexicon.default.json")
    ap.add_argument("--no-highlight", action="store_true", help="plain sentence captions instead of word-by-word highlighting")
    ap.add_argument("--allow-missing", action="store_true", help="do not fail when a spotlight/scroll/click target was not found")
    ap.add_argument("--source", action="append", default=[], help="project folder/file the narration's numbers and names must come from (grounding check; repeatable)")
    ap.add_argument("--skip-preflight", action="store_true", help="record even if the title-card/caption check finds errors")
    ap.add_argument("--engine", default="auto", choices=("auto",) + tts.ENGINES, help="text-to-speech engine (the storyboard's \"voice\"/\"cast\" can set it per speaker)")
    ap.add_argument("--voice", default=None, help="voice name for the engine")
    ap.add_argument("--rate", default=None, help="speaking rate, e.g. -10%% or +8%%. Default is per engine (kokoro -20%%, edge -4%%) to land near 160 words/min, shifted by the storyboard's tone")
    ap.add_argument("--finish", default=None, choices=tuple(voices.FINISH), help="how the narration is finished: warm (default: light EQ, de-esser, gentle compression), clean, broadcast, off")
    ap.add_argument("--voice-check", default="auto", choices=("auto", "warn", "off"),
                    help="chatterbox takes are checked by listening (Whisper) and re-taken when wrong; auto stops the build when a sentence never comes out right, warn only lists it")
    ap.add_argument("--no-gates", action="store_true", help="skip the plan, pronunciation and voice-sample gates (selftest and private timing previews only)")
    ap.add_argument("--draft-voice", action="store_true", help="allow a robotic/dated voice (sapi, say, flite) for a timing preview nobody else will see")
    ap.add_argument("--out", default="out/video.mp4")
    ap.add_argument("--size", default="1600x900")
    ap.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH"), help="path to a Chromium/Chrome binary (default: Playwright's)")
    ap.add_argument("--no-captions", action="store_true", help="do not burn captions into the picture")
    ap.add_argument("--only", type=int, default=0, help="debug: only the first N beats")
    ap.add_argument("--work", default=None, help="working directory (kept; default: a temp dir)")
    look = ap.add_argument_group("look (compose step; the storyboard's \"look\" object sets the same, flags win)")
    look.add_argument("--frame", choices=("window", "browser", "none"), default=None, help="app in a rounded window, with a browser bar, or full-bleed (default window)")
    look.add_argument("--background", default=None, help="behind the window: brand | dark | light | blur (the app itself, blurred) | #hex | #hex1,#hex2 | image file (default brand)")
    look.add_argument("--cards", choices=("auto", "blur", "plain"), default=None, help="title cards over the app: blur = the app blurred behind the card, plain = gradient, auto (default) = first and last plain, the cards between blurred")
    look.add_argument("--camera", choices=("auto", "manual", "off"), default=None, help="auto-zoom into clicks, typing and small spotlights (default auto); manual = only actions with \"zoom\"")
    look.add_argument("--out-size", default=None, help="video size, e.g. 1920x1080 (default: 1080p in the recording's shape)")
    look.add_argument("--show-url", default=None, help="text for the browser bar instead of a localhost address, e.g. app.acme.com")
    look.add_argument("--dsf", type=float, default=None, help="pixel density of the recording (default 1.25: sharper zooms and still ~30 fps capture; 1.5 = sharpest zoom but ~20 fps during motion)")
    look.add_argument("--easing", choices=("spring", "smooth"), default=None, help="camera moves: spring (quick start, soft settle; default) or smooth ease")
    look.add_argument("--no-chapter-titles", action="store_true", help="no chapter title when a chapter starts")
    look.add_argument("--no-progress-bar", action="store_true", help="no chapter progress bar along the top")
    look.add_argument("--no-sfx", action="store_true", help="no interface sounds (mouse clicks, key taps while typing, keycaps, card whooshes)")
    look.add_argument("--sfx-db", type=float, default=None, help="loudness of the loudest sound effect (default -16 dB)")
    look.add_argument("--no-motion-blur", action="store_true", help="no motion blur on fast camera moves")
    look.add_argument("--jobs", type=int, default=4, help="parallel workers for the compose step")
    look.add_argument("--no-compose", action="store_true", help="old single pass: captions drawn into the page, no frame, no camera")
    a = ap.parse_args()

    env.safe_console()
    env.require_tools()
    W, H = (int(x) for x in a.size.lower().split("x"))
    work = a.work or tempfile.mkdtemp(prefix="narrated-video-")
    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)

    data_line = ""     # kept so storyboards written for older versions (with {DATA}) still work: it renders as nothing
    with open(a.script, encoding="utf-8") as f:
        script = json.load(f)
    rd = lambda p: open(p, encoding="utf-8").read() if p else ""
    lex = tts.load_lexicon()
    import pronounce_check
    if os.path.exists(pronounce_check.project_lexicon_path(a.script)):      # <script folder>/lexicon.json is always used
        lex.update(tts.load_lexicon(pronounce_check.project_lexicon_path(a.script)))
    if a.lexicon:
        lex.update(tts.load_lexicon(a.lexicon))

    # 0. check the storyboard before spending minutes on voice and recording (errors stop here, warnings are listed)
    E, Wn, N = lint_storyboard.lint(script, a.script, a.source, lexicon=lex, parts=("text",))
    lint_storyboard.report(E, Wn, N, "checking the storyboard...")
    if E:
        sys.exit(2)
    import plan_board
    why = None if a.no_gates else plan_board.check_approval(script, a.script)
    if why:
        sys.exit("build stopped: " + why)
    if not a.no_gates:
        left = pronounce_check.unreviewed(script, a.script, lex)
        if left:
            print("\n%d term(s) the voice may get wrong - decide each before voicing:\n%s"
                  % (len(left), pronounce_check.report(left, lex)))
            print("Fix with: python scripts/pronounce_check.py --script %s --add \"Term=how it is said\"  (or --accept when the "
                  "written form is right), then build again." % a.script)
            sys.exit(7)
    if a.only:
        script["beats"] = script["beats"][:a.only]
    beats = script["beats"]

    lk = script.get("look") or {}
    set_card_backdrops(script, a.cards or lk.get("cards", "auto"))
    composing = not a.no_compose
    copts = {"frame": a.frame or lk.get("frame", "window"), "background": a.background or lk.get("background", "brand"),
             "camera": a.camera or lk.get("camera", "auto"), "url": a.show_url or lk.get("url"),
             "outro_url": ((script.get("stages") or {}).get("outro") or {}).get("url"), "captions": not a.no_captions,
             "out_size": tuple(int(x) for x in a.out_size.lower().split("x")) if a.out_size else None,
             "easing": a.easing or lk.get("easing", "spring"),
             "chapter_titles": not a.no_chapter_titles and lk.get("chapter_titles", True),
             "progress_bar": not a.no_progress_bar and lk.get("progress_bar", True),
             "motion_blur": not a.no_motion_blur and lk.get("motion_blur", True),
             "sfx": not a.no_sfx and lk.get("sfx", True), "sfx_db": a.sfx_db if a.sfx_db is not None else lk.get("sfx_db", -16.0),
             "jobs": a.jobs, "chromium": a.chromium}
    dsf = a.dsf or (1.25 if composing else 1)
    if composing:
        print("look : %s frame, %s background, camera %s, recorded at %gx pixel density"
              % (copts["frame"], copts["background"], copts["camera"], dsf))

    plan = voices.plan(script, a.script, a.engine, a.voice, a.rate, lex, draft_ok=a.draft_voice)
    finish = a.finish or plan["finish"]
    for line in voices.describe(plan):
        print("voice: " + line)
    if any(s["engine"] in voices.DRAFT for s in plan["speakers"].values()):
        print("  !! DRAFT VOICE: robotic/dated - a timing preview only, never a video to share")
    print("sound: %s finish, tone %s" % (finish, script.get("tone", "explainer")))
    print("app  : %s" % a.url)

    # 0b. voice sample: a few real lines in the real voices, checked by machine, approved by ear, before the long part
    if a.no_gates:
        print("  !! gates off (--no-gates): plan, pronunciation and voice sample are NOT checked - for selftests and timing previews only")
    else:
        import sample
        fp = sample.fingerprint(plan, lex)
        if sample.check_approval(work, fp):
            rep = sample.make(plan, script, lex, work, check=a.voice_check)
            print("\n" + sample.describe(rep))
            if rep["fails"]:
                sys.exit(8)
            print("Listen to %s (a few lines, finished like the final narration). If it sounds right, approve it:\n"
                  "  python scripts/sample.py --work %s --approve\nthen run the same build command again. If not, change the "
                  "voice, speed or lexicon and build again for a new sample." % (rep["wav"], work))
            sys.exit(8)
        print("voice sample approved (%s)" % fp)

    # 1. narration, one WAV per sentence (cached by content so re-runs are quick; chatterbox takes checked by listening)
    wav_dir = os.path.join(work, "wav")
    print("synthesizing narration...")
    res = narrate.voice_all(plan, wav_dir, check=a.voice_check)
    durations, wavs, word_times = res["durations"], res["wavs"], {}
    if res["stopped"]:
        print("\n!! %d sentence(s) never came out right in %d takes:" % (len(res["stopped"]), narrate.TAKES))
        for s in res["stopped"]:
            print("   - " + s)
        if a.voice_check == "auto":
            print("Simplify the sentence (shorter, plainer words, no symbols), pick a calmer mood, or use another voice; "
                  "then build again (good sentences are kept). --voice-check warn keeps the best take anyway.")
            sys.exit(5)
    exact = 0
    if not a.no_highlight:                              # caption words timed against the real clip
        for i, b in enumerate(beats):
            cap = (b.get("cap") or b["say"]).replace("{DATA}", data_line).strip()
            word_times[i] = words.word_timings(cap, wavs[i])
            exact += words.is_exact(cap, wavs[i])
    print("  narration: %.0f s of speech in %d lines" % (sum(durations), len(beats)))
    if not a.no_highlight:
        print("  caption timing: %s" % ("exact for all %d lines (word times from the voice)" % len(beats) if exact == len(beats) else
                                        "exact for %d of %d lines, estimated for the rest (edge reports word times; "
                                        "chatterbox gets them from Whisper)" % (exact, len(beats))))
    speakers = None
    if plan["multi"]:
        speakers = {ln["i"]: [ln["name"], plan["speakers"][ln["who"]]["index"], plan["speakers"][ln["who"]]["color"]]
                    for ln in plan["lines"]}
    if not a.only:                                      # timing rules need the real clip lengths of the whole film
        _, Wt, Nt = lint_storyboard.lint(script, a.script, durations=durations, parts=("timing",))
        for x in Wt:
            print("  warn   " + x)
        for x in Nt:
            print("  note   " + x)

    # 2. check the cards on the real page, then record the screen
    srv = start_server(a.serve_cmd, a.url, a.ready_path)
    url = a.url
    page_opts = dict(chromium=a.chromium, size=(W, H), init_js=rd(a.init_js), config_js=rd(a.config_js), color_scheme=a.scheme,
                     storage_state=a.storage_state,
                     http_credentials=dict(zip(("username", "password"), a.basic_auth.split(":", 1))) if a.basic_auth else None,
                     brand=json.load(open(a.brand, encoding="utf-8")) if a.brand else None,
                     blur={"selectors": [x.strip() for x in a.blur.split(",") if x.strip()], "auto": a.blur_auto} if (a.blur or a.blur_auto) else None)
    try:
        if not a.skip_preflight:
            import preflight
            print("checking title cards and captions...")
            E, Wp, sheet = asyncio.run(preflight.run(script, url, os.path.join(work, "preflight"), **page_opts))
            preflight.report(E, Wp, sheet)
            if E:
                sys.exit(4)
        frames_dir = os.path.join(work, "frames")
        shutil.rmtree(frames_dir, ignore_errors=True)
        print("recording...")
        rec = asyncio.run(record(script, durations, url, frames_dir, sidebar_px=a.sidebar_px, captions=not a.no_captions,
                                 badge=a.badge, data_line=data_line, word_times=word_times, compose=composing, dsf=dsf,
                                 speakers=speakers, **page_opts))
    finally:
        if srv is not None:
            srv.terminate()

    # 3. assemble
    list_path = os.path.join(work, "frames.txt")
    t0, total = frames_to_list(rec, list_path)
    print("recorded %d frames, %.1f s  (first frame is %.2f s before the first beat; expect ~0.3)"
          % (len(rec["frames"]), total, rec["beats"][0]["start"] - t0))
    copts["finish"] = finish
    if composing:                                       # compose.py reuses these, and so does a later recompose
        copts["accent"] = rec["timeline"].get("accent")
        rec["compose_opts"] = {k: v for k, v in copts.items() if k != "chromium"}
    with open(os.path.join(work, "rec.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f)
    write_voice_json(os.path.join(work, "voice.json"), plan, wavs, res["verdicts"])     # for check_narration.py
    narr = os.path.join(work, "narration.wav")
    gains = mix_narration(narr, total + 0.5, [(max(0.0, bt["start"] - t0 + LEAD), wavs[bt["i"]]) for bt in rec["beats"]],
                          [plan["lines"][bt["i"]]["who"] for bt in rec["beats"]])
    if gains:
        print("  voices levelled: " + ", ".join("%s %+.1f dB" % (plan["speakers"][s]["name"], 20 * math.log10(g)) for s, g in gains.items()))
    if composing:
        import compose
        compose.compose(work, a.out, copts)
    else:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                        "-i", narr, "-vf", "fps=%d,format=yuv420p" % FPS, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                        "-af", voices.audio_filter(finish), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                        "-shortest", a.out], check=True)

    # 4. captions + chapters + script
    base = os.path.splitext(a.out)[0]
    with open(base + ".srt", "w", encoding="utf-8") as f, open(base + ".chapters.txt", "w", encoding="utf-8") as c, \
            open(base + ".script.md", "w", encoding="utf-8") as s:
        s.write("# Narration script\n\n")
        for n, bt in enumerate(rec["beats"], 1):
            b = beats[bt["i"]]
            text = (b.get("cap") or b["say"]).replace("{DATA}", data_line).strip()
            who = plan["lines"][bt["i"]]["name"] if plan["multi"] else ""
            start = max(0.0, bt["start"] - t0 + LEAD)
            f.write("%d\n%s --> %s\n%s%s\n\n" % (n, srt_time(start), srt_time(start + durations[bt["i"]]),
                                                 "[%s] " % who if who else "", text))
            if b.get("chapter"):
                m = int(start // 60)
                c.write("%d:%02d %s\n" % (m, int(start % 60), b["chapter"]))
                s.write("\n## %s\n\n" % b["chapter"])
            s.write(("**%s:** " % who if who else "") + text + "\n\n")
    write_description(base + ".description.md", script, rec, beats, durations, plan, a.badge, t0)
    if rec.get("issues"):                               # what the page health probe saw while recording
        print("\n!! the page showed something a viewer should not see - check these beats in the review sheets:")
        for bi, found in rec["issues"].items():
            for kind, detail in found:
                print("   - beat %d: %s: %s" % (int(bi) + 1, kind, detail))
    if rec.get("missing"):
        print("\n!! %d target(s) were not found, so those beats show no spotlight/scroll/click:" % len(rec["missing"]))
        for m in rec["missing"]:
            print("   -", m)
        if not a.allow_missing:
            print("Fix the targets in the storyboard (or pass --allow-missing). The video was still written to %s." % a.out)
    print("\nDone: %s  (%.1f MB, %d:%02d)" % (a.out, os.path.getsize(a.out) / 1e6, total // 60, total % 60))
    print("      %s.srt / .chapters.txt / .script.md / .description.md" % base)
    print("      working files kept in %s" % work)
    if rec.get("missing") and not a.allow_missing:
        sys.exit(3)


if __name__ == "__main__":
    main()
