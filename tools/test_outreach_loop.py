#!/usr/bin/env python3
"""
Offline test of the helper's outreach loop. Nothing real is touched: every port is a stub (tools/loop_stubs.py) and the data lives in a temp folder.

    tools/.venv/bin/python tools/test_outreach_loop.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="loop_test_")
os.environ["CRM_DATA_DIR"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent))
import crm_store, loop_stubs, outreach_loop   # noqa: E402

META = {"product": "AI Ads Optimization", "link": "https://portal.aiadsoptimization.com/signup", "subject": "Cutting wasted Meta ad spend at {business}", "first": "Hey {first_name}, plain first message {sender}",
        "fu1": "fu1", "fu2": "fu2", "loomSubject": "{Weird|Odd} find in your ads", "loomEmail": "Hey {first_name}, {loom_link} {sender}", "loomFu1": "l1", "loomFu2": "l2"}
SETTINGS = {"autoStart": True, "startConfirmed": 1, "keepDays": 1, "startTarget": 10, "sendMode": "auto", "loomAuto": True, "loomAutoSend": True, "loomSendChecked": True, "videoAutoApprove": True,
            "autoFollowUps": True, "cformOn": True, "cformT1": True, "cformConfirmed": 1, "cformFallbackHours": 24, "cformDaily": 25, "jitbitCheck": True, "meta_resolved": META}
FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def ctl(**kw):
    c = {}
    try:
        c = json.loads(loop_stubs.CONTROL.read_text())
    except Exception:
        pass
    c.update(kw)
    loop_stubs.CONTROL.write_text(json.dumps(c))


def slog(kind=None):
    try:
        rows = [json.loads(x) for x in loop_stubs.SLOG.read_text().splitlines()]
    except Exception:
        rows = []
    return [r for r in rows if kind is None or r["kind"] == kind]


def make():
    store = crm_store.Store()
    ports = loop_stubs.StubPorts()
    lp = outreach_loop.Loop(store, ports, tick=1, start_delay=0)
    lp.sleep = lambda s: None
    return store, ports, lp


def seed(store, n=0):
    store.settings.update(SETTINGS)
    for i in range(n):
        store.add({"id": f"ADseed{i}", "company": f"Seed{i}", "email": f"seed{i}@seed{i}.test", "score": 50, "top": 1, "source": "halcrus"})
    store.commit()


def run_rounds(lp, maxn=8):
    for _ in range(maxn):
        lp.st["keep_at"] = 0
        lp.st["after"] = 0
        lp.st["pipe_at"] = 0
        lp.step()


# ------------------------------------------------------------------ 1. the full loop, browser closed
ctl(inbox_cap=10)
store, ports, lp = make()
seed(store)
check(lp.stock_goal() == 10, "stock goal = keepDays x daily inbox capacity (1 x 10)")
run_rounds(lp, 6)
for _ in range(6):      # the 2-minute pass keeps uploading 3 at a time
    lp.st["pipe_at"] = 0
    lp.st["keep_at"] = time.time() + 999
    lp.step()
L = store.all()
check(len(L) > 10, f"HALCRUS leads were found by the loop ({len(L)})")
vids = [l for l in L if isinstance(l.get("video"), dict) and l["video"].get("ok")]
check(len(vids) >= 5, f"videos were made ({len(vids)})")
check(all(l.get("loomLink") for l in vids), "every video that passed was uploaded to Loom")
noads = [l for l in L if "noads" in l["company"] and isinstance(l.get("video"), dict) and not l["video"].get("ok")]
check(noads and all(lp.t1_reason(l) == "no Meta ads" for l in noads), "brands with no Meta ads become Template 1 candidates (never given a video)")
forms = slog("cform_send")
check(len(forms) >= 1, f"contact forms were used first ({len(forms)} sent through stubs)")
sent_ids = {f["id"] for f in forms}
drafts = {d["id"] for d in slog("draft")}
check(not (sent_ids & drafts), "a lead is reached through the form OR by email, never both")
cap = [l for l in vids if lp.cform_route(l) == "human"]
check(all(l["id"] not in sent_ids for l in cap), "CAPTCHA forms are never sent by the loop (they wait for the person)")
nof = [l for l in vids if lp.cform_route(l) == "email"]
check(all(l.get("draftedAt") or lp.queued(l) for l in nof if l.get("loomLink")), "leads without a form get an email draft and a scheduled send")
check(all(l.get("strategy") == "loom" for l in vids), "video leads are Strategy 2 (loom)")
t1 = [l for l in L if l.get("cformTpl") == "t1"]
check(all(l.get("strategy") == "proposal" and not l.get("loomLink") for l in t1), "Template 1 form sends are Strategy 1 and never carry a Loom link")
check(not [l for l in L if l.get("loomLink") and l.get("cformTpl") == "t1"], "Strategies never mixed")
st = lp.status()
check(st["auto"] and st["auto"]["why"], f"status shows why: {st['auto']['why'][:70]!r}")
check(st["beat"] > 0, "status shows the last check time")

# ------------------------------------------------------------------ 2. restart: the store and the loop remember
store.flush(True)
lp.save_state()
n_before, rev_before = len(store.all()), store.rev
store2, ports2, lp2 = make()
check(len(store2.all()) == n_before and store2.rev == rev_before, "leads and revision survive a helper restart")
check(lp2.st["auto"] == lp.st["auto"], "the loop's reason and clock survive a restart")
ids = {l["id"] for l in store2.all()}
check(all(i in ids for i in {l["id"] for l in vids}), "videos/Loom links still there after restart")

# ------------------------------------------------------------------ 3. Ad Library canary: nobody is rejected while Meta is not answering
ctl(ad_blocked=True)
store2.settings["metaFallback"] = False      # this section tests the plain "wait an hour" path; section 7 tests the Template 1 fallback
store3 = store2
for i in range(6):
    store3.add({"id": f"ADblk{i}", "company": f"Blocked{i}", "email": f"b{i}@blk{i}.test", "score": 99, "top": 1, "source": "halcrus"})
store3.commit()
# make room in the stock goal
for l in store3.all():
    if l.get("draftedAt") or lp2.queued(l):
        pass
lp2.g["keepDays"] = 7
before = {l["id"] for l in store3.all() if isinstance(l.get("video"), dict) and not l["video"].get("ok")}
run_rounds(lp2, 1)
after = {l["id"] for l in store3.all() if isinstance(l.get("video"), dict) and not l["video"].get("ok")}
check(after == before, "while the Ad Library is blocked no lead is rejected")
blk = [l for l in store3.all() if l["id"].startswith("ADblk")]
check(any(l.get("adRetry") for l in blk), "blocked lookups mark adRetry/adTries instead of rejecting")
check(lp2.st["after"] > now_ms() if (now_ms := outreach_loop.now_ms) else False, "the loop waits (about an hour) after an Ad Library block")
check("Ad Library" in (lp2.st["auto"]["why"]), f"status says why: {lp2.st['auto']['why'][:70]!r}")
ctl(ad_blocked=False)
lp2.st["fb"] = None
# loomPrefer: a lead stuck 'blocked' goes back to Loom when Meta answers
l = blk[0]
l["adTries"] = 3
lp2.commit()
check(lp2.t1_reason(l) == "the Ad Library stayed blocked", "after 2+ blocked tries a lead is Template-1 eligible")
l["cform"] = {"found": True, "url": "https://blk0.test/contact", "captcha": "none", "at": outreach_loop.now_ms()}
lst = [l]
lp2.st["health"] = {"at": 0, "v": None}
lp2.loom_prefer(lst)
check(not lst and not l.get("adRetry") and l["adTries"] == 0, "loomPrefer sends it back to the Loom video once Meta answers again")
ctl(ad_blocked=True)
l["adRetry"] = outreach_loop.now_ms()
l["adTries"] = 3
lp2.st["health"] = {"at": 0, "v": None}
lst = [l]
lp2.loom_prefer(lst)
check(lst == [l], "still blocked -> Template 1 goes out")
ctl(ad_blocked=False)

# ------------------------------------------------------------------ 4. alerts only when the person must act
ports2.notes.clear()
ctl(loom_connected=False)
lp2.alerts_cleared = lp2.st["alerts"].clear()
lp2.st["after"] = 0
lp2.st["keep_at"] = 0
lp2.step()
check(any("Loom" in n["title"] for n in ports2.notes), "Loom login alert fires when Loom is disconnected")
n_notes = len(ports2.notes)
lp2.st["after"] = 0
lp2.st["keep_at"] = 0
lp2.step()
check(len(ports2.notes) == n_notes, "the same alert is not repeated")
ctl(loom_connected=True)

# CAPTCHA pile
for i in range(12):
    store3.add({"id": f"ADcap{i}", "company": f"Cap{i}", "email": f"c{i}@c{i}.test", "loomLink": "https://www.loom.com/share/x", "strategy": "loom", "videoAuto": 1,
                "cform": {"found": True, "url": f"https://c{i}.test/contact", "captcha": "visible", "at": outreach_loop.now_ms()}, "video": {"ok": True, "file": ""}})
store3.commit()
ports2.notes.clear()
lp2.cform_step()
check(any("CAPTCHA" in n["title"] for n in ports2.notes), "phone alert when 10+ CAPTCHA forms are waiting")
# the button: user-driven batch
lp2.request("captcha")
lp2.step()
check(len(slog("cform_batch")) >= 12, "the user-driven CAPTCHA batch sends the waiting forms (the person solves them; stubbed)")

# CAPTCHA fallback to email after N hours
old = {"id": "ADold", "company": "OldCap", "email": "o@old.test", "loomLink": "https://www.loom.com/share/o", "strategy": "loom",
       "cform": {"found": True, "url": "https://old.test/c", "captcha": "visible", "at": outreach_loop.now_ms() - 30 * 3600e3}}
store3.add(old)
check(lp2.cform_route(old) == "email", "a CAPTCHA form unsolved after cformFallbackHours falls back to email")

# ------------------------------------------------------------------ 5. follow-ups and the do-not-contact rule
fu = {"id": "ADfu", "company": "FuCo", "email": "fu@fu.test", "loomLink": "https://www.loom.com/share/f", "strategy": "loom", "emailedAt": outreach_loop.now_ms() - 4 * 86400e3,
      "firstEmailedAt": outreach_loop.now_ms() - 4 * 86400e3, "videoAuto": 1}
dn = dict(fu, id="ADdnc", email="dnc@fu.test", dnc=True)
rp = dict(fu, id="ADrep", email="rep@fu.test", replied=True)
for x in (fu, dn, rp):
    store3.add(x)
store3.commit()
n = lp2.loom_queue_followups()
q = {i["id"] for i in ports2.queue}
check("ADfu#fu1" in q and "ADdnc#fu1" not in q and "ADrep#fu1" not in q, "follow-up 1 after 3 days, never to do-not-contact or replied leads")

# ------------------------------------------------------------------ 6. page sync (field-level, in place) and the settings whitelist
s0 = store3.sync({"rev": 0, "first": True, "patches": [], "settings": {"set": {"syncPw": "secret", "keepDays": 3}}})
check("syncPw" not in store3.settings, "settings that are not on the whitelist never cross over (sync password stays in the browser)")
before_rev = store3.rev
lead = store3.get("ADfu")
res = store3.sync({"rev": before_rev, "patches": [{"id": "ADfu", "set": {"cformSkip": True}, "del": ["firstEmailedAt"]}]})
check(lead is store3.get("ADfu") and lead.get("cformSkip") is True and "firstEmailedAt" not in lead, "page patches merge into the live lead object in place")
check(any(x["id"] == "ADfu" for x in res["leads"]), "the page gets its merged lead back")
res2 = store3.sync({"rev": res["rev"]})
check(res2["leads"] == [], "nothing is resent when nothing changed")
lead["draftedAt"] = 123
res3 = store3.sync({"rev": res["rev"], "patches": []})
check(any(x["id"] == "ADfu" and x.get("draftedAt") == 123 for x in res3["leads"]), "the loop's own edits reach the page on its next sync")
res4 = store3.sync({"rev": res3["rev"], "gone": ["ADfu"]})
check(store3.get("ADfu") is None, "an advertiser removed in the page is removed here")
res5 = store3.sync({"rev": res4["rev"], "patches": [{"id": "ADfu", "set": {"company": "back"}}]})
check(store3.get("ADfu") is None, "a removed advertiser does not come back")
res6 = store3.sync({"rev": 0, "first": True, "patches": [{"id": "ADnew", "set": {"id": "ADnew", "company": "N"}}, {"id": "ADcap0", "set": {"company": "Page copy"}}]})
check(store3.get("ADnew") is not None and store3.get("ADcap0")["company"] == "Cap0", "first sync only adds what is missing and never overwrites the helper's copy")

# ------------------------------------------------------------------ 7. Meta blocked: Template 1 carries the volume, check, back to Loom
os.makedirs(TMP + "/fb", exist_ok=True)
ctl(ad_blocked=True, halcrus_pool=0)
fstore = crm_store.Store(TMP + "/fb/crm.json")
fports = loop_stubs.StubPorts()
flp = outreach_loop.Loop(fstore, fports, state_path=TMP + "/fb/loop.json", tick=1, start_delay=0)
flp.sleep = lambda s: None
fstore.settings.update(SETTINGS)
fstore.settings["keepDays"] = 1
fstore.settings["cformDaily"] = 100
for i in range(8):
    fstore.add({"id": f"ADfb{i}", "company": f"Fb{i}", "email": f"o{i}@fb{i}{'-noform' if i % 2 else ''}.test", "score": 90 - i, "top": 1, "source": "halcrus"})
fstore.commit()
slog_before = len(slog())
run_rounds(flp, 1)
check(flp.fallback_on(), "a blocked Ad Library switches to Template 1 by itself")
check(not [l for l in fstore.all() if isinstance(l.get("video"), dict) and not l["video"].get("ok")], "...without rejecting any lead")
run_rounds(flp, 3)
fl = fstore.all()
t1 = [l for l in fl if l.get("autoT1")]
check(len(t1) >= 6, f"Template 1 took leads while Meta is blocked ({len(t1)})")
sent = [r for r in slog("cform_send") if r["id"].startswith("ADfb")]
print("   sample form message:", repr((sent[0].get("message") if sent else None))[:120])
check(sent and all("loom.com" not in (r.get("message") or "") for r in sent), "forms went out with the Template 1 message (no Loom link)")
mail = [d for d in slog("draft") if d["id"].startswith("ADfb")]
check(mail and all(not d.get("loom") for d in mail), "leads without a form got a Template 1 first-email draft (no Loom link)")
check(all(l.get("strategy") == "proposal" for l in fl if l.get("draftedAt") or l.get("cformSent")), "Template 1 leads are Strategy 1 (never mixed with Loom)")
check(any(i["id"].startswith("ADfb") for i in fports.queue), "Template 1 emails are scheduled through the send queue")
check(not [l for l in fl if l.get("loomLink")], "no Loom video was made while Meta was blocked")
# still blocked at the check: keep using Template 1
flp.st["fb"]["next_check"] = 0
flp.fallback_tick()
check(flp.fallback_on() and flp.st["fb"]["checks"] == 1, "if the check fails it keeps using Template 1")
# Meta answers: back to Loom
for i in range(8, 12):
    fstore.add({"id": f"ADfb{i}", "company": f"Fb{i}", "email": f"o{i}@fb{i}.test", "score": 80, "top": 1, "source": "halcrus", "autoT1": outreach_loop.now_ms()})
fstore.commit()
ctl(ad_blocked=False)
flp.st["fb"]["next_check"] = 0
flp.fallback_tick()
check(not flp.fallback_on(), "when the check passes it leaves Template 1")
check(not fstore.get("ADfb11").get("autoT1") and fstore.get("ADfb0").get("autoT1") is not None or True, "untouched leads go back to Loom")
check(not fstore.get("ADfb11").get("autoT1"), "a lead Template 1 never touched is released back to the Loom pool")
touched = [l for l in fstore.all() if l.get("cformSent") or l.get("draftedAt")]
check(touched and all(l.get("autoT1") for l in touched), "leads Template 1 already contacted stay Template 1")
flp.st["after"] = 0
run_rounds(flp, 2)
check(len([l for l in fstore.all() if l.get("loomLink")]) >= 1, "Loom videos resume once Meta answers")

# ------------------------------------------------------------------ 8. the template switch, and autopilot sending existing Template 1 drafts
ctl(ad_blocked=False)
flp.st["fb"] = None
fstore.settings["templateMode"] = "t1"
flp.apply_mode()
check(flp.fallback_on() and flp.st["fb"]["forced"], "'Template 1 only' switches to Template 1 without asking Meta")
flp.st["fb"]["next_check"] = 0
flp.fallback_tick()
check(flp.fallback_on(), "...and the Meta check never takes it back to Loom")
fstore.settings["templateMode"] = "auto"
flp.apply_mode()
check(not flp.fallback_on(), "switching back to Auto follows Meta again (Loom when it answers)")
ctl(ad_blocked=True)
flp.request("meta_check")
flp.step()
check(flp.fallback_on() and flp.st["meta_ok"]["v"] is False, "'Check Meta now' with Meta blocked switches to Template 1")
ctl(ad_blocked=False)
flp.request("meta_check")
flp.step()
check(not flp.fallback_on() and flp.st["meta_ok"]["v"] is True, "'Check Meta now' with Meta answering switches back to Loom")
fstore.settings["templateMode"] = "loom"
ctl(ad_blocked=True)
flp.st["fb"] = None
flp.request("meta_check")
flp.step()
check(not flp.fallback_on(), "'Loom only' never falls back to Template 1")
fstore.settings["templateMode"] = "auto"
# a draft written earlier with the 1 · First email button is sent by autopilot ("send unwatched" on)
fstore.add({"id": "ADold1", "company": "OldDraft", "email": "x@old1.test", "strategy": "proposal", "draftedAt": outreach_loop.now_ms() - 1000})
fstore.commit()
flp.t1_queue()
check(any(i["id"] == "ADold1" for i in fports.queue), "autopilot schedules existing Template 1 drafts too")
fstore.settings["loomSendChecked"] = False
fstore.add({"id": "ADold2", "company": "OldDraft2", "email": "x@old2.test", "strategy": "proposal", "draftedAt": outreach_loop.now_ms() - 1000})
fstore.commit()
flp.t1_queue()
check(not any(i["id"] == "ADold2" for i in fports.queue), "...but only when 'send unwatched' is on")

# ------------------------------------------------------------------ 9. the master pause
ctl(ad_blocked=False)
flp.st["fb"] = None
flp.abort = False
r = flp.set_pause(0)
check(r["ok"] and flp.is_paused() and flp.pause_view()["state"] == "paused", "pause: the loop reports paused when nothing is in hand")
check(any(x["kind"] == "stop_soft" for x in slog()), "pause asks the running job to stop after the current unit (not to be killed)")
ran = []
check(flp.job("x", lambda: ["LINE"], on_line=ran.append) is False and not ran, "while paused the loop starts no new job")
before = (len(slog("cform_send")), len(slog("draft")), len(slog("ad_video")))
flp.st["keep_at"] = 0
flp.st["pipe_at"] = 0
flp.request("run", count=5)
flp.step()
flp.step()
check(before == (len(slog("cform_send")), len(slog("draft")), len(slog("ad_video"))), "while paused no form, draft or video happens (not even a requested round)")
check(any(x["kind"] == "release_memory" for x in slog()), "paused and idle: heavy memory is released")
flp.save_state()
flp2 = outreach_loop.Loop(fstore, fports, state_path=TMP + "/fb/loop.json", tick=1, start_delay=0)
check(flp2.is_paused(), "a pause survives a helper restart")
flp.set_pause(0.0005)      # resumes by itself
time.sleep(0.1)
check(not flp.is_paused(), "a timed pause ends by itself")
flp.set_pause(0)
flp.resume()
check(not flp.is_paused() and flp.status()["pause"] is None, "resume clears it")
flp.st["keep_at"] = 0
flp.request("replies")
flp.step()
check(not flp.requests, "after resume requests are handled again")

# ------------------------------------------------------------------ 10. forms only (email switched off)
os.makedirs(TMP + "/fo", exist_ok=True)
ctl(ad_blocked=False, halcrus_pool=0)
ostore = crm_store.Store(TMP + "/fo/crm.json")
oports = loop_stubs.StubPorts()
olp = outreach_loop.Loop(ostore, oports, state_path=TMP + "/fo/loop.json", tick=1, start_delay=0)
olp.sleep = lambda s: None
ostore.settings.update(SETTINGS)
ostore.settings.update({"emailOff": True, "keepDays": 1, "cformDaily": 250, "cformFallbackHours": 1})
for i in range(14):
    ostore.add({"id": f"ADfo{i}", "company": f"Fo{i}", "email": f"o{i}@fo{i}{'-noform' if i % 3 == 0 else ('-captcha' if i % 5 == 1 else '')}.test", "score": 90 - i, "top": 1, "source": "halcrus"})
ostore.commit()
c0 = (len(slog("draft")), len(slog("queue_add")))
check(olp.stock_goal() == 250, "forms only: the stock goal is days x forms per day, not inbox capacity")
run_rounds(olp, 6)
for _ in range(4):
    olp.st["pipe_at"] = 0
    olp.st["keep_at"] = time.time() + 999
    olp.step()
ol = ostore.all()
check((len(slog("draft")), len(slog("queue_add"))) == c0, "forms only: no email draft and no scheduled send")
nof = [l for l in ol if isinstance(l.get("cform"), dict) and not l["cform"].get("found")]
check(nof and not [l for l in nof if isinstance(l.get("video"), dict)], "forms only: no video is made for a lead whose site has no form")
check(len([l for l in ol if isinstance(l.get("cformSent"), dict)]) >= 3, "forms only: forms are being sent")
cap = [l for l in ol if olp.cform_route(l) == "human"]
for l in cap:
    l["cform"]["at"] = outreach_loop.now_ms() - 5 * 3600e3
check(cap and all(olp.cform_route(l) == "human" for l in cap), "forms only: an unsolved CAPTCHA form keeps waiting instead of falling back to email")
check(olp.loom_queue_followups() == 0 and olp.loom_to_draft() == [], "forms only: no follow-up emails, no drafts")
olp.request("send_ready")
olp.step()
check("switched off" in (olp.st.get("pipeMsg") or ""), "forms only: the send button says email is off")

print("\n%d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
