#!/usr/bin/env python3
"""The final rendering step: turn the recorded app layer into the finished picture, frame by frame.

    python compose.py --work video/work --out video/out/demo.mp4 [--frame window|browser|none] [--background brand]
                      [--camera auto|manual|off] [--easing spring|smooth] [--out-size 1920x1080] [--url app.acme.com]
                      [--no-captions] [--no-chapter-titles] [--no-progress-bar] [--no-motion-blur] [--jobs 4]

build_video.py records the app clean (cursor, spotlight, callouts and title cards are in it; captions and the badge are
not) and saves a timeline of what happened when (rec.json). This step draws every output frame:
  - the recorded frame inside a rounded window with a soft shadow on a background (or full-bleed while a title card is
    up; the window grows and shrinks as cards come and go), optionally with a browser bar showing the page's address,
  - moved by the camera: auto-zoom into what is clicked, typed or spotlighted, back out for scrolls and tab changes,
    on a spring (quick start, soft settle) with motion blur while it moves fast,
  - with the word-highlighted caption, the chapter title when a chapter starts, a thin chapter progress bar along the
    top, keycaps ("Ctrl + K") when keys are pressed, and the badge, at output resolution.
Chrome draws the pieces that need a browser once (background, shadow, browser bar, each caption in three states:
words to come, words said, word being said); Pillow workers then put every frame together in parallel. Every frame is a
pure function of time, so a run is repeatable, and the frame, background or camera can be changed and rendered again in
a minute without re-recording (run this script again with other flags). The narration (work/narration.wav) is mixed in
at the end. Settings the build used are stored in rec.json and are the defaults here.
"""
import argparse
import asyncio
import bisect
import json
import math
import multiprocessing
import os
import subprocess
import sys
import time
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import env  # noqa: E402

FPS = 30
LEAD = 0.30          # record.py: narration starts this long after its beat
CARD_FADE = 0.7      # overlay.js fades title cards over 0.7 s; the window grows/shrinks over the same time
CAP_FADE = 0.3       # caption fade, as in overlay.js
TITLE_IN, TITLE_HOLD, TITLE_OUT = 0.35, 3.0, 0.3   # chapter title: slide in, stay, fade out (seconds)
KEYS_IN, KEYS_HOLD, KEYS_OUT = 0.16, 1.3, 0.3      # keycaps: pop in, stay, fade out
SHUTTER = 0.5        # motion blur integrates over half a frame interval (a film camera's 180-degree shutter)
CALLOUTS = ("arrow", "circle", "underline", "highlight")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")
KEY_NAMES = {"control": "Ctrl", "controlormeta": "Ctrl", "meta": "Cmd", "alt": "Alt", "shift": "Shift", "enter": "Enter",
             "escape": "Esc", "backspace": "Backspace", "delete": "Del", "tab": "Tab", "space": "Space", " ": "Space",
             "arrowup": "↑", "arrowdown": "↓", "arrowleft": "←", "arrowright": "→",
             "pageup": "PgUp", "pagedown": "PgDn", "home": "Home", "end": "End"}


def ease(k):
    k = min(1.0, max(0.0, k))
    return 4 * k * k * k if k < .5 else 1 - math.pow(-2 * k + 2, 3) / 2


def ease_slow(k):
    k = min(1.0, max(0.0, k))
    return -(math.cos(math.pi * k) - 1) / 2


def spring(k, zeta=0.82):
    """A slightly under-damped spring: starts at once, overshoots about 1 %, settles by the end (Apple-style motion)."""
    if k <= 0:
        return 0.0
    if k >= 1:
        return 1.0
    w = 6.0 / zeta                          # settled to ~0.25 % at k = 1
    wd = w * math.sqrt(1 - zeta * zeta)
    return 1 - math.exp(-zeta * w * k) * (math.cos(wd * k) + zeta / math.sqrt(1 - zeta * zeta) * math.sin(wd * k))


EASINGS = {"cubic": ease, "slow": ease_slow, "spring": spring}


def key_parts(combo):
    """'Control+Shift+K' -> ['Ctrl', 'Shift', 'K'] for keycaps."""
    parts = [p for p in str(combo).replace(" + ", "+").split("+") if p] or [str(combo)]
    return [KEY_NAMES.get(p.lower(), p.upper() if len(p) == 1 else p) for p in parts]


# ---------------------------------------------------------------- colours, background, address bar text

def _rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def mix(a, b, t):
    """Colour `t` of the way from a to b."""
    return _hex(tuple(x + (y - x) * t for x, y in zip(_rgb(a), _rgb(b))))


def rgba(h, alpha):
    r, g, b = _rgb(h)
    return "rgba(%d,%d,%d,%.3f)" % (r, g, b, alpha)


def luminance(h):
    return sum(w * c for w, c in zip((0.2126, 0.7152, 0.0722), _rgb(h))) / 255


# caption colours for the second, third... speaker: distinct hues that all carry white text (contrast >= 3.3)
SPEAKER_PALETTE = ("#2563eb", "#0d9488", "#7c3aed", "#db2777", "#ea580c", "#16a34a")


def _hue(h):
    import colorsys
    return colorsys.rgb_to_hsv(*(c / 255.0 for c in _rgb(h)))[0] * 360


def speaker_colours(caps, accent):
    """{speaker index: (highlight colour, name-label colour)}. The first speaker keeps the accent; every other speaker
    gets its cast "color" or the palette colour furthest in hue from the colours already taken, so who is talking
    reads at a glance. Labels use a lighter tint, readable on the dark caption pill."""
    want = {}
    for c in caps:
        if c.get("speaker") is not None:
            want.setdefault(int(c["speaker"]), c.get("color"))
    out, taken = {}, [accent]
    for idx in sorted(want):
        col = want[idx] or (accent if idx == 0 else None)
        if not col:
            dist = lambda p: min(min(abs(_hue(p) - _hue(t)), 360 - abs(_hue(p) - _hue(t))) for t in taken)
            col = max((p for p in SPEAKER_PALETTE if p not in taken), key=dist)
        taken.append(col)
        out[idx] = (col, mix(col, "#ffffff", 0.45))
    return out


