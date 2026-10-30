#!/usr/bin/env python3
"""The plan the user approves BEFORE any voice, recording or animation is made: a high-level flow of boxes plus a few words.

    python plan_board.py --script video/script.json --out video/plan            # writes plan.html and plan.outline.md
    python plan_board.py --script video/script.json --out video/plan --approve  # records the user's approval

Reads the storyboard (the script the skill wrote from the code, the app and the user's answers) and draws one box per
section - where the video jumps to, what is shown, what is explained in one line, about how long, who speaks - joined by
arrows, with the user's highlights marked. Not the full script: a picture and a few words that can be judged in a minute.
When the storyboard changes the flow (a section added, removed or reordered, a different view) the approval no longer
matches and build_video.py asks for the plan to be shown again; rewording a sentence does not.
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

WPM = 160.0


def _view_name(sel):
    """A readable name for a tab/click selector: '#nav button[data-s=usage]' -> 'usage', 'text:Reports' -> 'Reports'."""
    sel = str(sel)
    if sel.startswith("text:"):
        return sel[5:].strip()
    m = re.findall(r"=\"?'?([\w .-]+)\"?'?\]", sel)
    name = m[-1].strip() if m else None
    if not name:
        m = re.findall(r"[#.]([A-Za-z][\w-]*)", sel)
        name = m[-1] if m else sel[:24]
    name = re.sub(r"^[a-z](?=[A-Z])", "", name)                 # tProj -> Proj
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("-", " ").replace("_", " ")
    return name.strip().lower() if name.isupper() is False else name


def _first_words(text, n=13):
    words = str(text).replace("{DATA}", "").split()
    out = " ".join(words[:n])
    return out + ("..." if len(words) > n else "")


def sections(script):
    """[{n, title, jump, shows, says, seconds, speakers, beats, stars}] - one per chapter (beats before the first chapter
    form 'Opening'). Deterministic, so the same storyboard always gives the same plan."""
    beats = [b for b in script.get("beats", []) if isinstance(b, dict)]
    cast = script.get("cast") or {}
    names = {k: (v or {}).get("name") or k.title() for k, v in cast.items()}
    highs = [str(h).strip() for h in (script.get("highlights") or []) if str(h).strip()]
    out, cur = [], None
    for b in beats:
        if cur is None or b.get("chapter"):
            cur = {"title": b.get("chapter") or "Opening", "jump": [], "shows": [], "says": "", "words": 0, "speakers": [],
                   "beats": 0, "text": ""}
            out.append(cur)
        cur["beats"] += 1
        cur["words"] += len(str(b.get("say", "")).split())
        cur["text"] += " " + str(b.get("say", "")) + " " + str(b.get("cap", ""))
        if not cur["says"] and len(str(b.get("say", "")).split()) >= 6:
            cur["says"] = _first_words(b.get("say", ""))
        who = names.get(b.get("who") or (next(iter(cast), "")), "") if cast else ""
        if who and who not in cur["speakers"]:
            cur["speakers"].append(who)
        for a in b.get("do", []):
            if not isinstance(a, dict):
                continue
            if a.get("stage"):
                cur["jump"].append("title card: %s" % a["stage"])
            for k, label in (("tab", "go to"), ("click", "click"), ("type", "type in")):
                if k in a:
                    v = a[k] if not isinstance(a[k], dict) else a[k].get("selector", "")
                    cur["jump"].append("%s %s" % (label, _view_name(v)))
                    if k == "tab" and not cur.get("view"):
                        cur["view"] = str(v)
            if a.get("label"):
                cur["shows"].append(str(a["label"]))
    result = []
    for i, c in enumerate(out, 1):
        low = c["text"].lower()
        stars = [h for h in highs if all(w in low for w in re.findall(r"[a-z0-9]{3,}", h.lower()))]
        dedup = lambda xs: list(dict.fromkeys(xs))
        result.append({"n": i, "title": c["title"], "jump": dedup(c["jump"])[:3], "shows": dedup(c["shows"])[:4],
                       "says": c["says"], "seconds": int(round(c["words"] / WPM * 60 + c["beats"] * 0.85)),
                       "speakers": c["speakers"], "beats": c["beats"], "stars": stars, "view": c.get("view", "")})
    return result


def fingerprint(script):
    """What the approval covers: the order of sections, their titles, where they jump and what they show - not the wording."""
    flow = [(s["title"], s["jump"], s["shows"]) for s in sections(script)]
    return hashlib.sha1(json.dumps(flow, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def outline(script):
    secs = sections(script)
    total = sum(s["seconds"] for s in secs)
    lines = ["# %s - plan (about %d:%02d, %d sections)" % (script.get("title", "Video"), total // 60, total % 60, len(secs)), ""]
    highs = script.get("highlights") or []
    if highs:
        lines += ["Highlights asked for: " + "; ".join(str(h) for h in highs), ""]
    for s in secs:
        lines.append("%d. **%s** (~%ds%s)%s" % (s["n"], s["title"], s["seconds"],
                                               ", " + " + ".join(s["speakers"]) if s["speakers"] else "",
                                               "  [HIGHLIGHT]" if s["stars"] else ""))
        if s["jump"]:
            lines.append("   - jump: " + "; ".join(s["jump"]))
        if s["shows"]:
            lines.append("   - shows: " + ", ".join(s["shows"]))
        if s["says"]:
            lines.append("   - explains: \"%s\"" % s["says"])
    return "\n".join(lines) + "\n"


CSS = """
:root{--ink:#161a24;--mut:#667085;--line:#d6dbe6;--acc:#4361ee;--star:#f59e0b;--bg:#f4f6fb;--card:#fff;--hi-bg:#fff7e6;--hi-line:#f6d58b;--hi-ink:#3a2e10;--j:#3a4256;--tag-bg:#fff3d6;--tag-ink:#8a5a00}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ink:#e8ebf3;--mut:#97a0b5;--line:#2c3447;--bg:#12151d;--card:#1a1f2b;--hi-bg:#2a2412;--hi-line:#5b4a1f;--hi-ink:#f3dca3;--j:#c9d0e0;--tag-bg:#3a2e10;--tag-ink:#f6c969;--acc:#7b93ff}}
:root[data-theme="dark"]{--ink:#e8ebf3;--mut:#97a0b5;--line:#2c3447;--bg:#12151d;--card:#1a1f2b;--hi-bg:#2a2412;--hi-line:#5b4a1f;--hi-ink:#f3dca3;--j:#c9d0e0;--tag-bg:#3a2e10;--tag-ink:#f6c969;--acc:#7b93ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;padding:28px 16px}
h1{font-size:24px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 20px}.hi{background:var(--hi-bg);color:var(--hi-ink);border:1px solid var(--hi-line);border-radius:10px;padding:8px 14px;margin:0 0 20px;font-size:14px}
.flow{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:26px 34px;padding-right:18px}
.step{position:relative}
.step:not(:last-child)::after{content:"➜";position:absolute;right:-27px;top:50%;transform:translateY(-50%);color:var(--acc);font-size:22px}
.box{height:100%;background:var(--card);border:1.5px solid var(--line);border-radius:14px;padding:13px 15px;position:relative;box-shadow:0 1px 3px rgba(20,30,60,.06)}
.box.star{border-color:var(--star);box-shadow:0 0 0 3px rgba(245,158,11,.16)}
.n{position:absolute;top:-12px;left:14px;background:var(--acc);color:#fff;border-radius:999px;font-weight:700;font-size:13px;padding:2px 10px}
.star .n{background:var(--star)}.t{font-weight:700;font-size:16px;margin:2px 0 6px}
.j{font-size:12.5px;color:var(--j);margin:0 0 6px}.j b{color:var(--acc)}.sh{font-size:12.5px;color:var(--mut);margin:0 0 6px}
.say{font-size:13px;border-top:1px dashed var(--line);padding-top:6px;color:var(--j)}.meta{margin-top:7px;font-size:12px;color:var(--mut)}
.tag{display:inline-block;background:var(--tag-bg);color:var(--tag-ink);border-radius:6px;padding:1px 7px;font-size:11.5px;font-weight:600;margin-top:5px}
"""


INTERACTIVE_CSS = """
.ctl{margin-top:9px;display:flex;flex-wrap:wrap;gap:5px;align-items:center}
.ctl button{font:inherit;font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);background:transparent;color:var(--ink);cursor:pointer}
.ctl button.on[data-d=keep]{background:#e4f6ec;border-color:#7ccf9d;color:#14633a}
.ctl button.on[data-d=change]{background:#fff3d6;border-color:#f0c35a;color:#8a5a00}
.ctl button.on[data-d=remove]{background:#fde8e8;border-color:#f09a9a;color:#9b1c1c}
.ctl input{flex:1 1 100%;font:inherit;font-size:12.5px;padding:5px 8px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--ink);display:none}
.ctl.open input{display:block}
.thumb{width:100%;border-radius:8px;border:1px solid var(--line);margin:0 0 8px;display:block}
.bar{position:sticky;bottom:0;margin:26px -12px 0;padding:14px 12px;background:var(--bg);border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start}
.bar textarea{flex:1 1 320px;min-height:52px;font:inherit;font-size:14px;padding:8px 10px;border-radius:10px;border:1px solid var(--line);background:transparent;color:var(--ink)}
.bar button{font:inherit;font-weight:650;padding:9px 18px;border-radius:10px;border:0;cursor:pointer}
#approve{background:var(--acc);color:#fff}#send{background:transparent;border:1px solid var(--acc);color:var(--acc)}
.bar button:disabled{opacity:.45;cursor:not-allowed}#msg{flex:1 1 100%;font-size:13px;color:var(--mut)}
@media (prefers-color-scheme:dark){.ctl button.on[data-d=keep]{background:#12331f;color:#9be3b8}.ctl button.on[data-d=change]{background:#3a2e10;color:#f6c969}.ctl button.on[data-d=remove]{background:#3a1717;color:#f4a3a3}}
"""

INTERACTIVE_JS = """
(function(){
  var FP = %(fp)s, N = %(n)d, state = {}, db = null;
  var $ = function(s, r){ return (r || document).querySelector(s); }, $$ = function(s, r){ return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  function summary(status){
    var secs = {}; Object.keys(state).forEach(function(k){ if (state[k].decision !== 'keep' || state[k].note) secs[k] = state[k]; });
    return {status: status, fingerprint: FP, sections: secs, general: $('#gen').value.trim(), at: new Date().toISOString()};
  }
  function refresh(){
    var changes = Object.keys(state).filter(function(k){ return state[k].decision !== 'keep'; }).length;
    $('#approve').disabled = changes > 0;
    $('#send').disabled = changes === 0 && !$('#gen').value.trim() && !Object.keys(state).some(function(k){ return state[k].note; });
  }
  $$('.ctl').forEach(function(c){
    var n = c.getAttribute('data-n'); state[n] = {decision: 'keep', note: ''};
    $$('button', c).forEach(function(b){ b.onclick = function(){
      state[n].decision = b.getAttribute('data-d');
      $$('button', c).forEach(function(x){ x.classList.toggle('on', x === b); });
      c.classList.toggle('open', state[n].decision !== 'keep'); refresh(); }; });
    $('input', c).oninput = function(e){ state[n].note = e.target.value; refresh(); };
  });
  $('#gen').oninput = refresh; refresh();
  async function send(status){
    var data = summary(status), msg = $('#msg');
    if (db) {
      try { await db.doc('plan/decisions').set(data); msg.textContent = status === 'approved' ? 'Approved. Tell Claude: "plan approved".' : 'Sent. Tell Claude: "I sent my changes".'; return; }
      catch (e) { msg.textContent = 'Could not save here (' + ((e && e.code) || 'error') + '). Copying the answer instead.'; }
    }
    var text = (status === 'approved' ? 'PLAN APPROVED (' + FP + ')' : 'PLAN CHANGES (' + FP + ')') + '\\n' + JSON.stringify(data, null, 1);
    try { await navigator.clipboard.writeText(text); msg.textContent = 'Copied. Paste it to Claude in the chat.'; } catch (e) { $('#gen').value = text; msg.textContent = 'Copy this text and paste it to Claude in the chat.'; }
  }
  $('#approve').onclick = function(){ send('approved'); };
  $('#send').onclick = function(){ send('changes'); };
  if (window.claude && claude.use) { claude.use('db').then(function(d){ db = d; }).catch(function(){}); }
})();
"""


def board_html(script, thumbs=None, interactive=True):
    """The plan page. `thumbs` {section number: data-URI}. Interactive: keep / change / remove per section, a note box and
    Approve / Send changes; answers go to the artifact's database (doc plan/decisions) for Claude to read, or are copied."""
    thumbs = thumbs or {}
    secs = sections(script)
    total = sum(s["seconds"] for s in secs)
    e = html.escape
    boxes = []
    for s in secs:
        jump = "".join("<div class='j'>&#8594; <b>%s</b></div>" % e(j) for j in s["jump"])
        shows = "<div class='sh'>shows: %s</div>" % e(", ".join(s["shows"])) if s["shows"] else ""
        star = "<div class='tag'>&#9733; %s</div>" % e(" / ".join(s["stars"])) if s["stars"] else ""
        meta = "~%ds%s" % (s["seconds"], " &middot; " + e(" + ".join(s["speakers"])) if s["speakers"] else "")
        img = "<img class='thumb' alt='' src='%s'>" % thumbs[s["n"]] if s["n"] in thumbs else ""
        ctl = ("<div class='ctl' data-n='%d'><button data-d='keep' class='on'>Keep</button><button data-d='change'>Change</button>"
               "<button data-d='remove'>Remove</button><input placeholder='What should change?'></div>" % s["n"]) if interactive else ""
        boxes.append("<div class='step'><div class='box%s'><span class='n'>%d</span><div class='t'>%s</div>%s%s%s"
                     "<div class='say'>%s</div>%s<div class='meta'>%s</div>%s</div></div>"
                     % (" star" if s["stars"] else "", s["n"], e(s["title"]), img, jump, shows,
                        e(s["says"]) if s["says"] else "&nbsp;", star, meta, ctl))
    highs = script.get("highlights") or []
    hi = "<div class='hi'>&#9733; Highlights you asked for: %s</div>" % e("; ".join(str(h) for h in highs)) if highs else ""
    bar = ("<div class='bar'><textarea id='gen' placeholder='Anything else to change? (optional)'></textarea>"
           "<button id='approve'>Approve plan</button><button id='send'>Send changes</button><div id='msg'></div></div>") if interactive else ""
    js = "<script>%s</script>" % (INTERACTIVE_JS % {"fp": json.dumps(fingerprint(script)), "n": len(secs)}) if interactive else ""
    sub = ("The plan: %d sections, about %d:%02d. Mark each section Keep, Change or Remove, then approve or send your changes. "
           "Nothing is voiced or recorded until you approve." % (len(secs), total // 60, total % 60))
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Video Plan</title>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'><style>%s%s</style></head><body>"
            "<h1>%s</h1><p class='sub'>%s</p>%s<div class='flow'>%s</div>%s%s</body></html>"
            % (CSS, INTERACTIVE_CSS if interactive else "", e(script.get("title", "Video plan")), e(sub), hi, "".join(boxes), bar, js))


def thumbnails(script, shots_dir, width=360):
    """{section number: data-URI} from the draft's screenshots (shots/NN-name.png), matched by the view a section opens."""
    if not shots_dir or not os.path.isdir(shots_dir):
        return {}
    try:
        from PIL import Image
    except ImportError:
        return {}
    import base64
    import io
    files = [f for f in os.listdir(shots_dir) if f.lower().endswith(".png") and not f.lower().endswith("-full.png")]
    out = {}
    for s in sections(script):
        key = re.sub(r"[^a-z0-9]+", "", _view_name(s["view"]).lower()) if s["view"] else ""
        if not key:
            continue
        for f in files:
            if key in re.sub(r"[^a-z0-9]+", "", f.lower()):
                im = Image.open(os.path.join(shots_dir, f)).convert("RGB")
                im.thumbnail((width, width * 3))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=70)
                out[s["n"]] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
                break
    return out


def approval_path(out):
    return out + ".approved.json"


def check_approval(script, script_path):
    """None when the build may go ahead; else the reason. A project that never made a plan board is not held up."""
    out = os.path.join(os.path.dirname(os.path.abspath(script_path)), "plan")
    if not os.path.exists(out + ".outline.md"):
        return None
    try:
        with open(approval_path(out), encoding="utf-8") as f:
            ok = json.load(f).get("fingerprint")
    except (OSError, ValueError):
        return "the plan has not been approved yet: show %s.html to the user and, once they agree, run plan_board.py --approve" % out
    if ok != fingerprint(script):
        return "the storyboard's flow changed after the plan was approved: show the new plan (plan_board.py) and get approval again"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--script", required=True)
    ap.add_argument("--out", default=None, help="path prefix, default <script folder>/plan")
    ap.add_argument("--approve", action="store_true", help="record that the user approved the plan as it is now")
    ap.add_argument("--shots", default=None, help="folder of draft_storyboard screenshots for the thumbnails (default <script folder>/shots)")
    ap.add_argument("--static", action="store_true", help="no Keep/Change/Remove buttons (a read-only page)")
    a = ap.parse_args()
    with open(a.script, encoding="utf-8") as f:
        script = json.load(f)
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.script)), "plan")
    if a.approve:
        with open(approval_path(out), "w", encoding="utf-8") as f:
            json.dump({"fingerprint": fingerprint(script), "sections": len(sections(script))}, f)
        print("plan approved (%s); the build may start" % fingerprint(script))
        return
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out + ".html", "w", encoding="utf-8") as f:
        f.write(board_html(script, thumbnails(script, a.shots or os.path.join(os.path.dirname(os.path.abspath(a.script)), "shots")),
                           interactive=not a.static))
    with open(out + ".outline.md", "w", encoding="utf-8") as f:
        f.write(outline(script))
    if os.path.exists(approval_path(out)):
        os.remove(approval_path(out))            # a new plan is a new question
    print(outline(script))
    print("plan page: %s.html\nShow it to the user (publish it as an artifact in Claude's window). Build only after they approve: "
          "plan_board.py --script %s --approve" % (out, a.script))


if __name__ == "__main__":
    main()
