"""Text-to-speech adapters for the walkthrough video.

Every engine turns one sentence of text into a mono 24 kHz WAV file.

  edge        Microsoft neural voices via the `edge-tts` package (pip install edge-tts).
              Most natural and reliable, free, needs internet. Default when it answers.
  chatterbox  Chatterbox (Resemble AI, open source): real emotion per sentence and voice cloning from a sample, offline
              on CPU. Runs in the voice lab's own Python (voicelab.py setup); slower than real time on a laptop.
  kokoro      Kokoro-82M open neural voice, offline on CPU (pip install kokoro-onnx; set
              KOKORO_MODEL and KOKORO_VOICES). Natural sounding, no network needed.
  sapi        Windows built-in voices. Dated and flat: draft previews only (--draft-voice).
  say         macOS `say`. Draft previews only.
  flite       ffmpeg's bundled flite filter. Robotic: draft previews only.

`auto` picks the first natural engine that works: edge, then chatterbox, then kokoro. It never picks sapi, say or
flite: a video narrated by a robotic voice is not worth making, so those need an explicit --draft-voice.
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys

ENGINES = ("edge", "chatterbox", "kokoro", "sapi", "say", "flite")
NATURAL = ("edge", "chatterbox", "kokoro")
# Natural narration is ~150-170 words/min. Measured: Kokoro at its default speed reads ~200 wpm (brisk, tiring over minutes),
# edge-tts ~165. So each engine gets a default rate that lands near the comfortable range; override with --rate.
DEFAULT_RATE = {"edge": "-4%", "chatterbox": "+0%", "kokoro": "-20%", "sapi": "+0%", "say": "+0%", "flite": "+0%"}
# voices that speak faster than their engine's default assumes. Measured: Microsoft's conversational voices (the cast
# defaults) read ~180-190 words/min at -4%, about 10 % faster than AriaNeural.
VOICE_RATE = {"en-US-AndrewMultilingualNeural": "-10%", "en-US-AvaMultilingualNeural": "-10%",
              "en-US-BrianMultilingualNeural": "-10%", "en-US-EmmaMultilingualNeural": "-10%",
              "en-US-AndrewNeural": "-10%", "en-US-AvaNeural": "-10%", "en-US-BrianNeural": "-10%", "en-US-EmmaNeural": "-10%"}
DEFAULT_VOICE = {"edge": "en-US-AriaNeural", "chatterbox": "default", "kokoro": "af_heart", "sapi": "", "say": "Samantha",
                 "flite": "slt"}


# Kokoro's male voices (am_*, bm_*) are graded lower than af_heart by their authors and carry a grainy, hissy top end that
# the female voices do not (measured: -12 dB of hiss above 6 kHz after this chain). They get a cleanup right after synthesis:
# rumble out, FFT noise reduction for white hiss, and the band above 9 kHz (where the grain lives, not the speech) rolled off.
# Female voices are left exactly as they were.
KOKORO_CLEAN = "highpass=f=70,afftdn=nr=12:nf=-42:nt=w,lowpass=f=9000"


def voice_fx(engine, voice):
    """The cleanup filter a voice gets after synthesis, or "" (it is part of the cache key, so a changed filter re-voices)."""
    if engine == "kokoro" and str(voice or "")[:3] in ("am_", "bm_"):
        return KOKORO_CLEAN
    return ""


def load_lexicon(path=None):
    """Pronunciation fixes: {"API": "A P I", "SQL": "sequel"}. Applied to what is SPOKEN only; captions keep the real
    spelling. Default file: lexicon.default.json next to this script. Pass several with load_lexicon(a) | load_lexicon(b)."""
    import json
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "lexicon.default.json")
    with open(path, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


# Letter names respelled so every voice says them as letters: a bare "A I" is read as the article "a" plus "I" ("uh eye"),
# which is why AI used to come out wrong. "ay eye" is unambiguous for edge, kokoro and chatterbox.
LETTER_NAMES = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (
    "ay bee see dee ee eff jee aitch eye jay kay ell em en oh pee cue ar ess tee you vee double-you ex why zee").split()))
# all-caps short forms that are said as a word, not letter by letter (respelled where a voice would guess wrong)
SAID_AS_WORD = {"NASA": "nasa", "RAM": "ram", "ROM": "rom", "GIF": "gif", "JPEG": "jay-peg", "SQL": "sequel", "JSON": "jay-son",
                "YAML": "yam-ul", "ONNX": "onyx", "SASS": "sass", "RAG": "rag", "BERT": "bert", "CUDA": "koo-da",
                "GGUF": "guh-guff", "LORA": "low-ra", "ASAP": "ay-sap", "FAQ": "fack", "CRUD": "crud", "WASM": "wazz-um",
                "NPM": "en pee em", "MACOS": "mac oh ess", "IOS": "eye oh ess", "WIFI": "why-fye"}
# ordinary words that happen to be written in capitals: never spelled out
CAPS_WORDS = {"NOTE", "TODO", "WARNING", "ERROR", "FREE", "NEW", "ONLY", "NOW", "ALL", "THE", "AND", "FOR", "NOT", "YES", "NO",
              "OK", "BETA", "LIVE", "DONE", "STOP", "START", "TIP", "FAIL", "PASS", "NULL", "TRUE", "FALSE", "AM", "PM",
              "ON", "OFF", "IT", "IS", "IF", "IN", "OR", "TO", "UP", "US", "WE", "ME", "MY", "BE", "DO", "GO", "SO", "AS", "AT", "BY"}


def spell_out(word):
    """"AI" -> "ay eye", "LLMs" -> "ell ell ems": the letters as names, a final s as a plural."""
    plural = len(word) > 2 and word.endswith("s") and word[:-1].isupper()
    names = [LETTER_NAMES[c] for c in (word[:-1] if plural else word)]
    if plural:
        names[-1] += "es" if names[-1].endswith(("s", "x")) else "s"
    return " ".join(names)


def spell_acronyms(text):
    """Say every 2-6 letter ALL-CAPS short form letter by letter (AI, GPU, RLHF, MCP, ...), except the ones said as a
    word (SAID_AS_WORD) and ordinary words typed in capitals (CAPS_WORDS)."""
    def fix(m):
        w = m.group(0)
        base = w[:-1] if w.endswith("s") and len(w) > 2 else w
        if w in CAPS_WORDS or base in CAPS_WORDS:
            return w
        if w in SAID_AS_WORD:
            return SAID_AS_WORD[w]
        if base in SAID_AS_WORD:
            return SAID_AS_WORD[base] + "s"
        return spell_out(w)
    return re.sub(r"(?<![A-Za-z0-9])[A-Z]{2,6}s?(?![A-Za-z0-9])", fix, text)


def is_auto_spoken(word):
    """True for a short form apply_lexicon says correctly without a lexicon entry."""
    base = word[:-1] if word.endswith("s") and len(word) > 2 else word
    return bool(re.fullmatch(r"[A-Z]{2,6}s?", word)) and word not in CAPS_WORDS and base not in CAPS_WORDS


def apply_lexicon(text, lex):
    """Pronunciation: the lexicon's whole-word fixes first (case-sensitive, longest first, e.g. SQL -> sequel, but not inside
    'RAPID'), then every remaining ALL-CAPS short form is spelled letter by letter (AI -> ay eye)."""
    for k in sorted(lex, key=len, reverse=True):
        text = re.sub(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(k), lex[k].replace("\\", "\\\\"), text)
    return spell_acronyms(text)


def _ffmpeg_to_wav(src, dst, af=None):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src] + (["-af", af] if af else []) +
                   ["-ar", "24000", "-ac", "1", "-sample_fmt", "s16", dst], check=True)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def _edge(text, out, voice, rate, pitch="+0Hz", **_):
    """Microsoft's service also reports when every word is spoken; those times are saved next to the clip
    (<clip>.words.json, [[start_s, end_s, word], ...]) so captions highlight each word exactly, not by estimate.
    `pitch` ("+4Hz") is the only expressive setting the free service accepts besides rate (no speaking styles)."""
    import json

    import edge_tts

    tmp, said = out + ".mp3", []

    async def go():
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch or "+0Hz", boundary="WordBoundary")
        with open(tmp, "wb") as f:
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    said.append([chunk["offset"] / 1e7, (chunk["offset"] + chunk["duration"]) / 1e7, chunk["text"]])

    asyncio.run(go())
    _ffmpeg_to_wav(tmp, out)
    os.remove(tmp)
    side = out + ".words.json"
    if said:
        with open(side, "w", encoding="utf-8") as f:
            json.dump(said, f)
    elif os.path.exists(side):
        os.remove(side)


_CB = {}


def chatterbox_model():
    """standard (emotion control, the default) or turbo (faster, no emotion control): NSV_CHATTERBOX_MODEL or the lab's."""
    import voicelab
    return os.environ.get("NSV_CHATTERBOX_MODEL") or (voicelab.ready() or {}).get("model", "standard")


