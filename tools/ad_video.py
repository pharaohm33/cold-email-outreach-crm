#!/usr/bin/env python3
"""
A narrated, face-free video of a prospect's own Meta ads, made without anyone recording anything.

    python3 tools/ad_video.py '{"id":"x","company":"Uproot Clean","email":"hi@uprootclean.com","web":"https://uprootclean.com","first":"Sophia","business":"Uproot Clean"}'

For each lead:
  1. Finds the advertiser in the Meta Ad Library by the email's domain (the website's domain if the email is Gmail and the like).
  2. BRAND CHECK: reads the destination domain of the ads on that advertiser's page and only goes on when they really point at the lead's own domain.
     A lookalike advertiser with a similar name is rejected.
  3. Loads the advertiser's own view with the filters cleared (active AND inactive), then the "active" view, and reads the real counts.
     No active ads means no video: there is nothing true to show.
  4. Writes a short script from those facts only, speaks it (Kokoro by default, Microsoft's HD voices as the fallback), and records the page the way a person
     browses it: wandering up and down near the top of the page, with a few real clicks into ads to open their details, and captions that match the words.
     The sign up link sits on screen for the last 20 seconds and no other web address is visible anywhere.
     Output: tools/ad_videos/<id>.mp4 and <id>.json with the facts behind every number said.

The script never claims anything it did not read off the page, and it says "I took a look at", never "I'm looking right now".
"""
import asyncio
import difflib
import json
import math
import random
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
if not shutil.which("ffmpeg"):  # no system ffmpeg: use the one the static-ffmpeg package carries (tools/.venv)
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
    except Exception:
        pass
OUT = HERE / "ad_videos"
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com", "live.com", "msn.com", "me.com", "protonmail.com", "ymail.com",
             "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "bellsouth.net", "googlemail.com"}
LIB = "https://www.facebook.com/ads/library/"
VIEWPORT = {"width": 1280, "height": 720}
KOKORO_URL = "http://localhost:3000/api/v1/audio/speech"
DIA_URL = "http://127.0.0.1:8766"               # tools/dia_server.py, run by the Dia venv; started on demand
DIA_PY = HERE / ".dia_venv" / "bin" / "python"
DEFAULT_VOICE = "kokoro:am_michael"        # runs locally from tools/.kokoro_venv (about 9x faster than real time); "dia" is the slower, cloned-voice alternative
KOKORO_PY = HERE / ".kokoro_venv" / "bin" / "python"
FALLBACK_VOICE = "edge:en-US-AndrewMultilingualNeural"
SIGNUP_LINK = "https://portal.aiadsoptimization.com/signup"
LINK_SECONDS = 20  # (no longer used to start the link early: it appears only at the call to action)
FPS = 15


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def open_context(p):
    """(context, closer): the logged-in Meta profile when "Connect Meta" was done (Meta answers a logged-in browser far more reliably), else a plain headless Chrome."""
    try:
        import meta_session
        if meta_session.connected()[0]:
            ctx = meta_session.context(p, False)
            return ctx, ctx.close
    except Exception:
        pass
    browser = p.chromium.launch(channel="chrome", headless=True)
    return browser.new_context(viewport=VIEWPORT, locale="en-US"), browser.close


def brand_domain(email, web):
    """The domain whose ads we expect to see: the email's, or the website's when the email is a free mailbox."""
    d = str(email or "").split("@")[-1].lower().strip()
    if d and d not in FREE_MAIL:
        return d
    w = re.sub(r"^https?://(www\.)?", "", str(web or "").lower()).split("/")[0]
    return w


def root_label(domain):
    parts = [p for p in str(domain).lower().split(".") if p and p != "www"]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov") and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def host_matches(host, label):
    """order.uprootclean.com, www.uprootclean.co and uprootclean.com all match the label 'uprootclean'."""
    label = norm(label)
    if len(label) < 4:
        return False
    return any(norm(part) == label or (len(label) >= 6 and norm(part).startswith(label)) for part in str(host).lower().split("."))


def name_close(a, b):
    import difflib
    a, b = norm(a), norm(b)
    return bool(a and b) and (a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8)


COUNTRY = ["US"]  # the Ad Library country for this video: US first; ALL when the advertiser is only found outside the US (a .co.uk shop, for example)


def lib_url(**kw):
    q = {"active_status": "all", "ad_type": "all", "country": COUNTRY[0], "media_type": "all"}
    q.update(kw)
    return LIB + "?" + urllib.parse.urlencode(q)


def settle(pg, log, secs=11):
    """Waits for results or the 'no ads' message. Returns 'results', 'none' or 'timeout'."""
    for _ in range(int(secs * 2)):
        pg.wait_for_timeout(500)
        t = pg.inner_text("body")
        if re.search(r"~?[\d,]+ results?", t):
            return "results"
        if "No ads match" in t:
            return "none"
    return "timeout"


def clear_filters(pg, log):
    """The Ad Library sometimes opens with a filter on (for example Active ads only). Open Filters, Clear all, Apply."""
    try:
        pg.get_by_role("button", name=re.compile(r"^Filters", re.I)).first.click(timeout=4000)
        pg.wait_for_timeout(1200)
        pg.get_by_text(re.compile(r"^Clear all$", re.I)).first.click(timeout=4000)
        pg.wait_for_timeout(500)
        pg.get_by_role("button", name=re.compile(r"^Apply", re.I)).first.click(timeout=4000)
        pg.wait_for_timeout(2500)
        log("  cleared the filters")
        return True
    except Exception:
        try:
            pg.keyboard.press("Escape")
        except Exception:
            pass
        return False