def background_css(spec, accent):
    """(css background, is_dark) for brand | dark | light | #hex | #hex1,#hex2 | path to an image.
    "blur" is drawn per frame from the app itself (see _render_chunk); this CSS is only its fallback and sets the shadow."""
    spec = (spec or "brand").strip()
    if spec == "blur":
        spec = "dark"
    if spec == "brand":
        return ("radial-gradient(1400px 900px at 10% 6%, {g1} 0%, transparent 60%), "
                "radial-gradient(1100px 800px at 95% 100%, {g2} 0%, transparent 58%), linear-gradient(140deg, {a}, {b})").format(
            g1=rgba(mix(accent, "#ffffff", 0.3), 0.55), g2=rgba(mix(accent, "#000000", 0.1), 0.45),
            a=mix(accent, "#0b0d14", 0.45), b=mix(accent, "#0b0d14", 0.82)), True
    if spec == "dark":
        return "radial-gradient(1200px 800px at 80%% 0%%, %s, transparent 60%%), #0e1016" % rgba(accent, 0.22), True
    if spec == "light":
        return "radial-gradient(1200px 800px at 15%% 0%%, %s, transparent 60%%), linear-gradient(160deg, #f5f7fb, #e2e7f0)" % rgba(accent, 0.12), False
    if spec.startswith("#"):
        cols = [c.strip() for c in spec.split(",") if c.strip()]
        css = cols[0] if len(cols) == 1 else "linear-gradient(135deg, %s)" % ", ".join(cols)
        return css, sum(luminance(c) for c in cols) / len(cols) < 0.5
    if os.path.exists(spec):
        from pathlib import Path
        return "#111 url('%s') center/cover no-repeat" % Path(spec).resolve().as_uri(), True
    raise SystemExit("unknown background %r: use brand, dark, light, blur, #hex, #hex1,#hex2 or an image file" % spec)


def display_url(url, override=None, fallback_host=None):
    """What the browser bar shows: the host and path, never the query string (it can hold tokens)."""
    try:
        p = urlsplit(url or "")
    except ValueError:
        return override or ""
    if p.scheme in ("data", "about", "file", ""):
        return override or fallback_host or ""
    host = override or (fallback_host if p.netloc.split(":")[0] in LOCAL_HOSTS and fallback_host else p.netloc)
    path = p.path if p.path not in ("", "/") else ""
    return (host + path)[:90]


# ---------------------------------------------------------------- the camera

def _clamp_view(z, cx, cy, W, H):
    vw, vh = W / z, H / z
    return (min(max(cx - vw / 2, 0.0), W - vw), min(max(cy - vh / 2, 0.0), H - vh), vw)


def _fit(rect, W, H, zmax, bias=0.0):
    """Zoom that frames `rect` with breathing room, or None when zooming would barely help."""
    l, t, r, b = rect["l"], rect["t"], rect["r"], rect["b"]
    w, h = max(r - l, 1.0), max(b - t, 1.0)
    mx, my = max(48.0, 0.35 * h, 0.12 * w), max(48.0, 0.6 * h)
    z = min(zmax, W / (w + 2 * mx), H / (h + 2 * my))
    if z < 1.12:
        return None
    return (z, (l + r) / 2, (t + b) / 2 + bias * (H / z))


def plan_camera(events, W, H, mode="auto", max_zoom=1.8, easing="spring"):
    """Camera moves from the recorded actions: [(t, dur, easing, left, top, vw)] in viewport CSS pixels.

    auto: zoom into what is typed (field kept in the upper part so results below stay visible), clicked or spotlighted
    when it is small; a slow push-in on big spotlighted areas; back to the full view for scrolls, tab changes, chart
    sweeps and title cards; no move when the next target is already well inside the current view (no jitter).
    manual: only actions with an explicit "zoom". off: never moves. "zoom": false on an action keeps the full view."""
    over = (0.0, 0.0, float(W))
    keys, cur = [], over
    if mode == "off":
        return keys
    move = "spring" if easing == "spring" else "cubic"

    def add(t, view, dur=None, easing=move):
        nonlocal cur
        if all(abs(a - b) < 0.5 for a, b in zip(view, cur)):
            return
        if dur is None:
            dur = 0.9 if view[2] < cur[2] - 1 else (0.8 if view[2] > cur[2] + 1 else 0.7)
        keys.append((t, dur, easing) + tuple(view))
        cur = view

    def steady(z, cx, cy):          # already zoomed and the new target sits well inside the current view
        if cur[2] >= W - 1:
            return False
        z0 = W / cur[2]
        ccx, ccy = cur[0] + cur[2] / 2, cur[1] + cur[2] * H / W / 2
        return abs(z - z0) < 0.2 and abs(cx - ccx) < 0.3 * cur[2] and abs(cy - ccy) < 0.3 * cur[2] * H / W

    for e in sorted(events, key=lambda e: e.get("t0", e["t"])):
        if e["kind"] == "stage":
            add(e["t"], over)
            continue
        if e["kind"] != "act":
            continue
        what, rect, zoom, t0 = e["what"], e.get("rect"), e.get("zoom"), e["t0"]
        if e.get("on_card") or what == "stage" or zoom is False:
            add(t0, over)
            continue
        if what in ("sweep", "hscroll") and cur[2] >= W / 1.15:
            continue                                # a gentle push-in can stay while a chart is swept or a table slides
        if what in ("scroll", "scroll_y", "scroll_sheet", "hscroll", "tab", "sweep"):
            add(t0, over)
            continue
        if not rect:
            continue
        cx, cy = (rect["l"] + rect["r"]) / 2, (rect["t"] + rect["b"]) / 2
        if isinstance(zoom, (int, float)) and not isinstance(zoom, bool) and zoom >= 1:
            add(t0, _clamp_view(float(zoom), cx, cy, W, H) if zoom > 1.001 else over)
            continue
        if mode != "auto":
            continue
        fit, dur, easing = None, None, move
        if what == "type":
            fit = _fit(rect, W, H, min(max_zoom, 1.6), bias=0.18)
        elif what == "click":
            fit = _fit(rect, W, H, min(max_zoom, 1.35))
        elif what in ("spot",) + CALLOUTS:
            area = (rect["r"] - rect["l"]) * (rect["b"] - rect["t"])
            if area < 0.3 * W * H:
                fit = _fit(rect, W, H, min(max_zoom, 1.6))
            if fit is None:                         # big area: a slow push-in keeps the picture alive
                fit, dur, easing = (1.06, cx, cy), 3.2, "slow"
        else:
            continue
        if fit is None:
            add(t0, over)
            continue
        if steady(*fit):
            continue
        add(t0, _clamp_view(fit[0], fit[1], fit[2], W, H), dur, easing)
    # a zoom that would be undone within 1.6 s cannot be read - it is only a jolt: drop it, then drop moves that no
    # longer change anything
    kept, view = [], over
    for i, k in enumerate(keys):
        nxt = keys[i + 1] if i + 1 < len(keys) else None
        if k[5] < W * 0.95 and k[2] != "slow" and nxt is not None and nxt[0] - k[0] < 1.6:
            continue
        if all(abs(a - b) < 0.5 for a, b in zip(k[3:], view)):
            continue
        kept.append(k)
        view = k[3:]
    return kept


