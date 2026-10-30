"""Drive a web app through a storyboard (script.json) and capture frames.

Frames come from Chrome's screencast (JPEG, near-lossless) with wall-clock timestamps, so the
narration can be placed on the exact moment each beat started.

Two modes:
  compose (default)  the app layer is recorded clean - cursor, spotlight, callouts and title cards are in it, captions
                     and the badge are not - together with a timeline of what happened when (cards, captions with word
                     timings, where every clicked/typed/spotlighted element was). compose.py then draws the finished
                     picture: window frame, background, camera zoom, captions. Recorded at a higher pixel density so a
                     zoomed-in frame stays sharp.
  legacy             captions and badge are drawn into the page and build_video.py turns the frames straight into the
                     video (the original single-pass pipeline).
"""
import asyncio
import base64
import json
import math
import os
import time

from playwright.async_api import async_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
LEAD = 0.30     # seconds between a beat's visuals starting and its narration starting
TAIL = 0.55     # pause after each sentence
ACTION_KEYS = ("stage", "reveal", "tab", "scroll", "scroll_y", "scroll_sheet", "spot", "hover", "sweep", "click", "type",
               "hscroll", "key", "badge", "eval", "wait", "arrow", "circle", "underline", "highlight", "clear")
CALLOUTS = ("arrow", "circle", "underline", "highlight")
TARGETED = ("tab", "spot", "hover", "sweep", "click", "type") + CALLOUTS

# Looks for what should never be on screen in a product video: error messages, spinners/skeletons, an empty page.
# Findings are warnings for review, not failures (an "Error rate" chart is not an error).
HEALTH_JS = r"""
() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 4 && r.height > 4 && r.bottom > 0 && r.top < innerHeight && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity > 0.05; };
  const mine = e => e.closest('#v-root,#v-stage,#v-cursor');
  const out = [], seen = new Set();
  const errRx = /\b(error|failed|failure|exception|went wrong|not found|unauthori[sz]ed|forbidden|access denied|timed? ?out|unavailable|try again|crash(ed)?)\b/i;
  for (const e of document.querySelectorAll('[role=alert],[role=alertdialog],[aria-live=assertive],[class*=toast],[class*=alert],[class*=error],[class*=snackbar],[class*=notif],[class*=banner]')) {
    if (!vis(e) || mine(e)) continue;
    const t = (e.innerText || '').trim().replace(/\s+/g, ' ');
    if (t && errRx.test(t) && !seen.has(t)) { seen.add(t); out.push(['error message on screen', t.slice(0, 90)]); }
  }
  for (const e of document.querySelectorAll('[aria-busy=true],[role=progressbar],[class*=spinner],[class*=loading],[class*=skeleton],[class*=loader],[class*=shimmer]')) {
    if (vis(e) && !mine(e)) { out.push(['still loading', (typeof e.className === 'string' && e.className) || e.getAttribute('role') || e.tagName]); break; }
  }
  const txt = (document.body.innerText || '').trim();
  if (txt.replace(/\s+/g, '').length < 25) out.push(['almost empty page', 'fewer than 25 characters of text visible']);
  if (/^(404|500|502|503)\b|page not found|this site can.t be reached|application error|internal server error/i.test(document.title + ' | ' + txt.slice(0, 400)))
    out.push(['error page', (document.title || txt.slice(0, 60)).slice(0, 90)]);
  return out;
}
"""


def _ease(k):
    return 4 * k * k * k if k < .5 else 1 - math.pow(-2 * k + 2, 3) / 2


