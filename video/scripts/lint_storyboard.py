#!/usr/bin/env python3
"""Check a storyboard before any voice is made or frame recorded: the rules a good product video follows, as code.

    python lint_storyboard.py --script video/script.json [--source .] [--source ../app/src]

ERRORS stop build_video.py (exit 2): a broken storyboard shape, unknown actions, a card that does not exist, an unknown
tone, TODO text anywhere, secrets (emails, API keys, tokens) in narration, captions, cards or the share caption.

WARNINGS are printed to fix or consciously accept:
  - filler that fits any product ("streamline your workflow", "seamless", "unlock") - unless the product itself says it
  - a weak first line ("Welcome to...", "In this video...") - the first sentence is the hook
  - the app appearing late behind a long card intro; sentences over 30 words; beats where nothing on screen moves
  - cards that leave the screen before their text can be read (~0.3 s per word)
  - numbers and names in narration, captions and cards that appear nowhere in the app or the code (grounding)
  - total length outside the range for the storyboard's tone

Grounding reads page-text.json next to the storyboard (draft_storyboard.py writes it) plus any --source folders or
files. It cannot check capabilities ("exports to PDF"): re-read those against the app yourself.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import env  # noqa: E402

ACTIONS = ("stage", "reveal", "tab", "scroll", "scroll_y", "scroll_sheet", "spot", "hover", "sweep", "click",
           "hscroll", "key", "badge", "eval", "wait", "type", "arrow", "circle", "underline", "highlight", "clear")
MOVING = ("stage", "reveal", "tab", "scroll", "scroll_y", "scroll_sheet", "spot", "hover", "sweep", "click", "hscroll",
          "eval", "type", "arrow", "circle", "underline", "highlight")
LOOK = {"frame": ("window", "browser", "none"), "camera": ("auto", "manual", "off"), "easing": ("spring", "smooth"),
        "chapter_titles": (True, False), "progress_bar": (True, False), "motion_blur": (True, False), "sfx": (True, False),
        "cards": ("auto", "blur", "plain")}
# tone: (shortest s, longest s, app on screen by s, speaking-rate offset % on top of the engine default)
TONES = {
    "explainer": (30, 900, 25, 0),       # the default: calm, clear walkthrough
    "polished": (30, 360, 20, -5),       # premium: fewer beats, longer holds, restraint
    "launch": (20, 90, 10, 6),           # short and energetic: hook, the product working, payoff
    "tutorial": (45, 1500, 30, -6),      # step by step: every click shown, slower
}
LEAD_TAIL = 0.85        # record.py LEAD + TAIL around every sentence
LEAD = 0.30             # record.py: narration starts this long after its beat; the rest is the beat's "pause"
MOODS = ("neutral", "calm", "warm", "serious", "curious", "happy", "excited")     # voices.MOODS
VOICE_KEYS = ("engine", "voice", "rate", "mood", "finish", "sample")
CAST_KEYS = ("name", "engine", "voice", "rate", "mood", "sample", "color")
FINISHES = ("warm", "clean", "broadcast", "off")
OUTRO_HOLD = 2.2        # record.py holds the last frame this long
EST_WPM = 160           # before the voice exists, assume a comfortable narration pace
READ_S_PER_WORD = 0.3   # time a viewer needs per word of on-screen text
FILLER = [r"streamlin\w* (your|the) workflow", r"\bsupercharg\w*", r"\bunlock\w*", r"\belevat\w*", r"\bseamless(ly)?\b",
          r"game[- ]chang\w*", r"\brevolutioni[sz]\w*", r"cutting[- ]edge", r"next[- ]level", r"\bempower\w*",
          r"\bleverag\w*", r"best[- ]in[- ]class", r"world[- ]class", r"one[- ]stop[- ]shop", r"fast[- ]paced world",
          r"\bsynerg\w*", r"harness(ing)? the power", r"at your fingertips", r"\bbuckle up\b", r"look no further"]
WEAK_OPEN = re.compile(r"^\s*(hi|hello|hey)\b|^\s*welcome( back)? to\b|^\s*in this (video|walkthrough|tutorial|demo)\b"
                       r"|^\s*today,? (we|i)\b|^\s*let'?s (take a look|get started|dive in)\b|^\s*so,?\s", re.I)
SECRETS = [(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "an email address"),
           (r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b", "an API key"), (r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "a GitHub token"),
           (r"\bAKIA[0-9A-Z]{16}\b", "an AWS key"), (r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b", "a Slack token"),
           (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b", "a JWT")]
SOURCE_EXT = (".html", ".htm", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".astro", ".py", ".rb", ".go", ".rs",
              ".java", ".kt", ".swift", ".php", ".md", ".mdx", ".txt", ".rst", ".json", ".yml", ".yaml", ".toml", ".csv")
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", ".nuxt", "out", "venv", ".venv", "__pycache__", "coverage",
             "secrets", "credentials", ".cache", "target", "vendor"}
SKIP_FILE = re.compile(r"^\.env|\.(pem|key|p12|pfx)$|^id_[rd]sa|lock\.|\.lock$|-lock\.json$|service[-_]account", re.I)
NOT_NAMES = {"I", "I'm", "I'll", "I've", "I'd", "OK", "Okay"}

_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
NUM_RE = re.compile(r"(?<![\w.])[$€£]?\d[\d,]*(?:\.\d+)?(?:\s?(?:%|percent\b|[kKmMbB]\b|x\b))?")


# ---------------------------------------------------------------- numbers and names (grounding)

def _num_value(tok):
    """'$12.4k' -> [12.4, 12400.0]; '4.1%' -> [4.1]; '12,400' -> [12400.0]. Both raw and scaled forms, for matching."""
    m = re.match(r"[$€£]?([\d,]*\d(?:\.\d+)?)\s*(%|percent|[kKmMbB]|x)?", tok.strip())
    if not m:
        return []
    v = float(m.group(1).replace(",", ""))
    scale = {"k": 1e3, "m": 1e6, "b": 1e9}.get((m.group(2) or "").lower(), 1)
    return [v, v * scale] if scale != 1 else [v]


def spoken_numbers(text):
    """Numbers written out in words: 'twelve point four thousand' -> 12400.0. Yields (phrase, value)."""
    words = re.findall(r"[a-z]+", text.lower().replace("-", " "))
    run = []
    for w in words + ["."]:
        if w in _UNITS or w in _TENS or w in _SCALES or w == "hundred" or (run and w in ("point", "and")):
            run.append(w)
            continue
        while run and run[-1] in ("point", "and"):
            run.pop()
        if run:
            total, cur, dec, in_dec = 0.0, 0.0, "", False
            for t in run:
                if t == "and":
                    continue
                if t == "point":
                    in_dec = True
                elif in_dec and t in _UNITS and _UNITS[t] < 10:
                    dec += str(_UNITS[t])
                elif t in _UNITS:
                    cur += _UNITS[t]
                elif t in _TENS:
                    cur += _TENS[t]
                elif t == "hundred":
                    cur = (cur or 1) * 100
                else:
                    total += (cur + (float("0." + dec) if dec else 0)) * _SCALES[t]
                    cur, dec, in_dec = 0.0, "", False
            yield " ".join(run), total + cur + (float("0." + dec) if dec else 0)
        run = []


def number_claims(text):
    """[(as written, [values])] for numbers worth checking: skip small counts (one to ten), which are rarely claims."""
    out = []
    for m in NUM_RE.finditer(text):
        vals = _num_value(m.group(0))
        plain_small = vals and vals[0] <= 10 and float(vals[0]).is_integer() and not re.search(r"[%.kKmMbBx]|percent", m.group(0))
        if vals and not plain_small:
            out.append((m.group(0).strip(), vals))
    for phrase, v in spoken_numbers(text):
        if v > 10 or not float(v).is_integer():
            out.append((phrase, [v]))
    return out


def name_claims(text):
    """Capitalised names in the middle of a sentence ('the Weekly Digest card') and anything in double quotes."""
    names = set(re.findall(r"[\"“]([^\"”]{3,40})[\"”]", text))
    for m in re.finditer(r"(?<=[a-z0-9,;:)]\s)([A-Z][\w'&.+-]*(?:\s+(?:of |for |and )?[A-Z][\w'&.+-]*)*)", text):
        n = m.group(1).rstrip(".,'")
        if n not in NOT_NAMES and len(n) > 1:
            names.add(n)
    return sorted(names)


def load_corpus(script_path, sources):
    """All text the video may quote: the app's rendered text (page-text.json) and the project's source files."""
    parts, used = [], []
    if script_path:
        pt = os.path.join(os.path.dirname(os.path.abspath(script_path)), "page-text.json")
        if os.path.exists(pt):
            parts.append(open(pt, encoding="utf-8").read())
            used.append("page-text.json")
    budget = 40 * 1024 * 1024
    for src in sources or ():
        files = [src] if os.path.isfile(src) else []
        for root, dirs, names in os.walk(src) if os.path.isdir(src) else ():
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            files += [os.path.join(root, n) for n in names if n.lower().endswith(SOURCE_EXT) and not SKIP_FILE.search(n)]
        for f in files:
            try:
                if os.path.getsize(f) > 2 * 1024 * 1024 or budget <= 0:
                    continue
                txt = open(f, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            budget -= len(txt)
            parts.append(txt)
        used.append(src)
    if not parts:
        return None, used
    text = re.sub(r"\s+", " ", " ".join(parts))
    nums = set()
    for m in NUM_RE.finditer(text):
        nums.update(round(v, 4) for v in _num_value(m.group(0)))
    for _, v in spoken_numbers(text):
        nums.add(round(v, 4))
    return {"text": text.lower(), "nums": nums}, used


def _grounded_num(vals, corpus):
    return any(abs(v - c) <= max(0.01, abs(c) * 0.01) for v in vals for c in corpus["nums"])


# ---------------------------------------------------------------- the checks

def _strip_private(x):
    if isinstance(x, dict):
        return {k: _strip_private(v) for k, v in x.items() if not str(k).startswith("_")}
    if isinstance(x, list):
        return [_strip_private(v) for v in x]
    return x


def card_fields(spec):
    """Every separate piece of text on a card. Kept apart so a name never runs from one field into the next."""
    if not isinstance(spec, dict):
        return []
    bits = [spec.get(k, "") for k in ("eyebrow", "title", "sub", "cmd", "url")]
    for i in spec.get("items", []):
        if isinstance(i, dict):
            bits += [i.get("k", ""), i.get("v", "")]
    bits += [str(c) for c in spec.get("chips", [])]
    return [re.sub(r"\*\*", "", str(b)) for b in bits if b]


def card_text(spec):
    return " ".join(card_fields(spec))


def beat_seconds(beats, durations):
    """Estimated (or, with durations, real) length of every beat on screen."""
    out = []
    for i, b in enumerate(beats):
        speech = durations[i] if durations else len(str(b.get("say", "")).split()) / EST_WPM * 60
        waits = sum(a.get("wait", 0) for a in b.get("do", []) if isinstance(a, dict) and isinstance(a.get("wait"), (int, float)))
        p = b.get("pause")
        around = LEAD + p if isinstance(p, (int, float)) and not isinstance(p, bool) else LEAD_TAIL
        out.append(max(speech + around, waits))
    return out


def check_voices(script, script_path, E, W, N):
    """The "voice" defaults, the "cast" of speakers, and each beat's "who", "mood" and "pause"."""
    vdef, cast, beats = script.get("voice"), script.get("cast"), script["beats"]
    base = os.path.dirname(os.path.abspath(script_path)) if script_path else "."
    if vdef is not None and not isinstance(vdef, dict):
        E.append("\"voice\" must be an object like {\"engine\": \"edge\", \"mood\": \"warm\"}")
        vdef = {}
    vdef = vdef or {}
    for k in vdef:
        if k not in VOICE_KEYS:
            E.append("voice.%s is not a voice setting (use %s)" % (k, ", ".join(VOICE_KEYS)))
    if vdef.get("mood") and vdef["mood"] not in MOODS:
        E.append("voice.mood %r: use one of %s" % (vdef["mood"], ", ".join(MOODS)))
    if vdef.get("finish") and vdef["finish"] not in FINISHES:
        E.append("voice.finish %r: use one of %s" % (vdef["finish"], ", ".join(FINISHES)))
    if cast is not None and (not isinstance(cast, dict) or not cast):
        E.append("\"cast\" must be an object of speakers, e.g. {\"host\": {\"name\": \"Alex\"}, \"guest\": {\"name\": \"Maya\"}}")
        cast = {}
    cast = cast or {}
    names = set()
    samples = [(where, s) for where, s in [("voice", vdef.get("sample"))] + [("cast.%s" % k, (c or {}).get("sample"))
                                                                           for k, c in cast.items() if isinstance(c, dict)] if s]
    for sid, c in cast.items():
        if not isinstance(c, dict):
            E.append("cast.%s must be an object like {\"name\": \"Maya\", \"voice\": \"...\"}" % sid)
            continue
        for k in c:
            if k not in CAST_KEYS:
                E.append("cast.%s.%s is not a speaker setting (use %s)" % (sid, k, ", ".join(CAST_KEYS)))
        if c.get("mood") and c["mood"] not in MOODS:
            E.append("cast.%s.mood %r: use one of %s" % (sid, c["mood"], ", ".join(MOODS)))
        if c.get("color") and not re.fullmatch(r"#[0-9a-fA-F]{6}", str(c["color"])):
            E.append("cast.%s.color must be #rrggbb" % sid)
        names.add(str(c.get("name") or sid.replace("_", " ").title()))
    for where, s in samples:
        p = s if os.path.isabs(s) else os.path.join(base, s)
        if not os.path.exists(p):
            E.append("%s.sample %s does not exist (paths are relative to the storyboard's folder)" % (where, s))
        elif not os.path.exists(os.path.splitext(p)[0] + ".consent.json"):
            E.append("%s.sample %s has no consent record: prepare it with scripts/prepare_voice.py --speaker ... --consent ..." % (where, s))
    if len(cast) > 1 and len(names) < len(cast):
        E.append("two speakers in \"cast\" share a name: captions could not tell them apart")
    said = {}
    for i, b in enumerate(beats):
        if not isinstance(b, dict):
            continue
        where = "beat %d" % (i + 1)
        who = b.get("who")
        if who is not None and who not in cast:
            E.append("%s: \"who\": %r is not in \"cast\"%s" % (where, who, " (%s)" % ", ".join(cast) if cast else " - add a cast first"))
        said[who or next(iter(cast), None)] = said.get(who or next(iter(cast), None), 0) + 1
        if b.get("mood") is not None and b["mood"] not in MOODS:
            E.append("%s: mood %r: use one of %s" % (where, b["mood"], ", ".join(MOODS)))
        p = b.get("pause")
        if p is not None and (isinstance(p, bool) or not isinstance(p, (int, float)) or not 0.1 <= p <= 3):
            E.append("%s: \"pause\" is the silence after this sentence in seconds, 0.1 to 3" % where)
    for sid in cast:
        if len(cast) > 1 and not said.get(sid):
            W.append("cast.%s never speaks (no beat has \"who\": %r)" % (sid, sid))
    if len(cast) > 1:
        N.append("conversation: %s; a reply sounds natural ~0.3 s after a question (\"pause\": 0.3 on the question)"
                 % ", ".join("%s %d line(s)" % (sid, said.get(sid, 0)) for sid in cast))
    moods = {b.get("mood") for b in beats if isinstance(b, dict) and b.get("mood")}
    if moods - {"neutral"}:
        N.append("moods %s: real emotion with the chatterbox voice; with edge or kokoro only pace and pitch change a little"
                 % ", ".join(sorted(moods)))
    return names


