"""Voice every sentence: cached by content, and for the expressive voice, checked by listening and re-taken when wrong.

Chatterbox samples its delivery, so now and then a take swallows or adds a word, trails off, rushes or stops for an
odd pause. Each Chatterbox take is transcribed with Whisper (faster-whisper) and compared with the text; a bad take is
voiced again with another seed (up to 3 takes, then one calmer take), and the best is kept. When no take is good the
build stops and names the sentence: a garbled line is never shipped silently. Whisper's word times are saved next to
the clip (<clip>.words.json), so captions highlight every word exactly, as with edge. The voice model and Whisper take
turns (all takes of a round, then all listening), so an 8 GB laptop never holds both.

edge and kokoro say the text as written, so they are not re-checked here; a network hiccup on edge is retried.
check_narration.py listens to the finished narration for every engine.
"""
import hashlib
import json
import os
import subprocess
import time

import tts
import words as wordtime

TAKES = 3                 # different seeds before giving up on the requested mood
WPM_RANGE = (95, 235)     # outside this a sentence sounds dragged or rushed
LONG_GAP = 1.5            # seconds of silence inside one sentence that sound like a glitch
# Chatterbox talks fast: measured 190-265 words/min of speech on CPU (built-in voice fastest; settings and the
# repetition penalty change it by ~10 % at most), where narration feels natural at ~160. Each take is therefore
# slowed to its mood's pace (pitch kept, rubberband), never by more than 28 %.
TARGET_WPM = {"neutral": 165, "calm": 150, "warm": 160, "serious": 155, "curious": 170, "happy": 172, "excited": 180}
SLOWEST, FASTEST = 0.72, 1.05
UNHEARD_FACTOR = 0.85     # without Whisper the real pace is unknown: the measured average correction
NEED_GB = 3.0             # memory the Chatterbox worker holds (model ~2.6 GB) plus Whisper's share
MIN_FREE_GB = 1.5         # below this the model swaps to disk: measured 9 % of one sentence in 2 h 20 min at 0.8 GB


class Listener:
    """faster-whisper on CPU, loaded once per build."""

    def __init__(self, model="base.en"):
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel
        self.m = WhisperModel(model, device="cpu", compute_type="int8")

    def hear(self, wav):
        segs, _ = self.m.transcribe(wav, language="en", beam_size=5, word_timestamps=True,
                                    condition_on_previous_text=False, vad_filter=False)
        said = [[round(w.start, 3), round(w.end, 3), w.word.strip()] for s in segs for w in (s.words or []) if w.word.strip()]
        return " ".join(w[2] for w in said), said


def get_listener(log):
    try:
        return Listener()
    except ImportError:
        log("  ! Whisper is not installed, so Chatterbox takes cannot be checked by listening. Install it: "
            "python -m pip install faster-whisper")
    except Exception as exc:  # noqa: BLE001 - offline / model blocked: say so, voice without the check
        log("  ! the Whisper model could not be loaded (%s); Chatterbox takes are not checked" % str(exc).splitlines()[0][:100])
    return None


