#!/usr/bin/env python3
"""
The helper's own copy of the CRM's Meta-ads data. This file is the single source of truth for the outreach loop (tools/outreach_loop.py):

  - leads        the page's S.adLeads (every advertiser / HALCRUS lead and everything the pipeline learned about it)
  - settings     only the keys in SETTINGS_KEYS (the page's S.settings has more, including secrets that must never leave the browser)
  - gone         ids of advertisers removed in the CRM (so they never come back)

It lives in tools/crm_state.json (git-ignored; set CRM_DATA_DIR to keep it somewhere else, for example in tests). It is written
atomically, so a crash or restart loses at most the last couple of seconds.

The CRM page keeps working from its own browser copy and talks to this store through sync(): it sends the field-level changes it made
(patches) and gets back every lead that changed here since the revision it last saw. Merging is per field, in place, so a lead the loop is
working on keeps its identity (like the page's own in-place merge between tabs). The helper's loop and the page can both change the same
lead; they only collide when both change the same field at the same moment, and then the last writer wins.
"""
import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.getenv("CRM_DATA_DIR") or HERE)

# page settings the helper needs (or owns the meaning of); nothing else crosses over, so sync passwords and the like stay in the browser
SETTINGS_KEYS = {
    "autoStart", "startConfirmed", "keepDays", "startTarget", "sendMode", "loomAuto", "loomAutoSend", "loomSendChecked", "videoAutoApprove",
    "autoFollowUps", "cformOn", "cformT1", "cformConfirmed", "cformFallbackHours", "cformDaily", "cformAnyTime", "cformSubject", "cformMsg",
    "cformMsgT1", "formWebsite", "jitbitCheck", "aiEmailCheck", "trustHalcrus", "halcrusTier", "adVoice", "videoName", "videoHost", "loomShow", "keepLocalVideos",
    "notifyReady", "runStrategy", "metaFallback", "templateMode", "emailOff", "apprStats", "meta_resolved", "outsideNote",
}