def count_results(pg):
    t = pg.inner_text("body")
    m = re.search(r"~?([\d,]+) results?", t)
    return int(m.group(1).replace(",", "")) if m else 0


def dest_hosts(pg):
    return pg.evaluate("""()=>[...document.querySelectorAll("a[href*='l.facebook.com/l.php']")].map(a=>{try{return new URL(new URL(a.href).searchParams.get('u')).hostname.toLowerCase()}catch(e){return ''}}).filter(Boolean)""")


def lookup_healthy(pg, log):
    """True when the Ad Library answers a search for a brand that certainly advertises. A 'no advertiser found' only counts when this is true: if Meta is
    rate-limiting or not answering, an empty result says nothing about the lead."""
    COUNTRY[0] = "US"
    try:
        pg.goto(lib_url(q="nike", search_type="keyword_unordered"), wait_until="domcontentloaded")
        return settle(pg, log, 15) == "results"
    except Exception:
        return False


def foreign_domain(domain):
    """A country domain (.co.uk, .com.au, .ca ...): its ads are likelier to be outside the US."""
    tld = str(domain).lower().rsplit(".", 1)[-1]
    return len(tld) == 2 and tld not in ("us", "co", "io", "ai", "me", "tv", "fm", "ly", "gg")


def candidates(pg, domain, company, log):
    """Advertiser ids from a keyword search on the domain (then on the company name), most frequent first: [(page_id, page_name)]."""
    out = []
    for q in (domain, company):
        if not q:
            continue
        pg.goto(lib_url(q=q, search_type="keyword_unordered"), wait_until="domcontentloaded")
        if settle(pg, log) != "results":
            continue
        for _ in range(2):
            pg.mouse.wheel(0, 900)
            pg.wait_for_timeout(1200)
        html = pg.content()
        pairs = re.findall(r'"page_id":"(\d+)"[^{}]{0,300}?"page_name":"([^"]+)"', html) + [(i, n) for n, i in re.findall(r'"page_name":"([^"]+)"[^{}]{0,300}?"page_id":"(\d+)"', html)]
        freq = {}
        for i, n in pairs:
            freq[(i, n)] = freq.get((i, n), 0) + 1
        for (i, n), _c in sorted(freq.items(), key=lambda kv: -kv[1]):
            if (i, n) not in out:
                out.append((i, n))
        if out:
            break
    return out[:4]


def read_cards(pg):
    """[{status, started, shared}] for the ads loaded so far, from the page text."""
    for _ in range(4):
        pg.mouse.wheel(0, 1100)
        pg.wait_for_timeout(1300)
    t = pg.inner_text("body").replace("​", "")
    parts = re.split(r"\n(?=(?:Active|Inactive)\s*\n\s*Library ID:)", t)
    cards = []
    for seg in parts[1:]:
        st = re.match(r"(Active|Inactive)", seg)
        if not st:
            continue
        sh = re.search(r"(\d+) ads use this creative and text", seg[:700])
        sd = re.search(r"Started running on ([A-Za-z]+ \d+, \d{4})", seg[:300])
        cards.append({"status": st.group(1), "started": sd.group(1) if sd else "", "shared": int(sh.group(1)) if sh else 0})
    return cards


def view_url(pid, status):
    return lib_url(active_status=status, view_all_page_id=pid, search_type="page")


def inspect(pg, pid, label, log):
    """Loads the advertiser's own view (filters cleared) and returns the facts, or None when the page does not belong to this brand."""
    pg.goto(view_url(pid, "all"), wait_until="domcontentloaded")
    state = settle(pg, log)
    if state != "results":
        if clear_filters(pg, log):
            state = settle(pg, log)
    if state != "results":
        return {"page_id": pid, "ok": False, "reason": "the advertiser page shows no ads even with the filters cleared"}
    total = count_results(pg)
    for _ in range(3):
        pg.mouse.wheel(0, 900)
        pg.wait_for_timeout(1200)
    hosts = dest_hosts(pg)
    match = [h for h in hosts if host_matches(h, label)]
    rate = len(match) / len(hosts) if hosts else 0.0
    pg.goto(view_url(pid, "active"), wait_until="domcontentloaded")
    st = settle(pg, log)
    if st == "none":
        active = 0
    elif st == "results":
        active = count_results(pg)
    else:
        if clear_filters(pg, log):
            pg.goto(view_url(pid, "active"), wait_until="domcontentloaded")
            st = settle(pg, log)
        active = count_results(pg) if st == "results" else 0
    cards = read_cards(pg) if active else []
    shared_cards = [c for c in cards if c["shared"] >= 2]
    return {"page_id": pid, "ok": True, "total": total, "active": active, "hosts": sorted(set(hosts))[:8], "host_match_rate": round(rate, 2),
            "matching_hosts": sorted(set(match))[:6], "looked": len(cards), "shared_creative_ads": len(shared_cards), "max_shared": max([c["shared"] for c in shared_cards] or [0]),
            "oldest_active_start": (sorted([c["started"] for c in cards if c["started"]])[:1] or [""])[0]}


def say(n):
    return f"{n:,}"


# ------------------------------------------------------------------------------------------------ the voice