def judge(line, heard_text, said, seconds):
    """How a take came out: {"match", "bad": [reasons it must be re-taken], "notes", "wpm", "score"}."""
    import difflib
    import check_narration as cn
    heard = cn.canon(heard_text)
    best = None
    for meant_text in {line["text"], line["caption"]}:
        meant = cn.canon(meant_text)
        sm = difflib.SequenceMatcher(None, meant, heard, autojunk=False)
        ops = sm.get_opcodes()
        missing, extra, wrong = [], [], []
        for op, a0, a1, b0, b1 in ops:
            if op in ("replace", "delete"):
                for x, y in zip(meant[a0:a1], heard[b0:b1] if op == "replace" else []):
                    if not ((x in cn.SMALL and y in cn.SMALL) or (x, y) in cn.SOUNDS_SAME or (y, x) in cn.SOUNDS_SAME
                            or difflib.SequenceMatcher(None, x, y).ratio() >= 0.8):
                        wrong.append("%s -> %s" % (x, y))
                missing += meant[a0 + (b1 - b0 if op == "replace" else 0):a1]
            if op in ("replace", "insert"):
                extra += heard[b0 + (a1 - a0 if op == "replace" else 0):b1]
        # where it went wrong matters: nothing heard after the last words = cut off; words before or after the
        # sentence = the model babbled. A misheard word (a name Whisper spells its own way) is neither.
        cut = meant[ops[-1][1]:ops[-1][2]] if ops and ops[-1][0] == "delete" else []
        around = (heard[ops[0][3]:ops[0][4]] if ops and ops[0][0] == "insert" else []) + \
                 (heard[ops[-1][3]:ops[-1][4]] if len(ops) > 1 and ops[-1][0] == "insert" else [])
        r = (sm.ratio(), meant, missing, extra, wrong, cut, around)
        best = r if best is None or r[0] > best[0] else best
    ratio, meant, missing, extra, wrong, cut, around = best
    bad, notes = [], []
    if ratio < 0.8:
        bad.append("only %d%% of the words came out right" % round(ratio * 100))
    real_missing = [w for w in missing if w not in cn.SMALL]
    if len(real_missing) >= 1 and len(missing) >= 2 or len(real_missing) >= 2:
        bad.append("words not spoken: " + " ".join(missing[:6]))
    if [w for w in cut if w not in cn.SMALL]:
        bad.append("cut off before the end (%s)" % " ".join(cut[:4]))
    if [w for w in around if w not in cn.SMALL]:
        bad.append("extra words before or after the sentence: " + " ".join(around[:6]))
    elif len(extra) > 2:
        bad.append("extra words: " + " ".join(extra[:6]))
    if wrong:
        notes.append("came out differently: " + ", ".join(wrong[:4]))
    if said:
        gaps = [b[0] - a[1] for a, b in zip(said, said[1:])]
        if gaps and max(gaps) > LONG_GAP:
            bad.append("a %.1f s silence in the middle" % max(gaps))
        span = max(said[-1][1] - said[0][0], 0.5)
    else:
        span = seconds
    wpm = len(line["text"].split()) / span * 60
    if wpm > WPM_RANGE[1]:
        bad.append("rushed (%d words/min)" % wpm)
    elif wpm < WPM_RANGE[0]:
        bad.append("dragging (%d words/min)" % wpm)
    return {"match": round(ratio, 3), "bad": bad, "notes": notes, "wpm": round(wpm), "score": ratio - 0.25 * len(bad)}


def _ffmpeg(args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + args, check=True)


def tidy(path, said=None):
    """Trim a generated clip to its speech (+ a short breath of room at both ends), so pauses come from the storyboard,
    not from silence or noise the model left before or after the sentence."""
    t0, t1 = wordtime.speech_window(path)
    if said:              # Whisper knows where the words are: a breath or murmur well before or after them goes
        t0 = max(t0, said[0][0] - 0.25)
        t1 = min(t1, said[-1][1] + 0.45)
    total = tts.duration(path)
    a, b = max(0.0, t0 - 0.08), min(total, t1 + 0.22)
    if a < 0.02 and total - b < 0.02:
        return 0.0
    tmp = path + ".tidy.wav"
    _ffmpeg(["-i", path, "-af", "atrim=start=%.3f:end=%.3f,asetpts=N/SR/TB,afade=t=in:d=0.02,afade=t=out:st=%.3f:d=0.06"
             % (a, b, max(0.0, b - a - 0.06)), "-ar", "24000", "-ac", "1", "-sample_fmt", "s16", tmp])
    os.replace(tmp, path)
    return a


_RUBBERBAND = []


