#!/usr/bin/env python3
"""
Clip Studio - lokale App rund um clipper.py.

Start:  python app.py      (oder start_windows.bat / start_mac.command)
Öffnet  http://127.0.0.1:7860 im Browser. Alles läuft lokal auf deinem Rechner.
"""
from __future__ import annotations

import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

BASE = Path(__file__).resolve().parent
JOBS = BASE / "jobs"
SETTINGS_FILE = BASE / "settings.json"
CLIPPER = BASE / "clipper.py"
PORT = int(os.environ.get("CLIPSTUDIO_PORT", "7860"))
VERSION = (BASE / "VERSION").read_text(encoding="utf-8").strip() if (BASE / "VERSION").exists() else "0"
RESTART_CODE = 42  # start scripts restart the app when it exits with this code
# GitHub repo the in-app updater pulls from ("owner/name"); can be overridden in settings.json
UPDATE_REPO = "Ege2905/AI-Clipper"
UPDATE_BRANCH = "main"
KEEP_ON_UPDATE = {"jobs", "venv", "settings.json", ".git"}

DEFAULT_SETTINGS = {
    "ai_provider": "ollama",          # "ollama" (lokal, kostenlos) or "anthropic" (Claude API)
    "ollama_model": "qwen3:8b",
    "ollama_url": "http://localhost:11434",
    "api_key": "",
    "ai_model": "claude-sonnet-4-5",
    "hashtags": "",
    "font": "Arial",
    "whisper_model": "small",
    "language": "",
    "update_repo": "",
}

# progress ranges per stage (overall 0..100)
STAGES = {
    "start": (0, 2, "Starte"),
    "download": (2, 15, "Video herunterladen"),
    "transcribe": (15, 55, "Sprache erkennen"),
    "select": (55, 60, "Beste Momente finden"),
    "render": (60, 100, "Clips rendern"),
    "clip": (60, 100, "Clips rendern"),
    "done": (100, 100, "Fertig"),
}

app = Flask(__name__, static_folder=None)
lock = threading.Lock()
work_q: "queue.Queue[str]" = queue.Queue()
jobs: dict[str, dict] = {}
procs: dict[str, subprocess.Popen] = {}
ID_RE = re.compile(r"^[a-f0-9]{10}$")


# ----------------------------------------------------------------------------- helpers
def fix_path() -> None:
    """Make ffmpeg findable right after a fresh winget/brew install."""
    extra = []
    if platform.system() == "Windows":
        la = os.environ.get("LOCALAPPDATA", "")
        extra += [Path(la) / "Microsoft" / "WinGet" / "Links"]
    else:
        extra += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    for d in extra:
        if d.is_dir() and str(d) not in os.environ["PATH"]:
            os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except ValueError:
            pass
    return s


def job_dir(jid: str) -> Path:
    if not ID_RE.match(jid):
        abort(404)
    return JOBS / jid


def save_job(job: dict) -> None:
    d = JOBS / job["id"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / "job.json")


def load_all_jobs() -> None:
    JOBS.mkdir(exist_ok=True)
    for f in JOBS.glob("*/job.json"):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if job.get("status") in ("running", "queued"):
            job["status"] = "error"
            job["error"] = "Unterbrochen (App wurde geschlossen)"
            save_job(job)
        jobs[job["id"]] = job


