#!/usr/bin/env python3
"""Arabic Content Factory: YouTube URL -> Arabic script -> Arabic narration.

Pipeline (each tier is a separate self-hosted service):
  1. Agent-Reach / yt-dlp  -> fetch title + transcript of the source video
  2. Open Notebook API     -> rewrite transcript as an Arabic script (any LLM)
  3. VoiceStudio API       -> synthesize the script with an Arabic voice

Standard library only; needs `yt-dlp` and `ffmpeg` on PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

TRANSFORMATION_NAME = "arabic_content_factory"

STYLES = {
    "summary": "ملخص سردي واضح ومشوّق لأهم الأفكار، مدته 3 إلى 5 دقائق عند القراءة",
    "shorts": "سكربت فيديو قصير (60 ثانية كحد أقصى) يبدأ بخطّاف قوي في أول جملة",
    "podcast": "حلقة بودكاست بأسلوب حواري ودود بين مقدّم ومستمع، مع مقدمة وخاتمة",
    "translate": "ترجمة أمينة وكاملة للنص مع الحفاظ على الترتيب والمعنى",
}

DIALECTS = {
    "msa": "العربية الفصحى المبسطة",
    "egyptian": "اللهجة المصرية",
    "gulf": "اللهجة الخليجية",
    "levantine": "اللهجة الشامية",
    "maghrebi": "اللهجة المغاربية",
}

PROMPT = """You are an expert Arabic content writer.
The input starts with a line `STYLE: ...` and a line `DIALECT: ...`, followed by a
transcript (usually English). Write a script in the requested Arabic dialect and
style, meant to be read aloud by a text-to-speech voice:
- Output only the script text, no headings, no markdown, no stage directions.
- Spell out numbers and abbreviations as they should be spoken.
- Keep technical terms understandable; add a short Arabic explanation if needed.
- End with a one-sentence call to action asking the listener to follow the channel.
"""


# ---------------------------------------------------------------- HTTP helpers
def _request(method: str, url: str, body: dict | None = None,
             token: str | None = None, timeout: int = 600) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        sys.exit(f"HTTP {e.code} from {url}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach {url}: {e.reason}. Is the service running?")


def _json(method: str, url: str, body: dict | None = None,
          token: str | None = None) -> dict | list:
    return json.loads(_request(method, url, body, token))


# ------------------------------------------------------- 1. fetch (yt-dlp)
def _vtt_to_text(vtt: str) -> str:
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if (not line or "-->" in line or line.startswith(("WEBVTT", "Kind:",
                                                           "Language:", "NOTE"))
                or line.isdigit()):
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        # Auto-captions repeat each line while it scrolls; drop the duplicates.
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return " ".join(lines)


def fetch_youtube(url: str, workdir: Path) -> tuple[str, str]:
    if not shutil.which("yt-dlp"):
        sys.exit("yt-dlp not found. Run `agent-reach install --env=auto` "
                 "or `pip install -U 'yt-dlp[default]'`.")
    out = str(workdir / "src")
    cmd = ["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs",
           "--sub-langs", "en.*,en", "--sub-format", "vtt",
           "--print", "title", "--no-simulate", "-o", out, url]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"yt-dlp failed:\n{proc.stderr[-1000:]}")
    title = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else url
    subs = sorted(workdir.glob("src*.vtt"))
    if not subs:
        sys.exit("No English subtitles found for this video. "
                 "Use --text-file with your own transcript instead.")
    return title, _vtt_to_text(subs[0].read_text(encoding="utf-8"))


# --------------------------------------------- 2. write (Open Notebook API)
def ensure_transformation(api: str, token: str | None) -> str:
    for t in _json("GET", f"{api}/api/transformations", token=token):
        if t.get("name") == TRANSFORMATION_NAME:
            return t["id"]
    created = _json("POST", f"{api}/api/transformations", {
        "name": TRANSFORMATION_NAME,
        "title": "Arabic Content Factory",
        "description": "Rewrites a transcript as a spoken Arabic script.",
        "prompt": PROMPT,
    }, token)
    return created["id"]


def write_script(api: str, token: str | None, transcript: str, style: str,
                 dialect: str, model_id: str | None) -> str:
    tid = ensure_transformation(api, token)
    body = {
        "transformation_id": tid,
        "input_text": (f"STYLE: {STYLES[style]}\nDIALECT: {DIALECTS[dialect]}\n\n"
                       f"{transcript}"),
    }
    if model_id:
        body["model_id"] = model_id
    result = _json("POST", f"{api}/api/transformations/execute", body, token)
    return result["output"].strip()


# ------------------------------------------------- 3. voice (VoiceStudio API)
def _chunks(text: str, limit: int = 3500) -> list[str]:
    parts = re.split(r"(?<=[.!?؟\n])\s+", text)
    out: list[str] = []
    cur = ""
    for p in parts:
        while len(p) > limit:  # a single overlong sentence
            out.append(p[:limit])
            p = p[limit:]
        if len(cur) + len(p) + 1 > limit and cur:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        out.append(cur)
    return out


def synthesize(api: str, text: str, voice: str, model: str, workdir: Path,
               dest: Path) -> None:
    wavs: list[Path] = []
    pieces = _chunks(text)
    for i, chunk in enumerate(pieces, 1):
        print(f"  voice chunk {i}/{len(pieces)}", flush=True)
        audio = _request("POST", f"{api}/v1/audio/speech", {
            "model": model, "input": chunk, "voice": voice,
            "language": "ar", "response_format": "wav",
        })
        wav = workdir / f"part{i:03d}.wav"
        wav.write_bytes(audio)
        wavs.append(wav)
    listing = workdir / "parts.txt"
    listing.write_text("".join(f"file '{w}'\n" for w in wavs))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat",
                    "-safe", "0", "-i", str(listing), "-c:a", "libmp3lame",
                    "-q:a", "2", str(dest)], check=True)


# ----------------------------------------------------------------------- CLI
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="YouTube video URL")
    src.add_argument("--text-file", help="Use a local transcript instead")
    p.add_argument("--style", choices=STYLES, default="summary")
    p.add_argument("--dialect", choices=DIALECTS, default="msa")
    p.add_argument("--voice", default="default",
                   help="VoiceStudio voice profile ID (e.g. a cloned voice)")
    p.add_argument("--tts-model", default="omnivoice")
    p.add_argument("--llm-model", help="Open Notebook model ID (optional)")
    p.add_argument("--out", default="output")
    p.add_argument("--no-audio", action="store_true", help="Script only")
    p.add_argument("--notebook-api",
                   default=os.environ.get("OPEN_NOTEBOOK_API",
                                          "http://localhost:5055"))
    p.add_argument("--voicestudio-api",
                   default=os.environ.get("VOICESTUDIO_API",
                                          "http://127.0.0.1:3900"))
    args = p.parse_args()
    token = os.environ.get("OPEN_NOTEBOOK_PASSWORD")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        print("1/3 fetching source...", flush=True)
        if args.url:
            title, transcript = fetch_youtube(args.url, work)
        else:
            title = Path(args.text_file).stem
            transcript = Path(args.text_file).read_text(encoding="utf-8")

        slug = re.sub(r"[^\w\-]+", "_", title)[:60].strip("_") or "item"
        out = Path(args.out) / slug
        out.mkdir(parents=True, exist_ok=True)
        (out / "transcript.txt").write_text(transcript, encoding="utf-8")

        print("2/3 writing Arabic script via Open Notebook...", flush=True)
        script = write_script(args.notebook_api.rstrip("/"), token, transcript,
                              args.style, args.dialect, args.llm_model)
        (out / "script_ar.txt").write_text(script, encoding="utf-8")

        if not args.no_audio:
            print("3/3 generating Arabic voice via VoiceStudio...", flush=True)
            synthesize(args.voicestudio_api.rstrip("/"), script, args.voice,
                       args.tts_model, work, out / "audio_ar.mp3")

    print(f"done -> {out}")


if __name__ == "__main__":
    main()
