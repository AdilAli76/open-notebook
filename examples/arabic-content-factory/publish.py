#!/usr/bin/env python3
"""Shorts + publishing: dub output -> vertical Arabic-subtitled clips -> Postiz.

  1. pick the best 20-60 s moments (Open Notebook LLM, or a local heuristic)
  2. render each as 1080x1920 with a blurred backdrop + burned Arabic subtitles
  3. upload to Postiz and create one post per channel (YouTube Shorts, TikTok,
     Instagram Reels, ...). Posts are created as *drafts* unless --schedule.

Input is a dub.py output folder (dubbed_ar[_lipsync].mp4 + segments.json).
Standard library only; needs ffmpeg built with libass.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PICKER_NAME = "arabic_shorts_picker"
PICKER_PROMPT = """You pick viral short-form clips from a dubbed Arabic video.
Input: numbered transcript lines `[i] start-end: text` and a line
`WANT: N clips, MIN-MAX seconds`. Choose self-contained moments with a strong
hook in the first line. Reply with ONLY a JSON array, no prose:
[{"start": i, "end": j, "title": "<Arabic title, max 70 chars>",
  "caption": "<Arabic caption, 1-2 sentences>", "hashtags": ["tag", ...]}]
`start`/`end` are line numbers (inclusive). Clips must not overlap.
"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def http(method: str, url: str, *, body: bytes | None = None,
         headers: dict | None = None, timeout: int = 600):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} -> HTTP {e.code}: "
                 f"{e.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach {url}: {e.reason}")


# ------------------------------------------------------------- clip picking
def pick_heuristic(segs: list[dict], n: int, lo: float, hi: float) -> list[dict]:
    """Densest non-overlapping windows: more words per second = more value."""
    windows = []
    for i in range(len(segs)):
        for j in range(i, len(segs)):
            dur = segs[j]["end"] - segs[i]["start"]
            if dur > hi:
                break
            if dur >= lo:
                chars = sum(len(s["arabic"]) for s in segs[i:j + 1])
                windows.append((chars / dur, i, j))
    windows.sort(reverse=True)
    picked: list[tuple[int, int]] = []
    for _, i, j in windows:
        if all(j < a or i > b for a, b in picked):
            picked.append((i, j))
        if len(picked) == n:
            break
    clips = []
    for i, j in sorted(picked):
        first = segs[i]["arabic"].strip()
        clips.append({"start": i, "end": j, "title": first[:70],
                      "caption": first, "hashtags": []})
    return clips


def pick_llm(segs: list[dict], n: int, lo: float, hi: float, api: str,
             token: str | None, model_id: str | None) -> list[dict] | None:
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    jh = {"Content-Type": "application/json", **auth}
    existing = http("GET", f"{api}/api/transformations", headers=auth)
    tid = next((t["id"] for t in existing if t.get("name") == PICKER_NAME), None)
    if not tid:
        tid = http("POST", f"{api}/api/transformations", headers=jh, body=json.dumps({
            "name": PICKER_NAME, "title": "Arabic Shorts Picker",
            "description": "Picks short-form clips from a dubbed transcript.",
            "prompt": PICKER_PROMPT}).encode())["id"]
    lines = "\n".join(f"[{k}] {s['start']:.1f}-{s['end']:.1f}: {s['arabic']}"
                      for k, s in enumerate(segs))
    body = {"transformation_id": tid,
            "input_text": f"WANT: {n} clips, {lo:.0f}-{hi:.0f} seconds\n{lines}"}
    if model_id:
        body["model_id"] = model_id
    out = http("POST", f"{api}/api/transformations/execute", headers=jh,
               body=json.dumps(body).encode())["output"]
    m = re.search(r"\[.*\]", out, re.S)
    try:
        raw = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return None
    clips, used = [], set()
    for c in raw:
        try:
            i, j = int(c["start"]), int(c["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= i <= j < len(segs)) or used & set(range(i, j + 1)):
            continue
        if not lo * 0.8 <= segs[j]["end"] - segs[i]["start"] <= hi * 1.1:
            continue
        used |= set(range(i, j + 1))
        clips.append({"start": i, "end": j, "title": str(c.get("title", ""))[:70],
                      "caption": str(c.get("caption", "")),
                      "hashtags": [str(h).lstrip("#") for h in c.get("hashtags", [])]})
    return clips[:n] or None


# ---------------------------------------------------------------- rendering
def _ass_time(t: float) -> str:
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def write_ass(segs: list[dict], t0: float, t1: float, path: Path, font: str) -> None:
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 0\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, "
        "BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},72,&H00FFFFFF,&H00000000,&H80000000,1,1,5,2,"
        "2,80,80,460,1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Text\n")
    events = []
    for s in segs:
        if s["end"] <= t0 or s["start"] >= t1:
            continue
        text = re.sub(r"[{}\\]", "", s["arabic"]).replace("\n", " ")
        events.append(f"Dialogue: 0,{_ass_time(s['start'] - t0)},"
                      f"{_ass_time(min(s['end'], t1) - t0)},Default,{text}")
    path.write_text(head + "\n".join(events) + "\n", encoding="utf-8")


def render(video: Path, segs: list[dict], clip: dict, dest: Path, font: str) -> None:
    t0 = segs[clip["start"]]["start"]
    t1 = segs[clip["end"]]["end"]
    ass = dest.with_suffix(".ass")
    write_ass(segs, t0, t1, ass, font)
    graph = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
             "crop=1080:1920,gblur=sigma=30[bg];"
             "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2,ass={ass.name}[v]")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t0:.2f}",
         "-t", f"{t1 - t0:.2f}", "-i", str(video.resolve()),
         "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
         dest.name], cwd=dest.parent, check=True)
    ass.unlink()


