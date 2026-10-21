#!/usr/bin/env python3
"""
autoclipper - turn long videos (stream VODs, podcasts) into vertical short clips.

Pipeline:
  1. fetch       download with yt-dlp (YouTube, Twitch, Kick, ...) or use a local file
  2. transcribe  faster-whisper with word-level timestamps (cached)
  3. select      find the best moments (heuristic scoring, or Claude with --ai)
  4. reframe     9:16 with face tracking, streamer split-screen, or blurred fit
  5. captions    word-by-word highlighted subtitles burned into the video
  6. export      MP4s + clips.json + a ready-to-paste caption file per clip

Usage examples:
  python clipper.py "https://www.twitch.tv/videos/123456789" --num-clips 8
  python clipper.py podcast.mp4 --layout track --min 25 --max 55
  python clipper.py "<url>" --section 1:00:00-2:30:00 --ai
  python clipper.py video.mp4 --dry-run            # only list the moments it found
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

OUT_W, OUT_H = 1080, 1920

# Words that often show up around reactions/highlights (English + German stream slang)
HYPE_WORDS = [
    "no way", "oh my god", "omg", "what the", "wtf", "holy", "insane", "crazy", "bro",
    "wait", "let's go", "lets go", "are you serious", "dude", "hell", "lol", "haha",
    "unbelievable", "never", "nobody", "secret", "truth", "actually",
    "krass", "alter", "digga", "boah", "was zur", "wie bitte", "nein", "junge", "ehrlich",
    "safe", "geil", "wallah",
]
HOOK_STARTS = ["why", "how", "what", "the reason", "nobody", "here's", "this is", "i never",
               "warum", "wie", "was", "niemand", "das ist", "ich habe nie"]


# ----------------------------------------------------------------------------- utils
def log(msg: str) -> None:
    print(f"[autoclipper] {msg}", flush=True)


def emit(stage: str, pct: float | None = None, **extra) -> None:
    """Machine-readable progress line for the Clip Studio app (ignored on the CLI)."""
    if os.environ.get("CLIPSTUDIO"):
        print("@@" + json.dumps({"stage": stage, "pct": pct, **extra}, ensure_ascii=False), flush=True)


def warn(msg: str) -> None:
    """Non-fatal problem: shown as a yellow note in the app, job keeps running."""
    emit("warn", None, msg=msg)


def run(cmd: list[str], **kw) -> None:
    subprocess.run(cmd, check=True, **kw)


def need(binary: str) -> None:
    if shutil.which(binary) is None:
        sys.exit(f"'{binary}' not found on PATH. See README for install steps.")


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,r_frame_rate:format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    info = json.loads(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    num, den = (int(x) for x in v["r_frame_rate"].split("/"))
    fps = num / den if den else 30.0
    return {"w": int(v["width"]), "h": int(v["height"]), "fps": min(fps, 60.0),
            "duration": float(info["format"]["duration"]),
            "has_audio": any(s["codec_type"] == "audio" for s in info["streams"])}


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", "_", s)[:n].strip("_") or "clip"


def ts(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ----------------------------------------------------------------------------- 1. fetch
def find_js_runtime() -> tuple[str, str] | None:
    """yt-dlp needs a JS runtime for YouTube. Find deno (or node/bun) even if not on PATH."""
    for name in ("deno", "node", "bun"):
        p = shutil.which(name)
        if p:
            return name, p
    home, la = Path.home(), Path(os.environ.get("LOCALAPPDATA", ""))
    cands = [la / "Microsoft" / "WinGet" / "Links" / "deno.exe", home / ".deno" / "bin" / "deno.exe",
             home / ".deno" / "bin" / "deno", Path("/opt/homebrew/bin/deno"), Path("/usr/local/bin/deno")]
    cands += sorted((la / "Microsoft" / "WinGet" / "Packages").glob("DenoLand.Deno*/deno.exe")) if la.parts else []
    for c in cands:
        if c.is_file():
            return "deno", str(c)
    return None



def fetch(source: str, workdir: Path, section: str | None) -> Path:
    if Path(source).exists():
        emit("meta", title=Path(source).stem)
        return Path(source).resolve()
    existing = [p for p in workdir.glob("source.*") if p.suffix not in (".json", ".part", ".ytdl")]
    if existing:
        log(f"using cached download {existing[0].name}")
    else:
        log("downloading with yt-dlp ...")
        emit("download", 0)
        cmd = [sys.executable, "-m", "yt_dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
               "--merge-output-format", "mp4", "--newline", "--write-info-json",
               "-o", str(workdir / "source.%(ext)s")]
        rt = find_js_runtime()
        if rt:
            cmd += ["--js-runtimes", f"{rt[0]}:{rt[1]}"]
        else:
            log("no JavaScript runtime (deno) found - YouTube downloads may miss formats")
        if section:
            cmd += ["--download-sections", f"*{section}", "--force-keyframes-at-cuts"]
        run(cmd + [source])
        existing = [p for p in workdir.glob("source.*") if p.suffix not in (".json", ".part", ".ytdl")]
    info_json = next(workdir.glob("source*.info.json"), None)
    if info_json:
        try:
            meta = json.loads(info_json.read_text(encoding="utf-8"))
            emit("meta", title=meta.get("title"), uploader=meta.get("uploader"))
        except (OSError, ValueError):
            pass
    return existing[0]


# ----------------------------------------------------------------------------- 2. transcribe
CHUNK_SEC = 1200  # transcribe in 20-minute pieces: low memory, resumable, live progress


def load_audio(video: Path, start: float, dur: float, sr: int = 16000) -> np.ndarray:
    """Decode a piece of audio with ffmpeg (avoids PyAV version problems in faster-whisper)."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                          "-i", str(video), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def transcribe(video: Path, workdir: Path, model_size: str, language: str | None,
               duration: float = 0.0) -> dict:
    cache = workdir / "transcript.json"
    if cache.exists():
        log("using cached transcript")
        return json.loads(cache.read_text(encoding="utf-8"))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from faster_whisper import WhisperModel

    log(f"transcribing with whisper '{model_size}' (first run downloads the model) ...")
    emit("transcribe", 0, msg="Modell wird geladen")
    model = WhisperModel(model_size, device="auto", compute_type="auto")

    parts_dir = workdir / "transcript_parts"
    parts_dir.mkdir(exist_ok=True)
    total = duration or CHUNK_SEC
    n_chunks = max(1, math.ceil(total / CHUNK_SEC))
    segs, words, lang = [], [], language
    for i in range(n_chunks):
        c0 = i * CHUNK_SEC
        part = parts_dir / f"part_{i:03d}.json"
        if part.exists():  # resume after a crash / restart
            d = json.loads(part.read_text(encoding="utf-8"))
            lang = lang or d.get("language")
        else:
            audio = load_audio(video, c0, CHUNK_SEC)
            if len(audio) < 1600:
                break
            segments, info = model.transcribe(audio, word_timestamps=True,
                                              vad_filter=True, language=lang)
            d = {"language": info.language, "segments": [], "words": []}
            for sg in segments:
                d["segments"].append({"start": round(c0 + sg.start, 2), "end": round(c0 + sg.end, 2),
                                      "text": sg.text.strip()})
                for w in sg.words or []:
                    if w.word.strip():
                        d["words"].append({"start": round(c0 + w.start, 2),
                                           "end": round(c0 + w.end, 2), "word": w.word.strip()})
                done = c0 + sg.end
                if os.environ.get("CLIPSTUDIO"):
                    emit("transcribe", min(done / total, 1.0), msg=f"{ts(done)} / {ts(total)}")
                else:
                    print(f"\r  transcribed up to {ts(done)} / {ts(total)}", end="", flush=True)
            lang = lang or info.language  # keep the language of the first piece for the rest
            part.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        segs += d["segments"]
        words += d["words"]
        emit("transcribe", min((c0 + CHUNK_SEC) / total, 1.0),
             msg=f"{ts(min(c0 + CHUNK_SEC, total))} / {ts(total)}")
    print()
    data = {"language": lang or "en", "segments": segs, "words": words}
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


