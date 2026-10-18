#!/usr/bin/env python3
"""
Local helper so the website's "Run live" button can start google_ai_to_crm.py on your computer.

Start it once and leave the window open:
    cd ~/clipper-crm && python3 tools/local_runner.py

It only listens on 127.0.0.1 and only accepts requests from this site (or localhost). Your keys live in
tools/.env (copy tools/.env.example) and are never sent to the browser.
"""
import json, os, random, re, subprocess, sys, threading, time
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cold_email, email_finder, personalize, auto_reply, phone_finder, coach_finder, meta_reply, greeting, send_queue, crm_store, outreach_loop
SCRIPT = os.getenv("RUNNER_SCRIPT", str(HERE / "google_ai_to_crm.py"))
PORT = int(os.getenv("RUNNER_PORT", "8765"))
APP_URL = os.getenv("CLIPPER_URL", "http://127.0.0.1:5001").rstrip("/")
GRAB = os.getenv("GRAB_SCRIPT", str(HERE / "grab_sample.sh"))
SAMPLES = os.getenv("SAMPLES_DIR", str(Path.home() / "Downloads" / "clip-samples"))
OUT_FOLDER = os.getenv("SAMPLES_FOLDER", "Podcast Outreach")
SETTINGS_FILE = Path(os.getenv("OUTREACH_SETTINGS_FILE", str(HERE / "outreach_settings.json")))
_DEFAULT_SETTINGS = {"captions": True, "quality": "high", "silence_cut": True, "silence_secs": "0.8", "quote": True,
                     "reuse": False, "viral": False, "full_thought": True, "host_question": False, "hook": True,
                     "ai": True, "after": "draft"}
REVEAL_CMD = os.getenv("REVEAL_CMD", "open -R").split()
ALLOWED = {"https://pharaohm33.github.io"}
STATE = {"running": False, "log": [], "proc": None}
LOCK = threading.Lock()


def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def origin_ok(origin):
    if not origin:
        return True
    return origin in ALLOWED or re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", origin) is not None


def clean(s, n=80):
    return re.sub(r"[^\w\s.,'&/+-]", "", str(s))[:n].strip()


def run_job(argv):
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.Popen([sys.executable, "-u", SCRIPT, *argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, cwd=str(HERE.parent))
        STATE["proc"] = proc
        for line in proc.stdout:
            STATE["log"].append(line.rstrip())
            if "asking you to verify" in line:
                if phone_notify("Google needs you", "Google is asking for a verification and the lead search is waiting (up to 5 minutes). " + REMOTE_HINT, key="captcha"):
                    STATE["log"].append("[runner] sent a phone alert")
        proc.wait()
        STATE["log"].append(f"[runner] finished (exit {proc.returncode})")
    except Exception as e:
        STATE["log"].append(f"[runner] failed to start: {e}")
    finally:
        STATE["running"] = False


def run_fit(path):
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.Popen([sys.executable, "-u", str(HERE / "fit_check.py"), path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, cwd=str(HERE))
        STATE["proc"] = proc
        for line in proc.stdout:
            STATE["log"].append(line.rstrip())
            if "asking you to verify" in line:
                if phone_notify("Google needs you", "Google is asking for a verification and the fit check is waiting (up to 5 minutes). " + REMOTE_HINT, key="captcha"):
                    STATE["log"].append("[runner] sent a phone alert")
        proc.wait()
        STATE["log"].append(f"[runner] fit check finished (exit {proc.returncode})")
    except Exception as e:
        STATE["log"].append(f"[runner] fit check failed to start: {e}")
    finally:
        STATE["running"] = False


def run_email_check(path, ai, jitbit=True):
    """Checks lead emails (tools/email_ai_check.py) and streams its 'EMAILCHECK {json}' lines to the page."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.Popen([sys.executable, "-u", str(HERE / "email_ai_check.py"), path] + ([] if ai else ["--no-ai"]) + ([] if jitbit else ["--no-jitbit"]), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, cwd=str(HERE))
        STATE["proc"] = proc
        for line in proc.stdout:
            STATE["log"].append(line.rstrip())
            if "asking you to verify" in line:
                phone_notify("Google needs you", "Google is asking for a verification and the email check is waiting (up to 5 minutes).", key="captcha")
        proc.wait()
        STATE["log"].append(f"[runner] email check finished (exit {proc.returncode})")
    except Exception as e:
        STATE["log"].append(f"[runner] email check failed to start: {e}")
    finally:
        STATE["running"] = False


def outreach_settings():
    """The clipper settings every outreach template starts with (tools/outreach_settings.json; edit that file to change them)."""
    try:
        return {**_DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(_DEFAULT_SETTINGS)


def app_call(path, payload=None, timeout=5):
    """JSON call to the local clipper app. Returns (status_code, dict) or (0, {}) if it is unreachable."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(APP_URL + path, data=data, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


AWAKE = {"proc": None, "until": 0}


RISKY_TLDS = {"online", "site", "space", "store", "world", "xyz", "top", "click", "club", "shop", "icu", "buzz", "monster", "cyou", "sbs", "quest", "rest", "fun", "work", "win", "bid", "vip", "live", "website", "tech", "cfd"}
PHONE_LAST = {}
REMOTE_HINT = os.getenv("PHONE_REMOTE_HINT", "To fix it from your phone: open RVNC Viewer, connect to your Mac (emmanuels-macbook-air.taile1d9f0.ts.net or its Tailscale address), find the Chrome window and complete the check there.")


def phone_ready():
    return bool(os.getenv("PHONE_BOT_TOKEN") and os.getenv("PHONE_CHAT_ID"))


NOTIFY_PREFS = HERE / "notify_prefs.json"


def notify_prefs():
    try:
        return json.loads(NOTIFY_PREFS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def clips_alerts():
    """Alerts about the podcast clips side (the Clipper app restarting, Autopilot, drafts from podcast leads). Off unless you switch them on in the CRM settings."""
    return bool(notify_prefs().get("clips"))


def phone_notify(title, message, key=None, every=600, topic=""):
    """A push message to your phone through your Telegram bot. The same alert is not repeated within `every` seconds. Returns True if sent."""
    if topic == "clips" and not clips_alerts():
        return False
    if not phone_ready():
        return False
    k = key or title
    if time.time() - PHONE_LAST.get(k, 0) < every:
        return False
    PHONE_LAST[k] = time.time()
    text = f"ClipperCRM: {title}\n{message}"
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{os.environ['PHONE_BOT_TOKEN']}/sendMessage",
                                     data=json.dumps({"chat_id": os.environ["PHONE_CHAT_ID"], "text": text[:900]}).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:
        return False


def awake_status():
    """Is this tool keeping the Mac from sleeping (a 'caffeinate' it started), how long is left, and will closing the lid still sleep it."""
    p = AWAKE["proc"]
    on = bool(p and p.poll() is None)
    out = {"on": on, "seconds_left": max(0, int(AWAKE["until"] - time.time())) if on else 0, "on_ac": None, "battery": None, "lid_sleep_disabled": False}
    if sys.platform == "darwin":
        try:
            b = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
            out["on_ac"] = "AC Power" in b
            m = re.search(r"(\d+)%", b)
            out["battery"] = int(m.group(1)) if m else None
            s = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"SleepDisabled\s+(\d)", s)
            out["lid_sleep_disabled"] = bool(m and m.group(1) == "1")
        except Exception:
            pass
    return out


def awake_set(on, minutes=180):
    """Starts or stops keeping the Mac awake. It always has an end time, so it cannot be left on forever by accident."""
    p = AWAKE["proc"]
    if p and p.poll() is None:
        p.terminate()
    AWAKE["proc"] = None
    if on and sys.platform == "darwin":
        minutes = max(5, min(int(minutes or 180), 720))
        AWAKE["proc"] = subprocess.Popen(["caffeinate", "-dimsu", "-t", str(minutes * 60)])
        AWAKE["until"] = time.time() + minutes * 60
    return awake_status()


def clipper_status():
    code, d = app_call("/api/clipper/status", timeout=2)
    if code != 200:
        return {"state": "off", "url": APP_URL}
    return {"state": "busy" if d.get("running") else "on", "stage": d.get("stage"), "message": d.get("message"),
            "pct": d.get("pct"), "error": d.get("error"), "template_slug": d.get("template_slug"), "url": APP_URL}


YT_RE = re.compile(r"^https://((www|m)\.)?(youtube\.com|youtu\.be)/\S+$")
TIME_RE = re.compile(r"^\d{1,2}(:\d{2}){1,2}$")


def sample_templates():
    code, d = app_call("/api/clip-templates", timeout=5)
    if code != 200:
        return None
    out = []
    for t in d.get("templates", []):
        f = t.get("folder") or ""
        if f == OUT_FOLDER or f.startswith(OUT_FOLDER + "/"):
            vp = t.get("video_path") or ""
            out.append({"slug": t.get("slug"), "name": t.get("name"), "folder": f, "video_path": vp,
                        "dir": str(Path(vp).parent) if vp else "", "duration": t.get("duration")})
    return out


def log(msg):
    STATE["log"].append(msg)


def _download(url, start, end):
    """Downloads a slice of a YouTube video. Returns the saved file path, or None after logging why not."""
    log(f"[runner] downloading {start} to {end} ...")
    env = {**os.environ, "NO_OPEN": "1"}
    p = subprocess.run(["bash", GRAB, url, start, end, SAMPLES], capture_output=True, text=True, env=env, timeout=1800)
    saved = [l[6:].strip() for l in p.stdout.splitlines() if l.startswith("Saved: ")]
    if p.returncode != 0 or not saved:
        log("[runner] download failed. " + (p.stderr.strip().splitlines() or ["yt-dlp gave no details"])[-1])
        return None
    video = saved[-1]
    log(f"[runner] saved {video}")
    return video


def _prepare(url, start, end, name, folder=None):
    """Downloads a slice of a YouTube video and saves it as a NEW template in the clipper (transcribes it once).
    Waits while the clipper is busy. Returns (video_path, slug) or None after logging why it could not."""
    video = _download(url, start, end)
    return _create_template(video, name, folder) if video else None


def _create_template(video, name, folder=None):
    """Saves an already downloaded slice as a NEW template in the clipper and waits for it to be transcribed."""
    waited, announced = 0, False
    while True:
        st = clipper_status()
        if st["state"] == "off":
            log(f"[runner] the clipper app is off ({APP_URL}). The clip source is saved here, start the app and click again:\n   {video}")
            return None
        if st["state"] == "on":
            code, resp = app_call("/api/clip-templates/create", {**outreach_settings(), "name": name, "path": video, "folder": folder or OUT_FOLDER}, timeout=30)
            if code == 200 and resp.get("success"):
                break
            if code != 409:
                log(f"[runner] clipper rejected the template: {resp.get('error') or code}")
                return None
        if waited >= 900:
            log("[runner] clipper stayed busy for 15 minutes, giving up. The file is saved:\n   " + video)
            return None
        if not announced or waited % 60 == 0:
            log(f"[runner] clipper is busy ({st.get('message') or 'working'}), waiting for it to finish ...")
            announced = True
        time.sleep(10)
        waited += 10
    log("[runner] template is being created (transcribing the slice) ...")
    last = ""
    for _ in range(1800):
        time.sleep(1)
        st = clipper_status()
        if st["state"] == "off":
            log("[runner] lost contact with the clipper app.")
            return None
        if st.get("message") and st["message"] != last:
            last = st["message"]
            log(f"   clipper: {last}")
        if st["state"] == "on":
            break
    st = clipper_status()
    if st.get("error"):
        log(f"[runner] the clipper reported an error: {st['error']}")
        return None
    return video, (st.get("template_slug") or "")


def run_send(url, start, end, name, folder=None):
    """Download a slice of a YouTube video, then save it as a NEW template in the clipper (no clips are generated:
    you change the rules there and press Generate yourself)."""
    try:
        got = _prepare(url, start, end, name, folder)
        if got:
            video, slug = got
            log("RESULT " + json.dumps({"slug": slug, "name": name, "video": video, "dir": str(Path(video).parent), "folder": folder or OUT_FOLDER}))
            log(f"[runner] DONE. Template \"{name}\" is in the clipper (folder: {folder or OUT_FOLDER}). Open it, adjust the rules, then press Generate.")
    except Exception as e:
        log(f"[runner] failed: {e}")
    finally:
        STATE["running"] = False


# ----------------------------------------------------------------------------------- automatic sample-clip pipeline
PIPE = {"stop": False}
REPO = Path(os.getenv("CLIPPER_REPO", str(Path.home() / "instagram-video-generator")))


def video_duration(url):
    """Length of a YouTube video in seconds (None if it can't be read)."""
    try:
        out = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-playlist", "-q", "--no-warnings", "--extractor-args",
                              "youtube:player_client=android", "--print", "duration", url],
                             capture_output=True, text=True, timeout=90).stdout.strip().splitlines()
        return float(out[-1]) if out else None
    except Exception:
        return None