class Recorder:
    def __init__(self, page, size, sidebar_px=0, timeline=None):
        self.page, self.size, self.sidebar_px = page, size, sidebar_px
        self.x, self.y = size[0] - 160, size[1] // 2
        self.stage_on = False
        self.missing = []        # targets that were not found; build_video fails on these in strict mode
        self.tl = timeline       # compose mode: events are logged here for compose.py
        self.beat = -1
        self.issues = {}         # beat -> [[kind, detail]] from the page health probe and page errors

    async def v(self, expr, arg=None):
        return await self.page.evaluate(expr, arg)

    def log(self, kind, **kw):
        if self.tl is not None:
            self.tl["events"].append(dict(kind=kind, t=time.time(), beat=self.beat, **kw))

    def issue(self, kind, detail):
        lst = self.issues.setdefault(max(self.beat, 0), [])
        if [kind, detail] not in lst:
            lst.append([kind, detail])

    async def move(self, x, y, ms=800):
        x0, y0, t0 = self.x, self.y, time.perf_counter()
        while True:
            k = min(1.0, (time.perf_counter() - t0) * 1000 / max(ms, 1))
            e = _ease(k)
            await self.page.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e)
            if k >= 1:
                break
            await asyncio.sleep(0.011)
        self.x, self.y = x, y

    def _clamp(self, r):
        w, h = self.size
        # Keep the cursor off the caption bar (bottom) and the sticky header (top). A fixed sidebar is exempt:
        # clicks there must land exactly on the button, or every nav click silently misses.
        if self.sidebar_px and r["x"] < self.sidebar_px:
            return (r["x"], r["y"])
        lo = self.sidebar_px + 100 if self.sidebar_px else 60
        return (min(max(r["x"], lo), w - 60), min(max(r["y"], 150), h - 130))

    async def point_at(self, target, ms=850, exact=False):
        """Glide the cursor to a target. exact=True (anything that will be clicked) lands on the element's centre with no
        clamping: nudging a click target off its button is how a click silently hits its neighbour."""
        r = await self.v("t => V.rect(t)", target)
        if not r:
            print("   ! target not found:", target)
            self.missing.append(target)
            return False
        w, h = self.size
        await self.move(*((min(max(r["x"], 4), w - 4), min(max(r["y"], 4), h - 4)) if exact else self._clamp(r)), ms=ms)
        return True

    async def do(self, act, budget, stages):
        """Run one action; in compose mode also log where its target was, for the camera."""
        kind = next((k for k in ACTION_KEYS if k in act), None)
        t0, rect = time.time(), None
        if self.tl is not None and kind in TARGETED and act.get(kind):
            rect = await self.v("([t, x]) => V.rect(t, x)", [act[kind], kind in CALLOUTS])
        await self._do(act, budget, stages)
        if self.tl is not None and kind not in ("wait", "key", "eval", "badge", "reveal", "clear"):
            self.log("act", what=kind, t0=t0, t1=time.time(), rect=rect, zoom=act.get("zoom"), on_card=self.stage_on)

    async def _do(self, act, budget, stages):
        """Run one action. `budget` is the time left in the beat, for actions marked "over"."""
        p = self.page
        if "stage" in act:
            await self.v("() => V.hideCursor()")
            await self.v("() => V.clearCallouts(false)")
            await self.v("s => V.stage(s)", stages[act["stage"]] if act["stage"] else None)
            self.stage_on = bool(act["stage"])
            self.log("stage", name=act["stage"])
            await asyncio.sleep(0.9 if act["stage"] else 0.8)
        elif "reveal" in act:
            n = await self.v("() => V.revealCount()")
            if act["reveal"] == "over":
                # spread the reveals across the sentence; the last item lands ~1s before it ends
                span = max(1.0, budget - 1.2)
                for i in range(1, n + 1):
                    await self.v("i => V.reveal(i)", i)
                    if i < n:
                        await asyncio.sleep(span / n)
            else:
                await self.v("i => V.reveal(i)", act["reveal"])
        elif "tab" in act:                    # click a navigation target, then start the new view at the top
            await self.v("() => V.spot(null)")
            await self.v("() => V.clearCallouts(false)")
            if await self.point_at(act["tab"], 750, exact=True):
                await p.mouse.down(); await p.mouse.up()
            await self.v("() => scrollTo(0, 0)")
            await asyncio.sleep(1.1)
        elif "scroll" in act:
            y = await self.v("([t, c]) => V.scrollInfo(t, c)", [act["scroll"], bool(act.get("center"))])
            if y is None:
                print("   ! scroll target not found:", act["scroll"])
                self.missing.append(act["scroll"])
            else:
                await self.v("([y, ms]) => V.scrollTo(y, ms)", [y, act.get("ms", 1500)])
        elif "scroll_y" in act:
            await self.v("([y, ms]) => V.scrollTo(y, ms)", [act["scroll_y"], act.get("ms", 1200)])
        elif "scroll_sheet" in act:
            sel = act["scroll_sheet"] if isinstance(act["scroll_sheet"], str) else "#sBox"
            await self.v("""async (sel) => {
                const el = document.querySelector(sel); if (!el) return;
                const max = el.scrollHeight - el.clientHeight; if (max < 8) return;
                const go = (a, b, ms) => new Promise(res => { const t0 = performance.now();
                  const f = n => { const k = Math.min(1, (n - t0) / ms); const e = k < .5 ? 4*k*k*k : 1 - Math.pow(-2*k+2, 3)/2;
                    el.scrollTop = a + (b - a) * e; k < 1 ? requestAnimationFrame(f) : res(); }; requestAnimationFrame(f); });
                await go(0, max * 0.7, 2200); await go(max * 0.7, 0, 1400); }""", sel)
        elif "spot" in act:
            ok = await self.v("([t, l]) => V.spot(t, l)", [act["spot"], act.get("label")])
            if not ok and act["spot"]:
                print("   ! spot target not found:", act["spot"])
                self.missing.append(str(act["spot"]))
        elif "hover" in act:
            await self.point_at(act["hover"], act.get("ms", 900))
        elif "sweep" in act:
            r = await self.v("t => { const el = V.find(t); if (!el) return null; const c = el.querySelector('canvas') || el; "
                             "const q = c.getBoundingClientRect(); return {l:q.left, r:q.right, t:q.top, b:q.bottom}; }", act["sweep"])
            if r:
                w, h = self.size
                y = min(max((r["t"] + r["b"]) / 2, 170), h - 150)
                x0, x1 = max(r["l"], 340) + 40, min(r["r"], w - 40) - 40
                await self.move(x0, y, 700)
                span = max(1.0, (budget - 0.8) * 1000) if act.get("over") else act.get("ms", 2500)
                await self.move(x1, y, span)
        elif "click" in act:
            if await self.point_at(act["click"], 800, exact=True):
                await asyncio.sleep(0.15)
                await p.mouse.down(); await p.mouse.up()
                await asyncio.sleep(0.5)
        elif "type" in act:                   # click a field and type into it with real key presses, so the app reacts
            if await self.point_at(act["type"], 750, exact=True):
                await p.mouse.down(); await p.mouse.up()
                await asyncio.sleep(0.25)
                if act.get("clear", True):
                    await p.keyboard.press("ControlOrMeta+A"); await p.keyboard.press("Backspace")
                text = str(act.get("text", ""))
                # "over": spread the typing across the sentence, finishing ~1.4 s before it ends; else a steady pace
                per = min(0.25, max(0.03, (budget - 1.4) / max(len(text), 1))) if act.get("over") else 1.0 / act.get("cps", 14)
                await p.keyboard.type(text, delay=per * 1000)
                if act.get("enter"):
                    await asyncio.sleep(0.3)
                    if act.get("show", True):
                        self.log("keys", combo="Enter")
                    await p.keyboard.press("Enter")
                    await asyncio.sleep(0.6)
        elif any(k in act for k in CALLOUTS):  # arrow / circle / underline / highlight that draws itself on its target
            kind = next(k for k in CALLOUTS if k in act)
            ok = await self.v("([k, t, o]) => V.callout(k, t, o)",
                              [kind, act[kind], {"label": act.get("label"), "from": act.get("from", "auto"), "keep": bool(act.get("keep"))}])
            if not ok:
                print("   ! %s target not found: %s" % (kind, act[kind]))
                self.missing.append(str(act[kind]))
        elif "clear" in act:                  # remove every callout now (they also clear by themselves at the next beat)
            await self.v("() => V.clearCallouts(false)")
        elif "hscroll" in act:
            res = await self.v("([t, to, ms]) => V.hscroll(t, to, ms)", [act["hscroll"], act["to"], act.get("ms", 2000)])
            if res is None:
                print("   ! hscroll target not found:", act["hscroll"])
                self.missing.append(act["hscroll"])
        elif "key" in act:                    # compose mode shows the keys as keycaps ("Ctrl + K"); "show": false hides them
            if act.get("show", True):
                self.log("keys", combo=act["key"])
            await p.keyboard.press(act["key"])
        elif "badge" in act:
            if self.tl is not None:            # compose mode: the badge is drawn by compose.py, outside the zoomable app
                self.log("badge", text=act["badge"] or None)
            else:
                await self.v("t => V.badge(t)", act["badge"] or None)
        elif "eval" in act:                   # escape hatch: run any JS in the page (toggle a theme, open a drawer...)
            await self.v("(src) => (0, eval)(src)", act["eval"])
        elif "wait" in act:
            await asyncio.sleep(act["wait"])
        else:
            raise ValueError("unknown action: %r" % (act,))


