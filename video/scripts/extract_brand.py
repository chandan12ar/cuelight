#!/usr/bin/env python3
"""Read an app's own look (accent colour, font) so the title cards match it instead of a default theme.

    python extract_brand.py --url http://127.0.0.1:8799/ --out brand.json [--accent "#c2573a"] [--chromium PATH]

Accent comes from, in order: --accent, a CSS variable named like --acc/--accent/--primary/--brand on :root,
then the most saturated colour found on buttons, links and headings. The title-card background is a very
dark tint of the accent hue, which keeps white text readable on any brand. Pass the result to
build_video.py --brand brand.json. Open the file and adjust by hand if the guess is off.
"""
import argparse
import colorsys
import json
import re
import sys

from playwright.sync_api import sync_playwright

JS = r"""
() => {
  const cs = getComputedStyle(document.documentElement), out = {vars: {}, samples: []};
  for (const n of ['--acc','--accent','--primary','--brand','--color-primary','--color-accent','--brand-color','--link']) {
    const v = cs.getPropertyValue(n).trim(); if (v) out.vars[n] = v;
  }
  const take = (sel, prop) => document.querySelectorAll(sel).forEach((e, i) => { if (i < 40) out.samples.push(getComputedStyle(e)[prop]); });
  take('button, .btn, [role=button]', 'backgroundColor'); take('a', 'color'); take('h1,h2,h3', 'color'); take('svg [fill]', 'fill');
  const b = getComputedStyle(document.body);
  out.bg = b.backgroundColor; out.fg = b.color; out.font = b.fontFamily;
  return out;
}
"""


def parse_color(s):
    s = (s or "").strip()
    m = re.match(r"#([0-9a-fA-F]{6})$", s)
    if m:
        h = m.group(1); return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = re.match(r"#([0-9a-fA-F]{3})$", s)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1))
    m = re.match(r"rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)(?:[ ,/]+([\d.]+))?", s)
    if m and float(m.group(4) or 1) > 0.5:
        return tuple(int(m.group(i)) for i in (1, 2, 3))
    return None


def hexof(rgb):
    return "#%02x%02x%02x" % tuple(int(round(x)) for x in rgb)


def sat_light(rgb):
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    return h, s, l


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", default="brand.json")
    ap.add_argument("--accent", default=None, help="force the accent colour (#rrggbb)")
    ap.add_argument("--chromium", default=None)
    ap.add_argument("--size", default="1600x900")
    a = ap.parse_args()
    W, H = (int(x) for x in a.size.split("x"))
    with sync_playwright() as pw:
        launch = {"headless": True}
        if a.chromium:
            launch["executable_path"] = a.chromium
        br = pw.chromium.launch(**launch)
        pg = br.new_page(viewport={"width": W, "height": H})
        pg.goto(a.url); pg.wait_for_timeout(1200)
        info = pg.evaluate(JS)
        br.close()

    accent = parse_color(a.accent) if a.accent else None
    src = "--accent" if accent else None
    if not accent:
        for name, val in info["vars"].items():
            c = parse_color(val)
            if c and sat_light(c)[1] > 0.2:
                accent, src = c, "css variable %s" % name
                break
    if not accent:
        best = None
        for s in info["samples"]:
            c = parse_color(s)
            if not c:
                continue
            _, sat, lig = sat_light(c)
            score = sat * (1 - abs(lig - 0.5))
            if lig < 0.9 and lig > 0.1 and sat > 0.25 and (best is None or score > best[0]):
                best = (score, c)
        if best:
            accent, src = best[1], "most saturated element colour"
    if not accent:
        accent, src = (194, 87, 58), "default (nothing found - pass --accent)"
    h, _, _ = sat_light(accent)
    bg = tuple(c * 255 for c in colorsys.hls_to_rgb(h, 0.07, 0.30))      # very dark tint of the accent hue
    brand = {"accent": hexof(accent), "stageBg": hexof(bg), "stageFg": "#f5f3f0", "muted": "#b9bcc6", "font": info["font"] or "inherit",
             "_source": src, "_app_bg": info["bg"], "_app_fg": info["fg"]}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(brand, f, indent=2)
    print("brand: accent %s (%s), card background %s, font %s -> %s" % (brand["accent"], src, brand["stageBg"], brand["font"][:40], a.out))


if __name__ == "__main__":
    sys.exit(main())