class Camera:
    """Camera position at any time: each move starts from wherever the previous one had got to, the view width is
    interpolated geometrically (a real zoom feel) and the view always stays inside the page (also during a spring's
    small overshoot)."""

    def __init__(self, keys, W, H):
        self.keys, self.starts, self.W, self.H = keys, [], W, H
        state = (0.0, 0.0, float(W))
        for i, k in enumerate(keys):
            if i:
                state = self._at(i - 1, k[0])
            self.starts.append(state)
        self.times = [k[0] for k in keys]

    def _at(self, i, t):
        t0, dur, easing, l1, t1, w1 = self.keys[i]
        l0, tp0, w0 = self.starts[i]
        e = EASINGS.get(easing, ease)((t - t0) / dur)
        vw = min(float(self.W), max(self.W / 3.2, math.exp(math.log(w0) + (math.log(w1) - math.log(w0)) * e)))
        vh = vw * self.H / self.W
        return (min(max(l0 + (l1 - l0) * e, 0.0), self.W - vw), min(max(tp0 + (t1 - tp0) * e, 0.0), self.H - vh), vw)

    def at(self, t):
        i = bisect.bisect_right(self.times, t) - 1
        return self._at(i, t) if i >= 0 else (0.0, 0.0, float(self.W))


# ---------------------------------------------------------------- the timeline

class Timeline:
    def __init__(self, rec, opts):
        tl = rec["timeline"]
        self.W, self.H = tl["viewport"]
        self.frames = ["frames/" + os.path.basename(f[1]) for f in rec["frames"]]
        self.ts = [f[0] for f in rec["frames"]]
        self.t0, self.t_end = self.ts[0], rec["t_end"]
        self.caps = sorted(tl["captions"], key=lambda c: c["t"])
        self.cap_t = [c["t"] for c in self.caps]
        ev = sorted(tl["events"], key=lambda e: e["t"])
        self.badge_ev = [(e["t"], e["text"]) for e in ev if e["kind"] == "badge"]
        self.url_ev = [(e["t"], e["url"]) for e in ev if e["kind"] == "url"]
        self.cards, self.card_t = [], []    # (t, framed-progress at t, target): 0 = card full-bleed, 1 = framed app
        for e in (e for e in ev if e["kind"] == "stage"):
            m = 1.0
            if self.cards:                  # a card change starts from wherever the previous transition had got to
                te, m0, target = self.cards[-1]
                m = m0 + (target - m0) * ease((e["t"] - te) / CARD_FADE)
            self.cards.append((e["t"], m, 0.0 if e["name"] else 1.0))
            self.card_t.append(e["t"])
        self.camera = Camera(plan_camera(ev, self.W, self.H, opts.get("camera", "auto"), opts.get("max_zoom", 1.8),
                                         opts.get("easing", "spring")), self.W, self.H)
        self.n = max(1, int(round((self.t_end - self.t0) * FPS)))
        self.key_ev = [(e["t"], e["combo"]) for e in ev if e["kind"] == "keys"]
        self.key_t = [k[0] for k in self.key_ev]
        # chapters: (start, name) from the beats that open one; the title shows once the app (not a card) is on screen
        self.chapters = [(c["t"], c["chapter"]) for c in self.caps if c.get("chapter")]
        self.chap_t = [c[0] for c in self.chapters]
        self.titles = []                    # (show, hide, chapter index)
        for ci, (tc, _) in enumerate(self.chapters):
            end = self.chapters[ci + 1][0] if ci + 1 < len(self.chapters) else self.t_end
            t = tc
            while t < end and self.card(t) < 0.98:
                t += 1.0 / FPS
            if t >= end - TITLE_IN:
                continue                    # the chapter is all title card: the card already names it
            hide = min(t + TITLE_IN + TITLE_HOLD, end - 0.1)
            u = t
            while u < hide and self.card(u) >= 0.98:
                u += 1.0 / FPS
            if min(hide, u) - t >= 1.2:                    # a title that cannot stay long enough to read would just flash
                self.titles.append((t, min(hide, u), ci))

    def chapter_title(self, t):
        """(chapter index, opacity, slide offset 0..1) while a chapter title is up, else None."""
        for show, hide, ci in self.titles:
            if show <= t < hide:
                k_in, k_out = (t - show) / TITLE_IN, (hide - t) / TITLE_OUT
                return (ci, round(ease(min(k_in, k_out, 1.0)), 2), round(1 - ease(min(k_in, 1.0)), 2))
        return None

    def progress(self, t):
        """(current chapter, its share already narrated in 1/8 steps): the bar moves sentence by sentence, so a still
        screen stays a repeatable frame."""
        ci = bisect.bisect_right(self.chap_t, t) - 1
        if ci < 0:
            return None
        start = self.chap_t[ci]
        end = self.chap_t[ci + 1] if ci + 1 < len(self.chapters) else self.t_end
        inside = [c["t"] for c in self.caps if start <= c["t"] < end]
        done = sum(1 for x in inside if x <= t)
        return (ci, round(done / max(len(inside), 1) * 8) / 8.0)

    def keys(self, t):
        """(key index, opacity, scale) while keycaps are up, else None."""
        i = bisect.bisect_right(self.key_t, t) - 1
        if i < 0:
            return None
        e = t - self.key_t[i]
        total = KEYS_IN + KEYS_HOLD + KEYS_OUT
        if e >= total:
            return None
        k_in = e / KEYS_IN
        op = min(1.0, k_in, (total - e) / KEYS_OUT)
        return (i, round(op, 2), round(0.88 + 0.12 * ease(min(k_in, 1.0)), 3))

    def card(self, t):
        i = bisect.bisect_right(self.card_t, t) - 1
        if i < 0:
            return 1.0
        te, m0, target = self.cards[i]
        return m0 + (target - m0) * ease((t - te) / CARD_FADE)

    def frame(self, t):
        return max(0, bisect.bisect_right(self.ts, t) - 1)

    def caption(self, t):
        """(caption index, word being said or -1, words already said, opacity) or None."""
        i = bisect.bisect_right(self.cap_t, t) - 1
        if i < 0:
            return None
        c = self.caps[i]
        prev_shown = i > 0 and self.caps[i - 1]["show"] and self.caps[i - 1]["text"]
        if not c["show"] or not c["text"]:
            k = (t - c["t"]) / CAP_FADE
            if prev_shown and k < 1:        # the previous caption fades out, fully said
                n = len(self.caps[i - 1]["text"].split())
                return (i - 1, -1, n, round(1 - ease(k), 2))
            return None
        words, wt = c["text"].split(), c.get("words")
        cur, done = -1, len(words)
        if wt and len(wt) == len(words):
            e = t - c["t"] - LEAD
            if e < wt[-1][1]:
                cur = bisect.bisect_right([w[0] for w in wt], e) - 1
                done = max(cur, 0)
        return (i, cur, done, 1.0 if prev_shown else round(ease((t - c["t"]) / CAP_FADE), 2))

    def _last(self, evs, t, default):
        v = default
        for te, x in evs:
            if te <= t:
                v = x
        return v

    def badge(self, t):
        return self._last(self.badge_ev, t, None)

    def url(self, t):
        return self._last(self.url_ev, t, "")