def pick_range(duration, minutes=5):
    """A slice that skips the intro and ad reads: about 20 percent in (never before 2:00), kept inside the episode."""
    length = int(minutes * 60)
    if not duration or duration < 120:
        return 600, 600 + length
    if duration < length + 90:
        return 0, int(duration) - 2
    start = int(max(120, min(duration * 0.2, duration - length - 30)))
    return start, int(min(duration - 5, start + length))


def _mmss(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def run_pipeline(leads, minutes=5, clips=1, after=None, folder=None, share=False):
    """For each lead in turn: pick a slice, download it, make the template, generate the clip, put it on a shareable
    link. One 'PIPE {json}' line per finished lead; the website saves it on the lead. Contact is still up to you."""
    def fetch(ld):
        """Picks the slice and downloads it (needs no clipper), so it can happen while the clipper is busy with another lead."""
        try:
            if ld.get("start") and ld.get("end"):
                s0, e0 = ld["start"], ld["end"]
            else:
                s_sec, e_sec = pick_range(video_duration(ld["url"]), minutes)
                s0, e0 = _mmss(s_sec), _mmss(e_sec)
            return s0, e0, _download(ld["url"], s0, e0)
        except Exception as e:
            return None, None, ("ERR", str(e)[:150])

    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)
    pending = {}
    try:
        PIPE["stop"] = False
        for i, ld in enumerate(leads, 1):
            if PIPE["stop"]:
                log("[runner] stopped.")
                break
            name = f"Sample {ld.get('name') or ld['id']}"[:60]
            log(f"[{i}/{len(leads)}] {ld.get('name')}")
            try:
                s0, e0, video0 = (pending.pop(i).result() if i in pending else fetch(ld))
                if isinstance(video0, tuple):
                    raise RuntimeError(video0[1])
                if i < len(leads):
                    pending[i + 1] = pool.submit(fetch, leads[i])  # download the next slice while this one is processed
                got = _create_template(video0, name, folder) if video0 else None
                if not got:
                    log(f"PIPEFAIL {json.dumps({'id': ld['id'], 'why': 'could not prepare the slice'})}")
                    continue
                video, slug = got
                body = {"slug": slug, "count": max(1, int(clips))}
                if after is not None:
                    body["after"] = after
                log("   generating the clip ...")
                code, resp = app_call("/api/clip-templates/generate", body, timeout=60)
                if code != 200 or not resp.get("success"):
                    log(f"PIPEFAIL {json.dumps({'id': ld['id'], 'why': resp.get('error') or f'clipper said {code}'})}")
                    continue
                last = ""
                for _ in range(2700):
                    time.sleep(1)
                    c2, raw = app_call("/api/clipper/status", timeout=10)
                    if c2 != 200:
                        break
                    if raw.get("message") and raw["message"] != last:
                        last = raw["message"]
                        log(f"   clipper: {last[:100]}")
                    if not raw.get("running"):
                        break
                if raw.get("error") or not raw.get("outputs"):
                    log(f"PIPEFAIL {json.dumps({'id': ld['id'], 'why': raw.get('error') or 'no clip came out (the slice may have no strong moment)'})}")
                    continue
                outs = list(raw["outputs"])[:max(1, int(clips))]
                infos = list(raw.get("clips") or [])
                files, links, titles = [], [], []
                for k, out in enumerate(outs):
                    files.append(str(REPO / "opencut-exports" / out))
                    titles.append((infos[k] if k < len(infos) else {}).get("title", ""))
                    if not share:
                        continue  # the clips stay private drafts in the template's folder; links are made on request (/clips/share)
                    c3, shared = app_call("/api/outreach/share-clip", {"id": f"opencut-exports/{out}", "name": f"{ld.get('name')} - clip {k + 1}"}, timeout=300)
                    if c3 == 200 and shared.get("success"):
                        links.append(shared.get("link", ""))
                    else:
                        log(f"   (could not make a share link for clip {k + 1}: {shared.get('error') or c3})")
                first = infos[0] if infos else {}
                clip_file = files[0] if files else str(REPO / "opencut-exports" / outs[0])
                link = links[0] if links else ""
                ctext = clip_text_from_template(slug, first.get("real_start", first.get("start")), first.get("real_end", first.get("end"))) if first.get("start") is not None else ""
                personal = personal_note(ld, Path(clip_file).name, first.get("title", ""), first.get("hook", ""), ctext)
                log("PIPE " + json.dumps({"id": ld["id"], "slug": slug, "video": video, "dir": str(Path(video).parent), "clip": clip_file,
                                          "title": first.get("title", ""), "hook": first.get("hook", ""), "link": link, "personal": personal,
                                          "links": links, "files": files, "titles": titles, "yt_start": _yt_start(s0, first.get("start")),
                                          "range": f"{s0}-{e0}", "name": name}))
            except Exception as e:
                log(f"PIPEFAIL {json.dumps({'id': ld.get('id'), 'why': str(e)[:200]})}")
        log("[runner] pipeline finished.")
    finally:
        pool.shutdown(wait=False)
        STATE["running"] = False


def clip_text_from_template(slug, start, end):
    """The words spoken between start and end, read from the template's saved transcript (the same transcription the clip was cut from).
    Returns '' when it cannot be read, and the caller falls back to listening to the clip again."""
    try:
        f = REPO / "generated_content" / "clip_templates" / f"{slug}.transcript.json"
        segs = json.loads(f.read_text(encoding="utf-8"))
        s0, e0 = float(start), float(end)
        words = [w["w"] for seg in segs for w in (seg.get("words") or []) if s0 - 0.05 <= float(w["s"]) < e0]
        if not words:
            words = [seg["text"] for seg in segs if float(seg["e"]) > s0 and float(seg["s"]) < e0]
        return " ".join(words).strip()
    except Exception:
        return ""


def _yt_start(slice_start, clip_start):
    """Seconds into the YouTube episode where the first clip begins (slice start like '7:01' plus the clip's offset inside the slice)."""
    try:
        base = sum(int(x) * 60 ** i for i, x in enumerate(reversed(str(slice_start).split(":"))))
        return int(base + float(clip_start or 0))
    except Exception:
        return None


def personal_note(ld, clip_name, title, hook, text=""):
    """Reads the words spoken in the clip (the clipper's own transcript, the same one the clip editor shows), then asks DeepSeek for
    one specific note about it. Returns '' if anything is missing; the email still works without it."""
    if len(text.split()) < 25:
        code, w = app_call("/api/clip-edit/words", {"id": f"opencut-exports/{clip_name}"}, timeout=900)
        if code != 200 or not w.get("success"):
            log(f"   (no personal note: could not read the clip's words: {(w or {}).get('error') or code})")
            return ""
        text = w.get("text", "")
    line = personalize.write_line(ld.get("name") or "", ld.get("epTitle") or "", title, hook, text)
    if not line:
        log("   (no personal note: not enough to say, or no DeepSeek key found)")
    return line


def run_personalize(leads):
    """For leads that already have a clip: writes the personal note. One 'PERSONAL {json}' line each."""
    try:
        for ld in leads:
            if PIPE["stop"]:
                log("[runner] stopped.")
                break
            name = Path(str(ld.get("clipFile") or "")).name
            if not name:
                continue
            log(f"[runner] writing a personal note for {ld.get('name')}")
            line = personal_note(ld, name, ld.get("clipTitle") or "", ld.get("clipHook") or "")
            log("PERSONAL " + json.dumps({"id": ld["id"], "line": line}))
        log("[runner] personal notes finished.")
    finally:
        STATE["running"] = False


