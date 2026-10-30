#!/usr/bin/env python3
"""The voice lab: a separate Python environment for the expressive, cloneable voice (Chatterbox, open source, MIT).

    python scripts/voicelab.py setup [--home D:/voices]   # once: own Python env + Chatterbox + model (~5 GB), then a test
    python scripts/voicelab.py status                     # where it is, whether it works, how fast it speaks here
    python scripts/voicelab.py say "Text" --out x.wav [--mood excited] [--sample voices/me.wav]

Why separate: Chatterbox needs PyTorch, numpy 1.x and exact versions of transformers and diffusers. Put into the main
Python they could break other tools, so the lab gets its own environment, used only through voicelab_worker.py. The
build talks to one worker process that loads the model once and voices every line.

Where it lives: $NSV_VOICE_HOME, else the folder named in ~/.cache/narrated-screen-video/voicelab-home.txt (written by
`setup --home`), else ~/.cache/narrated-screen-video/voicelab. The model (Hugging Face: ResembleAI/chatterbox) is kept
inside it, so deleting the folder removes everything.

Every clip Chatterbox makes carries Resemble's inaudible Perth watermark (it marks the audio as AI-made); keep it.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(os.path.expanduser("~"), ".cache", "narrated-screen-video")
POINTER = os.path.join(CONF, "voicelab-home.txt")
WINDOWS = os.name == "nt"


def home():
    h = os.environ.get("NSV_VOICE_HOME")
    if not h and os.path.exists(POINTER):
        with open(POINTER, encoding="utf-8") as f:
            h = f.read().strip()
    return os.path.abspath(h or os.path.join(CONF, "voicelab"))


def python_exe(h=None):
    h = h or home()
    return os.path.join(h, "venv", "Scripts" if WINDOWS else "bin", "python.exe" if WINDOWS else "python")


def ready():
    """The lab's last successful self-test ({"model", "rtf", ...}) or None when it is not set up."""
    p = os.path.join(home(), "ready.json")
    if not (os.path.exists(p) and os.path.exists(python_exe())):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def worker_env(model):
    h = home()
    env = dict(os.environ, HF_HOME=os.path.join(h, "hf"), HF_HUB_DISABLE_SYMLINKS_WARNING="1", PYTHONUNBUFFERED="1",
               PYTHONIOENCODING="utf-8", NSV_CHATTERBOX_MODEL=model, TOKENIZERS_PARALLELISM="false")
    env.pop("PYTHONPATH", None)                       # nothing from the main Python may leak into the lab
    env.pop("PYTHONHOME", None)
    return env


