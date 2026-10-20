#!/usr/bin/env python3
"""
The autonomous outreach loop, run by the always-on helper (tools/local_runner.py) instead of by an open CRM browser tab.

It works on the lead store the helper keeps (tools/crm_store.py) and does, in this order of priority, one thing at a time (the helper runs one job at a time):

    find leads (HALCRUS) -> email check (built-in + Jitbit, optional Google AI) -> "can this lead be emailed?" (the engine's own preview)
    -> Ad Library brand check + the narrated video (ad_video) -> Loom upload -> contact form first (forms before email) / email draft + scheduled send
    -> follow-ups -> reply checks (people vs automatic replies)

Every rule of handoff/HANDOFF.md lives here as a port of what index.html used to do in the page (the page keeps showing and controlling it):
  - Strategy 1 and Strategy 2 never mix (stratOf), forms before email (cformRoute), the Loom message is preferred over Template 1 (loomPrefer)
  - the Ad Library canary: "no advertiser found" only counts while a canary search answers, otherwise the lead is left alone and the loop waits an hour
  - stock goal = keepDays x what the enabled inboxes may send today; follow-ups 3 and 7 days; the do-not-contact list; sending hours and the send queue
  - it never solves a CAPTCHA: CAPTCHA forms wait for the person (the page's button opens the visible batch window) and fall back to email after N hours

All side effects go through `ports` (see Ports in local_runner.py), so the same code runs against stubs in tests.
Its own memory (why it paused, when it last looked, alerts already sent) is in tools/loop_state.json, so a restart carries on where it was.
"""
import json
import os
import random
import re
import threading
import time
import traceback
from pathlib import Path

import crm_store

DAY = 86400000
HERE = Path(__file__).resolve().parent
VOICES = [("kokoro:am_michael", "m"), ("dia", "m"), ("kokoro:af_heart", "f"), ("kokoro:af_bella", "f"), ("kokoro:af_nicole", "f"), ("kokoro:am_fenrir", "m"),
          ("edge:en-US-AndrewMultilingualNeural", "m"), ("edge:en-US-BrianMultilingualNeural", "m"), ("edge:en-US-AvaMultilingualNeural", "f"), ("edge:en-US-EmmaMultilingualNeural", "f")]
QUEUED = ("scheduled", "sending", "sent", "canceled", "failed", "bounced", "skipped")
RUN_KEEP_STEPS = 60


def now_ms():
    return int(time.time() * 1000)


def parse_tag(line, tag):
    if not isinstance(line, str) or not line.startswith(tag + " "):
        return None
    try:
        return json.loads(line[len(tag) + 1:])
    except Exception:
        return None


def jsnum(x, default=0):
    """JS's +x||default."""
    try:
        v = float(x)
        return v if v == v and v != 0 else default
    except (TypeError, ValueError):
        return default


# Template 1 through a contact form: no "reply here" (a form cannot be answered), it sends them to the website
DEF_FORM_T1_SUBJECT = "Free ads optimization performance base"
DEF_FORM_T1 = "Hey {first_name},\n\nI came across {business} while looking at brands that advertise on Meta.\n\n{product} connects to your existing Meta ads account, monitors your campaigns 24/7 and automatically cuts wasteful ad spend. There's no setup fee and no monthly fee. We only take 20% of the money we actually save you, and if we don't save you anything, you don't pay us anything.\n\nSee how it works here: {website}\n\n{sender}"


def spin_first(text):
    """{a|b|c} -> a (what the page did for form subjects and messages)."""
    return re.sub(r"\{([^{}|]+)\|[^{}]*\}", r"\1", text)


class LoopError(Exception):
    pass


