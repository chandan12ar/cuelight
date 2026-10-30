#!/usr/bin/env python3
"""Pronunciation gate: find every term a voice is likely to get wrong BEFORE the narration is voiced, and make a person
(or Claude, with the user's help) decide on each one, once.

    python pronounce_check.py --script video/script.json [--lexicon yours.json]        # list the unreviewed terms
    python pronounce_check.py --script video/script.json --add "Kubernetes=koo-ber-net-eez" # fix a pronunciation
    python pronounce_check.py --script video/script.json --accept                      # "these are fine as written"

What is flagged (heuristics, no dictionary needed): names and brand words written with a capital in the middle of a
sentence, camelCase words (PyTorch, macOS), words mixing letters and digits (GPT4, x86), long numbers, ALL-CAPS words
of 7+ letters, and file names or paths (app.py, api/v1). Short ALL-CAPS forms (AI, GPU, MCP...) are spelled letter by
letter automatically and never flagged. A term stays flagged until it is in a lexicon (--add writes
<script folder>/lexicon.json, which build_video.py reads) or was accepted (<script folder>/pronunciation.reviewed.json).
build_video.py stops with exit 7 while any term is unreviewed, so a mispronounced product name is a decision, not a
surprise found after a 20-minute build. The voice sample (sample.py) then lets the user hear the risky terms.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# capitalised words every voice already says right (days, months, big names) - not worth a decision
COMMON = {w.lower() for w in (
    "Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April June July August September "
    "October November December English Python Windows Linux Mac Chrome Google Microsoft Anthropic Claude GitHub Excel "
    "Word Slack Zoom Apple Amazon Meta Netflix Reddit Twitter Notion Figma Stripe Shopify Docker Android Safari Firefox "
    "Edge Gmail Outlook Teams Drive Calendar Dashboard Overview Settings Reports Usage Billing Analytics Home Projects "
    "Quality Value Flow Advisor Health Adoption Weekly Models Tools Focus Time"
).split()}


def terms_of(script):
    """{term: [beat numbers]} for everything in the narration that deserves a decision."""
    cast = script.get("cast") or {}
    skip = {str((v or {}).get("name", "")).lower() for v in cast.values()} | COMMON
    found = {}
    alltext = " ".join(str(b.get("say", "")) + " " + str(b.get("cap", "")) for b in script.get("beats", []) if isinstance(b, dict))
    alltext += " " + json.dumps(script.get("stages") or {})
    lower_seen = set(re.findall(r"[a-z][a-z]+", alltext))          # a word also written in lower case is an ordinary word
    for n, b in enumerate(script.get("beats", []), 1):
        if not isinstance(b, dict):
            continue
        text = str(b.get("say", "")).replace("{DATA}", "")
        for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9._/'-]*[A-Za-z0-9]|[A-Za-z0-9]", text):
            w = re.sub(r"['’]s$", "", m.group(0).strip(".'-/"))
            if not w or w.lower() in skip:
                continue
            before = text[:m.start()].rstrip()
            sentence_start = not before or before[-1] in ".?!:\"'(" or before.endswith("—")
            why = None
            if re.search(r"[a-z][A-Z]", w):
                why = "camelCase"
            elif re.search(r"[A-Za-z]", w) and re.search(r"\d", w) and not re.fullmatch(r"\d+(st|nd|rd|th|x|s|k|m)", w.lower()):
                why = "letters and digits"
            elif re.fullmatch(r"\d{4,}", w) and not 1900 <= int(w) <= 2099:
                why = "long number"
            elif re.fullmatch(r"[A-Z]{7,}", w):
                why = "long ALL-CAPS word"
            elif re.search(r"[A-Za-z][._/][A-Za-z]", w):
                why = "file name or path"
            elif w[0].isupper() and w[1:].islower() and len(w) >= 4 and not sentence_start and w.lower() not in lower_seen:
                why = "name"
            if why:
                found.setdefault(w, {"why": why, "beats": []})["beats"].append(n)
    return found


def suggest(term):
    """A starting pronunciation to put in the lexicon: camelCase split into words, digits and dots spoken."""
    t = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", term)
    t = t.replace(".", " dot ").replace("/", " slash ").replace("_", " ")
    return t.lower()


def reviewed_path(script_path):
    return os.path.join(os.path.dirname(os.path.abspath(script_path)), "pronunciation.reviewed.json")


def project_lexicon_path(script_path):
    return os.path.join(os.path.dirname(os.path.abspath(script_path)), "lexicon.json")


def load_reviewed(script_path):
    try:
        with open(reviewed_path(script_path), encoding="utf-8") as f:
            return set(json.load(f).get("terms", []))
    except (OSError, ValueError):
        return set()


def unreviewed(script, script_path, lex):
    """{term: info} still waiting for a decision: not in any lexicon and not accepted."""
    done = set(lex) | load_reviewed(script_path)
    return {t: i for t, i in terms_of(script).items() if t not in done}


def report(items, lex=None):
    import tts
    lex = lex or {}
    lines = []
    for t, i in sorted(items.items(), key=lambda kv: kv[1]["beats"][0]):
        spoken = tts.apply_lexicon(t, lex)
        lines.append("   %-18s (%s, beat %s) will be said as: %s   suggestion: %s"
                     % (t, i["why"], ",".join(str(b) for b in i["beats"][:4]), spoken, suggest(t)))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--script", required=True)
    ap.add_argument("--lexicon", default=None, help="extra lexicon JSON (merged over the default and the project lexicon)")
    ap.add_argument("--add", action="append", default=[], metavar="TERM=SPOKEN", help="write a pronunciation into <script folder>/lexicon.json")
    ap.add_argument("--accept", action="store_true", help="mark every listed term as fine as written (after you checked them)")
    a = ap.parse_args()
    import tts
    with open(a.script, encoding="utf-8") as f:
        script = json.load(f)
    if a.add:
        p = project_lexicon_path(a.script)
        data = {}
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        for item in a.add:
            k, _, v = item.partition("=")
            if not k.strip() or not v.strip():
                raise SystemExit("--add needs TERM=SPOKEN, e.g. --add \"Kubernetes=koo-ber-net-eez\"")
            data[k.strip()] = v.strip()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print("wrote %d pronunciation(s) to %s" % (len(a.add), p))
    lex = tts.load_lexicon()
    if os.path.exists(project_lexicon_path(a.script)):
        lex.update(tts.load_lexicon(project_lexicon_path(a.script)))
    if a.lexicon:
        lex.update(tts.load_lexicon(a.lexicon))
    left = unreviewed(script, a.script, lex)
    if a.accept and left:
        done = load_reviewed(a.script) | set(left)
        with open(reviewed_path(a.script), "w", encoding="utf-8") as f:
            json.dump({"terms": sorted(done)}, f, indent=2)
        print("accepted as written: %s" % ", ".join(sorted(left)))
        return 0
    if left:
        print("%d term(s) the voice may get wrong - decide each before the build:\n%s\n" % (len(left), report(left, lex)))
        print("Fix:  --add \"Term=how it is said\"   (then listen to the voice sample)   or   --accept   when the written form is right.")
        return 7
    print("pronunciation: every risky term is in a lexicon or accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