# ----------------------------------------------------------------------------- 3a. audio energy
HOP = 0.25  # seconds per loudness sample


def loudness_curve(video: Path, workdir: Path) -> np.ndarray:
    cache = workdir / "loudness.npy"
    if cache.exists():
        return np.load(cache)
    log("analysing audio loudness ...")
    sr = 8000
    hop = int(sr * HOP)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1",
                             "-ar", str(sr), "-f", "s16le", "-"], stdout=subprocess.PIPE)
    out, block = [], hop * 2 * 4000  # read ~17 minutes of audio at a time
    rest = b""
    while True:
        buf = proc.stdout.read(block)
        if not buf:
            break
        buf = rest + buf
        usable = len(buf) // (hop * 2) * hop * 2
        rest = buf[usable:]
        pcm = np.frombuffer(buf[:usable], dtype=np.int16).astype(np.float32) / 32768.0
        out.append(np.sqrt(np.mean(pcm.reshape(-1, hop) ** 2, axis=1) + 1e-10))
    proc.wait()
    rms = np.concatenate(out) if out else np.zeros(1)
    db = 20 * np.log10(rms)
    np.save(cache, db)
    return db


# ----------------------------------------------------------------------------- 3b. heuristic selection
def _z(x: np.ndarray) -> np.ndarray:
    sd = x.std()
    return (x - x.mean()) / sd if sd > 1e-9 else np.zeros_like(x)


