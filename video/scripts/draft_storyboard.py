#!/usr/bin/env python3
"""Explore a web app and draft a storyboard from what is actually on screen.

    python draft_storyboard.py --url http://127.0.0.1:8799/ --out video/script.json --shots video/shots [--nav "css selector"]

It finds the navigation items (tabs / sidebar / top nav), clicks through each one and lists what each view
contains: headings, cards, KPI tiles, charts, and tables (flagging wide ones that scroll sideways). From that it
writes script.json with the right scroll / spotlight / hover / sweep / hscroll actions already in place, plus
screenshots of every view so YOU can look at them.

The narration is deliberately left as "TODO: ..." with the on-screen text in `_context`. Write each sentence
yourself from what the product is for - a tool cannot know why a number matters. build_video.py refuses to
record while any TODO remains. Spoken text should answer "what question does this view help me answer?".
"""
import argparse
import json
import os
import sys

from playwright.sync_api import sync_playwright

NAV_JS = r"""
(custom) => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 20 && r.height > 14 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const groups = custom ? [custom] : ['[role=tablist] [role=tab]', 'nav button', 'nav a', 'aside button', 'aside a', '[class*=sidebar] button', '[class*=sidebar] a', '[class*=nav] button', '[class*=tabs] button', 'header a', '[data-tab]', '[data-s]'];
  for (const g of groups) {
    const els = [...document.querySelectorAll(g)].filter(vis).filter(e => !(e.tagName === 'A' && /^(https?:)?\/\//.test(e.getAttribute('href') || '') && !e.href.startsWith(location.origin)));
    const seen = new Set(), items = [];
    for (const e of els) {
      const label = (e.innerText || e.getAttribute('aria-label') || e.title || '').trim().split('\n')[0].trim();
      if (!label || label.length > 40 || seen.has(label)) continue;
      seen.add(label); items.push({label, group: g});
    }
    if (items.length >= 2) return items;
  }
  return [];
}
"""