def log_line(jid: str, line: str) -> None:
    with open(JOBS / jid / "log.txt", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def safe_name(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^\w.\- ]", "_", name, flags=re.UNICODE).strip() or "video.mp4"
    return name[:120]


def clip_file(jid: str, fname: str) -> Path:
    d = job_dir(jid) / "clips"
    p = (d / Path(fname).name).resolve()
    if p.parent != d.resolve():
        abort(404)
    return p


def read_clips(jid: str) -> list[dict]:
    d = JOBS / jid / "clips"
    f = d / "clips.json"
    if not f.exists():
        return []
    try:
        clips = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return []
    out = []
    for c in clips:
        mp4 = d / c["file"]
        if not mp4.exists():
            continue
        txt = mp4.with_suffix(".txt")
        c["post_text"] = txt.read_text(encoding="utf-8") if txt.exists() else ""
        c["size_mb"] = round(mp4.stat().st_size / 1e6, 1)
        out.append(c)
    return out


def write_clips(jid: str, clips: list[dict]) -> None:
    keep = [{k: v for k, v in c.items() if k not in ("post_text", "size_mb")} for c in clips]
    (JOBS / jid / "clips" / "clips.json").write_text(
        json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------- worker
def build_cmd(job: dict, settings: dict) -> list[str]:
    p = job["params"]
    cmd = [sys.executable, "-u", str(CLIPPER), p["source"],
           "-n", str(int(p.get("num_clips", 5))),
           "--min", str(float(p.get("min", 20))), "--max", str(float(p.get("max", 60))),
           "--layout", p.get("layout", "auto"),
           "--whisper-model", p.get("whisper_model") or settings["whisper_model"],
           "--font", p.get("font") or settings["font"],
           "--words-per-line", str(int(p.get("words_per_line", 3))),
           "-o", str(JOBS / job["id"] / "clips")]
    lang = p.get("language") or settings["language"]
    if lang:
        cmd += ["--language", lang]
    if p.get("section"):
        cmd += ["--section", p["section"]]
    if p.get("transcript"):  # advanced: reuse an existing transcript.json
        cmd += ["--transcript", p["transcript"]]
    if p.get("facecam"):
        cmd += ["--facecam", p["facecam"]]
    if p.get("ai"):
        if settings.get("ai_provider") == "anthropic":
            cmd += ["--ai", "--ai-provider", "anthropic", "--ai-model", settings["ai_model"]]
        else:
            cmd += ["--ai", "--ai-provider", "ollama", "--ai-model", settings["ollama_model"],
                    "--ollama-url", settings["ollama_url"]]
    tags = " ".join(x for x in (settings.get("hashtags", ""), p.get("hashtags", "")) if x)
    if tags:
        cmd += ["--hashtags", tags]
    if not p.get("upper", True):
        cmd.append("--no-upper")
    if not p.get("title_overlay", True):
        cmd.append("--no-title")
    return cmd


def set_progress(job: dict, stage: str, frac: float | None, msg: str = "") -> None:
    lo, hi, label = STAGES.get(stage, (job.get("progress", 0), job.get("progress", 0), stage))
    if frac is not None:
        job["progress"] = round(lo + (hi - lo) * max(0.0, min(1.0, frac)), 1)
    else:
        job["progress"] = max(job.get("progress", 0), lo)
    job["stage"] = label + (f" · {msg}" if msg else "")


def run_job(jid: str) -> None:
    settings = load_settings()
    with lock:
        job = jobs[jid]
        if job["status"] == "cancelled":
            return
        job.update(status="running", started=time.time(), progress=0, error="", warnings=[])
        set_progress(job, "start", 0)
        save_job(job)

    env = os.environ.copy()
    env.update(CLIPSTUDIO="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               HF_HUB_DISABLE_SYMLINKS_WARNING="1", PYTHONWARNINGS="ignore")
    if settings.get("api_key"):
        env["ANTHROPIC_API_KEY"] = settings["api_key"]
    cmd = build_cmd(job, settings)
    log_line(jid, "$ " + " ".join(cmd[2:]))
    flags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1, env=env,
                            cwd=str(BASE), creationflags=flags)
    procs[jid] = proc
    last_save, last_dl_logged, tail = 0.0, -10.0, []
    dl_re = re.compile(r"\[download\]\s+([\d.]+)%")

    for raw in proc.stdout:
        line = raw.rstrip()
        if not line:
            continue
        with lock:
            if line.startswith("@@"):
                try:
                    ev = json.loads(line[2:])
                except ValueError:
                    continue
                st = ev.get("stage")
                if st == "meta":
                    if ev.get("title"):
                        job["title"] = ev["title"]
                elif st == "info":
                    job["duration"] = ev.get("duration")
                elif st == "warn":
                    job.setdefault("warnings", []).append(ev.get("msg", ""))
                    job["warnings"] = job["warnings"][-5:]
                elif st == "clip":
                    job["clip_count"] = job.get("clip_count", 0) + 1
                    set_progress(job, "clip", ev.get("pct"))
                else:
                    set_progress(job, st, ev.get("pct"), ev.get("msg", ""))
            else:
                m = dl_re.search(line)
                if m:
                    pct = float(m.group(1))
                    set_progress(job, "download", pct / 100, f"{pct:.0f}%")
                    if pct - last_dl_logged < 10 and pct < 100:
                        continue
                    last_dl_logged = pct
                log_line(jid, line)
                tail = (tail + [line])[-15:]
            if time.time() - last_save > 0.7:
                save_job(job)
                last_save = time.time()

    rc = proc.wait()
    procs.pop(jid, None)
    with lock:
        if job["status"] == "cancelled":
            job["stage"] = "Abgebrochen"
        elif rc == 0:
            job.update(status="done", progress=100, stage="Fertig")
        else:
            job["status"] = "error"
            job["error"] = friendly_error("\n".join(tail))
            job["stage"] = "Fehler"
        job["finished"] = time.time()
        save_job(job)


def friendly_error(tail: str) -> str:
    t = tail.lower()
    hints = [
        ("no clips found", "Keine Clips gefunden – enthält das Video Sprache?"),
        ("anthropic_api_key", "KI-Auswahl braucht einen API-Key (Einstellungen)."),
        ("ollama not reachable", "Ollama läuft nicht – starte die Ollama-App (ollama.com) und versuch es nochmal."),
        ("not installed. run: ollama pull", "Das lokale KI-Modell ist nicht installiert – siehe Einstellungen → Lokale KI."),
        ("unsupported url", "Dieser Link wird nicht unterstützt."),
        ("private video", "Das Video ist privat oder nicht verfügbar."),
        ("sign in to confirm", "YouTube verlangt eine Anmeldung. Lade das Video manuell herunter und nutze die Datei."),
        ("ffmpeg", "Problem mit ffmpeg – ist es installiert?"),
        ("no face detector", "OpenCV 5 installiert – bitte 'pip install \"opencv-python-headless<5\"'."),
        ("not_found_error", "KI-Modell nicht gefunden – prüfe das Modell in den Einstellungen."),
        ("authentication", "API-Key ungültig."),
        ("timed out", "Zeitüberschreitung – klick auf „Erneut versuchen“, bereits Erledigtes wird übersprungen."),
        ("huggingface", "Sprachmodell konnte nicht geladen werden – Internetverbindung prüfen (nur beim ersten Start nötig)."),
        ("proxyerror", "Keine Verbindung ins Internet (Proxy/Firewall) – wird beim ersten Start für das Sprachmodell gebraucht."),
        ("out of memory", "Zu wenig Arbeitsspeicher – wähle bei Genauigkeit „Schnell“ oder „Ausgewogen“."),
    ]
    for key, msg in hints:
        if key in t:
            return msg
    last = [l for l in tail.splitlines() if l.strip()]
    return last[-1][:300] if last else "Unbekannter Fehler"


def worker() -> None:
    while True:
        jid = work_q.get()
        try:
            run_job(jid)
        except Exception as e:  # keep the worker alive
            with lock:
                jobs[jid].update(status="error", error=str(e), stage="Fehler")
                save_job(jobs[jid])


def new_job(params: dict, title: str, cache_from: Path | None = None) -> dict:
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "title": title, "params": params, "status": "queued", "progress": 0,
           "stage": "In Warteschlange", "created": time.time(), "posted": {}, "error": ""}
    (JOBS / jid / "clips").mkdir(parents=True, exist_ok=True)
    if cache_from and cache_from.exists():  # reuse download + transcript + loudness
        new_work = JOBS / jid / "clips" / ".work"
        new_work.mkdir(parents=True, exist_ok=True)
        for f in cache_from.iterdir():
            if f.is_file() and f.suffix != ".ass":
                try:
                    os.link(f, new_work / f.name)
                except OSError:
                    shutil.copy2(f, new_work / f.name)
    with lock:
        jobs[jid] = job
        save_job(job)
    work_q.put(jid)
    return job


def summary(job: dict) -> dict:
    return {k: job.get(k) for k in ("id", "title", "status", "progress", "stage", "created",
                                    "error", "clip_count")}


# ----------------------------------------------------------------------------- routes
@app.get("/")
def index():
    return send_from_directory(BASE / "ui", "index.html")


def ollama_status(url: str) -> dict:
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=2) as r:
            models = [m["name"] for m in json.load(r).get("models", [])]
        return {"running": True, "models": models}
    except Exception:
        return {"running": False, "models": []}


