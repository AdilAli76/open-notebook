#!/usr/bin/env python3
"""Train an RVC voice model from your own recordings (for sing.py).

  1. collect recordings (files, folders or URLs of *your own* uploads)
  2. optionally strip background music with Demucs
  3. clean: high-pass, light denoise, cut silences, normalise loudness
  4. check the dataset: total clean minutes and clipping
  5. train with Applio (preprocess -> extract -> train -> index)
  6. publish voices/<name>/<name>.pth + .index + voice.json

Recording tips: 10-30 minutes, one speaker/singer, no music, no reverb, a
quiet room; singing data gives the best singing voice.

The voice owner's consent is required (--consent) and stored in voice.json.
Applio (https://github.com/IAHispano/Applio) must be installed separately;
pass its folder with --applio-dir and its Python with --applio-python.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".mp4",
             ".mkv", ".webm", ".mov"}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,40}$")
# Applio's click commands exit 0 even when a stage fails; they report it here.
APPLIO_FAIL = re.compile(r"\b(failed|error|traceback)\b", re.I)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def ffmpeg(*args: str) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"ffmpeg failed: {proc.stderr[-400:]}")


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def peak_db(path: Path) -> float:
    err = subprocess.run(["ffmpeg", "-i", str(path), "-af", "astats=metadata=0",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    peaks = [float(m) for m in re.findall(r"Peak level dB:\s*(-?[\d.]+)", err)]
    return max(peaks) if peaks else -99.0


# ------------------------------------------------------------- dataset prep
def collect(inputs: list[str], raw: Path) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        if item.startswith(("http://", "https://")):
            if not shutil.which("yt-dlp"):
                sys.exit("yt-dlp is needed for URLs (agent-reach install --env=auto)")
            proc = subprocess.run(
                ["yt-dlp", "-f", "bestaudio", "-x", "--audio-format", "wav",
                 "--print", "after_move:filepath",
                 "-o", str(raw / "url_%(id)s.%(ext)s"), item],
                capture_output=True, text=True)
            if proc.returncode != 0:
                sys.exit(f"yt-dlp failed for {item}:\n{proc.stderr[-400:]}")
            files += [Path(p) for p in proc.stdout.split("\n") if p.strip()]
            continue
        path = Path(item)
        if path.is_dir():
            files += sorted(p for p in path.rglob("*") if p.suffix.lower() in AUDIO_EXT)
        elif path.is_file():
            files.append(path)
        else:
            sys.exit(f"not found: {item}")
    if not files:
        sys.exit("No audio files found.")
    return files


def strip_music(tools_python: str, src: Path, work: Path, device: str) -> Path:
    proc = subprocess.run([tools_python, "-m", "demucs", "--two-stems", "vocals",
                           "-n", "htdemucs_ft", "-d", device, "-o", str(work), str(src)],
                          capture_output=True, text=True)
    vocals = work / "htdemucs_ft" / src.stem / "vocals.wav"
    if proc.returncode != 0 or not vocals.exists():
        sys.exit(f"Demucs failed on {src.name}:\n{proc.stderr[-400:]}")
    return vocals


def clean(src: Path, dst: Path, denoise: bool) -> None:
    chain = ["highpass=f=70", "lowpass=f=16000"]
    if denoise:
        chain.append("afftdn=nf=-25")
    # Drop pauses longer than 0.6 s (keeps phrasing, removes dead air).
    chain.append("silenceremove=stop_periods=-1:stop_duration=0.6:"
                 "stop_threshold=-45dB")
    chain.append("loudnorm=I=-20:TP=-3:LRA=11")
    ffmpeg("-i", str(src), "-af", ",".join(chain), "-ac", "1", "-ar", "44100",
           "-c:a", "pcm_s16le", str(dst))


def prepare(args, dataset: Path, work: Path) -> float:
    raw = work / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    files = collect(args.inputs, raw)
    log(f"    {len(files)} file(s)")
    total = 0.0
    for k, f in enumerate(files, 1):
        src = f
        if args.strip_music:
            src = strip_music(args.tools_python, f, work / "sep", args.device)
        dst = dataset / f"clip_{k:03d}.wav"
        clean(src, dst, not args.no_denoise)
        secs = duration(dst)
        if secs < 2:
            dst.unlink()
            log(f"    skip {f.name}: almost no audio after cleaning")
            continue
        if peak_db(f) > -0.1:
            log(f"    ! {f.name} is clipped (distorted); re-record quieter if possible")
        total += secs
    return total / 60


# ---------------------------------------------------------------- training
def applio(args, *cmd: str) -> None:
    full = [args.applio_python, "core.py", *cmd]
    log("    $ " + " ".join(cmd[:3]) + " ...")
    proc = subprocess.run(full, cwd=args.applio_dir, capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    tail = "\n".join(output.splitlines()[-15:])
    if proc.returncode != 0 or APPLIO_FAIL.search(proc.stdout.splitlines()[-1]
                                                  if proc.stdout.strip() else ""):
        sys.exit(f"Applio '{cmd[0]}' failed:\n{tail}")


def train(args, dataset: Path) -> tuple[Path, Path | None]:
    name, sr = args.name, str(args.sample_rate)
    cores = str(max(1, (os.cpu_count() or 2) - 1))
    gpu = "-" if args.device == "cpu" else args.device.split(":")[-1]
    if not args.skip_prerequisites:
        applio(args, "prerequisites")
    applio(args, "preprocess", "--model-name", name, "--dataset-path",
           str(dataset.resolve()), "--sample-rate", sr, "--cpu-cores", cores)
    applio(args, "extract", "--model-name", name, "--f0-method", "rmvpe",
           "--sample-rate", sr, "--cpu-cores", cores, "--gpu", gpu)
    applio(args, "train", "--model-name", name, "--sample-rate", sr,
           "--total-epoch", str(args.epochs), "--save-every-epoch",
           str(min(100, max(1, args.epochs // 10))), "--save-only-latest",
           "--batch-size", str(args.batch_size), "--gpu", gpu if gpu != "-" else "0")
    applio(args, "index", "--model-name", name)

    logs = Path(args.applio_logs or Path(args.applio_dir) / "logs") / name
    weights = [p for p in logs.glob("*.pth")
               if not p.name.startswith(("G_", "D_"))]
    if not weights:
        sys.exit(f"Training finished but no model weights found in {logs}")
    pth = max(weights, key=lambda p: p.stat().st_mtime)
    index = logs / f"{name}.index"
    return pth, index if index.exists() else None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("inputs", nargs="+", help="Audio/video files, folders or URLs")
    p.add_argument("--name", required=True, help="Voice id (letters, digits, _ -)")
    p.add_argument("--display-name", help="Shown to customers, e.g. 'صوت أحمد'")
    p.add_argument("--consent", required=True, metavar="OWNER",
                   help="Full name of the voice owner who consented to cloning")
    p.add_argument("--public", action="store_true",
                   help="Offer this voice to portal customers")
    p.add_argument("--strip-music", action="store_true",
                   help="Remove background music with Demucs first")
    p.add_argument("--no-denoise", action="store_true")
    p.add_argument("--min-minutes", type=float, default=5)
    p.add_argument("--epochs", type=int, default=300)
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--sample-rate", type=int, default=40000,
                   choices=(32000, 40000, 48000))
    p.add_argument("--device", default="cuda:0", help="cuda:N | cpu")
    p.add_argument("--prepare-only", action="store_true",
                   help="Build the cleaned dataset and stop")
    p.add_argument("--applio-dir", default=os.environ.get("APPLIO_DIR", "Applio"))
    p.add_argument("--applio-python", default=os.environ.get("APPLIO_PYTHON",
                                                             sys.executable))
    p.add_argument("--applio-logs", help="Applio logs dir (default <applio>/logs)")
    p.add_argument("--skip-prerequisites", action="store_true",
                   help="Pretrained models are already downloaded")
    p.add_argument("--tools-python", default=os.environ.get("SING_TOOLS_PYTHON",
                                                            sys.executable),
                   help="Python with demucs (for --strip-music)")
    p.add_argument("--voices-dir", default="voices")
    args = p.parse_args()

    if not NAME_RE.match(args.name):
        p.error("--name: 2-40 chars of letters, digits, _ or -")
    if len(args.consent.strip()) < 3:
        p.error("--consent: the voice owner's full name")
    if not args.prepare_only and not (Path(args.applio_dir) / "core.py").is_file():
        p.error(f"Applio not found at {args.applio_dir} (set --applio-dir)")

    out = Path(args.voices_dir) / args.name
    work = out / "work"
    dataset = out / "dataset"
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(dataset, ignore_errors=True)
    dataset.mkdir(parents=True)

    log("1/3 preparing dataset...")
    minutes = prepare(args, dataset, work)
    log(f"    {minutes:.1f} clean minutes")
    if minutes < args.min_minutes:
        sys.exit(f"Only {minutes:.1f} clean minutes; record at least "
                 f"{args.min_minutes:g} (10-30 is best).")
    if minutes < 10:
        log("    ! under 10 minutes: the voice will work but sound less natural")
    shutil.rmtree(work, ignore_errors=True)

    meta = {
        "id": args.name, "display_name": args.display_name or args.name,
        "consent": {"owner": args.consent.strip(),
                    "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat()},
        "public": args.public, "dataset_minutes": round(minutes, 1),
        "sample_rate": args.sample_rate, "epochs": args.epochs,
    }
    if args.prepare_only:
        (out / "voice.json").write_text(json.dumps(meta, ensure_ascii=False,
                                                   indent=2), encoding="utf-8")
        log(f"dataset ready -> {dataset}")
        return

    log(f"2/3 training with Applio ({args.epochs} epochs)...")
    pth, index = train(args, dataset)

    log("3/3 publishing voice...")
    shutil.copy(pth, out / f"{args.name}.pth")
    if index:
        shutil.copy(index, out / f"{args.name}.index")
    meta.update(model=f"{args.name}.pth",
                index=f"{args.name}.index" if index else None,
                trained_from=pth.name)
    (out / "voice.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    log(f"done -> {out}\n  try it: python sing.py --file song.mp3 "
        f"--model {out / (args.name + '.pth')}"
        + (f" --index {out / (args.name + '.index')}" if index else ""))


if __name__ == "__main__":
    main()