async def open_page(pw, url, size=(1600, 900), chromium=None, init_js="", config_js="", color_scheme="light",
                    storage_state=None, http_credentials=None, brand=None, blur=None, dsf=1):
    """Launch Chromium, open the app and inject the overlay. The recorder and preflight.py both use this, so the
    card check sees exactly the page the recording will (same fonts, CSS, login and brand). `dsf` is the pixel
    density: 1.5 renders the same layout with 1.5x the pixels, so text stays sharp when compose.py zooms in."""
    W, H = size
    launch = {"headless": True, "args": ["--hide-scrollbars", "--force-device-scale-factor=%s" % dsf]}
    if chromium:
        launch["executable_path"] = chromium
    browser = await pw.chromium.launch(**launch)
    ctx_args = {"viewport": {"width": W, "height": H}, "device_scale_factor": dsf, "color_scheme": color_scheme}
    if storage_state:                              # saved login (scripts/save_login.py)
        ctx_args["storage_state"] = storage_state
    if http_credentials:                           # {"username": ..., "password": ...} for HTTP basic auth
        ctx_args["http_credentials"] = http_credentials
    ctx = await browser.new_context(**ctx_args)
    if init_js:                                   # runs before the page's own scripts (seed localStorage, flags...)
        await ctx.add_init_script(init_js)
    page = await ctx.new_page()
    await page.goto(url)
    if brand:                                     # extract_brand.py output: colours/font for the title cards
        await page.add_script_tag(content="window.V_BRAND = %s;" % json.dumps(brand))
    if blur:                                      # {"selectors": [...], "auto": true}
        await page.add_script_tag(content="window.V_BLUR = %s;" % json.dumps(blur))
    if config_js:                                 # window.V_CONFIG = {...}, see overlay.js
        await page.add_script_tag(content=config_js)
    await page.add_script_tag(path=os.path.join(HERE, "overlay.js"))
    await page.evaluate("() => V.ready()")
    return browser, ctx, page


