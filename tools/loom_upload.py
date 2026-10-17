#!/usr/bin/env python3
"""
Uploads an approved video to YOUR Loom account and gives it the same two things your hand-made videos have:

  - a comment holding your sign up link, posted near the END of the video (Loom timestamps a comment at the moment you post it)
  - Loom's own clickable link chip on the player, which Loom creates by itself from a link in a comment
  - a clean title, so the viewer does not see a file name

and returns the loom.com/share link for the email.

It drives a real Chrome window with its OWN saved profile (tools/.loom_profile, kept out of the repo), so it never touches your everyday Chrome.
You log in to Loom once in that window ("Connect Loom" in the CRM); Loom remembers it. Nothing else of yours is read.

    python3 tools/loom_upload.py login
    python3 tools/loom_upload.py upload <video.mp4> "<title>" "<comment text>" [--show]
"""
import json
import threading
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".loom_profile"
SESSION = HERE / "loom_session.json"
DEBUG = HERE / "loom_debug"
LIBRARY = "https://www.loom.com/looms/videos"


class NotLoggedIn(Exception):
    pass


def _ctx(p, show):
    PROFILE.mkdir(exist_ok=True)
    return p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=not show, viewport={"width": 1400, "height": 900},
                                                args=["--disable-blink-features=AutomationControlled"])


def _snap(page, name):
    try:
        DEBUG.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG / f"{name}.png"))
    except Exception:
        pass


def connected():
    try:
        d = json.loads(SESSION.read_text(encoding="utf-8"))
        return bool(d.get("ok")), d.get("at", 0)
    except Exception:
        return False, 0


def _mark(ok):
    SESSION.write_text(json.dumps({"ok": ok, "at": int(time.time())}), encoding="utf-8")