def run_find_coaches(job):
    """Asks Google AI for independent coaches (names and websites only), then keeps the ones whose own website checks out.
    One 'COACH {json}' line per kept coach, with the email and phone found on that website."""
    import tempfile
    try:
        PIPE["stop"] = False
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(job, f)
        f.close()
        proc = subprocess.Popen([sys.executable, "-u", str(HERE / "coach_finder.py"), "discover", f.name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                cwd=str(HERE), env={**os.environ, "PYTHONUNBUFFERED": "1"})
        STATE["proc"] = proc
        for line in proc.stdout:
            line = line.rstrip()
            if "asking you to verify" in line:
                log(line)
                phone_notify("Google needs you", "Google is asking for a verification and the coach search is waiting (up to 5 minutes). " + REMOTE_HINT, key="captcha")
            else:
                log(line)
        proc.wait()
        log("[runner] coach search finished.")
    finally:
        STATE["running"] = False


def run_enrich_coaches(prospects):
    """Public email and phone for prospects that have a website, read from the site's own pages (no browser). 'CONTACT {json}' per prospect."""
    from concurrent.futures import ThreadPoolExecutor
    try:
        PIPE["stop"] = False

        def one(p):
            if PIPE["stop"]:
                return p, None
            try:
                return p, coach_finder.contacts(coach_finder.origin(p.get("website")))
            except Exception as e:
                return p, {"error": str(e)[:80]}
        n = 0
        with ThreadPoolExecutor(max_workers=3) as pool:
            for i, (p, got) in enumerate(pool.map(one, prospects), 1):
                if got is None:
                    continue
                if got.get("email") or got.get("phone"):
                    n += 1
                    log(f"[{i}/{len(prospects)}] {p.get('name')}: {got.get('email') or 'no email'} / {got.get('phone') or 'no phone'}")
                    log("CONTACT " + json.dumps({"id": p["id"], **{k: got.get(k) for k in ("email", "email_quality", "phone", "phone_mobile", "phone_how", "others")}}))
                else:
                    log(f"[{i}/{len(prospects)}] {p.get('name')}: nothing on their own pages")
                    log("CONTACTNONE " + json.dumps({"id": p["id"]}))
        log(f"[runner] contact search finished: {n} of {len(prospects)} had something.")
    finally:
        STATE["running"] = False


def run_find_emails(leads, use_ai=True):
    """Looks for public contact emails, three leads at a time (each browser search is mostly waiting on pages).
    Leads whose own pages show nothing then go to Google AI, one at a time, as a last resort."""
    from concurrent.futures import ThreadPoolExecutor
    found_ids = set()
    try:
        def one(item):
            i, ld = item
            if PIPE["stop"]:
                return i, ld, None
            try:
                return i, ld, email_finder.find_email(ld.get("name") or "", ld.get("url") or "", ld.get("web") or "", ld.get("episode") or "", log=lambda m: None)
            except Exception as e:
                return i, ld, {"email": "", "error": str(e)[:80]}
        with ThreadPoolExecutor(max_workers=3) as pool:
            for i, ld, got in pool.map(one, list(enumerate(leads, 1))):
                if got is None:
                    continue
                if got.get("email"):
                    found_ids.add(ld["id"])
                    log(f"[{i}/{len(leads)}] {ld.get('name')}: {got['email']} ({got['source']})")
                    log("EMAILFOUND " + json.dumps({"id": ld["id"], "email": got["email"], "source": got["source"], "quality": got.get("quality", "good")}))
                else:
                    log(f"[{i}/{len(leads)}] {ld.get('name')}: no email on their own pages")
        missing = [l for l in leads if l["id"] not in found_ids]
        if missing and use_ai and os.getenv("EMAIL_AI", "1") != "0" and not PIPE["stop"]:
            import tempfile
            f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump({"leads": [{k: str(l.get(k) or "")[:300] for k in ("id", "name", "url", "web", "owner", "business")} for l in missing[:6]]}, f)
            f.close()
            log(f"[runner] asking Google AI about {min(6, len(missing))} lead(s) with no public email yet")
            proc = subprocess.Popen([sys.executable, "-u", str(HERE / "email_ai.py"), f.name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(HERE),
                                    env={**os.environ, "PYTHONUNBUFFERED": "1"})
            STATE["proc"] = proc
            for line in proc.stdout:
                line = line.rstrip()
                if "asking you to verify" in line:
                    log(line)
                    phone_notify("Google needs you", "Google is asking for a verification and the email search is waiting (up to 5 minutes). " + REMOTE_HINT, key="captcha")
                elif line.startswith("EMAILAI "):
                    o = json.loads(line[8:])
                    if o.get("email"):
                        found_ids.add(o["id"])
                        log("EMAILFOUND " + json.dumps({"id": o["id"], "email": o["email"], "source": "Google AI: " + o.get("where", ""), "quality": o.get("quality", "good")}))
                else:
                    log(line)
            proc.wait()
        for l in leads:
            if l["id"] not in found_ids:
                log("EMAILNONE " + json.dumps({"id": l["id"]}))
        log("[runner] email search finished.")
    finally:
        STATE["running"] = False


def run_ads(items, template=None):
    """Google AI: decision maker, work email, phone and public LinkedIn link per advertiser. 'DMAKER {json}' lines stream back."""
    try:
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"template": template, "leads": items}, f)
        f.close()
        proc = subprocess.Popen([sys.executable, "-u", str(HERE / "decision_maker_ai.py"), f.name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(HERE),
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        STATE["proc"] = proc
        for line in proc.stdout:
            line = line.rstrip()
            if "asking you to verify" in line:
                phone_notify("Google needs you", "Google is asking for a verification and the lookup is waiting (up to 5 minutes). " + REMOTE_HINT, key="captcha")
            log(line)
        proc.wait()
        log("[runner] advertiser lookup finished.")
    finally:
        STATE["running"] = False


def run_ig(items, template=None):
    """Instagram accounts: the brand's own site first (free, exact), then Google AI for the personal account. 'IGSITE {json}' and 'IGAI {json}' lines stream back."""
    try:
        import ad_leads
        for it in items:
            if PIPE["stop"]:
                break
            h = ""
            try:
                h = ad_leads.instagram_from_site(it.get("web") or "") if it.get("web") else ""
            except Exception:
                pass
            log("IGSITE " + json.dumps({"id": it["id"], "brand_handle": h}))
            log(f"[{it['company']}] website Instagram link: {('@' + h) if h else 'none'}")
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"template": template, "leads": items}, f)
        f.close()
        proc = subprocess.Popen([sys.executable, "-u", str(HERE / "ig_finder_ai.py"), f.name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(HERE),
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        STATE["proc"] = proc
        for line in proc.stdout:
            line = line.rstrip()
            if "asking you to verify" in line:
                phone_notify("Google needs you", "Google is asking for a verification and the Instagram lookup is waiting (up to 5 minutes). " + REMOTE_HINT, key="captcha")
            log(line)
        proc.wait()
        log("[runner] Instagram lookup finished.")
    finally:
        STATE["running"] = False


def run_ad_video(items, voice, product, link=None, host="loom", narrator=None):
    """Makes the face-free ad video for each lead, shares it as a view link, and streams 'ADVIDEO {json}' lines. One lead at a time, spaced out."""
    try:
        import ad_video, shutil as _sh
        cfg = cold_email.load_config()
        sender = str(cfg.get("sender_name") or "Emmanuel")
        for n, it in enumerate(items):
            if PIPE["stop"]:
                break
            log(f"[{n + 1}/{len(items)}] {it.get('company')}")
            try:
                res = ad_video.make(it, voice=voice or ad_video.DEFAULT_VOICE, sender=sender, product=product or "AI Ads Optimization", log=log, link=ad_video.SIGNUP_LINK, host=host, narrator=narrator)
            except Exception as e:
                res = {"ok": False, "reason": "error: " + str(e)[:160]}
            share = ""
            qa = ad_video.qa(res) if res.get("ok") else {"ok": False, "match": 0, "reason": res.get("reason", "")}
            if res.get("ok"):
                log(f"   automatic check: {'passed' if qa['ok'] else 'needs a human look: ' + qa['reason']} (words {qa['match']:.0%})")
            if res.get("ok") and host == "drive":  # only the Drive hosting mode needs a share link from the clipper app; Loom videos are uploaded from the file
                slug = "adv_" + re.sub(r"[^A-Za-z0-9_-]", "", str(it.get("id")))[:40]
                dest = REPO / "opencut-exports" / f"{slug}.mp4"
                try:
                    dest.parent.mkdir(exist_ok=True)
                    _sh.copyfile(res["file"], dest)
                    code, shared = app_call("/api/outreach/share-clip", {"id": f"opencut-exports/{slug}.mp4", "name": f"{it.get('company')} - Meta ads video"}, timeout=300)
                    share = (shared or {}).get("link", "") if code == 200 else ""
                    if not share:
                        log("   (could not make a share link: " + ((shared or {}).get("error") or ("the clipper app is not running" if code == 0 else str(code))) + ")")
                except Exception as e:
                    log("   (could not share the video: " + str(e)[:100] + ")")
            log("ADVIDEO " + json.dumps({"id": it.get("id"), "ok": bool(res.get("ok")), "reason": res.get("reason", ""), "transient": bool(res.get("transient")), "link": share, "file": res.get("file", ""),
                                         "facts": res.get("facts", {}), "script": res.get("script", ""), "seconds": res.get("seconds", 0), "display_name": res.get("display_name", ""), "voice": res.get("voice", ""), "ad_clicks": res.get("ad_clicks", 0), "qa": qa}))
            if n < len(items) - 1:
                time.sleep(random.uniform(4, 8))
        log("[runner] video maker finished.")
    finally:
        STATE["running"] = False


CFORM_LOG = HERE / "contact_form_log.jsonl"


def cform_today():
    """How many contact-form messages this helper sent (or may have sent) today."""
    n, day = 0, time.strftime("%Y-%m-%d")
    try:
        for line in CFORM_LOG.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e.get("day") == day and e.get("status") in ("sent", "unsure"):
                n += 1
    except Exception:
        pass
    return n


def cform_record(id_, url, status):
    with open(CFORM_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "day": time.strftime("%Y-%m-%d"), "id": id_, "url": url, "status": status}) + "\n")


def cform_values(body):
    """Who the message comes from: the sender's name, the Reply-To inbox (answers must reach the inbox this tool reads), the phone; subject, company and website from the page."""
    cfg = cold_email.load_config()
    v = body.get("values") or {}
    mail = cfg.get("reply_to") or ((cfg.get("accounts") or [{}])[0].get("email") or "")
    return {"name": str(cfg.get("sender_name") or "Emmanuel"), "email": mail, "phone": str(cfg.get("phone") or ""), "subject": str(v.get("subject") or "")[:150],
            "company": str(v.get("company") or "")[:100], "website": str(v.get("website") or "")[:200]}


def run_cform_find(items):
    """Looks for a contact form on each lead's website and streams 'CFORM {json}' (id plus what was found)."""
    try:
        import contact_form
        for n, it in enumerate(items):
            if PIPE["stop"]:
                break
            log(f"[{n + 1}/{len(items)}] looking for a contact form: {it.get('domain') or it.get('web')}")
            try:
                res = contact_form.find(it.get("domain") or it.get("web") or "", web=it.get("web") or "", log=log)
            except Exception as e:
                res = {"found": False, "why": "error: " + str(e)[:120], "error": True}
            log("CFORM " + json.dumps({"id": it.get("id"), **res}))
        log("[runner] contact form look finished.")
    finally:
        STATE["running"] = False


def run_cform_send(items, body):
    """Sends each message through its lead's contact form (only forms without a CAPTCHA), spaced out, up to the daily limit. Streams 'CFORMSEND {json}'."""
    try:
        import contact_form
        values = cform_values(body)
        limit = int(body.get("limit") or cold_email.load_config().get("cform_daily_limit") or 25)
        for n, it in enumerate(items):
            if PIPE["stop"]:
                break
            if cform_today() >= limit:
                log("CFORMSEND " + json.dumps({"id": it.get("id"), "status": "later", "note": f"today's contact form limit ({limit}) is used up"}))
                continue
            log(f"[{n + 1}/{len(items)}] sending through the contact form: {it.get('url')}")
            try:
                res = contact_form.submit(it["url"], {**values, "message": it.get("message", ""), **({"subject": it["subject"]} if it.get("subject") else {})}, confirm=True, log=log)
            except Exception as e:
                res = {"status": "failed", "note": "error: " + str(e).splitlines()[0][:120]}
            cform_record(it.get("id"), it.get("url"), res["status"])
            log("CFORMSEND " + json.dumps({"id": it.get("id"), **res}))
            if n < len(items) - 1:
                time.sleep(random.uniform(25, 70))
        log("[runner] contact form sending finished.")
    finally:
        STATE["running"] = False


def run_cform_batch(items, body):
    """CAPTCHA forms: a visible Chrome window, every form filled in; you solve each CAPTCHA, it presses send and moves on. Tabs left alone for 10 minutes are saved for later."""
    try:
        import contact_form
        values = cform_values(body)

        def result(r):
            if r.get("status") in ("sent", "unsure"):
                cform_record(r.get("id"), "", r["status"])
            log("CFORMSEND " + json.dumps(r))
        contact_form.captcha_batch(items, values, wait_minutes=10, on_result=result, stop=lambda: PIPE["stop"])
        log("[runner] CAPTCHA forms finished.")
    finally:
        STATE["running"] = False