def model_installed(model: str, models: list[str]) -> bool:
    return any(m == model or m == model + ":latest" or m.split(":")[0] == model for m in models)


@app.get("/api/system")
def system():
    def has_mod(name: str) -> bool:
        try:
            __import__(name)
            return True
        except Exception:
            return False
    import cv2
    s = load_settings()
    ol = ollama_status(s.get("ollama_url", DEFAULT_SETTINGS["ollama_url"]))
    if s.get("ai_provider") == "anthropic":
        has_ai = bool(s.get("api_key")) and has_mod("anthropic")
    else:
        has_ai = ol["running"] and model_installed(s.get("ollama_model", ""), ol["models"])
    return jsonify({
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "whisper": has_mod("faster_whisper"),
        "ytdlp": has_mod("yt_dlp"),
        "opencv_ok": hasattr(cv2, "CascadeClassifier"),
        "anthropic": has_mod("anthropic"),
        "has_key": bool(s.get("api_key")),
        "provider": s.get("ai_provider", "ollama"),
        "ollama": ol,
        "ollama_model": s.get("ollama_model"),
        "ollama_model_ok": model_installed(s.get("ollama_model", ""), ol["models"]),
        "has_ai": has_ai,
        "os": platform.system(),
        "version": VERSION,
    })


pull_state = {"active": False, "model": "", "status": "", "pct": 0.0, "error": ""}