def _chatterbox(text, out, voice, rate, sample=None, seed=1, exaggeration=0.5, cfg=0.5, **_):
    """One take from the voice lab's worker (started on first use, kept for the whole narration; see shutdown()).
    `voice` "default" is the model's own voice; a cloned voice comes as `sample`. Pace is set afterwards (narrate.py)."""
    import voicelab
    if "w" not in _CB:
        _CB["w"] = voicelab.Worker(chatterbox_model())
    limit = max(300, 60 * len(text.split()) / 2.7)   # 60x the sentence's length: normal is 9-17x on a laptop CPU
    try:
        _CB["w"].call(op="say", text=text, out=os.path.abspath(out), seed=int(seed), exaggeration=float(exaggeration),
                      cfg=float(cfg), sample=os.path.abspath(sample) if sample else None, limit=limit)
    except RuntimeError:
        _CB.pop("w", None)                           # the worker is gone; the next call starts a fresh one
        raise


def shutdown():
    """Stop the Chatterbox worker: it holds ~3 GB of memory the recording needs."""
    w = _CB.pop("w", None)
    if w is not None:
        w.close()


def _sapi(text, out, voice, rate, **_):
    tmp = out + ".src.wav"
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          + ("$s.SelectVoice('%s'); " % voice if voice else "")
          + "$s.Rate = 0; $s.SetOutputToWaveFile($env:TTS_OUT); $s.Speak($env:TTS_TEXT); $s.Dispose()")
    env = dict(os.environ, TTS_OUT=tmp, TTS_TEXT=text)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, env=env)
    _ffmpeg_to_wav(tmp, out)
    os.remove(tmp)