def cform_replies(items):
    """Answers to contact form messages arrive in the Reply-To inbox from someone at the lead's company: look there for mail from the lead's domain since the message was sent.
    items: [{id, domain, since_ts, email}]. Returns [{id, kind: 'replied'|'optout', snippet}]."""
    import imaplib
    from datetime import datetime
    cfg = cold_email.load_config()
    inboxes = [a for a in cfg.get("accounts", [])]
    ra = cfg.get("reply_account")
    if ra and ra.get("password"):
        inboxes.append({**ra, "smtp_host": ra.get("imap_host", "")})
    found = {}
    for acct in inboxes:
        try:
            imap = imaplib.IMAP4_SSL(acct.get("imap_host") or acct["smtp_host"].replace("smtp.", "imap."))
            imap.login(acct.get("username") or acct["email"], acct["password"])
            imap.select("INBOX", readonly=True)
            for it in items:
                if it["id"] in found or not it.get("domain"):
                    continue
                since = datetime.fromtimestamp(it.get("since_ts") or time.time() - 14 * 86400).strftime("%d-%b-%Y")
                typ, data = imap.search(None, f'(FROM "@{it["domain"]}" SINCE {since})')
                ids = data[0].split() if typ == "OK" and data and data[0] else []
                if not ids:
                    continue
                pick = None   # a person's answer beats an automatic one ("we received your message", out of office)
                for mid in reversed(ids[-6:]):
                    typ, raw = imap.fetch(mid, "(BODY.PEEK[]<0.30000>)")
                    rb = raw[0][1] if raw and raw[0] else b""
                    cls, sub = cold_email._reply_class(rb)
                    if pick is None or (pick[1] == "auto" and cls == "human"):
                        pick = (rb, cls, sub)
                    if cls == "human":
                        break
                rb, cls, sub = pick
                text = cold_email._reply_text(rb)[0]
                if cls == "auto":
                    found[it["id"]] = {"id": it["id"], "kind": "auto", "subtype": sub, "snippet": re.sub(r"\s+", " ", text)[:160]}
                    continue
                kind = "optout" if cold_email.OPT_OUT.search(text) else "replied"
                if kind == "optout" and it.get("email"):
                    cold_email.suppress(it["email"], "asked to stop (contact form reply)")
                found[it["id"]] = {"id": it["id"], "kind": kind, "snippet": re.sub(r"\s+", " ", text)[:160]}
            imap.logout()
        except Exception as e:
            log(f"could not check {acct.get('email')}: {str(e)[:100]}")
    return list(found.values())


def run_loom_login():
    """Opens a visible Chrome window (with the helper's own saved profile) for the one-time Loom login."""
    try:
        import loom_upload
        loom_upload.login(timeout=600, log=log)
        log("[runner] Loom login finished.")
    finally:
        STATE["running"] = False


def run_loom_upload(items, show, keep_local=False):
    """Uploads each approved video to Loom, posts the link comment near the end, and streams 'LOOM {json}' lines with the share link."""
    try:
        import loom_upload
        loom_upload.CANCEL.clear()
        PIPE["stop"] = False
        root = (HERE / "ad_videos").resolve()
        for n, it in enumerate(items):
            if PIPE["stop"]:
                break
            log(f"[{n + 1}/{len(items)}] {it.get('title')}")
            f = Path(str(it.get("file") or ""))
            f = (f if f.is_absolute() else HERE.parent / f).resolve()
            if root not in f.parents or f.suffix.lower() != ".mp4":
                res = {"ok": False, "error": "only the videos made by the video maker can be uploaded"}
            else:
                res = loom_upload.upload_and_comment(f, it.get("title") or "A quick look at your Meta ads", it.get("comment") or "", show=show, log=log)
            if res.get("ok") and res.get("link") and not (keep_local or os.getenv("KEEP_LOCAL_VIDEOS") == "1"):
                try:  # the video lives in Loom now: do not let finished videos pile up on this disk
                    f.unlink()
                    res["deleted"] = True
                    log("   (removed the local copy, it is in Loom now)")
                except Exception:
                    pass
            log("LOOM " + json.dumps({"id": it.get("id"), **res}))
            if res.get("needs_login"):
                phone_notify("Loom needs you", "Loom asked for a login again, so videos are waiting. Open the CRM, Meta Ads tab, and press Connect Loom.", key="loom-login", every=6 * 3600)
                break
            if n < len(items) - 1:
                time.sleep(random.uniform(5, 9))
        log("[runner] Loom upload finished.")
    finally:
        STATE["running"] = False