def pace_factor(line, said):
    """Tempo that brings a take to its mood's natural pace (shifted by the speaker's rate, e.g. a launch tone +6 %)."""
    want = TARGET_WPM.get(line["mood"], 165) * (1 + int(str(line["rate"]).rstrip("%") or 0) / 100.0)
    n = len(line["text"].split())
    span = (said[-1][1] - said[0][0]) if said else 0
    if not said or span < 0.6:
        return min(FASTEST, max(SLOWEST, UNHEARD_FACTOR * want / 165.0))
    f = want / (n / span * 60)
    lo, hi = (0.8, 1.0) if n < 6 else (SLOWEST, FASTEST)    # a short question is quick by nature: correct it less
    return min(hi, max(lo, f))


def stretch(path, rate):
    """Speak faster or slower without changing the pitch (Chatterbox has no rate setting): rubberband when ffmpeg has
    it, else atempo. `rate` is "+5%" or a tempo factor (0.85 = 15 % slower). Returns the tempo factor."""
    f = rate if isinstance(rate, float) else 1.0 + int(str(rate).rstrip("%") or 0) / 100.0
    if abs(f - 1.0) < 0.005:
        return 1.0
    if not _RUBBERBAND:
        _RUBBERBAND.append("rubberband" in subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True,
                                                          text=True).stdout)
    tmp = path + ".rate.wav"
    _ffmpeg(["-i", path, "-af", ("rubberband=tempo=%.4f:pitchq=quality" if _RUBBERBAND[0] else "atempo=%.4f") % f,
             "-ar", "24000", "-ac", "1", "-sample_fmt", "s16", tmp])
    os.replace(tmp, path)
    return f


NOISES = {"hm", "hmm", "mm", "mhm", "uh", "um", "ah", "huh"}


def _drop_edge_noises(said, text):
    """A breath or murmur before or after the sentence (Whisper writes "Hmm.", "Uh.") is trimmed away, not kept."""
    words = [w.strip(".,!?;:").lower() for w in text.split()]
    while said and said[0][2].strip(".,!?;:").lower() in NOISES and (not words or said[0][2].strip(".,!?;:").lower() != words[0]):
        said = said[1:]
    while said and said[-1][2].strip(".,!?;:").lower() in NOISES and (not words or said[-1][2].strip(".,!?;:").lower() != words[-1]):
        said = said[:-1]
    return said


def _tries(line):
    """(seed, exaggeration, cfg) of every take a line may get: its mood with 3 seeds, then one calmer delivery.
    The seed ignores pace, so a faster version of a line (revoice.py fitting the old picture) is the same take."""
    base = int(hashlib.sha1(("%s|%s|%s|%s" % (line["text"], line["voice"], line["sample_sha"], line["mood"])).encode())
               .hexdigest()[:6], 16)
    tries = [(base + k * 7919, line["exaggeration"], line["cfg"]) for k in range(TAKES)]
    if line["exaggeration"] > 0.55 or line["cfg"] < 0.45:
        tries.append((base + 99991, 0.5, 0.5))     # last resort: a calmer, more stable delivery of the same words
    return tries


def _finish_take(line, path, listener):
    """Trim and pace a raw take, and judge it by what Whisper hears (word times moved to match the trimmed clip)."""
    heard, said = listener.hear(path) if listener else ("", None)
    if said:
        said = _drop_edge_noises(said, line["text"])
        heard = " ".join(w[2] for w in said)
    cut = tidy(path, said)
    f = stretch(path, pace_factor(line, said))
    secs = tts.duration(path)
    if not listener:
        return {"match": None, "bad": [], "notes": ["not checked (Whisper missing)"], "wpm": None, "score": 0}, None, secs
    said = [[round(max(0.0, (s - cut) / f), 3), round(max(0.0, (e - cut) / f), 3), w] for s, e, w in said]
    v = judge(line, heard, said, secs)
    v["heard"] = heard
    return v, said, secs