def login(timeout=600, log=print):
    """Opens a visible Chrome window on Loom's login page and waits until you are in. Never sees or stores your password: Loom keeps the session in the profile."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _ctx(p, True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(LIBRARY, wait_until="domcontentloaded")
            log("A Chrome window opened. Log in to Loom there. It closes by itself when you are in.")
            end = time.time() + timeout
            while time.time() < end:
                page.wait_for_timeout(2000)
                if re.search(r"loom\.com/(looms|home|spaces|share|settings)", page.url) and not re.search(r"login|signin|sso", page.url):
                    try:
                        page.get_by_role("button", name=re.compile(r"New video", re.I)).first.wait_for(timeout=4000)
                        _mark(True)
                        log("Loom is connected.")
                        return True
                    except Exception:
                        continue
            _mark(False)
            log("Timed out waiting for the Loom login.")
            return False
        finally:
            ctx.close()


def _open_library(page):
    page.goto(LIBRARY, wait_until="domcontentloaded")
    page.wait_for_timeout(3500)
    if re.search(r"login|signin|sso", page.url):
        _mark(False)
        raise NotLoggedIn("Loom asked for a login: press Connect Loom in the CRM")


def _find_card_link(page, token):
    """The /share/ link of the library card whose title holds the token (Loom names an uploaded video after its file)."""
    return page.evaluate("""(tok)=>{const links=[...document.querySelectorAll('a[href*="/share/"]')];
      for(const a of links){const card=a.closest('div[class]')||a;let n=a,txt='';for(let i=0;i<6&&n;i++){txt=(n.innerText||'');if(txt.includes(tok))return a.href.split('?')[0];n=n.parentElement}}return ''}""", token)


def _share_links(page):
    """Every /share/ link on the library page, newest first (Loom lists the newest video first)."""
    return page.evaluate("""()=>[...new Set([...document.querySelectorAll('a[href*="/share/"]')].map(a=>a.href.split('?')[0]))]""")


CANCEL = threading.Event()  # set by the helper's Stop button; the upload loop checks it


UPLOADS = HERE / "loom_uploads.json"  # video file name -> its Loom link, so a retry never uploads the same video twice


def _uploads():
    try:
        return json.loads(UPLOADS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _remember(stem, link):
    d = _uploads()
    d[stem] = {"link": link, "at": int(time.time())}
    UPLOADS.write_text(json.dumps(d, indent=1), encoding="utf-8")


SEEK_JS = """(at)=>{let v=null,d=0;for(const x of document.querySelectorAll('video')){const t=isFinite(x.duration)?x.duration:0;if(t>d){d=t;v=x}}
 if(v&&d>3){v.pause();v.currentTime=(at!==null&&at!==undefined)?Math.max(0,Math.min(at,d-0.5)):Math.max(0,d-8)}return d}"""


def _meta(video):
    """What the video maker recorded about this video (tools/ad_videos/<id>.json): where the call to action starts."""
    try:
        return json.loads(Path(video).with_suffix(".json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _domain(url):
    return re.sub(r"^https?://", "", url).split("/")[0].lower()


def _link_on_page(page, url):
    """True when the sign up link is already on the video page as a link (Loom's Links area or its link chip)."""
    dom = _domain(url).replace("www.", "")
    root = dom.split(".")[-2] if dom.count(".") >= 1 else dom
    try:
        return page.locator("a[href*='" + root + "']").count() > 0
    except Exception:
        return False


def _rename(page, title, log):
    """Gives the video a clean title. Loom may still be processing the video and then offers an AI title, so it tries a few times, checks the title really changed, and
    leaves Loom's own title if it never works."""
    short = re.sub(r"\s+", " ", title).strip()
    for attempt in range(12):
        try:
            btn = page.get_by_role("button", name=re.compile(r"^Rename video", re.I)).first
            btn.click(timeout=4000)
            box = page.locator("input:focus").first
            box.fill(title, timeout=3000)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
            now = page.get_by_role("button", name=re.compile(r"^Rename video", re.I)).first.get_attribute("aria-label") or ""
            if short.lower() in now.lower():
                return True
        except Exception:
            pass
        page.wait_for_timeout(4000)
    log("  (could not rename the video, left Loom's own title)")
    return False


def _links_report(page, url):
    """How many links Loom lists under the video, and whether the sign up link is one of them."""
    try:
        n = page.evaluate("""()=>[...document.querySelectorAll('button,[role=button],[aria-label]')].filter(e=>/^Delete link$/i.test((e.innerText||e.getAttribute('aria-label')||'').trim())).length""")
    except Exception:
        n = 0
    ours = _link_on_page(page, url)
    return {"listed": n, "ours": ours, "other": max(0, n - (1 if ours else 0))}


def _persistent(page, log):
    """'Only show links at the end of the video' must be OFF so the link stays on the video the whole time. Returns True when it is off."""
    try:
        cb = page.get_by_role("checkbox", name=re.compile(r"Only show links at the end", re.I)).first
        if cb.is_checked():
            cb.uncheck(force=True, timeout=4000)
            page.wait_for_timeout(1000)
        return not cb.is_checked()
    except Exception:
        return False


def _add_link(page, url, label, log):
    """Loom's own Links box: Add a link, the URL, a short title, Save."""
    try:
        page.get_by_role("button", name=re.compile(r"Add a link", re.I)).first.click(timeout=6000)
        page.wait_for_timeout(800)
        page.get_by_placeholder(re.compile(r"Link URL", re.I)).first.fill(url, timeout=5000)
        try:
            page.get_by_placeholder(re.compile(r"Add title", re.I)).first.fill(label, timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(500)
        try:  # the link stays on the video the whole time
            page.get_by_role("checkbox", name=re.compile(r"Pin this link", re.I)).first.check(force=True, timeout=2000)
        except Exception:
            pass
        page.get_by_role("button", name=re.compile(r"^Save$", re.I)).first.click(timeout=5000)
        page.wait_for_timeout(2000)
        return _link_on_page(page, url)
    except Exception as e:
        log("  (could not add the link in Loom's Links box: " + str(e).splitlines()[0][:100] + ")")
        return False


def _comment_exists(page, url):
    """Loom lists comments in the Activity tab. True when a comment holding this link is already there (so a retry never posts it twice)."""
    try:
        t = page.get_by_role("tab", name=re.compile(r"^Activity$", re.I))
        (t.first if t.count() else page.get_by_text("Activity", exact=True).first).click(timeout=4000)
        page.wait_for_timeout(1800)
        txt = page.inner_text("body").replace("\n", " ").lower()
        return bool(re.search(r"\b[1-9]\d* comments?\b", txt)) and _domain(url) in txt
    except Exception:
        return False


def _post_comment(page, comment, log, at=None):
    """A comment holding the link. Loom stamps it with the playing time, so the video is paused at `at` first: the call to action, right after the video says we only get
    paid on performance (near the end if that moment is not known)."""
    try:
        if _comment_exists(page, comment):
            log("  the comment with the link is already on this video")
            return True
        try:  # back to the Edit tab, where the Comment button is
            page.get_by_role("tab", name=re.compile(r"^Edit$", re.I)).first.click(timeout=3000)
            page.wait_for_timeout(800)
        except Exception:
            pass
        dur = 0.0
        for _ in range(30):
            dur = page.evaluate(SEEK_JS, at) or 0
            if dur > 3:
                break
            page.wait_for_timeout(2000)
        page.wait_for_timeout(1200)
        page.get_by_role("button", name=re.compile(r"^Comment$", re.I)).first.click(timeout=8000)
        page.wait_for_timeout(1000)
        box = page.locator("[role=textbox]:visible").first
        box.click(timeout=6000)
        page.keyboard.type(comment, delay=25)
        page.wait_for_timeout(600)
        page.get_by_role("button", name=re.compile(r"^Comment at ", re.I)).first.click(timeout=6000)
        page.wait_for_timeout(2000)
        for _ in range(6):
            if _comment_exists(page, comment):
                return True
            page.wait_for_timeout(1500)
        return False
    except Exception as e:
        log("  (could not post the comment: " + str(e).splitlines()[0][:100] + ")")
        return False


def upload_and_comment(video, title, comment, show=False, log=print):
    """Uploads the video (unless this file was already uploaded: then it reuses that Loom link), gives it a clean title, makes sure the sign up link is in Loom's Links area,
    and posts it as a comment near the end. Returns {'ok': True, 'link': ..., 'link_on_page': bool, 'comment_posted': bool}. The Loom link is returned in every case where the video
    reached Loom, even if a later step fails ({'ok': False, 'error': ..., 'link': ...}), so it is never lost."""
    from playwright.sync_api import sync_playwright
    video = Path(video)
    token = re.sub(r"[^A-Za-z0-9]", "", video.stem)[:24] or "advideo"
    known = (_uploads().get(token) or {}).get("link", "")
    if not known and not video.exists():
        return {"ok": False, "error": f"the video file is missing: {video}"}
    staged = HERE / f"{token}.mp4"  # Loom titles the upload after the file, so the file carries a unique token we can find again
    if not known and staged != video:
        staged.write_bytes(video.read_bytes())
    link = known
    try:
        with sync_playwright() as p:
            ctx = _ctx(p, show)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if known:
                    log("  already uploaded earlier, reusing: " + known)
                else:
                    _open_library(page)
                    before = set(_share_links(page))  # Loom renames uploads on its own (an AI title), so the new video is found as the card that was not there before
                    for attempt in (1, 2):   # Loom sometimes does not start an upload: one more try when no new video shows up
                        log("  opening the upload box" + (" (second try)" if attempt == 2 else ""))
                        page.get_by_role("button", name=re.compile(r"^New video$", re.I)).first.click(timeout=8000)
                        page.wait_for_timeout(600)
                        try:
                            page.get_by_text(re.compile(r"Upload a video", re.I)).first.click(timeout=4000)
                        except Exception:
                            pass
                        page.wait_for_selector("input.uppy-Dashboard-input", state="attached", timeout=10000)
                        page.locator("input.uppy-Dashboard-input").first.set_input_files(str(staged))
                        page.wait_for_timeout(1500)
                        for pat in (r"^Upload (1 )?file", r"^Upload$", r"^Start upload", r"^Done$"):  # the dialog may need a confirm click
                            try:
                                page.get_by_role("button", name=re.compile(pat, re.I)).first.click(timeout=2500)
                                break
                            except Exception:
                                continue
                        log("  uploading; waiting for it to appear in the library")
                        for i in range(24 if attempt == 1 else 48):  # about 2 minutes the first time, about 6 the second
                            if CANCEL.is_set():
                                return {"ok": False, "error": "stopped by you"}
                            page.wait_for_timeout(4500)
                            try:
                                page.goto(LIBRARY, wait_until="domcontentloaded")
                                page.wait_for_timeout(2500)
                            except Exception:
                                continue
                            link = _find_card_link(page, token)
                            if not link:
                                new = [l for l in _share_links(page) if l not in before]
                                link = new[0] if new else ""
                            if link:
                                break
                        if link:
                            break
                        _snap(page, f"upload_not_found_{attempt}")
                        if attempt == 1:
                            log("  (nothing appeared in the library, trying the upload once more)")
                            _open_library(page)
                    if not link:
                        return {"ok": False, "error": "the upload did not show up in your Loom library in time (see tools/loom_debug)"}
                    _remember(token, link)  # from here on the video is on Loom: its link is kept whatever happens next
                    log("  uploaded: " + link)
                page.goto(link, wait_until="domcontentloaded")
                page.wait_for_timeout(6000)
                _rename(page, title, log)
                added = False
                link_ok = _link_on_page(page, comment)
                if not link_ok:
                    added = link_ok = _add_link(page, comment, "Sign up", log)
                persistent = _persistent(page, log)
                rep_ = _links_report(page, comment)
                if rep_["other"]:
                    log(f"  (Loom lists {rep_['other']} other link(s) under this video; only the sign up link is wanted, remove the others in Loom)")
                at = _meta(video).get("link_from")
                comment_ok = _post_comment(page, comment, log, at)
                if not (link_ok or comment_ok):
                    _snap(page, "link_and_comment_failed")
                    return {"ok": False, "error": "the video is on Loom but the sign up link could not be added (see tools/loom_debug)", "link": link}
                if not comment_ok:
                    _snap(page, "comment_not_posted")
                return {"ok": True, "link": link, "link_on_page": bool(link_ok), "link_added": bool(added), "persistent": bool(persistent), "other_links": rep_["other"],
                        "comment_posted": bool(comment_ok), "comment_at": at}
            finally:
                ctx.close()
    except NotLoggedIn as e:
        return {"ok": False, "error": str(e), "needs_login": True, **({"link": link} if link else {})}
    except Exception as e:
        return {"ok": False, "error": "Loom upload failed: " + str(e)[:200], **({"link": link} if link else {})}
    finally:
        if staged != video:
            try:
                staged.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "login":
        sys.exit(0 if login() else 1)
    if a and a[0] == "upload" and len(a) >= 4:
        print(json.dumps(upload_and_comment(a[1], a[2], a[3], show="--show" in a), indent=1))
    else:
        sys.exit(__doc__)
