"""Who says each sentence, in what mood, with which engine and voice - decided once, before any audio is made.

Storyboard keys (all optional; without them one narrator says every line, as before):

  "voice": {"engine": "chatterbox", "voice": "...", "rate": "-4%", "mood": "warm", "finish": "warm"}
           defaults for every speaker
  "cast":  {"host":  {"name": "Alex", "voice": "en-US-AndrewMultilingualNeural"},
            "guest": {"name": "Maya", "engine": "chatterbox", "sample": "voices/maya.wav", "color": "#0f9d76"}}
           two or more speakers: a conversation (host asks, expert answers); captions show who is talking
  beat:    {"who": "guest", "mood": "excited", "pause": 0.3, "say": "..."}

Moods are real with chatterbox (the model's emotion intensity and pacing change). edge and kokoro take no emotion
setting, so there a mood only nudges pace and pitch a little; say so instead of promising emotion.

A cloned voice (`sample`) is used only with the consent record prepare_voice.py writes next to it, and only while the
sample is the exact file that consent was given for.
"""
import hashlib
import json
import os
import re

import tts

# mood: (pace %, pitch Hz) for edge/kokoro, (exaggeration, cfg_weight) for chatterbox. Pace and pitch stay small on
# purpose: past ~10 % a neural voice starts to sound pushed. Chatterbox: more exaggeration = more emotion (and faster),
# lower cfg = slower, more deliberate pacing (Resemble's guidance for expressive speech: exaggeration 0.7+, cfg ~0.3);
# its pace is then set per take from what Whisper heard (narrate.TARGET_WPM).
MOODS = {
    "neutral": (0, 0, 0.50, 0.50),
    "calm": (-6, -2, 0.35, 0.50),
    "warm": (-3, -1, 0.55, 0.45),
    "serious": (-5, -3, 0.40, 0.50),
    "curious": (0, 3, 0.60, 0.40),
    "happy": (2, 4, 0.70, 0.35),
    "excited": (5, 7, 0.85, 0.30),
}
NATURAL = ("edge", "chatterbox", "kokoro")
DRAFT = ("sapi", "say", "flite")        # robotic or dated system voices: never in a video someone will watch
# distinct, natural voices for a cast that names none, best first (Microsoft's "conversation" voices for edge)
CAST_VOICES = {"edge": ["en-US-AndrewMultilingualNeural", "en-US-AvaMultilingualNeural", "en-US-BrianMultilingualNeural",
                        "en-US-EmmaMultilingualNeural"],
               "kokoro": ["af_heart", "am_michael", "bf_emma", "am_adam"],
               "chatterbox": ["default"]}
# the narration's sound: high-pass (rumble), gentle compression (even level), de-esser (sharp s), light EQ (body and
# presence). Applied once to the mixed narration before loudness normalisation; "off" keeps only the loudness step.
# Order matters (measured): a compressor lifts consonants against vowels (s/t band +3 dB), so the de-esser comes after it.
# warm on Microsoft voices: s band -3 dB, low body +1 dB, presence unchanged, loudness range narrower.
FINISH = {
    "off": "",
    "clean": "highpass=f=70,acompressor=threshold=0.125:ratio=2:attack=10:release=160:knee=6,deesser=i=0.4:m=0.5",
    "warm": "highpass=f=70,acompressor=threshold=0.125:ratio=2.5:attack=10:release=150:knee=6,deesser=i=0.5:m=0.5,"
            "equalizer=f=180:t=q:w=1:g=1.5,equalizer=f=3200:t=q:w=1.2:g=1,highshelf=f=9000:g=-1",
    "broadcast": "highpass=f=80,acompressor=threshold=0.09:ratio=3.5:attack=6:release=120:knee=4,deesser=i=0.6:m=0.6,"
                 "equalizer=f=200:t=q:w=1:g=1,equalizer=f=3500:t=q:w=1:g=2,highshelf=f=10000:g=-1",
}
DEFAULT_FINISH = "warm"
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


def audio_filter(finish=DEFAULT_FINISH):
    """ffmpeg -af chain for the final narration: the finish, then loudness normalisation."""
    return ",".join(x for x in (FINISH.get(finish or "off", ""), LOUDNORM) if x)


def _pct(rate):
    return int(str(rate or "+0%").rstrip("%") or 0)