def heuristic_select(data: dict, db: np.ndarray, n: int, min_d: float, max_d: float) -> list[dict]:
    segs, words = data["segments"], data["words"]
    if not segs:
        return []
    w_start = np.array([w["start"] for w in words]) if words else np.zeros(0)
    voiced = db[db > np.percentile(db, 20)] if len(db) else db
    med = np.median(voiced) if len(voiced) else -30
    p90 = np.percentile(voiced, 90) if len(voiced) else -10

    cands = []
    for i in range(len(segs)):
        for j in range(i, len(segs)):
            a, b = segs[i]["start"], segs[j]["end"]
            dur = b - a
            if dur < min_d:
                continue
            if dur > max_d:
                break
            cands.append((i, j, a, b))
    if not cands:  # transcript too short / segments too long: fall back to fixed windows
        total = segs[-1]["end"]
        step = max_d / 2
        cands = [(0, 0, t, min(t + max_d, total)) for t in np.arange(0, max(total - min_d, 1), step)]

    feats = {k: [] for k in ("loud", "peaks", "density", "hype", "gap", "hook", "clean")}
    for i, j, a, b in cands:
        e = db[int(a / HOP): max(int(b / HOP), int(a / HOP) + 1)]
        top = np.sort(e)[-max(1, len(e) // 5):] if len(e) else np.array([med])
        feats["loud"].append(float(top.mean() - med))
        feats["peaks"].append(float((e > p90).mean()) if len(e) else 0.0)
        lo, hi = np.searchsorted(w_start, a), np.searchsorted(w_start, b)
        feats["density"].append((hi - lo) / (b - a))
        ws = w_start[lo:hi]
        gaps = np.diff(np.concatenate([[a], ws, [b]])) if len(ws) else np.array([b - a])
        feats["gap"].append(float(gaps.max()))
        text = " ".join(s["text"] for s in segs[i:j + 1]).lower()
        feats["hype"].append(sum(text.count(k) for k in HYPE_WORDS) / (b - a) * 60)
        first = segs[i]["text"].lower().strip()
        feats["hook"].append(1.0 if first.endswith("?") or any(first.startswith(h) for h in HOOK_STARTS) else 0.0)
        feats["clean"].append(1.0 if segs[j]["text"].rstrip().endswith((".", "!", "?")) else 0.0)

    F = {k: np.array(v, dtype=float) for k, v in feats.items()}
    score = (1.0 * _z(F["loud"]) + 0.8 * _z(F["peaks"]) + 0.5 * _z(F["density"])
             + 0.6 * _z(F["hype"]) - 0.7 * _z(F["gap"]) + 0.4 * F["hook"] + 0.3 * F["clean"])

    picked: list[dict] = []
    for idx in np.argsort(-score):
        i, j, a, b = cands[idx]
        if any(a < p["end"] + 5 and b > p["start"] - 5 for p in picked):
            continue
        text = " ".join(s["text"] for s in segs[i:j + 1])
        picked.append({"start": float(a), "end": float(b), "score": round(float(score[idx]), 2),
                       "title": make_title(text), "caption": "", "hashtags": [],
                       "reason": "heuristic: loudness/reactions/speech density"})
        if len(picked) >= n:
            break
    return picked


def make_title(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    best = max(sentences[:4], key=lambda s: ("?" in s or "!" in s, -abs(len(s) - 50)), default=text)
    best = best.strip()
    return (best[:67] + "...") if len(best) > 70 else best


# ----------------------------------------------------------------------------- 3c. AI selection (Claude)
AI_PROMPT = """You are an expert short-form video editor who finds viral moments in long videos
(Twitch streams, podcasts, interviews) for TikTok, YouTube Shorts and Instagram Reels.

Below is a timestamped transcript ([start-end] text, seconds). Pick the {n} best self-contained
moments. Each clip must:
- be between {min_d:.0f} and {max_d:.0f} seconds long
- start with a strong hook in the first 2 seconds (a surprising line, a question, a reaction)
- make sense without extra context and end on a payoff / punchline / complete thought
- not overlap with other clips

Return ONLY JSON, no prose:
{{"clips": [{{"start": 12.3, "end": 51.0, "score": 9, "title": "short punchy on-screen title",
  "caption": "post caption, 1-2 sentences", "hashtags": ["#tag1", "#tag2"],
  "reason": "why this works"}}]}}
Write title/caption in the transcript's language ({lang}).

TRANSCRIPT:
{transcript}"""


class LLM:
    """Small wrapper so moment selection works with Claude (API) or a local model (Ollama)."""

    def __init__(self, provider: str, model: str, ollama_url: str):
        self.provider, self.model, self.url = provider, model, ollama_url.rstrip("/")
        if provider == "anthropic":
            try:
                import anthropic
            except ImportError:
                sys.exit("--ai needs the anthropic package: pip install anthropic")
            if not os.environ.get("ANTHROPIC_API_KEY"):
                sys.exit("--ai needs ANTHROPIC_API_KEY set (get one at console.anthropic.com)")
            self.client = anthropic.Anthropic()
            self.chunk_words = 40000
        else:
            self._check_ollama()
            self.chunk_words = 5000  # local models have small context windows

    def _check_ollama(self) -> None:
        import urllib.request
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=5) as r:
                names = [m["name"] for m in json.load(r).get("models", [])]
        except OSError:
            sys.exit(f"Ollama not reachable at {self.url} - is the Ollama app running? (ollama.com)")
        if not any(n == self.model or n.split(":")[0] == self.model or n == self.model + ":latest"
                   for n in names):
            sys.exit(f"Ollama model '{self.model}' not installed. Run: ollama pull {self.model}"
                     + (f"  (installed: {', '.join(names)})" if names else ""))

    def complete(self, prompt: str) -> str:
        if self.provider == "anthropic":
            msg = self.client.messages.create(model=self.model, max_tokens=4000,
                                              messages=[{"role": "user", "content": prompt}])
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        import socket
        import urllib.error
        import urllib.request
        if self.model.lower().startswith("qwen3"):
            prompt += "\n/no_think"  # qwen3: skip the long hidden reasoning phase
        body = json.dumps({"model": self.model, "stream": True, "format": "json", "think": False,
                           "messages": [{"role": "user", "content": prompt}],
                           "options": {"num_ctx": 8192, "num_predict": 2048,
                                       "temperature": 0.3}}).encode()
        req = urllib.request.Request(self.url + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        parts, t0, n_tok = [], time.time(), 0
        try:
            # timeout applies per read: fails only if the model is silent for 10 minutes
            with urllib.request.urlopen(req, timeout=600) as r:
                for raw in r:
                    if not raw.strip():
                        continue
                    ev = json.loads(raw)
                    if ev.get("error"):
                        raise RuntimeError(ev["error"])
                    parts.append(ev.get("message", {}).get("content", ""))
                    n_tok += 1
                    if ev.get("done"):
                        secs = ev.get("eval_duration", 0) / 1e9
                        if secs:
                            log(f"  {ev.get('eval_count', n_tok)} tokens, "
                                f"{ev.get('eval_count', n_tok) / secs:.1f} tokens/s, "
                                f"total {time.time() - t0:.0f}s")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Ollama error {e.code}: {e.read().decode(errors='replace')[:300]}")
        except (socket.timeout, TimeoutError):
            raise RuntimeError("local model did not respond within 10 minutes")
        return re.sub(r"<think>.*?</think>", "", "".join(parts), flags=re.S)


def parse_clips_json(text: str) -> list[dict]:
    for candidate in (text, (re.search(r"\{.*\}", text, re.S) or [None])[0]):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict)]
        if isinstance(data, dict):
            clips = data.get("clips", [])
            return [c for c in clips if isinstance(c, dict)]
    return []


def ai_select(data: dict, n: int, min_d: float, max_d: float, llm: LLM) -> list[dict]:
    segs = data["segments"]
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segs]

    chunks, cur, count = [], [], 0
    for ln in lines:
        cur.append(ln)
        count += len(ln.split())
        if count > llm.chunk_words:
            chunks.append(cur); cur, count = [], 0
    if cur:
        chunks.append(cur)

    who = "Claude" if llm.provider == "anthropic" else "local model"
    clips: list[dict] = []
    per_chunk = max(2, math.ceil(n * 1.5 / len(chunks)))
    for k, chunk in enumerate(chunks, 1):
        log(f"asking {who} ({llm.model}) for highlights, chunk {k}/{len(chunks)} ...")
        emit("select", (k - 1) / len(chunks), msg=f"KI {k}/{len(chunks)}")
        try:
            text = llm.complete(AI_PROMPT.format(
                n=per_chunk, min_d=min_d, max_d=max_d, lang=data.get("language", "en"),
                transcript="\n".join(chunk)))
        except Exception as e:
            log(f"  chunk {k} failed: {e}")
            warn(f"KI-Abschnitt {k} fehlgeschlagen – dort wird die normale Auswahl genutzt")
            continue
        found = parse_clips_json(text)
        if not found:
            log("  could not parse response, skipping chunk")
        for c in found:
            try:
                clips.append(snap_clip(c, segs, min_d, max_d))
            except (KeyError, TypeError, ValueError):
                continue

    clips.sort(key=lambda c: -float(c.get("score", 0)))
    picked = []
    for c in clips:
        if all(not (c["start"] < p["end"] and c["end"] > p["start"]) for p in picked):
            picked.append(c)
        if len(picked) >= n:
            break
    return picked