def voice_chatterbox(lines, outs, check="auto", log=print, total=None):
    """Voice Chatterbox lines in rounds: a take of every line that still needs one while the voice model is loaded,
    then the model is unloaded and Whisper listens to all of them. The two never sit in memory together (on an 8 GB
    laptop they did not fit: "mkl_malloc: failed to allocate memory"). A line whose take came out wrong gets another
    round, up to its last take; the best take is kept. Returns {beat: verdict}."""
    import gc
    total = total or len(lines)
    tries = {ln["i"]: _tries(ln) for ln in lines}
    used = {ln["i"]: 0 for ln in lines}
    best = {}                                      # beat -> (score, -take, path, verdict, said)
    pending, quiet = list(lines), False
    while pending:
        made, t = [], time.time()
        try:
            for ln in pending:
                k = used[ln["i"]]
                seed, exag, cfg = tries[ln["i"]][k]
                path = "%s.take%d.wav" % (outs[ln["i"]], k)
                tts.synth(ln["text"], path, "chatterbox", ln["voice"], "+0%", sample=ln.get("sample"), seed=seed,
                          exaggeration=exag, cfg=cfg)
                made.append((ln, path, k, seed, exag, cfg))
                used[ln["i"]] = k + 1
        finally:
            tts.shutdown()                         # free the voice model before Whisper loads
        took = (time.time() - t) / max(1, len(made))
        listener = get_listener((lambda *_: None) if quiet else log) if check != "off" else None
        quiet, again = True, []
        for ln, path, k, seed, exag, cfg in made:
            v, said, secs = _finish_take(ln, path, listener)
            v.update(take=k + 1, seed=seed, exaggeration=exag, cfg=cfg, seconds=round(secs, 2), took=round(took, 1))
            cand, prev = (v["score"], -k, path, v, said), best.get(ln["i"])
            if prev is None or cand[:2] > prev[:2]:
                if prev is not None and os.path.exists(prev[2]):
                    os.remove(prev[2])
                best[ln["i"]] = cand
            elif os.path.exists(path):
                os.remove(path)
            if v["bad"] and listener is not None and used[ln["i"]] < len(tries[ln["i"]]):
                log("  [%2d/%d] take %d: %s - again" % (ln["i"] + 1, total, k + 1, "; ".join(v["bad"])))
                again.append(ln)
        listener = None                            # free Whisper before the voice model loads again
        gc.collect()
        pending = again
    verdicts = {}
    for ln in lines:
        _, _, path, v, said = best[ln["i"]]
        out = outs[ln["i"]]
        os.replace(path, out)
        side = out + ".words.json"
        if said:
            with open(side, "w", encoding="utf-8") as f:
                json.dump(said, f)
        elif os.path.exists(side):
            os.remove(side)
        if v["exaggeration"] != ln["exaggeration"]:
            v["notes"].append("kept a calmer take: the %s takes came out wrong" % ln["mood"])
        verdicts[ln["i"]] = v
    return verdicts


def too_little_memory():
    """Why Chatterbox should not start now (too little free memory: it would swap to disk and crawl), or None."""
    import env
    ram, _ = env.free_memory_gb()
    if ram is not None and ram < MIN_FREE_GB and os.environ.get("NSV_IGNORE_MEMORY") != "1":
        return ("only %.1f GB of memory is free; Chatterbox needs ~%.1f GB. With this little it swaps to disk and one "
                "sentence can take hours (measured)." % (ram, NEED_GB))
    return None