def parse_voice(voice):
    """'kokoro:af_heart' | 'dia' | 'edge:en-US-AndrewMultilingualNeural' | a bare Edge voice name -> (engine, id)."""
    v = str(voice or DEFAULT_VOICE)
    if v == "dia":
        return "dia", "ref"
    if ":" in v:
        e, i = v.split(":", 1)
        return (e if e in ("kokoro", "dia", "edge") else "edge"), i
    return "edge", v


def voice_gender(voice):
    e, i = parse_voice(voice)
    if e == "dia":
        return "m"  # the saved reference voice (tools/assets/dia_ref.wav) is a man's voice, about 104 Hz
    if e == "kokoro":
        return "f" if i[:2] in ("af", "bf") else "m"
    return "f" if re.search(r"Ava|Emma|Aria|Jenny|Michelle|Ana|Sonia|Libby|Nancy|Jane|Sara|Amber|Ashley|Cora|Elizabeth|Monica|Clara", i) else "m"


def build_script(facts, first, business, narrator, product="", price="", host="loom"):
    """Strategy 2, the Loom: the Loom script from the client acquisition system, spoken as a recorded video. The only fact it states is the active ad count read off the page.
    `narrator` is the name said in the video: the sender's own name, or empty to say no name at all. (`product` and `price` are not used: this script names neither,
    and Strategy 1's pricing line is never mixed into it.)"""
    a = facts["active"]
    poss = business + ("'" if business.lower().endswith("s") else "'s")
    running = f"{say(a)} different ad{'s' if a != 1 else ''}"
    given = "given how many ads you're running" if a >= 3 else "given the ads you're running"
    if host == "loom":  # on Loom the link is clickable in the comments and the link area under the video
        cta = "If you want to take a look at our website, I've linked it below this video. It's on your screen now too."
    else:
        cta = "If you want to take a look at our website, the link is on your screen now."
    lines = [f"Hey {first}, it's {narrator} here." if narrator else f"Hey {first}.",
             f"I took a look at {poss} Facebook ads.",  # the Loom template says "looking right now"; this video is recorded, so it says what is true
             f"And I noticed you're currently running {running}. I can almost guarantee some of these here are wasting your ad budget, that don't need to be, and that's money that can be right back in your pocket.",
             "We've actually got an AI software that monitors Meta ad accounts 24/7 and automatically cuts wasted ad spend.",
             f"And {given}, I'm extremely confident that it can help save on your ad spend.",
             "And I believe in my product so much that we don't charge anything up front. We prove that it can get results for you before we ask you to pay us anything.",
             cta,
             "I think there's definitely potential we can help you out.",
             "If you're open to trying it out or have any questions, just reply to my email and I'm happy to help."]
    return " ".join(lines)


def _duration(path):
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(d.stdout.strip() or 0)


def _spread(tokens, start, dur):
    """Gives each token a start and end inside [start, start+dur], in proportion to its length, with a little extra after commas."""
    w = [len(re.sub(r"[^A-Za-z0-9]", "", t)) + 2 + (4 if t.endswith(",") else 0) for t in tokens]
    tot = sum(w) or 1
    out, t = [], start
    for tok, wi in zip(tokens, w):
        d = dur * wi / tot
        out.append((t, t + d, tok))
        t += d
    return out


def _speak_edge(text, voice_id, mp3):
    import edge_tts

    async def go():
        comm = edge_tts.Communicate(text, voice_id, boundary="WordBoundary")
        audio, words = bytearray(), []
        async for ch in comm.stream():
            if ch["type"] == "audio":
                audio.extend(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append((ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7, ch["text"]))
        return bytes(audio), words
    audio, bw = asyncio.run(go())
    if len(audio) < 2000:
        raise RuntimeError("the free voice service did not answer")
    Path(mp3).write_bytes(audio)
    secs = _duration(mp3)
    tokens = text.split()
    if len(bw) == len(tokens):
        words = [(s, e, tok) for (s, e, _w), tok in zip(bw, tokens)]
    else:  # the service split a token differently: spread proportionally instead
        words = _spread(tokens, 0.1, max(0.1, secs - 0.2))
    return secs, words


def _speak_kokoro_local(text, voice_id, mp3, log):
    """Kokoro from tools/.kokoro_venv in one call (about 6 s for a whole script); returns Kokoro's own word times lined up with the caption text."""
    tmp = Path(tempfile.mkdtemp(prefix="kokl_"))
    try:
        wav = tmp / "say.wav"
        r = subprocess.run([str(KOKORO_PY), "-I", str(HERE / "kokoro_say.py")], input=json.dumps({"text": _spoken(text), "voice": voice_id, "out": str(wav)}),
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not wav.exists():
            raise RuntimeError("local Kokoro failed: " + (r.stderr.strip().splitlines() or ["no output"])[-1][:120])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-q:a", "3", str(mp3)], check=True)
        secs = _duration(mp3)
        toks = text.split()
        return secs, (_align(toks, out["words"], secs) or _spread(toks, 0, secs))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _speak_kokoro(text, voice_id, mp3, log):
    import requests
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    tmp = Path(tempfile.mkdtemp(prefix="kok_"))
    try:
        files, words, t = [], [], 0.0
        sil = tmp / "sil.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.12", "-q:a", "5", str(sil)], check=True)
        for i, s in enumerate(sentences):
            r = requests.post(KOKORO_URL, json={"model": "model_q8f16", "voice": voice_id, "input": s, "response_format": "mp3"}, timeout=240)
            if r.status_code != 200 or len(r.content) < 2000:
                raise RuntimeError(f"Kokoro answered {r.status_code}")
            raw = tmp / f"r{i}.mp3"
            raw.write_bytes(r.content)
            f = tmp / f"s{i}.mp3"  # cut the silence Kokoro leaves at the start and end of each sentence, so the voice does not hang between them
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-af",
                            "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.02,areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.02,areverse",
                            "-c:a", "libmp3lame", "-q:a", "3", str(f)], check=True)
            d = _duration(f)
            words += _spread(s.split(), t, d)
            files += [f, sil]
            t += d + 0.12
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in files[:-1]), encoding="utf-8")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "libmp3lame", "-q:a", "3", str(mp3)], check=True)
        return _duration(mp3), words
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _dia_up():
    import requests
    try:
        return requests.get(DIA_URL + "/health", timeout=3).status_code == 200
    except Exception:
        return False


