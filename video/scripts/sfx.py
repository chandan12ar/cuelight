#!/usr/bin/env python3
"""Interface sound effects, made from nothing (no downloads, no licences): mouse clicks, key taps while typing, keycap
presses, a soft whoosh when a title card comes or goes, a quiet pop when a callout draws.

    python sfx.py --work video/work --out video/work/sfx.wav [--db -16]      # from a recording's timeline

compose.py mixes this track under the narration, ducked while someone speaks, so effects never fight the voice. Every
sound is synthesised with the standard library only and is deterministic (same recording -> same track). `--no-sfx` in
build_video.py / compose.py turns it off; `look.sfx: false` in the storyboard does the same.
"""
import argparse
import array
import json
import math
import os
import random
import wave

SR = 24000


def _env(n, attack, decay):
    """Amplitude envelope: linear attack, exponential decay (samples n)."""
    a = max(1, int(attack * SR))
    return [(i / a if i < a else math.exp(-(i - a) / (decay * SR))) for i in range(n)]


def _hp(x, k=0.92):
    """One-pole high-pass: removes the rumble a noise burst carries."""
    out, prev_x, prev_y = [], 0.0, 0.0
    for v in x:
        prev_y = k * (prev_y + v - prev_x)
        prev_x = v
        out.append(prev_y)
    return out


def _lp(x, k=0.35):
    out, y = [], 0.0
    for v in x:
        y += k * (v - y)
        out.append(y)
    return out


def click(rng):
    """A soft mouse click: a short noise tick plus a low thump."""
    n = int(0.06 * SR)
    e = _env(n, 0.0008, 0.006)
    noise = _hp([rng.uniform(-1, 1) for _ in range(n)], 0.85)
    thump = [math.sin(2 * math.pi * 150 * i / SR) * math.exp(-i / (0.012 * SR)) for i in range(n)]
    return [(0.55 * noise[i] * e[i] + 0.45 * thump[i]) * 0.9 for i in range(n)]


def key(rng, variant=0):
    """One key tap: a lighter, higher tick than a click; four variants so typing does not sound like a machine gun."""
    n = int(0.045 * SR)
    e = _env(n, 0.0005, 0.0045)
    f = (900, 1100, 1300, 1000)[variant % 4] * rng.uniform(0.96, 1.04)
    noise = _hp([rng.uniform(-1, 1) for _ in range(n)], 0.9)
    tone = [math.sin(2 * math.pi * f * i / SR) * math.exp(-i / (0.008 * SR)) for i in range(n)]
    return [(0.6 * noise[i] * e[i] + 0.4 * tone[i]) * 0.75 for i in range(n)]


def keycap(rng):
    """A keycap shortcut (Ctrl + K): a firmer double tap."""
    a, b = key(rng, 1), key(rng, 2)
    gap = int(0.05 * SR)
    out = a + [0.0] * gap + b
    return [v * 1.15 for v in out]


def whoosh(rng, up=True):
    """A card sliding in or out: filtered noise that sweeps up (in) or down (out), faded at both ends."""
    n = int(0.55 * SR)
    out, y1, y2 = [], 0.0, 0.0
    for i in range(n):
        k = i / n
        pos = k if up else 1 - k
        coef = 0.04 + 0.5 * pos * pos                 # low-pass cut-off sweeps from ~150 Hz to ~4 kHz
        v = rng.uniform(-1, 1)
        y1 += coef * (v - y1)
        y2 += coef * (y1 - y2)
        out.append(y2 * math.sin(math.pi * k) ** 2 * 1.8)
    return _hp(out, 0.97)


def pop(rng):
    """A callout drawing itself: a quiet rising blip."""
    n = int(0.09 * SR)
    ph, out = 0.0, []
    for i in range(n):
        f = 520 + 380 * i / n
        ph += 2 * math.pi * f / SR
        out.append(math.sin(ph) * math.exp(-i / (0.03 * SR)) * 0.5)
    return out


def plan_events(timeline_events, t0, typing_rate=11.0):
    """[(seconds from the start of the video, kind, variant)] from rec.json's events.
    click/tab/hover-free: a click sounds where the cursor arrives (end of the action); typing taps spread over the
    action with a little jitter; a card change whooshes; keycaps and callouts get their own sounds."""
    rng = random.Random(7)
    out = []
    for e in timeline_events:
        kind = e.get("kind")
        if kind == "act":
            what, a, b = e.get("what"), e["t0"] - t0, e["t1"] - t0
            if what in ("click", "tab"):
                out.append((max(a, b - 0.12), "click", 0))
            elif what == "type":
                n = max(2, int((b - a) * typing_rate))
                for i in range(n):
                    out.append((a + (b - a) * (i + 0.5) / n + rng.uniform(-0.02, 0.02), "key", rng.randrange(4)))
            elif what in ("arrow", "circle", "underline", "highlight"):
                out.append((a + 0.1, "pop", 0))
        elif kind == "keys":
            out.append((e["t"] - t0, "keycap", 0))
        elif kind == "stage":
            out.append((e["t"] - t0, "whoosh_in" if e.get("name") else "whoosh_out", 0))
    return sorted((max(0.0, t), k, v) for t, k, v in out)


def render(events, total_s, path, db=-16.0):
    """Write the effects track (mono 24 kHz WAV, `total_s` long) with every sound at its time. `db` sets the loudness
    of the loudest effect relative to full scale (default -16 dB; compose.py ducks it further under speech)."""
    rng = random.Random(11)
    samples = {"click": click(rng), "keycap": keycap(rng), "whoosh_in": whoosh(rng, True), "whoosh_out": whoosh(rng, False),
               "pop": pop(rng)}
    keys = [key(rng, i) for i in range(4)]
    n = int(total_s * SR) + SR
    buf = array.array("f", bytes(4 * n))
    for t, kind, variant in events:
        s = keys[variant % 4] if kind == "key" else samples[kind]
        gain = {"key": 0.8, "click": 1.0, "keycap": 0.9, "whoosh_in": 0.7, "whoosh_out": 0.5, "pop": 0.5}[kind]
        start = int(t * SR)
        for i, v in enumerate(s):
            if start + i >= n:
                break
            buf[start + i] += v * gain
    peak = max((abs(v) for v in buf), default=0.0) or 1.0
    scale = (10 ** (db / 20.0)) / peak
    pcm = array.array("h", (max(-32767, min(32767, int(v * scale * 32767))) for v in buf))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--db", type=float, default=-16.0)
    a = ap.parse_args()
    with open(os.path.join(a.work, "rec.json"), encoding="utf-8") as f:
        rec = json.load(f)
    t0 = rec["frames"][0][0]
    ev = plan_events(rec["timeline"]["events"], t0)
    out = a.out or os.path.join(a.work, "sfx.wav")
    render(ev, rec["t_end"] - t0, out, a.db)
    kinds = {}
    for _, k, _ in ev:
        kinds[k] = kinds.get(k, 0) + 1
    print("sfx track: %s (%s)" % (out, ", ".join("%d %s" % (v, k) for k, v in sorted(kinds.items())) or "no events"))


if __name__ == "__main__":
    main()
