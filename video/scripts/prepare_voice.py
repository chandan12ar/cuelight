#!/usr/bin/env python3
"""Turn a voice recording into a clean voice sample for cloning, together with the consent it may be used under.

    python scripts/prepare_voice.py --passage                       # what to read aloud (about 15 seconds)
    python scripts/prepare_voice.py --in me.m4a --out video/voices/me.wav --speaker "Sam Lee" \
        --consent "Sam Lee recorded this himself on 2026-10-24 and agrees to its use for Acme product videos"

Only the voice of the person who asked, or of someone who has said yes to this use. Never a public figure, a customer,
or anyone from a clip found online. The consent record (<out>.consent.json) names the speaker, what they agreed to and
the exact recording (its SHA-256): the build refuses a sample without it, or one that was changed afterwards.

What it does to the recording: any audio format in (phone memo, m4a, mp3, wav); mono 24 kHz; rumble below 70 Hz cut;
silence at both ends trimmed; at most ~20 s kept (the voice model listens to the first 10); level evened out. It then
checks what makes a clone sound bad: too short, clipped (distorted) peaks, background noise. Problems stop it, with
the reason; borderline ones are listed.

Good samples: a quiet room, phone or headset 15-30 cm from the mouth, natural lively speech (the clone copies the tone
it hears: a flat read gives a flat voice), no music, no other voices.
"""
import argparse
import array
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import env  # noqa: E402
import voices  # noqa: E402

PASSAGE = ("Here's the thing about good tools: you only notice them when they're missing. This one puts the numbers "
           "that matter on one page, right when you need them. So let's open it up and look around. Honestly? I think "
           "you're going to love how simple it is!")
MAX_S = 20.0


def frames_rms(pcm, sr, ms=50):
    hop = int(sr * ms / 1000)
    return [math.sqrt(sum(x * x for x in pcm[i:i + hop]) / hop) for i in range(0, len(pcm) - hop, hop)]


def db(x):
    return 20 * math.log10(max(x, 1e-9) / 32768.0)


def main():
    env.safe_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="src", help="the recording (any format ffmpeg reads)")
    ap.add_argument("--out", help="the voice sample to write, e.g. video/voices/sam.wav")
    ap.add_argument("--speaker", help="whose voice this is (full name)")
    ap.add_argument("--consent", help="what they agreed to, in a sentence (who, when, for what)")
    ap.add_argument("--passage", action="store_true", help="print a short text to read aloud for the recording")
    a = ap.parse_args()
    if a.passage:
        print("Read this aloud the way you would explain it to a friend (lively, not a flat read), ~15 s:\n\n  " + PASSAGE)
        return 0
    if not (a.src and a.out):
        ap.error("--in and --out are needed (or --passage)")
    if not (a.speaker and a.speaker.strip()):
        ap.error("--speaker is needed: whose voice is this?")
    if not a.consent or len(a.consent.strip()) < 15:
        ap.error("--consent is needed: one sentence on who agreed, when, and for what use")
    env.require_tools()
    if not os.path.exists(a.src):
        raise SystemExit("no such recording: %s" % a.src)

    tmp = tempfile.mkdtemp(prefix="voice-")
    raw = os.path.join(tmp, "raw.wav")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", a.src, "-vn", "-af", "highpass=f=70",
                    "-ac", "1", "-ar", "24000", "-sample_fmt", "s16", raw], check=True)
    with wave.open(raw, "rb") as w:
        sr = w.getframerate()
        pcm = array.array("h")
        pcm.frombytes(w.readframes(w.getnframes()))
    if len(pcm) < sr:
        raise SystemExit("the recording is shorter than a second")
    env_ = frames_rms(pcm, sr)
    loud = sorted(env_)
    speech_lvl = loud[int(len(loud) * 0.90)]
    gate = max(speech_lvl * 0.06, 40.0)
    on = next(i for i, e in enumerate(env_) if e > gate)
    off = max(i for i, e in enumerate(env_) if e > gate)
    hop = int(sr * 0.05)
    start, end = max(0, on * hop - int(0.1 * sr)), min(len(pcm), (off + 1) * hop + int(0.15 * sr))
    if (end - start) / sr > MAX_S:                    # cut at a pause near 20 s, not mid-word
        limit = start + int(MAX_S * sr)
        quiet = [i for i in range(len(env_)) if start <= i * hop <= limit and env_[i] < gate * 1.5 and i * hop > start + 12 * sr]
        end = quiet[-1] * hop + hop if quiet else limit
    pcm = pcm[start:end]
    dur = len(pcm) / float(sr)
    env_ = frames_rms(pcm, sr)
    loud = sorted(env_)
    noise, speech = loud[int(len(loud) * 0.10)], loud[int(len(loud) * 0.90)]
    snr = db(speech) - db(noise)
    clipped = sum(1 for x in pcm if abs(x) >= 32600) / float(len(pcm))
    voiced = sum(1 for e in env_ if e > gate) * 0.05

    errors, notes = [], []
    if voiced < 5.0:
        errors.append("only %.1f s of speech - record at least 8 seconds (10-20 is best)" % voiced)
    elif voiced < 8.0:
        notes.append("%.1f s of speech works, 10-20 s clones better" % voiced)
    if clipped > 0.001:
        errors.append("%.2f%% of the samples are clipped (distorted) - record a little quieter or further from the mic" % (clipped * 100))
    if snr < 15:
        errors.append("background noise is high (speech only %.0f dB above it) - record in a quieter room" % snr)
    elif snr < 25:
        notes.append("some background noise (speech %.0f dB above it); a quieter room clones cleaner" % snr)
    print("recording: %.1f s kept, %.1f s of speech, speech %.0f dB above the background, peaks %s"
          % (dur, voiced, snr, "clipped" if clipped > 0.001 else "clean"))
    for n in notes:
        print("  note  " + n)
    if errors:
        for e in errors:
            print("  ERROR " + e)
        raise SystemExit("voice sample not written")

    cut = os.path.join(tmp, "cut.wav")
    with wave.open(cut, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", cut, "-af",
                    "afade=t=in:d=0.05,loudnorm=I=-20:TP=-2:LRA=11", "-ar", "24000", "-ac", "1", "-sample_fmt", "s16", a.out],
                   check=True)
    rec = {"speaker": a.speaker.strip(), "consent": a.consent.strip(), "recorded_from": os.path.basename(a.src),
           "prepared": time.strftime("%Y-%m-%d %H:%M"), "seconds": round(dur, 1), "speech_seconds": round(voiced, 1),
           "snr_db": round(snr, 1), "sha256": voices.file_sha(a.out),
           "note": "Consent covers this exact file (sha256). Re-run prepare_voice.py for a new recording."}
    with open(voices.consent_path(a.out), "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
    name = os.path.splitext(os.path.basename(a.out))[0]
    try:
        shown = os.path.relpath(a.out)
    except ValueError:                                # another drive (Windows): only an absolute path works
        shown = os.path.abspath(a.out)
    print("wrote %s and %s" % (a.out, voices.consent_path(a.out)))
    print("use it in the storyboard (a relative \"sample\" path counts from the storyboard's folder):")
    print('  "cast": {"%s": {"name": "%s", "sample": "%s"}}' % (name, a.speaker.strip().split()[0], shown.replace("\\", "/")))
    print("audition it: python scripts/voicelab.py say \"A test sentence.\" --sample %s --out test.wav" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
