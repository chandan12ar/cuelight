#!/usr/bin/env python3
"""Check every title card, caption and spotlight label in seconds, before a long recording.

    python preflight.py --script video/script.json --url http://127.0.0.1:8799/ [--brand brand.json] [--config-js cfg.js]

Shows each stage card (all items revealed) and measures each caption on the real page, with the real fonts, CSS and
brand, exactly as the recorder will (it uses the same page set-up). It reports:
  ERROR    text cut off by the screen edge or spilling out of its box; a card taller than the screen;
           text against its background below 3:1 contrast (unreadable on a phone)
  warning  below the WCAG AA target (4.5:1, or 3:1 for large text); a title over 3 lines; a caption that wraps to
           3+ lines; a spotlight label too wide; an accent so light that white label text on it is hard to read
and writes a still of every card plus a contact sheet (preflight.jpg). LOOK at the sheet.

build_video.py runs this automatically once the app is up and stops on errors (exit 4); --skip-preflight overrides.
Without --url it checks on a blank page: layout is right but the app's own font may not be loaded.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys

from playwright.async_api import async_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import env  # noqa: E402
from record import open_page  # noqa: E402

MEASURE_CARD = r"""
() => {
  const W = innerWidth, H = innerHeight, out = [];
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', {willReadFrequently: true});
  const rgba = c => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = 'rgba(0,0,0,0)'; cx.fillStyle = c; cx.fillRect(0, 0, 1, 1);
                      const d = cx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const lum = c => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
                     return .2126 * f(c[0]) + .7152 * f(c[1]) + .0722 * f(c[2]); };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
  const mix = (top, under) => under.map((v, i) => top[i] * top[3] + v * (1 - top[3]));
  const stage = document.getElementById('v-stage');
  const base = rgba(getComputedStyle(stage).backgroundColor);     // the card colour (the soft gradient tints are ignored)
  const bgOf = el => {           // composite every semi-transparent background between the text and the card
    const layers = [];
    for (let e = el; e && e !== stage; e = e.parentElement) { const c = rgba(getComputedStyle(e).backgroundColor); if (c[3] > 0) layers.push(c); }
    let col = base.slice(0, 3); for (const c of layers.reverse()) col = mix(c, col); return col;
  };
  const seen = {};               // the same contrast finding on every row of a list is reported once, with a count
  const once = (level, key, msg) => { if (seen[key]) { seen[key][2]++; return; } seen[key] = [level, msg, 0]; };
  const box = stage.querySelector('.vs-in');
  let tall = false;               // a card taller than the screen is one finding, not one per row it pushes off screen
  if (box) { const r = box.getBoundingClientRect(); if (r.top < 0 || r.bottom > H) { tall = true; out.push(['error', 'card is taller than the screen (' + Math.round(r.height) + ' px of ' + H + ') - fewer items or shorter text, or split it into two cards']); } }
  for (const el of stage.querySelectorAll('.vs-eyebrow,.vs-title,.vs-title em,.vs-sub,.vs-k,.vs-v,.vs-chip,.vs-cmd,.vs-url')) {
    const r = el.getBoundingClientRect(); if (!r.width) continue;
    const cs = getComputedStyle(el), name = el.className.replace('vs-', '') || 'title accent', txt = el.textContent.trim().slice(0, 40);
    const sideways = r.left < -1 || r.right > W + 1, vertical = r.top < -1 || r.bottom > H + 1;
    if (sideways || (vertical && !tall)) out.push(['error', name + ' "' + txt + '" is cut off by the screen edge']);
    if (!sideways && cs.display !== 'inline' && el.scrollWidth > el.clientWidth + 2) out.push(['error', name + ' "' + txt + '" spills out of its box (a long word, URL or command) - shorten it']);
    if (/vs-title|vs-sub/.test(el.className) && cs.display !== 'inline') {
      const lines = Math.round(r.height / parseFloat(cs.lineHeight));
      if (lines > 3) out.push(['warn', name + ' wraps to ' + lines + ' lines - shorten it']);
    }
    const fg = rgba(cs.color), bg = bgOf(el), c = ratio(mix(fg, bg), bg);
    const px = parseFloat(cs.fontSize), large = px >= 24 || (px >= 18.66 && parseInt(cs.fontWeight) >= 700);
    if (c < 3) once('error', name + c.toFixed(1), name + ' "' + txt + '" contrast ' + c.toFixed(1) + ':1 is unreadable - use a lighter text colour or darker card (brand.json)');
    else if (c < (large ? 3 : 4.5)) once('warn', name + c.toFixed(1), name + ' "' + txt + '" contrast ' + c.toFixed(1) + ':1 is below ' + (large ? 3 : 4.5) + ':1');
  }
  for (const [level, msg, more] of Object.values(seen)) out.push([level, msg + (more ? ' (and ' + more + ' more like it)' : '')]);
  return out;
}
"""

MEASURE_TEXT = r"""
([caps, labels]) => {
  const W = innerWidth, out = [], cap = document.getElementById('v-cap'), lab = document.getElementById('v-label');
  const cs = getComputedStyle(cap), padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom), lh = parseFloat(cs.lineHeight);
  caps.forEach(([i, t]) => { cap.textContent = t; const lines = Math.round((cap.offsetHeight - padY) / lh);
    if (lines > 2) out.push(['warn', 'beat ' + i + ': caption wraps to ' + lines + ' lines at this size - shorten the sentence or give it a shorter "cap"']); });
  cap.textContent = '';
  labels.forEach(([i, t]) => { lab.textContent = t;
    if (lab.offsetWidth > W - 32) out.push(['error', 'beat ' + i + ': spotlight label "' + t.slice(0, 40) + '" is wider than the screen']);
    else if (lab.offsetWidth > W * 0.5) out.push(['warn', 'beat ' + i + ': spotlight label "' + t.slice(0, 40) + '" is long - 1-4 words read best']); });
  lab.textContent = '';
  const cv = document.createElement('canvas'); cv.width = cv.height = 1; const cx = cv.getContext('2d');
  cx.fillStyle = getComputedStyle(document.getElementById('v-root')).getPropertyValue('--v-acc').trim(); cx.fillRect(0, 0, 1, 1);
  const a = cx.getImageData(0, 0, 1, 1).data, f = v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
  const L = .2126 * f(a[0]) + .7152 * f(a[1]) + .0722 * f(a[2]), c = 1.05 / (L + .05);
  if (c < 3) out.push(['warn', 'accent colour is light: white text on it (spotlight labels, the highlighted caption word) is ' + c.toFixed(1) + ':1 - pick a darker accent in brand.json']);
  return out;
}
"""


async def run(script, url, out_dir, size=(1600, 900), chromium=None, init_js="", config_js="", color_scheme="light",
              storage_state=None, http_credentials=None, brand=None, blur=None, log=print):
    """Returns (errors, warnings, contact_sheet_path)."""
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        if f.startswith("pf-") and f.endswith(".png"):
            os.remove(os.path.join(out_dir, f))
    stages = script.get("stages") or {}
    beats = script.get("beats") or []
    E, W, shots = [], [], []
    async with async_playwright() as pw:
        browser, ctx, page = await open_page(pw, url, size, chromium, init_js, config_js, color_scheme,
                                             storage_state, http_credentials, brand, blur)
        for n, (name, spec) in enumerate(stages.items(), 1):
            await page.evaluate("s => V.stage(s)", spec)
            await asyncio.sleep(0.8)                         # the card fades in over 0.7 s
            await page.evaluate("() => V.reveal(999)")
            await asyncio.sleep(0.6)                         # items slide in over 0.5 s
            for level, msg in await page.evaluate(MEASURE_CARD):
                (E if level == "error" else W).append("card %r: %s" % (name, msg))
            shot = os.path.join(out_dir, "pf-%02d.png" % n)
            await page.screenshot(path=shot)
            shots.append((shot, "card " + name))
        await page.evaluate("() => V.stage(null)")
        caps, labels, longest = [], [], ""
        for i, b in enumerate(beats, 1):
            t = str(b.get("cap") or b.get("say", "")).strip()
            caps.append([i, t])
            longest = max(longest, t, key=len)
            labels += [[i, str(a["label"])] for a in b.get("do", []) if isinstance(a, dict) and a.get("label")]
        for level, msg in await page.evaluate(MEASURE_TEXT, [caps, labels]):
            (E if level == "error" else W).append(msg)
        if longest:                                          # one still of the longest caption over the app
            await asyncio.sleep(0.8)
            await page.evaluate("t => V.caption(t)", longest)
            await asyncio.sleep(0.5)
            shot = os.path.join(out_dir, "pf-%02d.png" % (len(shots) + 1))
            await page.screenshot(path=shot)
            shots.append((shot, "longest caption"))
        await browser.close()
    sheet = None
    if shots:
        sheet = os.path.join(out_dir, "preflight.jpg")
        cols = 2 if len(shots) <= 4 else 3
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-start_number", "1", "-i",
                        os.path.join(out_dir, "pf-%02d.png"), "-vf", "scale=640:-1,tile=%dx%d" % (cols, (len(shots) + cols - 1) // cols),
                        "-frames:v", "1", "-q:v", "3", sheet], check=True)
    for shot, what in shots:
        log("  still: %s  (%s)" % (os.path.basename(shot), what))
    return E, W, sheet


def report(E, W, sheet, out=print):
    for e in E:
        out("  ERROR  " + e)
    for w in W:
        out("  warn   " + w)
    if sheet:
        out("  contact sheet: %s  (look at it)" % sheet)
    out("  preflight: %d error(s), %d warning(s)" % (len(E), len(W)))


def main():
    env.safe_console()
    env.require_tools()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--script", required=True)
    ap.add_argument("--url", default=None, help="the app (already running); default: a blank page")
    ap.add_argument("--out-dir", default="preflight", help="where the stills and preflight.jpg go")
    ap.add_argument("--config-js", default=None)
    ap.add_argument("--init-js", default=None)
    ap.add_argument("--brand", default=None)
    ap.add_argument("--size", default="1600x900")
    ap.add_argument("--scheme", default="light", choices=("light", "dark"))
    ap.add_argument("--storage-state", default=None)
    ap.add_argument("--blur", default="", help="comma-separated CSS selectors to blur in the stills")
    ap.add_argument("--blur-auto", action="store_true", help="blur emails, keys and IPs in the stills")
    ap.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH"))
    a = ap.parse_args()
    rd = lambda p: open(p, encoding="utf-8").read() if p else ""
    with open(a.script, encoding="utf-8") as f:
        script = json.load(f)
    W_, H_ = (int(x) for x in a.size.lower().split("x"))
    url = a.url or "data:text/html,<!doctype html><html><body></body></html>"
    brand = json.load(open(a.brand, encoding="utf-8")) if a.brand else None
    blur = {"selectors": [x.strip() for x in a.blur.split(",") if x.strip()], "auto": a.blur_auto} if (a.blur or a.blur_auto) else None
    E, W, sheet = asyncio.run(run(script, url, a.out_dir, (W_, H_), a.chromium, rd(a.init_js),
                                  rd(a.config_js) if a.url else "", a.scheme, a.storage_state, None, brand, blur))
    report(E, W, sheet)
    return 4 if E else 0


if __name__ == "__main__":
    sys.exit(main())