class Loop:
    def __init__(self, store, ports, state_path=None, tick=15, start_delay=30):
        self.store = store
        self.ports = ports
        self.path = Path(state_path) if state_path else crm_store.DATA / "loop_state.json"
        self.tick_s = tick
        self.start_delay = start_delay
        self.lock = threading.RLock()
        self.requests = []
        self.wake = threading.Event()   # a button press wakes the loop at once instead of at the next tick
        self.abort = False
        self.busy_now = ""          # what the loop is doing this second
        self.es = None
        self.es_at = 0
        self.hc_connected = None
        self.loom_connected = None
        self.hc_last_found = 0
        self.last_error = ""
        self.errors_in_row = 0
        self.alive_at = 0
        self.t_started = time.time()
        self.sleep = time.sleep
        self.st = {"pause": None, "meta_ok": None, "fb": None, "auto": None, "beat": 0, "after": 0, "health": {"at": 0, "v": None}, "cformRepAt": 0, "readyNotified": 0, "alerts": {}, "run": self._idle_run(),
                   "pipeMsg": "", "cfMsg": "", "pipe_at": 0, "keep_at": 0, "last_pipeline": 0}
        self._load_state()

    # ------------------------------------------------------------------ memory
    @staticmethod
    def _idle_run():
        return {"state": "idle", "label": "", "step": "", "startedAt": 0, "endedAt": 0, "stepAt": 0, "lastLine": "", "lastLineAt": 0, "steps": [], "summary": "", "details": []}

    def _load_state(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.st.update({k: v for k, v in d.items() if k in self.st})
        except Exception:
            pass
        if self.st["run"].get("state") == "working":     # the helper restarted in the middle of a round: say so, then carry on
            self.st["run"].update(state="warn", endedAt=now_ms(), summary="The helper restarted during this round. It carries on by itself.")

    def save_state(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.st, default=str), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            print("loop state:", e)

    # ------------------------------------------------------------------ settings and helpers
    @property
    def g(self):
        return self.store.settings

    @property
    def leads(self):
        return self.store.all()

    def commit(self):
        self.store.commit()

    def meta(self):
        m = self.g.get("meta_resolved")
        if not isinstance(m, dict) or not m:
            raise LoopError("The CRM page has not handed its email templates to the helper yet. Open the CRM page once (Meta Ads tab).")
        return m

    def load_es(self, force=False):
        if force or not self.es or time.time() - self.es_at > 20:
            self.es = self.ports.email_status()
            self.es_at = time.time()
        return self.es

    def sender(self):
        return (self.load_es().get("sender_name") or "Emmanuel")

    # ------------------------------------------------------------------ run panel (what the page shows as "Working / Done")
    def run_begin(self, label):
        self.abort = False
        self.st["run"] = {**self._idle_run(), "state": "working", "label": label, "step": "Starting…", "startedAt": now_ms(), "stepAt": now_ms()}
        self.save_state()

    def run_step(self, m):
        r = self.st["run"]
        if r["state"] != "working":
            return
        r["step"] = m
        r["stepAt"] = now_ms()
        r["steps"].append(time.strftime("%-I:%M:%S %p  ") + m)
        r["steps"] = r["steps"][-RUN_KEEP_STEPS:]

    def run_end(self, state, summary, details=None):
        r = self.st["run"]
        r.update(state=state, summary=summary, details=details or [], endedAt=now_ms())
        self.save_state()

    def run_line(self, t):
        r = self.st["run"]
        m = re.match(r"^(ADVIDEO|LOOM|EMAIL|HALCRUSEND|HALCRUS) ", t or "")
        pretty = {"ADVIDEO": "a video finished", "LOOM": "a video was uploaded to Loom", "EMAIL": "an email draft was handled", "HALCRUS": "a new lead was found", "HALCRUSEND": "finished reading HALCRUS"}
        r["lastLine"] = pretty[m.group(1)] if m else str(t or "")[:140]
        r["lastLineAt"] = now_ms()

    def finish_from(self, res):
        """runFinishFrom for the Loom flow."""
        d = []
        if not res or res.get("busy"):
            return self.run_end("warn", "Another job was already running, so nothing new was started. It tries again by itself.")
        if res.get("fatal"):
            return self.run_end("fail", res["fatal"])
        for x in res.get("rejected") or []:
            d.append(f"{x['company']}: no video ({x['reason']})")
        d += res.get("notes") or []
        parts = []
        if res.get("made"):
            parts.append(f"{res['made']} video{'' if res['made'] == 1 else 's'} made")
        if res.get("uploaded"):
            parts.append(f"{res['uploaded']} uploaded to Loom")
        if res.get("drafted"):
            parts.append(f"{res['drafted']} draft{'' if res['drafted'] == 1 else 's'} saved")
        tail = f" {res['ready']} approved email{' is' if res['ready'] == 1 else 's are'} ready to send." if res.get("ready") else ""
        if res.get("made") and res.get("loomDown"):
            return self.run_end("warn", f"{res['made']} video{'' if res['made'] == 1 else 's'} made but NOT uploaded: Loom is not connected. Press \"Connect Loom\" and log in, then it carries on by itself.", d)
        if parts or res.get("ready"):
            return self.run_end("ok", (", ".join(parts) or "Nothing new was made") + "." + tail, d)
        if res.get("rejected"):
            msg = "No videos could be made: every lead that was tried was skipped (details below), and there were no more leads to try. Import more leads, or connect HALCRUS so it can find more."
        elif res.get("noLeads"):
            msg = "No leads to make videos for. Import leads, or connect HALCRUS so it can find more."
        else:
            msg = "Nothing new to do right now."
        return self.run_end("warn", msg, d)

    # ------------------------------------------------------------------ the rules (ports of index.html)
    def strat_of(self, l):
        if l.get("strategy"):
            return l["strategy"]
        v = l.get("video")
        if l.get("loomLink") or l.get("loomSentAt") or l.get("videoAuto") or (isinstance(v, dict) and v.get("ok")):
            return "loom"
        if l.get("emailedAt") or l.get("draftedAt"):
            return "proposal"
        return ""

    @staticmethod
    def queued(l):
        return l.get("queueStatus") in QUEUED

    @staticmethod
    def is_bounced(l):
        return bool(l.get("bounced") or l.get("queueStatus") == "bounced")

    @staticmethod
    def cf_domain(l):
        e = str(l.get("email") or "")
        return e.split("@")[1] if "@" in e else ""

    def t1_reason(self, l):
        """Why this lead may get Template 1 (the plain first message) through its contact form instead of a Loom video."""
        if self.g.get("cformT1") is False or l.get("loomLink") or l.get("emailedAt") or l.get("draftedAt") or l.get("dnc") or l.get("replied") or l.get("removedAt") \
                or l.get("contacted") or not l.get("email") or self.strat_of(l) == "loom":
            return ""
        v = l.get("video")
        if isinstance(v, dict) and not v.get("ok") and not re.search(r"^error|transient", v.get("reason") or "", re.I):
            return "no Meta ads"
        if l.get("autoT1"):
            return "Meta was blocking the Ad Library (Template 1 for now)"
        if l.get("adRetry") and (l.get("adTries") or 0) >= 2:
            return "the Ad Library stayed blocked"
        ine = l.get("ineligible")
        if isinstance(ine, dict) and re.search(r"email|mail server|disposable|valid", ine.get("why") or "", re.I):
            return "email not usable"
        return ""

    def t1_eligible(self, l):
        return bool(self.t1_reason(l))

    def cform_route(self, l):
        """'wait' (no Loom link yet) | 'check' (form not looked for yet) | 'form-auto' | 'human' (CAPTCHA etc., waiting for you) | 'form-done' | 'email'"""
        if self.g.get("cformOn") is False:
            return "email"
        if l.get("queueStatus") == "sending":
            return "email"
        cs = l.get("cformSent")
        sent = cs.get("status") if isinstance(cs, dict) else None
        c = l.get("cform")
        if sent in ("sent", "unsure"):
            return "form-done"
        if not l.get("loomLink") and not self.t1_eligible(l):
            return "wait"
        if l.get("cformSkip") or sent == "failed":
            return "email"
        if not isinstance(c, dict):
            return "email" if (l.get("emailedAt") and not self.is_bounced(l)) else "check"
        if not c.get("found"):
            return "email"
        if l.get("emailedAt") and not self.is_bounced(l):
            return "email"
        if sent == "needs_human" and (l.get("cformTries") or 0) >= 3:
            return "email"
        if (l.get("cformSkips") or 0) >= 2:
            return "email"
        if c.get("captcha") == "visible" or c.get("needs_human") or sent == "needs_human":
            h = self.g.get("cformFallbackHours")
            h = 48 if h is None else jsnum(h, 0)
            if h > 0 and now_ms() - (c.get("at") or 0) > h * 3600e3 and not self.email_off():
                return "email"       # (with email switched off a CAPTCHA form keeps waiting for you instead)
            return "human"
        return "form-auto"

    def email_off(self):
        """Forms only: nothing is emailed (no drafts, no scheduled sends, no follow-ups); leads without a usable form are left alone."""
        return bool(self.g.get("emailOff"))

    def email_route(self, l):
        return self.cform_route(l) == "email"

    def pending_form(self, l):
        return self.cform_route(l) in ("form-auto", "human")

    def loom_to_upload(self):
        return [l for l in self.leads if isinstance(l.get("video"), dict) and l["video"].get("ok") and l["video"].get("file") and l.get("videoApproved") and not l.get("loomLink")
                and not l.get("loomFail") and not l.get("dnc") and not l.get("emailedAt")]

    def loom_to_draft(self):
        if self.email_off():
            return []
        return [l for l in self.leads if l.get("videoAuto") and l.get("videoApproved") and l.get("loomLink") and not l.get("draftedAt") and not l.get("emailedAt")
                and not l.get("dnc") and not l.get("loomSentAt") and self.email_route(l)]

    def loom_to_send_base(self, manual=False):
        if self.email_off():
            return []
        return [l for l in self.leads if l.get("videoAuto") and l.get("videoApproved") and (manual or not l.get("videoAutoApproved") or self.g.get("loomSendChecked")) and l.get("loomLink")
                and l.get("draftedAt") and not l.get("emailedAt") and not l.get("dnc") and not self.queued(l) and self.email_route(l)]

    def start_pool(self):
        t = now_ms()
        out = []
        for l in self.leads:
            v = l.get("video")
            ine = l.get("ineligible")
            if (l.get("email") and not l.get("dnc") and not l.get("replied") and not l.get("contacted") and not l.get("emailedAt") and not l.get("loomSentAt") and not l.get("loomLink")
                    and not l.get("bounced") and not self.queued(l) and not l.get("removedAt") and not (isinstance(ine, dict) and t - (ine.get("at") or 0) < 6048e5)
                    and self.strat_of(l) != "proposal" and not l.get("autoT1") and not (isinstance(v, dict) and (v.get("ok") or v.get("madeAt")))
                    and not (self.email_off() and isinstance(l.get("cform"), dict) and not l["cform"].get("found"))):
                out.append(l)
        out.sort(key=lambda l: (0 if l.get("top") else 1, -jsnum(l.get("score"))))
        return out

    def stock_now(self):
        if self.email_off():     # forms only: what is "saved" is the forms waiting to go out (or waiting for you), not drafts or scheduled emails
            return len([l for l in self.leads if (self.strat_of(l) == "loom" or l.get("autoT1")) and self.pending_form(l) and not l.get("dnc") and not l.get("removedAt") and not l.get("replied")])
        return len([l for l in self.leads if (self.strat_of(l) == "loom" or l.get("autoT1")) and (l.get("draftedAt") or self.queued(l) or self.pending_form(l)) and not l.get("emailedAt") and not l.get("dnc")
                    and not l.get("removedAt") and not l.get("bounced") and not l.get("replied") and l.get("queueStatus") != "sent"])

    def daily_cap(self):
        inb = self.load_es().get("inboxes") or []
        return max(10, sum(int(i.get("cap") or 0) for i in inb if not i.get("paused")))

    def keep_days(self):
        return int(max(1, min(7, jsnum(self.g.get("keepDays"), 2))))

    def stock_goal(self):
        if self.email_off():
            return self.keep_days() * int(max(1, min(250, jsnum(self.g.get("cformDaily"), 25))))
        return self.keep_days() * self.daily_cap()

    def fu_due(self, l):
        if not l.get("emailedAt") or l.get("replied") or l.get("dnc") or l.get("bounced"):
            return 0
        d = now_ms() - (l.get("firstEmailedAt") or l.get("emailedAt"))
        st = l.get("emailStep") or 0
        if st == 0 and d >= 3 * DAY - 36e5:
            return 1
        if st == 1 and d >= 7 * DAY - 36e5:
            return 2
        return 0

    def pipeline_backlog(self):
        """Leads whose video is made (or on Loom) but whose draft / scheduled send / form message does not exist yet: they will become stock."""
        done = ("form-done", "human", "form-auto") + (("email",) if self.email_off() else ())     # forms only: a lead with no form is a dead end, not stock on its way
        loom = len([l for l in self.leads if l.get("videoAuto") and l.get("videoApproved") and not l.get("emailedAt") and not l.get("dnc") and not l.get("removedAt") and not l.get("draftedAt")
                    and not self.queued(l) and self.cform_route(l) not in done])
        t1 = len([l for l in self.leads if l.get("autoT1") and not l.get("t1Skip") and not l.get("emailedAt") and not l.get("dnc") and not l.get("removedAt") and not l.get("draftedAt")
                  and not self.queued(l) and self.cform_route(l) not in done])
        return loom + t1

    def send_auto(self):
        return self.g.get("sendMode") == "auto"

    # ------------------------------------------------------------------ the email payloads (metaPayload, metaTemplateObj, ...)
    @staticmethod
    def gfirst(l):
        g = l.get("greet")
        return (g.get("first") if isinstance(g, dict) else None) or l.get("company")

    @staticmethod
    def gbiz(l):
        g = l.get("greet")
        return l.get("brandName") or (g.get("business") if isinstance(g, dict) else None) or l.get("company")

    @staticmethod
    def ads_phrase(l):
        n = jsnum(l.get("adsCount"))
        return f"running {int(n)} different ads" if n > 0 else "running several ads"

    def meta_payload(self, l):
        m = self.meta()
        p = {"id": l["id"], "name": l.get("company"), "first_name": self.gfirst(l), "business": self.gbiz(l), "email": l.get("email"),
             "vars": {"product": m.get("product"), "link": m.get("link"), "phone": self.load_es().get("phone") or "", "loom_link": l.get("loomLink") or "", "ads_phrase": self.ads_phrase(l)},
             "tplId": "meta-loom" if l.get("loomLink") else "meta-first"}
        if l.get("source") == "halcrus" and self.g.get("trustHalcrus") is not False:
            p["trusted_source"] = True
        if l.get("bodyOverride"):
            p["body_override"] = l["bodyOverride"]
            p["subject_override"] = l.get("subjectOverride") or ""
        return p

    def template_obj(self):
        m = self.meta()
        return {"library": [
            {"id": "meta-first", "name": "Meta ads: cold first email", "subject": m.get("subject"), "body": m.get("first"), "needs_clip": False, "keep_case": True, "weight": 1,
             "followups": [{"days": 3, "body": m.get("fu1")}, {"days": 7, "body": m.get("fu2")}]},
            {"id": "meta-loom", "name": "Meta ads: Loom video email", "subject": m.get("loomSubject"), "body": m.get("loomEmail"), "needs_clip": False, "keep_case": True, "skip_providers": [], "weight": 1,
             "followups": [{"days": 3, "body": m.get("loomFu1")}, {"days": 7, "body": m.get("loomFu2")}]}]}

    def greet(self, lst):
        need = [l for l in lst if l.get("email") and (not isinstance(l.get("greet"), dict) or l["greet"].get("email") != l["email"])]
        if not need:
            return
        try:
            names = self.ports.greet([{"id": l["id"], "email": l["email"], "company": l.get("company"), "person": l.get("person") or ""} for l in need])
        except Exception:
            return
        for l in need:
            g = (names or {}).get(l["id"])
            if g:
                l["greet"] = {"first": g.get("first"), "kind": g.get("kind"), "business": g.get("business"), "email": l["email"]}
        self.commit()

    def form_site(self):
        return self.g.get("formWebsite") or "http://AIadsOptimization.com"

    def form_t1_text(self, m):
        o = str(self.g.get("cformMsgT1") or "").strip()
        return DEF_FORM_T1 if (not o or o == str(m.get("first") or "").strip()) else self.g.get("cformMsgT1")

    def cform_text(self, l):
        m = self.meta()
        t1 = not l.get("loomLink")
        text = str(self.form_t1_text(m) if t1 else (self.g.get("cformMsg") or m.get("loomEmail") or ""))
        gr = l.get("greet")
        first = (gr.get("first") if isinstance(gr, dict) else None) or "there"
        text = re.sub(r"\{first_name\}|\{first\}", lambda _: first, text)
        for k, v in (("business", self.gbiz(l)), ("loom_link", l.get("loomLink") or ""), ("product", m.get("product") or ""), ("link", m.get("link") or ""), ("website", self.form_site()), ("sender", self.sender())):
            text = text.replace("{" + k + "}", str(v))
        return spin_first(text)

    def cform_subject(self):
        return self.g.get("cformSubject") or "A quick find in your Facebook ads"

    def cform_subject_for(self, l):
        if l.get("loomLink"):
            return self.cform_subject()
        return self.g.get("cformSubjectT1") or DEF_FORM_T1_SUBJECT

    def cform_vals(self):
        return {"subject": self.cform_subject(), "company": self.meta().get("product") or "AI Ads Optimization", "website": self.form_site()}

    def voice(self):
        v = self.g.get("adVoice")
        return v if v and any(v == x[0] for x in VOICES) else "kokoro:am_michael"

    def narrator(self):
        n = self.g.get("videoName")
        if n is not None:
            return n
        gender = next((x[1] for x in VOICES if x[0] == self.voice()), "f")
        return self.sender() if gender == "m" else ""

    # ------------------------------------------------------------------ alerts (only when a person must act)
    def alert(self, title, message, key, every=21600):
        """One phone/Mac alert per key per `every` seconds, remembered across restarts."""
        last = self.st["alerts"].get(key, 0)
        if time.time() - last < every:
            return False
        self.st["alerts"][key] = time.time()
        self.save_state()
        try:
            self.ports.notify(title, message, key, every)
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------ jobs
    def job(self, label, fn, *args, on_line=None, wait=1800):
        """Runs a helper job (one at a time) and feeds its output lines to on_line. Waits for the helper to be free; False if it never was."""
        if self.is_paused():
            return False
        self.busy_now = label
        try:
            return self.ports.run_job(label, fn, args, on_line, wait, lambda: self.abort)
        finally:
            self.busy_now = ""

    # ------------------------------------------------------------------ queue sync / uploads / small status checks
    def loom_status(self):
        try:
            self.loom_connected = bool(self.ports.loom_connected())
        except Exception:
            self.loom_connected = None
        return self.loom_connected

    def meta_connected(self):
        try:
            return bool(self.ports.meta_connected())
        except Exception:
            return False

    def hc_status(self):
        try:
            self.hc_connected = bool(self.ports.halcrus_connected())
        except Exception:
            self.hc_connected = None
        return self.hc_connected

    def queue_sync(self):
        try:
            r = self.ports.queue_list()
        except Exception:
            return
        items = (r or {}).get("items")
        if items is None:
            return
        self.queue_cache = r
        for q in items:
            parts = str(q["id"]).split("#fu")
            l = self.store.get(parts[0])
            if not l:
                continue
            if q.get("replied"):
                l["replied"] = True
            if len(parts) > 1:
                try:
                    n = q.get("step") or int(parts[1]) or 0
                except ValueError:
                    n = q.get("step") or 0
                l["fuQueued"] = max(l.get("fuQueued") or 0, n)
                if q.get("status") == "sent":
                    l["emailStep"] = max(l.get("emailStep") or 0, n)
                continue
            if q.get("status") == "canceled" and l.get("cformMoved"):
                continue   # cancelled by us because the contact form took over
            l["queueStatus"] = q.get("status")
            l["sendDue"] = q.get("due_at") or 0
            if q.get("status") in ("sent", "bounced"):
                if not l.get("emailedAt"):
                    l["emailedAt"] = int((q.get("sent_at") or 0) * 1000) or now_ms()
                    l["loomSentAt"] = l["emailedAt"]
                if not l.get("firstEmailedAt"):
                    l["firstEmailedAt"] = l["emailedAt"]
                l.pop("draftedAt", None)
                l.pop("skipWhy", None)
                if q["status"] == "bounced":
                    l["bounced"] = l.get("bounced") or now_ms()
            elif q.get("status") == "skipped":
                l["skipWhy"] = q.get("why") or "skipped"
        self.commit()

    def loom_clean(self):
        for l in self.leads:
            if self.strat_of(l) == "loom" and l.get("msSkip"):
                l.pop("msSkip", None)
                if re.search("Microsoft", l.get("skipWhy") or "", re.I):
                    l.pop("skipWhy", None)

    def loom_sync_uploads(self):
        try:
            ups = self.ports.loom_uploads() or {}
        except Exception:
            return 0

        def key(s):
            return re.sub(r"[^A-Za-z0-9]", "", str(s))[:24]
        by = {key(l["id"]): l for l in self.leads}
        n = 0
        for stem, v in ups.items():
            l = by.get(key(stem))
            if l and not l.get("loomLink") and v.get("link"):
                l["loomLink"] = v["link"]
                l["loomUploadedAt"] = int((v.get("at") or 0) * 1000) or now_ms()
                l.pop("loomFail", None)
                l["strategy"] = l.get("strategy") or "loom"
                n += 1
        if n:
            self.commit()
        return n

    # ------------------------------------------------------------------ steps of the pipeline
    def loom_upload_approved(self):
        todo = self.loom_to_upload()[:3]
        if not todo:
            return 0
        n = [0]
        m = self.meta()

        def on_line(t):
            self.run_line(t)
            r = parse_tag(t, "LOOM")
            if not r:
                return
            l = self.store.get(r.get("id"))
            if not l:
                return
            if r.get("ok") and r.get("link"):
                l["loomLink"] = r["link"]
                l["loomUploadedAt"] = now_ms()
                l.pop("loomFail", None)
                l["loomNoComment"] = 1 if r.get("comment_posted") is False else 0
                l["loomOther"] = r.get("other_links") or 0
                if r.get("deleted") and isinstance(l.get("video"), dict):
                    l["video"]["file"] = ""
                    l["video"]["inLoomOnly"] = 1
                n[0] += 1
            else:
                l["loomFail"] = r.get("error") or "upload failed"
                if r.get("link") and not r.get("ok"):
                    l["loomPartial"] = r["link"]
                if r.get("needs_login"):
                    self.loom_connected = False
                    self.alert("Loom needs you", "Loom asked for a login again, so videos are waiting. Open the CRM, Meta Ads tab, and press Connect Loom.", "loom-login", 6 * 3600)
            self.commit()
        items = [{"id": l["id"], "file": l["video"]["file"], "title": self.loom_title(l), "comment": m.get("link") or ""} for l in todo]
        self.job(f"uploading {len(items)} approved video(s) to Loom", self.ports.loom_upload, items, bool(self.g.get("loomShow")), bool(self.g.get("keepLocalVideos")), on_line=on_line)
        self.commit()
        return n[0]

    def loom_title(self, l):
        return f"A quick look at {self.gbiz(l)}'s Meta ads"

    def loom_make_drafts(self):
        todo = self.loom_to_draft()[:10]
        if not todo:
            return 0
        self.greet(todo)

        def on_line(t):
            self.run_line(t)
            r = parse_tag(t, "EMAIL")
            if not r:
                return
            l = self.store.get(r.get("id"))
            if not l:
                return
            if r.get("status") == "drafted":
                l["draftedAt"] = now_ms()
                l.pop("skipWhy", None)
                l.pop("msSkip", None)
            elif r.get("status") == "skip":
                l["skipWhy"] = r.get("why")
                if re.search("Microsoft", r.get("why") or "", re.I) and self.strat_of(l) != "loom":
                    l["msSkip"] = now_ms()
            self.commit()
        self.job(f"saving {len(todo)} email draft(s)", self.ports.email_drafts, [self.meta_payload(l) for l in todo], self.template_obj(), 10, on_line=on_line)
        self.commit()
        return len([l for l in todo if l.get("draftedAt")])

    def ready_check(self):
        """'I press Send myself' mode: tell the person when enough approved emails are waiting and sending hours are open."""
        if self.send_auto():
            return
        n = len(self.loom_to_send_base(True))
        x = int(max(1, jsnum(self.g.get("notifyReady"), 10)))
        if self.st.get("ready_x") != x:      # the person changed "notify me when N are ready": count again from zero
            self.st["ready_x"] = x
            self.st["readyNotified"] = 0
        k = n // x
        if n >= x and not self.load_es(True).get("in_send_window"):
            return
        if k > (self.st.get("readyNotified") or 0):
            self.st["readyNotified"] = k
            self.save_state()
            try:
                self.ports.notify("Ready to send", f"Sending hours are open and {n} approved emails are ready (videos are in Loom). Open the CRM (Meta Ads tab) and press \"Send {n} approved emails\" to start.", f"ready-{k}-{int(time.time())}", 3600)
            except Exception:
                pass
        elif n < x and self.st.get("readyNotified"):
            self.st["readyNotified"] = 0
            self.save_state()

    def pipeline(self, auto):
        """One pass: upload approved videos, look for contact forms (and send them), save email drafts, schedule approved emails (auto)."""
        say = lambda m: self.st.__setitem__("pipeMsg", m)
        self.loom_status()
        self.queue_sync()
        self.loom_sync_uploads()
        self.loom_clean()
        up = len(self.loom_to_upload())
        if up and self.loom_connected is False:
            say('Loom is not connected. Press "Connect Loom" and log in once.')
            self.alert("Loom needs you", f"{up} approved videos are waiting but Loom is not connected. Open the CRM, Meta Ads tab, and press Connect Loom.", "loom-login", 6 * 3600)
            return
        u = d = s = 0
        if up:
            say(f"Uploading {min(up, 3)} approved video(s) to Loom…")
            u = self.loom_upload_approved()
        if self.g.get("cformOn") is not False:
            say("Looking for contact forms (they go before email)…")
            self.cform_step()
        if self.loom_to_draft():
            say("Saving the emails as drafts…")
            d = self.loom_make_drafts()
        if auto and self.send_auto() and self.g.get("loomAutoSend") and self.loom_to_send_base(False):
            say("Scheduling the approved emails…")
            s = self.loom_queue_approved(False)
        else:
            self.ready_check()
        if auto and self.send_auto():
            self.t1_queue()
        if u or d or s:
            say(f"Uploaded {u}, saved {d} draft(s)" + (f", scheduled {s} to send" if auto and self.g.get("loomAutoSend") else "") + ".")
        elif not up and not self.loom_to_draft() and not (auto and self.loom_to_send_base(False)):
            say("Nothing waiting.")
        self.commit()
        self.st["last_pipeline"] = now_ms()
        self.save_state()

    # ----- contact forms
    def cform_find(self, mx=10, only=None):
        """Looks for contact forms. By default for leads that already have a Loom video / are Template 1 candidates; `only` = an explicit list (forms-only mode checks BEFORE spending a video)."""
        if only is not None:
            need = [l for l in only if not l.get("cform") and not l.get("cformSkip") and (l.get("cformTry") or 0) < 2 and not l.get("dnc")][:mx]
        else:
            need = [l for l in self.leads if (l.get("loomLink") or self.t1_eligible(l)) and not l.get("cform") and not l.get("dnc") and not l.get("cformSkip") and (l.get("cformTry") or 0) < 2
                    and not (l.get("emailedAt") and not self.is_bounced(l))][:mx]
        if not need:
            return 0
        found = [0]

        def on_line(t):
            r = parse_tag(t, "CFORM")
            if not r:
                return
            l = self.store.get(r.get("id"))
            if not l:
                return
            r = {k: v for k, v in r.items() if k != "id"}
            l["cform"] = {**r, "at": now_ms()}
            if r.get("found"):
                found[0] += 1
            self.commit()
        self.job(f"looking for contact forms on {len(need)} site(s)", self.ports.cform_find, [{"id": l["id"], "domain": self.cf_domain(l), "web": l.get("web") or ""} for l in need][:20], on_line=on_line)
        for l in need:
            if not l.get("cform"):
                l["cformTry"] = (l.get("cformTry") or 0) + 1
                if l["cformTry"] >= 2:
                    l["cform"] = {"found": False, "why": "the website could not be checked", "error": True, "at": now_ms()}
        self.commit()
        return found[0]

    def loom_prefer(self, lst):
        """Template 1 is the fallback: leads held back only because the Ad Library was blocked go back to the Loom video the moment it answers again."""
        blocked = [l for l in lst if not l.get("loomLink") and self.t1_reason(l) == "the Ad Library stayed blocked"]
        if not blocked:
            return
        h = self.st["health"]
        if time.time() * 1000 - h.get("at", 0) < 600000:
            v = h.get("v")
        else:
            try:
                v = self.ports.ads_health()
            except Exception:
                v = None
            self.st["health"] = {"at": now_ms(), "v": v}
            self.save_state()
        if v is True:
            for l in blocked:
                l.pop("adRetry", None)
                l["adTries"] = 0
                lst.remove(l)
            self.commit()
            self.st["cfMsg"] = "The Ad Library is answering again: those leads go back to the Loom video, not Template 1."
            self.st["after"] = 0
            self.st["keep_at"] = time.time() + 3
        elif v is None:
            for l in blocked:   # helper busy or unsure: wait one more pass rather than send Template 1 by mistake
                lst.remove(l)

    def cform_send_auto(self, manual=False):
        lst = [l for l in self.leads if self.cform_route(l) == "form-auto" and not l.get("dnc")][:8]
        if not lst:
            return 0
        if not self.g.get("cformConfirmed"):
            return 0
        es = self.load_es(True)
        if not self.g.get("cformAnyTime") and not es.get("in_send_window"):
            self.st["cfMsg"] = 'Outside sending hours. Switch on "Allow forms outside sending hours" to send now.'
            return 0
        self.loom_prefer(lst)
        if not lst:
            return 0
        self.greet(lst)
        for l in lst:
            if l.get("queueStatus") == "scheduled":   # the email was waiting for a slot: the form goes out instead, so they are never contacted twice
                try:
                    r = self.ports.queue_cancel(l["id"])
                    if r is not False:
                        l["cformMoved"] = now_ms()
                        l.pop("queueStatus", None)
                except Exception:
                    pass
        self.commit()
        sent = [0]

        def on_line(t):
            self.run_line(t)
            r = parse_tag(t, "CFORMSEND")
            if not r:
                return
            l = self.store.get(r.get("id"))
            if not l:
                return
            if r.get("status") == "later":
                if l.get("cformMoved"):
                    l["cformSkip"] = True
                self.commit()
                return
            l["cformSent"] = {"at": now_ms(), "status": r.get("status"), "note": r.get("note") or "", "shot": r.get("shot") or ""}
            if r.get("status") in ("sent", "unsure"):
                sent[0] += 1
                if not l.get("loomLink"):
                    l["cformTpl"] = "t1"
                    l["strategy"] = "proposal"
            self.commit()
        limit = int(max(1, min(250, jsnum(self.g.get("cformDaily"), 25))))
        items = [{"id": l["id"], "url": l["cform"]["url"], "message": self.cform_text(l), "subject": self.cform_subject_for(l)} for l in lst]
        self.job(f"sending {len(items)} message(s) through contact forms", self.ports.cform_send, items, self.cform_vals(), limit, on_line=on_line)
        self.commit()
        return sent[0]

    def waiting_human(self):
        return len([l for l in self.leads if self.cform_route(l) == "human"])

    def cform_step(self):
        if self.g.get("cformOn") is False:
            return
        try:
            f = self.cform_find(10)
            if f:
                self.st["cfMsg"] = f"{f} contact form(s) found."
            self.cform_send_auto(False)
            w = self.waiting_human()
            if w >= 10:
                self.alert("CAPTCHA forms waiting", f"{w} contact forms are filled-in-ready and need a CAPTCHA from you. Open the CRM, Meta Ads tab, step 4, and press \"Fill the CAPTCHA forms for me\". It takes a few minutes.", "cform-captcha")
        except LoopError:
            raise
        except Exception as e:
            self.last_error = "contact forms: " + str(e)[:120]

    def captcha_batch(self):
        """The person pressed the button: a visible Chrome window, every CAPTCHA form pre-filled, the person solves, the helper presses send. Never solves anything."""
        lst = [l for l in self.leads if self.cform_route(l) == "human" and isinstance(l.get("cform"), dict) and l["cform"].get("url") and not l.get("dnc")][:250]
        if not lst:
            self.st["cfMsg"] = "No CAPTCHA forms are waiting."
            return 0
        if not self.g.get("cformConfirmed"):
            self.st["cfMsg"] = "Allow contact forms first (the one-time question in step 4)."
            return 0
        self.greet(lst)
        rnd = max(1, min(10, int(self.g.get("cformRound") or 5)))
        self.st["cfMsg"] = f"Opening the first {min(rnd, len(lst))} of {len(lst)} CAPTCHA form(s) in a Chrome window. Solve each CAPTCHA there."
        self.save_state()
        n = [0]

        def on_line(t):
            stt = parse_tag(t, "CFORMSTATUS")
            if stt:
                if stt.get("state") == "round done":
                    self.st["cfMsg"] = f"Round {stt.get('round')} done: {stt.get('sent')} sent so far, {stt.get('waiting')} more waiting. Press \"Next {rnd} forms\" when you are ready, or \"Stop\" to come back later."
                elif stt.get("state") == "working":
                    self.st["cfMsg"] = f"Round {stt.get('round')}: {stt.get('open')} open, {stt.get('sent')} sent so far, {stt.get('waiting')} more waiting. Solve the CAPTCHAs in the open tabs."
                self.save_state()
                return
            r = parse_tag(t, "CFORMSEND")
            if not r:
                return
            l = self.store.get(r.get("id"))
            if not l:
                return
            if r.get("status") == "later":
                l["cformLaterAt"] = now_ms()
                l["cformLaterNote"] = r.get("note") or ""
                if str(r.get("note") or "").startswith("skipped"):
                    l["cformSkips"] = (l.get("cformSkips") or 0) + 1      # skipped twice: the form is probably broken, so it stops coming back
            else:
                l["cformSent"] = {"at": now_ms(), "status": r.get("status"), "note": r.get("note") or "", "shot": r.get("shot") or ""}
                if r.get("status") == "needs_human":
                    l["cformTries"] = (l.get("cformTries") or 0) + 1
                if r.get("status") in ("sent", "unsure"):
                    n[0] += 1
                    if not l.get("loomLink"):
                        l["cformTpl"] = "t1"
                        l["strategy"] = "proposal"
            self.commit()
        items = [{"id": l["id"], "url": l["cform"]["url"], "message": self.cform_text(l), "subject": self.cform_subject_for(l)} for l in lst]
        try:
            self.job(f"{len(items)} CAPTCHA form(s) in a window for you, {rnd} at a time", self.ports.cform_batch, items, {**self.cform_vals(), "window": rnd, "autoNext": bool(self.g.get("cformAutoNext"))}, on_line=on_line, wait=14400)
        finally:
            self.st["cfMsg"] = f"CAPTCHA forms: {n[0]} sent." if n[0] else ""
            self.commit()
            self.save_state()
        return n[0]

    def cform_replies_check(self, force=False):
        g = self.st
        if not force and now_ms() - (g.get("cformRepAt") or 0) < 30 * 60000:
            return
        items = [{"id": l["id"], "domain": self.cf_domain(l), "email": l.get("email"), "since_ts": int((l["cformSent"].get("at") or now_ms()) / 1000) - 3600}
                 for l in self.leads if isinstance(l.get("cformSent"), dict) and re.search("sent|unsure", l["cformSent"].get("status") or "") and not l.get("cformReply")
                 and now_ms() - (l["cformSent"].get("at") or 0) < 21 * DAY]
        g["cformRepAt"] = now_ms()
        self.save_state()
        if not items:
            return
        try:
            r = self.ports.cform_replies(items[:100])
        except Exception:
            return
        for x in r or []:
            if not isinstance(x, dict):
                continue
            l = self.store.get(x.get("id"))
            if not l:
                continue
            if x.get("kind") == "auto":
                l["cformAuto"] = {"at": now_ms(), "sub": x.get("subtype") or "automatic", "snippet": x.get("snippet") or ""}
                cs = l.get("cformSent")
                if isinstance(cs, dict) and cs.get("status") == "unsure":
                    cs["status"] = "sent"
                    cs["note"] = "confirmed by their automatic reply"
                continue
            l["cformReply"] = {"at": now_ms(), "kind": x.get("kind"), "snippet": x.get("snippet")}
            l["replied"] = True
            if x.get("kind") == "optout":
                l["dnc"] = True
        self.commit()

    # ----- queueing sends and follow-ups
    def loom_queue_followups(self):
        if self.email_off():
            return 0
        due = [(l, self.fu_due(l)) for l in self.leads]
        due = [(l, st) for l, st in due if st and (l.get("fuQueued") or 0) < st]
        if not due:
            return 0
        self.greet([x[0] for x in due])
        n = 0
        tpl = self.template_obj()
        for st in (1, 2):
            grp = [x[0] for x in due if x[1] == st]
            if not grp:
                continue
            try:
                r = self.ports.queue_add([{"id": f"{l['id']}#fu{st}", "to": l.get("email"), "company": l.get("brandName") or l.get("company"), "lead": self.meta_payload(l), "template": tpl,
                                           "campaign": "meta", "step": st} for l in grp][:60])
            except Exception:
                return n
            for x in r or []:
                l = self.store.get(str(x.get("id")).split("#fu")[0])
                if l and (x.get("status") == "scheduled" or x.get("why") == "already in the queue"):
                    l["fuQueued"] = st
                    n += 1
        self.commit()
        return n

    def loom_queue_approved(self, manual=False):
        if self.email_off():
            self.st["pipeMsg"] = "Email is switched off (forms only), so nothing was scheduled."
            return 0
        todo = self.loom_to_send_base(bool(manual))[:60]
        if not todo:
            return 0
        self.greet(todo)
        try:
            r = self.ports.queue_add([{"id": l["id"], "to": l.get("email"), "company": l.get("brandName") or l.get("company"), "lead": self.meta_payload(l), "template": self.template_obj(),
                                       "campaign": "meta", "step": 0} for l in todo])
        except Exception as e:
            self.st["pipeMsg"] = "Not scheduled: " + str(e)[:100]
            return 0
        n = 0
        for x in r or []:
            l = self.store.get(x.get("id"))
            if not l:
                continue
            if x.get("status") == "scheduled":
                l["queueStatus"] = "scheduled"
                l["sendDue"] = x.get("due_at")
                n += 1
            elif x.get("status") == "skipped":
                l["skipWhy"] = x.get("why")
        self.commit()
        self.queue_sync()
        return n

    # ----- finding leads, checking them, making the videos
    def add_halcrus_lead(self, r):
        have = {str(x.get("company")).lower() for x in self.leads}
        em = {str(x.get("email") or "").lower() for x in self.leads}
        if not r.get("company") or r["company"].lower() in have or str(r.get("email")).lower() in em:
            return 0
        name = r.get("name")
        lid = "AD" + format(int(time.time() * 1000), "x") + "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(3))
        self.store.add({"id": lid, "company": r["company"], "web": "", "fb": "", "email": r.get("email"), "phones": [], "person": name if name and name != "—" else "", "title": "", "linkedin": "",
                        "signal": r.get("why") or "", "est": "", "siteEmails": r.get("others") or [], "contacted": "", "score": jsnum(r.get("score")), "checkedAt": now_ms() if name else 0, "top": 1,
                        "source": "halcrus", "category": r.get("category") or ""})
        return 1

    def halcrus_more(self, n):
        self.hc_status()
        if not self.hc_connected:
            return -1
        adl = self.leads
        emails = [str(x).lower() for l in adl for x in [l.get("email"), *(l.get("siteEmails") or [])] if x]
        companies = [l.get("company") for l in adl if l.get("company")]
        got = [0]
        end = [None]

        def on_line(t):
            self.run_line(t)
            r = parse_tag(t, "HALCRUS")
            if r is not None:
                got[0] += self.add_halcrus_lead(r)
                return
            r = parse_tag(t, "HALCRUSEND")
            if r is not None:
                end[0] = r
        self.job(f"looking for {n} more leads in HALCRUS", self.ports.halcrus_leads, int(n), self.g.get("halcrusTier") or "Hot", emails, companies, on_line=on_line)
        self.hc_last_found = (end[0] or {}).get("found") or 0 if end[0] else 0
        self.commit()
        if end[0] and end[0].get("needs_login"):
            self.hc_connected = False
            return -1
        if end[0] and end[0].get("error"):
            return -2
        return got[0]

    def make_ad_videos(self, todo):
        stats = {"lookup_fail": 0}
        nm = lambda x: re.sub(r"[^a-z0-9]", "", str(x or "").lower())

        def on_line(t):
            self.run_line(t)
            r = parse_tag(t, "ADVIDEO")
            if not r:
                return
            l = self.store.get(r.get("id"))
            if not l:
                return
            if r.get("transient"):
                stats["lookup_fail"] += 1
                l["adRetry"] = now_ms()
                l["adTries"] = (l.get("adTries") or 0) + 1
                l.pop("video", None)
                self.commit()
                return
            l["video"] = {"ok": r.get("ok"), "reason": r.get("reason"), "link": r.get("link"), "facts": r.get("facts"), "script": r.get("script"), "madeAt": now_ms()}
            if r.get("ok") and r.get("display_name") and nm(r["display_name"]) != nm(l.get("company")):
                l["brandName"] = r["display_name"]
            if r.get("ok"):
                l["strategy"] = "loom"
                l["video"]["file"] = r.get("file")
                if (r.get("facts") or {}).get("active"):
                    l["adsCount"] = r["facts"]["active"]
                l["videoAuto"] = 1
                qa = r.get("qa")
                l["videoQa"] = qa or None
                l["videoApproved"] = now_ms() if (qa and qa.get("ok") and self.g.get("videoAutoApprove") is not False) else 0
                l["videoAutoApproved"] = 1 if l["videoApproved"] else 0
                host = self.g.get("videoHost") or "loom"
                l["video"]["host"] = host
                if r.get("link") and host == "drive":
                    l["loomLink"] = r["link"]
            self.commit()
        items = [{"id": l["id"], "company": l.get("company"), "email": l.get("email"), "web": l.get("web") or "", "first": self.gfirst(l), "business": self.gbiz(l)} for l in todo]
        self.job(f"making {len(items)} ad video(s)", self.ports.ad_video, items, self.voice(), self.meta().get("product"), "drive" if self.g.get("videoHost") == "drive" else "loom", self.narrator(), on_line=on_line)
        self.commit()
        return len([l for l in todo if isinstance(l.get("video"), dict) and l["video"].get("ok")]), stats["lookup_fail"]

    def check_lead_emails(self, lst):
        fresh = [l for l in lst if l.get("email") and not (isinstance(l.get("emailCheck"), dict) and l["emailCheck"].get("at"))][:40]
        if not fresh:
            return

        def on_line(t):
            self.run_line(t)
            r = parse_tag(t, "EMAILCHECK")
            if not r:
                return
            l = self.store.get(r.get("id"))
            if not l:
                return
            l["emailCheck"] = {"valid": r.get("valid"), "level": r.get("level"), "why": r.get("why"), "ai": r.get("ai"), "reason": r.get("reason"), "at": now_ms()}
            if r.get("valid") is False:
                l["ineligible"] = {"why": "email: " + str(r.get("why")), "at": now_ms()}
            self.commit()
        self.job(f"checking {len(fresh)} email(s)", self.ports.email_check, [{"id": l["id"], "email": l["email"], "company": l.get("brandName") or l.get("company"), "web": l.get("web") or ""} for l in fresh],
                 self.g.get("aiEmailCheck") is True, self.g.get("jitbitCheck") is not False, on_line=on_line)
        self.commit()

    def loom_eligible(self, lst):
        if not lst:
            return [], []
        try:
            items = self.ports.preview([{**self.meta_payload(l), "tplId": "meta-loom", "vars": {**self.meta_payload(l)["vars"], "loom_link": "https://www.loom.com/share/preview"}} for l in lst],
                                       self.template_obj(), max(60, len(lst)))
        except Exception:
            return lst, []
        if items is None:
            return lst, []
        by = {str(it.get("id")): it for it in items}
        ok, no = [], []
        for l in lst:
            it = by.get(str(l["id"]))
            if not it or it.get("status") == "ready":
                ok.append(l)
            else:
                no.append((l, it.get("why") or "it would be skipped"))
        return ok, no

    def start_outreach(self, count=None):
        """startOutreach: finds leads when the pool runs low, checks them, makes videos, uploads, drafts, schedules. Returns a result dict."""
        say = self.run_step
        try:
            if not self.g.get("startConfirmed"):
                return {"fatal": "Outreach has not been started yet: press the big button once in the CRM (it asks the questions that decide what may be sent)."}
            self.meta()
            target = int(max(1, min(10, jsnum(count) if jsnum(count) > 0 else jsnum(self.g.get("startTarget"), 10))))
            for l in self.leads:
                if l.get("loomFail") and not l.get("loomLink") and isinstance(l.get("video"), dict) and l["video"].get("ok") and l["video"].get("file"):
                    l.pop("loomFail", None)   # a new run retries videos whose Loom step failed (the helper remembers what is already uploaded, so nothing is uploaded twice)
            say("Checking the helper…")
            es = self.load_es(True)
            if not es:
                return {"fatal": "The helper could not read the email setup."}
            if not es.get("ready"):
                return {"fatal": "Email is not set up yet: " + "; ".join(es.get("problems") or [])}
            self.queue_sync()
            self.loom_status()

            def snap():
                return {"loom": len([l for l in self.leads if l.get("loomLink")]), "drafted": len([l for l in self.leads if l.get("draftedAt") or l.get("emailedAt")])}
            s0 = snap()
            rejected, notes = [], []
            no_leads = False
            if self.loom_connected is False:
                say('Note: Loom is not connected, so videos will be made but not uploaded until you press "Connect Loom".')
                notes.append("Loom is not connected: press \"Connect Loom\" and log in so the videos can be uploaded.")
            made = tries = 0
            no_hc = ad_blocked = False
            while made < target and tries < 14 and not self.abort:
                tries += 1
                pool = self.start_pool()
                if len(pool) < (target - made) * 2 and not no_hc:
                    say(f"Only {len(pool)} lead(s) ready, looking for more in HALCRUS…")
                    got = self.halcrus_more(max(20, (target - made) * 4))
                    if got == -1:
                        say('HALCRUS is not connected. Press "Connect HALCRUS" and log in once. Continuing with the leads I have.')
                        notes.append('HALCRUS is not connected, so no new leads could be found: press "Connect HALCRUS" and log in.')
                        self.alert("HALCRUS needs you", "HALCRUS is not connected, so no new leads can be found. Open the CRM, Meta Ads tab, and press Connect HALCRUS.", "hc-login", 6 * 3600)
                        no_hc = True
                    elif got == -2:
                        say("Could not read HALCRUS right now. Continuing with the leads I have.")
                        notes.append("HALCRUS could not be read just now.")
                        no_hc = True
                    else:
                        say(f"Added {got} new lead(s) from HALCRUS." if got else (f"HALCRUS returned {self.hc_last_found} lead(s) but every one is already in your CRM." if self.hc_last_found else "HALCRUS had no new matching leads on the pages it looked at."))
                    if got == 0:
                        no_hc = True
                    pool = self.start_pool()
                if pool:
                    probe = pool[:max(8, (target - made) * 3)]
                    if any(l.get("email") and not (isinstance(l.get("emailCheck"), dict) and l["emailCheck"].get("at")) for l in probe):
                        say(f"Checking {len(probe)} email(s) with Jitbit and Google AI before making any video…" if self.g.get("aiEmailCheck") is True else
                            (f"Checking {len(probe)} email(s) with Jitbit List Cleaner before making any video…" if self.g.get("jitbitCheck") is not False else f"Checking {len(probe)} email(s) before making any video…"))
                        self.check_lead_emails(probe)
                        pool = self.start_pool()
                    probe2 = pool[:max(8, (target - made) * 3)]
                    fresh = [l for l in probe2 if not (l.get("eligOk") and now_ms() - l["eligOk"] < 864e5)]
                    if fresh:
                        say(f"Making sure {len(fresh)} lead(s) can be emailed before spending time on their videos…")
                        ok, no = self.loom_eligible(fresh)
                        for l in ok:
                            l["eligOk"] = now_ms()
                        for l, why in no:
                            l["ineligible"] = {"why": why, "at": now_ms()}
                            rejected.append({"company": l.get("company"), "reason": "not emailed: " + str(why)})
                        self.commit()
                        if no:
                            say(f"{len(no)} lead(s) skipped before making a video ({'; '.join(list(dict.fromkeys(str(w) for _, w in no))[:2])}).")
                    pool = self.start_pool()
                    if self.email_off():     # forms only: find out which sites have a form BEFORE a video is made for them
                        todo = pool[:max(8, (target - made) * 3)]
                        if [l for l in todo if not l.get("cform")]:
                            say(f"Forms only: looking for a contact form on {len(todo)} site(s) before making any video…")
                            self.cform_find(20, only=todo)
                            pool = self.start_pool()
                batch = pool[:min(5, target - made)]
                if not batch:
                    say("No more leads to make videos for." if made else "There are no leads to make videos for.")
                    if not made:
                        no_leads = True
                    break
                say(f"Making {len(batch)} video(s): {', '.join(str(l.get('company')) for l in batch)}…")
                self.greet(batch)
                ok_n, lookup_fail = self.make_ad_videos(batch)
                made += ok_n
                if lookup_fail >= max(2, len(batch) - ok_n) and not ok_n:
                    say("The Meta Ad Library is not answering properly right now (it may be limiting requests). Stopping here and trying again later; none of these leads were marked as rejected.")
                    notes.append("The Meta Ad Library stopped answering (likely rate limiting). Leads were not rejected; it will try again later.")
                    ad_blocked = True
                    break
                bad = [l for l in batch if isinstance(l.get("video"), dict) and not l["video"].get("ok")]
                for l in bad:
                    rejected.append({"company": l.get("company"), "reason": l["video"].get("reason") or "it failed"})
                say(f"{ok_n} of {len(batch)} made" + (f"; skipped {', '.join(str(l.get('company')) + ' (' + str(l['video'].get('reason') or 'failed') + ')' for l in bad)}" if bad else "") + ".")
            look = len([l for l in self.leads if l.get("videoAuto") and not l.get("videoApproved") and not l.get("emailedAt") and not l.get("loomLink")])
            say(f"Videos ready: {made}." + (f" {look} need a human look before they go on." if look else "") + " Uploading, saving drafts and scheduling…")
            for _ in range(6):      # a pass uploads 3 videos: carry on until the round's videos are all through (or Loom needs a login)
                self.pipeline(True)
                if not self.loom_to_upload() or self.loom_connected is False or self.abort:
                    break
            self.queue_sync()
            act = [x for x in ((getattr(self, "queue_cache", None) or {}).get("items") or []) if x.get("status") == "scheduled"]
            nx = min((x["due_at"] for x in act), default=0)
            rdy = len(self.loom_to_send_base(True))
            if not self.send_auto() and rdy:
                say(f"{rdy} approved email{' is' if rdy == 1 else 's are'} ready. Press \"Send {rdy} approved email{'' if rdy == 1 else 's'}\" when you want {'it' if rdy == 1 else 'them'} to go out.")
            elif act:
                say(f"{len(act)} email(s) scheduled. The first goes out {time.strftime('%a %-I:%M %p', time.localtime(nx))}.")
            else:
                say("Videos are ready. Connect Loom, then it uploads and schedules them." if self.loom_connected is False else "Nothing scheduled yet; anything that needs your approval is listed in the CRM.")
            nc = len([l for l in self.leads if l.get("loomNoComment") and not l.get("emailedAt")])
            ot = len([l for l in self.leads if l.get("loomOther") and not l.get("emailedAt")])
            if nc:
                notes.append(f"{nc} video{' is' if nc == 1 else 's are'} on Loom but the comment with the sign up link was not posted: add it in Loom, or run again.")
            if ot:
                notes.append(f"{ot} video{'' if ot == 1 else 's'} show other links under them in Loom. Only the sign up link is wanted: remove the others in Loom.")
            s1 = snap()
            return {"made": made, "rejected": rejected, "notes": notes, "noLeads": no_leads, "adBlocked": ad_blocked, "uploaded": max(0, s1["loom"] - s0["loom"]), "drafted": max(0, s1["drafted"] - s0["drafted"]),
                    "ready": rdy, "loomDown": self.loom_connected is False}
        except LoopError as e:
            return {"fatal": str(e)}
        except Exception as e:
            traceback.print_exc()
            say("Stopped: " + str(e)[:140])
            return {"fatal": "Stopped by an error: " + str(e)[:200]}
        finally:
            self.commit()

    # ------------------------------------------------------------------ Meta blocked: keep the volume going with Template 1, check, go back to Loom
    FB_CHECK_EVERY = 30 * 60      # seconds between "does the Ad Library answer again?" checks while Template 1 carries the volume

    def template_mode(self):
        """'auto' (Template 1 while Meta blocks this IP's Ad Library searches, Loom when it answers) | 'loom' (Loom only, wait when blocked) | 't1' (Template 1 only)."""
        m = self.g.get("templateMode")
        if m in ("auto", "loom", "t1"):
            return m
        return "loom" if self.g.get("metaFallback") is False else "auto"

    def fallback_enabled(self):
        return self.template_mode() != "loom"

    def fallback_on(self):
        return bool(self.st.get("fb"))

    def enter_fallback(self, why="the Ad Library is not answering", forced=False):
        """The Ad Library stopped answering: instead of waiting, the loop sends Template 1 (contact form first, else the plain first email) and re-checks Meta every 30 minutes."""
        if not self.fallback_enabled() or self.fallback_on():
            return False
        self.st["fb"] = {"since": now_ms(), "next_check": time.time() + self.FB_CHECK_EVERY, "checks": 0, "why": why, "forced": forced}
        self.st["after"] = 0
        self.st["keep_at"] = time.time() + 3
        self.st["cfMsg"] = "Meta is not answering: Template 1 carries the volume until it does."
        self.save_state()
        return True

    def exit_fallback(self):
        """Meta answers again: leads that Template 1 has not touched yet go back to the Loom video; the ones already drafted, queued or sent stay Template 1 (never mixed)."""
        n = 0
        for l in self.leads:
            if l.get("autoT1") and not (l.get("cformSent") or l.get("draftedAt") or l.get("emailedAt") or self.queued(l)):
                l.pop("autoT1", None)
                l.pop("adRetry", None)
                l["adTries"] = 0
                l.pop("t1Skip", None)
                n += 1
        self.st["fb"] = None
        self.st["after"] = 0
        self.st["keep_at"] = time.time() + 3
        self.st["cfMsg"] = f"The Ad Library answers again: back to Loom videos ({n} lead(s) released from Template 1)."
        self.commit()
        self.save_state()

    def apply_mode(self):
        """The person's template switch: t1 = always Template 1, loom = never fall back, auto = follow the Ad Library on this IP."""
        mode, fb = self.template_mode(), self.st.get("fb")
        if mode == "t1" and not fb:
            self.enter_fallback("you chose Template 1 only", forced=True)
        elif mode == "t1" and fb and not fb.get("forced"):
            fb["forced"] = True
            fb["why"] = "you chose Template 1 only"
            self.save_state()
        elif fb and fb.get("forced") and mode != "t1":
            self.exit_fallback()
        elif fb and mode == "loom":
            self.exit_fallback()

    def meta_check_now(self):
        """The button: ask Meta right now (one canary search) and switch template accordingly."""
        try:
            v = self.ports.ads_health()
        except Exception:
            v = None
        self.st["meta_ok"] = {"at": now_ms(), "v": v}
        self.st["health"] = {"at": now_ms(), "v": v}
        mode = self.template_mode()
        if v is True and self.fallback_on() and not (self.st["fb"] or {}).get("forced"):
            self.exit_fallback()
        elif v is False and mode == "auto" and not self.fallback_on():
            self.enter_fallback("the Ad Library is not answering (checked by you)")
        elif v is None:
            self.st["cfMsg"] = "The helper was busy, so Meta was not checked. Try again in a minute."
        self.save_state()

    def fallback_tick(self):
        fb = self.st.get("fb")
        if not fb or fb.get("forced") or time.time() < fb.get("next_check", 0):
            return
        try:
            v = self.ports.ads_health()
        except Exception:
            v = None
        fb["last_check"] = now_ms()
        self.st["meta_ok"] = {"at": now_ms(), "v": v}
        if v is True:
            self.exit_fallback()
        elif v is False:
            fb["checks"] = fb.get("checks", 0) + 1
            fb["next_check"] = time.time() + self.FB_CHECK_EVERY      # still blocked: keep using Template 1
        else:
            fb["next_check"] = time.time() + 300                       # the helper was busy: ask again soon
        self.save_state()

    def t1_email_step(self):
        """Template 1 by email for fallback leads whose site has no usable contact form: first-email draft, then (automatic mode) a scheduled send."""
        if self.email_off():
            return
        todo = [l for l in self.leads if l.get("autoT1") and self.cform_route(l) in ("email", "wait") and not l.get("draftedAt") and not self.queued(l) and not l.get("emailedAt")
                and not l.get("dnc") and l.get("email") and not l.get("t1Skip") and not l.get("replied") and not l.get("bounced")][:10]
        if todo:
            self.greet(todo)

            def on_line(t):
                self.run_line(t)
                r = parse_tag(t, "EMAIL")
                if not r:
                    return
                l = self.store.get(r.get("id"))
                if not l:
                    return
                if r.get("status") == "drafted":
                    l["draftedAt"] = now_ms()
                    l["strategy"] = l.get("strategy") or "proposal"
                    l.pop("skipWhy", None)
                elif r.get("status") == "skip":
                    l["skipWhy"] = r.get("why") or "skipped"
                    if not re.search(r"send window|daily cap|too soon|at its cap", r.get("why") or "", re.I):    # "not now" reasons are tried again later
                        l["t1Skip"] = l["skipWhy"]
                self.commit()
            self.job(f"saving {len(todo)} Template 1 draft(s)", self.ports.email_drafts, [self.meta_payload(l) for l in todo], self.template_obj(), 10, on_line=on_line)
            self.commit()
        self.t1_queue()

    def t1_queue(self):
        """Autopilot sends the drafts: every Template 1 draft (the fallback's, and, when 'send those too, even if I did not watch them' is on, the ones written with the 1 · First email button)
        goes into the send queue, so sending hours and inbox limits decide when."""
        if self.send_auto() and not self.email_off():
            unwatched = bool(self.g.get("loomSendChecked"))
            q = [l for l in self.leads if (l.get("autoT1") or (unwatched and self.strat_of(l) == "proposal")) and l.get("draftedAt") and not l.get("emailedAt") and not self.queued(l)
                 and not l.get("dnc") and not l.get("replied") and not l.get("bounced") and l.get("email") and self.strat_of(l) != "loom"][:60]
            if q:
                try:
                    r = self.ports.queue_add([{"id": l["id"], "to": l.get("email"), "company": l.get("brandName") or l.get("company"), "lead": self.meta_payload(l), "template": self.template_obj(),
                                               "campaign": "meta", "step": 0} for l in q])
                except Exception as e:
                    self.st["pipeMsg"] = "Not scheduled: " + str(e)[:100]
                    r = []
                for x in r or []:
                    l = self.store.get(x.get("id"))
                    if l and x.get("status") == "scheduled":
                        l["queueStatus"] = "scheduled"
                        l["sendDue"] = x.get("due_at")
                    elif l and x.get("status") == "skipped":
                        l["skipWhy"] = x.get("why")
                self.commit()
                self.queue_sync()

    def t1_round(self, need):
        """One round of the fallback: take fresh leads (no video), check their emails, hand them to Template 1; the pipeline sends forms, the email step drafts and schedules the rest."""
        say = self.run_step
        target = int(max(1, min(25, need)))
        taken, no_hc = 0, False
        pool = self.start_pool()
        if len(pool) < target * 2:
            say(f"Only {len(pool)} lead(s) ready, looking for more in HALCRUS…")
            got = self.halcrus_more(max(20, target * 4))
            no_hc = got < 0 or got == 0
            say("HALCRUS is not connected; continuing with the leads I have." if got == -1 else f"Added {max(got, 0)} new lead(s) from HALCRUS.")
            pool = self.start_pool()
        probe = pool[:target * 2]
        if any(l.get("email") and not (isinstance(l.get("emailCheck"), dict) and l["emailCheck"].get("at")) for l in probe):
            say(f"Checking {len(probe)} email(s) before sending Template 1…")
            self.check_lead_emails(probe)
            pool = self.start_pool()
        if self.email_off():
            say("Forms only: checking which sites have a contact form first…")
            for _ in range(3):
                todo = [l for l in pool[:target * 2] if not l.get("cform")]
                if not todo:
                    break
                self.cform_find(20, only=todo)
                pool = self.start_pool()
        batch = pool[:target]
        for l in batch:
            l["autoT1"] = now_ms()
        self.greet(batch)
        self.commit()
        taken = len(batch)
        say(f"Template 1 for {taken} lead(s): contact forms first, then the plain first email…")
        for _ in range(4):      # look for the forms of everything just taken (10 sites per look) before deciding who gets the email
            if not [l for l in self.leads if l.get("autoT1") and not l.get("cform") and not l.get("cformSkip") and (l.get("cformTry") or 0) < 2 and not l.get("emailedAt")]:
                break
            self.cform_find(10)
        self.pipeline(True)
        self.t1_email_step()
        self.commit()
        return {"taken": taken, "noLeads": taken == 0}

    # ------------------------------------------------------------------ the automatic top-up (autoKeep)
    def why(self, msg, hours=0):
        self.st["auto"] = {"at": now_ms(), "why": msg, "until": now_ms() + int(hours * 3600e3) if hours else 0}
        self.save_state()

    def later(self, hours, msg):
        self.st["after"] = now_ms() + int(hours * 3600e3)
        self.why(msg or "Waiting.", hours)

    def auto_keep(self):
        g = self.g
        if not g.get("autoStart") or not g.get("startConfirmed"):
            return
        self.st["beat"] = now_ms()
        if now_ms() < (self.st.get("after") or 0):
            self.save_state()
            return
        try:
            es = self.load_es(True)
            if not es or not es.get("ready"):
                return self.why("Email is not set up or the helper cannot read it.")
            if (es.get("paused") or {}).get("meta"):
                return self.why("The Meta campaign is paused (Settings).")
            try:
                self.meta()
            except LoopError as e:
                return self.why(str(e))
            self.queue_sync()
            self.cform_replies_check(False)
            if g.get("autoFollowUps") is not False:
                if self.loom_queue_followups():
                    self.queue_sync()
            self.apply_mode()
            self.fallback_tick()
            look = len([l for l in self.leads if l.get("videoAuto") and not l.get("videoApproved") and not l.get("emailedAt") and not l.get("dnc") and not l.get("loomLink") and isinstance(l.get("video"), dict) and l["video"].get("ok")])
            stock0 = self.stock_now()
            coming = self.pipeline_backlog()     # videos made or on Loom that the pipeline is still turning into drafts, scheduled emails or form messages
            need = self.stock_goal() - stock0 - coming
            if need <= 0:
                return self.why(f"Goal reached: {stock0} emails saved{f' and {coming} on their way' if coming else ''} ({self.keep_days()} day{'' if self.keep_days() == 1 else 's'}). It starts again when it drops below {self.stock_goal()}.")
            if self.fallback_on():       # Meta is blocking the Ad Library: Template 1 keeps the volume up until it answers again
                fb = self.st["fb"]
                nxt = time.strftime("%-I:%M %p", time.localtime(fb.get("next_check", 0)))
                self.why((f"You chose Template 1 only: contact form first, then the plain email. {stock0} of {self.stock_goal()} saved." if fb.get("forced") else
                          f"Meta is blocking the Ad Library, so Template 1 (contact form first, then the plain email) carries the volume. {stock0} of {self.stock_goal()} saved. Next Meta check about {nxt}; Loom videos resume as soon as it answers."))
                self.run_begin("Automatic top-up (Template 1: Meta is blocking the Ad Library)")
                rr = self.t1_round(min(25, need))
                if self.is_paused():
                    self.run_end("warn", "Paused by you: the unit in hand was finished, nothing further was started.")
                    return self.why("Paused by you. Press Resume when you want it to carry on.")
                self.run_end("ok" if rr["taken"] else "warn", f"Template 1: {rr['taken']} lead(s) taken. Loom resumes when Meta answers (next check about {nxt})." if rr["taken"] else "No usable leads for Template 1 right now.")
                self.queue_sync()
                if rr["noLeads"]:
                    return self.later(1, "Meta is blocking the Ad Library and there are no usable leads for Template 1 right now. Trying again in 1 hour.")
                self.st["keep_at"] = time.time() + 60
                return
            if look >= 5:
                self.alert("Videos need a look", f"{look} videos did not pass the automatic check and are waiting for you in the CRM (Meta Ads tab). Nothing new is being made until you review them.", "look")
                return self.later(1, f"{look} videos need your look (step 3). Nothing new is made until you approve or remove them.")
            self.loom_status()
            if self.loom_connected is False:
                self.alert("Loom needs you", "Loom is not connected, so new videos cannot be uploaded. Open the CRM, Meta Ads tab, and press Connect Loom.", "loom-login", 6 * 3600)
                return self.later(1, "Loom is not connected. Press Connect Loom (step 1).")
            self.why(f"Working: {stock0} of {self.stock_goal()} emails saved, making more.")
            self.run_begin("Automatic top-up")
            rr = self.start_outreach(min(10, need))
            if self.is_paused():
                self.run_end("warn", "Paused by you: the unit in hand was finished, nothing further was started.")
                return self.why("Paused by you. Press Resume when you want it to carry on.")
            self.finish_from(rr)
            self.queue_sync()
            if rr.get("fatal"):
                self.alert("Outreach stopped", "The automatic top-up stopped: " + str(rr["fatal"])[:140], "fatal", 6 * 3600)
                return self.later(1, "Stopped: " + str(rr["fatal"])[:140])
            if rr.get("adBlocked"):
                if not self.meta_connected():
                    self.alert("Meta Ad Library needs you", "Meta is not answering Ad Library searches from this Mac (it says \"No ads match\" even for Nike). Open the CRM, Meta Ads tab, step 1, and press Connect Meta to log in to Facebook once; until then it retries every hour.", "meta-blocked", 12 * 3600)
                if self.enter_fallback("the Ad Library is not answering"):
                    return self.why("The Meta Ad Library is not answering. Switching to Template 1 now (contact form first, then the plain email); it checks Meta every 30 minutes and goes back to Loom videos as soon as it answers.")
                return self.later(1, "The Meta Ad Library is not answering properly (likely rate limiting). Trying again in 1 hour.")
            stock1 = self.stock_now()
            if stock1 <= stock0 and not rr.get("made"):
                if self.hc_connected is False:
                    self.alert("HALCRUS needs you", "HALCRUS is not connected, so no new leads can be found. Open the CRM, Meta Ads tab, and press Connect HALCRUS.", "hc-login", 6 * 3600)
                self.later(3, ("No usable leads left: HALCRUS had nothing new" + (" (it is not connected: press Connect HALCRUS)" if self.hc_connected is False else "") + ". Trying again in 3 hours.") if rr.get("noLeads")
                           else f"That round saved no new emails ({len(rr.get('rejected') or [])} leads were skipped, {rr.get('made') or 0} videos made). Trying again in 3 hours.")
            elif stock1 + self.pipeline_backlog() < self.stock_goal():
                self.why(f"{stock1} of {self.stock_goal()} saved. Starting the next round.")
                self.st["keep_at"] = time.time() + 20   # progress was made and there is still room: go again right away
            else:
                self.why(f"Goal reached: {stock1} emails saved.")
        except Exception as e:
            traceback.print_exc()
            self.why("Stopped by an error: " + str(e)[:140])
            raise
        finally:
            self.commit()

    # ------------------------------------------------------------------ the master pause (use heavy software live)
    def is_paused(self):
        p = self.st.get("pause")
        if p and p.get("until") and now_ms() >= p["until"]:
            self.resume()
            return False
        return bool(p)

    def set_pause(self, minutes=0):
        """Finish the single unit in hand (one form message, one email draft, one upload, one video) and stop: no further unit of the batch starts, the loop starts no new job and the
        send queue sends nothing more. Persisted, so a restart stays paused. Resumes by itself after `minutes` when given."""
        self.st["pause"] = {"at": now_ms(), "until": now_ms() + int(minutes * 60000) if minutes else 0, "released": False}
        self.abort = True
        try:
            self.ports.stop_soft()
        except Exception:
            pass
        self.save_state()
        return {"ok": True, "pause": self.pause_view()}

    def resume(self):
        self.st["pause"] = None
        self.abort = False
        self.st["after"] = 0
        self.st["keep_at"] = time.time() + 3
        self.st["pipe_at"] = 0
        self.save_state()
        self.wake.set()
        return {"ok": True, "pause": None}

    def pause_view(self):
        p = self.st.get("pause")
        if not p:
            return None
        working = bool(self.busy_now)
        try:
            working = working or bool(getattr(self.ports, "job_running", lambda: False)())
        except Exception:
            pass
        return {"state": "pausing" if working else "paused", "at": p.get("at"), "until": p.get("until") or 0, "unit": self.busy_now}

    # ------------------------------------------------------------------ requests from the page's buttons
    def request(self, kind, **kw):
        with self.lock:
            self.requests.append({"kind": kind, "at": time.time(), **kw})
        self.wake.set()
        return True

    def send_ready_now(self):
        if self.email_off():
            self.st["pipeMsg"] = "Email is switched off (forms only), so nothing was scheduled."
            self.save_state()
            return
        self.loom_status()
        n = self.loom_queue_approved(True)
        self.queue_sync()
        act = [x for x in ((getattr(self, "queue_cache", None) or {}).get("items") or []) if x.get("status") == "scheduled"]
        nx = min((x["due_at"] for x in act), default=0)
        self.st["pipeMsg"] = f"{n} email(s) scheduled. The first goes out {time.strftime('%a %-I:%M %p', time.localtime(nx))}." if n else (self.st.get("pipeMsg") or "Nothing was scheduled.")
        self.st["readyNotified"] = 0
        self.save_state()

    def handle(self, r):
        k = r["kind"]
        if k == "run":
            self.st["after"] = 0
            self.run_begin("Loom videos")
            rr = self.start_outreach(r.get("count"))
            self.finish_from(rr)
            if rr.get("adBlocked"):
                self.enter_fallback("the Ad Library is not answering")
            if self.g.get("autoStart"):
                self.st["keep_at"] = time.time() + 15
        elif k == "pipeline":
            self.run_begin("Upload to Loom and save drafts")
            self.run_step("Uploading approved videos to Loom and saving their drafts…")
            self.pipeline(bool(r.get("auto")))
            m = self.st.get("pipeMsg") or "Done."
            self.run_end("warn" if re.search("not connected|could not|Not scheduled|failed", m, re.I) else "ok", m)
        elif k == "forms_find":
            self.st["cfMsg"] = "Looking for contact forms…"
            f = self.cform_find(20)
            self.st["cfMsg"] = f"{f} form(s) found." if f else "None of these sites has a usable contact form (they go by email)."
        elif k == "forms_send":
            self.st["cfMsg"] = "Sending through contact forms…"
            self.save_state()
            n = self.cform_send_auto(True)
            self.st["cfMsg"] = f"{n} sent." if n else (self.st.get("cfMsg") if "Outside" in (self.st.get("cfMsg") or "") else "Nothing was sent.")
        elif k == "replies":
            self.st["cfMsg"] = "Checking for replies…"
            self.cform_replies_check(True)
            self.st["cfMsg"] = "Replies checked."
        elif k == "captcha":
            self.captcha_batch()
        elif k == "send_ready":
            self.send_ready_now()
        elif k == "meta_check":
            self.meta_check_now()
        elif k == "mode":
            self.st["after"] = 0
            self.apply_mode()
            self.st["keep_at"] = time.time() + 2
        elif k == "keep":
            self.st["after"] = 0
            self.st["keep_at"] = time.time() + 2
        self.commit()
        self.save_state()

    # ------------------------------------------------------------------ the loop itself
    def step(self):
        """One scheduling pass. Everything runs in this one thread, so two steps can never collide."""
        self.alive_at = time.time()
        if self.is_paused():
            with self.lock:
                self.requests = []
            p = self.st["pause"]
            if not self.busy_now and not p.get("released"):     # idle now: free the heavy memory once
                p["released"] = True
                try:
                    self.ports.release_memory()
                except Exception:
                    pass
                self.save_state()
            return
        with self.lock:
            reqs, self.requests = self.requests, []
        for r in reqs:
            try:
                self.handle(r)
            except Exception as e:
                traceback.print_exc()
                self.last_error = f"{r['kind']}: {str(e)[:140]}"
                self.run_end("fail", "Something went wrong: " + str(e)[:160])
        now = time.time()
        if not self.store.settings:
            return
        try:
            self.queue_sync()       # what the send queue did (sent, bounced, cancelled) reaches the leads within a tick, page open or not
        except Exception as e:
            self.last_error = "queue: " + str(e)[:120]
        if self.g.get("loomAuto") and now >= self.st.get("pipe_at", 0):
            self.st["pipe_at"] = now + 120
            try:
                self.run_pipeline_pass()
            except Exception as e:
                traceback.print_exc()
                self.last_error = "pipeline: " + str(e)[:140]
        if self.g.get("autoStart") and self.g.get("startConfirmed") and now >= self.st.get("keep_at", 0):
            self.st["keep_at"] = now + 600
            self.auto_keep()

    def run_pipeline_pass(self):
        try:
            self.meta()
        except LoopError:
            return
        self.pipeline(True)

    def _heartbeat(self, th):
        """A long job (making videos takes minutes) keeps the loop thread busy, so it cannot stamp alive_at itself: this does, as long as that thread lives."""
        while th.is_alive():
            self.alive_at = time.time()
            time.sleep(10)

    def run_forever(self):
        threading.Thread(target=self._heartbeat, args=(threading.current_thread(),), daemon=True).start()
        time.sleep(self.start_delay)
        self.st["keep_at"] = max(self.st.get("keep_at", 0), time.time())
        while True:
            try:
                self.step()
                self.errors_in_row = 0
            except Exception as e:
                traceback.print_exc()
                self.errors_in_row += 1
                self.last_error = str(e)[:160]
                if self.errors_in_row >= 3:
                    self.alert("Outreach loop failing", "The helper's outreach loop keeps failing: " + self.last_error + " Check ~/Library/Logs/clipper/helper.log.", "loop-fail", 6 * 3600)
            self.wake.wait(self.tick_s)
            self.wake.clear()

    # ------------------------------------------------------------------ what the page shows
    def routes(self):
        out, t1 = {}, {}
        for l in self.leads:
            r = self.cform_route(l)
            if r != "wait":
                out[str(l["id"])] = r
            reason = self.t1_reason(l)
            if reason:
                t1[str(l["id"])] = reason
        return out, t1

    def channel_stats(self):
        leads = [l for l in self.leads if self.strat_of(l) == "loom" or l.get("cformSent") or l.get("autoT1")]

        def sent_form(l):
            cs = l.get("cformSent")
            return isinstance(cs, dict) and re.search("sent|unsure", cs.get("status") or "")
        pc = lambda a, b: f"{round(100 * a / b)}%" if b else "-"
        em = [l for l in leads if (l.get("emailedAt") or l.get("queueStatus") == "sent") and not sent_form(l)]
        em_rep = [l for l in em if l.get("replied") and not l.get("cformReply")]
        em_bnc = [l for l in leads if self.is_bounced(l)]
        fm = [l for l in leads if sent_form(l)]
        fm_rep = [l for l in fm if l.get("cformReply")]
        return {"t1Sent": len([l for l in leads if l.get("cformTpl") == "t1" and sent_form(l)]), "t1Rep": len([l for l in leads if l.get("cformTpl") == "t1" and l.get("cformReply")]),
                "emAuto": len([l for l in em if l.get("autoReply") and not l.get("replied")]), "fmAuto": len([l for l in fm if l.get("cformAuto") and not l.get("cformReply")]),
                "emSent": len(em), "emRep": len(em_rep), "emRate": pc(len(em_rep), len(em)), "emBnc": len(em_bnc), "fmSent": len(fm), "fmRep": len(fm_rep), "fmRate": pc(len(fm_rep), len(fm)),
                "waiting": len([l for l in leads if self.cform_route(l) == "human"]), "auto": len([l for l in leads if self.cform_route(l) == "form-auto"]),
                "recovered": len([l for l in fm if self.is_bounced(l)])}

    def status(self, light=False):
        try:
            self.load_es()
        except Exception:
            pass
        with self.store.lock:
            try:
                stock = {"now": self.stock_now(), "goal": self.stock_goal() if self.es else 0, "keepDays": self.keep_days(), "dailyCap": self.daily_cap() if self.es else 0}
            except Exception:
                stock = {"now": 0, "goal": 0, "keepDays": 2, "dailyCap": 0}
            out = {"ok": True, "now": now_ms(), "alive_at": int(self.alive_at * 1000), "started_at": int(self.t_started * 1000), "busy": self.busy_now, "auto": self.st.get("auto"), "beat": self.st.get("beat"),
                   "after": self.st.get("after"), "run": self.st.get("run"), "pipeMsg": self.st.get("pipeMsg"), "cfMsg": self.st.get("cfMsg"), "stock": stock,
                   "loom": self.loom_connected, "halcrus": self.hc_connected, "meta": self.meta_connected(), "error": self.last_error, "lastPipeline": self.st.get("last_pipeline"),
                   "store": self.store.snapshot(), "queued_requests": len(self.requests), "seeded": bool(self.store.settings), "fallback": self.st.get("fb"), "meta_ok": self.st.get("meta_ok"), "mode": self.template_mode(), "pause": self.pause_view(), "email_off": self.email_off()}
            if not light:
                out["routes"], out["t1"] = self.routes()
                out["stats"] = self.channel_stats()
                out["counts"] = {"toUpload": len(self.loom_to_upload()), "toDraft": len(self.loom_to_draft()), "toSend": len(self.loom_to_send_base(True)), "waitingHuman": self.waiting_human()}
            return out
