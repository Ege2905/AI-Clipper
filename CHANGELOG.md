# Changelog

## 2.0.0 – Web-App
- Läuft als Server auf deinem PC: unsichtbar im Hintergrund, Autostart, Neustart nach Abstürzen
- Updates kommen automatisch von GitHub (abschaltbar)
- Konten: Admin + Gäste per Einladungslink, jeder sieht nur seine eigenen Projekte
- Mit anderen teilen über Tailscale Funnel oder Cloudflare Tunnel (Anleitung im README)
- Sicherheit: Passwort-Hashes, Login-Sperre nach Fehlversuchen, CSRF-Schutz, Gäste ohne Zugriff auf PC-Dateien
- Uploads in Stücken (fortsetzbar, funktionieren durch Tunnel-Limits)
- Als App aufs Handy installierbar
- Produktions-Webserver (waitress)

## 1.3.1
- Updates kommen jetzt aus github.com/Ege2905/AI-Clipper (Einstellungen → Updates)

## 1.3.0
- Lokale KI (Ollama) bewertet jetzt vorausgewählte Kandidaten statt des ganzen Transkripts – viel schneller, kein Timeout mehr bei langen Streams
- qwen3 „denkt“ nicht mehr minutenlang nach (Thinking aus)
- KI-Fehler brechen das Projekt nicht mehr ab: es wird automatisch die normale Auswahl genutzt, „Erneut versuchen“ holt nur Fehlendes nach
- Transkription in 20-Minuten-Stücken, setzt nach Abbruch fort; liest Audio direkt mit ffmpeg (Fix für PyAV-Fehler)
- deno wird automatisch gefunden (YouTube-Downloads)
- In-App-Updates über GitHub

## 1.2.0
- Lokale KI über Ollama, Modell-Download in den Einstellungen

## 1.1.0
- Clip Studio App (Browser-Oberfläche, Warteschlange, Clip-Verwaltung)

## 1.0.0
- Kommandozeilen-Clipper
