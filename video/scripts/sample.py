#!/usr/bin/env python3
"""Voice sample gate: before the full build, voice a few real lines in the real voices, check them by machine, and let the
user listen and say yes. A wrong voice, a noisy voice or a mispronounced term then costs seconds, not a 20-minute build.

    python sample.py --work video/work --approve        # after listening to <work>/sample.wav (refused if the checks failed)

build_video.py makes the sample by itself (the lines are cached, so the full build re-uses them) and stops with exit 8
until it is approved for exactly this voice set-up: engine, voice, speed, mood, finish and pronunciation. Changing any of
them asks again; changing a sentence does not.

The sample holds the first line of each speaker plus the lines with the most risky terms (names, numbers, file names),
up to 4 lines (2 with Chatterbox, which is slow), finished the way the final narration is. Machine checks:
  - listening: Whisper (faster-whisper) transcribes each line; words that came out wrong are listed (needs Whisper)
  - noise: the energy above 6 kHz relative to the whole voice; clean neural voices sit near -17 dB, a hissy voice is
    above -11 dB (fails) - the "noisy Leo" problem is caught here, not by someone's ears after the render
  - pace: words per minute within the comfortable range
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

HISS_WARN, HISS_FAIL = -14.0, -11.0      # dB of the 6 kHz+ band relative to the whole voice (measured: edge clips -16 to -21)
WPM_LOW, WPM_HIGH = 110, 200
MATCH_MIN = 0.85


def fingerprint(plan, lex):
    """What the approval covers: who speaks with which voice, speed, mood and finish, and every pronunciation in force."""
    sp = [(s["id"], s["engine"], s["voice"], s["sample_sha"], s["rate"], s["mood"]) for s in plan["speakers"].values()]
    moods = sorted({(ln["who"], ln["mood"]) for ln in plan["lines"]})
    blob = json.dumps([sp, moods, plan.get("finish"), sorted((lex or {}).items())], sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def pick_lines(plan, script, risky):
    """First line of every speaker, then the lines with the most risky terms; capped (Chatterbox lines are slow)."""
    lines = plan["lines"]
    cap = 2 if any(ln["engine"] == "chatterbox" for ln in lines) else 4
    chosen, seen = [], set()
    for ln in lines:
        if ln["who"] not in seen and len(ln["text"].split()) >= 5:
            seen.add(ln["who"])
            chosen.append(ln)
    score = lambda ln: sum(1 for t in risky if re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(t), str(script["beats"][ln["i"]].get("say", ""))))
    for ln in sorted(lines, key=lambda l: -score(l)):
        if len(chosen) >= cap:
            break
        if score(ln) and ln not in chosen:
            chosen.append(ln)
    return sorted(chosen[:cap], key=lambda l: l["i"])


def _rms_db(path, af=None):
    cmd = ["ffmpeg", "-hide_banner", "-i", path] + (["-af", af + ",astats=metadata=0"] if af else ["-af", "astats=metadata=0"]) + ["-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stderr
    m = re.search(r"RMS level dB:\s*(-?[\d.]+|-inf)", out)
    return float(m.group(1)) if m and m.group(1) != "-inf" else -90.0


def hiss_db(path):
    """Level of the band above 6 kHz relative to the whole clip (dB, more negative = cleaner)."""
    return _rms_db(path, "highpass=f=6000") - _rms_db(path)


def listen(lines, wavs, log):
    """Whisper's transcript of each line -> {i: (match 0..1, [problems], heard)}; {} when Whisper is not available."""
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("base.en", device="cpu", compute_type="int8")
    except Exception as exc:  # noqa: BLE001
        log("  ! no Whisper (%s): the words could not be checked by machine - listen closely, or pip install faster-whisper"
            % (str(exc).splitlines() or ["not installed"])[0][:90])
        return {}
    import check_narration as cn
    res = {}
    for ln, w in zip(lines, wavs):
        segs, _ = model.transcribe(w, language="en", beam_size=5, condition_on_previous_text=False)
        heard = " ".join(s.text.strip() for s in segs).strip()
        ratio, problems = max((cn.compare(cn.canon(x), cn.canon(heard)) for x in {ln["text"], ln["caption"]}), key=lambda r: r[0])
        res[ln["i"]] = (ratio, problems, heard)
    return res