def _say(text, out, voice, rate, **_):
    tmp = out + ".aiff"
    subprocess.run(["say", "-v", voice, "-o", tmp, text], check=True)
    _ffmpeg_to_wav(tmp, out)
    os.remove(tmp)


_KOKORO = None


def _kokoro(text, out, voice, rate, **_):
    """Kokoro-82M, a small open (Apache-2.0) neural voice that runs offline on CPU.
    KOKORO_MODEL = path to model_quantized.onnx, KOKORO_VOICES = folder of <voice>.bin style files
    (the kokoro-js npm package ships them). speed comes from `rate` like "+5%"."""
    global _KOKORO
    import wave

    import numpy as np
    from kokoro_onnx import Kokoro

    if _KOKORO is None:
        vdir = os.environ["KOKORO_VOICES"]
        npz = os.path.join(vdir, "_voices.npz")
        if not os.path.exists(npz):
            np.savez(npz, **{f[:-4]: np.fromfile(os.path.join(vdir, f), dtype=np.float32).reshape(-1, 1, 256)
                             for f in os.listdir(vdir) if f.endswith(".bin")})
        _KOKORO = Kokoro(os.environ["KOKORO_MODEL"], npz)
    speed = 1.0 + float(str(rate).rstrip("%") or 0) / 100.0
    audio, sr = _KOKORO.create(text, voice=voice, speed=speed, lang="en-us")
    tmp = out + ".src.wav"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    fx = voice_fx("kokoro", voice)
    if fx:
        _ffmpeg_to_wav(tmp, out, fx)
    else:
        _ffmpeg_to_wav(tmp, out)
    os.remove(tmp)