def run_maps(items):
    """Google AI: owner + YouTube channel per business. 'MAPS {json}' lines stream back."""
    try:
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(items, f)
        f.close()
        proc = subprocess.Popen([sys.executable, "-u", str(HERE / "maps_youtube.py"), f.name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(HERE),
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        STATE["proc"] = proc
        for line in proc.stdout:
            line = line.rstrip()
            if "asking you to verify" in line:
                phone_notify("Google needs you", "Google is asking for a verification and the lookup is waiting (up to 5 minutes). " + REMOTE_HINT, key="captcha")
            log(line)
        proc.wait()
        log("[runner] business lookup finished.")
    finally:
        STATE["running"] = False


def run_find_phones(leads, use_ai=True, template=None):
    """Phone numbers, in three steps: their website (fast), a real Chrome visit, then Google AI for the leads that still have none.
    One 'PHONE {json}' line per lead that got a number, 'PHONENONE {json}' for the rest."""
    from concurrent.futures import ThreadPoolExecutor
    try:
        PIPE["stop"] = False

        def one(item):
            i, ld = item
            if PIPE["stop"]:
                return i, ld, None
            try:
                return i, ld, phone_finder.find_phone(ld.get("name") or "", ld.get("url") or "", ld.get("web") or "", ld.get("episode") or "", log=lambda m: None)
            except Exception as e:
                return i, ld, {"phone": "", "error": str(e)[:80]}
        missing = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            for i, ld, got in pool.map(one, list(enumerate(leads, 1))):
                if got is None:
                    continue
                if got.get("phone"):
                    log(f"[{i}/{len(leads)}] {ld.get('name')}: {got['phone']} ({got['how']}, {got['source']})")
                    log("PHONE " + json.dumps({"id": ld["id"], "phone": got["phone"], "others": got.get("others", []), "source": got["source"], "how": got["how"], "kind": got["kind"]}))
                else:
                    missing.append(ld)
                    log(f"[{i}/{len(leads)}] {ld.get('name')}: no number on their own pages")
        if missing and use_ai and os.getenv("PHONE_AI", "1") != "0" and not PIPE["stop"]:
            import tempfile
            f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump({"template": template, "leads": [{k: str(l.get(k) or "")[:300] for k in ("id", "name", "url", "web", "owner", "business")} for l in missing]}, f)
            f.close()
            log(f"[runner] asking Google AI about {len(missing)} lead(s) with no number yet")
            proc = subprocess.Popen([sys.executable, "-u", str(HERE / "phone_ai.py"), f.name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(HERE),
                                    env={**os.environ, "PYTHONUNBUFFERED": "1"})
            STATE["proc"] = proc
            answered = set()
            for line in proc.stdout:
                line = line.rstrip()
                if "asking you to verify" in line:
                    log(line)
                    phone_notify("Google needs you", "Google is asking for a verification and the phone number search is waiting (up to 5 minutes). " + REMOTE_HINT, key="captcha")
                elif line.startswith("PHONEAI "):
                    o = json.loads(line[8:])
                    if o.get("phone"):
                        answered.add(o["id"])
                        log("PHONE " + json.dumps({"id": o["id"], "phone": o["phone"], "others": [], "source": "Google AI: " + o.get("where", ""), "how": "Google AI, confirm when you call", "kind": o.get("kind", "")}))
                else:
                    log(line)
            proc.wait()
            missing = [l for l in missing if l["id"] not in answered]
        for l in missing:
            log("PHONENONE " + json.dumps({"id": l["id"]}))
        log("[runner] phone search finished.")
    finally:
        STATE["running"] = False


def run_email(leads, template, mode, limit, ignore_window, step, campaign="clips"):
    try:
        PIPE["stop"] = False
        cold_email_sleep = lambda secs: [time.sleep(1) for _ in range(int(secs)) if not PIPE["stop"]]
        results = cold_email.run(leads, template, mode, limit=limit, ignore_window=ignore_window, step=step, log_fn=log, sleep=cold_email_sleep, campaign=campaign, should_stop=lambda: PIPE["stop"])
        for r in results:
            log("EMAIL " + json.dumps({k: r.get(k) for k in ("id", "to", "from", "status", "why", "subject", "step", "message_id", "template_id", "template_name")}))
        log(f"[runner] done: {sum(1 for r in results if r['status'] in ('sent', 'drafted'))} {mode} / {sum(1 for r in results if r['status'] == 'skip')} skipped / {sum(1 for r in results if r['status'] == 'failed')} failed")
    except Exception as e:
        log(f"[runner] email run stopped: {e}")
    finally:
        STATE["running"] = False


def email_status():
    cfg = cold_email.load_config()
    log_ = cold_email.read_log()
    problems = []
    if not cfg.get("accounts"):
        problems.append("add at least one sending inbox in tools/email_accounts.json (copy email_accounts.example.json)")
    if not (cfg.get("postal_address") or "").strip() or "mailing address" in cfg.get("postal_address", ""):
        problems.append("add your real postal address (cold email law requires it in every email)")
    if not (cfg.get("sender_name") or "").strip() or cfg.get("sender_name") == "Your Name":
        problems.append("add your name as sender_name")
    warnings = []
    rt = (cfg.get("reply_to") or "").lower()
    if rt and rt.split("@")[-1] in ("gmail.com","googlemail.com","yahoo.com","outlook.com","hotmail.com","live.com","icloud.com","aol.com","proton.me","protonmail.com"):
        warnings.append(f"Replies go to a free address ({rt}). Spam filters penalize a Gmail/Yahoo Reply-To on a different From domain (mail-tester: -2.5). Before real outreach, set Reply-To to a mailbox on a domain you own (python3 tools/setup_email.py, option 4).")
    risky = sorted({a["email"].split("@")[1].rsplit(".", 1)[-1] for a in cfg.get("accounts", [])} & RISKY_TLDS)
    if risky:
        warnings.append("Some sending domains end in " + ", ".join("." + t for t in risky) + ". Spam filters distrust these endings (mail-tester scored them 6 out of 10, about 3.5 points lost to the ending alone). A .com ending avoids that. Authentication and the email text are fine.")
    if not (cfg.get("phone") or "").strip():
        warnings.append("Add your phone number (python3 tools/setup_email.py, option 3). Automatic replies to people who say yes need it, and nothing is written without it.")
    caps = cold_email.capacity(cfg, log_)
    return {"paused": pause_state(), "sender_name": cfg.get("sender_name") or "", "phone": cfg.get("phone") or "", "warnings": warnings, "auto_reply": "send" if cfg.get("auto_reply") == "send" else "draft", "ready": not problems, "problems": problems, "inboxes": caps, "capacity_left": sum(max(0, c["cap"] - c["sent_today"]) for c in caps),
            "suppressed": len(cold_email.suppressed()), "suppressed_list": sorted(cold_email.suppressed())[:2000], "in_send_window": cold_email.in_window(cfg),
            "window": cfg.get("send_window") or {"start_hour": 9, "end_hour": 17}}


PAUSE_FILE = HERE / "campaign_pause.json"
# routes that belong to the podcast clips campaign: finding podcasts and their emails, making clips, writing and sending the emails
CLIPS_ONLY_ROUTES = {"/run", "/emails/find", "/pipeline/start", "/email/run"}


def pause_state():
    try:
        return json.loads(PAUSE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def run_halcrus_login():
    """Opens a visible Chrome window (with the helper's own saved profile) for the one-time HALCRUS login."""
    try:
        import halcrus_leads
        halcrus_leads.login(timeout=600, log=log)
        log("[runner] HALCRUS login finished.")
    finally:
        STATE["running"] = False


def run_meta_login():
    """Opens a visible Chrome window (the helper's own Meta profile) so you can log in to Facebook once; the Ad Library lookups then run in that profile."""
    try:
        import meta_session
        meta_session.login(timeout=900, log=log)
        log("[runner] Meta login finished.")
    finally:
        STATE["running"] = False


def run_halcrus_leads(want, emails, companies, tier):
    """Reads new leads from the HALCRUS portal and streams 'HALCRUS {json}' per lead, then 'HALCRUSEND {json}'."""
    try:
        import halcrus_leads
        # whatever the CRM page knows, never bring back anyone this helper has already emailed or drafted to, anyone who replied or bounced, or the do-not-contact list
        emails = list({*emails, *(e.get("to", "").lower() for e in cold_email.read_log() if e.get("to")), *(a.lower() for a in cold_email.suppressed())})
        leads = halcrus_leads.fetch(want, emails, companies, tier=tier, log=log)
        for ld in leads:
            log("HALCRUS " + json.dumps(ld))
        log("HALCRUSEND " + json.dumps({"found": len(leads)}))
    except Exception as e:
        log("HALCRUSEND " + json.dumps({"found": 0, "error": str(e)[:200], "needs_login": e.__class__.__name__ == "NotLoggedIn"}))
        if e.__class__.__name__ == "NotLoggedIn":
            phone_notify("HALCRUS needs you", "HALCRUS asked for a login again, so no new leads can be found. Open the CRM, Meta Ads tab, and press Connect HALCRUS.", key="hc-login", every=6 * 3600)
    finally:
        STATE["running"] = False


QUEUE_TICK = 20
QUEUE_RECONCILE = 1800


QUEUE_AWAKE = {"mine": False}


def queue_keep_awake():
    """While emails are waiting to go out in the next 13 hours the Mac is kept from idle-sleeping (caffeinate, renewed before it ends), and released when the queue is empty.
    Closing the lid still sleeps a laptop unless it is on power with an external display."""
    now = time.time()
    soon = [i for i in send_queue.listing() if i["status"] in ("scheduled", "sending") and (i.get("due_at") or 0) - now < 13 * 3600]
    st = awake_status()
    if LOOP is not None and LOOP.busy_now and not (soon or QUEUE_AWAKE["mine"]):   # the outreach loop is in the middle of a job (videos take a long time): do not let the Mac idle-sleep under it
        if not st["on"] or st["seconds_left"] < 900:
            awake_set(True, 120)
        return
    if soon:
        if not st["on"] or st["seconds_left"] < 1800:
            awake_set(True, 720)
            QUEUE_AWAKE["mine"] = True
    elif QUEUE_AWAKE["mine"]:
        if st["on"]:
            awake_set(False)
        QUEUE_AWAKE["mine"] = False


def queue_alerts(r=None, bounced=0):
    """Phone alerts for what needs a person: a send that failed for good, a skip, bounces, and one short summary at the end of each sending day."""
    if r and r.get("status") == "failed":
        phone_notify("A send failed", f"{r.get('id')}: {str(r.get('why') or '')[:120]}. It is in the Scheduled sends table; Reschedule it when fixed.", key="send-fail-" + str(r.get("id")), every=86400)
    prefs = notify_prefs()      # the phone only buzzes when you must act; these are informational, so they are opt-in (tools/notify_prefs.json: skips, bounces, summary)
    if r and r.get("status") == "skipped" and prefs.get("skips"):
        phone_notify("A send was skipped", f"{r.get('id')}: {str(r.get('why') or '')[:120]}", key="send-skip-" + str(r.get("id")), every=86400)
    if bounced and prefs.get("bounces"):
        phone_notify("Bounces", f"{bounced} email(s) bounced. They will not be retried.", key="bounce-" + time.strftime("%Y%m%d%H"), every=3600)
    cfg = cold_email.load_config()
    now = datetime.now()
    if prefs.get("summary") and now.hour >= int((cfg.get("send_window") or {}).get("end_hour", 17)):
        items = send_queue.listing()
        today = now.date()
        sent = [i for i in items if i["status"] == "sent" and i.get("sent_at") and datetime.fromtimestamp(i["sent_at"]).date() == today]
        left = [i for i in items if i["status"] in ("scheduled", "sending")]
        bad = [i for i in items if i["status"] in ("failed", "bounced", "canceled") ]
        if sent or left:
            nxt = min((i["due_at"] for i in left if i.get("due_at")), default=0)
            phone_notify("Today's sending", f"Sent {sent and len(sent) or 0} today, {len(left)} still scheduled{(' (next ' + datetime.fromtimestamp(nxt).strftime('%a %-I:%M %p') + ')') if nxt else ''}, {len(bad)} need a look.",
                         key="summary-" + today.isoformat(), every=86400)


def queue_loop():
    """The scheduled-send queue (tools/send_queue.py): sends each approved email at its slot, whether or not the CRM page is open."""
    try:
        send_queue.recover()
    except Exception as e:
        print("send queue recover:", e)
    last = 0
    while True:
        time.sleep(QUEUE_TICK)
        try:
            n = 0
            if time.time() - last > QUEUE_RECONCILE:
                last = time.time()
                send_queue.prune()
                try:
                    for r in send_queue.scan_replies("meta"):  # a prospect answered or asked to stop: tell the phone, and follow-ups to them stop
                        phone_notify("Reply from " + r["email"] if r["kind"] == "replied" else "Asked to stop: " + r["email"], r.get("snippet") or "", key="reply-" + r["email"], every=7 * 86400)
                except Exception as e:
                    print("reply scan:", e)
                n = send_queue.reconcile()
            queue_keep_awake()
            if LOOP is not None and LOOP.is_paused():     # master pause: nothing new is sent (a send already in progress has finished, they are one at a time)
                continue
            job = STATE.get("job") or ""
            # the outreach loop keeps the helper busy most of the day (videos, contact forms); due emails must not wait for that. Only a job that writes drafts/sends mail itself blocks the queue.
            if (STATE["running"] and not (job and "draft" not in job.lower())) or not email_status()["ready"]:
                queue_alerts(None, n)
                continue
            r = send_queue.tick(paused=lambda c: bool(pause_state().get(c or "meta")))
            if r:
                print("send queue:", r)
            queue_alerts(r, n)
        except Exception as e:
            print("send queue error:", e)


def do_notify(title, message, key=None, every=600, topic="", mac=True, phone=True):
    """A Mac banner plus a phone message (Telegram). Returns whether the phone message went out."""
    title = clean(title or "Clipper CRM", 60)
    msg = clean(message or "", 160)
    if topic == "clips" and not clips_alerts():
        return False
    if mac and sys.platform == "darwin" and msg:
        try:
            subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s" sound name "Glass"' % (msg.replace('"', "'"), ("ClipperCRM: " + title).replace('"', "'"))], capture_output=True, timeout=10)
        except Exception:
            pass
    sent = False
    if phone and msg:
        extra = (" " + REMOTE_HINT) if "needs you" in title.lower() else ""
        sent = phone_notify(title, msg + extra, key=str(key or title), every=int(every or 600))
    return sent


def queue_list_payload():
    st = email_status()
    return {"items": send_queue.listing(), "now": time.time(), "in_send_window": st["in_send_window"], "window": st["window"], "capacity_left": st["capacity_left"], "ready": st["ready"]}


class Ports:
    """What the outreach loop (tools/outreach_loop.py) uses to touch the world. Job methods run in a helper job thread (one at a time); the rest answer at once."""

    # --- instant answers
    def email_status(self):
        return email_status()

    def greet(self, items):
        return greeting.resolve(items)

    def preview(self, leads, template, limit):
        return list(cold_email.plan(leads, template, "preview", int(limit), True, 0))

    def loom_connected(self):
        import loom_upload
        return loom_upload.connected()[0]

    def loom_uploads(self):
        import loom_upload
        return loom_upload._uploads()

    def meta_connected(self):
        import meta_session
        return meta_session.connected()[0]

    def halcrus_connected(self):
        import halcrus_leads
        return halcrus_leads.connected()[0]

    def queue_list(self):
        return {"items": send_queue.listing()}

    def queue_add(self, entries):
        st = email_status()
        if not st["ready"]:
            raise RuntimeError("; ".join(st["problems"]))
        tpl = (entries[0].get("template") if entries else {}) or {}
        if not (str(tpl.get("subject") or "").strip() and str(tpl.get("body") or "").strip()) and not (tpl.get("library") or []):
            raise RuntimeError("no email template, so nothing was scheduled")
        items = [{"id": str(b.get("id") or "")[:80], "to": str(b.get("to") or (b.get("lead") or {}).get("email") or "")[:200], "company": str(b.get("company") or "")[:80], "lead": b.get("lead") or {},
                  "template": b.get("template") or tpl, "campaign": str(b.get("campaign") or "meta"), "step": int(b.get("step") or 0)} for b in entries][:60]
        return send_queue.add(items)

    def queue_cancel(self, id_):
        return send_queue.cancel(str(id_))

    def cform_replies(self, items):
        return cform_replies(items)

    def notify(self, title, message, key, every):
        do_notify(title, message, key, every)

    def ads_health(self):
        box = {}

        def work():
            try:
                import ad_video
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    ctx, closer = ad_video.open_context(p)
                    try:
                        box["v"] = bool(ad_video.lookup_healthy(ctx.new_page(), lambda m: None))
                    finally:
                        closer()
            except Exception as e:
                box["err"] = str(e)[:100]
            finally:
                STATE["running"] = False
        if not self.run_job("checking whether the Meta Ad Library answers", work, (), None, 0, lambda: False):
            return None    # the helper is busy: unsure
        return box.get("v")

    def job_running(self):
        return bool(STATE["running"])

    def stop_soft(self):
        """Finish the unit in hand (one form, one video, one draft, one upload), start no further one of the batch."""
        PIPE["stop"] = True

    def release_memory(self):
        """Paused and idle: let go of the heavy things (the Dia voice server keeps a large model in memory). They start again on demand."""
        try:
            subprocess.run(["pkill", "-f", "tools/dia_server.py"], capture_output=True, timeout=10)
        except Exception:
            pass

    # --- the one-job-at-a-time runner
    def run_job(self, label, fn, args, on_line, wait, should_abort):
        """Starts fn(*args) as THE helper job (waiting up to `wait` seconds for the helper to be free), feeds its output lines to on_line, returns True when it finished, False if it never started."""
        t0 = time.time()
        while True:
            with LOCK:
                if not STATE["running"]:
                    STATE.update(running=True, job=label, log=[f"[runner] {label}"])
                    PIPE["stop"] = False
                    threading.Thread(target=fn, args=args, daemon=True).start()
                    break
            if should_abort() or time.time() - t0 >= wait:
                return False
            time.sleep(3)
        since = 0
        while True:
            time.sleep(0.5)
            done = not STATE["running"]
            lines = STATE["log"][since:]
            since += len(lines)
            for ln in lines:
                try:
                    on_line and on_line(ln)
                except Exception as e:
                    print("loop line handler:", e)
            if done:
                STATE.pop("job", None)
                return True

    # --- job bodies (the same functions the page's buttons use)
    def loom_upload(self, items, show, keep_local):
        run_loom_upload([{k: str(b.get(k) or "")[:300] for k in ("id", "file", "title", "comment")} for b in items][:5], bool(show), bool(keep_local))

    def ad_video(self, items, voice, product, host, narrator):
        run_ad_video([{k: str(b.get(k) or "")[:300] for k in ("id", "company", "email", "web", "first", "business")} for b in items][:5], str(voice or "")[:60], str(product or "")[:60], None,
                     "drive" if host == "drive" else "loom", (str(narrator)[:40] if narrator is not None else None))

    def halcrus_leads(self, want, tier, emails, companies):
        run_halcrus_leads(max(1, min(40, int(want))), [str(x)[:200] for x in emails][:20000], [str(x)[:120] for x in companies][:20000], tier if tier in ("Hot", "Warm", "Nurture", "All") else "Hot")

    def email_check(self, items, ai, jitbit):
        import tempfile
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump([{"id": str(b.get("id") or "")[:80], "email": str(b.get("email") or "")[:200], "company": str(b.get("company") or "")[:100], "web": str(b.get("web") or "")[:200]} for b in items][:40], fh)
        fh.close()
        run_email_check(fh.name, bool(ai), bool(jitbit))

    def email_drafts(self, leads, template, limit):
        st = email_status()
        if not st["ready"]:
            log("[runner] email is not set up: " + "; ".join(st["problems"]))
            STATE["running"] = False
            return
        run_email(leads, template, "drafts", min(60, int(limit)), False, 0, "meta")

    def cform_find(self, items):
        run_cform_find([{"id": str(b.get("id") or "")[:80], "domain": str(b.get("domain") or "")[:200], "web": str(b.get("web") or "")[:200]} for b in items][:20])

    def cform_send(self, items, values, limit):
        run_cform_send([{"id": str(b.get("id") or "")[:80], "url": str(b.get("url") or "")[:500], "message": str(b.get("message") or "")[:3000], "subject": str(b.get("subject") or "")[:150]} for b in items
                        if str(b.get("url") or "").startswith("http")][:25], {"values": values, "limit": limit})

    def cform_batch(self, items, values):
        run_cform_batch([{"id": str(b.get("id") or "")[:80], "url": str(b.get("url") or "")[:500], "message": str(b.get("message") or "")[:3000], "subject": str(b.get("subject") or "")[:150]} for b in items
                         if str(b.get("url") or "").startswith("http")][:250], {"values": values})


STORE = None
LOOP = None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        origin = self.headers.get("Origin", "")
        body = json.dumps(obj or {}).encode()
        self.send_response(code)
        if origin and origin_ok(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self):
        if not origin_ok(self.headers.get("Origin", "")):
            self._send(403, {"error": "origin not allowed"})
            return False
        return True

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        if u.path == "/status":
            self._send(200, {"ok": True, "phone": phone_ready(), "running": STATE["running"], "lines": len(STATE["log"]),
                             "configured": bool(os.getenv("CRM_SYNC_URL") and os.getenv("CRM_SYNC_PASSWORD")), "phone": phone_ready(),
                             "yt": bool(os.getenv("YT_API_KEY"))})
        elif u.path == "/cform/shot":   # a screenshot kept by the contact form sender (only files from its own folder)
            name = os.path.basename(parse_qs(u.query).get("f", [""])[0])
            f = HERE / "contact_form_debug" / name
            if name.endswith(".png") and f.is_file():
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
                return
            return self._send(404, {"error": "no such screenshot"})
        elif u.path == "/loop/status":
            if LOOP is None:
                return self._send(503, {"error": "the outreach loop is off"})
            self._send(200, LOOP.status())
        elif u.path == "/awake":
            self._send(200, awake_status())
        elif u.path == "/clipper/templates":
            t = sample_templates()
            self._send(200 if t is not None else 503, {"templates": t or [], "folder": OUT_FOLDER, **({} if t is not None else {"error": "clipper is off"})})
        elif u.path == "/email/status":
            self._send(200, email_status())
        elif u.path == "/clipper/status":
            self._send(200, clipper_status())
        elif u.path == "/log":
            since = int(parse_qs(u.query).get("since", ["0"])[0] or 0)
            self._send(200, {"lines": STATE["log"][since:], "next": len(STATE["log"]), "running": STATE["running"], "label": (STATE["log"][0] if STATE["log"] else "").replace("[runner] ", "")[:120] if STATE["running"] else ""})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        if self.path == "/crm/sync":     # the page hands over its changes and gets everything that changed here (see tools/crm_store.py)
            if STORE is None:
                return self._send(503, {"error": "the helper's CRM store is off"})
            res = None
            for _ in range(4):
                try:
                    res = STORE.sync(body)
                    break
                except RuntimeError:     # the loop changed a lead while it was being copied: try again
                    time.sleep(0.05)
            if res is None:
                return self._send(503, {"error": "busy, try again"})
            res["loop"] = LOOP.status() if LOOP is not None else None
            return self._send(200, res)
        if self.path == "/loop/status":
            if LOOP is None:
                return self._send(503, {"error": "the outreach loop is off"})
            return self._send(200, LOOP.status(light=bool(body.get("light"))))
        if self.path == "/loop/pause":     # the master stop: finish the ONE unit in hand, then nothing runs until /loop/resume (optionally resumes by itself after `minutes`)
            if LOOP is None:
                return self._send(503, {"error": "the outreach loop is off"})
            return self._send(200, LOOP.set_pause(float(body.get("minutes") or 0)))
        if self.path == "/loop/resume":
            if LOOP is None:
                return self._send(503, {"error": "the outreach loop is off"})
            return self._send(200, LOOP.resume())
        if self.path == "/loop/request":   # the page's buttons: the loop does the work itself, one thing at a time
            if LOOP is None:
                return self._send(503, {"error": "the outreach loop is off"})
            kind = str(body.get("kind") or "")
            if kind not in ("run", "pipeline", "forms_find", "forms_send", "replies", "captcha", "send_ready", "keep", "meta_check", "mode"):
                return self._send(400, {"error": "unknown request"})
            if kind in ("run", "forms_send", "captcha", "send_ready") and body.get("confirm") is not True:
                return self._send(400, {"error": "needs confirm: true"})
            LOOP.request(kind, count=body.get("count"), auto=bool(body.get("auto")))
            return self._send(200, {"ok": True})
        if self.path == "/campaign/pause":
            c = str(body.get("campaign") or "clips")
            data = pause_state()
            data[c] = bool(body.get("paused", True))
            PAUSE_FILE.write_text(json.dumps(data), encoding="utf-8")
            return self._send(200, {"ok": True, "paused": data})
        if (self.path in CLIPS_ONLY_ROUTES or self.path == "/clipper/send") and str(body.get("campaign") or "clips") == "clips" and not pause_state().get("clips"):
            ensure_clipper_up()   # the watchdog no longer restarts the Clipper app on its own (clip alerts are off by default): start it when a clip task needs it
        if self.path in CLIPS_ONLY_ROUTES and pause_state().get("clips") and str(body.get("campaign") or "clips") == "clips":
            return self._send(409, {"error": "Clips pitching is paused. Resume it from the Email tab (Autopilot panel) when you want it back."})
        if self.path == "/stop":
            p = STATE.get("proc")
            if p and STATE["running"]:
                p.terminate()
            PIPE["stop"] = True
            if LOOP is not None:
                LOOP.abort = True       # the round the loop is in ends after the current job
            try:
                import loom_upload
                loom_upload.CANCEL.set()   # in-process Loom upload: it checks this between its waits
            except Exception:
                pass
            return self._send(200, {"ok": True})
        if self.path == "/reveal":
            p = str(body.get("path", ""))
            known = {t["video_path"] for t in (sample_templates() or [])}
            allowed = (os.path.realpath(SAMPLES), os.path.realpath(REPO / "opencut-exports"))
            if not p or not os.path.isfile(p) or not (p in known or any(os.path.realpath(p).startswith(a + os.sep) for a in allowed)):
                return self._send(400, {"error": "that file is not one of your samples"})
            subprocess.Popen([*REVEAL_CMD, p])
            return self._send(200, {"ok": True})
        if self.path == "/pipeline/start":
            leads = [l for l in (body.get("leads") or []) if isinstance(l, dict) and l.get("id") and YT_RE.match(str(l.get("url", "")))][:25]
            if not leads:
                return self._send(400, {"error": "no leads with a YouTube episode link"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] preparing clips for {len(leads)} lead(s)"])
                threading.Thread(target=run_pipeline, args=(leads, float(body.get("minutes") or 5), int(body.get("clips") or 1),
                                 body.get("after"), clean(body.get("folder") or "", 80) or None), daemon=True).start()
            return self._send(200, {"ok": True, "leads": len(leads)})
        if self.path == "/emails/find":
            leads = [l for l in (body.get("leads") or []) if isinstance(l, dict) and l.get("id") and l.get("name")][:60]
            if not leads:
                return self._send(400, {"error": "no leads to search"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] looking for public contact emails for {len(leads)} podcast(s)"])
                PIPE["stop"] = False
                threading.Thread(target=run_find_emails, args=(leads,), daemon=True).start()
            return self._send(200, {"ok": True, "leads": len(leads)})
        if self.path == "/email/preview":
            items = list(cold_email.plan(body.get("leads") or [], body.get("template") or {}, "preview", int(body.get("limit") or 20), True, int(body.get("step") or 0)))
            return self._send(200, {"items": items, "status": email_status()})
        if self.path == "/email/run":
            mode = body.get("mode")
            if mode not in ("drafts", "send"):
                return self._send(400, {"error": "mode must be drafts or send"})
            if mode == "send" and body.get("confirm") is not True:
                return self._send(400, {"error": "sending needs confirm: true"})
            st = email_status()
            if not st["ready"]:
                return self._send(400, {"error": "; ".join(st["problems"])})
            campaign = str(body.get("campaign") or "clips")
            tpl = body.get("template") or {}
            has_tpl = (str(tpl.get("subject") or "").strip() and str(tpl.get("body") or "").strip()) or any(
                str(t.get("subject") or "").strip() and str(t.get("body") or "").strip() and t.get("active", True) is not False for t in (tpl.get("library") or []))
            if campaign != "clips" and not has_tpl:
                return self._send(400, {"error": f"the {campaign} campaign has no email template, so nothing was written or sent (the clips template is never used for another campaign)"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] email run ({mode}, {campaign} campaign)"])
                threading.Thread(target=run_email, args=(body.get("leads") or [], body.get("template") or {}, mode, min(60, int(body.get("limit") or 20)),
                                 bool(body.get("ignore_window")), int(body.get("step") or 0), campaign), daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/email/check-replies":
            try:
                campaign = str(body.get("campaign") or "clips")
                res = cold_email.check_replies(body.get("sent") or [], log_fn=lambda m: None, campaign=campaign)
                if campaign == "clips":  # the automatic "yes" answer sends the clips sheet, so it only ever runs for the clips campaign
                    handled = auto_reply.handle(res.get("found") or [], log_fn=log)
                elif campaign == "meta":  # DeepSeek picks which of YOUR reply templates fits; the answer is only ever saved as a draft
                    handled = meta_reply.handle(res.get("found") or [], body.get("reply_templates") or {}, body.get("vars") or {}, log_fn=log, lead_info=body.get("lead_info") or {})
                else:
                    handled = [{**f, "auto": "needs you (this campaign has no automatic reply)"} for f in (res.get("found") or [])]
                res["found"] = [{k: v for k, v in f.items() if k != "text"} for f in handled]
                b = cold_email.check_bounces(log_fn=lambda m: None)
                return self._send(200, {**res, "bounced": b["bounced"], "errors": (res.get("errors") or []) + b["errors"]})
            except Exception as e:
                return self._send(500, {"error": str(e)[:200]})
        if self.path == "/email/personalize":
            leads = [l for l in (body.get("leads") or []) if l.get("clipFile")]
            if not leads:
                return self._send(400, {"error": "no leads with a clip yet"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] writing personal notes for {len(leads)} lead(s)"])
                PIPE["stop"] = False
                threading.Thread(target=run_personalize, args=(leads,), daemon=True).start()
            return self._send(200, {"ok": True, "leads": len(leads)})
        if self.path == "/handoff/lookup":
            code, res = app_call("/api/handoff/list", None, timeout=15)
            if code != 200:
                return self._send(502, {"error": "the clipper app is not running"})
            want = set(str(s) for s in (body.get("slugs") or []))
            return self._send(200, {"links": {s["slug"]: s["link"] for s in (res.get("sheets") or []) if s.get("slug") in want and s.get("link")}})
        if self.path == "/handoff/share":
            slug = str(body.get("slug") or "").strip()
            if not slug:
                return self._send(400, {"error": "this lead has no clip folder yet"})
            code, res = app_call("/api/handoff/share", {"slug": slug}, timeout=300)
            if code == 0:
                return self._send(502, {"error": "the clipper app is not running"})
            if code != 200 or not res.get("success"):
                return self._send(400, {"error": res.get("error") or f"clipper app said {code}"})
            return self._send(200, {"link": res.get("link"), "clips": res.get("clips")})
        if self.path == "/clips/share":
            root = (REPO / "opencut-exports").resolve()
            names = []
            for f in body.get("files") or []:
                p = Path(str(f)).resolve()
                if root in p.parents and p.suffix.lower() == ".mp4" and p.exists():
                    names.append(p.name)
            if not names:
                return self._send(400, {"error": "no clip files found to share"})
            links, errors = [], []
            for k, n in enumerate(names):
                code, shared = app_call("/api/outreach/share-clip", {"id": f"opencut-exports/{n}", "name": f"{(body.get('name') or 'clip')} - clip {k + 1}"}, timeout=300)
                if code == 200 and shared.get("success"):
                    links.append(shared.get("link", ""))
                else:
                    errors.append(shared.get("error") or str(code))
            return self._send(200, {"links": links, "errors": errors})
        if self.path == "/personal/neutralize":
            out = {}
            for k, phrase in (body.get("phrases") or {}).items():
                out[k] = personalize.neutralize(str(phrase)[:300])
            return self._send(200, {"phrases": out})
        if self.path == "/unibox/yes":
            try:
                return self._send(200, auto_reply.preview(str(body.get("to") or ""), str(body.get("name") or ""), str(body.get("slug") or "")))
            except Exception as e:
                return self._send(500, {"error": str(e)[:200]})
        if self.path in ("/unibox/drafts", "/unibox/replies", "/unibox/draft/update", "/unibox/draft/delete", "/unibox/draft/send"):
            try:
                campaign = str(body.get("campaign") or "clips")
                if self.path == "/unibox/drafts":
                    res = cold_email.list_drafts(log_fn=lambda m: None)
                    who = cold_email.campaign_by_recipient()
                    res["drafts"] = [d for d in res.get("drafts", []) if who.get(str(d.get("to", "")).lower(), "clips") == campaign]
                    return self._send(200, res)
                if self.path == "/unibox/replies":
                    return self._send(200, cold_email.list_replies(int(body.get("days") or 21), campaign=campaign))
                mid = str(body.get("mid") or "")
                if self.path == "/unibox/draft/update":
                    return self._send(200, {"mid": cold_email.update_draft(mid, str(body.get("subject") or ""), str(body.get("body") or ""))})
                if self.path == "/unibox/draft/delete":
                    return self._send(200, {"ok": cold_email.discard_draft(mid)})
                return self._send(200, {"ok": cold_email.send_saved_reply(mid)})
            except Exception as e:
                return self._send(500, {"error": str(e)[:200]})
        if self.path == "/fit/run":
            leads = [{k: str(l.get(k) or "")[:300] for k in ("id", "name", "url", "web", "subs")} for l in (body.get("leads") or [])][:40]
            if not leads:
                return self._send(400, {"error": "no leads to check"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                import tempfile
                f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
                json.dump(leads, f)
                f.close()
                STATE.update(running=True, log=[f"[runner] checking {len(leads)} lead(s) with Google AI"])
                threading.Thread(target=run_fit, args=(f.name,), daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/awake":
            if "on" in body:
                return self._send(200, awake_set(bool(body.get("on")), body.get("minutes") or 180))
            return self._send(200, awake_status())
        if self.path == "/audit/find":
            seg = {"fitness": "fitness coach", "weight_loss": "weight loss coach", "executive": "executive coach"}.get(str(body.get("segment") or ""))
            if not seg:
                return self._send(400, {"error": "pick a type of coach"})
            job = {"label": seg, "location": str(body.get("location") or "")[:80], "count": max(1, min(15, int(body.get("count") or 8))),
                   "exclude": [str(x)[:80] for x in (body.get("exclude") or [])][:60]}
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] looking for {job['count']} {seg}s" + (f" in {job['location']}" if job["location"] else "")])
                PIPE["stop"] = False
                threading.Thread(target=run_find_coaches, args=(job,), daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/audit/enrich":
            ps = [{k: str(p.get(k) or "")[:300] for k in ("id", "name", "website")} for p in (body.get("prospects") or []) if p.get("website")][:60]
            if not ps:
                return self._send(400, {"error": "no prospects with a website to look up"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] looking for public emails and phones on {len(ps)} website(s)"])
                PIPE["stop"] = False
                threading.Thread(target=run_enrich_coaches, args=(ps,), daemon=True).start()
            return self._send(200, {"ok": True, "prospects": len(ps)})
        if self.path == "/phones/find":
            leads = [{k: str(l.get(k) or "")[:300] for k in ("id", "name", "url", "web", "episode", "owner", "business")} for l in (body.get("leads") or [])][:40]
            if not leads:
                return self._send(400, {"error": "no leads to search"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] looking for public phone numbers for {len(leads)} lead(s)"])
                PIPE["stop"] = False
                threading.Thread(target=run_find_phones, args=(leads, bool(body.get("ai", True)), str(body.get("prompt") or "")[:3000] or None), daemon=True).start()
            return self._send(200, {"ok": True, "leads": len(leads)})
        if self.path == "/queue/add":
            if body.get("confirm") is not True:
                return self._send(400, {"error": "scheduling sends needs confirm: true"})
            st = email_status()
            if not st["ready"]:
                return self._send(400, {"error": "; ".join(st["problems"])})
            tpl = body.get("template") or {}
            if not (str(tpl.get("subject") or "").strip() and str(tpl.get("body") or "").strip()) and not (tpl.get("library") or []):
                return self._send(400, {"error": "no email template, so nothing was scheduled"})
            items = [{"id": str(b.get("id") or "")[:80], "to": str(b.get("to") or (b.get("lead") or {}).get("email") or "")[:200], "company": str(b.get("company") or "")[:80],
                      "lead": b.get("lead") or {}, "template": tpl, "campaign": str(body.get("campaign") or "meta"), "step": int(body.get("step") or 0)} for b in (body.get("items") or [])][:60]
            return self._send(200, {"items": send_queue.add(items)})
        if self.path == "/queue/list":
            st = email_status()
            return self._send(200, {"items": send_queue.listing(), "now": time.time(), "in_send_window": st["in_send_window"], "window": st["window"], "capacity_left": st["capacity_left"], "ready": st["ready"]})
        if self.path == "/queue/cancel":
            return self._send(200, {"ok": send_queue.cancel(str(body.get("id") or ""))})
        if self.path == "/queue/reschedule":
            when = body.get("when")
            return self._send(200, {"due_at": send_queue.reschedule(str(body.get("id") or ""), float(when) if when else None)})
        if self.path == "/halcrus/status":
            import halcrus_leads
            ok, at = halcrus_leads.connected()
            return self._send(200, {"connected": ok, "at": at})
        if self.path == "/meta/status":
            import meta_session
            ok, at = meta_session.connected()
            return self._send(200, {"connected": ok, "at": at})
        if self.path == "/meta/login":
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=["[runner] opening the Meta Ad Library so you can log in to Facebook (a Chrome window will appear)"])
                threading.Thread(target=run_meta_login, daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/halcrus/login":
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=["[runner] opening HALCRUS so you can log in (a Chrome window will appear)"])
                threading.Thread(target=run_halcrus_login, daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/halcrus/leads":
            tier = str(body.get("tier") or "Hot")
            if tier not in ("Hot", "Warm", "Nurture", "All"):
                tier = "Hot"
            want = max(1, min(40, int(body.get("want") or 10)))
            emails = [str(x)[:200] for x in (body.get("known_emails") or [])][:20000]
            companies = [str(x)[:120] for x in (body.get("known_companies") or [])][:20000]
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] looking for {want} more {tier} leads in HALCRUS"])
                PIPE["stop"] = False
                threading.Thread(target=run_halcrus_leads, args=(want, emails, companies, tier), daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path in ("/cform/find", "/cform/send", "/cform/batch"):
            kind = self.path.rsplit("/", 1)[1]
            raw = body.get("items") or []
            if kind == "find":
                items = [{"id": str(b.get("id") or "")[:80], "domain": str(b.get("domain") or "")[:200], "web": str(b.get("web") or "")[:200]} for b in raw][:20]
                target, args, label = run_cform_find, (items,), f"looking for contact forms on {len(items)} site(s)"
            else:
                if body.get("confirm") is not True:
                    return self._send(400, {"error": "needs confirm: true"})
                items = [{"id": str(b.get("id") or "")[:80], "url": str(b.get("url") or "")[:500], "message": str(b.get("message") or "")[:3000], "subject": str(b.get("subject") or "")[:150]} for b in raw
                         if str(b.get("url") or "").startswith("http")][:(25 if kind == "send" else 250)]
                target, args, label = (run_cform_send if kind == "send" else run_cform_batch), (items, body), (f"sending {len(items)} message(s) through contact forms" if kind == "send" else f"{len(items)} CAPTCHA form(s) in a window for you, 8 at a time")
            if not items:
                return self._send(400, {"error": "nothing to do"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] {label}"])
                PIPE["stop"] = False
                threading.Thread(target=target, args=args, daemon=True).start()
            return self._send(200, {"ok": True, "items": len(items), "sent_today": cform_today()})
        if self.path == "/cform/replies":
            items = [{"id": str(b.get("id") or ""), "domain": str(b.get("domain") or "").lower(), "since_ts": int(b.get("since_ts") or 0), "email": str(b.get("email") or "")} for b in (body.get("items") or [])][:100]
            try:
                return self._send(200, {"ok": True, "replies": cform_replies(items), "sent_today": cform_today()})
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)[:160], "replies": []})
        if self.path == "/emails/check":
            items = [{"id": str(b.get("id") or "")[:80], "email": str(b.get("email") or "")[:200], "company": str(b.get("company") or "")[:100], "web": str(b.get("web") or "")[:200]} for b in (body.get("items") or [])][:40]
            if not items:
                return self._send(400, {"error": "nothing to check"})
            import tempfile
            fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(items, fh)
            fh.close()
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] checking {len(items)} email(s)" + (" with Google AI" if body.get("ai") else "") + (" and Jitbit List Cleaner" if body.get("jitbit", True) else "")])
                threading.Thread(target=run_email_check, args=(fh.name, bool(body.get("ai")), body.get("jitbit", True) is not False), daemon=True).start()
            return self._send(200, {"ok": True, "items": len(items)})
        if self.path == "/loom/uploads":
            import loom_upload
            return self._send(200, {"uploads": loom_upload._uploads()})
        if self.path == "/loom/status":
            import loom_upload
            ok, at = loom_upload.connected()
            return self._send(200, {"connected": ok, "at": at})
        if self.path == "/loom/login":
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=["[runner] opening Loom so you can log in (a Chrome window will appear)"])
                threading.Thread(target=run_loom_login, daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/loom/upload":
            items = [{k: str(b.get(k) or "")[:300] for k in ("id", "file", "title", "comment")} for b in (body.get("items") or [])][:5]
            if not items:
                return self._send(400, {"error": "nothing to upload"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] uploading {len(items)} approved video(s) to Loom"])
                PIPE["stop"] = False
                threading.Thread(target=run_loom_upload, args=(items, bool(body.get("show")), bool(body.get("keep_local"))), daemon=True).start()
            return self._send(200, {"ok": True, "items": len(items)})
        if self.path == "/ads/video":
            items = [{k: str(b.get(k) or "")[:300] for k in ("id", "company", "email", "web", "first", "business")} for b in (body.get("items") or [])][:5]
            if not items:
                return self._send(400, {"error": "nothing to make"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] making {len(items)} ad video(s)"])
                PIPE["stop"] = False
                threading.Thread(target=run_ad_video, args=(items, str(body.get("voice") or "")[:60], str(body.get("product") or "")[:60], str(body.get("link") or "")[:200] or None, "drive" if body.get("host") == "drive" else "loom", (str(body.get("narrator"))[:40] if body.get("narrator") is not None else None)), daemon=True).start()
            return self._send(200, {"ok": True, "items": len(items)})
        if self.path == "/ads/ig":
            items = [{k: str(b.get(k) or "")[:300] for k in ("id", "company", "web", "person", "title")} for b in (body.get("items") or [])][:30]
            if not items:
                return self._send(400, {"error": "nothing to look up"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] looking up Instagram accounts for {len(items)} prospect(s)"])
                PIPE["stop"] = False
                threading.Thread(target=run_ig, args=(items, str(body.get("prompt") or "")[:3000] or None), daemon=True).start()
            return self._send(200, {"ok": True, "items": len(items)})
        if self.path == "/ads/names":
            items = [{k: str(b.get(k) or "")[:200] for k in ("id", "email", "company", "person")} for b in (body.get("items") or [])][:200]
            return self._send(200, {"names": greeting.resolve(items)})
        if self.path == "/ads/find":
            items = [{k: str(b.get(k) or "")[:300] for k in ("id", "company", "web", "facebook")} for b in (body.get("items") or [])][:20]
            if not items:
                return self._send(400, {"error": "nothing to look up"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] looking up decision makers for {len(items)} advertiser(s)"])
                threading.Thread(target=run_ads, args=(items, str(body.get("prompt") or "")[:3000] or None), daemon=True).start()
            return self._send(200, {"ok": True, "items": len(items)})
        if self.path == "/maps/resolve":
            items = [{k: str(b.get(k) or "")[:300] for k in ("id", "business", "web", "address", "category")} for b in (body.get("items") or [])][:30]
            if not items:
                return self._send(400, {"error": "nothing to look up"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] checking {len(items)} business(es) for an owner and a YouTube channel"])
                threading.Thread(target=run_maps, args=(items,), daemon=True).start()
            return self._send(200, {"ok": True, "items": len(items)})
        if self.path == "/email/unsuppress":
            addr = str(body.get("email") or "").strip().lower()
            if "@" not in addr:
                return self._send(400, {"error": "need an email address"})
            return self._send(200, {"ok": True, "removed": cold_email.unsuppress(addr)})
        if self.path == "/email/suppress":
            addr = str(body.get("email") or "").strip().lower()
            if "@" not in addr:
                return self._send(400, {"error": "need an email address"})
            cold_email.suppress(addr, str(body.get("why") or "asked not to be contacted")[:80])
            return self._send(200, {"ok": True})
        if self.path == "/email/validate":
            import email_check
            return self._send(200, {"results": {k: email_check.classify(v) for k, v in (body.get("emails") or {}).items()}})
        if self.path == "/email/thread":
            addr = str(body.get("email") or "").strip().lower()
            rows = [{k: e.get(k) for k in ("ts", "from", "subject", "body", "step", "status")} for e in cold_email.read_log() if str(e.get("to") or "").lower() == addr and e.get("status") in ("sent", "auto_replied")]
            return self._send(200, {"sent": rows})
        if self.path == "/email/sent":
            cutoff = time.time() - 30 * 86400
            camp = str(body.get("campaign") or "clips")
            rows = [{k: e.get(k) for k in ("id", "to", "from", "ts", "step", "subject", "variant", "provider")} for e in cold_email.sent_entries() if e.get("ts", 0) > cutoff and cold_email.campaign_of(e) == camp]
            return self._send(200, {"sent": rows})
        if self.path == "/notify":
            if str(body.get("topic") or "") == "clips" and not clips_alerts():
                return self._send(200, {"ok": True, "phone": False, "phone_configured": phone_ready(), "muted": "clips alerts are off"})
            sent = do_notify(body.get("title"), body.get("message"), body.get("key"), body.get("every") or 600, phone=bool(body.get("phone", True)))
            return self._send(200, {"ok": True, "phone": sent, "phone_configured": phone_ready()})
        if self.path == "/ads/health":   # is the Meta Ad Library answering right now? (one search for a brand that certainly advertises)
            if STATE["running"]:
                return self._send(200, {"healthy": None, "busy": True})
            try:
                import ad_video
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    ctx, closer = ad_video.open_context(p)
                    try:
                        ok = ad_video.lookup_healthy(ctx.new_page(), lambda m: None)
                    finally:
                        closer()
                return self._send(200, {"healthy": bool(ok)})
            except Exception as e:
                return self._send(200, {"healthy": None, "error": str(e)[:100]})
        if self.path == "/notify/prefs":
            prefs = notify_prefs()
            for k in ("clips", "skips", "bounces", "summary"):
                if k in body:
                    prefs[k] = bool(body.get(k))
            if any(k in body for k in ("clips", "skips", "bounces", "summary")):
                NOTIFY_PREFS.write_text(json.dumps(prefs), encoding="utf-8")
            return self._send(200, {"ok": True, "clips": clips_alerts(), "skips": bool(prefs.get("skips")), "bounces": bool(prefs.get("bounces")), "summary": bool(prefs.get("summary"))})
        if self.path == "/email/stats":
            return self._send(200, cold_email.stats())
        if self.path == "/pipeline/stop":
            PIPE["stop"] = True
            return self._send(200, {"ok": True})
        if self.path == "/clipper/send":
            url, start, end = str(body.get("url", "")).strip(), str(body.get("start", "")).strip(), str(body.get("end", "")).strip()
            name = clean(body.get("name") or "Outreach sample", 60) or "Outreach sample"
            folder = clean(body.get("folder") or "", 80) or None
            if not YT_RE.match(url) or not TIME_RE.match(start) or not TIME_RE.match(end):
                return self._send(400, {"error": "need a YouTube link and start/end like 12:00"})
            with LOCK:
                if STATE["running"]:
                    return self._send(409, {"error": "a run is already in progress"})
                STATE.update(running=True, log=[f"[runner] sending to clipper: {name}"])
                threading.Thread(target=run_send, args=(url, start, end, name, folder), daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        with LOCK:
            if STATE["running"]:
                return self._send(409, {"error": "a run is already in progress"})
            niches = [clean(x) for x in body.get("niches", []) if clean(x)][:15]
            if not niches:
                return self._send(400, {"error": "no niches"})
            argv = ["--count", str(max(1, min(25, int(body.get("count") or 10))))]
            focus = clean(body.get("focus", ""), 200)
            if focus:
                argv += ["--focus", focus]
            if body.get("dry"):
                argv.append("--dry-run")
            elif body.get("emit"):
                argv.append("--emit")
            argv += niches
            STATE.update(running=True, log=[f"[runner] starting: {' '.join(argv)}"])
            threading.Thread(target=run_job, args=(argv,), daemon=True).start()
        self._send(200, {"ok": True})


CLIPPER_DIR = Path(os.getenv("CLIPPER_REPO") or Path.home() / "instagram-video-generator")
CLIPPER_LOG = Path.home() / "Library" / "Logs" / "clipper" / "app.log"


def start_clipper_app():
    """Starts the clipper web app (python3 app.py) in its own session, so it keeps running if this helper stops. Returns True if started."""
    try:
        CLIPPER_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(CLIPPER_LOG, "a")
        subprocess.Popen([sys.executable, "app.py"], cwd=str(CLIPPER_DIR), stdout=log, stderr=log, start_new_session=True)
        return True
    except Exception:
        return False


def ensure_clipper_up():
    try:
        urllib.request.urlopen(urllib.request.Request(APP_URL + "/api/heartbeat", data=b"{}", headers={"Content-Type": "application/json"}), timeout=4).read()
        return True
    except Exception:
        pass
    if not start_clipper_app():
        return False
    for _ in range(20):
        time.sleep(1)
        try:
            urllib.request.urlopen(urllib.request.Request(APP_URL + "/api/heartbeat", data=b"{}", headers={"Content-Type": "application/json"}), timeout=3).read()
            return True
        except Exception:
            continue
    return False


def keep_clipper_awake():
    """Keeps the clipper app alive and running while this helper runs:
    - pings it every 30 seconds so its idle watchdog never shuts it down;
    - if it stops answering for about 90 seconds (it was killed, for example by macOS under memory pressure), starts it again and sends a phone alert.
    Set KEEP_CLIPPER_ALIVE=0 to turn all of this off, or AUTO_RESTART_CLIPPER=0 to only ping."""
    misses, last_restart = 0, 0
    while True:
        try:
            urllib.request.urlopen(urllib.request.Request(APP_URL + "/api/heartbeat", data=b"{}", headers={"Content-Type": "application/json"}), timeout=5).read()
            misses = 0
        except Exception:
            misses += 1
            if misses >= 3 and clips_alerts() and os.getenv("AUTO_RESTART_CLIPPER", "1") != "0" and time.time() - last_restart > 300:
                last_restart = time.time()
                ok = start_clipper_app()
                phone_notify("Clipper app restarted" if ok else "Clipper app is down", "The clipper app stopped" + (", so I started it again." if ok else " and I could not restart it. Start it in Terminal: cd ~/instagram-video-generator && python3 app.py"), key="clipper-restart", every=1800)
                misses = 0
        time.sleep(30)


if __name__ == "__main__":
    if os.getenv("KEEP_CLIPPER_ALIVE", "1") != "0":
        threading.Thread(target=keep_clipper_awake, daemon=True).start()
    load_env()
    STORE = crm_store.get_store()
    if os.getenv("OUTREACH_LOOP", "1") != "0":
        if os.getenv("LOOP_STUBS") == "1":      # tests: never touch Loom, HALCRUS, forms or email for real
            import loop_stubs
            _ports = loop_stubs.StubPorts()
        else:
            _ports = Ports()
        LOOP = outreach_loop.Loop(STORE, _ports, tick=int(os.getenv("LOOP_TICK", "15")), start_delay=int(os.getenv("LOOP_START_DELAY", "30")))
        threading.Thread(target=LOOP.run_forever, daemon=True).start()
    threading.Thread(target=STORE.flusher, daemon=True).start()

    def _bye(*a):       # launchd stops the helper with SIGTERM: keep the last couple of seconds of changes
        try:
            STORE.flush(True)
            if LOOP is not None:
                LOOP.save_state()
        finally:
            os._exit(0)
    import signal
    signal.signal(signal.SIGTERM, _bye)
    if os.getenv("QUEUE_LOOP", "1") != "0":     # tests run a second helper that must never send from the real queue
        threading.Thread(target=queue_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"Clipper runner listening on http://127.0.0.1:{PORT}  (leave this window open, Ctrl+C to stop)")
    if not (os.getenv("CRM_SYNC_URL") and os.getenv("CRM_SYNC_PASSWORD")):
        print("Note: CRM_SYNC_URL / CRM_SYNC_PASSWORD not set. Put them in tools/.env or runs will be preview only.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        STORE.flush(True)
