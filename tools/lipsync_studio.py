#!/usr/bin/env python3
"""
Voice + lip sync studio (helper side): pick or clone a voice, give it a photo, get a talking video.

Parts it drives (all on this Mac, nothing leaves it):
  - the lip-sync engine: MuseTalk v1.5 (Apple Silicon port in tools/.musetalk, its own venv), started on demand on 127.0.0.1:8777 and stopped again when idle
  - voices: Kokoro presets (tools/kokoro_say.py), VoiceStudio voices when its app is running (http://localhost:3900, OpenAI-compatible /v1/audio/speech)
    and voices you clone here (a clean reference recording kept in tools/studio_voices/, never uploaded anywhere)
  - speed: the photo is resized once to the chosen quality, its face data is prepared ONCE per photo and cached (the engine's avatar_key), audio is cut to 16 kHz mono,
    and the finished video is saved in tools/studio_out/ with how long each stage took

    python3 tools/lipsync_studio.py status
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE / ".musetalk"
ENGINE_PY = ENGINE / ".venv" / "bin" / "python"
OUT = HERE / "studio_out"
VOICES_DIR = HERE / "studio_voices"
PHOTOS = HERE / "studio_photos"
PORT = int(os.getenv("LIPSYNC_PORT", "8777"))
VS = os.getenv("VOICESTUDIO_URL", "http://localhost:3900")
KOKORO_PY = HERE / ".kokoro_venv" / "bin" / "python"
IDLE_STOP_SECONDS = 15 * 60

# photo size per quality preset: the long side, in pixels. The face is cut out at 256 x 256 whatever the photo size, so a bigger photo mostly costs time in the compositing and
# encoding steps, not in the lip movement itself.
QUALITY = {
    "fast": {"side": 640, "label": "Fast", "about": "about 1x the clip length", "crf": 20},
    "balanced": {"side": 896, "label": "Balanced", "about": "about 1.5x the clip length", "crf": 18},
    "high": {"side": 1280, "label": "High", "about": "about 2.5x the clip length", "crf": 16},
}

KOKORO_VOICES = [
    ("kokoro:am_michael", "Michael (male, the one used for your ads)"), ("kokoro:am_adam", "Adam (male)"), ("kokoro:am_eric", "Eric (male)"),
    ("kokoro:am_liam", "Liam (male)"), ("kokoro:bm_george", "George (male, British)"), ("kokoro:af_heart", "Heart (female)"),
    ("kokoro:af_bella", "Bella (female)"), ("kokoro:af_nicole", "Nicole (female)"), ("kokoro:bf_emma", "Emma (female, British)"),
]

LOCK = threading.Lock()
JOBS = {}
_state = {"proc": None, "last_use": 0.0, "avatars": {}}


class StudioError(Exception):
    pass


def _http(method, url, body=None, headers=None, timeout=60, raw=False):
    data = None
    h = dict(headers or {})
    if body is not None and not isinstance(body, (bytes, bytearray)):
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    elif body is not None:
        data = body
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
    return b if raw else (json.loads(b) if b else {})


# ------------------------------------------------------------------------------------------------ the lip-sync engine
def engine_installed():
    return ENGINE_PY.exists() and (ENGINE / "upstream" / "models" / "musetalkV15" / "unet.pth").exists() and (ENGINE / "server.py").exists()


def engine_up():
    try:
        return bool(_http("GET", f"http://127.0.0.1:{PORT}/health", timeout=3))
    except Exception:
        return False


def _ffmpeg_dir():
    """The engine needs an `ffmpeg` on its PATH: the static one that came with imageio-ffmpeg inside its own venv."""
    d = ENGINE / "bin"
    d.mkdir(exist_ok=True)
    link = d / "ffmpeg"
    if not link.exists():
        found = list((ENGINE / ".venv" / "lib").glob("python*/site-packages/imageio_ffmpeg/binaries/ffmpeg*"))
        if not found:
            raise StudioError("the engine's ffmpeg is missing (imageio-ffmpeg is not installed in tools/.musetalk/.venv)")
        link.symlink_to(found[0])
    return str(d)


def engine_start(wait=240):
    """Starts the lip-sync engine (localhost only) and waits until it answers. Loading the model takes about a minute the first time."""
    if not engine_installed():
        raise StudioError("the lip-sync engine is not installed yet (tools/.musetalk with its weights)")
    with LOCK:
        if not engine_up():
            env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1", "GLOG_minloglevel": "2", "PATH": _ffmpeg_dir() + os.pathsep + os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1", "MUSETALK_FP16_UNET": "1", "MUSETALK_BATCH": "32"}
            log = open(OUT / "engine.log", "a") if OUT.exists() else subprocess.DEVNULL
            _state["proc"] = subprocess.Popen([str(ENGINE_PY), "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(PORT)], cwd=str(ENGINE), env=env,
                                              stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < wait:
        if engine_up():
            _state["last_use"] = time.time()
            return True
        p = _state["proc"]
        if p is not None and p.poll() is not None:
            raise StudioError("the lip-sync engine stopped while starting (see tools/studio_out/engine.log)")
        time.sleep(2)
    raise StudioError("the lip-sync engine did not answer in time (see tools/studio_out/engine.log)")


def engine_stop():
    p = _state.get("proc")
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
    _state["proc"] = None
    _state["avatars"] = {}


def engine_idle_check():
    """Called now and then by the helper: frees the memory (several GB) when nothing was made for a while."""
    if time.time() - _state["last_use"] > IDLE_STOP_SECONDS and not any(j["state"] == "working" for j in JOBS.values()):
        if _state["proc"] is not None:
            engine_stop()
        w = _state.get("kokoro")
        if w is not None and w.poll() is None:
            w.terminate()
        _state["kokoro"] = None


# ------------------------------------------------------------------------------------------------ voices
def voicestudio_up():
    try:
        _http("GET", VS + "/health", timeout=3)
        return True
    except Exception:
        return False


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:40] or "voice"


def _cloned():
    out = []
    if VOICES_DIR.exists():
        for d in sorted(VOICES_DIR.iterdir()):
            try:
                m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                if (d / "ref.wav").exists():
                    out.append({"id": "clone:" + d.name, "name": m.get("name") or d.name, "source": "cloned", "note": m.get("note") or "", "at": m.get("at")})
            except Exception:
                continue
    return out


def voices():
    """Every voice the page can offer: [{id, name, source: 'preset'|'cloned'|'voicestudio', note}]."""
    out = [{"id": i, "name": n, "source": "preset", "note": "fast, built in"} for i, n in KOKORO_VOICES]
    out += _cloned()
    if voicestudio_up():
        try:
            d = _http("GET", VS + "/v1/audio/voices", timeout=8)
            rows = d.get("voices") if isinstance(d, dict) else d
            for v in rows or []:
                vid = v.get("id") or v.get("voice_id") or v.get("name")
                if vid:
                    out.append({"id": "vs:" + str(vid), "name": v.get("name") or str(vid), "source": "voicestudio", "note": v.get("description") or v.get("engine") or "from VoiceStudio"})
        except Exception:
            pass
    return out


def clone_voice(name, wav_bytes, consent, note=""):
    """Saves a reference recording of a voice you are allowed to use. It stays on this Mac. When VoiceStudio is running the sample is also offered to it for cloning."""
    if not consent:
        raise StudioError("Tick the box: this is your own voice, or you have the owner's permission.")
    if not wav_bytes or len(wav_bytes) < 20000:
        raise StudioError("That recording is too short. Use 10 to 30 seconds of clean speech, one person, no music.")
    d = VOICES_DIR / _slug(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "ref_original").write_bytes(wav_bytes)
    ref = d / "ref.wav"
    _to_wav(d / "ref_original", ref, rate=24000)
    (d / "meta.json").write_text(json.dumps({"name": name, "note": note, "consent": True, "at": int(time.time())}), encoding="utf-8")
    return {"id": "clone:" + d.name, "name": name, "source": "cloned"}


def _to_wav(src, dst, rate=16000):
    ff = _ffmpeg_dir() + "/ffmpeg"
    r = subprocess.run([ff, "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", str(dst)], capture_output=True, text=True)
    if r.returncode != 0:
        raise StudioError("could not read that audio file: " + r.stderr.strip()[:120])


def _kokoro(text, voice, out_wav, speed=1.0):
    """Kokoro speech through one worker process that stays loaded (saves the 3 to 4 s model start on every clip)."""
    with LOCK:
        w = _state.get("kokoro")
        if w is None or w.poll() is not None:
            w = subprocess.Popen([str(KOKORO_PY), str(HERE / "kokoro_worker.py")], cwd=str(HERE), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            _state["kokoro"] = w
            first = w.stdout.readline()
            if '"ready"' not in first:
                _state["kokoro"] = None
                raise StudioError("the Kokoro voice engine did not start")
        w.stdin.write(json.dumps({"text": text, "voice": voice, "speed": speed, "out": str(out_wav)}) + "\n")
        w.stdin.flush()
        r = json.loads(w.stdout.readline() or "{}")
    _state["last_use"] = time.time()
    if not r.get("ok"):
        raise StudioError("Kokoro could not make that speech: " + str(r.get("error") or "no answer"))


def say(text, voice_id, out_wav, speed=1.0):
    """Speech for `text` in the chosen voice, written to out_wav. Returns the seconds it took."""
    t0 = time.time()
    kind, _, vid = str(voice_id).partition(":")
    if kind == "kokoro":
        if not KOKORO_PY.exists():
            raise StudioError("the Kokoro voices are not installed (tools/.kokoro_venv)")
        _kokoro(text, vid, out_wav, speed)
    elif kind == "vs":
        if not voicestudio_up():
            raise StudioError("VoiceStudio is not running. Open the VoiceStudio app, then try again.")
        b = _http("POST", VS + "/v1/audio/speech", {"model": "tts-1", "voice": vid, "input": text, "response_format": "wav"}, timeout=600, raw=True)
        Path(out_wav).write_bytes(b)
    elif kind == "clone":
        if not voicestudio_up():
            raise StudioError("Cloned voices are spoken by VoiceStudio. Open the VoiceStudio app (and install a voice model there), then try again.")
        ref = VOICES_DIR / vid / "ref.wav"
        if not ref.exists():
            raise StudioError("that cloned voice is missing its recording")
        b = _clone_speech(text, ref)
        Path(out_wav).write_bytes(b)
    else:
        raise StudioError("unknown voice")
    return round(time.time() - t0, 1)


def _clone_speech(text, ref):
    """Speech in a cloned voice through VoiceStudio's own cloning endpoint (found from its OpenAPI list, because the exact route belongs to its version)."""
    spec = _http("GET", VS + "/openapi.json", timeout=15)
    paths = list((spec.get("paths") or {}).keys())
    cand = [p for p in paths if re.search(r"clone", p, re.I) and re.search(r"generate|speech|synth|tts", p, re.I)] or [p for p in paths if re.search(r"clone", p, re.I)]
    if not cand:
        raise StudioError("This VoiceStudio version has no cloning endpoint that I can find. Clone the voice inside the VoiceStudio app, then pick it from the list.")
    raise StudioError("VoiceStudio is running, but its cloning route (" + cand[0] + ") has not been wired yet.")