def lint(script, script_path=None, sources=(), durations=None, lexicon=None, parts=("text", "timing")):
    """Returns (errors, warnings, notes), each a list of strings. `durations`: real clip seconds per beat, if known."""
    E, W, N = [], [], []
    if not isinstance(script, dict) or not isinstance(script.get("beats"), list) or not script["beats"]:
        return ["the storyboard needs a \"beats\" list (see references/storyboard.md)"], W, N
    beats, stages = script["beats"], script.get("stages") or {}
    tone = script.get("tone", "explainer")
    if tone not in TONES:
        E.append("unknown tone %r: use one of %s" % (tone, ", ".join(TONES)))
        tone = "explainer"
    look = script.get("look") or {}
    for key, allowed in LOOK.items():
        if key in look and look[key] not in allowed:
            E.append("look.%s %r: use one of %s" % (key, look[key], ", ".join(allowed)))

    if "text" in parts:
        speaker_names = check_voices(script, script_path, E, W, N)
        corpus, used = load_corpus(script_path, sources)
        def texts():        # (where, text) for everything the viewer hears or reads
            for i, b in enumerate(beats):
                if isinstance(b, dict):
                    yield "beat %d" % (i + 1), str(b.get("cap") or b.get("say", ""))
            for name, spec in stages.items():
                for field in card_fields(spec):
                    yield "card %r" % name, field
            if script.get("share"):
                yield "share caption", str(script["share"])

        # shape, actions, TODO
        for i, b in enumerate(beats):
            where = "beat %d" % (i + 1)
            if not isinstance(b, dict):
                E.append("%s: must be an object with \"say\" and \"do\"" % where); continue
            if not str(b.get("say", "")).strip():
                E.append("%s: \"say\" is empty" % where)
            if not isinstance(b.get("do", []), list):
                E.append("%s: \"do\" must be a list of actions" % where); continue
            for a in b.get("do", []):
                keys = [k for k in a if k in ACTIONS] if isinstance(a, dict) else []
                if not keys:
                    E.append("%s: unknown action %s (known: %s)" % (where, json.dumps(a)[:60], ", ".join(ACTIONS))); continue
                if "stage" in a and a["stage"] and a["stage"] not in stages:
                    E.append("%s: card %r is not defined in \"stages\"" % (where, a["stage"]))
                if "type" in a and not isinstance(a.get("text"), str):
                    E.append("%s: a \"type\" action needs \"text\" (what to type)" % where)
                if "arrow" in a and a.get("from", "auto") not in ("auto", "left", "right", "top", "bottom"):
                    E.append("%s: arrow \"from\" must be auto, left, right, top or bottom" % where)
                z = a.get("zoom")
                if "zoom" in a and not (z is False or (isinstance(z, (int, float)) and not isinstance(z, bool) and 1 <= z <= 3)):
                    E.append("%s: \"zoom\" must be a number from 1 to 3, or false for no zoom" % where)
            if "TODO" in json.dumps(_strip_private(b), ensure_ascii=False):
                E.append("%s: still has TODO text" % where)
        for name, spec in stages.items():
            if "TODO" in json.dumps(_strip_private(spec), ensure_ascii=False):
                E.append("card %r: still has TODO text" % name)
        for key in ("title", "share"):
            if "TODO" in str(script.get(key, "")):
                E.append("%s: still has TODO text" % key)

        # secrets and filler
        for where, t in texts():
            for rx, what in SECRETS:
                if re.search(rx, t):
                    E.append("%s: contains %s - never say or show secrets or personal data" % (where, what))
            for rx in FILLER:
                m = re.search(rx, t, re.I)
                if m and not (corpus and m.group(0).lower() in corpus["text"]):
                    W.append("%s: filler %r fits any product - say what this one actually does" % (where, m.group(0)))

        # the hook, sentence length, motion
        first = next((b for b in beats if isinstance(b, dict) and b.get("say")), None)
        if first and WEAK_OPEN.search(first["say"]):
            W.append("beat 1: weak opening %r - open with the problem or the payoff, it decides whether people stay"
                     % WEAK_OPEN.search(first["say"]).group(0).strip())
        on_card = None
        for i, b in enumerate(beats):
            if not isinstance(b, dict):
                continue
            n = len(str(b.get("say", "")).split())
            if n > 30:
                W.append("beat %d: %d words - split it (10-30 words per sentence reads best)" % (i + 1, n))
            acts = [a for a in b.get("do", []) if isinstance(a, dict)]
            for a in acts:
                if "stage" in a:
                    on_card = a["stage"]
                if "reveal" in a and not on_card:
                    W.append("beat %d: \"reveal\" with no card on screen does nothing" % (i + 1))
            if not on_card and not any(k in a for a in acts for k in MOVING):
                W.append("beat %d: nothing moves on screen during this sentence - add a spotlight, scroll, hover or type" % (i + 1))

        # pronunciation hints
        if lexicon is not None:
            import tts
            acr = sorted({w for b in beats if isinstance(b, dict) for w in re.findall(r"\b[A-Z]{2,}s?\b", str(b.get("say", "")))
                          if w not in lexicon and w != "TODO" and not tts.is_auto_spoken(w) and w not in tts.CAPS_WORDS})
            if acr:
                N.append("said as written, not spelled out: %s (add a lexicon entry if the voice gets it wrong)" % ", ".join(acr[:12]))

        # grounding
        if corpus is None:
            N.append("grounding not checked: no page-text.json next to the storyboard and no --source")
        else:
            loose = []
            for where, t in texts():
                for shown, vals in number_claims(t):
                    if not _grounded_num(vals, corpus):
                        loose.append("%s: %r" % (where, shown))
                for nm in name_claims(t):
                    if nm.lower() not in corpus["text"] and nm not in speaker_names:   # "Thanks, Maya" is no claim
                        loose.append("%s: %r" % (where, nm))
            for x in loose[:15]:
                W.append("not found in the app or code - confirm it is true or reword: %s" % x)
            if len(loose) > 15:
                W.append("...and %d more unverified numbers/names" % (len(loose) - 15))
            N.append("grounding checked against %s; capabilities (\"exports to PDF\") still need your own read" % ", ".join(used))
        if not script.get("share"):
            N.append("no \"share\" caption: add 1-3 sentences for the post; build_video writes it into description.md")

    if "timing" in parts and all(isinstance(b, dict) and isinstance(b.get("do", []), list) for b in beats):
        lo, hi, app_by, _ = TONES[tone]
        secs = beat_seconds(beats, durations)
        total = sum(secs) + OUTRO_HOLD
        how = "" if durations else " (estimated at %d words/min)" % EST_WPM
        N.append("length about %d:%02d%s, tone %s" % (total // 60, total % 60, how, tone))
        if not lo <= total <= hi:
            W.append("length %d s is outside %d-%d s for tone %r - %s" % (total, lo, hi, tone, "cut beats" if total > hi else "add beats or pick another tone"))
        on_card, t, app_at, shows = None, 0.0, None, []   # shows: [card, first beat, seconds on screen]
        for i, b in enumerate(beats):
            for a in (x for x in b.get("do", []) if isinstance(x, dict) and "stage" in x):
                on_card = a["stage"]
                if on_card:
                    shows.append([on_card, i, 0.0])
            if on_card and shows:
                shows[-1][2] += secs[i] + (OUTRO_HOLD if i == len(beats) - 1 else 0)
            if not on_card and app_at is None:
                app_at = t
            t += secs[i]
        if app_at is not None and app_at > app_by:
            W.append("the app first appears at %d s - viewers leave during long intros; show the product by ~%d s for tone %r"
                     % (app_at, app_by, tone))
        for name, i, shown in shows:
            # the voice usually says much of what the card shows, so warn only when it is clearly too short
            need = len(card_text(stages.get(name)).split()) * READ_S_PER_WORD
            if shown < need * 0.85:
                W.append("card %r (beat %d) is on screen %.1f s but needs ~%.1f s to read - trim its text or lengthen the sentence"
                         % (name, i + 1, shown, need))
    return E, W, N


def report(E, W, N, header=None, out=print):
    if header:
        out(header)
    for e in E:
        out("  ERROR  " + e)
    for w in W:
        out("  warn   " + w)
    for n in N:
        out("  note   " + n)
    out("  storyboard check: %d error(s), %d warning(s)" % (len(E), len(W)))


def main():
    env.safe_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--script", required=True, help="storyboard JSON")
    ap.add_argument("--source", action="append", default=[], help="project folder or file the claims must come from (repeatable)")
    ap.add_argument("--lexicon", default=None, help="your pronunciation lexicon, to skip acronyms it already covers")
    a = ap.parse_args()
    try:
        with open(a.script, encoding="utf-8") as f:
            script = json.load(f)
    except (OSError, ValueError) as exc:
        print("  ERROR  cannot read %s: %s" % (a.script, exc))
        return 2
    import tts
    lex = tts.load_lexicon()
    if a.lexicon:
        lex.update(tts.load_lexicon(a.lexicon))
    E, W, N = lint(script, a.script, a.source, lexicon=lex)
    report(E, W, N, "checking %s (%d beats)" % (a.script, len(script.get("beats", []))))
    return 2 if E else 0


if __name__ == "__main__":
    sys.exit(main())
