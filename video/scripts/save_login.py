#!/usr/bin/env python3
"""Save a signed-in browser session so the recorder can film pages behind a login.

    python save_login.py --url https://app.example.com/login --out state.json

Run this on YOUR machine (it opens a visible browser). Sign in by hand, including SSO / 2FA, then press
Enter in the terminal. The cookies and local storage are written to state.json; record with
build_video.py --storage-state state.json. Treat state.json like a password: it is a logged-in session.
Add it to .gitignore, never commit it. Delete it when you are done.
"""
import argparse

from playwright.sync_api import sync_playwright


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", default="state.json")
    ap.add_argument("--chromium", default=None)
    a = ap.parse_args()
    with sync_playwright() as pw:
        launch = {"headless": False}
        if a.chromium:
            launch["executable_path"] = a.chromium
        br = pw.chromium.launch(**launch)
        ctx = br.new_context()
        pg = ctx.new_page(); pg.goto(a.url)
        input("Sign in in the browser window, open a page that proves you are in, then press Enter here... ")
        ctx.storage_state(path=a.out)
        br.close()
    print("saved %s  (a logged-in session: keep it private, add to .gitignore)" % a.out)


if __name__ == "__main__":
    main()
