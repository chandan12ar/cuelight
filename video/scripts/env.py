"""Small start-up helpers every script calls first, so a freshly set-up machine works instead of failing.

ensure_tools()  finds ffmpeg/ffprobe even when this terminal's PATH is stale. Right after `winget install Gyan.FFmpeg`
                (or `brew install ffmpeg`) the tool is on disk but a terminal opened earlier cannot see it until it is
                restarted; this adds the usual install folders to this process's PATH (children inherit it).
require_tools() the same, but stops with the one command that tells the user how to install what is missing.
safe_console()  printing narration with curly quotes or arrows to a redirected Windows console raises
                UnicodeEncodeError and kills a 20-minute build; replace unprintable characters instead.
free_memory_gb() memory free right now, so the expressive voice can warn before it runs out of memory.
"""
import glob
import os
import shutil
import sys

WINDOWS = sys.platform.startswith("win")


def _candidate_dirs():
    if WINDOWS:
        la = os.environ.get("LOCALAPPDATA", "")
        yield os.path.join(la, "Microsoft", "WinGet", "Links")
        yield from sorted(glob.glob(os.path.join(la, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "*", "bin")), reverse=True)
        yield os.path.join(os.environ.get("USERPROFILE", ""), "scoop", "shims")
        yield r"C:\ProgramData\chocolatey\bin"
        yield os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg", "bin")
        yield r"C:\ffmpeg\bin"
    else:
        yield "/opt/homebrew/bin"
        yield "/usr/local/bin"
        yield "/usr/bin"


def ensure_tools(tools=("ffmpeg", "ffprobe")):
    """{tool: path or None}. A tool found outside PATH has its folder added to PATH for this process."""
    found = {}
    for t in tools:
        p = shutil.which(t)
        if not p:
            for d in _candidate_dirs():
                exe = os.path.join(d, t + (".exe" if WINDOWS else ""))
                if os.path.isfile(exe):
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                    p = exe
                    break
        found[t] = p
    return found


def require_tools(tools=("ffmpeg", "ffprobe")):
    missing = [t for t, p in ensure_tools(tools).items() if not p]
    if missing:
        raise SystemExit("%s not found. Run `python scripts/doctor.py`: it prints the install command for this computer."
                         % " and ".join(missing))


def safe_console():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(errors="replace")


def free_memory_gb():
    """(free RAM, free commit) in GB right now; either is None where it cannot be read. The expressive voice needs
    ~3 GB: with less free RAM it crawls (paging), with less free commit (Windows: RAM + page file still unpromised)
    it crashes ("The paging file is too small")."""
    try:
        if WINDOWS:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullAvailPhys / 1e9, m.ullAvailPageFile / 1e9
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024 / 1e9, None
    except (OSError, AttributeError, ValueError):
        pass
    return None, None