# ---------------------------------------------------------------- publishing
def settings_for(identifier: str, title: str, tags: list[str]) -> dict:
    if identifier == "youtube":
        return {"__type": "youtube", "title": f"{title} #Shorts"[:100],
                "type": "public", "selfDeclaredMadeForKids": "no",
                "tags": [{"value": t, "label": t} for t in tags[:10]]}
    if identifier in ("tiktok", "tiktok-business"):
        return {"__type": identifier, "title": title[:90],
                "privacy_level": "PUBLIC_TO_EVERYONE", "duet": False,
                "stitch": False, "comment": True, "autoAddMusic": "no",
                "brand_content_toggle": False, "brand_organic_toggle": False,
                "video_made_with_ai": True, "content_posting_method": "DIRECT_POST"}
    if identifier in ("instagram", "instagram-standalone"):
        return {"__type": identifier, "post_type": "post", "collaborators": []}
    return {"__type": identifier}


class Postiz:
    def __init__(self, base: str, key: str):
        self.base, self.key = base.rstrip("/"), key

    def integrations(self) -> list[dict]:
        return http("GET", f"{self.base}/integrations",
                    headers={"Authorization": self.key})

    def upload(self, path: Path) -> dict:
        boundary = f"----acf{int(time.time() * 1000)}"
        ctype = mimetypes.guess_type(path.name)[0] or "video/mp4"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        return http("POST", f"{self.base}/upload", body=body, headers={
            "Authorization": self.key,
            "Content-Type": f"multipart/form-data; boundary={boundary}"})

    def post(self, kind: str, when: dt.datetime, posts: list[dict]) -> dict:
        body = {"type": kind, "shortLink": False, "tags": [], "posts": posts,
                "date": when.astimezone(dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z")}
        return http("POST", f"{self.base}/posts", body=json.dumps(body).encode(),
                    headers={"Authorization": self.key,
                             "Content-Type": "application/json"})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("dub_dir", help="Folder produced by dub.py")
    p.add_argument("--clips", type=int, default=3)
    p.add_argument("--min-seconds", type=float, default=20)
    p.add_argument("--max-seconds", type=float, default=60)
    p.add_argument("--picker", choices=("llm", "auto"), default="llm",
                   help="llm = Open Notebook picks moments; auto = local heuristic")
    p.add_argument("--llm-model", help="Open Notebook model ID (optional)")
    p.add_argument("--font", default="Noto Sans Arabic")
    p.add_argument("--hashtags", default="ذكاء_اصطناعي,تقنية",
                   help="Comma-separated hashtags added to every post")
    p.add_argument("--credit", help="Source credit line (required for CC content)")
    p.add_argument("--channels", default="youtube,tiktok,instagram",
                   help="Postiz provider identifiers to post to")
    p.add_argument("--schedule", action="store_true",
                   help="Schedule posts instead of creating drafts")
    p.add_argument("--start", help="First slot, ISO time (default: in 1 hour)")
    p.add_argument("--every-hours", type=float, default=6,
                   help="Gap between scheduled clips")
    p.add_argument("--no-publish", action="store_true", help="Render clips only")
    p.add_argument("--notebook-api", default=os.environ.get(
        "OPEN_NOTEBOOK_API", "http://localhost:5055"))
    p.add_argument("--postiz-api", default=os.environ.get(
        "POSTIZ_API", "https://api.postiz.com/public/v1"))
    args = p.parse_args()

    d = Path(args.dub_dir)
    video = next((d / n for n in ("dubbed_ar_lipsync.mp4", "dubbed_ar.mp4")
                  if (d / n).exists()), None)
    if not video or not (d / "segments.json").exists():
        sys.exit(f"{d} needs dubbed_ar*.mp4 and segments.json (run dub.py first)")
    segs = json.loads((d / "segments.json").read_text(encoding="utf-8"))
    source = d / "source.json"
    credit = args.credit or (json.loads(source.read_text()).get("url")
                             if source.exists() else None)

    log(f"1/3 picking {args.clips} clips ({args.picker})...")
    clips = None
    if args.picker == "llm":
        clips = pick_llm(segs, args.clips, args.min_seconds, args.max_seconds,
                         args.notebook_api.rstrip("/"),
                         os.environ.get("OPEN_NOTEBOOK_PASSWORD"), args.llm_model)
        if not clips:
            log("    LLM gave no usable clips; falling back to heuristic")
    clips = clips or pick_heuristic(segs, args.clips, args.min_seconds,
                                    args.max_seconds)
    if not clips:
        sys.exit("Video too short for the requested clip length.")

    log("2/3 rendering vertical clips with Arabic subtitles...")
    out = d / "shorts"
    out.mkdir(exist_ok=True)
    base_tags = [t.strip().lstrip("#") for t in args.hashtags.split(",") if t.strip()]
    for k, c in enumerate(clips, 1):
        c["file"] = out / f"short_{k:02d}.mp4"
        c["hashtags"] = list(dict.fromkeys(c["hashtags"] + base_tags))
        tags = " ".join(f"#{t.replace(' ', '_')}" for t in c["hashtags"])
        c["text"] = "\n\n".join(x for x in (
            c["caption"], f"المصدر: {credit}" if credit else "", tags) if x)
        log(f"    {k}/{len(clips)} {c['title']}")
        render(video, segs, c, c["file"], args.font)
    (out / "clips.json").write_text(json.dumps(
        [{**c, "file": c["file"].name} for c in clips], ensure_ascii=False,
        indent=2), encoding="utf-8")

    if args.no_publish:
        log(f"done -> {out}")
        return
    key = os.environ.get("POSTIZ_API_KEY")
    if not key:
        sys.exit("Set POSTIZ_API_KEY (Postiz -> Settings -> Public API), "
                 "or use --no-publish.")

    log("3/3 publishing via Postiz...")
    pz = Postiz(args.postiz_api, key)
    wanted = [c.strip() for c in args.channels.split(",") if c.strip()]
    targets = [i for i in pz.integrations()
               if i.get("identifier") in wanted and not i.get("disabled")]
    if not targets:
        sys.exit(f"No connected Postiz channels match {wanted}. "
                 "Connect them in the Postiz dashboard first.")
    log("    channels: " + ", ".join(f"{t['name']} ({t['identifier']})"
                                     for t in targets))
    when = (dt.datetime.fromisoformat(args.start) if args.start
            else dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
    if when.tzinfo is None:
        when = when.astimezone()
    kind = "schedule" if args.schedule else "draft"
    for k, c in enumerate(clips):
        media = pz.upload(c["file"])
        media_ref = {"id": media["id"], "path": media["path"]}
        posts = [{"integration": {"id": t["id"]},
                  "value": [{"content": c["text"], "image": [media_ref]}],
                  "settings": settings_for(t["identifier"], c["title"],
                                           c["hashtags"])} for t in targets]
        slot = when + dt.timedelta(hours=args.every_hours * k)
        pz.post(kind, slot, posts)
        log(f"    {kind}: {c['file'].name} @ {slot.isoformat(timespec='minutes')}")
    log(f"done -> {out}" + ("" if args.schedule else
                            " (drafts: review and approve them in Postiz)"))


if __name__ == "__main__":
    main()
