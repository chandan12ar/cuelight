#!/usr/bin/env python3
"""Derive the extra deliverables from a finished video: poster/thumbnail, 30-second teaser, vertical 9:16 cut, GIF.

    python make_extras.py out/demo.mp4 --work <build --work dir> --script video/script.json [--out-dir out/extras]
                          [--only poster teaser vertical gif] [--teaser-len 30] [--gif-at 20 --gif-len 6]

poster    1280x720 JPEG of the intro title card (clean: no caption burned in). Use --poster-at SECONDS to take an app
          frame instead (captions are baked into the picture, so choose a moment between sentences), and
          --poster-title to lay the title on a band (needs Pillow).
teaser    ~30 s: the opening line, one strong beat from several chapters, and the closing card. Whole beats only,
          so no sentence is cut in half; short fades between them. Needs --work (rec.json has beat times).
vertical  1080x1920 for Shorts/Reels/TikTok: the 16:9 picture centred over a blurred copy of itself. Captions stay
          readable but small; for large portrait captions, record again with --size 1080x1920 on a responsive app.
gif       silent looping GIF (palette-optimised, 960 px wide) of a short moment, for READMEs and chat.

--bake-poster  with --poster-at: also write that frame into frame 0 of the video itself. Slack, X, Discord and most
          players show frame 0 as the thumbnail and ignore cover-art metadata, so this is the only reliable way to choose
          it. Only frame 0 changes: same length, same frame count, audio copied. (Without --poster-at frame 0 is already
          the clean intro card, so there is nothing to bake.)
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env  # noqa: E402


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + [str(x) for x in args], check=True)


def beat_times(work):
    rec = json.load(open(os.path.join(work, "rec.json")))
    t0 = rec["frames"][0][0]
    return [(max(0.0, b["start"] - t0), b["dur"], b["i"]) for b in rec["beats"]]


def poster(video, out, beats, script, at, band=False):
    if at is None:
        at = 1.4                                   # the intro card, before the first caption (cards never carry one)
    tmp = out + ".tmp.png"
    ff("-ss", at, "-i", video, "-frames:v", 1, "-vf", "scale=1280:720", tmp)
    title = (script or {}).get("title", "") if band else ""
    try:
        from PIL import Image, ImageDraw, ImageFont
        im = Image.open(tmp).convert("RGB"); d = ImageDraw.Draw(im, "RGBA")
        font = None
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Helvetica.ttc", "C:/Windows/Fonts/segoeuib.ttf"):
            if os.path.exists(p):
                font = ImageFont.truetype(p, 54); break
        if title and font:
            d.rectangle([0, 560, 1280, 720], fill=(14, 16, 22, 215))
            d.text((56, 606), title[:58], fill=(255, 255, 255), font=font)
        im.save(out, quality=92)
    except ImportError:
        os.replace(tmp, out); tmp = None
    if tmp and os.path.exists(tmp):
        os.remove(tmp)
    print("poster  ->", out, "(frame at %.1f s)" % at)


def bake_poster(video, at):
    """Replace only frame 0 with the frame at `at` seconds (full resolution), in place."""
    png = os.path.splitext(video)[0] + ".poster-frame.png"
    tmp = os.path.splitext(video)[0] + ".poster-tmp.mp4"
    ff("-ss", at, "-i", video, "-frames:v", 1, png)
    ff("-i", video, "-i", png, "-filter_complex", "[0:v][1:v]overlay=0:0:enable='eq(n,0)'[v]", "-map", "[v]", "-map", "0:a?",
       "-c:v", "libx264", "-crf", 18, "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", tmp)
    os.replace(tmp, video)
    os.remove(png)
    print("baked   -> frame 0 of %s is now the frame at %.1f s (the thumbnail most platforms show)" % (video, at))


def teaser(video, out, beats, script, total):
    if not beats:
        print("teaser needs --work"); return
    pick = [beats[0]]
    used = pick[0][1]
    mid = [b for b in beats[1:-1] if b[1] <= 8.5 and any(k in a for a in script["beats"][b[2]].get("do", []) for k in ("spot", "scroll", "hscroll", "sweep", "click"))]
    want = max(1, int((total - used - beats[-1][1]) // 6))
    step = max(1, len(mid) // want)
    for b in mid[::step][:want]:
        pick.append(b); used += b[1]
    pick.append(beats[-1])
    fc, labels = [], []
    for n, (st, d, _) in enumerate(pick):
        d = min(d, 9.0); f = 0.35
        fc.append("[0:v]trim=%.3f:%.3f,setpts=PTS-STARTPTS,fade=t=in:d=%.2f,fade=t=out:st=%.3f:d=%.2f[v%d]" % (st, st + d, f, d - f, f, n))
        fc.append("[0:a]atrim=%.3f:%.3f,asetpts=PTS-STARTPTS,afade=t=in:d=%.2f,afade=t=out:st=%.3f:d=%.2f[a%d]" % (st, st + d, f, d - f, f, n))
        labels.append("[v%d][a%d]" % (n, n))
    fc.append("".join(labels) + "concat=n=%d:v=1:a=1[v][a]" % len(pick))
    ff("-i", video, "-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", 20, "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out)
    print("teaser  -> %s (%d beats, about %d s)" % (out, len(pick), sum(min(b[1], 9.0) for b in pick)))


def vertical(video, out):
    # the background is blurred at quarter size and scaled up: it looks the same, and blurring a full 1080x1920 frame
    # 30 times a second took ~4 minutes per minute of video
    ff("-i", video, "-filter_complex",
       "[0:v]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=7:2,scale=1080:1920,eq=brightness=-0.08[bg];"
       "[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v]",
       "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-crf", 21, "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out)
    print("vertical->", out)


def gif(video, out, at, length):
    pal = out + ".pal.png"
    ff("-ss", at, "-t", length, "-i", video, "-vf", "fps=12,scale=960:-1:flags=lanczos,palettegen=max_colors=128", pal)
    ff("-ss", at, "-t", length, "-i", video, "-i", pal, "-filter_complex", "fps=12,scale=960:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4", "-loop", 0, out)
    os.remove(pal)
    print("gif     -> %s (%.1f MB)" % (out, os.path.getsize(out) / 1e6))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--work", help="build_video --work folder (rec.json: beat times) - needed for teaser, helps poster/gif")
    ap.add_argument("--script", help="storyboard JSON")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--only", nargs="*", choices=("poster", "teaser", "vertical", "gif"))
    ap.add_argument("--teaser-len", type=float, default=30.0)
    ap.add_argument("--poster-at", type=float, default=None)
    ap.add_argument("--poster-title", action="store_true", help="lay the storyboard title on a band across the poster (needs Pillow)")
    ap.add_argument("--bake-poster", action="store_true", help="with --poster-at: make that frame the video's frame 0 (its thumbnail everywhere)")
    ap.add_argument("--gif-at", type=float, default=None, help="start second (default: the first spotlight beat)")
    ap.add_argument("--gif-len", type=float, default=6.0)
    a = ap.parse_args()
    env.safe_console()
    env.require_tools()
    out = a.out_dir or os.path.join(os.path.dirname(os.path.abspath(a.video)), "extras")
    os.makedirs(out, exist_ok=True)
    base = os.path.splitext(os.path.basename(a.video))[0]
    script = json.load(open(a.script, encoding="utf-8")) if a.script else {"beats": []}
    beats = beat_times(a.work) if a.work else None
    todo = a.only or ("poster", "teaser", "vertical", "gif")
    if "poster" in todo:
        poster(a.video, os.path.join(out, base + "-poster.jpg"), beats, script, a.poster_at, a.poster_title)
    if a.bake_poster:
        if a.poster_at is None:
            print("bake    skipped: frame 0 is already the intro card; pass --poster-at SECONDS to choose another frame")
        else:
            bake_poster(a.video, a.poster_at)
    if "teaser" in todo:
        if beats and a.script:
            teaser(a.video, os.path.join(out, base + "-teaser.mp4"), beats, script, a.teaser_len)
        else:
            print("teaser skipped: pass --work and --script")
    if "vertical" in todo:
        vertical(a.video, os.path.join(out, base + "-vertical.mp4"))
    if "gif" in todo:
        at = a.gif_at
        if at is None:
            at = next((st for st, d, i in beats or [] if any("spot" in x and x["spot"] for x in script["beats"][i].get("do", []))), 3.0)
        gif(a.video, os.path.join(out, base + ".gif"), at, a.gif_len)


if __name__ == "__main__":
    sys.exit(main())