def _pull(model: str, url: str) -> None:
    import urllib.request
    pull_state.update(active=True, model=model, status="Starte Download…", pct=0.0, error="")
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/pull",
                                     data=json.dumps({"model": model, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3600) as r:
            for raw in r:
                ev = json.loads(raw or b"{}")
                if ev.get("error"):
                    raise RuntimeError(ev["error"])
                pull_state["status"] = ev.get("status", "")
                if ev.get("total"):
                    pull_state["pct"] = round(100 * ev.get("completed", 0) / ev["total"], 1)
        pull_state.update(status="Fertig", pct=100.0)
    except Exception as e:
        pull_state["error"] = str(e)
    finally:
        pull_state["active"] = False


@app.post("/api/ollama/pull")
def ollama_pull():
    s = load_settings()
    model = (request.get_json(silent=True) or {}).get("model") or s["ollama_model"]
    if not pull_state["active"]:
        threading.Thread(target=_pull, args=(model, s["ollama_url"]), daemon=True).start()
    return jsonify(pull_state)


@app.get("/api/ollama/pull")
def ollama_pull_status():
    return jsonify(pull_state)


# ----------------------------------------------------------------------------- updates
def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def _repo() -> str:
    return load_settings().get("update_repo") or UPDATE_REPO


def _fetch(url: str, timeout: int = 15) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "clip-studio", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


@app.get("/api/update/check")
def update_check():
    repo = _repo()
    if not repo:
        return jsonify({"current": VERSION, "configured": False})
    try:
        latest = _fetch(f"https://raw.githubusercontent.com/{repo}/{UPDATE_BRANCH}/VERSION?t={int(time.time())}").decode().strip()
    except Exception as e:
        return jsonify({"current": VERSION, "configured": True, "error": f"GitHub nicht erreichbar: {e}"})
    notes = ""
    try:
        cl = _fetch(f"https://raw.githubusercontent.com/{repo}/{UPDATE_BRANCH}/CHANGELOG.md?t={int(time.time())}").decode()
        m = re.search(r"^## .*?(?=^## |\Z)", cl, re.M | re.S)  # newest section
        notes = m.group(0).strip() if m else ""
    except Exception:
        pass
    return jsonify({"current": VERSION, "latest": latest, "configured": True, "repo": repo,
                    "available": _vtuple(latest) > _vtuple(VERSION), "notes": notes})


@app.post("/api/update/apply")
def update_apply():
    import io
    import zipfile
    repo = _repo()
    if not repo:
        return jsonify({"error": "Kein Update-Repository eingestellt"}), 400
    with lock:
        if any(j["status"] in ("running", "queued") for j in jobs.values()):
            return jsonify({"error": "Bitte warten, bis alle Projekte fertig sind"}), 409
    try:
        data = _fetch(f"https://codeload.github.com/{repo}/zip/refs/heads/{UPDATE_BRANCH}", timeout=120)
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        return jsonify({"error": f"Download fehlgeschlagen: {e}"}), 502
    names = zf.namelist()
    root = names[0].split("/")[0] + "/"
    if not any(n == root + "app.py" for n in names):
        return jsonify({"error": "Update-Paket sieht falsch aus (kein app.py)"}), 502
    written = 0
    for n in names:
        rel = n[len(root):]
        if not rel or n.endswith("/") or rel.split("/")[0] in KEEP_ON_UPDATE:
            continue
        dest = (BASE / rel).resolve()
        if BASE not in dest.parents:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".new")
        tmp.write_bytes(zf.read(n))
        os.replace(tmp, dest)
        written += 1
    new_version = (BASE / "VERSION").read_text(encoding="utf-8").strip()
    # restart: the start script reinstalls requirements and starts the app again
    threading.Timer(1.0, lambda: os._exit(RESTART_CODE)).start()
    return jsonify({"ok": True, "files": written, "version": new_version})


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    key = s.pop("api_key", "")
    s["api_key_hint"] = (key[:7] + "…" + key[-4:]) if key else ""
    return jsonify(s)


