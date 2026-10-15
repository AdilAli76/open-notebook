#!/usr/bin/env python3
"""Arabic lip-synced dubbing: video URL/file -> Arabic dub -> lip-synced MP4.

Stages:
  1. VoiceStudio  ingest     -> download, Demucs vocal/music split, scenes
  2. VoiceStudio  transcribe -> WhisperX segments + pyannote speakers
  3. VoiceStudio  translate  -> Arabic in the chosen dialect (LLM path)
  4. VoiceStudio  generate   -> each speaker re-voiced with their own clone
  5. VoiceStudio  download   -> dubbed MP4 (+ voice-only track for lip-sync)
  6. LatentSync   (optional) -> mouths re-animated to match the Arabic audio

Standard library only; needs `ffmpeg` on PATH. Lip-sync needs a LatentSync
checkout (https://github.com/bytedance/LatentSync) with its checkpoints.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# BCP-47 tags VoiceStudio's LLM translator understands as regional dialects.
DIALECTS = {
    "msa": None,
    "egyptian": "ar-EG",
    "gulf": "ar-SA",
    "levantine": "ar-LB",
    "maghrebi": "ar-MA",
}

DIALECT_NOTES = {
    "msa": "Use clear, modern standard Arabic suitable for narration.",
    "egyptian": "Use natural spoken Egyptian Arabic (عامية مصرية).",
    "gulf": "Use natural spoken Gulf Arabic (لهجة خليجية).",
    "levantine": "Use natural spoken Levantine Arabic (لهجة شامية).",
    "maghrebi": "Use natural spoken Moroccan/Maghrebi Arabic (دارجة).",
}

LATENTSYNC_CMD = (
    "python -m scripts.inference "
    "--unet_config_path configs/unet/stage2_512.yaml "
    "--inference_ckpt_path checkpoints/latentsync_unet.pt "
    "--inference_steps 20 --guidance_scale 1.5 "
    "--video_path {video} --audio_path {audio} --video_out_path {out}"
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- HTTP helpers
class VoiceStudio:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _open(self, req: urllib.request.Request, timeout: int = 3600):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            sys.exit(f"VoiceStudio {req.full_url} -> HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            sys.exit(f"Cannot reach VoiceStudio at {self.base}: {e.reason}")

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with self._open(req) as r:
            return json.loads(r.read())

    def upload(self, path: Path) -> dict:
        boundary = f"----acf{int(time.time() * 1000)}"
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            self.base + "/dub/upload", data=payload, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with self._open(req) as r:
            return json.loads(r.read())

    def download(self, path: str, params: dict, dest: Path) -> None:
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        with self._open(urllib.request.Request(url)) as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)

    def stream(self, task_id: str):
        """Yield SSE events from /tasks/stream/{task_id}."""
        req = urllib.request.Request(f"{self.base}/tasks/stream/{task_id}")
        with self._open(req) as r:
            for raw in r:
                line = raw.decode(errors="replace").strip()
                if line.startswith("data: "):
                    try:
                        yield json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue


def wait_task(vs: VoiceStudio, task_id: str, finish: str) -> dict:
    last = -1
    for evt in vs.stream(task_id):
        etype = evt.get("type", "")
        if etype == "progress" and evt.get("current", 0) > last:
            last = evt.get("current", 0)
            log(f"    {last}/{evt.get('total')}")
        elif etype.endswith("_start"):
            log(f"    {etype.removesuffix('_start')}...")
        elif etype == "error" and evt.get("stage"):
            sys.exit(f"task {task_id} failed at {evt['stage']}: {evt.get('error')}")
        elif etype == "error":
            log(f"    ! segment {evt.get('segment')}: {evt.get('error')}")
        elif etype == "cancelled":
            sys.exit(f"task {task_id} was cancelled")
        elif etype == finish:
            return evt
    sys.exit(f"task {task_id} stream ended before '{finish}'")


# ------------------------------------------------------------------ pipeline
def dub(vs: VoiceStudio, args: argparse.Namespace, out: Path) -> str:
    log("1/6 ingest (download + vocal/music separation)...")
    if args.url:
        res = vs.post("/dub/ingest-url", {"url": args.url})
    else:
        res = vs.upload(Path(args.file))
    job_id = res["job_id"]
    wait_task(vs, res["task_id"], "ready")

    log("2/6 transcribe + speaker diarization...")
    query = f"?num_speakers={args.speakers}" if args.speakers else ""
    tx = vs.post(f"/dub/transcribe/{job_id}{query}", {})
    segs = tx.get("segments", [])
    if not segs:
        sys.exit("No speech found in the video.")
    speakers = {s.get("speaker_id") for s in segs if s.get("speaker_id")}
    log(f"    {len(segs)} segments, {len(speakers) or 1} speaker(s), "
        f"source={tx.get('source_lang')}")

    log(f"3/6 translate -> Arabic ({args.dialect})...")
    tr = vs.post("/dub/translate", {
        "job_id": job_id,
        "segments": [{"id": s["id"], "text": s.get("text_original") or s["text"],
                      "target_lang": "ar"} for s in segs],
        "target_lang": "ar",
        "quality": args.quality,
        "dialect": DIALECTS[args.dialect],
        "translation_instructions": DIALECT_NOTES[args.dialect],
    })
    by_id = {t["id"]: t.get("text") for t in tr.get("translated", [])}
    (out / "segments.json").write_text(json.dumps(
        [{"start": s["start"], "end": s["end"], "speaker": s.get("speaker_id"),
          "source": s.get("text_original") or s["text"],
          "arabic": by_id.get(s["id"]) or s["text"]} for s in segs],
        ensure_ascii=False, indent=2), encoding="utf-8")

    log("4/6 generate Arabic voices (one clone per speaker)...")
    gen = vs.post(f"/dub/generate/{job_id}", {
        "segments": [{
            "start": s["start"], "end": s["end"],
            "text": by_id.get(s["id"]) or s["text"], "target_lang": "ar",
            # Keep the auto-clone binding from transcription unless a fixed
            # voice was requested, so every speaker keeps their own timbre.
            "profile_id": args.voice or s.get("profile_id", ""),
        } for s in segs],
        "language": "Arabic",
        "language_code": "ar",
        "timing_strategy": args.timing,
        "voice_match": "consistent",
    })
    wait_task(vs, gen["task_id"], "done")
    return job_id


def lipsync(dubbed: Path, voice_only: Path, out: Path, args, work: Path) -> None:
    log("6/6 lip-sync with LatentSync...")
    vocals = work / "vocals.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(voice_only),
                    "-vn", "-ac", "1", "-ar", "16000", str(vocals)], check=True)
    synced = work / "synced.mp4"
    cmd = (args.lipsync_cmd or LATENTSYNC_CMD).format(
        video=shlex.quote(str(dubbed.resolve())),
        audio=shlex.quote(str(vocals.resolve())),
        out=shlex.quote(str(synced.resolve())))
    proc = subprocess.run(cmd, shell=True, cwd=args.latentsync_dir)
    if proc.returncode != 0 or not synced.exists():
        sys.exit("Lip-sync failed; the non-lip-synced dub is still in "
                 f"{dubbed}. Check the LatentSync setup.")
    # LatentSync trained on lips only: restore the full mix (voices + music).
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(synced),
                    "-i", str(dubbed), "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-shortest",
                    "-metadata:s:a:0", "language=ara", str(out)], check=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="YouTube or direct video URL")
    src.add_argument("--file", help="Local video file")
    p.add_argument("--dialect", choices=DIALECTS, default="msa")
    p.add_argument("--quality", choices=("fast", "cinematic"), default="cinematic",
                   help="cinematic uses the LLM translator (needed for dialects)")
    p.add_argument("--voice", help="Force one voice profile ID for all speakers")
    p.add_argument("--speakers", type=int, help="Exact speaker count (optional)")
    p.add_argument("--timing", default="smart_fit",
                   choices=("concise", "smart_fit", "strict_slot", "stretch_video"))
    p.add_argument("--lipsync", action="store_true",
                   help="Re-animate mouths with LatentSync (needs a GPU)")
    p.add_argument("--latentsync-dir",
                   default=os.environ.get("LATENTSYNC_DIR", "LatentSync"))
    p.add_argument("--lipsync-cmd", default=os.environ.get("LIPSYNC_CMD"),
                   help="Custom lip-sync command with {video} {audio} {out}")
    p.add_argument("--out", default="output")
    p.add_argument("--voicestudio-api",
                   default=os.environ.get("VOICESTUDIO_API", "http://127.0.0.1:3900"))
    args = p.parse_args()

    if args.dialect != "msa" and args.quality == "fast":
        log("note: dialects need --quality cinematic; machine translation "
            "falls back to standard Arabic.")
    if args.lipsync and not Path(args.latentsync_dir).is_dir():
        sys.exit(f"LatentSync not found at {args.latentsync_dir}. "
                 "Set --latentsync-dir or LATENTSYNC_DIR.")

    vs = VoiceStudio(args.voicestudio_api)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        out = Path(args.out) / f"dub_{time.strftime('%Y%m%d_%H%M%S')}"
        out.mkdir(parents=True, exist_ok=True)

        job_id = dub(vs, args, out)

        log("5/6 export...")
        dubbed = out / "dubbed_ar.mp4"
        vs.download(f"/dub/download/{job_id}/dubbed_ar.mp4",
                    {"include_tracks": "ar", "preserve_bg": "true"}, dubbed)
        if args.lipsync:
            voice_only = work / "voice_only.mp4"
            vs.download(f"/dub/download/{job_id}/voice_only.mp4",
                        {"include_tracks": "ar", "preserve_bg": "false"},
                        voice_only)
            lipsync(dubbed, voice_only, out / "dubbed_ar_lipsync.mp4", args, work)

    log(f"done -> {out}")


if __name__ == "__main__":
    main()
