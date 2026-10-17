#!/usr/bin/env python3
"""
The CRM's own scheduled-send queue. An approved email is given a send time and sent by the helper itself at that time, whether or not the CRM page is open.

  - Inside your sending hours (and with inbox capacity left) the first slot is right now; outside them it is the start of the next sending day.
  - Slots are spread by your own spacing (min_delay_seconds to max_delay_seconds) and never exceed what your inboxes may still send that day (warm-up and daily caps),
    counting what is already scheduled for that day.
  - The queue is the "memory" of when each email goes out. It is kept in tools/send_queue.json (git-ignored) until the email is really sent, so a restart loses nothing:
        scheduled -> sending -> sent
        canceled   you cancelled it (it can be rescheduled)
        failed     the mail server refused it 3 times (it can be rescheduled)
        bounced    it went out and came back undeliverable (never retried: the address is bad)
        skipped    a rule stopped it for good (do-not-contact list, already replied, emailed in the last 90 days, ...)
  - When one drops out (cancel, bounce, skip, failure) the emails behind it move up into the freed slot; a send that could not go because the day was over or the inbox was at
    its cap is moved to the next free slot, later today or the next sending day. Sent entries are kept 7 days for the record, then removed.
  - Everything is still checked by cold_email.run at the moment of sending (window, caps, do-not-contact list, no repeat within 90 days).
"""
import json
import random
import re
import threading
import time
from datetime import datetime, timedelta

import cold_email

HERE = cold_email.HERE
QUEUE = HERE / "send_queue.json"
LOCK = threading.RLock()
KEEP_DAYS = 7
MAX_ATTEMPTS = 3
RETRY_AFTER = 30 * 60  # a refused send is tried again after at least this long
TRANSIENT = re.compile(r"send window|daily cap|too soon", re.I)  # skip reasons that only mean "not now"
ACTIVE = ("scheduled", "sending")
DONE = ("sent", "canceled", "failed", "bounced", "skipped")


def _load():
    try:
        return json.loads(QUEUE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items):
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
    tmp.replace(QUEUE)


def _event(it, ev, note=""):
    it.setdefault("history", []).append({"ts": int(time.time()), "event": ev, "note": str(note)[:200]})


def _fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%a %-I:%M %p")


def _hours(cfg):
    w = cfg.get("send_window") or {}
    return int(w.get("start_hour", 9)), int(w.get("end_hour", 17))


def plan_slots(n, items, cfg, log, now=None, after=None):
    """The next n send times (timestamps), earliest first, given what is already scheduled in `items`."""
    now = now or datetime.now()
    sh, eh = _hours(cfg)
    lo, hi = float(cfg.get("min_delay_seconds", 60)), float(cfg.get("max_delay_seconds", 90))
    caps = cold_email.capacity(cfg, log)
    today_left = sum(max(0, c["cap"] - c["sent_today"]) for c in caps)
    full = sum(c["cap"] for c in caps)
    active = [datetime.fromtimestamp(i["due_at"]) for i in items if i.get("status") in ACTIVE and i.get("due_at")]
    floor = datetime.fromtimestamp(after) if after else now
    out = []
    for off in range(0, 45):
        d = now.date() + timedelta(days=off)
        start = datetime.combine(d, datetime.min.time()) + timedelta(hours=sh)
        end = start + timedelta(hours=max(0, eh - sh))
        mine = [t for t in active if t.date() == d]
        room = (today_left if off == 0 else full) - len(mine)
        t = max(start if off else max(start, now), floor)
        if off and t == start:
            t += timedelta(seconds=random.uniform(5, 25))
        if mine:
            t = max(t, max(mine) + timedelta(seconds=random.uniform(lo, hi)))
        while room > 0 and len(out) < n and t < end:
            out.append(t.timestamp())
            room -= 1
            t += timedelta(seconds=random.uniform(lo, hi))
        if len(out) >= n:
            break
    return out


def add(entries, now=None):
    """entries: [{id, to, company, lead, template, campaign, step}]. Returns [{id, status, due_at, why}] telling what happened to each."""
    cfg, log = cold_email.load_config(), cold_email.read_log()
    out = []
    with LOCK:
        items = _load()
        for e in entries:
            to = str(e.get("to") or (e.get("lead") or {}).get("email") or "").lower().strip()
            key = str(e.get("id"))
            if not to or "@" not in to:
                out.append({"id": key, "status": "skipped", "why": "no email address"})
                continue
            dup = next((i for i in items if i["status"] in ACTIVE + ("sent",) and (i["id"] == key or i["to"] == to) and i.get("step", 0) == int(e.get("step") or 0)), None)
            if dup:
                out.append({"id": key, "status": dup["status"], "due_at": dup.get("due_at"), "why": "already in the queue"})
                continue
            slot = plan_slots(1, items, cfg, log, now)
            if not slot:
                out.append({"id": key, "status": "skipped", "why": "no inbox has any capacity in the next 45 days"})
                continue
            it = {"id": key, "to": to, "company": str(e.get("company") or "")[:80], "lead": e.get("lead") or {}, "template": e.get("template") or {},
                  "campaign": str(e.get("campaign") or "meta"), "step": int(e.get("step") or 0), "status": "scheduled", "due_at": slot[0], "attempts": 0,
                  "created_at": int(time.time()), "history": []}
            _event(it, "scheduled", f"to send {_fmt(slot[0])}")
            items.append(it)
            out.append({"id": key, "status": "scheduled", "due_at": slot[0]})
        _save(items)
    return out