def _dia_start(log):
    """Starts the Dia server (model loads in about 80 s, then stays loaded) unless it is already answering."""
    if _dia_up():
        return
    if not DIA_PY.exists():
        raise RuntimeError("Dia is not installed (tools/.dia_venv is missing)")
    log("  (starting the Dia voice server, the first load takes about 80 seconds...)")
    subprocess.Popen([str(DIA_PY), str(HERE / "dia_server.py")], stdout=open(HERE / ".dia_server.log", "ab"), stderr=subprocess.STDOUT, start_new_session=True)
    import time
    for _ in range(150):
        time.sleep(2)
        if _dia_up():
            return
    raise RuntimeError("the Dia server did not come up in 5 minutes (see tools/.dia_server.log)")


_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _num_words(n):
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else "")
    if n < 1000:
        return f"{_ONES[n // 100]} hundred" + (f" {_num_words(n % 100)}" if n % 100 else "")
    if n < 1000000:
        return f"{_num_words(n // 1000)} thousand" + (f" {_num_words(n % 1000)}" if n % 1000 else "")
    return str(n)


def _spoken(text):
    """Numbers as words for Dia ('1,200' -> 'twelve hundred' is avoided: it says 'one thousand two hundred')."""
    return re.sub(r"\d[\d,]*", lambda m: _num_words(int(m.group().replace(",", ""))), text)


DIA_ONE_SHOT_WORDS = 125  # Dia holds about 35 s per generation, the reference clip included: this many words fit in one take


def _dia_parts(text, limit=DIA_ONE_SHOT_WORDS):
    """The whole script in one piece when it fits; otherwise the fewest balanced pieces, cut at sentence ends."""
    sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x.strip()]
    total = sum(len(x.split()) for x in sents)
    n = max(1, -(-total // limit))
    parts, cur, size = [], [], 0
    for x in sents:
        w = len(x.split())
        if cur and len(parts) < n - 1 and size + w / 2 > total / n * (len(parts) + 1):
            parts.append(" ".join(cur))
            cur = []
        cur.append(x)
        size += w
    parts.append(" ".join(cur))
    return parts


def _norm_word(w):
    return re.sub(r"[^a-z0-9']", "", w.lower())


def _align(tokens, heard, dur):
    """Real word times for the caption tokens from the words Dia's take was transcribed into ([[start, end, word], ...]).
    Returns [(start, end, token)], or None if too little of the text lines up."""
    spoken, owner = [], []
    for i, tok in enumerate(tokens):
        for w in (_spoken(tok).split() or [tok]):
            spoken.append(_norm_word(w))
            owner.append(i)
    if not spoken or not heard:
        return None
    st, en = [None] * len(spoken), [None] * len(spoken)
    for i, j, n in difflib.SequenceMatcher(None, spoken, [_norm_word(h[2]) for h in heard], autojunk=False).get_matching_blocks():
        for k in range(n):
            st[i + k], en[i + k] = heard[j + k][0], heard[j + k][1]
    known = [i for i, v in enumerate(st) if v is not None]
    if len(known) < 0.6 * len(spoken):
        return None
    prev_end, i = 0.0, 0
    while i < len(spoken):  # words the transcript missed or spelled differently ("20" for "twenty") share the gap around them
        if st[i] is not None:
            prev_end, i = en[i], i + 1
            continue
        j = i
        while j < len(spoken) and st[j] is None:
            j += 1
        nxt = st[j] if j < len(spoken) else max(dur, prev_end)
        step = max(nxt - prev_end, 0) / (j - i)
        for k in range(i, j):
            st[k], en[k] = prev_end + step * (k - i), prev_end + step * (k - i + 1)
        i = j
    out, last = [], 0.0
    for ti, tok in enumerate(tokens):
        idx = [k for k, o in enumerate(owner) if o == ti]
        a = max(st[idx[0]], last)
        e = max(en[idx[-1]], a + 0.02)
        out.append((a, e, tok))
        last = e
    return out


def _speak_dia(text, mp3, log):
    """The whole script in one take in the saved reference voice (several pieces only if it is too long for one); no second speaker is ever generated."""
    import requests
    _dia_start(log)
    parts = _dia_parts(text)
    tmp = Path(tempfile.mkdtemp(prefix="dia_"))
    try:
        files, words, t = [], [], 0.0
        sil = tmp / "sil.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.15", "-q:a", "5", str(sil)], check=True)
        for i, s in enumerate(parts):
            log(f"  voice {i + 1}/{len(parts)} ({len(s.split())} words, one take)")
            r = requests.post(DIA_URL + "/speak", json={"text": _spoken(s)}, timeout=3600)
            if r.status_code != 200 or len(r.content) < 2000:
                raise RuntimeError(f"Dia answered {r.status_code}: {r.text[:120]}")
            log(f"    words match {r.headers.get('X-Match')}, voice match {r.headers.get('X-Voice')}")
            wav = tmp / f"r{i}.wav"
            wav.write_bytes(r.content)
            f = tmp / f"s{i}.mp3"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-q:a", "3", str(f)], check=True)
            d = _duration(f)
            toks = s.split()
            al = _align(toks, json.loads(r.headers.get("X-Words") or "[]"), d)
            if al is None:
                al = _spread(toks, 0, d)
            words += [(a + t, e + t, w) for a, e, w in al]
            files += [f, sil]
            t += d + 0.15
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in files[:-1]), encoding="utf-8")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "libmp3lame", "-q:a", "3", str(mp3)], check=True)
        return _duration(mp3), words
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


