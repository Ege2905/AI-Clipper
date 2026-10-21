#!/bin/bash
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if ! command -v python3 >/dev/null; then
  echo "Python 3 fehlt: https://www.python.org/downloads/"; open https://www.python.org/downloads/; read -p "Enter zum Beenden"; exit 1
fi
if ! command -v ffmpeg >/dev/null; then
  if command -v brew >/dev/null; then
    echo "ffmpeg wird installiert (einmalig)..."; brew install ffmpeg
  else
    echo "ffmpeg fehlt. Installiere zuerst Homebrew (https://brew.sh) und dann: brew install ffmpeg"
    read -p "Enter zum Beenden"; exit 1
  fi
fi
if ! command -v deno >/dev/null && command -v brew >/dev/null; then
  echo "deno wird installiert (einmalig, für YouTube-Downloads)..."; brew install deno
fi
if [ ! -d venv ]; then
  echo "Erster Start: richte Clip Studio ein (dauert ein paar Minuten)..."
  python3 -m venv venv
fi
source venv/bin/activate
python -m pip install -q --upgrade pip
while true; do
  python -m pip install -q -r requirements.txt || { read -p "Installation fehlgeschlagen. Enter zum Beenden"; exit 1; }
  python app.py
  if [ $? -ne 42 ]; then break; fi
  echo "Update installiert - Clip Studio startet neu..."
  export CLIPSTUDIO_NO_BROWSER=1
done
