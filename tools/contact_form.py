#!/usr/bin/env python3
"""
Finds a website's contact form and (only when told to) sends a message through it. Used to put the Loom video in front of a lead whose email bounced or never arrived.

Cheapest first:
  1. plain HTTP: the home page's own "contact / get in touch / reach us" links plus the usual paths (/contact, /pages/contact, ...), read as HTML. No browser, no AI.
  2. only when the page has no readable form (a form built by JavaScript, a page builder, an iframe): a headless Chrome page is loaded and its forms are read.
A form is usable when it has a message box and an email field and does not look like a newsletter, login or search form.

It NEVER solves or gets around a CAPTCHA, never ticks a consent checkbox and never guesses a required field it cannot fill: those forms come back as
needs_human, with the form's address, so the message can be pasted by hand. It sends nothing unless submit() is called with confirm=True.

    python3 tools/contact_form.py find example.com
    python3 tools/contact_form.py find http://localhost:8123        # a full address works too (tests)
"""
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

HERE = Path(__file__).resolve().parent
DEBUG = HERE / "contact_form_debug"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com", "live.com", "msn.com", "me.com", "protonmail.com", "ymail.com"}
LINK_WORDS = re.compile(r"contact|get[\s-]*in[\s-]*touch|reach[\s-]*(us|out)|write[\s-]*(to[\s-]*)?us|e?nquir|talk[\s-]*to[\s-]*us|customer[\s-]*(service|care)|support|help[\s-]*(center|desk)?", re.I)
GUESS = ["/contact", "/contact-us", "/pages/contact", "/pages/contact-us", "/contact/", "/contact-us/", "/pages/contact-us/", "/pages/customer-service", "/support", "/about/contact"]
EMBEDS = re.compile(r"typeform\.com|hsforms\.|hubspot|jotform|wufoo|formstack|forms\.gle|docs\.google\.com/forms|airtable\.com|tally\.so|calendly|gorgias|zendesk|freshdesk", re.I)
BAD_ACTION = re.compile(r"/cart|/checkout|login|signin|sign-in|/search|subscribe|newsletter|/account|/customer|register|password", re.I)
MSG_WORDS = re.compile(r"message|comment|enquiry|inquiry|how can we|details|question|your note|tell us|write", re.I)


