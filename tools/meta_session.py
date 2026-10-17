#!/usr/bin/env python3
"""
An optional Facebook login for the Meta Ad Library lookups (tools/ad_video.py).

Meta sometimes answers every Ad Library search from this computer with "No ads match your search criteria" (even for Nike): a block of this
network or of logged-out automated browsing. A logged-in browser profile is treated far more kindly. "Connect Meta" in the CRM opens a VISIBLE Chrome
window with the helper's own profile (tools/.meta_profile, git-ignored); you log in to Facebook there yourself (or solve whatever Meta shows),
and the window closes by itself once the Ad Library answers. After that the lookups run headless inside that profile.

The code never sees or stores your password (Facebook keeps the session in the profile) and never solves a CAPTCHA or checkpoint for you.
Use an account you are comfortable using for this: lots of automated searches from a personal account can get it restricted.
"""
import json
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".meta_profile"
SESSION = HERE / "meta_session.json"
VIEWPORT = {"width": 1280, "height": 720}


def connected():
    try:
        d = json.loads(SESSION.read_text(encoding="utf-8"))
        return bool(d.get("ok")) and PROFILE.exists(), d.get("at", 0)
    except Exception:
        return False, 0


def _mark(ok):
    SESSION.write_text(json.dumps({"ok": ok, "at": int(time.time())}), encoding="utf-8")


def mark(ok):
    _mark(ok)


def context(p, show=False):
    """A persistent Chrome context in the Meta profile (headless unless show)."""
    PROFILE.mkdir(exist_ok=True)
    return p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=not show, viewport=VIEWPORT, locale="en-US",
                                                args=["--disable-blink-features=AutomationControlled"])


def login(timeout=900, log=print):
    """Visible window on the Ad Library (with a Log in button): wait until a Facebook session exists AND the Ad Library answers a search."""
    import ad_video
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = context(p, True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.facebook.com/login/?next=" + "https%3A%2F%2Fwww.facebook.com%2Fads%2Flibrary%2F", wait_until="domcontentloaded")
            log("A Chrome window opened. Log in to Facebook there (and finish anything Meta asks). It closes by itself when the Ad Library answers.")
            end = time.time() + timeout
            while time.time() < end:
                page.wait_for_timeout(3000)
                try:
                    cookies = {c["name"] for c in ctx.cookies("https://www.facebook.com")}
                except Exception:
                    cookies = set()
                if "c_user" in cookies and not re.search(r"/login|checkpoint|two_step", page.url):
                    probe = ctx.new_page()
                    try:
                        ok = ad_video.lookup_healthy(probe, log)
                    finally:
                        probe.close()
                    if ok:
                        _mark(True)
                        log("Meta is connected: the Ad Library answers in this profile.")
                        return True
                    log("Logged in, but the Ad Library still does not answer from here. Leaving the window open; it will keep checking.")
                    page.goto("https://www.facebook.com/ads/library/", wait_until="domcontentloaded")
            _mark(False)
            log("Timed out waiting for the Meta login.")
            return False
        finally:
            ctx.close()
