#!/usr/bin/env python3
"""
Stand-ins for everything the outreach loop touches (tools/outreach_loop.py `ports`), so the whole loop can be exercised without a single real
email, contact-form message, Loom comment, HALCRUS login or Meta request. Used by tools/test_outreach_loop.py and by the helper when started with
LOOP_STUBS=1 (the page can then be tried against a helper that cannot do any harm).

Behaviour is steered through a small JSON file (STUB_CONTROL, default stub_control.json in CRM_DATA_DIR) so a test can change it while the helper runs:
  {"ad_blocked": false, "loom_connected": true, "halcrus_connected": true, "in_send_window": true, "form_captcha": ["captcha-co"], "no_form": ["noform-co"],
   "no_ads": ["noads-co"], "bad_email": ["bad-co"], "halcrus_pool": 40, "delay": 0}
Companies are matched by substring of the company name; everything else behaves like a healthy lead with Meta ads and a plain contact form.
Every "send" is only recorded in the stub's own log (STUB_LOG, a JSONL file) so tests can assert on what WOULD have gone out.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

import crm_store

HERE = Path(__file__).resolve().parent
CONTROL = Path(os.getenv("STUB_CONTROL") or crm_store.DATA / "stub_control.json")
SLOG = Path(os.getenv("STUB_LOG") or crm_store.DATA / "stub_log.jsonl")


def control():
    base = {"ad_blocked": False, "loom_connected": True, "halcrus_connected": True, "in_send_window": True, "form_captcha": ["captcha"], "no_form": ["noform"], "no_ads": ["noads"], "bad_email": ["badmail"],
            "halcrus_pool": 40, "delay": 0, "inbox_cap": 10}
    try:
        base.update(json.loads(CONTROL.read_text(encoding="utf-8")))
    except Exception:
        pass
    return base


def record(kind, **kw):
    with open(SLOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "kind": kind, **kw}) + "\n")


def slog_rows(kind):
    try:
        return [r for r in (json.loads(x) for x in SLOG.read_text().splitlines()) if r["kind"] == kind]
    except Exception:
        return []


def has(names, company):
    c = str(company or "").lower()
    return any(n.lower() in c for n in names)


class StubPorts:
    def __init__(self):
        self.queue = []
        self.notes = []
        self.uploaded = {}
        self.next_halcrus = 1
        self.lock = threading.Lock()

    # --- instant
    def email_status(self):
        c = control()
        return {"ready": True, "problems": [], "paused": {}, "sender_name": "Emmanuel", "phone": "+1 555 0100", "in_send_window": c["in_send_window"], "window": {"start_hour": 9, "end_hour": 17},
                "inboxes": [{"email": "a@example.test", "sent_today": 0, "cap": c["inbox_cap"], "paused": False}], "capacity_left": c["inbox_cap"], "suppressed": 0, "suppressed_list": []}

    def greet(self, items):
        out = {}
        for it in items:
            p = (it.get("person") or "").split(" ")[0]
            out[it["id"]] = {"first": p or it.get("company"), "kind": "person" if p else "business", "business": it.get("company")}
        return out

    def preview(self, leads, template, limit):
        c = control()
        return [{"id": l["id"], "status": "skip" if has(c["bad_email"], l.get("name")) else "ready", "why": "stub: not emailable"} for l in leads]

    def loom_connected(self):
        return control()["loom_connected"]

    def loom_uploads(self):
        return dict(self.uploaded)

    def halcrus_connected(self):
        return control()["halcrus_connected"]

    def queue_list(self):
        c = control()
        return {"items": [dict(i) for i in self.queue], "now": time.time(), "in_send_window": c["in_send_window"], "window": {"start_hour": 9, "end_hour": 17}, "capacity_left": c["inbox_cap"], "ready": True}

    def queue_add(self, entries):
        out = []
        for e in entries:
            key = str(e["id"])
            if any(i["id"] == key for i in self.queue):
                out.append({"id": key, "status": "scheduled", "why": "already in the queue"})
                continue
            it = {"id": key, "to": e.get("to"), "company": e.get("company"), "status": "scheduled", "due_at": time.time() + 60 + 30 * len(self.queue), "step": e.get("step", 0), "history": []}
            self.queue.append(it)
            record("queue_add", id=key, to=e.get("to"), step=e.get("step", 0))
            out.append({"id": key, "status": "scheduled", "due_at": it["due_at"]})
        return out

    def queue_cancel(self, id_):
        for i in self.queue:
            if i["id"] == str(id_) and i["status"] == "scheduled":
                i["status"] = "canceled"
                record("queue_cancel", id=id_)
                return True
        return False

    def cform_replies(self, items):
        return []

    def notify(self, title, message, key, every):
        self.notes.append({"title": title, "message": message, "key": key})
        record("notify", title=title, message=message, key=key)

    def ads_health(self):
        return not control()["ad_blocked"]

    # --- jobs: the stub returns the output lines the real job would have printed
    def run_job(self, label, fn, args, on_line, wait, should_abort):
        time.sleep(float(control().get("delay") or 0))
        lines = fn(*args) or []
        for ln in lines:
            if on_line:
                on_line(ln)
        return True

    def loom_upload(self, items, show, keep_local):
        out = []
        for it in items:
            lk = f"https://www.loom.com/share/stub{re.sub('[^A-Za-z0-9]', '', str(it['id']))[:12]}"
            self.uploaded[re.sub(r"[^A-Za-z0-9]", "", str(it["id"]))[:24]] = {"link": lk, "at": time.time()}
            record("loom_upload", id=it["id"], title=it.get("title"))
            out.append("LOOM " + json.dumps({"id": it["id"], "ok": True, "link": lk, "comment_posted": True, "other_links": 0, "deleted": True}))
        return out

    def ad_video(self, items, voice, product, host, narrator):
        c = control()
        out = []
        for it in items:
            if c["ad_blocked"]:
                out.append("ADVIDEO " + json.dumps({"id": it["id"], "ok": False, "reason": "the Ad Library is not answering", "transient": True}))
            elif has(c["no_ads"], it.get("company")):
                out.append("ADVIDEO " + json.dumps({"id": it["id"], "ok": False, "reason": "no advertiser found for this brand", "transient": False}))
            else:
                record("ad_video", id=it["id"], company=it.get("company"), voice=voice, narrator=narrator)
                out.append("ADVIDEO " + json.dumps({"id": it["id"], "ok": True, "reason": "", "transient": False, "link": "", "file": f"tools/ad_videos/{it['id']}.mp4", "facts": {"active": 7, "total": 9},
                                                    "script": "stub script", "display_name": it.get("company"), "qa": {"ok": True, "match": 0.99, "reason": ""}}))
        return out

    def halcrus_leads(self, want, tier, emails, companies):
        c = control()
        have = {e.lower() for e in emails}
        out = []
        while len(out) < want and self.next_halcrus <= c["halcrus_pool"]:
            n = self.next_halcrus
            self.next_halcrus += 1
            em = f"owner{n}@brand{n}{'-captcha' if n % 7 == 0 else '-noform' if n % 11 == 0 else ''}.test"
            if em in have:
                continue
            name = f"Brand{n}" + ("-noads" if n % 5 == 0 else "")
            out.append("HALCRUS " + json.dumps({"company": name, "email": em, "name": "Pat Doe" if n % 2 else "—", "why": "stub", "score": 90 - n, "others": [], "category": "stub"}))
        out.append("HALCRUSEND " + json.dumps({"found": len(out)}))
        return out

    def email_check(self, items, ai, jitbit):
        c = control()
        return ["EMAILCHECK " + json.dumps({"id": it["id"], "email": it["email"], "valid": not has(c["bad_email"], it.get("company")), "level": "ok", "why": "stub", "ai": None, "reason": ""}) for it in items]

    def email_drafts(self, leads, template, limit):
        out = []
        for l in leads:
            record("draft", id=l["id"], to=l.get("email"), loom=l.get("vars", {}).get("loom_link"))
            out.append("EMAIL " + json.dumps({"id": l["id"], "to": l.get("email"), "status": "drafted", "why": ""}))
        return out

    def cform_find(self, items):
        c = control()
        out = []
        for it in items:
            co = it.get("domain", "")
            if has(c["no_form"], co):
                res = {"found": False, "why": "no contact form"}
            elif has(c["form_captcha"], co):
                res = {"found": True, "url": f"https://{co}/contact", "captcha": "visible", "needs_human": False}
            else:
                res = {"found": True, "url": f"https://{co}/contact", "captcha": "none", "needs_human": False}
            out.append("CFORM " + json.dumps({"id": it["id"], **res}))
        return out

    def cform_send(self, items, values, limit):
        out = []
        for it in items:
            if len(slog_rows("cform_send")) >= int(limit):      # like the real sender: the daily limit
                out.append("CFORMSEND " + json.dumps({"id": it["id"], "status": "later", "note": f"today's contact form limit ({limit}) is used up"}))
                continue
            record("cform_send", id=it["id"], url=it["url"], subject=it.get("subject"), message=it.get("message"))
            out.append("CFORMSEND " + json.dumps({"id": it["id"], "status": "sent", "note": "stub"}))
        return out

    def cform_batch(self, items, values):
        out = []
        for it in items:
            record("cform_batch", id=it["id"], url=it["url"])
            out.append("CFORMSEND " + json.dumps({"id": it["id"], "status": "sent", "note": "stub: solved by the person"}))
        return out