def _compact(items, cfg, log, now=None):
    """After something drops out: moves later emails up into the free slots (never later than they were)."""
    sched = sorted([i for i in items if i["status"] == "scheduled"], key=lambda i: i["due_at"])
    if not sched:
        return
    rest = [i for i in items if i["status"] != "scheduled"]
    slots = plan_slots(len(sched), rest, cfg, log, now)
    for it, s in zip(sched, slots):
        if s < it["due_at"] - 5:
            _event(it, "moved up", f"{_fmt(it['due_at'])} -> {_fmt(s)}")
            it["due_at"] = s


def cancel(key, note="cancelled by you", now=None):
    cfg, log = cold_email.load_config(), cold_email.read_log()
    with LOCK:
        items = _load()
        it = next((i for i in items if i["id"] == str(key) and i["status"] in ACTIVE), None)
        if not it:
            return False
        it["status"] = "canceled"
        _event(it, "canceled", note)
        _compact(items, cfg, log, now)
        _save(items)
    return True


def reschedule(key, when=None, now=None):
    """Puts a cancelled, failed or scheduled email back in the queue at `when` (timestamp) or the next free slot."""
    cfg, log = cold_email.load_config(), cold_email.read_log()
    with LOCK:
        items = _load()
        it = next((i for i in items if i["id"] == str(key) and i["status"] in ("canceled", "failed", "scheduled")), None)
        if not it:
            return None
        others = [i for i in items if i is not it]
        slot = [float(when)] if when else plan_slots(1, others, cfg, log, now)
        if not slot:
            return None
        it.update(status="scheduled", due_at=slot[0], attempts=0)
        _event(it, "rescheduled", f"to send {_fmt(slot[0])}")
        _save(items)
        return slot[0]


def _send_one(it):
    """Sends one queued email through the same engine as a manual send (window, caps, do-not-contact list and 90-day rule are checked again)."""
    res = cold_email.run([it["lead"]], it["template"], "send", limit=1, ignore_window=False, step=int(it.get("step") or 0), log_fn=lambda m: None,
                         sleep=lambda s: None, campaign=it.get("campaign", "meta"))
    return res[0] if res else {"status": "failed", "why": "nothing was sent"}


def recover():
    """After a restart: an email left in 'sending' is 'sent' if the send log shows it went out, otherwise it goes back to 'scheduled'."""
    log = cold_email.read_log()
    with LOCK:
        items = _load()
        changed = False
        for it in items:
            if it["status"] != "sending":
                continue
            since = it.get("sending_at", 0)
            hit = next((e for e in log if e.get("to", "").lower() == it["to"] and e.get("status") == "sent" and e.get("ts", 0) >= since - 5), None)
            if hit:
                it.update(status="sent", sent_at=int(hit["ts"]), message_id=hit.get("message_id"), **{"from": hit.get("from")})
                _event(it, "sent", "found in the send log after a restart")
            else:
                it["status"] = "scheduled"
                it["due_at"] = max(it["due_at"], time.time())
                _event(it, "back in the queue", "the helper restarted while sending")
            changed = True
        if changed:
            _save(items)