class Worker:
    """One voicelab_worker.py process: loads the model once, then voices one line per call.

    Every call has a time limit: with too little free memory Windows swaps the model to disk and a sentence that
    normally takes two minutes took hours (measured: 9 % of one sentence in 2 h 20 min). Past the limit the worker is
    stopped and the build says why, instead of hanging."""

    LOAD_LIMIT = 1800             # seconds: the first run also downloads ~3 GB

    def __init__(self, model="standard", log_path=None):
        import queue
        import threading
        if not os.path.exists(python_exe()):
            raise SystemExit("the voice lab is not set up (python scripts/voicelab.py setup)")
        self.log_path = log_path or os.path.join(home(), "worker.log")
        self.log = open(self.log_path, "a", encoding="utf-8", errors="replace")
        self.log.write("\n==== %s model=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), model))
        self.log.flush()
        self.p = subprocess.Popen([python_exe(), "-u", os.path.join(HERE, "voicelab_worker.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self.log, text=True, encoding="utf-8", errors="replace",
                                  env=worker_env(model), cwd=home())
        self.lines = queue.Queue()                 # replies, read on a thread so a call can give up waiting
        threading.Thread(target=self._pump, daemon=True).start()
        self.info = self.call(op="hello", limit=self.LOAD_LIMIT)

    def _pump(self):
        for line in self.p.stdout:
            self.lines.put(line)
        self.lines.put("")

    def call(self, limit=None, **job):
        import queue
        if self.p.poll() is not None:
            raise RuntimeError("the voice lab worker stopped (exit %s); see %s" % (self.p.returncode, self.log_path))
        self.p.stdin.write(json.dumps(job) + "\n")
        self.p.stdin.flush()
        end = time.time() + limit if limit else None
        while True:
            try:
                line = self.lines.get(timeout=max(0.1, end - time.time()) if end else None)
            except queue.Empty:
                self.kill()
                raise RuntimeError("Chatterbox took more than %d s for one step - many times slower than normal, which "
                                   "means the computer ran out of free memory and is swapping to disk. Close the browser "
                                   "and other big programs and build again (lines already voiced are kept), or use the "
                                   "edge voice for this build." % limit)
            if not line:
                code = self.p.wait(timeout=30)
                tail, hint = self.tail(), ""
                # access violation / segfault / killed, or Windows "paging file too small": out of memory while loading
                if code in (3221225477, -1073741819, 139, 137, -9, -11) or any(
                        x in tail for x in ("paging file is too small", "not enough memory", "MemoryError", "bad allocation")):
                    hint = "\n(out of memory: Chatterbox needs ~3 GB free. Close browsers and other big programs, then " \
                           "run: python scripts/voicelab.py test)"
                raise RuntimeError("the voice lab worker stopped (exit %s); the last lines of %s say why:\n%s%s"
                                   % (code, self.log_path, tail, hint))
            line = line.strip()
            if line.startswith("{"):
                r = json.loads(line)
                if not r.get("ok"):
                    raise RuntimeError("the voice lab could not voice this line: %s" % r.get("error"))
                return r

    def kill(self):
        """Stop the worker and its child: on Windows the venv's python.exe is a launcher that starts the real
        interpreter, and killing only the launcher would leave the model (~2.6 GB) running."""
        if self.p.poll() is None:
            if WINDOWS:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.p.pid)], capture_output=True)
            else:
                self.p.kill()

    def tail(self, n=12):
        try:
            self.log.flush()
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except OSError:
            return ""

    def close(self):
        if self.p.poll() is None:
            try:
                self.p.stdin.write(json.dumps({"op": "bye"}) + "\n")
                self.p.stdin.flush()
                self.p.wait(timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                self.kill()
        self.log.close()


def _run(cmd, log):
    print("  $ " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if r.returncode:
        raise SystemExit("failed (exit %d): %s - the end of %s says why" % (r.returncode, " ".join(cmd), log.name))


def setup(h, model):
    import shutil
    os.makedirs(h, exist_ok=True)
    if h != os.path.join(CONF, "voicelab"):            # remember a custom place so every script finds it
        os.makedirs(CONF, exist_ok=True)
        with open(POINTER, "w", encoding="utf-8") as f:
            f.write(h)
    os.environ["NSV_VOICE_HOME"] = h
    free = shutil.disk_usage(h).free / 1e9
    print("voice lab: %s (%.0f GB free; needs ~5 GB)" % (h, free))
    if free < 6:
        raise SystemExit("not enough room: pass --home on a drive with at least 6 GB free")
    log_path = os.path.join(h, "setup.log")
    with open(log_path, "a", encoding="utf-8") as log:
        if not os.path.exists(python_exe(h)):
            print("creating its own Python environment...")
            _run([sys.executable, "-m", "venv", os.path.join(h, "venv")], log)
        print("installing Chatterbox and PyTorch (CPU) - a few minutes, ~1 GB download...")
        _run([python_exe(h), "-m", "pip", "install", "--upgrade", "pip"], log)
        _run([python_exe(h), "-m", "pip", "install", "chatterbox-tts"], log)
    return test(model)


def test(model):
    """Load the model (the first time this downloads it, ~3 GB) and voice two lines; record the speed."""
    print("loading the %s Chatterbox model (the first time downloads ~3 GB)..." % model)
    t = time.time()
    w = Worker(model)
    print("  loaded in %.0f s, %d CPU threads" % (time.time() - t, w.info.get("threads", 0)))
    out = os.path.join(home(), "test.wav")
    took = spoke = 0.0
    try:
        for text, exag, cfg in (("This is the voice lab, ready to narrate your video.", 0.5, 0.5),
                                ("And it can sound genuinely excited when the moment calls for it!", 0.85, 0.3)):
            r = w.call(op="say", text=text, out=out, seed=7, exaggeration=exag, cfg=cfg)
            took += r["took"]
            spoke += r["seconds"]
    finally:
        w.close()
    rtf = took / max(spoke, 0.1)
    info = {"model": model, "rtf": round(rtf, 2), "tested": time.strftime("%Y-%m-%d %H:%M"), "python": python_exe()}
    with open(os.path.join(home(), "ready.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)
    print("READY: Chatterbox speaks %.1f s of audio in %.1f s here (%.1fx real time). A 1-minute narration takes about %d "
          "minutes to voice. Test clip: %s" % (spoke, took, rtf, max(1, round(rtf)), out))
    return 0


def main():
    sys.path.insert(0, HERE)
    import env
    env.safe_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="create the lab, install Chatterbox, download the model, test it")
    s.add_argument("--home", default=None, help="where to put it (~5 GB); default %s" % os.path.join(CONF, "voicelab"))
    s.add_argument("--model", default="standard", choices=("standard", "turbo"))
    sub.add_parser("status", help="is it set up, and how fast is it here")
    t = sub.add_parser("test", help="voice two test lines again and re-measure the speed")
    t.add_argument("--model", default="standard", choices=("standard", "turbo"))
    y = sub.add_parser("say", help="voice one line (to audition a mood or a cloned voice)")
    y.add_argument("text")
    y.add_argument("--out", required=True)
    y.add_argument("--mood", default="neutral")
    y.add_argument("--sample", default=None, help="a voice sample prepared with prepare_voice.py (needs its consent record)")
    y.add_argument("--check", action="store_true", help="listen with Whisper: bad takes are re-made, pace is set to the mood, "
                                                         "and it fails when no take comes out right (as in a build)")
    a = ap.parse_args()
    if a.cmd == "setup":
        return setup(os.path.abspath(a.home) if a.home else home(), a.model)
    if a.cmd == "status":
        r = ready()
        print("voice lab: %s" % home())
        if not r:
            print("NOT SET UP: python scripts/voicelab.py setup [--home <a drive with 6 GB free>]")
            return 1
        print("READY: model %s, %.1fx real time (tested %s)" % (r["model"], r["rtf"], r["tested"]))
        return 0
    if a.cmd == "test":
        return test(a.model)
    if a.cmd == "say":
        import voices
        if a.sample:
            why = voices.check_consent(a.sample)
            if why:
                raise SystemExit(why)
        # the same path a build takes: takes checked by listening, trimmed, paced to the mood
        import narrate
        import tts
        _, _, exag, cfg = voices.MOODS[a.mood]
        sha = voices.file_sha(a.sample) if a.sample else ""
        line = voices.rekey({"i": 0, "text": a.text, "caption": a.text, "voice": "sample:%s" % os.path.basename(a.sample)
                             if a.sample else "default", "sample": os.path.abspath(a.sample) if a.sample else None,
                             "sample_sha": sha, "mood": a.mood, "rate": "+0%", "exaggeration": exag, "cfg": cfg,
                             "engine": "chatterbox", "model": tts.chatterbox_model()})
        short = narrate.too_little_memory()
        if short:
            raise SystemExit(short + " Close the browser and other big programs, then try again.")
        t = time.time()
        v = narrate.voice_chatterbox([line], {0: os.path.abspath(a.out)}, "auto" if a.check else "off", print)[0]
        print("wrote %s (%.1f s of audio, made in %.0f s, mood %s, take %d)" % (a.out, tts.duration(a.out), time.time() - t,
                                                                              a.mood, v.get("take", 1)))
        if v.get("match") is not None:
            print("heard: %s\n%d%% of the words right, %s words/min%s" % (v.get("heard", ""), v["match"] * 100, v["wpm"],
                                                                         "" if not v["bad"] else "  PROBLEM: " + "; ".join(v["bad"])))
            return 1 if v["bad"] else 0
        return 0


if __name__ == "__main__":
    sys.exit(main())
