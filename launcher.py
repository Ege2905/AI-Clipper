#!/usr/bin/env python3
"""
Keeps Clip Studio running.

  python launcher.py               foreground (console window, opens the browser)
  pythonw launcher.py --background  background mode used by autostart (no window, logs to server.log)

Restarts app.py after an in-app update (exit code 42) and, in background mode, after crashes.
stop_server.bat creates stop.flag and ends the process tree.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
RESTART_CODE = 42
BG = "--background" in sys.argv
WIN = os.name == "nt"


def console_python() -> str:
    """python.exe next to pythonw.exe - so child processes get a (hidden) console."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return sys.executable


def log(msg: str, out) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    if out:
        out.write(line)
        out.flush()
    else:
        print(line, end="", flush=True)


def main() -> None:
    os.chdir(BASE)
    out = open(BASE / "server.log", "a", encoding="utf-8", buffering=1) if BG else None
    (BASE / "stop.flag").unlink(missing_ok=True)
    (BASE / "launcher.pid").write_text(str(os.getpid()))
    py = console_python()
    flags = subprocess.CREATE_NO_WINDOW if (WIN and BG) else 0
    first = True
    while True:
        if not first or BG:  # after an update requirements may have changed
            log("Pakete prüfen ...", out)
            subprocess.call([py, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                             "-r", "requirements.txt"], stdout=out, stderr=out, creationflags=flags)
        env = dict(os.environ)
        if BG or not first:
            env["CLIPSTUDIO_NO_BROWSER"] = "1"
        log("Clip Studio startet", out)
        try:
            rc = subprocess.call([py, "app.py"], stdout=out, stderr=out, env=env, creationflags=flags)
        except KeyboardInterrupt:
            break
        first = False
        if (BASE / "stop.flag").exists():
            break
        if rc == RESTART_CODE:
            log("Update/Neustart - starte neu", out)
            continue
        if not BG:
            break  # foreground: window closed / Ctrl+C / crash -> stop
        log(f"Clip Studio beendet (Code {rc}) - Neustart in 15 s", out)
        for _ in range(15):
            time.sleep(1)
            if (BASE / "stop.flag").exists():
                break
        if (BASE / "stop.flag").exists():
            break
    (BASE / "stop.flag").unlink(missing_ok=True)
    (BASE / "launcher.pid").unlink(missing_ok=True)
    log("Clip Studio gestoppt", out)


if __name__ == "__main__":
    main()