RATE_PROMPT = """You are an expert short-form video editor (TikTok, YouTube Shorts, Reels).
Below are candidate clips cut from a long video (stream or podcast), each with its transcript.
Rate how well each would perform as a standalone short: strong hook at the start, funny/surprising/
emotional moment, understandable without context, satisfying ending. 10 = viral, 1 = boring.

Return ONLY JSON:
{{"clips": [{{"id": 1, "score": 8, "title": "short punchy on-screen title (max 8 words)",
  "caption": "post caption, 1 sentence", "hashtags": ["#tag1", "#tag2", "#tag3"]}}]}}
Include every id. Write title and caption in {lang_name}.

CANDIDATES:
{cands}"""

LANG_NAMES = {"de": "German", "en": "English", "tr": "Turkish", "es": "Spanish", "fr": "French"}


def ai_rate_candidates(data: dict, db: np.ndarray, n: int, min_d: float, max_d: float,
                       llm: "LLM", workdir: Path) -> list[dict]:
    """Local-model mode: heuristic finds candidates, the model rates them and writes titles."""
    segs = data["segments"]
    cands = heuristic_select(data, db, min(36, max(12, n * 5)), min_d, max_d)
    cands.sort(key=lambda c: c["start"])
    log(f"rating {len(cands)} candidate moments with local model ({llm.model}) ...")
    cache_f = workdir / f"ai_rate_{slug(llm.model)}_{n}_{int(min_d)}_{int(max_d)}.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
    lang = data.get("language", "en")
    batch = 6
    groups = [list(range(i, min(i + batch, len(cands)))) for i in range(0, len(cands), batch)]
    for g, idxs in enumerate(groups, 1):
        emit("select", (g - 1) / len(groups), msg=f"KI bewertet {g}/{len(groups)}")
        if all(str(i) in cache for i in idxs):
            continue
        lines = []
        for i in idxs:
            c = cands[i]
            text = " ".join(x["text"] for x in segs if x["end"] > c["start"] and x["start"] < c["end"])
            words = text.split()
            if len(words) > 220:
                text = " ".join(words[:220]) + " ..."
            lines.append(f"[id {i + 1}] ({ts(c['start'])}, {c['end'] - c['start']:.0f}s): {text}")
        log(f"  batch {g}/{len(groups)}")
        try:
            reply = llm.complete(RATE_PROMPT.format(cands="\n\n".join(lines),
                                                    lang_name=LANG_NAMES.get(lang, lang)))
        except Exception as e:  # keep going - unrated candidates fall back to heuristic score
            log(f"  batch {g} failed: {e}")
            warn(f"KI-Block {g}/{len(groups)} fehlgeschlagen ({e}) – „Erneut versuchen“ holt nur diesen nach")
            continue
        for r in parse_clips_json(reply):
            try:
                i = int(r.get("id")) - 1
            except (TypeError, ValueError):
                continue
            if i in idxs:
                cache[str(i)] = r
        cache_f.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    out = []
    for i, c in enumerate(cands):
        r = cache.get(str(i))
        if r:
            try:
                ai_score = float(r.get("score", 5))
            except (TypeError, ValueError):
                ai_score = 5.0
            tags = r.get("hashtags") or []
            if isinstance(tags, str):
                tags = tags.split()
            c = {**c, "score": round(ai_score + 0.1 * c["score"], 2),
                 "title": str(r.get("title") or c["title"]), "caption": str(r.get("caption") or ""),
                 "hashtags": [str(t) for t in tags], "reason": "local AI rating"}
        else:
            # unrated (batch failed): neutral middle score so rated highlights win
            c = {**c, "score": round(min(6.0, 5.0 + 0.1 * c["score"]), 2)}
        out.append(c)
    out.sort(key=lambda c: -c["score"])
    return out[:n]


