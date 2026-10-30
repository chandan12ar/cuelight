#!/usr/bin/env python3
"""Listen to the narration: transcribe every sentence with Whisper and compare it with what the voice was asked to say.

    python check_narration.py --work video/work [--model base.en] [--strict]

Claude cannot hear the video; this can. It runs the speech-to-text model Whisper (faster-whisper, offline after a one-
time ~140 MB model download) on each sentence's audio clip and flags:
  - words that came out wrong, swallowed or slurred (usually a name or term the voice mispronounces)
  - a sentence cut off at the end, or extra words / noise
Every flagged sentence shows what was heard next to what was meant. The usual fix is a pronunciation in a lexicon
(references/voice.md, --lexicon) and a rebuild: only the changed sentences are voiced again.

Not every flag is a fault: Whisper spells rare product names its own way. Compare "heard" with "meant" and judge.
Needs: python -m pip install faster-whisper   (the build itself does not). Exit 0, or 1 with --strict when anything is flagged.
"""
import argparse
import difflib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import env  # noqa: E402
from lint_storyboard import spoken_numbers  # noqa: E402

LEAD = 0.30
# Whisper often swaps these little words on its own ("below it" -> "below at"); a swap between two of them is not a voice fault
SMALL = {"a", "an", "the", "it", "at", "in", "on", "of", "to", "and", "or", "is", "its", "as", "this", "that", "for", "by", "with"}
SOUNDS_SAME = {("4", "for"), ("4", "fore"), ("2", "to"), ("2", "too"), ("8", "ate"), ("1", "won"), ("their", "there"), ("its", "it")}
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")     # harmless Windows notice from the model download


def canon(text):
    """Lower-case word tokens with every number as one canonical token: 'Twelve point four thousand' -> ['12400']."""
    t = text.lower().replace("%", " percent ").replace("&", " and ")
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)                     # 12,400 -> 12400
    for phrase, value in sorted(spoken_numbers(t), key=lambda p: -len(p[0])):
        v = ("%.4f" % value).rstrip("0").rstrip(".")
        t = re.sub(r"\b%s\b" % re.escape(phrase).replace(r"\ ", r"[\s-]+"), " %s " % v, t, count=1)
    toks = []
    for w in re.findall(r"[a-z0-9]+(?:[.'][a-z0-9]+)*", t):
        w = w.replace("'", "")
        if re.fullmatch(r"\d+(\.\d+)?", w):
            w = ("%.4f" % float(w)).rstrip("0").rstrip(".")
        toks.append(w)
    return toks


def compare(meant, heard):
    """(similarity 0..1, [problems]) for one sentence."""
    sm = difflib.SequenceMatcher(None, meant, heard, autojunk=False)
    wrong, missing, extra = [], [], []
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == "replace":
            for x, y in zip(meant[a0:a1], heard[b0:b1]):
                if (x in SMALL and y in SMALL) or (x, y) in SOUNDS_SAME or (y, x) in SOUNDS_SAME:
                    continue
                if difflib.SequenceMatcher(None, x, y).ratio() < 0.8:     # spelling variants are not errors
                    wrong.append("%s -> %s" % (x, y))
            missing += meant[a0 + (b1 - b0):a1]
            extra += heard[b0 + (a1 - a0):b1]
        elif op == "delete":
            missing += meant[a0:a1]
        elif op == "insert":
            extra += heard[b0:b1]
    problems = []
    if wrong:
        problems.append("came out differently: " + ", ".join(wrong[:6]))
    if missing:
        problems.append("not heard: " + " ".join(missing[:8]))
    if meant and heard and meant[-2:] != heard[-2:] and set(meant[-2:]) - set(heard):
        problems.append("may be cut off at the end")
    if len(extra) > 2:
        problems.append("extra words: " + " ".join(extra[:8]))
    return sm.ratio(), problems


def main():
    env.safe_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="the build's --work folder (voice.json, wav/, rec.json)")
    ap.add_argument("--voice-file", default=None, help="voice json to check (default <work>/voice.json; revoice.py writes voice-<engine>-<voice>.json)")
    ap.add_argument("--model", default="base.en", help="Whisper model: tiny.en (fast), base.en (default), small.en (most accurate), or a local model folder")
    ap.add_argument("--language", default=None, help="language code when the narration is not English (use a multilingual model, e.g. base)")
    ap.add_argument("--threshold", type=float, default=0.85, help="flag sentences whose words match less than this (0..1)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when anything is flagged")
    a = ap.parse_args()
    env.require_tools()
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("narration check skipped: Whisper is not installed. Install it with: %s -m pip install faster-whisper" % sys.executable)
        return 0
    vf = a.voice_file or os.path.join(a.work, "voice.json")
    if not os.path.exists(vf):
        raise SystemExit("no %s: build the video first (build_video.py writes it)" % vf)
    with open(vf, encoding="utf-8") as f:
        voice = json.load(f)
    starts = {}
    rp = os.path.join(a.work, "rec.json")
    if os.path.exists(rp):
        with open(rp, encoding="utf-8") as f:
            rec = json.load(f)
        t0 = rec["frames"][0][0]
        starts = {b["i"]: max(0.0, b["start"] - t0 + LEAD) for b in rec["beats"]}
    print("loading Whisper %s (the first run downloads it)..." % a.model)
    try:
        model = WhisperModel(a.model, device="cpu", compute_type="int8")
    except Exception as exc:  # noqa: BLE001
        print("narration check skipped: the Whisper model could not be loaded (%s)." % str(exc).splitlines()[0][:120])
        print("  Offline or Hugging Face blocked? Copy a faster-whisper model folder here and pass --model <folder>.")
        return 0
    lang = a.language or ("en" if a.model.endswith(".en") else None)
    flagged, total_ratio = 0, 0.0
    print("listening to %d sentences (%s voice %s)...\n" % (len(voice["beats"]), voice.get("engine"), voice.get("voice")))
    for b in voice["beats"]:
        segs, _ = model.transcribe(b["wav"], language=lang, beam_size=5, condition_on_previous_text=False, vad_filter=False)
        heard_text = " ".join(s.text.strip() for s in segs).strip()
        heard = canon(heard_text)
        best = max((compare(canon(x), heard) for x in {b["spoken"], b["caption"]}), key=lambda r: r[0])
        ratio, problems = best
        total_ratio += ratio
        if ratio < a.threshold or problems:
            flagged += 1
            at = starts.get(b["i"])
            who = ", ".join(x for x in (b.get("who"), b.get("engine"), b.get("mood")) if x and x != "neutral")
            print("beat %d%s%s  match %d%%" % (b["i"] + 1, "  (%d:%02d)" % (at // 60, at % 60) if at is not None else "",
                                              "  [%s]" % who if who else "", ratio * 100))
            print("   meant: %s" % b["caption"])
            print("   heard: %s" % heard_text)
            for p in problems:
                print("   - %s" % p)
            print()
    n = max(1, len(voice["beats"]))
    print("narration check: %d of %d sentence(s) to listen to again; average match %d%%" % (flagged, n, total_ratio / n * 100))
    if flagged:
        print("Fix mispronounced names with a lexicon (--lexicon), shorten tangled sentences, then rebuild.")
    return 1 if (flagged and a.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