def frame_geometry(OW, OH, W, H, frame, ui):
    """Content rectangle of the framed window (x, y, w, h), browser-bar height and corner radius."""
    if frame == "none":
        return (0.0, 0.0, float(OW), float(OH)), 0.0, 0.0
    bar = round(40.0 * ui) if frame == "browser" else 0.0
    total = OH * (0.86 if frame == "window" else 0.88)
    ch = total - bar
    cw = ch * W / H
    if cw > OW * 0.9:
        cw = OW * 0.9
        ch = cw * H / W
        total = ch + bar
    return ((OW - cw) / 2, (OH - total) / 2 + bar, cw, ch), bar, 14.0 * ui


# ---------------------------------------------------------------- pieces drawn once by Chrome

ASSETS = r"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:%(OW)dpx;height:%(OH)dpx;overflow:hidden;background:transparent}
#bg{position:absolute;inset:0;background:%(BG)s;display:none}
#shadow{position:absolute;background:transparent;display:none}
#bar{position:absolute;left:0;top:0;overflow:hidden;background:linear-gradient(#f7f8fa,#eceef2);border-bottom:1px solid rgba(0,0,0,.08);box-sizing:border-box;display:none}
#bar .dots{position:absolute;left:%(DOTX)dpx;top:50%%;transform:translateY(-50%%);display:flex;gap:%(DOTG)dpx}
#bar .dots i{width:%(DOT)dpx;height:%(DOT)dpx;border-radius:50%%;display:block}
#url{position:absolute;left:50%%;top:50%%;transform:translate(-50%%,-50%%);width:46%%;height:%(URLH)dpx;border-radius:%(URLR)dpx;background:#fff;border:1px solid rgba(0,0,0,.09);
  font:500 %(URLF)dpx system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#4b5363;display:flex;align-items:center;justify-content:center;white-space:nowrap;overflow:hidden}
#cap{font-family:system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;position:absolute;left:%(M)dpx;top:%(M)dpx;max-width:%(CAPW)dpx;width:max-content;
  padding:%(CAPPY)dpx %(CAPPX)dpx;border-radius:%(CAPR)dpx;background:rgba(14,16,22,.82);color:#fff;font-size:%(CAPF)dpx;line-height:1.4;text-align:center;font-weight:500;
  box-shadow:0 %(CAPS)dpx %(CAPS4)dpx rgba(0,0,0,.35);display:none}
#cap .vw{opacity:.62;padding:1px %(WP)dpx;border-radius:%(WR)dpx;margin:0 -1px}
#cap.d .vw{opacity:1}
#cap.c .vw{opacity:1;background:var(--hl,%(ACC)s);color:#fff}
#cap .who{display:block;font-size:%(WHOF)dpx;font-weight:700;letter-spacing:.07em;text-transform:uppercase;line-height:1.25;margin:0 0 %(WHOM)dpx;color:var(--lab,#fff)}
#cap .who i{display:inline-block;width:%(WHOD)dpx;height:%(WHOD)dpx;border-radius:50%%;background:var(--hl,%(ACC)s);margin-right:%(WHOG)dpx;vertical-align:middle;position:relative;top:-1px}
#badge{position:absolute;left:%(M)dpx;top:%(M)dpx;padding:%(BPY)dpx %(BPX)dpx;border-radius:999px;background:rgba(14,16,22,.8);color:#fff;
  font:600 %(BF)dpx system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;letter-spacing:.03em;display:none}
#chap{position:absolute;left:%(M)dpx;top:%(M)dpx;display:none;align-items:center;gap:%(CHG)dpx;padding:%(CHPY)dpx %(CHPX)dpx %(CHPY)dpx %(CHPL)dpx;
  border-radius:%(CHR)dpx;background:rgba(14,16,22,.8);color:#fff;font:650 %(CHF)dpx system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  letter-spacing:.01em;box-shadow:0 %(CHS)dpx %(CHS4)dpx rgba(0,0,0,.3);white-space:nowrap}
#chap i{display:block;width:%(CHBW)dpx;height:%(CHBH)dpx;border-radius:%(CHBW)dpx;background:%(ACC)s}
#keys{position:absolute;left:%(M)dpx;top:%(M)dpx;display:none;align-items:center;gap:%(KG)dpx;padding:%(KPY)dpx %(KPX)dpx;border-radius:%(KR)dpx;
  background:rgba(14,16,22,.84);box-shadow:0 %(CHS)dpx %(CHS4)dpx rgba(0,0,0,.35);color:#fff;font:600 %(KF)dpx system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
#keys kbd{display:inline-block;min-width:%(KMIN)dpx;padding:%(KBPY)dpx %(KBPX)dpx;border-radius:%(KBR)dpx;background:linear-gradient(#fdfdfe,#e6e9ee);color:#1d2230;
  font:650 %(KF)dpx system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;text-align:center;box-shadow:0 %(KBS)dpx 0 #aab0bb,0 %(KBS2)dpx %(KBS4)dpx rgba(0,0,0,.3)}
