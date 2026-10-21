#!/usr/bin/env python3
"""
Clip Studio - web app around clipper.py.

Runs on your own PC (uses your GPU / Ollama) and is reachable in the browser:
  http://127.0.0.1:7860            on this PC
  https://<your tunnel address>     for invited users (Tailscale Funnel / Cloudflare Tunnel)

Start:  python app.py   (or start_windows.bat; autostart_einrichten.bat for background mode)
"""
from __future__ import annotations

import io
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
import zipfile
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import Accounts, install as install_auth

BASE = Path(__file__).resolve().parent
JOBS = BASE / "jobs"
UPLOADS = JOBS / "_uploads"
SETTINGS_FILE = BASE / "settings.json"
CLIPPER = BASE / "clipper.py"
PORT = int(os.environ.get("CLIPSTUDIO_PORT", "7860"))
HOST = os.environ.get("CLIPSTUDIO_HOST", "127.0.0.1")
VERSION = (BASE / "VERSION").read_text(encoding="utf-8").strip() if (BASE / "VERSION").exists() else "0"
RESTART_CODE = 42  # start scripts restart the app when it exits with this code
UPDATE_REPO = "Ege2905/AI-Clipper"
UPDATE_BRANCH = "main"
KEEP_ON_UPDATE = {"jobs", "venv", "settings.json", ".git", "users.json", ".secret_key",
                  "server.log", "app.pid", "launcher.pid", "stop.flag"}
CHUNK_MAX = 64 * 1024 * 1024  # max bytes per upload request (tunnels limit request sizes)

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
    "auto_update": True,
    "public_url": "",
    "max_jobs_per_user": 2,
    "max_upload_gb": 20,
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
app.config["MAX_CONTENT_LENGTH"] = CHUNK_MAX + 1024 * 1024
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # behind a tunnel
accounts = Accounts(BASE)
PUBLIC_PATHS = {"/", "/api/me", "/api/setup", "/api/login", "/api/register", "/api/ping",
                "/manifest.webmanifest", "/sw.js", "/icon-192.png", "/icon-512.png", "/icon.svg"}
ADMIN_PREFIXES = ("/api/settings", "/api/update", "/api/ollama", "/api/admin")
install_auth(app, accounts, PUBLIC_PATHS, ADMIN_PREFIXES)

lock = threading.Lock()
work_q: "queue.Queue[str]" = queue.Queue()
jobs: dict[str, dict] = {}
procs: dict[str, subprocess.Popen] = {}
ID_RE = re.compile(r"^[a-f0-9]{10}$")
UP_RE = re.compile(r"^[a-f0-9]{16}$")


# ----------------------------------------------------------------------------- helpers
def fix_path() -> None:
    """Make ffmpeg/deno findable right after a fresh winget/brew install."""
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


