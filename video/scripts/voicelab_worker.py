"""Runs INSIDE the voice lab's own Python (see voicelab.py), never in the main one: it imports torch and Chatterbox.

Speaks a tiny protocol: one JSON object per line on stdin, one JSON reply per line on stdout.
  {"op": "hello"}                                         -> {"ok": true, "model": ..., "sr": 24000, "load_s": ...}
  {"op": "say", "text": ..., "out": "x.wav", "seed": 1,
   "exaggeration": 0.5, "cfg": 0.5, "temperature": 0.8,
   "sample": "voice.wav" | null}                          -> {"ok": true, "seconds": 3.2, "took": 4.1}
  {"op": "bye"}                                           -> exits
Anything the libraries print goes to stderr (the caller sends it to a log file), so stdout stays clean JSON.
"""
import json
import os
import sys
import time
import wave

PROTO = sys.stdout
sys.stdout = sys.stderr          # library chatter (progress bars, warnings) must not corrupt the protocol


def reply(**kw):
    PROTO.write(json.dumps(kw) + "\n")
    PROTO.flush()


def _path(repo, name):
    from huggingface_hub import hf_hub_download
    try:
        return hf_hub_download(repo_id=repo, filename=name, local_files_only=True)
    except Exception:  # noqa: BLE001 - not downloaded yet (first run): fetch it
        return hf_hub_download(repo_id=repo, filename=name)


def tensors(path):
    """(name, tensor) for every tensor in a .safetensors file, read one at a time with plain file reads. No memory map:
    on Windows mapping a 2 GB weight file reserves its whole size in memory on top of the model, which is what ran an
    8 GB laptop out of memory ("The paging file is too small")."""
    import struct

    import torch
    kinds = {"F32": torch.float32, "F16": torch.float16, "BF16": torch.bfloat16, "F64": torch.float64,
             "I64": torch.int64, "I32": torch.int32, "I16": torch.int16, "I8": torch.int8, "U8": torch.uint8,
             "BOOL": torch.bool}
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(n))
        for name, info in sorted(header.items(), key=lambda kv: kv[1]["data_offsets"][0] if kv[0] != "__metadata__" else -1):
            if name == "__metadata__":
                continue
            start, end = info["data_offsets"]
            f.seek(8 + n + start)
            buf = bytearray(f.read(end - start))
            t = torch.frombuffer(buf, dtype=kinds[info["dtype"]]) if buf else torch.empty(0, dtype=kinds[info["dtype"]])
            yield name, t.reshape(info["shape"])


def load_standard_lean(half=True):
    """ChatterboxTTS.from_pretrained, but every weight is copied straight from the file into the model, one tensor at a
    time. The stock loader keeps a second full copy of each weight file while it loads (~7 GB at the peak), which fails
    on an 8 GB laptop ("The paging file is too small", or a crash).

    `half`: the token model (T3, 0.5 B parameters) is built in bfloat16: 1.06 GB instead of 2.1 GB. Measured on a
    4-core laptop CPU: same speed (8.7x vs 9.0x real time), every word still right, same pace. The audio decoder
    (S3Gen) stays float32. The whole model then needs ~2.6 GB instead of ~3.6 GB."""
    import gc

    import chatterbox.tts as ct
    import torch

    def fill(module, path, strict):
        own, seen = module.state_dict(), set()
        for k, t in tensors(path):
            if k in own:
                own[k].copy_(t)
                seen.add(k)
            elif strict:
                raise KeyError("unexpected weight %s in %s" % (k, path))
        if strict and len(seen) != len(own):
            raise KeyError("weights missing from %s: %s" % (path, [k for k in own if k not in seen][:5]))
        gc.collect()
        return module.eval()

    with torch.no_grad():
        ve = fill(ct.VoiceEncoder(), _path(ct.REPO_ID, "ve.safetensors"), True)
        if half:
            torch.set_default_dtype(torch.bfloat16)        # built in half size from the start: no full-size copy
        try:
            t3 = ct.T3()
        finally:
            torch.set_default_dtype(torch.float32)
        t3 = fill(t3, _path(ct.REPO_ID, "t3_cfg.safetensors"), True)          # copy_ converts float32 -> bfloat16
        s3gen = fill(ct.S3Gen(), _path(ct.REPO_ID, "s3gen.safetensors"), False)     # the stock loader is lenient here too
    conds = ct.Conditionals.load(_path(ct.REPO_ID, "conds.pt"), map_location=torch.device("cpu")).to("cpu")
    model = ct.ChatterboxTTS(t3, s3gen, ve, ct.EnTokenizer(_path(ct.REPO_ID, "tokenizer.json")), "cpu", conds=conds)
    if half:                                           # voice prompts come from float32 parts: match the token model
        prep = model.t3.prepare_conditioning
        model.t3.prepare_conditioning = lambda cond: prep(cond.to(dtype=torch.bfloat16))
    return model


def main():
    model_name = os.environ.get("NSV_CHATTERBOX_MODEL", "standard")
    threads = int(os.environ.get("NSV_THREADS", "0") or 0)
    t = time.time()
    import numpy as np
    import torch
    if threads:
        torch.set_num_threads(threads)
    if model_name in ("turbo", "nano"):
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        model = ChatterboxTurboTTS.from_pretrained(device="cpu", nano=model_name == "nano")
    else:
        try:
            model = load_standard_lean(half=os.environ.get("NSV_FULL_PRECISION") != "1")
        except (ImportError, AttributeError, KeyError, TypeError) as exc:   # a newer Chatterbox moved things: stock loader
            print("lean loader not usable (%s); using Chatterbox's own" % exc, file=sys.stderr)
            from chatterbox.tts import ChatterboxTTS
            model = ChatterboxTTS.from_pretrained(device="cpu")
    load_s = time.time() - t
    conds = {}                   # voice sample -> prepared conditionals (cloning prep is the slow part; do it once)
    default = model.conds

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
            op = job.get("op")
            if op == "bye":
                break
            if op == "hello":
                reply(ok=True, model=model_name, sr=model.sr, load_s=round(load_s, 1), threads=torch.get_num_threads())
                continue
            sample = job.get("sample")
            exag = float(job.get("exaggeration", 0.5))
            if sample:
                key = os.path.abspath(sample)
                if key not in conds:
                    model.prepare_conditionals(sample, exaggeration=exag)
                    conds[key] = model.conds
                model.conds = conds[key]
            else:
                model.conds = default
            torch.manual_seed(int(job.get("seed", 1)))
            np.random.seed(int(job.get("seed", 1)) % (2 ** 32))
            t = time.time()
            kw = dict(temperature=float(job.get("temperature", 0.8)))
            if model_name == "standard":
                kw.update(exaggeration=exag, cfg_weight=float(job.get("cfg", 0.5)))
            with torch.inference_mode():
                wav = model.generate(job["text"], **kw)
            pcm = wav.squeeze(0).detach().float().cpu().numpy()
            pcm = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
            tmp = job["out"] + ".part"
            with wave.open(tmp, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(model.sr)
                w.writeframes(pcm.tobytes())
            os.replace(tmp, job["out"])
            reply(ok=True, seconds=round(len(pcm) / float(model.sr), 3), took=round(time.time() - t, 2))
        except Exception as exc:  # noqa: BLE001 - report every failure to the caller, keep serving
            reply(ok=False, error=("%s: %s" % (type(exc).__name__, exc))[:400])


if __name__ == "__main__":
    main()
