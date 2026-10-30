"""When each word of a narrated sentence is spoken, for karaoke-style caption highlighting.

Exact when the voice reports it: edge-tts saves the time of every spoken word next to the clip (<clip>.words.json).
Those words are lined up with the caption's words (which can differ: the caption says "API" where the voice was given
"A P I"); matched words take their real times, a caption word that covers several spoken ones (or the other way round)
shares their time span by length.

Estimated otherwise (Kokoro, system voices, or an old cached clip):
1. find where speech really starts and ends inside the WAV (energy gate, skipping leading/trailing silence);
2. spread the words over that window in proportion to how long each takes to say (letters + a pause
   weight after commas and sentence ends).
The estimate is typically within ~100 ms per word, which already reads as "in sync".

    python words.py "Some caption text." clip.wav        # prints [[start, end], ...] in seconds
"""
import array
import difflib
import json
import os
import re
import sys
import wave


def speech_window(wav_path, hop_ms=10):
    """(start_s, end_s) of the audible part of a mono s16 WAV."""
    with wave.open(wav_path, "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        pcm = array.array("h"); pcm.frombytes(w.readframes(n))
    hop = max(1, int(sr * hop_ms / 1000))
    env = []
    for i in range(0, len(pcm) - hop, hop):
        seg = pcm[i:i + hop]
        env.append((sum(x * x for x in seg) / len(seg)) ** 0.5)
    if not env:
        return 0.0, n / float(sr)
    gate = max(max(env) * 0.04, 60.0)
    on = next((i for i, e in enumerate(env) if e > gate), 0)
    off = next((i for i in range(len(env) - 1, -1, -1) if env[i] > gate), len(env) - 1)
    return on * hop_ms / 1000.0, (off + 1) * hop_ms / 1000.0


def _weight(word):
    core = re.sub(r"[^A-Za-z0-9]", "", word)
    w = 1.5 + len(core) * 1.0                        # every word has a floor, longer words take longer
    w += len(re.findall(r"[aeiouy]+", core.lower())) * 0.8   # syllable-ish
    return w


def _pause_after(word):
    if re.search(r"[.!?][\"')\]]*$", word):
        return 4.0
    if re.search(r"[,;:—–-][\"')\]]*$", word) and len(word) > 1:
        return 2.2
    return 0.0


def _norm(word):
    return re.sub(r"[^a-z0-9]", "", word.lower())


def exact_timings(text, wav_path):
    """Caption word times from the voice's own word report, or None when there is none or it does not line up."""
    side = wav_path + ".words.json"
    words = text.split()
    if not words or not os.path.exists(side):
        return None
    with open(side, encoding="utf-8") as f:
        said = json.load(f)
    if not said:
        return None
    a, b = [_norm(w) for w in words], [_norm(s[2]) for s in said]
    out = [None] * len(words)
    matched = 0
    for op, a0, a1, b0, b1 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            matched += a1 - a0
            for k in range(a1 - a0):
                out[a0 + k] = [said[b0 + k][0], said[b0 + k][1]]
        elif op == "replace":                          # e.g. caption "API" vs spoken "A", "P", "I": share the span
            s, e = said[b0][0], said[b1 - 1][1]
            wts = [max(len(a[i]), 1) for i in range(a0, a1)]
            t = s
            for i, wt in zip(range(a0, a1), wts):
                d = (e - s) * wt / sum(wts)
                out[i] = [t, t + d]
                t += d
    if matched < 0.5 * len(words):                     # too different to trust: estimate instead
        return None
    for i in range(len(out)):                          # caption words the voice never said: a short slot after the last
        if out[i] is None:
            start = out[i - 1][1] if i and out[i - 1] else said[0][0]
            nxt = next((out[j][0] for j in range(i + 1, len(out)) if out[j]), start + 0.25)
            out[i] = [start, max(start, min(nxt, start + 0.25))]
    return [[round(s, 3), round(e, 3)] for s, e in out]


def estimated_timings(text, wav_path):
    words = text.split()
    if not words:
        return []
    t0, t1 = speech_window(wav_path)
    ws = [_weight(w) for w in words]
    ps = [_pause_after(w) for w in words]
    ps[-1] = 0.0
    total = sum(ws) + sum(ps)
    unit = max(t1 - t0, 0.1) / total
    out, t = [], t0
    for w, p in zip(ws, ps):
        a, b = t, t + w * unit
        out.append([round(a, 3), round(b, 3)])
        t = b + p * unit
    return out


def word_timings(text, wav_path):
    """[[start, end], ...] seconds from the start of the clip, one entry per whitespace-separated word of `text`."""
    return exact_timings(text, wav_path) or estimated_timings(text, wav_path)


def is_exact(text, wav_path):
    return exact_timings(text, wav_path) is not None


if __name__ == "__main__":
    print(json.dumps(word_timings(sys.argv[1], sys.argv[2])))