# ------------------------------------------------------------------------------------------------ photo + video
def prep_photo(image_bytes, quality):
    """Orients and resizes the photo once; returns (png path, key). The key changes with the photo and the size, so the engine's cached face data is reused for the same one."""
    from io import BytesIO
    try:
        from PIL import Image, ImageOps
    except Exception:
        raise StudioError("Pillow is missing in the helper's Python")
    img = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes))).convert("RGB")
    side = QUALITY[quality]["side"]
    w, h = img.size
    if max(w, h) > side:
        s = side / max(w, h)
        img = img.resize((max(2, int(w * s) // 2 * 2), max(2, int(h * s) // 2 * 2)), Image.LANCZOS)
    else:
        img = img.resize((w // 2 * 2, h // 2 * 2))
    PHOTOS.mkdir(exist_ok=True)
    key = hashlib.sha1(image_bytes).hexdigest()[:12] + "_" + str(side)
    p = PHOTOS / (key + ".png")
    img.save(p)
    return p, key


def make(job, text, voice_id, image_bytes, quality="balanced", audio_bytes=None, speed=1.0):
    """The whole thing. `job` is a dict updated as it goes: state, step, timings, result."""
    t_all = time.time()
    quality = quality if quality in QUALITY else "balanced"
    job.update(state="working", step="Preparing the photo", timings={})
    OUT.mkdir(exist_ok=True)
    jid = job["id"]
    png, key = prep_photo(image_bytes, quality)
    job["timings"]["photo"] = round(time.time() - t_all, 1)

    wav24 = OUT / f"{jid}_voice.wav"
    if audio_bytes:
        (OUT / f"{jid}_upload").write_bytes(audio_bytes)
        _to_wav(OUT / f"{jid}_upload", wav24, rate=24000)
        job["timings"]["voice"] = 0
    else:
        job["step"] = "Making the voice"
        job["timings"]["voice"] = say(text, voice_id, wav24, speed)
    wav16 = OUT / f"{jid}_16k.wav"
    _to_wav(wav24, wav16, rate=16000)

    job["step"] = "Starting the lip-sync engine"
    t = time.time()
    engine_start()
    job["timings"]["engine_start"] = round(time.time() - t, 1)
    _state["last_use"] = time.time()

    if key not in _state["avatars"]:
        job["step"] = "Learning the face (once per photo)"
        t = time.time()
        _http("POST", f"http://127.0.0.1:{PORT}/warmup", {"video_b64": base64.b64encode(png.read_bytes()).decode(), "avatar_key": key}, timeout=300)
        _state["avatars"][key] = time.time()
        job["timings"]["face"] = round(time.time() - t, 1)
    else:
        job["timings"]["face"] = 0

    job["step"] = "Making the lips move"
    t = time.time()
    mp4 = _http("POST", f"http://127.0.0.1:{PORT}/lipsync_stream", {"avatar_key": key, "audio_b64": base64.b64encode(wav16.read_bytes()).decode()}, timeout=3600, raw=True)
    job["timings"]["lipsync"] = round(time.time() - t, 1)
    out = OUT / f"{jid}.mp4"
    out.write_bytes(mp4)
    secs = _duration(wav16)
    job["timings"]["total"] = round(time.time() - t_all, 1)
    job.update(state="done", step="Done", file=out.name, seconds=secs, quality=quality, voice=voice_id)
    meta = {k: job.get(k) for k in ("id", "file", "seconds", "quality", "voice", "timings")}
    meta.update(text=(text or "")[:500], at=int(time.time()))
    (OUT / f"{jid}.json").write_text(json.dumps(meta), encoding="utf-8")
    try:
        for f in (wav16, OUT / f"{jid}_upload"):
            f.unlink(missing_ok=True)
    except Exception:
        pass
    return out


def _duration(wav):
    try:
        import wave
        with wave.open(str(wav)) as w:
            return round(w.getnframes() / w.getframerate(), 1)
    except Exception:
        return 0


def start_job(text, voice_id, image_bytes, quality, audio_bytes=None, speed=1.0):
    job = {"id": uuid.uuid4().hex[:10], "state": "queued", "step": "Queued", "timings": {}}
    JOBS[job["id"]] = job

    def run():
        try:
            make(job, text, voice_id, image_bytes, quality, audio_bytes, speed)
        except Exception as e:
            job.update(state="failed", step="Stopped", error=str(e)[:300])
    threading.Thread(target=run, daemon=True).start()
    return job["id"]


def history(n=12):
    rows = []
    if OUT.exists():
        for f in sorted(OUT.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]:
            try:
                rows.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
    return rows


def status():
    return {"engine_installed": engine_installed(), "engine_up": engine_up(), "voicestudio_up": voicestudio_up(), "qualities": QUALITY,
            "kokoro": KOKORO_PY.exists(), "history": history()}


if __name__ == "__main__":
    print(json.dumps(status(), indent=1)[:1500])