def tick(sender=None, now=None, paused=None):
    """Sends at most ONE due email (skipping campaigns that `paused(campaign)` says are paused). Returns a dict saying what happened, or None when nothing was due."""
    cfg, log = cold_email.load_config(), cold_email.read_log()
    now_dt = now or datetime.now()
    with LOCK:
        items = _load()
        due = sorted([i for i in items if i["status"] == "scheduled" and i["due_at"] <= now_dt.timestamp() and not (paused and paused(i.get("campaign")))], key=lambda i: i["due_at"])
        if not due:
            return None
        if not cold_email.in_window(cfg, now_dt):  # the time came but the day is over (or the helper was off): move them to the next free slots
            for it in due:
                s = plan_slots(1, [i for i in items if i is not it], cfg, log, now_dt)
                if s:
                    _event(it, "moved", f"outside the sending hours, now {_fmt(s[0])}")
                    it["due_at"] = s[0]
            _save(items)
            return {"moved": len(due)}
        it = due[0]
        it.update(status="sending", sending_at=int(time.time()))
        _save(items)
    res = (sender or _send_one)(dict(it))
    with LOCK:
        items = _load()
        cur = next((i for i in items if i["id"] == it["id"] and i["status"] == "sending"), None)
        if not cur:
            return {"id": it["id"], "status": res.get("status")}
        st, why = res.get("status"), str(res.get("why") or "")
        log = cold_email.read_log()
        if st == "sent":
            cur.update(status="sent", sent_at=int(time.time()), message_id=res.get("message_id"), **{"from": res.get("from")})
            _event(cur, "sent", f"via {res.get('from')}")
        elif st == "skip" and TRANSIENT.search(why):
            s = plan_slots(1, [i for i in items if i is not cur], cfg, log, now_dt, after=time.time() + 60)
            cur["status"] = "scheduled"
            if s:
                cur["due_at"] = s[0]
            _event(cur, "waiting", f"{why}; now {_fmt(cur['due_at'])}")
        elif st == "skip":
            cur["status"] = "skipped"
            cur["why"] = why
            _event(cur, "skipped", why)
            _compact(items, cfg, log, now_dt)
        else:
            cur["attempts"] = cur.get("attempts", 0) + 1
            if cur["attempts"] >= MAX_ATTEMPTS:
                cur["status"] = "failed"
                cur["why"] = why
                _event(cur, "failed", f"{why} (gave up after {MAX_ATTEMPTS} tries; you can reschedule it)")
                _compact(items, cfg, log, now_dt)
            else:
                s = plan_slots(1, [i for i in items if i is not cur], cfg, log, now_dt, after=time.time() + RETRY_AFTER)
                cur["status"] = "scheduled"
                if s:
                    cur["due_at"] = s[0]
                _event(cur, "retry", f"{why}; try {cur['attempts'] + 1} at {_fmt(cur['due_at'])}")
        _save(items)
        return {"id": cur["id"], "status": cur["status"], "why": why}


def reconcile(bounce_check=None):
    """Looks for bounces and replies on what was sent. A bounced address is marked bounced (never retried) and its slot is not reused for it;
    the emails still waiting move up into the time that frees on later days."""
    cfg, log = cold_email.load_config(), cold_email.read_log()
    try:
        res = (bounce_check or (lambda: cold_email.check_bounces(log_fn=lambda m: None)))()
        bounced = {a.lower() for a in (res or {}).get("bounced", [])}
    except Exception:
        bounced = set()
    log = cold_email.read_log()
    bounced |= {e["to"].lower() for e in log if e.get("status") == "bounced" and e.get("to")}
    replied = {e["to"].lower() for e in log if e.get("status") in ("replied", "optout") and e.get("to")}
    n = 0
    with LOCK:
        items = _load()
        for it in items:
            if it["status"] == "sent" and it["to"] in bounced:
                it["status"] = "bounced"
                _event(it, "bounced", "undeliverable: the address is bad, so it is not retried")
                n += 1
            elif it["status"] in ("sent",) and it["to"] in replied and not it.get("replied"):
                it["replied"] = True
                _event(it, "replied", "they answered (or asked to stop)")
        if n:
            _compact(items, cfg, log)
        _save(items)
    return n


def scan_replies(campaign="meta"):
    """Looks in the sending inboxes for replies from everyone this campaign emailed (queued or not) who has not replied yet. Replies and opt-outs are logged
    (opt-outs also go on the do-not-contact list), which stops further follow-ups to them. Returns only the NEW ones: [{email, kind, snippet}]."""
    log = cold_email.read_log()
    done = {e["to"].lower() for e in log if e.get("status") in ("replied", "optout", "bounced") and e.get("to")}
    first = {}
    for e in cold_email.sent_entries(log):
        to = (e.get("to") or "").lower()
        if cold_email.campaign_of(e) == campaign and to and to not in done and to not in first:
            first[to] = int(e.get("ts") or time.time()) - 3600
    if not first:
        return []
    res = cold_email.check_replies([{"id": to, "email": to, "since_ts": ts, "name": ""} for to, ts in first.items()], log_fn=lambda m: None, campaign=campaign)
    return [{"email": f["email"], "kind": f["kind"], "snippet": f.get("snippet", "")} for f in res.get("found", []) if f["email"].lower() not in done and f.get("kind") != "auto"]


def prune():
    cut = time.time() - KEEP_DAYS * 86400
    with LOCK:
        items = _load()
        keep = [i for i in items if i["status"] in ACTIVE or max((h["ts"] for h in i.get("history", [])), default=i.get("created_at", 0)) >= cut]
        if len(keep) != len(items):
            _save(keep)


def listing():
    """What the CRM page shows: every email still waiting plus the recent past, without the stored email text."""
    with LOCK:
        items = _load()
    out = [{k: i.get(k) for k in ("id", "to", "company", "status", "due_at", "attempts", "sent_at", "why", "replied", "from", "message_id", "step")} | {"history": (i.get("history") or [])[-4:]} for i in items]
    out.sort(key=lambda i: (i["status"] not in ACTIVE, i.get("due_at") or i.get("sent_at") or 0))
    return out