SPEED = 1.25  # every video is spoken at this tempo (1.0 = the voice's own speed)


def _speed_up(mp3, words):
    """Plays the voice SPEED times faster and moves every word time with it."""
    if SPEED == 1.0:
        return _duration(mp3), words
    tmp = Path(str(mp3) + ".fast.mp3")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-af", f"atempo={SPEED}", "-c:a", "libmp3lame", "-q:a", "3", str(tmp)], check=True)
    tmp.replace(mp3)
    return _duration(mp3), [(a / SPEED, b / SPEED, w) for a, b, w in words]


def speak(text, voice, mp3, log):
    """Returns (seconds, [(start, end, word_with_punctuation), ...], voice_used). Falls back to the Microsoft HD voice if Dia or Kokoro is not running."""
    engine, vid = parse_voice(voice)
    if engine == "dia":
        try:
            secs, words = _speak_dia(text, mp3, log)
            secs, words = _speed_up(mp3, words)
            return secs, words, "dia:ref"
        except Exception as e:
            log(f"  (Dia is not available: {str(e)[:120]}. Using the HD fallback voice.)")
            engine, vid = parse_voice(FALLBACK_VOICE)
    if engine == "kokoro":
        try:
            if KOKORO_PY.exists():
                try:
                    secs, words = _speak_kokoro_local(text, vid, mp3, log)
                    secs, words = _speed_up(mp3, words)
                    return secs, words, f"kokoro:{vid}"
                except Exception as e:
                    log(f"  ({str(e)[:140]}; trying the Kokoro server instead)")
            secs, words = _speak_kokoro(text, vid, mp3, log)
            secs, words = _speed_up(mp3, words)
            return secs, words, f"kokoro:{vid}"
        except Exception as e:
            log(f"  (Kokoro is not available: {str(e)[:80]}. Start it with: cd ~/instagram-video-generator && docker compose up -d. Using the HD fallback voice.)")
            engine, vid = parse_voice(FALLBACK_VOICE)
    secs, words = _speak_edge(text, vid, mp3)
    secs, words = _speed_up(mp3, words)
    return secs, words, f"edge:{vid}"