# ------------------------------------------------------------------------------------------------ reading HTML
class Page(HTMLParser):
    """Links, forms (with their fields) and embedded form iframes of one HTML page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.forms, self.iframes, self.labels = [], [], [], {}
        self._form, self._a, self._lab = None, None, None
        self.recaptcha_v3 = False

    def handle_starttag(self, tag, attrs):
        a = dict((k, (v or "")) for k, v in attrs)
        if tag == "a" and a.get("href"):
            self._a = {"href": a["href"], "text": "", "title": a.get("title", "") + " " + a.get("aria-label", "")}
        elif tag == "form":
            self._form = {"action": a.get("action", ""), "id": a.get("id", ""), "cls": a.get("class", ""), "fields": [], "cap": ""}
            self.forms.append(self._form)
        elif tag == "label":
            self._lab = a.get("for", "")
        elif tag in ("input", "textarea", "select") and self._form is not None:
            self._form["fields"].append({"tag": tag, "type": (a.get("type") or ("text" if tag == "input" else tag)).lower(), "name": a.get("name", ""), "id": a.get("id", ""),
                                         "ph": a.get("placeholder", ""), "aria": a.get("aria-label", ""), "label": "", "req": "required" in a or a.get("aria-required") == "true",
                                         "value": a.get("value", "")})
        elif tag == "iframe" and a.get("src"):
            self.iframes.append(a["src"])
        elif tag == "script" and "recaptcha/api.js" in a.get("src", "") and "render=" in a.get("src", "") and "render=explicit" not in a.get("src", ""):
            self.recaptcha_v3 = True
        if self._form is not None:
            cls = (a.get("class", "") + " " + a.get("data-sitekey", "") + " " + a.get("src", "")).lower()
            if re.search(r"g-recaptcha|h-captcha|cf-turnstile|friendlycaptcha", cls) or (tag == "iframe" and re.search(r"recaptcha|hcaptcha|turnstile", cls)):
                self._form["cap"] = "invisible" if a.get("data-size") == "invisible" else "visible"

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            self.links.append(self._a)
            self._a = None
        elif tag == "form":
            self._form = None
        elif tag == "label":
            self._lab = None

    def handle_data(self, data):
        if self._a is not None:
            self._a["text"] += data
        if self._lab:
            self.labels[self._lab] = (self.labels.get(self._lab, "") + " " + data).strip()


def fetch(url, timeout=12):
    import requests
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=timeout, allow_redirects=True)
        if r.status_code == 200 and "html" in r.headers.get("content-type", "html").lower():
            return r.url, r.text
    except Exception:
        pass
    return None, ""


def field_kind(f):
    """What a form field is for: email, phone, message, name, first, last, subject, company, website, checkbox, select, radio, file or '' (hidden / buttons)."""
    t, tag = f.get("type", ""), f.get("tag", "")
    if t in ("hidden", "submit", "button", "image", "reset"):
        return ""
    txt = " ".join(str(f.get(k, "")) for k in ("name", "id", "ph", "aria", "label")).lower()
    if t == "checkbox":
        return "checkbox"
    if t == "radio":
        return "radio"
    if t == "file":
        return "file"
    if tag == "select":
        return "select"
    if t == "email" or re.search(r"e-?mail", txt):
        return "email"
    if t == "tel" or re.search(r"phone|mobile|\btel\b|telephone", txt):
        return "phone"
    if tag == "textarea" or MSG_WORDS.search(txt) and t in ("text", ""):
        return "message"
    if re.search(r"first[\s_-]*name|fname|given", txt):
        return "first"
    if re.search(r"last[\s_-]*name|lname|surname|family", txt):
        return "last"
    if re.search(r"subject|topic|reason", txt):
        return "subject"
    if re.search(r"company|business|organi[sz]ation|brand|store", txt):
        return "company"
    if re.search(r"website|\burl\b|\bsite\b", txt):
        return "website"
    if re.search(r"name", txt):
        return "name"
    return "text"


def score_form(form):
    """How much this looks like a 'write to us' form. Usable at 6 or more and with a message box."""
    kinds = [field_kind(f) for f in form.get("fields", [])]
    act = form.get("action", "") + " " + form.get("id", "") + " " + form.get("cls", "")
    s = 0
    s += 5 if "message" in kinds else 0
    s += 2 if "email" in kinds else 0
    s += 1 if ("name" in kinds or "first" in kinds) else 0
    s += 1 if "phone" in kinds else 0
    if any(f.get("type") == "password" for f in form.get("fields", [])):
        s -= 10
    if any(f.get("type") == "search" or f.get("name") in ("q", "s", "search") for f in form.get("fields", [])):
        s -= 6
    if any("tags" in f.get("name", "") and f.get("value") == "newsletter" for f in form.get("fields", [])) or re.search(r"newsletter|subscribe|footer", act, re.I):
        s -= 6
    if BAD_ACTION.search(form.get("action", "")) and "contact" not in form.get("action", "").lower():
        s -= 6
    if re.search(r"contact|enquir|inquir", act, re.I):
        s += 2
    return s, ("message" in kinds and "email" in kinds)


def best_form(forms):
    ok = []
    for f in forms:
        s, usable = score_form(f)
        if usable and s >= 6:
            ok.append((s, f))
    return max(ok, key=lambda x: x[0])[1] if ok else None


def candidates_from(base, page):
    """Contact pages to look at: links on the home page that talk about contact, then the usual paths."""
    out = []
    host = urlparse(base).netloc.replace("www.", "")
    for lk in page.links:
        txt = (lk["text"] + " " + lk["title"]).strip()
        href = lk["href"]
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        full = urljoin(base, href).split("#")[0]
        if urlparse(full).netloc.replace("www.", "") != host:
            continue
        if re.search(r"contact", txt + " " + href, re.I):
            out.insert(0, full)
        elif LINK_WORDS.search(txt) and len(txt) < 40:
            out.append(full)
    for g in GUESS:
        out.append(urljoin(base, g))
    seen, res = set(), []
    for u in out:
        k = u.rstrip("/")
        if k not in seen:
            seen.add(k)
            res.append(u)
    return res[:6]


# ------------------------------------------------------------------------------------------------ the browser way
EXTRACT_JS = r"""() => {
  let n = 0;
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const info = e => ({tag: e.tagName.toLowerCase(), type: (e.type || '').toLowerCase(), name: e.name || '', id: e.id || '', ph: e.placeholder || '', aria: e.getAttribute('aria-label') || '',
     label: ((e.labels && e.labels[0]) ? e.labels[0].innerText : '') || '', req: !!e.required || e.getAttribute('aria-required') === 'true', value: e.value || '', vis: vis(e),
     cf: (e.setAttribute('data-cf-f', String(++n)), String(n))});
  const roots = new Map();
  document.querySelectorAll('form').forEach(f => roots.set(f, 'form'));
  document.querySelectorAll('textarea').forEach(t => { if (!t.closest('form')) { let p = t; for (let i = 0; i < 4 && p.parentElement; i++) p = p.parentElement; roots.set(p, 'virtual'); } });
  const out = [];
  let k = 0;
  roots.forEach((kind, r) => {
    if (!vis(r) && kind === 'virtual') return;
    r.setAttribute('data-cf-form', String(++k));
    const fields = [...r.querySelectorAll('input,textarea,select')].map(info);
    const cap = r.querySelector('.g-recaptcha,.h-captcha,.cf-turnstile,iframe[src*=recaptcha],iframe[src*=hcaptcha],iframe[src*=turnstile]');
    out.push({idx: String(k), kind, action: r.getAttribute('action') || '', id: r.id || '', cls: (r.className && r.className.toString()) || '', fields,
              cap: cap ? ((cap.getAttribute('data-size') === 'invisible') ? 'invisible' : 'visible') : '', vis: vis(r), buttons: [...r.querySelectorAll('button,input[type=submit]')].map(b => (b.innerText || b.value || '').trim())});
  });
  return out;
}"""


def browser_forms(pg):
    """Forms on the loaded page and its frames, with the elements stamped so they can be found again: [(frame, form)]."""
    res = []
    for fr in pg.frames:
        try:
            for f in fr.evaluate(EXTRACT_JS):
                res.append((fr, f))
        except Exception:
            continue
    return res


def _ctx(p, show=False):
    return p.chromium.launch(channel="chrome", headless=not show)


# ------------------------------------------------------------------------------------------------ find
def base_of(domain_or_url):
    d = str(domain_or_url or "").strip()
    if not d:
        return ""
    if d.startswith("http"):
        return d.rstrip("/")
    d = d.split("@")[-1].lower().strip().strip("/")
    return "https://" + d if d else ""


def find(domain_or_url, web="", log=print):
    """{found, url, via ('http'|'browser'), captcha ('' | 'invisible' | 'visible'), fields, embed, why}. Never submits anything."""
    base = base_of(domain_or_url)
    host = urlparse(base).netloc.lower().replace("www.", "")
    if (not base or host in FREE_MAIL) and web:
        base = base_of(web)
    if not base or urlparse(base).netloc.lower().replace("www.", "") in FREE_MAIL:
        return {"found": False, "why": "no website to look at (the email is a free mailbox)"}
    home_url, home_html = fetch(base)
    if not home_html and base.startswith("https://") and "localhost" not in base:
        home_url, home_html = fetch(base.replace("https://", "https://www.", 1)) if "://www." not in base else (None, "")
    if not home_html:
        return {"found": False, "why": "the website did not load"}
    home = Page()
    try:
        home.feed(home_html)
    except Exception:
        pass
    cands = candidates_from(home_url or base, home)
    pages = [(home_url or base, home)]
    embed = ""
    # 1. plain HTTP
    form = best_form(home.forms)
    if form:
        return _result(home_url or base, "http", form, home)
    for u in cands:
        url, html = fetch(u, timeout=10)
        if not html:
            continue
        pg = Page()
        try:
            pg.feed(html)
        except Exception:
            continue
        pages.append((url, pg))
        f = best_form(pg.forms)
        if f:
            return _result(url, "http", f, pg)
        for src in pg.iframes:
            if EMBEDS.search(src):
                embed = embed or src
        if len(pages) >= 4:
            break
    # 2. a browser, only when the plain pages show no form (JavaScript forms, page builders)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = _ctx(p)
            try:
                ctx = b.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA, locale="en-US")
                pg = ctx.new_page()
                tried = 0
                for u in [x[0] for x in pages[1:]] or cands[:3]:
                    if tried >= 3:
                        break
                    tried += 1
                    try:
                        pg.goto(u, wait_until="domcontentloaded", timeout=25000)
                        pg.wait_for_timeout(2500)
                    except Exception:
                        continue
                    forms = [f for _fr, f in browser_forms(pg) if f.get("vis", True)]
                    f = best_form(forms)
                    if f:
                        return _result(pg.url, "browser", f, None)
                    for fr in pg.frames[1:]:
                        if EMBEDS.search(fr.url or ""):
                            embed = embed or fr.url
            finally:
                b.close()
    except Exception as e:
        log("  (browser look failed: " + str(e).splitlines()[0][:100] + ")")
    if embed:
        return {"found": False, "embed": embed, "why": "the contact form is an embedded service (" + urlparse(embed).netloc + "), which needs a person"}
    return {"found": False, "why": "no contact form with a message box was found"}


def _result(url, via, form, page):
    kinds = [field_kind(f) for f in form["fields"]]
    cap = form.get("cap", "") or ("invisible" if (page is not None and page.recaptcha_v3) else "")
    needs = [k for f, k in zip(form["fields"], kinds) if f.get("req") and k in ("checkbox", "radio", "file", "select")]
    return {"found": True, "url": url, "via": via, "captcha": cap, "kinds": sorted({k for k in kinds if k}), "needs_human": cap == "visible" or bool(needs),
            "why": "a CAPTCHA" if cap == "visible" else ("a required " + needs[0] + " field") if needs else ""}


# ------------------------------------------------------------------------------------------------ submit
def _fill_value(kind, v):
    first = (v.get("name") or "").split(" ")[0]
    last = " ".join((v.get("name") or "").split(" ")[1:]) or "."
    return {"email": v.get("email", ""), "phone": v.get("phone", ""), "message": v.get("message", ""), "name": v.get("name", ""), "first": first, "last": last, "subject": v.get("subject", ""),
            "company": v.get("company", ""), "website": v.get("website", ""), "text": ""}.get(kind, "")


SUCCESS = re.compile(r"thank\s*you|thanks\b|message (has been |was )?(sent|received|submitted)|we('ll| will| have) (be in touch|get back|received)|successfully|submission (has been )?received|got your (message|inquiry)", re.I)
SOLVED_JS = """(root) => { const q = s => [...document.querySelectorAll(s)].some(e => (e.value || '').length > 20);
  return q('textarea[name=g-recaptcha-response]') || q('textarea[name=h-captcha-response]') || q('input[name=cf-turnstile-response]') || q('textarea[name=cf-turnstile-response]'); }"""


def _cookie_banner(pg):
    for sel in ("button:has-text('Accept all')", "button:has-text('Accept')", "button:has-text('Reject all')", "button:has-text('Got it')"):  # cookie banners only get in the way
        try:
            pg.locator(sel).first.click(timeout=700)
            return
        except Exception:
            pass


def _dismiss_popups(pg):
    """Discount / newsletter pop-ups cover the page and swallow clicks. Close them the way a visitor would."""
    try:
        pg.keyboard.press("Escape")
    except Exception:
        pass
    for sel in ("button:has-text('No thanks')", "a:has-text('No thanks')", "button:has-text('No, thanks')", "[aria-label='Close']:visible", "button[aria-label*='lose']:visible", "button:has-text('Maybe later')", "button:has-text('Dismiss')"):
        try:
            pg.locator(sel).first.click(timeout=600)
        except Exception:
            pass


def _page_text(pg):
    """All the text a visitor could see: the page, its frames, and hidden-but-present text."""
    out = []
    for fr in pg.frames:
        for js in ("document.body ? document.body.innerText : ''", "document.documentElement ? document.documentElement.textContent : ''"):
            try:
                out.append(fr.evaluate(js) or "")
            except Exception:
                pass
    return "\n".join(out)


def _form_closed(root):
    """After sending, most forms disappear or empty their message box."""
    try:
        if not root.is_visible():
            return True
        ta = root.locator("textarea").first
        return ta.count() > 0 and (ta.input_value() or "").strip() == ""
    except Exception:
        return True


def fill_form(pg, values):
    """Finds the contact form on the loaded page and fills it in WITHOUT sending it.
    Returns {ok, root, btn, form, captcha} or {ok: False, status, note} (status 'needs_human' or 'failed')."""
    found = [(fr, f) for fr, f in browser_forms(pg) if f.get("vis", True)]
    scored = []
    for fr, f in found:
        sc, usable = score_form(f)
        if usable and sc >= 6:
            scored.append((sc, fr, f))
    if not scored:
        return {"ok": False, "status": "failed", "note": "the form is not there any more"}
    _s, fr, form = max(scored, key=lambda x: x[0])
    root = fr.locator(f'[data-cf-form="{form["idx"]}"]').first
    filled = 0
    for f in form["fields"]:
        kind = field_kind(f)
        if not kind:
            continue
        if kind in ("checkbox", "radio", "file", "select"):
            if f.get("req"):
                return {"ok": False, "status": "needs_human", "note": f"the form needs a {kind} to be chosen"}
            continue
        val = _fill_value(kind, values)
        if not val:
            if f.get("req") and kind != "text":
                return {"ok": False, "status": "needs_human", "note": f"the form needs a {kind} that was not given"}
            if not f.get("req"):
                continue
            val = "n/a"
        try:
            root.locator(f'[data-cf-f="{f["cf"]}"]').first.fill(val, timeout=4000)
            filled += 1
        except Exception:
            if f.get("req"):
                return {"ok": False, "status": "needs_human", "note": f"could not fill the {kind} field"}
    if not filled:
        return {"ok": False, "status": "failed", "note": "nothing could be filled in"}
    btn = None
    for loc in (root.locator("button[type=submit], input[type=submit]"), root.locator("button")):
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                t = (el.inner_text() if el.evaluate("e=>e.tagName") == "BUTTON" else el.get_attribute("value")) or ""
            except Exception:
                continue
            if re.search(r"send|submit|contact|message|go|ask|request|get in touch|continue", t, re.I) and not re.search(r"search|subscribe|sign ?in|log ?in|cart", t, re.I):
                btn = el
                break
        if btn:
            break
    if btn is None:
        return {"ok": False, "status": "failed", "note": "no send button found"}
    return {"ok": True, "root": root, "btn": btn, "form": form, "captcha": form.get("cap", "")}


def _confirmed(pg, before):
    body = _page_text(pg)
    return bool(SUCCESS.search(body) or (re.search(r"thank|success|sent|confirm", pg.url, re.I) and pg.url != before))


def submit(url, values, confirm=False, show=False, log=print):
    """Fills the form at `url` with values {name,email,phone,subject,message,company,website} and presses its send button.
    Returns {status: 'sent' | 'unsure' | 'failed' | 'needs_human', note, shot}. Does nothing unless confirm is True."""
    if not confirm:
        return {"status": "failed", "note": "not confirmed"}
    from playwright.sync_api import sync_playwright
    DEBUG.mkdir(exist_ok=True)
    stamp = str(int(time.time()))
    with sync_playwright() as p:
        b = _ctx(p, show)
        try:
            pg = b.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA, locale="en-US").new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(3500)
            _cookie_banner(pg)
            _dismiss_popups(pg)
            r = fill_form(pg, values)
            if not r["ok"]:
                return {"status": r["status"], "note": r["note"]}
            if r["captcha"] == "visible":
                return {"status": "needs_human", "note": "the form has a CAPTCHA"}
            _dismiss_popups(pg)
            before = pg.url
            pg.screenshot(path=str(DEBUG / f"before_{stamp}.png"))
            try:
                r["btn"].click(timeout=6000, no_wait_after=True)   # no_wait_after: a slow server must not make the click look like a failure
            except Exception:
                if pg.url == before and r["root"].count() > 0:   # nothing was sent: a pop-up is probably over the button. Close it and press once more
                    _dismiss_popups(pg)
                    r["btn"].click(timeout=6000, force=True, no_wait_after=True)
            shot = f"after_{stamp}.png"
            end, closed_since = time.time() + 32, None
            while time.time() < end:
                pg.wait_for_timeout(800)
                if _confirmed(pg, before):
                    pg.screenshot(path=str(DEBUG / shot))
                    return {"status": "sent", "note": "the page confirmed the message was sent", "shot": shot}
                try:
                    if pg.locator("iframe[src*=bframe], iframe[src*=hcaptcha]").first.is_visible():
                        pg.screenshot(path=str(DEBUG / shot))
                        return {"status": "needs_human", "note": "a CAPTCHA appeared after pressing send", "shot": shot}
                except Exception:
                    pass
                if _form_closed(r["root"]):
                    closed_since = closed_since or time.time()
                    if time.time() - closed_since > 3:   # the form went away and stayed away: that is what a sent form does
                        pg.screenshot(path=str(DEBUG / shot))
                        return {"status": "sent", "note": "the form closed after sending", "shot": shot}
                else:
                    closed_since = None
            shot = f"unsure_{stamp}.png"
            pg.screenshot(path=str(DEBUG / shot))
            try:
                invalid = pg.locator("[aria-invalid=true], .error, .wpcf7-not-valid-tip, .field_error").count()
            except Exception:
                invalid = 0
            return {"status": "failed" if invalid else "unsure", "note": "the form showed errors" if invalid else "pressed send but no confirmation appeared", "shot": shot}
        finally:
            b.close()


def captcha_batch(items, values, wait_minutes=10, on_result=print, stop=lambda: False, _hook=None, _headless=False):
    """For forms that have a CAPTCHA. Opens a VISIBLE Chrome window with one tab per form, every form already filled in. YOU solve the CAPTCHA in each tab; the moment it is
    solved the send button is pressed, the result is reported and the next tab comes to the front. A tab you do not get to within `wait_minutes` (counted from when it opened)
    is closed and reported as 'later', so the lead stays saved for another go. The CAPTCHA itself is never touched by this code.
    items: [{id, url, message}], values: {name, email, phone, subject, company, website}. on_result gets {id, status, note}."""
    from playwright.sync_api import sync_playwright
    if not items:
        return
    DEBUG.mkdir(exist_ok=True)
    with sync_playwright() as p:
        b = _ctx(p, not _headless)
        try:
            ctx = b.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA, locale="en-US")
            tabs = []  # {id, pg, t0, before, btn, pressed}
            for it in items:
                if stop():
                    break
                pg = ctx.new_page()
                try:
                    pg.goto(it["url"], wait_until="domcontentloaded", timeout=30000)
                    pg.wait_for_timeout(2500)
                    _cookie_banner(pg)
                    _dismiss_popups(pg)
                    r = fill_form(pg, {**values, "message": it.get("message", "")})
                except Exception as e:
                    r = {"ok": False, "status": "failed", "note": str(e).splitlines()[0][:100]}
                if not r["ok"]:
                    on_result({"id": it["id"], "status": r["status"], "note": r["note"]})
                    pg.close()
                    continue
                if _hook:
                    _hook(pg, it)
                tabs.append({"id": it["id"], "pg": pg, "t0": time.time(), "before": pg.url, "btn": r["btn"], "pressed": False})
            if tabs:
                try:
                    tabs[0]["pg"].bring_to_front()
                except Exception:
                    pass
            while tabs and not stop():
                time.sleep(1.2)
                for t in list(tabs):
                    pg = t["pg"]
                    done = None
                    try:
                        if pg.is_closed():
                            done = {"status": "later", "note": "the tab was closed before it was sent"}
                        elif t["pressed"]:
                            if _confirmed(pg, t["before"]):
                                done = {"status": "sent", "note": "the page confirmed the message was sent"}
                            elif time.time() - t["pressed_at"] > 25:
                                done = {"status": "unsure", "note": "send was pressed but no confirmation appeared"}
                        elif pg.evaluate(SOLVED_JS, None):
                            t["btn"].click(timeout=5000, no_wait_after=True)   # the CAPTCHA was solved by you: press the form's own send button
                            t["pressed"], t["pressed_at"] = True, time.time()
                        elif time.time() - t["t0"] > wait_minutes * 60:
                            done = {"status": "later", "note": f"not solved within {wait_minutes} minutes, saved for later"}
                    except Exception as e:
                        if "closed" in str(e).lower():
                            done = {"status": "later", "note": "the tab was closed before it was sent"}
                    if done:
                        on_result({"id": t["id"], **done})
                        try:
                            if not pg.is_closed():
                                pg.close()
                        except Exception:
                            pass
                        tabs.remove(t)
                        if tabs:
                            try:
                                tabs[0]["pg"].bring_to_front()
                            except Exception:
                                pass
            for t in tabs:  # stopped
                on_result({"id": t["id"], "status": "later", "note": "stopped"})
        finally:
            b.close()


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["find"] and len(a) > 1:
        print(json.dumps(find(a[1], log=lambda m: print(m, file=sys.stderr)), indent=1))
    else:
        print(__doc__)