def save_settings(s: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def is_admin() -> bool:
    return request.user["role"] == "admin"


def job_dir(jid: str) -> Path:
    if not ID_RE.match(jid):
        abort(404)
    return JOBS / jid


def own_job(jid: str) -> dict:
    """The job if the current user may see it (owner or admin), else 404."""
    job_dir(jid)
    job = jobs.get(jid)
    if not job:
        abort(404)
    owner = job.get("owner")
    if not is_admin() and owner != request.user["name"]:
        abort(404)
    return job


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
            job["error"] = "Unterbrochen (App wurde neu gestartet) – „Erneut versuchen“ macht weiter"
            save_job(job)
        jobs[job["id"]] = job


def adopt_ownerless_jobs(admin: str) -> None:
    """Projects from before accounts existed belong to the admin."""
    with lock:
        for job in jobs.values():
            if not job.get("owner"):
                job["owner"] = admin
                save_job(job)


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


SECTION_RE = re.compile(r"^\d{1,2}(:\d{2}){0,2}-\d{1,2}(:\d{2}){0,2}$")
FACECAM_RE = re.compile(r"^\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*$")


def clean_params(p: dict, admin: bool) -> dict:
    """Whitelist + validate job parameters coming from the browser."""
    try:
        return _clean_params(p, admin)
    except (TypeError, ValueError):
        abort(jsonify_error("Ungültige Einstellungen", 400))


def _clean_params(p: dict, admin: bool) -> dict:
    out = {
        "num_clips": max(1, min(20, int(p.get("num_clips", 5)))),
        "min": max(5.0, min(170.0, float(p.get("min", 20)))),
        "max": max(10.0, min(180.0, float(p.get("max", 60)))),
        "layout": p.get("layout") if p.get("layout") in ("auto", "track", "split", "fit") else "auto",
        "language": p.get("language") if re.match(r"^[a-z]{2}$", p.get("language") or "") else "",
        "whisper_model": p.get("whisper_model") if p.get("whisper_model") in
        ("tiny", "base", "small", "medium", "large-v3") else "",
        "words_per_line": max(1, min(4, int(p.get("words_per_line", 3)))),
        "upper": bool(p.get("upper", True)),
        "title_overlay": bool(p.get("title_overlay", True)),
        "ai": bool(p.get("ai")),
        "section": p.get("section", "").replace(" ", "") if SECTION_RE.match(p.get("section", "").replace(" ", "")) else "",
        "facecam": p.get("facecam", "") if FACECAM_RE.match(p.get("facecam", "") or "") else "",
        "hashtags": " ".join(t for t in re.split(r"\s+", p.get("hashtags", "") or "")
                             if re.match(r"^#[\w]{1,40}$", t, re.UNICODE))[:300],
    }
    if out["min"] >= out["max"]:
        out["min"] = max(5.0, out["max"] - 10)
    if admin and p.get("transcript"):
        out["transcript"] = str(p["transcript"])
    return out


# ----------------------------------------------------------------------------- worker
def build_cmd(job: dict, settings: dict) -> list[str]:
    p = job["params"]
    cmd = [sys.executable, "-u", str(CLIPPER), p["source"],
           "-n", str(int(p.get("num_clips", 5))),
           "--min", str(float(p.get("min", 20))), "--max", str(float(p.get("max", 60))),
           "--layout", p.get("layout", "auto"),
           "--whisper-model", p.get("whisper_model") or settings["whisper_model"],
           "--font", settings["font"],
           "--words-per-line", str(int(p.get("words_per_line", 3))),
           "-o", str(JOBS / job["id"] / "clips")]
    lang = p.get("language") or settings["language"]
    if lang:
        cmd += ["--language", lang]
    if p.get("section"):
        cmd += ["--section", p["section"]]
    if p.get("transcript"):  # admin only: reuse an existing transcript.json
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


def new_job(params: dict, title: str, owner: str, cache_from: Path | None = None) -> dict:
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "title": title, "params": params, "status": "queued", "progress": 0,
           "stage": "In Warteschlange", "created": time.time(), "posted": {}, "error": "",
           "owner": owner}
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
            elif f.is_dir():
                shutil.copytree(f, new_work / f.name, dirs_exist_ok=True)
    with lock:
        jobs[jid] = job
        save_job(job)
    work_q.put(jid)
    return job


def active_jobs_of(user: str) -> int:
    return sum(1 for j in jobs.values() if j.get("owner") == user and j["status"] in ("queued", "running"))


def check_quota() -> None:
    if is_admin():
        return
    limit = int(load_settings().get("max_jobs_per_user", 2))
    if active_jobs_of(request.user["name"]) >= limit:
        abort(jsonify_error(f"Du hast schon {limit} Projekte in Arbeit – warte, bis eins fertig ist.", 429))


def jsonify_error(msg: str, code: int):
    r = jsonify({"error": msg})
    r.status_code = code
    return r