def qa(res):
    """Automatic check of a finished video, so it can be approved without watching: what is spoken matches the script (transcribed back), the ending is there,
    the length is sane, and the sign up link comes on screen before the end. Returns {"ok": bool, "match": float, "reason": str}."""
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return {"ok": False, "match": 0, "reason": "no transcriber installed, so it needs a human check"}
    tmp = Path(tempfile.mkdtemp(prefix="qa_"))
    try:
        wav = tmp / "a.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(res["file"]), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
        segs, _ = WhisperModel("base.en", device="cpu", compute_type="int8").transcribe(str(wav), language="en")
        heard = " ".join(x.text for x in segs)
        norm = lambda t: re.sub(r"[^a-z0-9' ]", " ", _spoken(t).lower().replace("%", " percent")).split()
        want, said = norm(res.get("script", "")), norm(heard)
        match = difflib.SequenceMatcher(None, want, said).ratio()
        secs, link_from = res.get("seconds") or 0, res.get("link_from")
        why = []
        if match < 0.93:
            why.append(f"spoken words match the script only {match:.0%}")
        if want[-3:] != said[-3:] and want[-2:] != said[-3:-1]:
            why.append("the ending is missing")
        if not 12 <= secs <= 120:
            why.append(f"odd length ({secs}s)")
        if link_from is None or not 0 < link_from < secs - 1:
            why.append("the sign up link is not on screen before the end")
        return {"ok": not why, "match": round(match, 3), "reason": "; ".join(why)}
    except Exception as e:
        return {"ok": False, "match": 0, "reason": "check failed: " + str(e)[:100]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def caption_chunks(words):
    """Short caption lines that follow the speech: [(start, end, text)]."""
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if (len(cur) >= 3 and re.search(r"[,.?!]$", w[2])) or len(cur) >= 6:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    out = []
    for i, c in enumerate(chunks):
        s, e = c[0][0], c[-1][1]
        nxt = chunks[i + 1][0][0] if i + 1 < len(chunks) else e + 0.15
        out.append((max(0.0, s - 0.05), min(nxt - 0.02, e + 0.12), " ".join(x[2] for x in c)))
    return out


def link_start(words, seconds):
    """When the sign up link goes on screen: only at the call to action ("If you want to take a look at our website"), which is spoken after the pitch,
    so nobody sees a sign up link before they have heard it. If the wording is not found, it waits for the last sentences."""
    toks = [norm(w[2]) for w in words]
    for i in range(len(toks) - 2):
        if toks[i] == "take" and toks[i + 1] == "a" and toks[i + 2] == "look" and i >= 4 and toks[i - 2] == "want":
            return words[i - 4][0]  # the "If" of "If you want to take a look"
    return max(0.0, seconds - 10)


# ------------------------------------------------------------------------------------------------ the recording

STRIP_JS = r"""()=>{
 const URLRX=/(https?:\/\/\S+|www\.\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|co|io|shop|store|us|ca|uk|au|ai|app|info|biz|tv|me|xyz|online|site)\b(?:\/\S*)?)/gi;
 const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);const nodes=[];while(w.nextNode())nodes.push(w.currentNode);
 for(const n of nodes){const t=n.nodeValue;if(!t||!t.trim())continue;const el=n.parentElement;if(!el||el.closest('#__cta')||el.closest('#__cap'))continue;const s=t.trim();URLRX.lastIndex=0;
  if(!URLRX.test(t))continue;URLRX.lastIndex=0;
  if(s.length<70&&/^[A-Za-z0-9.\-\/:_?=&%]+$/.test(s)){el.style.visibility='hidden'}else{n.nodeValue=t.replace(URLRX,'')}}
 document.querySelectorAll('a,[role=link]').forEach(a=>{if(a.closest('#__cta')||a.closest('#__cap'))return;const s=(a.innerText||'').trim();if(a.closest('[role=dialog]')||['Ad Library','Ad Library Report','Ad Library API','Branded Content','About','About ads and data use','Privacy','Terms','Cookies'].includes(s))a.style.visibility='hidden'});
}"""
FX_JS = """(link)=>{if(document.getElementById('__cta'))return;
 const d=document.createElement('div');d.id='__cta';
 d.style.cssText='position:fixed;left:50%;bottom:34px;transform:translateX(-50%);background:#0f172a;color:#fff;padding:16px 34px;border-radius:16px;border:3px solid #38bdf8;z-index:2147483646;text-align:center;box-shadow:0 8px 30px rgba(0,0,0,.45);display:none;font-family:Arial,Helvetica,sans-serif';
 const a=document.createElement('div');a.textContent='Sign up in 2 minutes';a.style.cssText='font:600 20px Arial;color:#7dd3fc';
 const b=document.createElement('div');b.textContent=link;b.style.cssText='font:700 33px Arial;margin-top:6px;letter-spacing:.2px';d.appendChild(a);d.appendChild(b);document.body.appendChild(d);
 const c=document.createElement('div');c.id='__cap';
 c.style.cssText='position:fixed;left:50%;bottom:48px;transform:translateX(-50%);max-width:1040px;background:rgba(0,0,0,.74);color:#fff;font:700 36px Arial,Helvetica,sans-serif;line-height:1.2;padding:10px 24px;border-radius:12px;text-align:center;z-index:2147483647;display:none;text-shadow:0 2px 4px rgba(0,0,0,.6)';
 document.body.appendChild(c);
 window.__fx=(cap,cta)=>{const cc=document.getElementById('__cap');cc.textContent=cap||'';cc.style.display=cap?'block':'none';cc.style.bottom=cta?'170px':'48px';document.getElementById('__cta').style.display=cta?'block':'none'}}"""


def make_path(n, fps, top_limit, seed):
    """Where the page is scrolled to on each frame: it wanders up and down near the top, pauses, drifts, with a tiny tremor. No steady scroll."""
    rnd = random.Random(seed)
    total = n / fps
    way, t = [(0.0, 0.0)], 0.0
    while t < total + 3:
        hold, dur = rnd.uniform(0.35, 1.1), rnd.uniform(1.3, 2.7)
        r = rnd.random()
        tgt = rnd.uniform(0, 380) if r < 0.42 else rnd.uniform(380, 950) if r < 0.85 else rnd.uniform(950, max(1000, top_limit))
        t += dur
        way.append((t, tgt))
        t += hold
        way.append((t, tgt))
    ys, j = [], 0
    for i in range(n):
        tt = i / fps
        while j < len(way) - 2 and way[j + 1][0] < tt:
            j += 1
        (t0, y0), (t1, y1) = way[j], way[j + 1]
        f = 0 if t1 <= t0 else min(1.0, max(0.0, (tt - t0) / (t1 - t0)))
        e = f * f * (3 - 2 * f)
        ys.append(max(0.0, min(float(top_limit), y0 + (y1 - y0) * e + math.sin(tt * 7.3) * 2.0)))
    return ys


def open_ad(pg, k):
    """Clicks into one ad ('See ad details'), the way a person would. True when the details opened."""
    try:
        btns = pg.get_by_text(re.compile(r"^See (ad|summary) details$"))
        cnt = btns.count()
        if cnt == 0:
            return False
        el = btns.nth((k * 2 + 1) % min(cnt, 8))
        el.scroll_into_view_if_needed(timeout=2500)
        pg.wait_for_timeout(300)
        el.click(timeout=3000)
        pg.wait_for_timeout(1100)
        if pg.locator('[role="dialog"]').count() == 0:
            return False
        pg.evaluate(STRIP_JS)
        return True
    except Exception:
        return False


def close_ad(pg):
    try:
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(500)
        if pg.locator('[role="dialog"]').count() > 0:
            pg.locator('[aria-label="Close"]').first.click(timeout=2000)
            pg.wait_for_timeout(500)
    except Exception:
        pass


FACE = HERE / "assets" / "narrator.jpg"
BUBBLE = 210  # diameter of the talking circle, in pixels of the 1280x720 frame


def voice_levels(mp3, n, fps):
    """How loud the voice is in each of the n frames, 0 to 1 (smoothed), so the rings can pulse with the words."""
    import array
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(mp3), "-f", "s16le", "-ac", "1", "-ar", "8000", "-"], capture_output=True).stdout
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    win = 8000 // fps
    rms = []
    for i in range(n):
        seg = a[i * win:(i + 1) * win]
        rms.append(math.sqrt(sum(x * x for x in seg) / len(seg)) if len(seg) else 0.0)
    top = sorted(rms)[int(len(rms) * 0.95)] or 1.0
    out, prev = [], 0.0
    for r in rms:
        v = min(1.0, r / top)
        prev = prev * 0.45 + v * 0.55 if v > prev else prev * 0.7 + v * 0.3  # quick to rise, slower to fall
        out.append(prev)
    return out


