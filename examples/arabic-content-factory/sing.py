#!/usr/bin/env python3
"""AI cover: song URL/file -> same song, same music, sung by an AI voice.

  1. fetch the audio (yt-dlp) and optionally trim it
  2. split lead vocals from the music with Demucs
  3. convert the vocals to the target voice with RVC (melody is preserved;
     --pitch shifts the key, e.g. +12 for a male song sung by a female voice)
  4. polish the new vocals, pitch-shift the music to the same key, remix and
     loudness-normalise to streaming level (-14 LUFS)
  5. optionally render a 1080x1920 video with a waveform and an
     "AI cover" disclosure, ready for Shorts/Reels/TikTok

Demucs and rvc-python are heavy, version-pinned ML stacks (rvc-python needs
Python 3.10 + numpy<=1.23.5), so they run in their own environment passed
with --tools-python. This script itself needs only the standard library,
ffmpeg and yt-dlp.
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
from pathlib import Path

RVC_SNIPPET = r"""
import json, sys
from rvc_python.infer import RVCInference
a = json.loads(sys.argv[1])
rvc = RVCInference(device=a["device"])
rvc.load_model(a["model"], version=a["version"], index_path=a["index"])
# rvc-python's own parameter names (f0up_key / f0method), not RVC-WebUI's.
rvc.set_params(f0up_key=a["pitch"], f0method=a["f0"], index_rate=a["index_rate"],
               protect=a["protect"], rms_mix_rate=a["rms_mix"], filter_radius=3)
rvc.infer_file(a["src"], a["dst"])
"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def run(cmd: list[str], **kw) -> None:
    proc = subprocess.run(cmd, **kw)
    if proc.returncode != 0:
        sys.exit(f"command failed ({proc.returncode}): {' '.join(cmd[:4])} ...")


def ffmpeg(*args: str) -> None:
    run(["ffmpeg", "-y", "-loglevel", "error", *args])


def has_filter(name: str) -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                         capture_output=True, text=True).stdout
    return re.search(rf"\s{name}\s", out) is not None


def pitch_filter(semitones: int) -> str:
    ratio = 2 ** (semitones / 12)
    if has_filter("rubberband"):
        return f"rubberband=pitch={ratio:.6f}:formant=preserved"
    # Fallback: resample trick, then restore tempo (atempo accepts 0.5-2.0).
    return f"asetrate=44100*{ratio:.6f},aresample=44100,atempo={1 / ratio:.6f}"