def tone_rate(engine, tone, voice=None):
    """The voice's (or engine's) default ("-4%") shifted by the tone's offset (launch +6, polished -5...) -> "+2%"."""
    import lint_storyboard
    off = lint_storyboard.TONES.get(tone, lint_storyboard.TONES["explainer"])[3]
    return "%+d%%" % (_pct(tts.VOICE_RATE.get(voice, tts.DEFAULT_RATE[engine])) + off)


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def consent_path(sample):
    return os.path.splitext(sample)[0] + ".consent.json"


def check_consent(sample):
    """None when the sample may be used, else the reason it may not."""
    if not os.path.exists(sample):
        return "voice sample %s does not exist" % sample
    cp = consent_path(sample)
    if not os.path.exists(cp):
        return ("no consent record for %s - prepare it with: python scripts/prepare_voice.py --in <recording> --out %s "
                "--speaker \"<their name>\" --consent \"<what they agreed to>\"" % (sample, sample))
    try:
        with open(cp, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError) as exc:
        return "consent record %s cannot be read (%s)" % (cp, exc)
    if len(str(rec.get("consent", "")).strip()) < 15 or not str(rec.get("speaker", "")).strip():
        return "consent record %s has no speaker or no consent statement" % cp
    if rec.get("sha256") != file_sha(sample):
        return ("%s changed after consent was recorded; prepare it again with prepare_voice.py (consent covers one exact "
                "recording)" % sample)
    return None


def consent_of(sample):
    """The speaker named in a sample's consent record."""
    try:
        with open(consent_path(sample), encoding="utf-8") as f:
            return json.load(f).get("speaker") or "the speaker"
    except (OSError, ValueError):
        return "the speaker"


def resolve_sample(sample, base_dir):
    if not sample:
        return None
    return sample if os.path.isabs(sample) else os.path.normpath(os.path.join(base_dir or ".", sample))