#keys span{opacity:.75}
</style></head><body>
<div id="bg"></div><div id="shadow"></div>
<div id="bar"><div class="dots"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i></div><div id="url"></div></div>
<div id="cap"></div><div id="badge"></div><div id="chap"><i></i><span id="chapt"></span></div><div id="keys"></div>
<script>
const $ = id => document.getElementById(id);
const only = id => ['bg', 'shadow', 'bar', 'cap', 'badge', 'chap', 'keys'].forEach(x => $(x).style.display = x === id ? (x === 'chap' || x === 'keys' ? 'flex' : 'block') : 'none');
window.A = {
  bg() { only('bg'); },
  shadow(x, y, w, h, r, sh) { only('shadow'); Object.assign($('shadow').style, {left: x + 'px', top: y + 'px', width: w + 'px', height: h + 'px', borderRadius: r + 'px', boxShadow: sh}); },
  bar(w, h, text) { only('bar'); Object.assign($('bar').style, {width: w + 'px', height: h + 'px'}); $('url').textContent = text; },
  cap(words, cls, who, hl, lab) {   // the caption box and every word's box, so the three versions can be mixed per frame;
    only('cap'); const c = $('cap');  // `who`: the speaker's name above the words when several voices talk
    const esc = t => t.replace(/&/g, '&amp;').replace(/</g, '&lt;');
    const sig = (who || '') + '|' + words.join(' ');
    if (c.dataset.t !== sig) {
      c.innerHTML = (who ? '<span class="who"><i></i>' + esc(who) + '</span>' : '') + words.map(t => '<span class="vw">' + esc(t) + '</span>').join(' ');
      c.dataset.t = sig;
    }
    if (hl) c.style.setProperty('--hl', hl); else c.style.removeProperty('--hl');
    if (lab) c.style.setProperty('--lab', lab); else c.style.removeProperty('--lab');
    c.className = cls;
    const b = c.getBoundingClientRect();
    return {box: [b.left, b.top, b.width, b.height], words: [...c.querySelectorAll('.vw')].map(s => { const q = s.getBoundingClientRect(); return [q.left, q.top, q.right, q.bottom]; })};
  },
  badge(text) { only('badge'); $('badge').textContent = text; const b = $('badge').getBoundingClientRect(); return [b.left, b.top, b.width, b.height]; },
  chap(text) { only('chap'); $('chapt').textContent = text; const b = $('chap').getBoundingClientRect(); return [b.left, b.top, b.width, b.height]; },
  keys(parts) {
    only('keys');
    $('keys').innerHTML = parts.map(p => '<kbd>' + p.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</kbd>').join('<span>+</span>');
    const b = $('keys').getBoundingClientRect(); return [b.left, b.top, b.width, b.height];
  }
};
</script></body></html>"""


async def _draw_assets(work, adir, g, tl, accent, bg_css, bg_dark, chromium):
    """Background, window shadow, browser bars, caption versions and badges as PNGs in `adir`; returns their index."""
    from pathlib import Path
    from playwright.async_api import async_playwright
    OW, OH = g["out"]
    f, ui = g["f"], g["ui"]
    M = int(56 * f)                                        # room around a caption for its soft shadow
    html = ASSETS % dict(OW=OW, OH=OH, BG=bg_css, ACC=accent, M=M,
                         DOTX=int(16 * ui), DOTG=int(8 * ui), DOT=int(12 * ui), URLH=int(24 * ui), URLR=int(7 * ui), URLF=int(13 * ui),
                         CAPW=int(1040 * f), CAPPY=int(13 * f), CAPPX=int(26 * f), CAPR=int(16 * f), CAPF=int(21 * f),
                         CAPS=int(10 * f), CAPS4=int(40 * f), WP=max(2, int(3 * f)), WR=int(7 * f),
                         WHOF=int(13 * f), WHOM=int(4 * f), WHOD=max(5, int(8 * f)), WHOG=int(7 * f),
                         BPY=int(5 * f), BPX=int(12 * f), BF=int(13 * f),
                         CHG=int(10 * f), CHPY=int(7 * f), CHPX=int(15 * f), CHPL=int(12 * f), CHR=int(10 * f), CHF=int(16 * f),
                         CHS=int(6 * f), CHS4=int(24 * f), CHBW=max(3, int(4 * f)), CHBH=int(17 * f),
                         KG=int(7 * f), KPY=int(8 * f), KPX=int(11 * f), KR=int(13 * f), KF=int(17 * f), KMIN=int(30 * f),
                         KBPY=int(5 * f), KBPX=int(11 * f), KBR=int(7 * f), KBS=max(2, int(3 * f)), KBS2=int(4 * f), KBS4=int(10 * f))
    page_path = os.path.join(adir, "assets.html")
    with open(page_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    index = {"caps": {}, "bars": {}, "badges": {}, "chapters": {}, "keys": {}, "M": M}
    launch = {"headless": True, "args": ["--hide-scrollbars", "--force-device-scale-factor=1"]}
    if chromium:
        launch["executable_path"] = chromium
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch)
        page = await (await browser.new_context(viewport={"width": OW, "height": OH}, device_scale_factor=1)).new_page()
        await page.goto(Path(page_path).resolve().as_uri())
        await page.evaluate("() => document.fonts.ready.then(() => true)")
        await page.evaluate("() => A.bg()")
        await page.screenshot(path=os.path.join(adir, "bg.png"))
        (x, y, w, h), bar, r = g["framed"], g["bar"], g["radius"]
        if g["frame"] != "none":
            sh = "0 %dpx %dpx %s, 0 0 0 1px rgba(255,255,255,%.2f)" % (int(24 * ui), int(70 * ui), "rgba(0,0,0,.55)" if bg_dark else "rgba(15,23,42,.28)",
                                                                     0.07 if bg_dark else 0.0)
            await page.evaluate("a => A.shadow(...a)", [x, y - bar, w, h + bar, r, sh])
            await page.screenshot(path=os.path.join(adir, "shadow.png"), omit_background=True)
        if g["frame"] == "browser":
            for u in sorted({display_url(u, g.get("url_override"), g.get("url_fallback")) for _, u in tl.url_ev} | {""}):
                name = "bar%02d.png" % len(index["bars"])
                await page.evaluate("a => A.bar(...a)", [round(w), int(bar), u])
                await page.screenshot(path=os.path.join(adir, name), clip={"x": 0, "y": 0, "width": round(w), "height": int(bar)})
                index["bars"][u] = name
        if g["captions"]:
            cols = g.get("speaker_colours") or {}
            for i, c in enumerate(tl.caps):
                if not (c["show"] and c["text"]):
                    continue
                words = c["text"].split()
                rects = None
                hl, lab = cols.get(c.get("speaker"), (None, None)) if c.get("who") else (None, None)
                for cls in ("", "d", "c"):                # words to come / words said / every word highlighted
                    m = await page.evaluate("a => A.cap(...a)", [words, cls, c.get("who") or "", hl or "", lab or ""])
                    bx, by, bw, bh = m["box"]
                    clip = {"x": bx - M, "y": by - M, "width": bw + 2 * M, "height": bh + 2 * M}
                    await page.screenshot(path=os.path.join(adir, "cap%03d%s.png" % (i, cls or "p")), clip=clip, omit_background=True)
                    rects = rects or [[q[0] - clip["x"], q[1] - clip["y"], q[2] - clip["x"], q[3] - clip["y"]] for q in m["words"]]
                index["caps"][str(i)] = {"w": bw, "h": bh, "words": rects}
        for _, txt in tl.badge_ev:
            if txt and txt not in index["badges"]:
                bx, by, bw, bh = await page.evaluate("t => A.badge(t)", txt)
                name = "badge%02d.png" % len(index["badges"])
                await page.screenshot(path=os.path.join(adir, name), clip={"x": bx - 2, "y": by - 2, "width": bw + 4, "height": bh + 4}, omit_background=True)
                index["badges"][txt] = {"file": name, "w": bw, "h": bh}
        ms = int(30 * f)                                   # room for the pills' soft shadows
        if g["chapter_titles"]:
            for _, _, ci in tl.titles:
                bx, by, bw, bh = await page.evaluate("t => A.chap(t)", tl.chapters[ci][1])
                name = "chap%02d.png" % ci
                await page.screenshot(path=os.path.join(adir, name), clip={"x": bx - ms, "y": by - ms, "width": bw + 2 * ms, "height": bh + 2 * ms},
                                      omit_background=True)
                index["chapters"][str(ci)] = {"file": name, "w": bw, "h": bh, "m": ms}
        for _, combo in tl.key_ev:
            if combo not in index["keys"]:
                bx, by, bw, bh = await page.evaluate("p => A.keys(p)", key_parts(combo))
                name = "keys%02d.png" % len(index["keys"])
                await page.screenshot(path=os.path.join(adir, name), clip={"x": bx - ms, "y": by - ms, "width": bw + 2 * ms, "height": bh + 2 * ms},
                                      omit_background=True)
                index["keys"][combo] = {"file": name, "w": bw, "h": bh, "m": ms}
        await browser.close()
    return index


# ---------------------------------------------------------------- per-frame state and the Pillow workers

def state_at(tl, k, g, bars):
    """Output frame k as a small hashable tuple: equal tuples are equal pictures, so a repeat costs nothing.
    (source frame, card progress, camera view(s), caption, badge, browser bar, chapter title, progress, keycaps).
    While the camera moves fast the view is sampled several times across the shutter: the worker averages them (motion blur)."""
    t = tl.t0 + k / FPS
    m = round(tl.card(t), 3)
    left, top, vw = tl.camera.at(t)
    views = ((round(left, 2), round(top, 2), round(vw, 2)),)
    if g["motion_blur"] and m > 0.001:
        h = SHUTTER / (2.0 * FPS)
        a, b = tl.camera.at(t - h), tl.camera.at(t + h)
        px = g["framed"][2] / max(vw, 1.0)                 # output pixels per page pixel at this zoom
        moved = max(abs(b[0] - a[0]), abs(b[0] + b[2] - a[0] - a[2]), abs(b[1] - a[1]) * 1.0) * px
        if moved > 1.5:                                    # a sample every ~1.5 px of movement, or it shows as ghost copies
            n = min(16, max(3, int(math.ceil(moved / 1.5))))
            views = tuple(tuple(round(v, 2) for v in tl.camera.at(t - h + 2 * h * i / (n - 1))) for i in range(n))
    url = display_url(tl.url(t), g.get("url_override"), g.get("url_fallback")) if g["frame"] == "browser" else ""
    prog = tl.progress(t) if g["progress_bar"] and tl.chapters else None
    return (tl.frame(t), m, views, tl.caption(t) if g["captions"] else None, tl.badge(t), bars.get(url) if bars else None,
            tl.chapter_title(t) if g["chapter_titles"] else None, (prog + (round(m, 2),)) if prog and m > 0.01 else None,
            (lambda kk: (tl.key_ev[kk[0]][1],) + kk[1:] if kk else None)(tl.keys(t)))


def _rounded_mask(w, h, r, cache):
    from PIL import Image, ImageDraw
    key = (w, h, int(r))
    if key not in cache:
        if r < 0.5:
            cache[key] = None
        else:
            s = 4                                          # draw 4x and shrink: smooth, anti-aliased corners
            big = Image.new("L", (w * s, h * s), 0)
            ImageDraw.Draw(big).rounded_rectangle([0, 0, w * s - 1, h * s - 1], radius=int(r * s), fill=255)
            cache[key] = big.resize((w, h), Image.LANCZOS)
        if len(cache) > 64:
            cache.pop(next(iter(cache)))
    return cache[key]


def _render_chunk(job):
    """Worker process: draw frames job["states"] and stream them into ffmpeg as raw RGB."""
    from PIL import Image
    env.ensure_tools()
    adir, work, g, idx = job["adir"], job["work"], job["g"], job["index"]
    OW, OH = g["out"]
    W, H = g["viewport"]
    (fx, fy, fw, fh), bar, rad = g["framed"], g["bar"], g["radius"]
    bg = Image.open(os.path.join(adir, "bg.png")).convert("RGB")
    bg_framed, shadow = bg.copy(), None
    if g["frame"] != "none":
        shadow = Image.open(os.path.join(adir, "shadow.png")).convert("RGBA")
        bg_framed = Image.alpha_composite(bg.convert("RGBA"), shadow).convert("RGB")
    blur_cache = {}
    bars = {name: Image.open(os.path.join(adir, name)).convert("RGB") for name in set(idx["bars"].values())}
    badges = {t: (Image.open(os.path.join(adir, b["file"])).convert("RGBA"), b) for t, b in idx["badges"].items()}
    chapters = {int(c): (Image.open(os.path.join(adir, m["file"])).convert("RGBA"), m) for c, m in idx["chapters"].items()}
    keycaps = {k: (Image.open(os.path.join(adir, m["file"])).convert("RGBA"), m) for k, m in idx["keys"].items()}
    caps, masks, src_cache, cap_cache = {}, {}, {}, {}
    ui, f = g["ui"], g["f"]
    hb, gap = max(3, int(round(4 * ui))), max(2, int(round(3 * ui)))     # progress bar height, gap between chapters
    acc = tuple(int(g["accent"][i:i + 2], 16) for i in (1, 3, 5))
    track = (255, 255, 255, 70) if g["bg_dark"] else (15, 23, 42, 50)

    def faded(im, op):
        if op >= 1:
            return im
        im = im.copy()
        im.putalpha(im.getchannel("A").point(lambda v: int(v * op)))
        return im

    def progress_strip(ci, fill, op):
        from PIL import ImageDraw
        strip = Image.new("RGBA", (OW, hb), (0, 0, 0, 0))
        d = ImageDraw.Draw(strip)
        for i, (a, b) in enumerate(g["chap_spans"]):
            x0, x1 = a * OW + gap / 2, b * OW - gap / 2
            if x1 - x0 < 2:
                continue
            d.rectangle([x0, 0, x1, hb - 1], fill=track)
            done = 1.0 if i < ci else (fill if i == ci else 0.0)
            if done > 0:
                d.rectangle([x0, 0, x0 + (x1 - x0) * done, hb - 1], fill=acc + (255,))
        return faded(strip, op)

    def cap_image(i, cur, done, op):
        key = (i, cur, done, op)
        if key in cap_cache:
            return cap_cache[key]
        if i not in caps:
            caps[i] = [Image.open(os.path.join(adir, "cap%03d%s.png" % (i, v))).convert("RGBA") for v in ("p", "d", "c")]
            if len(caps) > 6:
                caps.pop(next(iter(caps)))
        pend, said, hl = caps[i]
        meta = idx["caps"][str(i)]
        im = pend.copy()
        for j, (l, t, r, b) in enumerate(meta["words"]):
            src = hl if j == cur else (said if j < done else None)
            if src is not None:
                box = (int(l) - 1, int(t) - 1, int(math.ceil(r)) + 1, int(math.ceil(b)) + 1)
                im.paste(src.crop(box), box[:2])
        if op < 1:
            a = im.getchannel("A").point(lambda v: int(v * op))
            im.putalpha(a)
        if len(cap_cache) > 200:
            cap_cache.clear()
        cap_cache[key] = im
        return im

    def source(i):
        if i not in src_cache:
            src_cache.clear()
            src_cache[i] = Image.open(os.path.join(work, job["frames"][i])).convert("RGB")
        return src_cache[i]

    def blurred_backdrop(i, src):
        """background "blur": the app itself, heavily blurred and dimmed, behind the window (and its shadow)."""
        if i not in blur_cache:
            blur_cache.clear()
            from PIL import ImageFilter
            small = src.resize((max(2, OW // 8), max(2, OH // 8)), Image.BILINEAR).filter(ImageFilter.GaussianBlur(5))
            small = Image.blend(small, Image.new("RGB", small.size, (10, 12, 18)), 0.5)
            im = small.resize((OW, OH), Image.BICUBIC)
            if shadow is not None:
                im = Image.alpha_composite(im.convert("RGBA"), shadow).convert("RGB")
            blur_cache[i] = im
        return blur_cache[i]

    def content(src, views, size):
        """The app as the camera sees it, scaled to `size`; several views (fast camera move) are averaged: motion blur.
        Blurred frames are averaged at half size and scaled up: they hold no fine detail anyway, and it is 4x faster."""
        ppc = src.width / W
        work_size = size if len(views) < 4 else (max(1, size[0] // 2), max(1, size[1] // 2))
        im = None
        for n, (l, t, vw) in enumerate(views, 1):
            # rounded camera values can land a hair outside the page at its edges: keep the box inside the image
            box = (max(0.0, l * ppc), max(0.0, t * ppc), min(float(src.width), (l + vw) * ppc), min(float(src.height), (t + vw * H / W) * ppc))
            shot = src.resize(work_size, Image.BILINEAR, box=box)
            im = shot if im is None else Image.blend(im, shot, 1.0 / n)
        return im if im.size == size else im.resize(size, Image.BILINEAR)

    def draw(st):
        si, m, views, cap, badge, bar_name, title, prog, keys = st
        src = source(si)
        if m <= 0.001:                                     # title card: full-bleed
            out = content(src, views[:1], (OW, OH))
        else:
            cw, ch = OW + (fw - OW) * m, OH + (fh - OH) * m
            x, y, b = fx * m, fy * m, bar * m
            ci, hi_, bi = int(round(cw)), int(round(ch)), int(round(b))
            out = blurred_backdrop(si, src).copy() if g.get("blur_bg") else (bg_framed if m > 0.5 else bg).copy()
            win = Image.new("RGB", (ci, hi_ + bi))
            if bi and bar_name:
                win.paste(bars[bar_name].resize((ci, bi), Image.BILINEAR) if bi != int(bar) or ci != bars[bar_name].width else bars[bar_name], (0, 0))
            win.paste(content(src, views, (ci, hi_)), (0, bi))
            out.paste(win, (int(round(x)), int(round(y - b))), _rounded_mask(ci, hi_ + bi, rad * m, masks))
        if prog:                                           # chapter progress along the top edge
            strip = progress_strip(*prog)
            out.paste(strip, (0, 0), strip)
        if title and title[0] in chapters:                 # chapter title in the space above the window
            im, meta = chapters[title[0]]
            top_room = fy - bar
            ty = (top_room - meta["h"]) / 2 if top_room >= meta["h"] + hb + 8 * ui else hb + 14 * ui
            tx = (fx if top_room >= meta["h"] + hb + 8 * ui else 24 * ui) - title[2] * 18 * ui
            im = faded(im, title[1])
            out.paste(im, (int(tx - meta["m"]), int(max(ty, hb + 4) - meta["m"])), im)
        cap_top = OH - 30 * f
        if cap:
            ci = cap_image(*cap)
            meta, M = idx["caps"][str(cap[0])], idx["M"]
            dy = int((1 - cap[3]) * 14 * f)
            out.paste(ci, (int(OW / 2 - meta["w"] / 2 - M), int(OH - 30 * f - meta["h"] - M + dy)), ci)
            cap_top = OH - 30 * f - meta["h"]
        if keys and keys[0] in keycaps:                    # keycaps pop up just above the caption
            im, meta = keycaps[keys[0]]
            if keys[2] < 0.999:
                im = im.resize((max(1, int(im.width * keys[2])), max(1, int(im.height * keys[2]))), Image.BILINEAR)
            im = faded(im, keys[1])
            out.paste(im, (int(OW / 2 - im.width / 2), int(cap_top - 14 * f - (meta["h"] + meta["m"]) * keys[2])), im)
        if badge and badge in badges:
            im, b = badges[badge]
            out.paste(im, (int(OW - 18 * f - b["w"] - 2), int(14 * f - 2 + (hb if prog else 0))), im)
        return out.tobytes()

    ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", "%dx%d" % (OW, OH), "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium",
                           "-crf", "18", "-pix_fmt", "yuv420p", job["out"]], stdin=subprocess.PIPE)
    last, raw, drawn = None, None, 0
    try:
        for st in job["states"]:
            if st != last:
                raw, last = draw(st), st
                drawn += 1
            ff.stdin.write(raw)
    finally:
        ff.stdin.close()
        ff.wait()
    if ff.returncode != 0:
        raise RuntimeError("ffmpeg failed encoding %s" % job["out"])
    return drawn


def default_out_size(W, H):
    if abs(W / H - 16 / 9) < 0.01:
        return 1920, 1080
    if abs(W / H - 9 / 16) < 0.01:
        return 1080, 1920
    s = 1080 / min(W, H)
    return int(W * s) // 2 * 2, int(H * s) // 2 * 2


def compose(work, out, opts, log=print, rec=None):
    """Draw the finished video from <work>/rec.json + frames, mix in the narration, write `out`.
    opts: frame, background, camera, out_size (w, h) or None, captions, accent, url, outro_url, jobs, chromium,
    narration (default <work>/narration.wav). `rec` replaces rec.json (revoice.py passes captions timed to a new voice)."""
    env.require_tools()
    try:
        import PIL  # noqa: F401
    except ImportError:
        raise SystemExit("the compose step needs Pillow: python -m pip install pillow (or build with --no-compose)")
    if rec is None:
        with open(os.path.join(work, "rec.json"), encoding="utf-8") as f:
            rec = json.load(f)
    if not rec.get("compose"):
        raise SystemExit("this recording was made with --no-compose (captions are already in the picture); rebuild without it to compose")
    t_start = time.time()
    tl = Timeline(rec, opts)
    W, H = tl.W, tl.H
    OW, OH = opts.get("out_size") or default_out_size(W, H)
    if abs(OW / OH - W / H) > 0.01:
        raise SystemExit("output %dx%d must have the same shape as the recording %dx%d" % (OW, OH, W, H))
    ui = min(OW, OH) / 1080.0
    accent = opts.get("accent") or "#c2573a"
    if not (accent.startswith("#") and len(accent) == 7):
        accent = "#c2573a"
    frame = opts.get("frame") or "window"
    bg_css, bg_dark = background_css(opts.get("background"), accent)
    framed, bar, radius = frame_geometry(OW, OH, W, H, frame, ui)
    total = max(tl.t_end - tl.t0, 0.001)
    spans = [((c[0] - tl.t0) / total, ((tl.chapters[i + 1][0] if i + 1 < len(tl.chapters) else tl.t_end) - tl.t0) / total)
             for i, c in enumerate(tl.chapters)]
    if spans:
        spans[0] = (0.0, spans[0][1])
    g = {"out": (OW, OH), "viewport": (W, H), "framed": framed, "bar": bar, "radius": radius, "frame": frame,
         "captions": opts.get("captions", True), "url_override": opts.get("url"), "url_fallback": opts.get("outro_url"),
         "f": OH / H, "ui": ui, "accent": accent, "bg_dark": bg_dark, "chap_spans": spans,
         "chapter_titles": opts.get("chapter_titles", True), "progress_bar": opts.get("progress_bar", True),
         "motion_blur": opts.get("motion_blur", True), "speaker_colours": speaker_colours(tl.caps, accent),
         "blur_bg": str(opts.get("background") or "").strip() == "blur" and frame != "none"}
    adir = os.path.join(work, "compose")
    os.makedirs(adir, exist_ok=True)
    for name in os.listdir(adir):
        if name.endswith((".png", ".mp4")):
            os.remove(os.path.join(adir, name))
    index = asyncio.run(_draw_assets(work, adir, g, tl, accent, bg_css, bg_dark, opts.get("chromium")))
    states = [state_at(tl, k, g, index["bars"]) for k in range(tl.n)]
    jobs = max(1, min(int(opts.get("jobs") or 4), max(1, (os.cpu_count() or 2) - 1), max(1, len(states) // (FPS * 3))))
    size = (len(states) + jobs - 1) // jobs
    parts = [os.path.join(adir, "part%02d.mp4" % i) for i in range(0, (len(states) + size - 1) // size)]
    work_list = [{"states": states[i * size:(i + 1) * size], "adir": adir, "work": work, "g": g, "index": index,
                  "frames": tl.frames, "out": p} for i, p in enumerate(parts)]
    extras = [x for x, on in (("%d chapter titles" % len(tl.titles), g["chapter_titles"] and tl.titles),
                              ("progress bar", g["progress_bar"] and tl.chapters), ("%d keycaps" % len(tl.key_ev), tl.key_ev),
                              ("motion blur", g["motion_blur"]),
                              ("%d speakers labelled" % len(g["speaker_colours"]), len(g["speaker_colours"]) > 1)) if on]
    log("composing %d frames at %dx%d (%s frame, camera %s with %d %s moves%s, %d workers)..."
        % (len(states), OW, OH, frame, opts.get("camera", "auto"), len(tl.camera.keys), opts.get("easing", "spring"),
           (", " + ", ".join(extras)) if extras else "", len(parts)))
    with multiprocessing.get_context("spawn").Pool(len(parts)) as pool:
        drawn = sum(pool.map(_render_chunk, work_list))
    lst = os.path.join(adir, "parts.txt")
    with open(lst, "w") as f:
        f.writelines("file '%s'\n" % os.path.basename(p) for p in parts)
    video = os.path.join(adir, "video.mp4")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", video], check=True)
    narr = opts.get("narration") or os.path.join(work, "narration.wav")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", video]
    sfx_wav = None
    if opts.get("sfx", True):                         # click, typing and card sounds, made from the timeline
        import sfx
        ev = sfx.plan_events(rec["timeline"]["events"], tl.t0)
        if ev:
            sfx_wav = sfx.render(ev, tl.t_end - tl.t0, os.path.join(adir, "sfx.wav"), opts.get("sfx_db", -16.0))
            log("  sound effects: %d (clicks, key taps, card whooshes)" % len(ev))
    if os.path.exists(narr):
        import voices
        af = voices.audio_filter(opts.get("finish", voices.DEFAULT_FINISH))
        if sfx_wav:    # effects sit under the voice: ducked while someone speaks, then mixed and limited
            fc = ("[1:a]%s,asplit[n1][n2];[2:a]aformat=sample_rates=48000[s];[s][n1]sidechaincompress=threshold=0.02:ratio=10:"
                  "attack=5:release=250[sd];[n2][sd]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.84:level=disabled[a]" % af)
            cmd += ["-i", narr, "-i", sfx_wav, "-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-shortest"]
        else:
            cmd += ["-i", narr, "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-af", af, "-c:a", "aac", "-b:a", "192k", "-shortest"]
    elif sfx_wav:
        cmd += ["-i", sfx_wav, "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        cmd += ["-c:v", "copy"]
    subprocess.run(cmd + ["-movflags", "+faststart", out], check=True)
    log("  composed in %.0f s (%d of %d frames drawn, the rest repeated)" % (time.time() - t_start, drawn, len(states)))
    return out


def main():
    env.safe_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="the build's --work folder (rec.json, frames/, narration.wav)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--frame", choices=("window", "browser", "none"), default=None)
    ap.add_argument("--background", default=None, help="brand | dark | light | blur (the app itself, blurred) | #hex | #hex1,#hex2 | image file")
    ap.add_argument("--camera", choices=("auto", "manual", "off"), default=None)
    ap.add_argument("--out-size", default=None, help="e.g. 1920x1080 (same shape as the recording)")
    ap.add_argument("--url", default=None, help="what the browser bar shows instead of a localhost address, e.g. app.acme.com")
    ap.add_argument("--easing", choices=("spring", "smooth"), default=None, help="camera moves: spring (quick start, soft settle) or smooth ease")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--no-chapter-titles", action="store_true", help="no chapter title when a chapter starts")
    ap.add_argument("--no-progress-bar", action="store_true", help="no chapter progress bar along the top")
    ap.add_argument("--no-motion-blur", action="store_true", help="no motion blur on fast camera moves")
    ap.add_argument("--finish", default=None, choices=("warm", "clean", "broadcast", "off"), help="how the narration is finished (default: what the build used)")
    ap.add_argument("--no-sfx", action="store_true", help="no interface sounds (clicks, key taps, card whooshes)")
    ap.add_argument("--sfx-db", type=float, default=None, help="loudness of the loudest effect in dB (default -16)")
    ap.add_argument("--jobs", type=int, default=None, help="parallel workers (default 4)")
    ap.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH"))
    a = ap.parse_args()
    with open(os.path.join(a.work, "rec.json"), encoding="utf-8") as f:
        opts = dict(json.load(f).get("compose_opts") or {})
    for k in ("frame", "background", "camera", "url", "jobs", "chromium", "easing", "finish", "sfx_db"):
        if getattr(a, k) is not None:
            opts[k] = getattr(a, k)
    if a.out_size:
        opts["out_size"] = tuple(int(x) for x in a.out_size.lower().split("x"))
    elif opts.get("out_size"):
        opts["out_size"] = tuple(opts["out_size"])
    for flag, key in (("no_captions", "captions"), ("no_chapter_titles", "chapter_titles"), ("no_progress_bar", "progress_bar"),
                      ("no_motion_blur", "motion_blur"), ("no_sfx", "sfx")):
        if getattr(a, flag):
            opts[key] = False
    compose(a.work, a.out, opts)
    print("\nDone: %s" % a.out)


if __name__ == "__main__":
    main()