@app.post("/api/settings")
def post_settings():
    s = load_settings()
    data = request.get_json(force=True) or {}
    for k in DEFAULT_SETTINGS:
        if k in data and data[k] is not None:
            if k == "api_key" and data[k] == "":
                continue  # empty field = keep existing key
            s[k] = str(data[k]).strip()
    if data.get("clear_key"):
        s["api_key"] = ""
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return get_settings()


@app.get("/api/jobs")
def list_jobs():
    with lock:
        items = sorted(jobs.values(), key=lambda j: -j["created"])
        return jsonify([summary(j) for j in items])


@app.post("/api/jobs")
def create_job():
    if request.content_type and request.content_type.startswith("multipart/"):
        params = json.loads(request.form.get("params", "{}"))
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "Keine Datei"}), 400
        jid_tmp = uuid.uuid4().hex[:10]
        up = JOBS / "_uploads" / jid_tmp
        up.mkdir(parents=True, exist_ok=True)
        dest = up / safe_name(f.filename or "video.mp4")
        f.save(dest)
        params["source"] = str(dest)
        title = Path(f.filename or "Video").stem
    else:
        params = request.get_json(force=True) or {}
        src = (params.get("source") or "").strip().strip('"')
        if not src:
            return jsonify({"error": "Bitte einen Link oder eine Datei angeben"}), 400
        if not re.match(r"^https?://", src) and not Path(src).exists():
            return jsonify({"error": "Datei nicht gefunden: " + src}), 400
        params["source"] = src
        title = Path(src).stem if Path(src).exists() else src
    job = new_job(params, title)
    return jsonify(summary(job))


@app.get("/api/jobs/<jid>")
def get_job(jid):
    d = job_dir(jid)
    with lock:
        job = jobs.get(jid)
        if not job:
            abort(404)
        job = json.loads(json.dumps(job))
    log = (d / "log.txt").read_text(encoding="utf-8", errors="replace").splitlines()[-200:] \
        if (d / "log.txt").exists() else []
    job["log"] = log
    job["clips"] = read_clips(jid)
    job["queue_pos"] = list(work_q.queue).index(jid) + 1 if jid in list(work_q.queue) else 0
    return jsonify(job)


@app.post("/api/jobs/<jid>/cancel")
def cancel_job(jid):
    job_dir(jid)
    with lock:
        job = jobs.get(jid) or abort(404)
        if job["status"] in ("queued", "running"):
            job["status"] = "cancelled"
            job["stage"] = "Abgebrochen"
            save_job(job)
    p = procs.get(jid)
    if p and p.poll() is None:
        p.terminate()
    return jsonify(summary(jobs[jid]))