def plan(script, script_path=None, engine="auto", voice=None, rate=None, lexicon=None, draft_ok=False):
    """Every sentence's speaker, engine, voice and delivery. Raises SystemExit with a clear reason when a voice cannot
    be used (unknown speaker, no consent for a cloned voice, a robotic engine, two speakers who would sound the same).

    Returns {"speakers": {id: {...}}, "order": [ids], "lines": [one per beat], "multi": bool}."""
    vdef = script.get("voice") or {}
    cast = script.get("cast") or {}
    base = os.path.dirname(os.path.abspath(script_path)) if script_path else "."
    tone = script.get("tone", "explainer")
    lex = lexicon or {}
    order = list(cast) or ["narrator"]
    picked = {}

    def engine_for(e, sid):
        e = e or "auto"
        if e == "auto":
            if "auto" not in picked:
                picked["auto"] = tts.pick_engine("auto", allow_draft=draft_ok)
            return picked["auto"]
        if e not in tts.ENGINES:
            raise SystemExit("%s: unknown engine %r (use one of %s)" % (sid, e, ", ".join(tts.ENGINES)))
        if e in DRAFT and not draft_ok:
            raise SystemExit("%s: the %s voice sounds robotic or dated, and a video narrated by it is not worth watching. "
                             "Use edge (online), chatterbox or kokoro (offline); python scripts/doctor.py shows how. "
                             "--draft-voice allows it for a timing preview that nobody else will see." % (sid, e))
        if e not in picked:
            picked[e] = tts.pick_engine(e, allow_draft=draft_ok)
        return picked[e]

    speakers, used_voices = {}, {}
    for idx, sid in enumerate(order):
        c = cast.get(sid) or {}
        where = "cast.%s" % sid if cast else "voice"
        sample = resolve_sample(c.get("sample") or (vdef.get("sample") if not cast else None), base)
        chosen = c.get("engine") or ("chatterbox" if sample else None) or vdef.get("engine")
        e = engine_for(chosen or engine, where)
        if sample and e != "chatterbox":
            raise SystemExit("%s: a voice sample can only be spoken by the chatterbox engine (it clones the voice)" % where)
        v = c.get("voice")
        if not v and not sample and vdef.get("voice") and vdef.get("engine", e) == e and (not cast or idx == 0):
            v = vdef["voice"]
        if not v and not sample and voice and not chosen and (not cast or idx == 0):
            v = voice                                 # --voice belongs to the engine picked on the command line
        if not v and not sample:
            if not cast:
                v = tts.DEFAULT_VOICE[e]
            else:                                     # the best voice of this engine nobody in the cast has yet
                taken = {k[1] for k in used_voices if k[0] == e}
                v = next((x for x in CAST_VOICES.get(e) or [tts.DEFAULT_VOICE[e]] if x not in taken), None)
                if not v:
                    raise SystemExit("%s: %s has no further built-in voice to tell this speaker apart; give it a \"voice\", "
                                     "a voice \"sample\" (chatterbox) or another engine" % (where, e))
        if sample:
            why = check_consent(sample)
            if why:
                raise SystemExit("%s: %s" % (where, why))
            v = "sample:" + os.path.basename(sample)
        ident = (e, v, file_sha(sample) if sample else "")
        if ident in used_voices:
            raise SystemExit("cast.%s and cast.%s would have the same voice (%s %s): listeners could not tell them apart"
                             % (used_voices[ident], sid, e, v))
        used_voices[ident] = sid
        r = c.get("rate") or vdef.get("rate") or rate or tone_rate(e, tone, v)
        speakers[sid] = {"id": sid, "name": c.get("name") or (sid.replace("_", " ").title() if cast else ""), "engine": e,
                         "voice": v, "sample": sample, "sample_sha": ident[2], "rate": r, "color": c.get("color"),
                         "mood": c.get("mood") or vdef.get("mood") or "neutral", "index": idx}

    lines = []
    for i, b in enumerate(script["beats"]):
        sid = b.get("who") or order[0]
        if sid not in speakers:
            raise SystemExit("beat %d: \"who\": %r is not in \"cast\" (%s)" % (i + 1, sid, ", ".join(order)))
        sp = speakers[sid]
        mood = b.get("mood") or sp["mood"]
        if mood not in MOODS:
            raise SystemExit("beat %d: unknown mood %r (use one of %s)" % (i + 1, mood, ", ".join(MOODS)))
        pace, pitch, exag, cfg = MOODS[mood]
        spoken = tts.apply_lexicon(str(b["say"]).replace("{DATA}", "").strip(), lex)
        line = {"i": i, "who": sid, "name": sp["name"], "engine": sp["engine"], "voice": sp["voice"], "sample": sp["sample"],
                "sample_sha": sp["sample_sha"], "mood": mood, "text": spoken,
                "caption": str(b.get("cap") or b["say"]).replace("{DATA}", "").strip()}
        if sp["engine"] == "chatterbox":          # emotion is the model's own; pace comes from the speaker's rate only
            line.update(rate=sp["rate"], exaggeration=exag, cfg=cfg, model=tts.chatterbox_model())
        else:
            line.update(rate="%+d%%" % (_pct(sp["rate"]) + pace), pitch="%+dHz" % pitch if sp["engine"] == "edge" else "+0Hz")
        lines.append(rekey(line))
    for sid, sp in speakers.items():
        if tts.voice_fx(sp["engine"], sp["voice"]):
            print("note: %s speaks with kokoro %s, one of Kokoro's rougher male voices; it is cleaned of hiss, but a Microsoft "
                  "voice (engine edge, e.g. en-US-AndrewMultilingualNeural) sounds cleaner when internet is available "
                  "(a cast may mix engines)." % (sp["name"] or sid, sp["voice"]))
    finish = vdef.get("finish", DEFAULT_FINISH)
    if finish not in FINISH:
        raise SystemExit("voice.finish %r: use one of %s" % (finish, ", ".join(FINISH)))
    return {"speakers": speakers, "order": order, "lines": lines, "multi": len(order) > 1, "finish": finish}


def rekey(line):
    """Cache key of a line: everything that changes its sound (engine, voice or sample, pace, delivery, words)."""
    key = "|".join(str(line.get(k, "")) for k in ("engine", "voice", "sample_sha", "rate", "pitch", "exaggeration", "cfg",
                                                    "model", "text"))
    fx = tts.voice_fx(line.get("engine"), line.get("voice"))
    if fx:                                    # only voices with a cleanup: every other voice keeps its cached takes
        key += "|" + fx
    line["key"] = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return line


def faster(line, pct):
    """The same line, spoken `pct` percent faster (revoice.py fitting a new voice into the old picture)."""
    out = dict(line, rate="%+d%%" % (_pct(line["rate"]) + pct))
    return rekey(out)


def describe(p):
    """One line per speaker for the build log and description.md."""
    out = []
    for sid in p["order"]:
        s = p["speakers"][sid]
        moods = sorted({ln["mood"] for ln in p["lines"] if ln["who"] == sid})
        n = sum(1 for ln in p["lines"] if ln["who"] == sid)
        who = ("%s (%s)" % (s["name"], sid)) if p["multi"] else "narrator"
        out.append("%s: %s %s, rate %s, %d line(s), mood %s" % (who, s["engine"], s["voice"], s["rate"], n, "/".join(moods)))
    return out


def speaker_label(line, p):
    return line["name"] if p["multi"] else ""