def voice_all(plan, wav_dir, check="auto", log=print):
    """Voice every line of a voices.plan(); returns {"wavs", "durations", "verdicts", "stopped": [problems]}."""
    os.makedirs(wav_dir, exist_ok=True)
    lines = plan["lines"]
    outs = {ln["i"]: os.path.join(wav_dir, "%03d-%s.wav" % (ln["i"], ln["key"])) for ln in lines}

    def cached(ln):
        out = outs[ln["i"]]
        if not (os.path.exists(out) and os.path.exists(out + ".meta.json")):
            return None
        if ln["engine"] == "edge" and not os.path.exists(out + ".words.json"):
            return None                            # voiced before word times were saved: voice it again (a second)
        with open(out + ".meta.json", encoding="utf-8") as f:
            meta = json.load(f)
        if ln["engine"] == "chatterbox" and meta.get("bad") and check != "off":
            return None                            # a known-bad take from an earlier run: try again
        return meta

    metas = {ln["i"]: cached(ln) for ln in lines}
    new = set()
    todo = [ln for ln in lines if ln["engine"] == "chatterbox" and metas[ln["i"]] is None]
    if todo:                                       # say up front how long the slow part will take
        import env
        import voicelab
        rtf = (voicelab.ready() or {}).get("rtf") or 10.0
        speech = sum(len(ln["text"].split()) for ln in todo) / 160.0 * 60
        log("  chatterbox: %d line(s), about %d s of speech; at %.0fx real time here that is roughly %d minute(s)%s"
            % (len(todo), speech, rtf, max(1, round(speech * rtf / 60)), " plus any re-takes" if check != "off" else ""))
        short = too_little_memory()
        if short:
            raise SystemExit(short + " Close the browser and other big programs and build again, or voice this build "
                                     "with edge (NSV_IGNORE_MEMORY=1 tries anyway).")
        ram, commit = env.free_memory_gb()
        if commit is not None and commit < NEED_GB + 1:
            log("  ! only %.1f GB of memory can still be promised to programs; Chatterbox needs ~%.1f GB and may stop "
                "with \"out of memory\" - close the browser and other big programs first" % (commit, NEED_GB))
        elif ram is not None and ram < NEED_GB:
            log("  ! %.1f GB of memory is free; Chatterbox needs ~%.1f GB, so it will be slower than usual (paging) - "
                "closing the browser helps" % (ram, NEED_GB))
        t = time.time()
        for i, v in voice_chatterbox(todo, outs, check, log, len(lines)).items():
            with open(outs[i] + ".meta.json", "w", encoding="utf-8") as f:
                json.dump(v, f)
            metas[i] = v
            new.add(i)
        log("  chatterbox voiced in %.0f s" % (time.time() - t))
    wavs, durations, verdicts, stopped = [], [], {}, []
    for ln in lines:
        out, meta = outs[ln["i"]], metas[ln["i"]]
        label = ("%s, %s" % (ln["name"] or "narrator", ln["mood"])) if (plan["multi"] or ln["mood"] != "neutral") else ""
        if meta is None:                           # edge / kokoro: quick, and edge's network hiccups are retried
            t = time.time()
            for attempt in range(3):
                try:
                    tts.synth(ln["text"], out, ln["engine"], ln["voice"], ln["rate"], pitch=ln.get("pitch", "+0Hz"))
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2 or ln["engine"] != "edge":
                        raise
                    log("      %s failed (%s); trying again" % (ln["engine"], str(exc).splitlines()[0][:80]))
                    time.sleep(2 + 3 * attempt)
            meta = {"match": None, "bad": [], "notes": [], "take": 1, "took": round(time.time() - t, 1)}
            with open(out + ".meta.json", "w", encoding="utf-8") as f:
                json.dump(meta, f)
            new.add(ln["i"])
        d = tts.duration(out)
        wavs.append(out)
        durations.append(d)
        verdicts[ln["i"]] = meta
        if ln["i"] in new or meta.get("bad"):
            extra = ""
            if meta.get("match") is not None:
                extra = "  take %d, %d%% heard right, %s words/min" % (meta.get("take", 1), meta["match"] * 100, meta.get("wpm"))
            log("  [%2d/%d] %s%.1f s%s%s" % (ln["i"] + 1, len(lines), ("%s: " % label) if label else "", d, extra,
                                             "  (%.0f s)" % meta.get("took", 0) if ln["i"] in new else ""))
        if meta.get("bad"):
            stopped.append("beat %d (%s): %s\n      meant: %s\n      heard: %s"
                           % (ln["i"] + 1, label or ln["engine"], "; ".join(meta["bad"]), ln["caption"], meta.get("heard", "")))
    return {"wavs": wavs, "durations": durations, "verdicts": verdicts, "stopped": stopped}