def snap_clip(c: dict, segs: list[dict], min_d: float, max_d: float) -> dict:
    """Snap AI timestamps to real segment boundaries and enforce length limits."""
    starts = np.array([s["start"] for s in segs])
    ends = np.array([s["end"] for s in segs])
    a = float(starts[np.argmin(np.abs(starts - float(c["start"])))])
    b = float(ends[np.argmin(np.abs(ends - float(c["end"])))])
    if b - a > max_d:
        b = a + max_d
    if b - a < min_d:
        b = a + min_d
    tags = c.get("hashtags") or []
    if isinstance(tags, str):
        tags = tags.split()
    try:
        score = float(c.get("score", 5))
    except (TypeError, ValueError):
        score = 5.0
    return {**c, "start": a, "end": b, "score": score, "title": str(c.get("title") or ""),
            "caption": str(c.get("caption") or ""), "hashtags": [str(t) for t in tags]}


# ----------------------------------------------------------------------------- 4. reframing
def _cascade():
    import cv2
    if not hasattr(cv2, "CascadeClassifier"):
        sys.exit("Your OpenCV has no face detector (OpenCV 5 removed it). "
                 "Run: pip install \"opencv-python-headless<5\"")
    return cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")


def read_frames(video: Path, start: float, dur: float, w: int, h: int, vf: str | None = None):
    """Yield BGR frames decoded by ffmpeg (frame-accurate, matches the audio)."""
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(video)]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = w * h * 3
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        proc.stdout.close()
        proc.wait()


