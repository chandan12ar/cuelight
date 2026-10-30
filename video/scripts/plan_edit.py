#!/usr/bin/env python3
"""Edit the storyboard by SECTION, the way the plan page shows it, then redraw the plan for a new approval.

    python plan_edit.py --script video/script.json swap 4 5          # exchange two sections
    python plan_edit.py --script video/script.json move 7 2          # put section 7 where section 2 is
    python plan_edit.py --script video/script.json remove 3          # drop a section
    python plan_edit.py --script video/script.json rename 3 "Usage"  # change its chapter title (the plan, YouTube chapter, on-screen title)
    python plan_edit.py --script video/script.json list              # numbered sections, as the plan shows them

Section numbers are the plan's (one per chapter; beats before the first chapter are section 1, "Opening"). The old storyboard
is kept as script.json.bak-N. Beats keep their own sentences and actions, so voice lines that did not change stay cached and
only new or changed lines are voiced again; the recording itself is redone by the build. Because the flow changed, the plan's
approval no longer matches: run plan_board.py to show the new plan.

Shortening or rewriting a section's sentences is editing the words, which is Claude's job (with the user's direction), not a
mechanical move: edit script.json, then run plan_board.py.
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import plan_board  # noqa: E402


def split(beats):
    """[[beat, ...], ...] one group per section, as plan_board.sections() cuts them."""
    groups = []
    for b in beats:
        if not groups or (isinstance(b, dict) and b.get("chapter")):
            groups.append([])
        groups[-1].append(b)
    return groups


def join(groups):
    beats = [b for g in groups for b in g]
    # a section that moves to the front must open with its chapter, and the first beat of an "Opening" keeps working
    return beats


def apply(script, op, args):
    groups = split(script["beats"])
    n = len(groups)

    def idx(v):
        i = int(v)
        if not 1 <= i <= n:
            raise SystemExit("section %s does not exist (1-%d)" % (v, n))
        return i - 1
    if op == "swap":
        a, b = idx(args[0]), idx(args[1])
        groups[a], groups[b] = groups[b], groups[a]
    elif op == "move":
        a, b = idx(args[0]), idx(args[1])
        groups.insert(b, groups.pop(a))
    elif op == "remove":
        groups.pop(idx(args[0]))
        if not groups:
            raise SystemExit("that would remove every section")
    elif op == "rename":
        g = groups[idx(args[0])]
        g[0]["chapter"] = args[1]
    for g in groups:                                  # the first beat of every section carries a chapter title
        if isinstance(g[0], dict) and not g[0].get("chapter"):
            g[0]["chapter"] = "Opening"
    script["beats"] = join(groups)
    return script


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--script", required=True)
    ap.add_argument("op", choices=("swap", "move", "remove", "rename", "list"))
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    with open(a.script, encoding="utf-8") as f:
        script = json.load(f)
    if a.op == "list":
        for s in plan_board.sections(script):
            print("%2d. %-24s ~%ds  %s" % (s["n"], s["title"], s["seconds"], "; ".join(s["jump"])))
        return
    need = {"swap": 2, "move": 2, "remove": 1, "rename": 2}[a.op]
    if len(a.args) != need:
        raise SystemExit("%s needs %d argument(s)" % (a.op, need))
    k = 1
    while os.path.exists("%s.bak-%d" % (a.script, k)):
        k += 1
    shutil.copy(a.script, "%s.bak-%d" % (a.script, k))
    apply(script, a.op, a.args)
    with open(a.script, "w", encoding="utf-8") as f:
        json.dump(script, f, indent=2, ensure_ascii=False)
    print("done (%s kept as %s.bak-%d). Redraw the plan for approval: python scripts/plan_board.py --script %s"
          % (os.path.basename(a.script), a.script, k, a.script))


if __name__ == "__main__":
    main()