def _flite(text, out, voice, rate, **_):
    txt = out + ".txt"
    with open(txt, "w", encoding="utf-8") as f:
        f.write(text)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "flite=textfile=%s:voice=%s" % (txt.replace("\\", "/").replace(":", "\\:"), voice),
                    "-ar", "24000", "-ac", "1", "-sample_fmt", "s16", out], check=True)
    os.remove(txt)


_FN = {"edge": _edge, "chatterbox": _chatterbox, "kokoro": _kokoro, "sapi": _sapi, "say": _say, "flite": _flite}


def _available(engine):
    if engine == "edge":
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False
    if engine == "chatterbox":
        import voicelab
        return voicelab.ready() is not None
    if engine == "kokoro":
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return False
        return bool(os.environ.get("KOKORO_MODEL") and os.environ.get("KOKORO_VOICES"))
    if engine == "sapi":
        return sys.platform.startswith("win") and shutil.which("powershell") is not None
    if engine == "say":
        return shutil.which("say") is not None
    if engine == "flite":
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
        return "flite" in r.stdout
    return False


def edge_reachable():
    """edge-tts can be installed while Microsoft's voice service is blocked (offline, sandbox). Try one word."""
    import tempfile
    d = tempfile.mkdtemp(prefix="tts-probe-")
    try:
        _edge("Ready.", os.path.join(d, "probe.wav"), DEFAULT_VOICE["edge"], "+0%")
        return True, ""
    except Exception as exc:  # noqa: BLE001 - any failure means "not usable here"
        return False, (str(exc).splitlines() or [type(exc).__name__])[0][:100]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def pick_engine(requested="auto", allow_draft=False):
    """The engine is decided once, before the first sentence: `auto` skips edge when its service is unreachable, so a
    blocked network means the next natural engine, not a build that dies halfway. After this there is no fallback.
    Robotic and dated voices (sapi, say, flite) are never chosen unless `allow_draft` (the --draft-voice flag)."""
    if requested != "auto":
        if requested not in NATURAL and not allow_draft:
            raise SystemExit("the %s voice sounds robotic or dated, and a video narrated by it is not worth watching. Use edge "
                             "(online), chatterbox or kokoro (offline); python scripts/doctor.py shows how. --draft-voice "
                             "allows it for a timing preview nobody else will see." % requested)
        if not _available(requested):
            raise SystemExit("TTS engine %r is not available on this machine (python scripts/doctor.py shows how to install it)" % requested)
        return requested
    for e in ENGINES if allow_draft else NATURAL:
        if not _available(e):
            continue
        if e == "edge":
            ok, why = edge_reachable()
            if not ok:
                print("edge-tts is installed but Microsoft's voice service is unreachable (%s); trying the next engine" % why)
                continue
        return e
    raise SystemExit("no natural voice is available here (edge needs internet; chatterbox and kokoro work offline). "
                     "A robotic voice would make the video worthless, so the build stops. python scripts/doctor.py "
                     "prints the install command for each.")


def synth(text, out, engine, voice=None, rate="+0%", **delivery):
    """Synthesize `text` to `out` (WAV) and return its duration in seconds. `delivery`: pitch (edge), sample, seed,
    exaggeration, cfg (chatterbox); engines ignore what they cannot use.

    A failure is raised, never papered over: silently switching voice mid-video would be worse
    than stopping."""
    _FN[engine](text, out, voice or DEFAULT_VOICE[engine], rate, **delivery)
    return duration(out)