def analyse_faces(video: Path, info: dict, start: float, dur: float, sample_fps: float = 4.0):
    """Return list of (t, face) where face = (cx, cy, fw, fh) normalised 0..1, or None."""
    import cv2
    cascade = _cascade()
    aw = min(info["w"], 1280)
    ah = int(round(info["h"] * aw / info["w"] / 2) * 2)
    out = []
    for k, fr in enumerate(read_frames(video, start, dur, aw, ah, f"fps={sample_fps},scale={aw}:{ah}")):
        gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.1, 4, minSize=(int(aw * 0.02),) * 2)
        face = None
        if len(faces):
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            face = ((x + fw / 2) / aw, (y + fh / 2) / ah, fw / aw, fh / ah)
        out.append((k / sample_fps, face))
    return out


def choose_layout(faces) -> str:
    found = [f for _, f in faces if f]
    if len(found) < max(3, 0.3 * len(faces)):
        return "fit"
    fw = np.median([f[2] for f in found])
    cx = np.median([f[0] for f in found])
    cy = np.median([f[1] for f in found])
    # small face in a corner -> streamer facecam over gameplay
    if fw < 0.12 and (cx < 0.33 or cx > 0.67) and (cy < 0.4 or cy > 0.6):
        return "split"
    return "track"


def smooth_track(faces, dur: float, fps: float, deadzone: float = 0.06, alpha: float = 0.15):
    """Per-frame horizontal crop centre (0..1) that follows the face calmly."""
    ts_ = np.array([t for t, _ in faces]) if faces else np.array([0.0])
    raw = [f[0] if f else None for _, f in faces] or [None]
    # fill gaps with nearest known value
    known = [(i, v) for i, v in enumerate(raw) if v is not None]
    if not known:
        return np.full(int(math.ceil(dur * fps)) + 1, 0.5)
    filled = []
    for i, v in enumerate(raw):
        filled.append(v if v is not None else min(known, key=lambda kv: abs(kv[0] - i))[1])
    # deadzone + easing: only move when the face leaves the comfort zone
    cam, path = filled[0], []
    for v in filled:
        if abs(v - cam) > deadzone:
            cam += alpha * (v - cam) * 3
        path.append(cam)
    frame_t = np.arange(int(math.ceil(dur * fps)) + 1) / fps
    return np.interp(frame_t, ts_[: len(path)], path)


def render(video: Path, info: dict, clip: dict, layout: str, faces, ass_file: Path,
           out: Path, workdir: Path, crf: int, facecam: list[float] | None = None) -> None:
    import cv2
    a, b = clip["start"], clip["end"]
    dur = b - a
    W, H, fps = info["w"], info["h"], info["fps"]
    enc = ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest"]
    sub = f"ass={ass_file.name}"

    if layout == "track" and W / H > 9 / 16:
        cw = int(round(H * 9 / 16 / 2) * 2)
        xs = smooth_track(faces, dur, fps)
        writer = subprocess.Popen(
            ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
             "-s", f"{OUT_W}x{OUT_H}", "-r", f"{fps:.3f}", "-i", "-",
             "-ss", f"{a:.3f}", "-t", f"{dur:.3f}", "-i", str(video),
             "-map", "0:v", "-map", "1:a?", "-vf", sub, *enc, str(out.resolve())],
            stdin=subprocess.PIPE, cwd=workdir)
        for k, fr in enumerate(read_frames(video, a, dur, W, H)):
            cx = xs[min(k, len(xs) - 1)] * W
            x0 = int(np.clip(cx - cw / 2, 0, W - cw))
            writer.stdin.write(cv2.resize(fr[:, x0:x0 + cw], (OUT_W, OUT_H),
                                          interpolation=cv2.INTER_LINEAR).tobytes())
        writer.stdin.close()
        if writer.wait() != 0:
            raise RuntimeError("ffmpeg failed while rendering")
        return

    if layout == "split":
        top_h = 700  # facecam panel height in the 1080x1920 output
        if facecam:  # manual facecam box, fractions of the frame: x,y,w,h
            fx, fy, fw_, fh_ = facecam
            cam_x, cam_y, cam_w, cam_h = int(fx * W), int(fy * H), int(fw_ * W), int(fh_ * H)
        else:
            found = [f for _, f in faces if f]
            cx, cy, fw, fh = (float(np.median([f[i] for f in found])) for i in range(4))
            cam_w = min(W, int(fw * W * 3.2))           # area around the face
            cam_h = min(H, int(cam_w * top_h / OUT_W))
            cam_x = int(np.clip(cx * W - cam_w / 2, 0, W - cam_w))
            cam_y = int(np.clip(cy * H - cam_h / 2, 0, H - cam_h))
        bot_h = OUT_H - top_h
        g_w = min(W, int(H * OUT_W / bot_h))         # centre of the gameplay
        g_x = (W - g_w) // 2
        vf = (f"[0:v]split[c][g];"
              f"[c]crop={cam_w}:{cam_h}:{cam_x}:{cam_y},scale={OUT_W}:{top_h}:force_original_aspect_ratio=increase,crop={OUT_W}:{top_h}[top];"
              f"[g]crop={g_w}:{H}:{g_x}:0,scale={OUT_W}:{bot_h}:force_original_aspect_ratio=increase,crop={OUT_W}:{bot_h}[bot];"
              f"[top][bot]vstack,{sub}[v]")
    else:  # fit: full frame in the middle, blurred zoomed copy behind it
        vf = (f"[0:v]split[bg][fg];"
              f"[bg]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,eq=brightness=-0.15,scale={OUT_W}:{OUT_H}[b];"
              f"[fg]scale={OUT_W}:-2[f];[b][f]overlay=(W-w)/2:(H-h)/2,{sub}[v]")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}", "-t", f"{dur:.3f}", "-i", str(video),
         "-filter_complex", vf, "-map", "[v]", "-map", "0:a?", "-r", f"{fps:.3f}", *enc,
         str(out.resolve())], cwd=workdir)


