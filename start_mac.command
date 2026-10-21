#!/bin/bash
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if curl -s -m 2 http://127.0.0.1:7860/api/ping >/dev/null 2>&1; then open http://127.0.0.1:7860; exit 0; fi
if ! command -v python3 >/dev/null; then
  echo "Python 3 fehlt: https://www.python.org/downloads/"; open https://www.python.org/downloads/; read -p "Enter zum Beenden"; exit 1
fi
if ! command -v ffmpeg >/dev/null; then
  if command -v brew >/dev/null; then echo "ffmpeg wird installiert (einmalig)..."; brew install ffmpeg
  else echo "ffmpeg fehlt. Installiere Homebrew (https://brew.sh) und dann: brew install ffmpeg"; read -p "Enter zum Beenden"; exit 1; fi
fi
if ! command -v deno >/dev/null && command -v brew >/dev/null; then
  echo "deno wird installiert (einmalig, für YouTube-Downloads)..."; brew install deno
fi
if [ ! -d venv ]; then
  echo "Erster Start: richte Clip Studio ein (dauert ein paar Minuten)..."
  python3 -m venv venv && venv/bin/python -m pip install -q --upgrade pip
fi
venv/bin/python -m pip install -q -r requirements.txt || { read -p "Installation fehlgeschlagen. Enter zum Beenden"; exit 1; }
venv/bin/python launcher.py