# ------------------------------------------------------------------ stages
def fetch(args, work: Path) -> tuple[Path, str]:
    src = work / "source.wav"
    if args.url:
        if not shutil.which("yt-dlp"):
            sys.exit("yt-dlp not found. Run `agent-reach install --env=auto`.")
        proc = subprocess.run(
            ["yt-dlp", "-f", "bestaudio", "-x", "--audio-format", "wav",
             "--no-playlist", "--print", "after_move:title", "--print",
             "after_move:filepath", "-o", str(work / "dl.%(ext)s"), args.url],
            capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(f"yt-dlp failed:\n{proc.stderr[-800:]}")
        title, path = proc.stdout.strip().splitlines()[-2:]
        raw = Path(path)
    else:
        raw, title = Path(args.file), Path(args.file).stem
    trim = []
    if args.start:
        trim += ["-ss", str(args.start)]
    if args.duration:
        trim += ["-t", str(args.duration)]
    ffmpeg(*trim, "-i", str(raw), "-ac", "2", "-ar", "44100", str(src))
    return src, title


def separate(args, src: Path, work: Path) -> tuple[Path, Path]:
    run([args.tools_python, "-m", "demucs", "--two-stems", "vocals",
         "-n", args.demucs_model, "-d", args.device.split(":")[0],
         "-o", str(work / "sep"), str(src)])
    stem_dir = work / "sep" / args.demucs_model / src.stem
    vocals, music = stem_dir / "vocals.wav", stem_dir / "no_vocals.wav"
    if not (vocals.exists() and music.exists()):
        sys.exit(f"Demucs produced no stems in {stem_dir}")
    return vocals, music


def convert(args, vocals: Path, work: Path) -> Path:
    out = work / "vocals_ai.wav"
    if args.convert_cmd:  # e.g. Seed-VC or Applio CLI
        cmd = args.convert_cmd.format(src=vocals, dst=out, pitch=args.pitch)
        run(cmd, shell=True)
    else:
        payload = {"model": args.model, "index": args.index or "",
                   "version": args.rvc_version, "device": args.device,
                   "pitch": args.pitch, "f0": args.f0_method,
                   "index_rate": args.index_rate, "protect": args.protect,
                   "rms_mix": args.rms_mix, "src": str(vocals), "dst": str(out)}
        run([args.tools_python, "-c", RVC_SNIPPET, json.dumps(payload)])
    if not out.exists():
        sys.exit("Voice conversion produced no output.")
    return out


def mix(args, vocals_ai: Path, music: Path, out_wav: Path) -> None:
    vocal_chain = (f"aresample=44100,highpass=f=80,"
                   f"acompressor=threshold=-18dB:ratio=3:attack=10:release=120,"
                   f"aecho=0.8:0.6:60:0.12,volume={args.vocal_gain}dB")
    music_chain = "aresample=44100"
    if args.pitch and not args.keep_music_key:
        music_chain += "," + pitch_filter(args.pitch)
    ffmpeg("-i", str(vocals_ai), "-i", str(music), "-filter_complex",
           f"[0:a]{vocal_chain}[v];[1:a]{music_chain}[m];"
           "[v][m]amix=inputs=2:duration=longest:normalize=0,"
           "loudnorm=I=-14:TP=-1.5:LRA=11[out]",
           "-map", "[out]", "-ar", "44100", str(out_wav))


def render_video(audio: Path, title: str, voice: str, dest: Path, font: str) -> None:
    def esc(s: str) -> str:
        return re.sub(r"([\\:'%,\[\]])", r"\\\1", s)
    graph = (
        "[0:a]showwaves=s=1080x420:mode=cline:colors=0x2dd4bf:scale=sqrt:draw=full,"
        "format=rgba[w];color=c=0x101014:s=1080x1920[bg];"
        "[bg][w]overlay=0:750[v1];"
        f"[v1]drawtext=font='{esc(font)}':text='{esc(title[:40])}':fontcolor=white:"
        "fontsize=58:x=(w-tw)/2:y=420,"
        f"drawtext=font='{esc(font)}':text='{esc(voice)}':fontcolor=0x2dd4bf:"
        "fontsize=44:x=(w-tw)/2:y=520,"
        f"drawtext=font='{esc(font)}':text='AI cover · غناء بالذكاء الاصطناعي':"
        "fontcolor=0xa1a1aa:fontsize=38:x=(w-tw)/2:y=1500[v]")
    ffmpeg("-i", str(audio), "-filter_complex", graph, "-map", "[v]", "-map", "0:a",
           "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "22",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest",
           "-movflags", "+faststart", str(dest))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="Song URL (YouTube, SoundCloud, ...)")
    src.add_argument("--file", help="Local audio/video file")
    p.add_argument("--model", help="RVC voice model (.pth)")
    p.add_argument("--index", help="RVC feature index (.index), optional")
    p.add_argument("--voice-name", default="AI Voice", help="Shown in the video")
    p.add_argument("--pitch", type=int, default=0,
                   help="Key shift in semitones (-24..24): +12 male->female")
    p.add_argument("--keep-music-key", action="store_true",
                   help="Shift only the vocals (sounds off-key unless 0/±12)")
    p.add_argument("--f0-method", default="rmvpe",
                   choices=("rmvpe", "crepe", "harvest", "pm"))
    p.add_argument("--index-rate", type=float, default=0.6)
    p.add_argument("--protect", type=float, default=0.33)
    p.add_argument("--rms-mix", type=float, default=0.25,
                   help="0 = follow the original singer's dynamics")
    p.add_argument("--rvc-version", default="v2", choices=("v1", "v2"))
    p.add_argument("--vocal-gain", type=float, default=0.0, help="dB")
    p.add_argument("--start", type=float, help="Start at this second")
    p.add_argument("--duration", type=float, help="Only this many seconds")
    p.add_argument("--video", action="store_true", help="Also render a 9:16 MP4")
    p.add_argument("--font", default="Noto Sans Arabic")
    p.add_argument("--device", default="cuda:0", help="cuda:0 | cpu")
    p.add_argument("--demucs-model", default="htdemucs_ft")
    p.add_argument("--tools-python", default=os.environ.get("SING_TOOLS_PYTHON",
                                                            sys.executable),
                   help="Python with demucs + rvc-python installed")
    p.add_argument("--convert-cmd", help="Custom converter with {src} {dst} {pitch}")
    p.add_argument("--out", default="output")
    args = p.parse_args()
    if not args.model and not args.convert_cmd:
        p.error("--model (RVC .pth) or --convert-cmd is required")
    if args.model and not Path(args.model).is_file():
        p.error(f"model not found: {args.model}")
    if not -24 <= args.pitch <= 24:
        p.error("--pitch must be between -24 and 24")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        log("1/4 fetching audio...")
        source, title = fetch(args, work)
        slug = re.sub(r"[^\w\-]+", "_", title)[:50].strip("_") or "song"
        out = Path(args.out) / f"cover_{slug}"
        out.mkdir(parents=True, exist_ok=True)

        log("2/4 separating vocals and music (Demucs)...")
        vocals, music = separate(args, source, work)
        log("3/4 converting vocals to the AI voice...")
        vocals_ai = convert(args, vocals, work)
        log("4/4 mixing and mastering...")
        mix(args, vocals_ai, music, out / "cover.wav")

        ffmpeg("-i", str(out / "cover.wav"), "-c:a", "libmp3lame", "-q:a", "0",
               "-metadata", f"title={title} (AI cover)",
               "-metadata", f"artist={args.voice_name}",
               "-metadata", "comment=AI-generated vocals", str(out / "cover.mp3"))
        shutil.copy(vocals_ai, out / "vocals_ai.wav")
        shutil.copy(music, out / "instrumental.wav")
        if args.video:
            log("    rendering video...")
            render_video(out / "cover.wav", title, args.voice_name,
                         out / "cover.mp4", args.font)
        (out / "info.json").write_text(json.dumps({
            "title": title, "source": args.url or Path(args.file).name,
            "voice": args.voice_name, "model": Path(args.model).name if args.model
            else "custom", "pitch": args.pitch, "start": args.start,
            "duration": args.duration}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    log(f"done -> {out}")


if __name__ == "__main__":
    main()