# ----------------------------------------------------------------------------- 5. captions
def ass_time(t: float) -> str:
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def build_ass(words: list[dict], clip: dict, layout: str, path: Path, font: str,
              upper: bool, max_words: int, title_overlay: bool) -> None:
    a, b = clip["start"], clip["end"]
    ws = [w for w in words if w["end"] > a and w["start"] < b]
    margin_v = 1120 if layout == "split" else 520
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,{margin_v},1
Style: Title,{font},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&HC0000000,-1,0,0,0,100,100,0,0,3,18,0,8,80,80,170,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    if title_overlay and clip.get("title"):
        events.append(f"Dialogue: 1,{ass_time(0)},{ass_time(3.0)},Title,,0,0,0,,{{\\fad(150,250)}}{esc(clip['title'])}")

    # group words into short chunks (2-3 words), break on pauses and punctuation
    chunks, cur = [], []
    for w in ws:
        if cur and (len(cur) >= max_words or w["start"] - cur[-1]["end"] > 0.6
                    or cur[-1]["word"].endswith((".", "!", "?", ","))):
            chunks.append(cur); cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)

    for ch in chunks:
        tokens = [esc(w["word"].upper() if upper else w["word"]) for w in ch]
        for k, w in enumerate(ch):
            s = w["start"] - a
            e = (ch[k + 1]["start"] if k + 1 < len(ch) else ch[-1]["end"] + 0.15) - a
            parts = [(r"{\c&H00E6FF&\fscx108\fscy108}" + t + r"{\r}") if i == k else t
                     for i, t in enumerate(tokens)]
            events.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{' '.join(parts)}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


