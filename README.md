# AI-Clipper · Clip Studio

Creates clips of streams and videos with an AI that runs on your own system.

Clip Studio ist eine Web-App, die lange Videos (Stream-VODs, Podcasts, Interviews) automatisch in
fertige 9:16-Clips mit Untertiteln verwandelt – für YouTube Shorts, Instagram Reels und TikTok.
Sie läuft auf **deinem PC als Server** (nutzt deine Grafikkarte und lokale KI über Ollama) und wird
im Browser bedient – auf dem PC, auf dem Handy und von eingeladenen Freunden.

![Layouts](example_layouts.png)

## Einmal einrichten (Windows)

1. **Python 3.12 oder 3.13** von <https://www.python.org/downloads/> installieren
   („Add python.exe to PATH“ anhaken).
2. Dieses Repo herunterladen: **Code → Download ZIP**, entpacken, z.B. nach `Videos\clip-studio`.
3. **`autostart_einrichten.bat`** doppelklicken. Das installiert alles (Python-Pakete, ffmpeg, deno),
   startet Clip Studio unsichtbar im Hintergrund und trägt es in den Autostart ein.
4. Im Browser öffnet sich <http://127.0.0.1:7860> → **Admin-Konto anlegen**. Lesezeichen setzen – fertig.

Ab jetzt läuft Clip Studio immer, wenn dein PC an ist. Kein Fenster, kein Starten, keine Downloads:
**Updates kommen automatisch von GitHub** (alle 3 Stunden geprüft, installiert wenn gerade nichts läuft;
abschaltbar unter ⚙ Einstellungen → Updates).

| Datei | wofür |
|---|---|
| `autostart_einrichten.bat` | Hintergrund-Betrieb + Autostart einrichten (empfohlen) |
| `start_windows.bat` | Clip Studio im Vordergrund starten (mit schwarzem Fenster) bzw. öffnen, falls es schon läuft |
| `stop_server.bat` | Clip Studio beenden |
| `autostart_entfernen.bat` | Autostart wieder entfernen |
| `server.log` | Protokoll des Hintergrund-Servers (auch unter Einstellungen → Server) |

Tipp: In den Windows-Energieoptionen den Ruhezustand ausschalten oder verlängern, wenn andere
Clip Studio nutzen sollen – im Ruhezustand ist der Server nicht erreichbar.

**Mac:** Rechtsklick auf `start_mac.command` → Öffnen (Homebrew wird für ffmpeg gebraucht).

## Auf dem Handy

Öffne die Adresse von Clip Studio im Handy-Browser (siehe „Mit anderen teilen“) →
Browser-Menü → **„Zum Startbildschirm hinzufügen“** bzw. im Benutzer-Menü **„Als App installieren“**.
Dann startet Clip Studio wie eine normale App.

## Mit anderen teilen

Clip Studio lauscht nur auf deinem PC (`127.0.0.1`). Damit Freunde (oder dein Handy unterwegs) es
erreichen, brauchst du einen **Tunnel** – eine sichere Verbindung von einer öffentlichen
HTTPS-Adresse zu deinem PC, ohne Portfreigabe am Router:

### Variante A: Tailscale Funnel (kostenlos, keine eigene Domain nötig)
1. [Tailscale](https://tailscale.com/download) installieren und anmelden.
2. In der Eingabeaufforderung: `tailscale funnel --bg localhost:7860`
   Beim ersten Mal zeigt Tailscale einen Link, um Funnel/HTTPS für dein Konto freizuschalten.
3. Die angezeigte Adresse (`https://<pc-name>.<tailnet>.ts.net`) unter ⚙ Einstellungen →
   **Öffentliche Adresse** eintragen.

Hinweis: Funnel hat eine (nicht veröffentlichte) Bandbreitengrenze – für ein paar Freunde reicht das,
für große Uploads lieber Links (YouTube/Twitch) statt Dateien verwenden.

### Variante B: Cloudflare Tunnel (kostenlos, braucht eine eigene Domain)
Stabiler bei viel Verkehr. Domain bei Cloudflare hinzufügen → Zero Trust → Networks → Tunnels →
Tunnel anlegen, `cloudflared` installieren, Public Hostname auf `http://localhost:7860` zeigen lassen.
Uploads werden in 32-MB-Stücken übertragen, deshalb stört das 100-MB-Limit von Cloudflare nicht.

### Leute einladen
⚙ Einstellungen → **Zugang & Benutzer** → **Einladungslink erstellen** → Link verschicken.
Der Link gilt 7 Tage und nur einmal. Die Person wählt Name und Passwort und kann dann Projekte anlegen.

Was Gäste dürfen – und was nicht:
- ✅ Links (YouTube, Twitch, Kick …) und Videodateien hochladen, eigene Projekte und Clips verwalten
- ❌ fremde Projekte sehen, Dateien auf deinem PC verwenden, Einstellungen/Updates/KI-Modelle ändern
- Limits: max. Projekte gleichzeitig pro Gast und max. Upload-Größe (Einstellungen)
- Projekte laufen nacheinander in einer gemeinsamen Warteschlange

Sicherheit: Passwörter werden nur als Hash gespeichert, Logins nach 10 Fehlversuchen für 15 Minuten
gesperrt, Sitzungen laufen nach 30 Tagen ab und enden bei Passwortänderung. Konten, Einstellungen
(inkl. API-Key) und Projekte liegen nur auf deinem PC (`users.json`, `settings.json`, `jobs/`) und
werden nie zu GitHub hochgeladen.

**Verantwortung:** Wer Clip Studio öffentlich zugänglich macht, ist für die Nutzung verantwortlich.
Lade nur Leute ein, denen du vertraust, und clippt nur Inhalte, für die ihr die Rechte habt.

## So benutzt du es

1. **＋ Neues Projekt** → Twitch-/YouTube-/Kick-Link einfügen oder Videodatei reinziehen
2. Art wählen: *Gaming-Stream*, *Just Chatting*, *Podcast* (setzt passende Einstellungen)
3. **▶ Clips erstellen** – Fortschritt wird live angezeigt, Clips erscheinen nacheinander
4. Clips ansehen, Caption-Text anpassen (speichert automatisch), **📋 Text kopieren**,
   **⬇ Download**, und abhaken wo gepostet wurde (YouTube / Instagram / TikTok)
5. Nicht zufrieden? **✂️ Neu schneiden** – Download und Transkript werden wiederverwendet.
   Fehler oder Abbruch? **🔁 Erneut versuchen** macht dort weiter, wo es aufgehört hat.

Tipps:
- Lange Streams: unter *Erweiterte Einstellungen* einen **Abschnitt** angeben (z.B. `1:00:00-2:00:00`).
- Gaming-Streams: **Facecam-Position** einmal eintragen (`x,y,breite,höhe` als Anteil des Bildes,
  z.B. `0.80,0.02,0.18,0.25` = oben rechts).
- **Lokale KI (kostenlos):** [Ollama](https://ollama.com/download) installieren → ⚙ Einstellungen →
  „Lokal (Ollama)“ → **Modell herunterladen**. `qwen3:8b` (~8 GB Grafikspeicher/RAM),
  `qwen3:4b` für schwächere PCs, `qwen3:14b` für starke Grafikkarten. Die KI bewertet ca. 30
  vorausgewählte Stellen und schreibt Titel, Caption und Hashtags.
- **Claude API** statt lokal: ⚙ Einstellungen → „Claude API“ → Key von console.anthropic.com.

---

# Kommandozeile (für Fortgeschrittene)

Die App benutzt intern `clipper.py`, das du auch direkt aufrufen kannst:

## autoclipper CLI

Turns long videos (stream VODs, podcasts, interviews) into vertical 9:16 clips with
word-by-word captions, ready for YouTube Shorts, Instagram Reels and TikTok.

```
VOD / podcast ──► yt-dlp ──► Whisper transcript ──► pick best moments ──► 9:16 reframe ──► captions ──► MP4s
                                                    (heuristic or Claude)   (face track /
                                                                             facecam split /
                                                                             blurred fit)
```

## 1. Install (once)

1. **Python 3.10+**: <https://www.python.org/downloads/> (Windows: tick "Add to PATH")
2. **ffmpeg**
   - Windows: `winget install Gyan.FFmpeg`
   - macOS: `brew install ffmpeg`
3. In this folder (or just run the start script once):
   ```
   python -m venv venv
   venv\Scripts\activate          (Windows)   |   source venv/bin/activate   (macOS/Linux)
   pip install -r requirements.txt
   ```

The first run downloads the Whisper speech model (~500 MB for `small`).
If you have an NVIDIA GPU, transcription is much faster. On CPU, expect roughly
real-time / 3 with `small`, so try `--section` on long streams first.

## 2. Use

```bash
# 5 clips from a Twitch VOD (only the 2nd hour)
python clipper.py "https://www.twitch.tv/videos/123456789" --section 1:00:00-2:00:00

# Podcast file, 8 clips, 25-55 s each, German
python clipper.py podcast.mp4 -n 8 --min 25 --max 55 --language de

# Let Claude choose the moments and write titles/captions/hashtags
set ANTHROPIC_API_KEY=sk-ant-...        (Windows; macOS: export ANTHROPIC_API_KEY=...)
python clipper.py "<url>" --ai

# Just see which moments it would pick, without rendering
python clipper.py "<url>" --dry-run
```

Output in `clips/`:

| file | what |
|---|---|
| `01_title.mp4` | 1080×1920 clip, H.264/AAC, ready to upload |
| `01_title.txt` | title + caption + hashtags to paste when posting |
| `clips.json` | timestamps, scores, layout per clip |
| `.work/` | cached download, transcript, loudness; delete to start fresh |

Re-running on the same `--out` folder reuses the download and transcript, so trying other
settings is fast.

### Layouts

| `--layout` | best for | what it does |
|---|---|---|
| `auto` (default) | anything | picks one of the three below by detecting faces |
| `track` | podcasts, IRL/just-chatting | crops 9:16 and smoothly follows the speaker's face |
| `split` | gaming streams | facecam on top, gameplay below |
| `fit` | no faces, screen content | full frame centred over a blurred background |

For gaming streams, give the facecam position once and it is always right:
`--facecam 0.80,0.02,0.18,0.25` = x, y, width, height as fractions of the frame
(this example is the top-right corner). Take a screenshot of the stream and measure it.

### Other options

`-n` clip count · `--min/--max` length in seconds · `--whisper-model tiny|base|small|medium|large-v3`
· `--font "Montserrat Black"` (any installed font) · `--words-per-line 2` · `--no-upper`
· `--no-title` · `--hashtags "#twitch #fyp"` · `--crf 18` (higher quality) ·
`--ai-provider ollama --ai-model qwen3:8b` (local model via Ollama, free) or `--ai-provider anthropic --ai-model <id>` (Claude API; check docs.claude.com for current model ids).

## 3. How moment selection works

**Heuristic mode (free, offline)** scores every 20–60 s window that starts and ends on a
sentence boundary by:
- audio loudness peaks relative to the rest of the stream (shouting, laughing, hype)
- speech density (fast talk is more engaging than dead air)
- reaction words (EN + DE: "no way", "insane", "krass", "digga", ...)
- whether it opens with a hook (question / "nobody tells you...") and ends cleanly
- penalty for long silences

Good for streams with clear reactions. For podcasts, where the best moments are *what* is
said rather than how loud, `--ai` is noticeably better.

**AI mode** sends the timestamped transcript to Claude, which returns moments plus title,
caption and hashtags. A 3-hour stream costs a few cents to a dollar depending on the model.

Always watch your clips before posting. Automation finds candidates; you are the editor.

## 4. Posting workflow

1. Run the clipper, watch the results, delete the weak ones.
2. Upload each MP4 natively to each platform, with the text from the `.txt` file.
   Native uploads perform better than cross-posted videos with other apps' watermarks.
3. Best practice: 1–3 posts per day per platform, consistent niche (one streamer or topic per account).
4. Automating uploads later: YouTube Data API works, but uploads from unverified API projects
   stay *private* until Google audits the app. TikTok/Instagram APIs also need app review.
   For a handful of clips a day, a scheduler (Buffer, Metricool, etc.) or manual upload is simpler.

## 5. Rules: stay on the right side

- **Only clip content you have permission to use**: your own videos, creators who allow
  clipping, or paid clipping campaigns (e.g. Whop Content Rewards). Clipping movies/TV or
  random creators gets Content ID claims, strikes, and banned accounts.
- **Add value**: captions, a hook title and tight editing. YouTube's reused/"inauthentic"
  content policy demonetises channels that just re-upload others' content unchanged.
- Credit the creator in the caption (campaigns usually require specific tags).
- Don't remove other creators' watermarks, and don't buy views. Campaign platforms detect
  bot traffic and won't pay.
