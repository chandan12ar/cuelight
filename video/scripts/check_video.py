#!/usr/bin/env python3
"""Verify a finished video the way a careful editor would, without needing to listen to it.

    python check_video.py out/video.mp4 [--work WORKDIR] [--script script.json] [--sheet out/contact.jpg] [--frames 24]

Prints duration, resolution, fps, loudness (mean/peak dB) and, with --work, the picture/voice sync
(the first frame must come ~0.2-0.3 s before the first beat). --sheet writes a contact sheet of
evenly spaced frames: LOOK at it (open the image) to confirm every scene, scroll position and
caption is what the storyboard says. Frame grabs are cheap; re-recording is not.

With --work and --script it also checks pacing: words per minute per beat (130-170 is comfortable), very long
sentences, and stretches where the picture does not move for 3 s or more while the voice talks (add a
spotlight, scroll or hover there). Exit code 0 always: these are findings to act on, not failures.

--review DIR (with --work and --script) writes review sheets: one frame per sentence, taken late in the sentence when
its actions have played, labelled with the beat number, time, chapter and the sentence itself, plus anything the
recorder's page check saw (error messages, spinners, an empty page). Six beats per sheet. Open every sheet and check,
for each sentence: is the thing it talks about on screen and highlighted? any error, spinner or empty state? anything
secret? This is the visual half of verification; check_narration.py is the audio half.
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env  # noqa: E402


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _font(size, bold=False):
    from PIL import ImageFont
    names = (["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Helvetica.ttc"] if bold else
             ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc"])
    dirs = ["C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF", "/System/Library/Fonts", "/Library/Fonts"]
    for n in names:
        for d in dirs:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                                  # Pillow < 10.1
        return ImageFont.load_default()


def _wrap(draw, text, font, width):
    lines, line = [], ""
    for w in text.split():
        t = (line + " " + w).strip()
        if draw.textlength(t, font=font) <= width:
            line = t
        else:
            lines.append(line)
            line = w
    return lines + [line] if line else lines


def review(a, dur):
    """Per-sentence review sheets (6 beats each) for Claude, or anyone, to look at."""
    rec = json.load(open(os.path.join(a.work, "rec.json"), encoding="utf-8"))
    beats = json.load(open(a.script, encoding="utf-8"))["beats"]
    t0 = rec["frames"][0][0]
    issues = rec.get("issues") or {}
    os.makedirs(a.review, exist_ok=True)
    items, chapter = [], ""
    for bt in rec["beats"]:
        b = beats[bt["i"]]
        chapter = b.get("chapter") or chapter
        st = bt["start"] - t0
        at = min(st + max(bt["dur"] * 0.75, min(bt["dur"], 1.5)), st + bt["dur"] - 0.25, dur - 0.1)
        img = os.path.join(a.review, "beat%03d.jpg" % (bt["i"] + 1))
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "%.2f" % max(0.0, at), "-i", a.video,
                        "-frames:v", "1", "-vf", "scale=760:-2", "-q:v", "3", img], check=True)
        items.append({"i": bt["i"] + 1, "at": at, "chapter": chapter, "text": (b.get("cap") or b["say"]).strip(),
                      "issues": issues.get(str(bt["i"]), []), "img": img})
    lines = ["# Review sheets", "", "Open every sheet. For each sentence: is the thing it talks about on screen and highlighted?",
             "Any error message, spinner or empty state? Anything secret? Text readable?", ""]
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        Image = None
    sheets = []
    for s in range(0, len(items), 6):
        group = items[s:s + 6]
        name = os.path.join(a.review, "sheet-%02d.jpg" % (len(sheets) + 1))
        if Image:
            tiles = [Image.open(it["img"]).convert("RGB") for it in group]
            tw, th = 760, max(t.height for t in tiles)
            ph = 118
            sheet = Image.new("RGB", (2 * tw + 30, 3 * (th + ph) + 40), (24, 26, 32))
            d = ImageDraw.Draw(sheet)
            fb, fr = _font(19, True), _font(17)
            for n, (it, tile) in enumerate(zip(group, tiles)):
                x, y = 10 + (n % 2) * (tw + 10), 10 + (n // 2) * (th + ph + 10)
                sheet.paste(tile, (x, y))
                d.text((x, y + th + 6), "Beat %d  %d:%02d  %s" % (it["i"], it["at"] // 60, it["at"] % 60, it["chapter"]), font=fb, fill=(255, 255, 255))
                ty = y + th + 32
                for ln in _wrap(d, it["text"], fr, tw - 4)[:2]:
                    d.text((x, ty), ln, font=fr, fill=(200, 204, 214))
                    ty += 22
                if it["issues"]:
                    d.text((x, ty + 2), ("! " + "; ".join("%s: %s" % (k, v) for k, v in it["issues"]))[:95], font=fr, fill=(255, 120, 110))
            sheet.save(name, quality=88)
        sheets.append(name)
        lines.append("## %s" % os.path.basename(name))
        for it in group:
            lines.append("- beat %d (%d:%02d) %s%s" % (it["i"], it["at"] // 60, it["at"] % 60, it["text"],
                                                    "  **! " + "; ".join("%s: %s" % (k, v) for k, v in it["issues"]) + "**" if it["issues"] else ""))
        lines.append("")
    with open(os.path.join(a.review, "review.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\nreview: %d sheet(s) in %s - open each one and check every sentence (review.md lists them)" % (len(sheets), a.review))
    for s in sheets:
        print("  ", s)
    if not Image:
        print("   (Pillow is not installed, so the frames are separate files: beatNNN.jpg)")


def pacing(a, dur):
    import json as _j
    rec = _j.load(open(os.path.join(a.work, "rec.json")))
    beats = _j.load(open(a.script, encoding="utf-8"))["beats"]
    t0 = rec["frames"][0][0]
    starts = [(max(0.0, b["start"] - t0), b["dur"], b["i"]) for b in rec["beats"]]
    clips = {}                                             # the real clip of every sentence, when the build left them
    vp = os.path.join(a.work, "voice.json")
    if os.path.exists(vp):
        import tts as _tts
        for vb in _j.load(open(vp, encoding="utf-8")).get("beats", []):
            if os.path.exists(vb.get("wav", "")):
                clips[vb["i"]] = _tts.duration(vb["wav"])
    print("\npacing")
    issues = 0
    tot_w = tot_s = 0.0
    for st, d, i in starts:
        words = len(beats[i]["say"].split())
        pause = beats[i].get("pause", 0.55)
        speech = max(clips.get(i, d - 0.30 - pause), 0.5)  # beat = clip + 0.30 lead + the pause after it
        wpm = words / speech * 60
        tot_w += words; tot_s += speech
        flag = []
        if wpm > 185 and words >= 8:                       # a short question is said quickly by anyone
            flag.append("fast (%d wpm; comfortable is 140-170: lower --rate or shorten)" % wpm)
        if wpm < 95 and words > 6: flag.append("slow (%d wpm)" % wpm)
        if words > 40: flag.append("long sentence (%d words)" % words)
        if flag:
            issues += 1
            print("  beat %2d @ %s  %s  | %s" % (i + 1, "%d:%02d" % (st // 60, st % 60), ", ".join(flag), beats[i]["say"][:60]))
    print("  overall %d wpm over %d beats" % (tot_w / max(tot_s, 1) * 60, len(starts)))
    vf = "freezedetect=n=-40dB:d=%s" % a.freeze
    if rec.get("compose"):            # judge motion inside the app window, not across the still background around it
        import compose
        W, H = rec["timeline"]["viewport"]
        OW, OH = tuple((rec.get("compose_opts") or {}).get("out_size") or compose.default_out_size(W, H))
        (fx, fy, fw, fh), _, _ = compose.frame_geometry(OW, OH, W, H, (rec.get("compose_opts") or {}).get("frame", "window"), min(OW, OH) / 1080)
        # -55 dB: a sideways table scroll or a chart sweep counts as movement, a still screen does not (measured)
        vf = "crop=%d:%d:%d:%d,freezedetect=n=-55dB:d=%s" % (int(fw) // 2 * 2, int(fh) // 2 * 2, int(fx), int(fy), a.freeze)
    out = run("ffmpeg", "-hide_banner", "-i", a.video, "-vf", vf, "-an", "-f", "null", "-").stderr
    import re as _re
    starts_f = [float(x) for x in _re.findall(r"freeze_start: ([\d.]+)", out)]
    durs = [float(x) for x in _re.findall(r"freeze_duration: ([\d.]+)", out)]
    for fs, fd in zip(starts_f, durs):
        b = next((i for st, d, i in starts if st <= fs < st + d), None)
        if fs > dur - 3:                                    # the held outro card is meant to be still
            continue
        stage_beat = b is not None and any("stage" in x and x["stage"] for x in beats[b].get("do", []))
        if stage_beat or fs < starts[0][0] + 1.0:           # title/problem cards and the opening card are meant to hold
            continue
        issues += 1
        print("  almost no change on screen for %.1f s at %d:%02d (beat %s; cursor movement alone does not count): add a spotlight, scroll or hover" % (fd, fs // 60, fs % 60, (b + 1) if b is not None else "?"))
    print("  pacing: %s" % ("nothing to fix" if not issues else "%d thing(s) to look at" % issues))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--work", help="build_video --work folder (for the sync check)")
    ap.add_argument("--script", help="storyboard JSON (enables the pacing check together with --work)")
    ap.add_argument("--freeze", type=float, default=3.0, help="seconds of unchanged picture to report")
    ap.add_argument("--sheet", help="write a contact sheet JPEG here")
    ap.add_argument("--frames", type=int, default=24, help="frames on the contact sheet")
    ap.add_argument("--at", type=float, nargs="*", help="also save single frames at these seconds next to --sheet")
    ap.add_argument("--review", help="folder for per-sentence review sheets (needs --work and --script)")
    a = ap.parse_args()
    env.safe_console()
    env.require_tools()

    if not os.path.exists(a.video):
        raise SystemExit("no such video: %s (did the build finish?)" % a.video)
    p = json.loads(run("ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
                       "-of", "json", a.video).stdout)
    dur = float(p["format"]["duration"])
    v = next(s for s in p["streams"] if s["codec_type"] == "video")
    print("duration %d:%02d  %sx%s  %s fps  audio=%s" % (dur // 60, dur % 60, v["width"], v["height"], v["r_frame_rate"],
                                                         any(s["codec_type"] == "audio" for s in p["streams"])))
    vol = run("ffmpeg", "-hide_banner", "-i", a.video, "-af", "volumedetect", "-vn", "-f", "null", "-").stderr
    m = re.search(r"mean_volume: (-?[\d.]+) dB", vol); x = re.search(r"max_volume: (-?[\d.]+) dB", vol)
    if m and x:
        mean, peak = float(m.group(1)), float(x.group(1))
        print("loudness mean %.1f dB, peak %.1f dB  %s" % (mean, peak, "OK" if -22 < mean < -12 and peak <= -0.3 else "<- check levels (want mean ~-16, peak <= -1)"))
    if a.work:
        rec = json.load(open(os.path.join(a.work, "rec.json")))
        skew = rec["beats"][0]["start"] - rec["frames"][0][0]
        print("first frame is %.2f s before the first beat  %s" % (skew, "OK" if 0.1 < skew < 0.6 else "<- sync suspect"))
        print("beats: %d" % len(rec["beats"]))
        found = rec.get("issues") or {}
        print("page check: %s" % ("nothing a viewer should not see" if not found else
                                  "; ".join("beat %d %s" % (int(b) + 1, ", ".join(k for k, _ in v)) for b, v in found.items())))
    if a.work and a.script:
        pacing(a, dur)
    if a.review:
        if not (a.work and a.script):
            raise SystemExit("--review needs --work and --script")
        review(a, dur)
    if a.sheet:
        n = a.frames
        cols = 4
        rows = (n + cols - 1) // cols
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", a.video, "-vf",
                        "fps=%f,scale=640:-1,tile=%dx%d" % (n / dur, cols, rows), "-frames:v", "1", "-q:v", "3", a.sheet], check=True)
        print("contact sheet:", a.sheet)
        for t in a.at or []:
            out = "%s.%ds.jpg" % (os.path.splitext(a.sheet)[0], int(t))
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t), "-i", a.video, "-frames:v", "1", "-q:v", "2", out], check=True)
            print("frame:", out)


if __name__ == "__main__":
    main()
