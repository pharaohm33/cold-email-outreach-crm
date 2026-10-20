#!/usr/bin/env python3
"""
Kokoro voice kept loaded between requests (starting the voice model costs 3 to 4 seconds every time). Run with the Kokoro venv:
    tools/.kokoro_venv/bin/python tools/kokoro_worker.py
Reads one JSON request per line on stdin  {"text", "voice", "speed", "out"}  and answers one JSON line per request  {"ok": true, "seconds": ..}  or  {"ok": false, "error": ..}.
Same speech as kokoro_say.py (24 kHz mono wav, silence trimmed, 0.12 s between sentences).
"""
import json
import re
import sys

import numpy as np
import soundfile as sf
from kokoro import KPipeline

import kokoro_say as K

pipe = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
        parts = []
        for r in pipe(req["text"], voice=req.get("voice", "am_michael"), speed=float(req.get("speed", 1.0)), split_pattern=r"(?<=[.!?])\s+"):
            a = np.asarray(r.audio.numpy() if hasattr(r.audio, "numpy") else r.audio, dtype=np.float32)
            s, e = K.trim(a)
            parts += [a[s:e], np.zeros(K.GAP, dtype=np.float32)]
        audio = np.concatenate(parts[:-1])
        sf.write(req["out"], audio, K.SR)
        print(json.dumps({"ok": True, "seconds": round(len(audio) / K.SR, 3)}), flush=True)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:200]}), flush=True)
