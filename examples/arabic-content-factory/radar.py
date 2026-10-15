#!/usr/bin/env python3
"""Trend radar: find fast-rising videos that have no Arabic version yet.

For each topic it:
  1. searches recent YouTube uploads with yt-dlp (the Agent-Reach YouTube tool)
  2. adds YouTube links that are hot on chosen subreddits (optional)
  3. scores each video by views/hour and engagement
  4. checks whether an Arabic audio track or Arabic re-upload already exists
  5. writes a ranked report and can hand the top picks to dub.py

Videos under a Creative Commons licence are marked as safe to republish;
everything else is an outreach lead: dub it *for* the channel owner.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ARABIC = re.compile(r"[؀-ۿ]")
YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([\w-]{11})")
HERE = Path(__file__).resolve().parent


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def ytdlp_json(target: str, flat: bool = False, timeout: int = 300,
               limit: int | None = None) -> list[dict]:
    cmd = ["yt-dlp", "--dump-json", "--skip-download", "--no-warnings",
           "--ignore-errors"]
    if flat:
        cmd.append("--flat-playlist")
    if limit:
        cmd += ["--playlist-end", str(limit)]
    try:
        proc = subprocess.run(cmd + [target], capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        sys.exit("yt-dlp not found. Run `agent-reach install --env=auto`.")
    except subprocess.TimeoutExpired:
        log(f"  ! yt-dlp timed out on {target}")
        return []
    items = []
    for line in proc.stdout.splitlines():
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not items and proc.stderr.strip():
        log(f"  ! yt-dlp: {proc.stderr.strip().splitlines()[-1][:200]}")
    return items


# ------------------------------------------------------------------ sources
def youtube_candidates(topic: str, per_topic: int) -> list[dict]:
    # sp=CAI= sorts by upload date, so the radar sees what is rising *now*.
    query = urllib.parse.quote_plus(topic)
    return ytdlp_json(
        f"https://www.youtube.com/results?search_query={query}&sp=CAI%3D",
        limit=per_topic)


def reddit_youtube_ids(subreddits: list[str]) -> dict[str, int]:
    """Return {video_id: reddit_score} for YouTube links hot on the subs."""
    found: dict[str, int] = {}
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=50"
        req = urllib.request.Request(url, headers={"User-Agent": "trend-radar/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                posts = json.loads(r.read())["data"]["children"]
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
            log(f"  ! reddit r/{sub} unavailable ({e}); skipping")
            continue
        for p in posts:
            d = p.get("data", {})
            m = YT_ID.search(d.get("url", "") or "")
            if m:
                found[m.group(1)] = max(found.get(m.group(1), 0), d.get("score", 0))
    return found


# ------------------------------------------------------------------ scoring
def has_arabic_audio(video: dict) -> bool:
    return any((f.get("language") or "").lower().startswith("ar")
               for f in video.get("formats") or [] if f.get("acodec") != "none")


def arabic_copies(video: dict) -> int:
    """Count Arabic-titled results for the same video (re-uploads/dubs)."""
    words = " ".join(re.findall(r"\w+", video.get("title", ""))[:6])
    hits = ytdlp_json(f"ytsearch8:{words} مترجم عربي", flat=True, timeout=120)
    return sum(1 for h in hits if ARABIC.search(h.get("title") or ""))


def score(video: dict, reddit_score: int = 0) -> dict:
    now = time.time()
    ts = video.get("timestamp") or now - 86400
    hours = max((now - ts) / 3600, 1.0)
    views = video.get("view_count") or 0
    likes = video.get("like_count") or 0
    comments = video.get("comment_count") or 0
    vph = views / hours
    engagement = (likes + 3 * comments) / max(views, 1)
    value = (math.log10(vph + 1) * (1 + 10 * min(engagement, 0.2))
             + math.log10(reddit_score + 1))
    return {
        "id": video["id"],
        "url": video.get("webpage_url") or f"https://youtu.be/{video['id']}",
        "title": video.get("title"),
        "channel": video.get("channel"),
        "channel_url": video.get("channel_url"),
        "subscribers": video.get("channel_follower_count"),
        "views": views,
        "views_per_hour": round(vph),
        "engagement": round(engagement, 4),
        "reddit_score": reddit_score,
        "duration_min": round((video.get("duration") or 0) / 60, 1),
        "hours_old": round(hours),
        "creative_commons": "creative commons" in (video.get("license") or "").lower(),
        "score": round(value, 3),
    }


# ------------------------------------------------------------------- report
def write_report(picks: list[dict], path: Path) -> None:
    rows = ["# رادار الترند — " + time.strftime("%Y-%m-%d %H:%M"), "",
            "| # | الفيديو | القناة | مشاهدة/ساعة | التفاعل | المدة | الترخيص | النتيجة |",
            "|---|---|---|---|---|---|---|---|"]
    for i, p in enumerate(picks, 1):
        lic = "✅ CC (مسموح)" if p["creative_commons"] else "📩 تواصل مع القناة"
        title = (p["title"] or "").replace("|", "/")
        rows.append(
            f"| {i} | [{title}]({p['url']}) | [{p['channel']}]({p['channel_url']}) "
            f"| {p['views_per_hour']:,} | {p['engagement']:.1%} "
            f"| {p['duration_min']} د | {lic} | {p['score']} |")
    rows += ["", "**CC** = يمكن دبلجته وإعادة نشره مع ذكر المصدر. "
             "**تواصل** = اعرض على صاحب القناة دبلجة قناته للعربية (عميل محتمل)."]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--topic", action="append", required=True,
                   help="Search topic (repeatable), e.g. --topic 'AI agents'")
    p.add_argument("--subreddit", action="append", default=[],
                   help="Subreddit to watch for YouTube links (repeatable)")
    p.add_argument("--per-topic", type=int, default=10)
    p.add_argument("--min-minutes", type=float, default=2)
    p.add_argument("--max-minutes", type=float, default=30)
    p.add_argument("--max-hours", type=float, default=72,
                   help="Ignore uploads older than this")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--cc-only", action="store_true",
                   help="Only Creative Commons videos (safe to republish)")
    p.add_argument("--auto-dub", type=int, default=0, metavar="N",
                   help="Run dub.py on the top N CC picks")
    p.add_argument("--dub-args", default="", help="Extra args passed to dub.py")
    p.add_argument("--state", default="output/radar_seen.json")
    p.add_argument("--out", default="output")
    args = p.parse_args()

    state_path = Path(args.state)
    seen: set[str] = set(json.loads(state_path.read_text())) if state_path.exists() else set()

    videos: dict[str, dict] = {}
    for topic in args.topic:
        log(f"youtube: {topic}")
        for v in youtube_candidates(topic, args.per_topic):
            videos.setdefault(v["id"], v)

    reddit = reddit_youtube_ids(args.subreddit) if args.subreddit else {}
    for vid in reddit.keys() - videos.keys():
        log(f"reddit link: {vid}")
        for v in ytdlp_json(f"https://youtu.be/{vid}"):
            videos.setdefault(v["id"], v)

    ranked = []
    for v in videos.values():
        dur = (v.get("duration") or 0) / 60
        age = (time.time() - (v.get("timestamp") or time.time())) / 3600
        if (v["id"] in seen or v.get("is_live") or ARABIC.search(v.get("title") or "")
                or not args.min_minutes <= dur <= args.max_minutes
                or age > args.max_hours or has_arabic_audio(v)):
            continue
        s = score(v, reddit.get(v["id"], 0))
        if args.cc_only and not s["creative_commons"]:
            continue
        ranked.append(s)
    ranked.sort(key=lambda s: s["score"], reverse=True)

    # The Arabic-copy search costs a request per video: only check finalists.
    picks = []
    for s in ranked:
        if len(picks) >= args.top:
            break
        copies = arabic_copies(videos[s["id"]])
        if copies:
            log(f"  skip (already {copies} Arabic copies): {s['title']}")
            continue
        picks.append(s)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    (out / f"radar_{stamp}.json").write_text(
        json.dumps(picks, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(picks, out / f"radar_{stamp}.md")
    log(f"{len(picks)} picks -> {out / f'radar_{stamp}.md'}")

    dub_queue = [s for s in picks if s["creative_commons"]][: args.auto_dub]
    if args.auto_dub and len(dub_queue) < args.auto_dub:
        log("note: only Creative Commons videos are auto-dubbed; contact the "
            "other channels before republishing their content.")
    for s in dub_queue:
        log(f"dubbing: {s['title']}")
        cmd = [sys.executable, str(HERE / "dub.py"), "--url", s["url"],
               "--out", str(out), *args.dub_args.split()]
        if subprocess.run(cmd).returncode != 0:
            log(f"  ! dub failed for {s['url']}")

    seen.update(s["id"] for s in picks)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted(seen)))


if __name__ == "__main__":
    main()