def make_bubble(level, t):
    """The circular face with a white edge and soft rings that swell and fade with the voice. Returns an RGBA image."""
    from PIL import Image, ImageDraw
    S, K = 340, 3  # canvas size, supersampling for smooth edges
    big = Image.new("RGBA", (S * K, S * K), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    c, r0 = S * K / 2, BUBBLE * K / 2
    for j in range(3):  # rings drift outward and fade, strength follows the voice
        ph = (t * 0.9 + j / 3) % 1.0
        rad = r0 + (8 + ph * 48 * (0.35 + level)) * K
        al = int(150 * (1 - ph) * (0.15 + level))
        if al > 4:
            d.ellipse([c - rad, c - rad, c + rad, c + rad], outline=(110, 90, 255, al), width=3 * K)
    glow = r0 + (4 + 7 * level) * K
    d.ellipse([c - glow, c - glow, c + glow, c + glow], outline=(110, 90, 255, int(120 + 120 * level)), width=5 * K)
    big = big.resize((S, S), Image.LANCZOS)
    face = Image.open(FACE).convert("RGB").resize((BUBBLE, BUBBLE), Image.LANCZOS)
    mask = Image.new("L", (BUBBLE * K, BUBBLE * K), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, BUBBLE * K - 1, BUBBLE * K - 1], fill=255)
    mask = mask.resize((BUBBLE, BUBBLE), Image.LANCZOS)
    big.paste(face, ((S - BUBBLE) // 2, (S - BUBBLE) // 2), mask)
    return big


def add_bubble(jpg, level, t):
    from PIL import Image
    im = Image.open(jpg).convert("RGB")
    b = make_bubble(level, t)
    im.paste(b, (14, VIEWPORT["height"] - b.height - 6), b)
    im.save(jpg, quality=80)


def record(pg, pid, seconds, mp3, out_mp4, log, words, link=SIGNUP_LINK, show_from=None, seed=None):
    """Browses the active view for `seconds`, wandering near the top and clicking into a few ads, with captions that follow the words,
    and stitches the frames with the narration. No loading screens, no cursor, no face."""
    pg.goto(view_url(pid, "active"), wait_until="domcontentloaded")
    settle(pg, log)
    for _ in range(5):  # load the cards before the take
        pg.mouse.wheel(0, 1000)
        pg.wait_for_timeout(900)
    pg.evaluate(STRIP_JS)
    pg.evaluate(FX_JS, link)
    pg.evaluate("window.scrollTo(0,0)")
    pg.wait_for_timeout(800)
    height = pg.evaluate("document.documentElement.scrollHeight")
    top_limit = max(300, min(height - VIEWPORT["height"], 1500))
    n = max(30, int((seconds + 0.8) * FPS))
    path = make_path(n, FPS, top_limit, seed if seed is not None else random.randrange(10 ** 6))
    chunks = caption_chunks(words)
    levels = voice_levels(mp3, n, FPS) if FACE.exists() else None
    events = [seconds * f for f in (0.20, 0.47, 0.72)] if seconds >= 22 else [seconds * 0.45]
    events = [e for e in events if not (show_from is not None and e > show_from + 12)] or events[:1]
    ev, in_ad, ad_until, clicks = 0, False, 0.0, 0
    tmp = Path(tempfile.mkdtemp(prefix="advid_"))
    try:
        for i in range(n):
            t = i / FPS
            if not in_ad and ev < len(events) and t >= events[ev]:
                ev += 1
                if open_ad(pg, ev):
                    in_ad, ad_until, clicks = True, t + random.uniform(2.4, 3.4), clicks + 1
            elif in_ad and t >= ad_until:
                close_ad(pg)
                pg.evaluate(STRIP_JS)
                in_ad = False
            cap = next((c[2] for c in chunks if c[0] <= t <= c[1]), "")
            cta = show_from is not None and t >= show_from
            if in_ad:
                pg.evaluate("([a,b])=>window.__fx(a,b)", [cap, cta])
            else:
                pg.evaluate("([y,a,b])=>{window.scrollTo(0,y);window.__fx(a,b)}", [int(path[i]), cap, cta])
            pg.wait_for_timeout(25)
            pg.screenshot(path=str(tmp / f"f{i:04d}.jpg"), type="jpeg", quality=72)
            if levels:
                add_bubble(tmp / f"f{i:04d}.jpg", levels[i], t)
            if i % 30 == 0:
                pg.evaluate(STRIP_JS)
        if in_ad:
            close_ad(pg)
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(tmp / "f%04d.jpg"), "-i", str(mp3), "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", "-r", "30", "-vf", "scale=1280:720", "-c:a", "aac", "-shortest", str(out_mp4)], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + r.stderr[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return clicks


def make(item, voice=DEFAULT_VOICE, sender="Emmanuel", product="AI Ads Optimization", log=print, link=SIGNUP_LINK, host="loom", narrator=None):
    if link != SIGNUP_LINK:  # the on-screen link is ALWAYS the sign up page, whatever a caller passes (never a Drive or any other link)
        log(f"  (ignoring link {str(link)[:60]}; the video always shows {SIGNUP_LINK})")
        link = SIGNUP_LINK
    """Returns {'ok': bool, 'reason': str, 'file': path, 'facts': {...}, 'script': str}.
    narrator: the name said in the video. None = the sender's own name for a male voice and no name for a female voice. '' = no name."""
    from playwright.sync_api import sync_playwright
    OUT.mkdir(exist_ok=True)
    COUNTRY[0] = "US"
    company = item.get("company") or ""
    domain = brand_domain(item.get("email"), item.get("web"))
    label = root_label(domain)
    if not label:
        return {"ok": False, "reason": "no domain to check the brand against"}
    first = (item.get("first") or "").strip() or "there"
    business = (item.get("business") or company or label).strip()
    if narrator is None:
        narrator = sender if voice_gender(voice) == "m" else ""
    log(f"[{company}] brand domain {domain}; searching the Ad Library")
    with sync_playwright() as p:
        ctx, closer = open_context(p)
        try:
            pg = ctx.new_page()
            chosen, rejected, any_cands, seen = None, [], False, set()
            for country in ("US", "ALL"):  # the US Ad Library first; all countries when nothing there is this brand, for country domains only (a UK or Canadian shop)
                if country == "ALL" and not foreign_domain(domain):
                    continue
                COUNTRY[0] = country
                if country == "ALL":
                    log("  nothing matching in the US ads; looking in the all-countries Ad Library")
                cands = candidates(pg, domain, company, log)
                any_cands = any_cands or bool(cands)
                if country == "ALL":  # only pages that are named like this brand: the all-countries keyword search is full of unrelated pages
                    cands = [(i, nm) for i, nm in cands if i not in seen and (name_close(nm, company) or name_close(nm, label))]
                for pid, pname in cands[:3]:
                    seen.add(pid)
                    facts = inspect(pg, pid, label, log)
                    facts["page_name"] = pname
                    good = facts.get("ok") and facts["host_match_rate"] >= 0.5 and facts["matching_hosts"]
                    log(f"  candidate {pname} ({pid}): " + (f"{len(facts['matching_hosts'])} brand domain(s), match rate {facts['host_match_rate']}, {facts['active']} active of {facts['total']}" if facts.get("ok") else facts.get("reason", "")))
                    if good:
                        chosen = facts
                        break
                    if f"{pname}: ads point" not in " ".join(rejected):
                        rejected.append(f"{pname}: ads point to {', '.join(facts.get('hosts', [])[:3]) or 'no domain'}")
                if chosen:
                    break
            if not any_cands:
                if not lookup_healthy(pg, log):
                    COUNTRY[0] = "US"
                    return {"ok": False, "transient": True, "reason": "the Ad Library did not answer properly (it may be limiting requests), so nothing was decided about this lead"}
                COUNTRY[0] = "US"
                return {"ok": False, "reason": f"no advertiser found in the Ad Library for {domain} or {company}"}
            if not chosen:
                COUNTRY[0] = "US"
                return {"ok": False, "reason": "brand check failed: " + "; ".join(rejected)[:300]}
            if chosen["active"] < 1:
                return {"ok": False, "reason": f"{chosen['page_name']} has no active ads right now ({chosen['total']} in the library), so there is nothing true to show", "facts": chosen}
            display = business if name_close(business, chosen["page_name"]) else chosen["page_name"]
            script = build_script(chosen, first, display, narrator, product, host=host)
            vid = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("id") or label))[:40] or label
            mp3, mp4 = OUT / f"{vid}.mp3", OUT / f"{vid}.mp4"
            secs, words, used = speak(script, voice, mp3, log)
            start = link_start(words, secs)
            log(f"  voice {used}, {secs:.0f}s; the sign up link is on screen from {start:.0f}s; recording")
            clicks = record(pg, chosen["page_id"], secs, mp3, mp4, log, words, link=link, show_from=start)
            (OUT / f"{vid}.json").write_text(json.dumps({"item": {k: item.get(k) for k in ("id", "company", "email")}, "facts": chosen, "script": script, "voice": used, "narrator": narrator,
                                                          "host": host, "link": link, "link_from": round(start, 1), "ad_clicks": clicks}, indent=1), encoding="utf-8")
            try:
                mp3.unlink()
            except OSError:
                pass
            return {"ok": True, "display_name": display, "file": str(mp4), "facts": chosen, "script": script, "seconds": round(secs), "link_from": round(start, 1), "voice": used, "ad_clicks": clicks}
        finally:
            closer()


if __name__ == "__main__":
    it = json.loads(sys.argv[1])
    print(json.dumps(make(it, log=lambda m: print(m, file=sys.stderr)), indent=1))