VIEW_JS = r"""
() => {
  const vw = innerWidth, vh = innerHeight;
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 40 && r.height > 20 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const path = el => {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) return '#' + CSS.escape(el.id);
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.body && parts.length < 6) {
      if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) { parts.unshift('#' + CSS.escape(el.id)); break; }
      const sib = [...el.parentElement.children].filter(c => c.tagName === el.tagName);
      parts.unshift(el.tagName.toLowerCase() + (sib.length > 1 ? ':nth-of-type(' + (sib.indexOf(el) + 1) + ')' : ''));
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  const main = document.querySelector('main') || document.body;
  const scope = [...main.querySelectorAll('section')].filter(s => vis(s) && !s.hidden)[0] || main;
  const heads = [...scope.querySelectorAll('h1,h2,h3,h4')].filter(vis);
  const blocks = [], used = new Set();
  const card = h => {         // climb to the card-like container that holds this heading alone
    let el = h, best = h;
    for (let i = 0; i < 4 && el.parentElement && el.parentElement !== scope; i++) {
      const p = el.parentElement, r = p.getBoundingClientRect();
      if (r.height > 760 || r.width > vw - 8 && r.height > 500) break;
      if (p.querySelectorAll('h1,h2,h3,h4').length > 1) break;
      el = best = p;
    }
    return best;
  };
  for (const h of heads) {
    const c = card(h); if (used.has(c)) continue; used.add(c);
    const r = c.getBoundingClientRect();
    const table = c.querySelector('table'), chart = c.querySelector('canvas, svg');
    let wide = false;
    c.querySelectorAll('*').forEach(n => { if (!wide && n.scrollWidth > n.clientWidth + 12 && /(auto|scroll)/.test(getComputedStyle(n).overflowX)) { wide = path(n); } });
    const kind = table ? 'table' : (chart && chart.getBoundingClientRect().height > 100 ? 'chart' : 'card');
    blocks.push({kind, title: h.innerText.trim().slice(0, 80), selector: path(c), wide, top: Math.round(r.top + scrollY), height: Math.round(r.height),
                 text: c.innerText.replace(/\s+/g, ' ').trim().slice(0, 220)});
  }
  const kpis = [...scope.querySelectorAll('[class*=kpi],[class*=stat],[class*=metric],[class*=tile]')].filter(vis)
    .filter(e => e.innerText.length < 120 && e.getBoundingClientRect().height < 220).slice(0, 8);
  const kpiBox = kpis.length >= 2 ? {selector: path(kpis[0].parentElement), count: kpis.length, text: kpis.map(k => k.innerText.replace(/\s+/g, ' ').trim()).join(' | ').slice(0, 220)} : null;
  // a field the viewer could watch someone type into (search, prompt, form) - typing makes the product feel alive
  const f = [...scope.querySelectorAll('input, textarea, [contenteditable=true]')].filter(vis)
    .find(e => !/^(password|email|hidden|checkbox|radio|submit|button|file|range|color|date)$/i.test(e.type || ''));
  const field = f ? {selector: path(f), hint: (f.placeholder || f.getAttribute('aria-label') || f.name || '').slice(0, 60)} : null;
  return {title: (scope.querySelector('h1,h2') || {}).innerText || '', blocks, kpiBox, field, pageHeight: document.documentElement.scrollHeight, viewport: [vw, vh],
          text: (main.innerText || '').replace(/[ \t]+/g, ' ').slice(0, 20000)};
}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", default="script.json")
    ap.add_argument("--shots", default=None, help="folder for a screenshot of every view (look at them!)")
    ap.add_argument("--nav", default=None, help="CSS selector of the navigation items if auto-detection picks the wrong ones")
    ap.add_argument("--max-blocks", type=int, default=4, help="cards to cover per view")
    ap.add_argument("--size", default="1600x900")
    ap.add_argument("--chromium", default=None)
    ap.add_argument("--name", default=None, help="product name for the title card")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):          # tab labels with emoji or arrows must not crash a redirected console
        sys.stdout.reconfigure(errors="replace")
    W, H = (int(x) for x in a.size.split("x"))
    if a.shots:
        os.makedirs(a.shots, exist_ok=True)

    views = []
    with sync_playwright() as pw:
        launch = {"headless": True}
        if a.chromium:
            launch["executable_path"] = a.chromium
        br = pw.chromium.launch(**launch)
        pg = br.new_page(viewport={"width": W, "height": H})
        pg.goto(a.url); pg.wait_for_timeout(1500)
        name = a.name or (pg.title() or "The product").split("|")[0].split("-")[0].strip()
        meta = pg.evaluate("() => ({title: document.title, description: (document.querySelector('meta[name=description]') || {}).content || ''})")
        nav = pg.evaluate(NAV_JS, a.nav)
        if not nav:
            print("no navigation found: treating the page as a single view (use --nav to point at it)")
            nav = [{"label": None}]
        for n, item in enumerate(nav):
            if item["label"]:
                try:
                    pg.get_by_text(item["label"], exact=True).first.click(timeout=3000)
                except Exception as exc:
                    print("  could not click %r: %s" % (item["label"], str(exc).splitlines()[0])); continue
                pg.wait_for_timeout(900)
            pg.evaluate("scrollTo(0,0)")
            v = pg.evaluate(VIEW_JS); v["label"] = item["label"]; views.append(v)
            if a.shots:
                slug = "%02d-%s" % (n + 1, "".join(c if c.isalnum() else "-" for c in (item["label"] or "page")).lower())
                pg.screenshot(path=os.path.join(a.shots, slug + ".png"))
                pg.screenshot(path=os.path.join(a.shots, slug + "-full.png"), full_page=True)
            print("  %-22s %d blocks%s" % (item["label"] or "(page)", len(v["blocks"]), "  + KPI row" if v["kpiBox"] else ""))
        br.close()

    stages = {
        "intro": {"align": "center", "mark": True, "eyebrow": "TODO: category", "title": "%s" % name, "sub": "TODO: one-line promise"},
        "problem": {"eyebrow": "The problem", "title": "TODO: what is hard or invisible today", "sub": "TODO"},
        "solution": {"align": "center", "mark": True, "eyebrow": "The answer", "title": "TODO: what this does", "chips": ["TODO", "TODO", "TODO"]},
        "outro": {"align": "center", "mark": True, "title": name, "sub": "TODO: closing line", "cmd": "TODO: how to start", "url": "TODO"},
    }
    beats = [
        {"chapter": "Intro", "say": "TODO: name the product and its one-line promise.", "do": [{"stage": "intro"}, {"wait": 1.2}]},
        {"chapter": "The problem", "say": "TODO: the problem, in the viewer's words.", "do": [{"stage": "problem"}]},
        {"say": "TODO: what the product does, in one breath.", "do": [{"stage": "solution"}, {"reveal": "over"}]},
    ]
    for vi, v in enumerate(views):
        label = v["label"]
        first = {"chapter": label or "Overview", "say": "TODO: what question the %s view answers." % (label or "main"),
                 "_context": v["title"], "do": ([{"stage": None}, {"wait": 0.8}] if vi == 0 else [])}
        if label:
            first["do"].append({"tab": "text:" + label})
        if v["kpiBox"]:
            first["do"].append({"spot": v["kpiBox"]["selector"], "label": "TODO: label"})
            first["_context"] += " | KPIs: " + v["kpiBox"]["text"]
        beats.append(first)
        if v["field"]:
            beats.append({"say": "TODO: show the product doing its job: type what a real user would, and say what the result tells them.",
                          "_context": "input: %s" % (v["field"]["hint"] or v["field"]["selector"]),
                          "do": [{"spot": None}, {"type": v["field"]["selector"], "text": "TODO: realistic text a user would type"}, {"wait": 0.8}]})
        for b in v["blocks"][:a.max_blocks]:
            do = [{"spot": None}]
            if b["top"] > H - 200:
                do.append({"scroll": b["selector"], "ms": 1500})
            do.append({"spot": b["selector"], "label": b["title"][:40]})
            if b["kind"] == "chart":
                do.append({"sweep": b["selector"], "over": True})
            if b["wide"]:
                do += [{"hscroll": b["wide"], "to": 1, "ms": 2200}, {"hscroll": b["wide"], "to": 0, "ms": 1200}]
            beats.append({"say": "TODO: why '%s' matters / what decision it supports." % b["title"][:50], "_context": "%s: %s" % (b["kind"], b["text"]), "do": do})
    beats.append({"chapter": "Wrap up", "say": "TODO: closing line and how to get started.", "do": [{"spot": None}, {"stage": "outro"}, {"wait": 1.0}]})

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"title": "%s - walkthrough" % name, "tone": "explainer",
                   "share": "TODO: 1-3 sentences for the post - specific to this product, in its own words",
                   "stages": stages, "beats": beats}, f, indent=2, ensure_ascii=False)
    # everything the app says on screen, so lint_storyboard.py can check the narration's numbers and names against it
    page_text = os.path.join(os.path.dirname(os.path.abspath(a.out)), "page-text.json")
    with open(page_text, "w", encoding="utf-8") as f:
        json.dump({"url": a.url, "title": meta["title"], "description": meta["description"],
                   "views": [{"label": v["label"], "text": v["text"]} for v in views]}, f, indent=1, ensure_ascii=False)
    print("\nwrote %s: %d views, %d beats, and %s (the app's text, for the grounding check)." % (a.out, len(views), len(beats), page_text))
    print("Next: look at the screenshots, write video/plan.md (references/inspect.md), then replace every TODO "
          "(keep `_context` for reference) and run lint_storyboard.py.")


if __name__ == "__main__":
    sys.exit(main())