def summary(job: dict) -> dict:
    keys = ("id", "title", "status", "progress", "stage", "created", "error", "clip_count", "owner")
    return {k: job.get(k) for k in keys}


# ----------------------------------------------------------------------------- pages / pwa
@app.get("/")
def index():
    r = send_from_directory(BASE / "ui", "index.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(BASE / "ui", "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    r = send_from_directory(BASE / "ui", "sw.js", mimetype="text/javascript")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/icon-192.png")
@app.get("/icon-512.png")
@app.get("/icon.svg")
def icons():
    return send_from_directory(BASE / "ui", request.path.lstrip("/"), max_age=86400)


@app.get("/api/ping")
def ping():
    return jsonify({"ok": True, "app": "clip-studio", "version": VERSION})


@app.after_request
def security_headers(r):
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("X-Frame-Options", "DENY")
    r.headers.setdefault("Referrer-Policy", "same-origin")
    return r


# ----------------------------------------------------------------------------- system / ai
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
    s = load_settings()
    ol = ollama_status(s.get("ollama_url", DEFAULT_SETTINGS["ollama_url"]))
    if s.get("ai_provider") == "anthropic":
        has_ai = bool(s.get("api_key")) and has_mod("anthropic")
    else:
        has_ai = ol["running"] and model_installed(s.get("ollama_model", ""), ol["models"])
    info = {"has_ai": has_ai, "provider": s.get("ai_provider", "ollama"), "version": VERSION,
            "ollama_model": s.get("ollama_model"), "max_upload_gb": s.get("max_upload_gb", 20)}
    if not is_admin():  # guests only need to know whether AI is available
        return jsonify(info)
    import cv2
    return jsonify({**info,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "whisper": has_mod("faster_whisper"),
        "ytdlp": has_mod("yt_dlp"),
        "opencv_ok": hasattr(cv2, "CascadeClassifier"),
        "anthropic": has_mod("anthropic"),
        "has_key": bool(s.get("api_key")),
        "ollama": ol,
        "ollama_model_ok": model_installed(s.get("ollama_model", ""), ol["models"]),
        "os": platform.system(),
        "local": request.remote_addr in ("127.0.0.1", "::1") and not request.headers.get("X-Forwarded-For"),
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
    if not re.match(r"^[\w.:/\-]{1,80}$", model):
        return jsonify_error("Ungültiger Modellname", 400)
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


def check_for_update() -> dict:
    repo = _repo()
    if not repo:
        return {"current": VERSION, "configured": False}
    try:
        latest = _fetch(f"https://raw.githubusercontent.com/{repo}/{UPDATE_BRANCH}/VERSION?t={int(time.time())}").decode().strip()
    except Exception as e:
        return {"current": VERSION, "configured": True, "error": f"GitHub nicht erreichbar: {e}"}
    notes = ""
    try:
        cl = _fetch(f"https://raw.githubusercontent.com/{repo}/{UPDATE_BRANCH}/CHANGELOG.md?t={int(time.time())}").decode()
        m = re.search(r"^## .*?(?=^## |\Z)", cl, re.M | re.S)  # newest section
        notes = m.group(0).strip() if m else ""
    except Exception:
        pass
    return {"current": VERSION, "latest": latest, "configured": True, "repo": repo,
            "available": _vtuple(latest) > _vtuple(VERSION), "notes": notes}


def busy() -> bool:
    return any(j["status"] in ("running", "queued") for j in jobs.values())


def install_update() -> dict:
    """Download the repo zip from GitHub, overwrite program files, then restart."""
    repo = _repo()
    data = _fetch(f"https://codeload.github.com/{repo}/zip/refs/heads/{UPDATE_BRANCH}", timeout=120)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    root = names[0].split("/")[0] + "/"
    if not any(n == root + "app.py" for n in names):
        raise RuntimeError("Update-Paket sieht falsch aus (kein app.py)")
    written = 0
    for n in names:
        rel = n[len(root):]
        if not rel or n.endswith("/") or rel.split("/")[0] in KEEP_ON_UPDATE:
            continue
        dest = (BASE / rel).resolve()
        if BASE not in dest.parents:
            continue
        content = zf.read(n)
        if dest.exists() and dest.read_bytes() == content:
            continue  # unchanged (also avoids touching running .bat files)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".new")
        tmp.write_bytes(content)
        os.replace(tmp, dest)
        written += 1
    new_version = (BASE / "VERSION").read_text(encoding="utf-8").strip()
    print(f"[update] installed {new_version} ({written} files), restarting", flush=True)
    threading.Timer(1.0, lambda: os._exit(RESTART_CODE)).start()
    return {"ok": True, "files": written, "version": new_version}


@app.get("/api/update/check")
def update_check():
    return jsonify(check_for_update())


@app.post("/api/update/apply")
def update_apply():
    if busy():
        return jsonify_error("Bitte warten, bis alle Projekte fertig sind", 409)
    try:
        return jsonify(install_update())
    except Exception as e:
        return jsonify_error(f"Update fehlgeschlagen: {e}", 502)


def auto_updater() -> None:
    """Checks GitHub every 3 hours; installs when enabled and nothing is running."""
    time.sleep(60)
    while True:
        try:
            if load_settings().get("auto_update", True):
                info = check_for_update()
                if info.get("available"):
                    while busy():
                        time.sleep(120)
                    install_update()
                    return
        except Exception as e:
            print(f"[update] auto update failed: {e}", flush=True)
        time.sleep(3 * 3600)


# ----------------------------------------------------------------------------- settings / admin
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
    for k, default in DEFAULT_SETTINGS.items():
        if k in data and data[k] is not None:
            if k == "api_key" and data[k] == "":
                continue  # empty field = keep existing key
            if isinstance(default, bool):
                s[k] = bool(data[k])
            elif isinstance(default, int):
                try:
                    s[k] = max(1, int(data[k]))
                except (TypeError, ValueError):
                    pass
            else:
                s[k] = str(data[k]).strip()
    if data.get("clear_key"):
        s["api_key"] = ""
    save_settings(s)
    return get_settings()


@app.post("/api/admin/restart")
def admin_restart():
    if busy():
        return jsonify_error("Bitte warten, bis alle Projekte fertig sind", 409)
    threading.Timer(0.8, lambda: os._exit(RESTART_CODE)).start()
    return jsonify({"ok": True})


@app.get("/api/admin/log")
def admin_log():
    f = BASE / "server.log"
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-300:] if f.exists() else []
    return jsonify({"log": lines})


# ----------------------------------------------------------------------------- jobs
@app.get("/api/jobs")
def list_jobs():
    with lock:
        items = sorted(jobs.values(), key=lambda j: -j["created"])
        if not is_admin():
            items = [j for j in items if j.get("owner") == request.user["name"]]
        return jsonify([summary(j) for j in items])


@app.post("/api/jobs")
def create_job():
    check_quota()
    raw = request.get_json(force=True) or {}
    params = clean_params(raw, is_admin())
    src = (raw.get("source") or "").strip().strip('"')
    if not src:
        return jsonify_error("Bitte einen Link oder eine Datei angeben", 400)
    if re.match(r"^https?://[^\s]+$", src):
        title = src
    elif is_admin() and Path(src).is_file():  # local file paths: only the PC owner
        title = Path(src).stem
    else:
        return jsonify_error("Bitte einen gültigen Link (https://…) angeben oder eine Datei hochladen", 400)
    params["source"] = src
    job = new_job(params, title, request.user["name"])
    return jsonify(summary(job))


# chunked uploads: POST /api/uploads -> PUT /api/uploads/<id>?offset=N (repeat) -> POST .../finish
def _upload(uid: str) -> tuple[Path, dict]:
    if not UP_RE.match(uid):
        abort(404)
    d = UPLOADS / uid
    meta_f = d / "meta.json"
    if not meta_f.exists():
        abort(404)
    meta = json.loads(meta_f.read_text(encoding="utf-8"))
    if meta["owner"] != request.user["name"]:
        abort(404)
    return d, meta


@app.post("/api/uploads")
def upload_start():
    check_quota()
    data = request.get_json(force=True) or {}
    size = int(data.get("size", 0))
    limit = float(load_settings().get("max_upload_gb", 20)) * 1024 ** 3
    if size <= 0 or size > limit:
        return jsonify_error(f"Datei zu groß (max. {load_settings().get('max_upload_gb', 20)} GB)", 413)
    free = shutil.disk_usage(JOBS).free
    if size * 1.5 > free:
        return jsonify_error("Nicht genug Speicherplatz auf dem Server", 507)
    uid = uuid.uuid4().hex[:16]
    d = UPLOADS / uid
    d.mkdir(parents=True)
    name = safe_name(data.get("name") or "video.mp4")
    meta = {"owner": request.user["name"], "name": name, "size": size, "created": time.time()}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / (name + ".part")).touch()
    return jsonify({"id": uid, "chunk": 32 * 1024 * 1024, "received": 0})


@app.put("/api/uploads/<uid>")
def upload_chunk(uid):
    d, meta = _upload(uid)
    part = d / (meta["name"] + ".part")
    have = part.stat().st_size
    offset = int(request.args.get("offset", -1))
    if offset != have:  # client resends from what we actually have
        return jsonify({"received": have, "resync": True}), 409
    body = request.get_data(cache=False)
    if have + len(body) > meta["size"]:
        return jsonify_error("Mehr Daten als angekündigt", 400)
    with open(part, "ab") as fh:
        fh.write(body)
    return jsonify({"received": have + len(body)})


@app.get("/api/uploads/<uid>")
def upload_status(uid):
    d, meta = _upload(uid)
    return jsonify({"received": (d / (meta["name"] + ".part")).stat().st_size, "size": meta["size"]})


@app.post("/api/uploads/<uid>/finish")
def upload_finish(uid):
    check_quota()
    d, meta = _upload(uid)
    part = d / (meta["name"] + ".part")
    if part.stat().st_size != meta["size"]:
        return jsonify_error("Upload unvollständig", 400)
    final = d / meta["name"]
    os.replace(part, final)
    params = clean_params(request.get_json(force=True) or {}, is_admin())
    params["source"] = str(final)
    job = new_job(params, Path(meta["name"]).stem, request.user["name"])
    meta["job"] = job["id"]
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return jsonify(summary(job))


@app.get("/api/jobs/<jid>")
def get_job(jid):
    d = job_dir(jid)
    with lock:
        job = json.loads(json.dumps(own_job(jid)))
    log = (d / "log.txt").read_text(encoding="utf-8", errors="replace").splitlines()[-200:] \
        if (d / "log.txt").exists() else []
    if not is_admin():  # don't leak server paths to guests
        log = [re.sub(r"([A-Za-z]:)?[\\/][^\s\"']*[\\/]", "…/", l) for l in log]
        job["params"] = {k: v for k, v in job["params"].items() if k != "transcript"}
        if not re.match(r"^https?://", job["params"].get("source", "")):
            job["params"]["source"] = "Hochgeladene Datei"
    job["log"] = log
    job["clips"] = read_clips(jid)
    q = list(work_q.queue)
    job["queue_pos"] = q.index(jid) + 1 if jid in q else 0
    return jsonify(job)


@app.post("/api/jobs/<jid>/cancel")
def cancel_job(jid):
    with lock:
        job = own_job(jid)
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
    own_job(jid)
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
    if "_uploads" in src and Path(src).parent.parent == UPLOADS:
        shutil.rmtree(Path(src).parent, ignore_errors=True)
    shutil.rmtree(d, ignore_errors=True)
    return jsonify({"ok": True})


@app.post("/api/jobs/<jid>/retry")
def retry_job(jid):
    """Run a failed/cancelled job again in place - keeps download, transcript parts etc."""
    with lock:
        job = own_job(jid)
        if job["status"] not in ("error", "cancelled"):
            return jsonify_error("Projekt läuft bereits oder ist fertig", 400)
    check_quota()
    with lock:
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
        src_job = own_job(jid)
        base_params = dict(src_job["params"])
        title = re.sub(r" \(neu geschnitten\)$", "", src_job.get("title", "")) + " (neu geschnitten)"
        owner = src_job.get("owner") or request.user["name"]
    check_quota()
    params = clean_params({**base_params, **(request.get_json(force=True) or {})}, is_admin())
    params["source"] = base_params["source"]
    if base_params.get("transcript"):
        params["transcript"] = base_params["transcript"]
    job = new_job(params, title, owner, cache_from=old / "clips" / ".work")
    return jsonify(summary(job))


@app.get("/media/<jid>/<fname>")
def media(jid, fname):
    own_job(jid)
    p = clip_file(jid, fname)
    if not p.exists():
        abort(404)
    return send_file(p, conditional=True,
                     as_attachment=request.args.get("download") == "1")


@app.get("/thumb/<jid>/<fname>")
def thumb(jid, fname):
    own_job(jid)
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
    own_job(jid)
    p = clip_file(jid, fname)
    data = request.get_json(force=True) or {}
    if "post_text" in data:
        p.with_suffix(".txt").write_text(str(data["post_text"])[:5000], encoding="utf-8")
    if "posted" in data and isinstance(data["posted"], dict):
        with lock:
            job = jobs[jid]
            job.setdefault("posted", {})[p.name] = {k: bool(v) for k, v in data["posted"].items()
                                                    if k in ("yt", "ig", "tt")}
            save_job(job)
    return jsonify({"ok": True})


@app.delete("/api/jobs/<jid>/clips/<fname>")
def delete_clip(jid, fname):
    own_job(jid)
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


@app.post("/api/admin/open/<jid>")
def open_folder(jid):
    """Opens the clip folder in Explorer - only useful when sitting at the server PC."""
    own_job(jid)
    d = job_dir(jid) / "clips"
    fname = (request.get_json(silent=True) or {}).get("file")
    try:
        if platform.system() == "Windows":
            if fname:
                subprocess.Popen(["explorer", "/select,", str(clip_file(jid, fname))])
            else:
                os.startfile(str(d))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", str(clip_file(jid, fname))] if fname else ["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])
    except Exception as e:
        return jsonify({"error": str(e), "path": str(d)}), 500
    return jsonify({"ok": True, "path": str(d)})


def cleanup_uploads() -> None:
    """Remove abandoned uploads older than 2 days (finished ones belong to a job)."""
    if not UPLOADS.exists():
        return
    for d in UPLOADS.iterdir():
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            if not meta.get("job") and time.time() - meta["created"] > 2 * 86400:
                shutil.rmtree(d, ignore_errors=True)
        except (OSError, ValueError, KeyError):
            continue


# ----------------------------------------------------------------------------- main
def main() -> None:
    fix_path()
    load_all_jobs()
    cleanup_uploads()
    admins = [u["name"] for u in accounts.list() if u["role"] == "admin"]
    if admins:
        adopt_ownerless_jobs(admins[0])
    (BASE / "app.pid").write_text(str(os.getpid()))
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=auto_updater, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  Clip Studio {VERSION} läuft auf {url}\n", flush=True)
    if not os.environ.get("CLIPSTUDIO_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        from waitress import serve
        serve(app, host=HOST, port=PORT, threads=12, channel_timeout=600,
              max_request_body_size=CHUNK_MAX + 1024 * 1024, ident="clip-studio")
    except ImportError:
        app.run(host=HOST, port=PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