# ----------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="Turn long videos into vertical short clips.")
    ap.add_argument("source", help="URL (YouTube/Twitch/Kick/...) or local video file")
    ap.add_argument("-n", "--num-clips", type=int, default=5)
    ap.add_argument("--min", type=float, default=20, help="min clip length (s)")
    ap.add_argument("--max", type=float, default=60, help="max clip length (s)")
    ap.add_argument("--section", help="only download part of a long VOD, e.g. 1:00:00-2:00:00")
    ap.add_argument("--layout", choices=["auto", "track", "split", "fit"], default="auto",
                    help="track=follow face, split=facecam top/gameplay bottom, fit=blurred bg")
    ap.add_argument("--facecam", help="facecam box for split layout as fractions x,y,w,h "
                    "(e.g. 0.82,0.02,0.17,0.24 = top-right corner). Implies --layout split")
    ap.add_argument("--ai", action="store_true", help="let Claude pick moments + write titles")
    ap.add_argument("--ai-provider", choices=["anthropic", "ollama"], default="anthropic",
                    help="anthropic = Claude API, ollama = local model via Ollama (free, offline)")
    ap.add_argument("--ai-model", help="model id (default: claude-sonnet-4-5 / qwen3:8b)")
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--whisper-model", default="small",
                    help="tiny/base/small/medium/large-v3 (bigger = better, slower)")
    ap.add_argument("--language", help="force transcript language, e.g. de or en")
    ap.add_argument("--transcript", help="use an existing transcript.json instead of whisper")
    ap.add_argument("--font", default="Arial")
    ap.add_argument("--no-upper", action="store_true", help="don't uppercase captions")
    ap.add_argument("--words-per-line", type=int, default=3)
    ap.add_argument("--no-title", action="store_true", help="no title overlay at the start")
    ap.add_argument("--hashtags", default="", help='extra hashtags, e.g. "#twitch #fyp"')
    ap.add_argument("--crf", type=int, default=20, help="quality (lower = better/bigger)")
    ap.add_argument("-o", "--out", default="clips")
    ap.add_argument("--dry-run", action="store_true", help="only print the moments found")
    args = ap.parse_args()

    need("ffmpeg"); need("ffprobe")
    out_dir = Path(args.out).resolve()
    workdir = out_dir / ".work"
    workdir.mkdir(parents=True, exist_ok=True)

    video = fetch(args.source, workdir, args.section)
    info = probe(video)
    emit("info", duration=info["duration"], width=info["w"], height=info["h"])
    log(f"source: {video.name}  {info['w']}x{info['h']} @ {info['fps']:.0f}fps, {ts(info['duration'])}")

    if args.transcript:
        data = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
    else:
        data = transcribe(video, workdir, args.whisper_model, args.language, info["duration"])
    log(f"transcript: {len(data['segments'])} segments, {len(data['words'])} words")

    emit("select", 0)
    if args.ai:
        model = args.ai_model or os.environ.get("CLIPPER_MODEL") or (
            "claude-sonnet-4-5" if args.ai_provider == "anthropic" else "qwen3:8b")
        try:
            llm = LLM(args.ai_provider, model, args.ollama_url)
        except SystemExit as e:
            llm = None
            log(f"AI not available: {e}")
            warn(f"KI nicht verfügbar ({e}) – normale Auswahl verwendet")
        if llm is None:
            db = loudness_curve(video, workdir) if info["has_audio"] else np.zeros(int(info["duration"] / HOP))
            clips = heuristic_select(data, db, args.num_clips, args.min, args.max)
        elif args.ai_provider == "ollama":
            db = loudness_curve(video, workdir) if info["has_audio"] else np.zeros(int(info["duration"] / HOP))
            clips = ai_rate_candidates(data, db, args.num_clips, args.min, args.max, llm, workdir)
        else:
            clips = ai_select(data, args.num_clips, args.min, args.max, llm)
        if len(clips) < args.num_clips:  # small local models sometimes return too few
            db = loudness_curve(video, workdir) if info["has_audio"] else np.zeros(int(info["duration"] / HOP))
            for c in heuristic_select(data, db, args.num_clips * 3, args.min, args.max):
                if len(clips) >= args.num_clips:
                    break
                if all(not (c["start"] < p["end"] and c["end"] > p["start"]) for p in clips):
                    clips.append(c)
            log(f"AI returned too few moments, filled up to {len(clips)} with heuristic picks")
        for c in clips:
            if not c["title"]:
                c["title"] = make_title(" ".join(s["text"] for s in data["segments"]
                                                 if s["start"] >= c["start"] and s["end"] <= c["end"]))
    else:
        db = loudness_curve(video, workdir) if info["has_audio"] else np.zeros(int(info["duration"] / HOP))
        clips = heuristic_select(data, db, args.num_clips, args.min, args.max)
    if not clips:
        sys.exit("no clips found - is there speech in the video?")

    for c in clips:  # tiny padding so words aren't cut off
        c["start"] = max(0.0, c["start"] - 0.15)
        c["end"] = min(info["duration"], c["end"] + 0.35)

    log(f"found {len(clips)} moments:")
    for k, c in enumerate(clips, 1):
        print(f"  {k:2d}. {ts(c['start'])}-{ts(c['end'])} ({c['end'] - c['start']:.0f}s) "
              f"score {c['score']}  {c['title']}")
    if args.dry_run:
        return

    extra_tags = args.hashtags.split()
    results = []
    for k, c in enumerate(clips, 1):
        name = f"{k:02d}_{slug(c['title'])}"
        log(f"[{k}/{len(clips)}] rendering {name}.mp4")
        emit("render", (k - 1) / len(clips), msg=f"{k}/{len(clips)}")
        faces = analyse_faces(video, info, c["start"], c["end"] - c["start"])
        layout = choose_layout(faces) if args.layout == "auto" else args.layout
        facecam = [float(v) for v in args.facecam.split(",")] if args.facecam else None
        if facecam:
            layout = "split"
        if layout == "split" and not facecam and not any(f for _, f in faces):
            layout = "fit"
        ass = workdir / f"{name}.ass"
        build_ass(data["words"], c, layout, ass, args.font, not args.no_upper,
                  args.words_per_line, not args.no_title)
        out = out_dir / f"{name}.mp4"
        render(video, info, c, layout, faces, ass, out, workdir, args.crf, facecam)

        tags = list(dict.fromkeys((c.get("hashtags") or []) + extra_tags))
        post = "\n\n".join(x for x in (c["title"], c.get("caption", ""), " ".join(tags)) if x) + "\n"
        (out_dir / f"{name}.txt").write_text(post, encoding="utf-8")
        results.append({**c, "file": out.name, "layout": layout})
        log(f"      layout={layout} -> {out}")
        (out_dir / "clips.json").write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        emit("clip", k / len(clips), file=out.name)

    emit("done", 1)
    log(f"done. {len(results)} clips in {out_dir}")


if __name__ == "__main__":
    main()