@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    d = job_dir(jid)
    cancel_job(jid)
    p = procs.get(jid)
    if p:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    with lock:
        job = jobs.pop(jid, None)
    src = (job or {}).get("params", {}).get("source", "")
    if "_uploads" in src:
        shutil.rmtree(Path(src).parent, ignore_errors=True)
    shutil.rmtree(d, ignore_errors=True)
    return jsonify({"ok": True})


@app.post("/api/jobs/<jid>/retry")
def retry_job(jid):
    """Run a failed/cancelled job again in place - keeps download, transcript parts etc."""
    job_dir(jid)
    with lock:
        job = jobs.get(jid) or abort(404)
        if job["status"] not in ("error", "cancelled"):
            return jsonify({"error": "Projekt läuft bereits oder ist fertig"}), 400
        job.update(status="queued", progress=0, error="", stage="In Warteschlange")
        save_job(job)
    log_line(jid, "\n----- erneuter Versuch -----")
    work_q.put(jid)
    return jsonify(summary(job))


@app.post("/api/jobs/<jid>/rerun")
def rerun_job(jid):
    """New job with changed settings; reuses download + transcript of the old one."""
    old = job_dir(jid)
    with lock:
        src_job = jobs.get(jid) or abort(404)
        base_params = dict(src_job["params"])
        title = re.sub(r" \(neu geschnitten\)$", "", src_job.get("title", "")) + " (neu geschnitten)"
    params = {**base_params, **(request.get_json(force=True) or {})}
    params["source"] = base_params["source"]
    job = new_job(params, title, cache_from=old / "clips" / ".work")
    return jsonify(summary(job))


@app.get("/media/<jid>/<fname>")
def media(jid, fname):
    p = clip_file(jid, fname)
    if not p.exists():
        abort(404)
    return send_file(p, conditional=True,
                     as_attachment=request.args.get("download") == "1")


@app.get("/thumb/<jid>/<fname>")
def thumb(jid, fname):
    p = clip_file(jid, fname)
    if not p.exists():
        abort(404)
    t = p.parent / ".work" / (p.stem + ".thumb.jpg")
    if not t.exists() or t.stat().st_mtime < p.stat().st_mtime:
        t.parent.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(p), "-frames:v", "1",
                        "-vf", "scale=540:-2", "-q:v", "4", str(t)], check=False)
    if not t.exists():
        abort(404)
    return send_file(t, max_age=3600)


@app.post("/api/jobs/<jid>/clips/<fname>")
def update_clip(jid, fname):
    p = clip_file(jid, fname)
    data = request.get_json(force=True) or {}
    if "post_text" in data:
        p.with_suffix(".txt").write_text(data["post_text"], encoding="utf-8")
    if "posted" in data:
        with lock:
            job = jobs[jid]
            job.setdefault("posted", {})[p.name] = data["posted"]
            save_job(job)
    return jsonify({"ok": True})


@app.delete("/api/jobs/<jid>/clips/<fname>")
def delete_clip(jid, fname):
    p = clip_file(jid, fname)
    clips = [c for c in read_clips(jid) if c["file"] != p.name]
    write_clips(jid, clips)
    for f in (p, p.with_suffix(".txt")):
        f.unlink(missing_ok=True)
    with lock:
        if jid in jobs:
            jobs[jid]["clip_count"] = len(clips)
            save_job(jobs[jid])
    return jsonify({"ok": True})


@app.post("/api/jobs/<jid>/open")
def open_folder(jid):
    d = job_dir(jid) / "clips"
    target = d
    fname = (request.get_json(silent=True) or {}).get("file")
    try:
        if platform.system() == "Windows":
            if fname:
                subprocess.Popen(["explorer", "/select,", str(clip_file(jid, fname))])
            else:
                os.startfile(str(target))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", str(clip_file(jid, fname))] if fname else ["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as e:
        return jsonify({"error": str(e), "path": str(target)}), 500
    return jsonify({"ok": True, "path": str(target)})


# ----------------------------------------------------------------------------- main
def main() -> None:
    fix_path()
    load_all_jobs()
    threading.Thread(target=worker, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  Clip Studio läuft auf {url}\n  (Fenster offen lassen, Strg+C zum Beenden)\n", flush=True)
    if not os.environ.get("CLIPSTUDIO_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