async def record(script, durations, url, frames_dir, chromium=None, size=(1600, 900), init_js="", config_js="",
                 sidebar_px=0, color_scheme="light", captions=True, badge="", data_line="", log=print,
                 storage_state=None, http_credentials=None, brand=None, blur=None, word_times=None,
                 compose=False, dsf=1, speakers=None):
    """Returns {"frames": [(ts, path)], "t_end": ts, "beats": [{"start": ts, "dur": s, "i": n}], "missing": [...],
    "issues": {beat: [[kind, detail]]}, "compose": bool, "timeline": {...} (compose mode)}.
    `speakers` {beat: [name, cast index, color]} when several voices talk: compose.py labels each caption with its speaker.
    A beat's "pause" (seconds after its sentence, default TAIL) lets a reply follow a question as quickly as in a real
    conversation."""
    os.makedirs(frames_dir, exist_ok=True)
    stages = script["stages"]
    W, H = size
    timeline = {"viewport": [W, H], "dsf": dsf, "captions": [], "events": []} if compose else None
    async with async_playwright() as pw:
        browser, ctx, page = await open_page(pw, url, size, chromium, init_js, config_js, color_scheme,
                                             storage_state, http_credentials, brand, blur, dsf)
        rec = Recorder(page, size, sidebar_px, timeline)
        await page.mouse.move(rec.x, rec.y)
        # problems the page reports by itself while the video is being made
        page.on("pageerror", lambda exc: rec.issue("JavaScript error", str(exc).splitlines()[0][:110]))
        page.on("response", lambda r: rec.issue("HTTP %d" % r.status, r.url[:110])
                if (r.status >= 400 and r.request.resource_type == "document") or
                   (r.status >= 500 and r.request.resource_type in ("xhr", "fetch")) else None)

        # start on the title card so the first frame is already the intro, not the app
        if "intro" in stages:
            await page.evaluate("s => V.stage(s)", stages["intro"])
            rec.stage_on = True
            rec.log("stage", name="intro")
            await asyncio.sleep(1.0)
        await page.evaluate("() => V.hideCursor()")

        frames = []
        cdp = await ctx.new_cdp_session(page)
        pending = set()

        async def on_frame(ev):
            path = os.path.join(frames_dir, "f%06d.jpg" % len(frames))
            frames.append((ev["metadata"]["timestamp"], path))
            with open(path, "wb") as f:
                f.write(base64.b64decode(ev["data"]))
            await cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})

        def handler(ev):
            t = asyncio.ensure_future(on_frame(ev)); pending.add(t); t.add_done_callback(pending.discard)

        cdp.on("Page.screencastFrame", handler)
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": int(W * dsf),
                                                "maxHeight": int(H * dsf), "everyNthFrame": 1})
        await asyncio.sleep(0.3)

        beats = []
        for i, b in enumerate(script["beats"]):
            dur = durations[i] + LEAD + float(b.get("pause", TAIL))
            t0 = time.time()
            rec.beat = i
            beats.append({"i": i, "start": t0, "dur": dur})
            text = (b.get("cap") or b["say"]).replace("{DATA}", data_line).strip()
            # no caption over a title card: the card is the visual
            has_stage = any("stage" in a and a["stage"] for a in b.get("do", [])) or (
                rec.stage_on and not any("stage" in a for a in b.get("do", [])))
            wt = (word_times or {}).get(i)
            show = bool(captions and not has_stage)
            if compose:                          # compose.py draws the caption, outside the zoomable app layer
                cap = {"beat": i, "t": t0, "text": text, "words": wt, "show": show, "chapter": b.get("chapter")}
                if speakers and i in speakers:
                    cap["who"], cap["speaker"], cap["color"] = speakers[i]
                timeline["captions"].append(cap)
                rec.log("url", url=page.url)
            else:
                await page.evaluate("([t, o]) => V.caption(t, o)", [text if show else None,
                                                                    {"words": wt, "delay": LEAD} if wt else None])
            if i:                                # callouts belong to the sentence that drew them, unless "keep"
                await page.evaluate("() => V.clearCallouts(true)")
            log("  [%2d/%d] %s" % (i + 1, len(script["beats"]), text[:70]))
            for a in b.get("do", []):
                a = json.loads(json.dumps(a).replace("{BADGE}", badge).replace("{DATA}", data_line))
                remaining = dur - (time.time() - t0)
                await rec.do(a, remaining, stages)
            if not rec.stage_on:                 # what is on screen now, while the sentence plays
                for kind, detail in await page.evaluate(HEALTH_JS):
                    rec.issue(kind, detail)
            left = dur - (time.time() - t0)
            if left > 0:
                await asyncio.sleep(left)
            else:
                beats[-1]["dur"] = time.time() - t0     # actions ran long: the beat grows to fit
        if not compose:
            await page.evaluate("() => V.caption(null)")
        else:                                    # the accent the overlay really used (brand.json, cfg.js or default), as #rrggbb
            timeline["accent"] = await page.evaluate("""() => { const x = document.createElement('canvas').getContext('2d');
                x.fillStyle = getComputedStyle(document.getElementById('v-root')).getPropertyValue('--v-acc').trim() || '#c2573a';
                return x.fillStyle; }""")
        await asyncio.sleep(2.2)    # hold on the outro card
        t_end = time.time()
        await cdp.send("Page.stopScreencast")
        await asyncio.sleep(0.4)
        if pending:
            await asyncio.gather(*pending)
        await browser.close()
    out = {"frames": frames, "t_end": t_end, "beats": beats, "missing": sorted(set(rec.missing)),
           "issues": {str(k): v for k, v in sorted(rec.issues.items())}, "compose": bool(compose)}
    if compose:
        out["timeline"] = timeline
    return out