def _fp(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else DATA / "crm_state.json"
        self.lock = threading.RLock()
        self.leads = {}          # id -> lead dict (insertion order = the page's order)
        self.settings = {}
        self.gone = []           # [{id, rev}]
        self.rev = 0
        self.lead_rev = {}       # id -> revision of its last change
        self.set_rev = {}        # settings key -> revision of its last change
        self._fp = {}
        self._set_fp = {}
        self._dirty = False
        self._last_flush = 0
        self.load()

    # ------------------------------------------------------------------ persistence
    def load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        with self.lock:
            self.leads = {str(l["id"]): l for l in d.get("adLeads", []) if isinstance(l, dict) and l.get("id") is not None}
            self.settings = d.get("settings") or {}
            self.gone = d.get("gone") or []
            self.rev = int(d.get("rev") or 0)
            self.lead_rev = {k: int(v) for k, v in (d.get("lead_rev") or {}).items()}
            self.set_rev = {k: int(v) for k, v in (d.get("set_rev") or {}).items()}
            for k in self.leads:
                self.lead_rev.setdefault(k, self.rev)
            self._fp = {k: _fp(l) for k, l in self.leads.items()}
            self._set_fp = {k: _fp(v) for k, v in self.settings.items()}

    def flush(self, force=False):
        with self.lock:
            if not (self._dirty or force):
                return False
            blob = json.dumps({"rev": self.rev, "adLeads": list(self.leads.values()), "settings": self.settings, "gone": self.gone[-3000:],
                               "lead_rev": self.lead_rev, "set_rev": self.set_rev, "saved_at": int(time.time())}, default=str)
            self._dirty = False
            self._last_flush = time.time()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(blob, encoding="utf-8")
        tmp.replace(self.path)
        return True

    def flusher(self, every=2.0):
        """Background thread body: writes changes out every couple of seconds."""
        while True:
            time.sleep(every)
            try:
                self.flush()
            except Exception as e:
                print("crm store flush:", e)

    # ------------------------------------------------------------------ change tracking
    def commit(self):
        """Notices what changed since the last commit (the loop edits lead dicts in place) and gives those leads a new revision."""
        with self.lock:
            changed = False
            seen = set()
            for k, l in list(self.leads.items()):
                seen.add(k)
                try:
                    f = _fp(l)
                except RuntimeError:     # the loop changed this lead while it was being read: it is picked up on the next commit
                    continue
                if self._fp.get(k) != f:
                    if not changed:
                        self.rev += 1
                        changed = True
                    self._fp[k] = f
                    self.lead_rev[k] = self.rev
            for k in list(self._fp):
                if k not in seen:
                    del self._fp[k]
                    self.lead_rev.pop(k, None)
            for k, v in self.settings.items():
                f = _fp(v)
                if self._set_fp.get(k) != f:
                    if not changed:
                        self.rev += 1
                        changed = True
                    self._set_fp[k] = f
                    self.set_rev[k] = self.rev
            for k in list(self._set_fp):
                if k not in self.settings:
                    del self._set_fp[k]
                    if not changed:
                        self.rev += 1
                        changed = True
                    self.set_rev[k] = self.rev      # a deleted key keeps a revision so the page learns about the removal
            if changed:
                self._dirty = True
            return changed

    # ------------------------------------------------------------------ helper-side editing (used by the loop)
    def get(self, lead_id):
        return self.leads.get(str(lead_id))

    def all(self):
        return list(self.leads.values())

    def add(self, lead):
        with self.lock:
            self.leads[str(lead["id"])] = lead
        return lead

    def set_setting(self, key, value):
        with self.lock:
            if value is None:
                self.settings.pop(key, None)
            else:
                self.settings[key] = value

    # ------------------------------------------------------------------ the page's side
    def sync(self, body):
        """body: {rev, first, patches: [{id, set: {}, del: []}], gone: [ids], settings: {set: {}, del: []}, refetch: [ids]}
        Applies what the page changed, then returns everything that changed here since body['rev'] (including the merged result of its own patches).
        With first=true (the page never synced before) the page may only ADD leads and settings this store does not have yet; it never overwrites."""
        cr = int(body.get("rev") or 0)
        first = bool(body.get("first"))
        with self.lock:
            self.commit()   # absorb the loop's own edits first, so the page's patches land on top of them
            gone_ids = {g["id"] for g in self.gone}
            for p in body.get("patches") or []:
                lid = str(p.get("id"))
                if not lid or lid == "None":
                    continue
                cur = self.leads.get(lid)
                if cur is None:
                    if lid in gone_ids:
                        continue
                    cur = {"id": p.get("set", {}).get("id", lid)}
                    self.leads[lid] = cur
                elif first:
                    continue
                for k, v in (p.get("set") or {}).items():
                    if k != "id":
                        cur[k] = v
                for k in p.get("del") or []:
                    if k != "id":
                        cur.pop(k, None)
            gone_in = [str(x) for x in (body.get("gone") or [])]
            if len(gone_in) > max(20, len(self.leads) // 4):     # a page that lost its data must not wipe the real store
                gone_in = []
                refused = len(body.get("gone") or [])
            else:
                refused = 0
            for gid in gone_in:
                if gid in self.leads:
                    del self.leads[gid]
                if gid not in gone_ids:
                    self.rev += 1
                    self._dirty = True
                    self.gone.append({"id": gid, "rev": self.rev})
                    gone_ids.add(gid)
            st = body.get("settings") or {}
            for k, v in (st.get("set") or {}).items():
                if k in SETTINGS_KEYS and not (first and k in self.settings):
                    self.settings[k] = v
            if not first:
                for k in st.get("del") or []:
                    if k in SETTINGS_KEYS:
                        self.settings.pop(k, None)
            self.commit()
            ref = {str(x) for x in (body.get("refetch") or [])}
            full = first or cr == 0
            leads = []
            for k, l in list(self.leads.items()):
                if full or self.lead_rev.get(k, 0) > cr or k in ref:
                    for _ in range(5):
                        try:
                            leads.append(copy.deepcopy(l))
                            break
                        except RuntimeError:
                            time.sleep(0.02)
            sets = {k: copy.deepcopy(v) for k, v in self.settings.items() if full or self.set_rev.get(k, 0) > cr}
            dels = [k for k, r in self.set_rev.items() if k not in self.settings and r > cr and not full]
            gone = [g["id"] for g in self.gone if g["rev"] > cr] if not full else [g["id"] for g in self.gone]
            return {"ok": True, "refused_gone": refused, "rev": self.rev, "leads": leads, "settings": {"set": sets, "del": dels}, "gone": gone, "count": len(self.leads), "full": full}

    def snapshot(self):
        with self.lock:
            return {"rev": self.rev, "count": len(self.leads)}


STORE = None


def get_store():
    global STORE
    if STORE is None:
        STORE = Store()
    return STORE