def make(plan, script, lex, work, log=print, check="auto"):
    """Voice the sample, check it, write <work>/sample.wav, sample.json and sample.md; returns the report dict."""
    import narrate
    import tts
    import voices
    os.makedirs(work, exist_ok=True)
    import pronounce_check
    risky = list(pronounce_check.terms_of(script)) + [k for k in (lex or {}) if k in json.dumps(script.get("beats", []))]
    lines = pick_lines(plan, script, risky)
    log("voice sample: %d line(s) in the real voices..." % len(lines))
    sub = dict(plan, lines=lines)
    res = narrate.voice_all(sub, os.path.join(work, "wav"), check=check, log=log)
    wavs = res["wavs"]
    heard = listen(lines, wavs, log) if check != "off" else {}
    items, fails, warns = [], [], []
    for ln, w, d in zip(lines, wavs, res["durations"]):
        wpm = len(ln["text"].split()) / max(d, 0.1) * 60
        hd = hiss_db(w)
        item = {"beat": ln["i"] + 1, "who": ln["name"] or ln["who"], "voice": "%s %s" % (ln["engine"], ln["voice"]), "text": ln["caption"],
                "seconds": round(d, 1), "wpm": round(wpm), "hiss_db": round(hd, 1)}
        if ln["i"] in heard:
            item["match"], item["problems"], item["heard"] = round(heard[ln["i"]][0], 2), heard[ln["i"]][1], heard[ln["i"]][2]
            if heard[ln["i"]][0] < MATCH_MIN or heard[ln["i"]][1]:
                fails.append("beat %d (%s): %s" % (ln["i"] + 1, item["who"], "; ".join(heard[ln["i"]][1]) or "only %d%% of the words heard" % (heard[ln["i"]][0] * 100)))
        if hd > HISS_FAIL:
            fails.append("beat %d (%s): the voice is noisy (%.1f dB above 6 kHz; clean voices are near -17) - use another voice, "
                         "e.g. edge en-US-AndrewMultilingualNeural" % (ln["i"] + 1, item["who"], hd))
        elif hd > HISS_WARN:
            warns.append("beat %d (%s): a little hiss (%.1f dB above 6 kHz)" % (ln["i"] + 1, item["who"], hd))
        if not WPM_LOW <= wpm <= WPM_HIGH and len(ln["text"].split()) >= 8:
            warns.append("beat %d (%s): %d words/min is outside %d-%d" % (ln["i"] + 1, item["who"], wpm, WPM_LOW, WPM_HIGH))
        items.append(item)
    # one file to listen to: the lines with a pause between, finished like the final narration
    lst = os.path.join(work, "sample.txt")
    sil = os.path.join(work, "sample-gap.wav")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.6", sil], check=True)
    with open(lst, "w", encoding="utf-8") as f:
        for w in wavs:
            f.write("file '%s'\nfile '%s'\n" % (os.path.abspath(w).replace("\\", "/"), os.path.abspath(sil).replace("\\", "/")))
    out = os.path.join(work, "sample.wav")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-af", voices.audio_filter(plan.get("finish", voices.DEFAULT_FINISH)), "-ar", "24000", out], check=True)
    rep = {"fingerprint": fingerprint(plan, lex), "wav": out, "items": items, "fails": fails, "warns": warns,
           "listened": bool(heard), "voices": voices.describe(plan)}
    with open(os.path.join(work, "sample.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1)
    with open(os.path.join(work, "sample.md"), "w", encoding="utf-8") as f:
        f.write(describe(rep))
    return rep


def describe(rep):
    L = ["Voice sample: %s" % rep["wav"], ""]
    L += ["  " + v for v in rep["voices"]] + [""]
    for it in rep["items"]:
        L.append("  beat %d  %s (%s)  %.1f s, %d words/min, hiss %.1f dB%s" % (
            it["beat"], it["who"], it["voice"], it["seconds"], it["wpm"], it["hiss_db"],
            ("  words heard right: %d%%" % round(it["match"] * 100)) if "match" in it else ""))
        L.append("      \"%s\"" % it["text"])
        if it.get("problems"):
            L.append("      heard: %s" % it["heard"])
    for w in rep["warns"]:
        L.append("  note: " + w)
    for f in rep["fails"]:
        L.append("  FAILED: " + f)
    L.append("")
    L.append("Machine checks %s." % ("FAILED - fix the voice, speed or a lexicon entry; the build will make a new sample" if rep["fails"]
                                     else "passed" + ("" if rep["listened"] else " (words not checked: no Whisper)")))
    return "\n".join(L) + "\n"


def approval_path(work):
    return os.path.join(work, "sample.approved.json")


def check_approval(work, fp):
    """None when this voice set-up was approved, else why not."""
    try:
        with open(approval_path(work), encoding="utf-8") as f:
            if json.load(f).get("fingerprint") == fp:
                return None
    except (OSError, ValueError):
        pass
    return "not approved"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True)
    ap.add_argument("--approve", action="store_true", help="record that the user listened to sample.wav and is happy with it")
    a = ap.parse_args()
    p = os.path.join(a.work, "sample.json")
    if not os.path.exists(p):
        raise SystemExit("no sample yet: run build_video.py first, it makes one and stops")
    with open(p, encoding="utf-8") as f:
        rep = json.load(f)
    if not a.approve:
        print(describe(rep))
        return 0
    if rep["fails"]:
        print(describe(rep))
        raise SystemExit("not approved: the machine checks failed (above). Fix them, build again for a new sample.")
    with open(approval_path(a.work), "w", encoding="utf-8") as f:
        json.dump({"fingerprint": rep["fingerprint"]}, f)
    print("voice approved (%s); the full build may start" % rep["fingerprint"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
