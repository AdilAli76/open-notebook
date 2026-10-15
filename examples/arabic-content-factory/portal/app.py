"""Customer portal: order -> quote -> pay -> automatic dub or AI cover -> download.

Run:  uvicorn app:app --host 0.0.0.0 --port 8080   (from this folder)
Config is read from environment variables, see README.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
FACTORY = HERE.parent
DATA = Path(os.environ.get("PORTAL_DATA", HERE / "data"))
DB_PATH = DATA / "portal.db"

BRAND = os.environ.get("PORTAL_BRAND", "دبلجة عربية بالذكاء الاصطناعي")
PUBLIC_URL = os.environ.get("PORTAL_PUBLIC_URL", "http://localhost:8080").rstrip("/")
ADMIN_PASSWORD = os.environ.get("PORTAL_ADMIN_PASSWORD", "")
CURRENCY = os.environ.get("PRICE_CURRENCY", "usd").lower()
PRICE_BASE = float(os.environ.get("PRICE_BASE", "3"))
PRICE_PER_MIN = float(os.environ.get("PRICE_PER_MIN", "1.5"))
PRICE_LIPSYNC_PER_MIN = float(os.environ.get("PRICE_LIPSYNC_PER_MIN", "2"))
PRICE_SHORTS = float(os.environ.get("PRICE_SHORTS", "5"))
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "30"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
PAYMENT = os.environ.get("PAYMENT_PROVIDER", "manual")  # manual | stripe
MANUAL_INSTRUCTIONS = os.environ.get(
    "PAYMENT_INSTRUCTIONS",
    "حوّل المبلغ إلى الحساب الذي نرسله لك، واكتب رقم الطلب في ملاحظة التحويل.")
STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DUB_EXTRA_ARGS = shlex.split(os.environ.get("DUB_EXTRA_ARGS", ""))
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "30"))
VOICES_DIR = Path(os.environ.get("VOICES_DIR", FACTORY / "voices"))
PRICE_COVER = float(os.environ.get("PRICE_COVER", "8"))
MAX_SONG_MINUTES = float(os.environ.get("MAX_SONG_MINUTES", "8"))
SING_EXTRA_ARGS = shlex.split(os.environ.get("SING_EXTRA_ARGS", ""))

DIALECTS = {
    "msa": "الفصحى",
    "egyptian": "المصرية",
    "gulf": "الخليجية",
    "levantine": "الشامية",
    "maghrebi": "المغاربية",
}
STATUS = {
    "awaiting_payment": "بانتظار الدفع",
    "paid": "تم الدفع، في قائمة الانتظار",
    "processing": "جارٍ التنفيذ",
    "done": "جاهز للتحميل",
    "failed": "تعذّر التنفيذ، سنتواصل معك",
    "cancelled": "ملغى",
    "expired": "انتهت صلاحية الملفات",
}
DOWNLOADABLE = re.compile(r"^(dubbed_ar(_lipsync)?\.mp4|segments\.json|short_\d{2}\.mp4"
                          r"|cover\.(mp3|mp4|wav))$")

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(brand=BRAND, dialects=DIALECTS, status_names=STATUS,
                             currency=CURRENCY.upper())


# ------------------------------------------------------------------ storage
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, created REAL NOT NULL,
            name TEXT, email TEXT NOT NULL, source_url TEXT, source_file TEXT,
            title TEXT, minutes REAL NOT NULL, dialect TEXT NOT NULL,
            lipsync INTEGER NOT NULL, shorts INTEGER NOT NULL, price REAL NOT NULL,
            status TEXT NOT NULL, payment_ref TEXT, output_dir TEXT, error TEXT)""")
        have = {r["name"] for r in c.execute("PRAGMA table_info(orders)")}
        for col, ddl in (("service", "TEXT NOT NULL DEFAULT 'dub'"),
                         ("voice", "TEXT"), ("pitch", "INTEGER NOT NULL DEFAULT 0")):
            if col not in have:
                c.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")


def public_voices() -> dict[str, dict]:
    """Voices trained by train_voice.py that are marked public."""
    voices = {}
    for meta_path in sorted(VOICES_DIR.glob("*/voice.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        model = meta_path.parent / str(meta.get("model") or "")
        if meta.get("public") and meta.get("consent") and model.is_file():
            index = meta_path.parent / str(meta.get("index") or "")
            voices[meta["id"]] = {"name": meta.get("display_name", meta["id"]),
                                  "model": model,
                                  "index": index if index.is_file() else None}
    return voices


def get_order(token: str) -> sqlite3.Row:
    with db() as c:
        row = c.execute("SELECT * FROM orders WHERE token=?", (token,)).fetchone()
    if not row:
        raise HTTPException(404, "الطلب غير موجود")
    return row


def set_status(order_id: str, status: str, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in ("status", *fields))
    with db() as c:
        c.execute(f"UPDATE orders SET {cols} WHERE id=?",
                  (status, *fields.values(), order_id))


# ------------------------------------------------------------------ pricing
def quote(minutes: float, lipsync: bool, shorts: bool, service: str = "dub") -> float:
    if service == "cover":
        return round(PRICE_COVER, 2)
    billable = max(1, math.ceil(minutes))
    price = PRICE_BASE + PRICE_PER_MIN * billable
    if lipsync:
        price += PRICE_LIPSYNC_PER_MIN * billable
    if shorts:
        price += PRICE_SHORTS
    return round(price, 2)


def probe_url(url: str) -> tuple[str, float]:
    try:
        proc = subprocess.run(
            ["yt-dlp", "--dump-json", "--skip-download", "--no-playlist",
             "--no-warnings", url], capture_output=True, text=True, timeout=90)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise HTTPException(400, "تعذّر قراءة الرابط حاليًا، جرّب رفع الملف مباشرة.")
    if proc.returncode != 0:
        raise HTTPException(400, "الرابط غير صالح أو الفيديو غير متاح للعامة.")
    info = json.loads(proc.stdout.splitlines()[0])
    if info.get("is_live"):
        raise HTTPException(400, "لا يمكن دبلجة بث مباشر.")
    return info.get("title") or url, (info.get("duration") or 0) / 60


def probe_file(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(proc.stdout.strip()) / 60
    except ValueError:
        raise HTTPException(400, "الملف ليس فيديو صالحًا.")


# ------------------------------------------------------------------ payment
def stripe_checkout(order: sqlite3.Row) -> str:
    form = {
        "mode": "payment",
        "customer_email": order["email"],
        "client_reference_id": order["id"],
        "metadata[order_id]": order["id"],
        "success_url": f"{PUBLIC_URL}/order/{order['token']}",
        "cancel_url": f"{PUBLIC_URL}/order/{order['token']}",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": CURRENCY,
        "line_items[0][price_data][unit_amount]": str(round(order["price"] * 100)),
        "line_items[0][price_data][product_data][name]":
            (f"AI cover #{order['id']}" if order["service"] == "cover" else
             f"Arabic dubbing ({DIALECTS[order['dialect']]}) #{order['id']}"),
    }
    req = urllib.request.Request(
        "https://api.stripe.com/v1/checkout/sessions",
        data=urllib.parse.urlencode(form).encode(), method="POST",
        headers={"Authorization": f"Bearer {STRIPE_SECRET}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            session = json.loads(r.read())
    except urllib.error.URLError:
        raise HTTPException(502, "تعذّر الاتصال ببوابة الدفع، حاول بعد قليل.")
    return session["url"]


def verify_stripe(payload: bytes, header: str, secret: str,
                  tolerance: int = 300) -> dict:
    pairs = [p.split("=", 1) for p in header.split(",") if "=" in p]
    sigs = [v for k, v in pairs if k == "v1"]
    try:
        ts = int(dict(pairs).get("t", ""))
    except ValueError:
        raise HTTPException(400, "bad signature header")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload,
                        hashlib.sha256).hexdigest()
    if abs(time.time() - ts) > tolerance or not any(
            hmac.compare_digest(expected, s) for s in sigs):
        raise HTTPException(400, "invalid signature")
    return json.loads(payload)


# ------------------------------------------------------------------- worker
def run_cover(order: sqlite3.Row, out: Path, src: list[str], log) -> None:
    voice = public_voices().get(order["voice"])
    if not voice:
        raise RuntimeError(f"voice {order['voice']} is no longer available")
    cmd = [sys.executable, str(FACTORY / "sing.py"), *src, "--model",
           str(voice["model"]), "--voice-name", voice["name"], "--pitch",
           str(order["pitch"]), "--video", "--out", str(out), *SING_EXTRA_ARGS]
    if voice["index"]:
        cmd += ["--index", str(voice["index"])]
    if subprocess.run(cmd, stdout=log, stderr=log).returncode != 0:
        raise RuntimeError("sing.py failed")
    cover_dir = max(out.glob("cover_*"), key=lambda p: p.stat().st_mtime)
    set_status(order["id"], "done", output_dir=str(cover_dir))


def run_order(order: sqlite3.Row) -> None:
    out = DATA / "orders" / order["id"]
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "worker.log", "a", encoding="utf-8")
    src = (["--url", order["source_url"]] if order["source_url"]
           else ["--file", order["source_file"]])
    if order["service"] == "cover":
        return run_cover(order, out, src, log)
    cmd = [sys.executable, str(FACTORY / "dub.py"), *src, "--dialect",
           order["dialect"], "--out", str(out), *DUB_EXTRA_ARGS]
    if order["lipsync"]:
        cmd.append("--lipsync")
    if subprocess.run(cmd, stdout=log, stderr=log).returncode != 0:
        raise RuntimeError("dub.py failed")
    dub_dir = max(out.glob("dub_*"), key=lambda p: p.stat().st_mtime)
    if order["shorts"]:
        cmd = [sys.executable, str(FACTORY / "publish.py"), str(dub_dir),
               "--no-publish"]
        if subprocess.run(cmd, stdout=log, stderr=log).returncode != 0:
            raise RuntimeError("publish.py failed")
    set_status(order["id"], "done", output_dir=str(dub_dir))


def purge_expired() -> None:
    """Delete uploads and outputs older than RETENTION_DAYS (as promised)."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    with db() as c:
        old = c.execute("SELECT id, source_file FROM orders WHERE created < ? "
                        "AND status NOT IN ('paid', 'processing') "
                        "AND (output_dir IS NOT NULL OR source_file IS NOT NULL)",
                        (cutoff,)).fetchall()
        for o in old:
            if o["source_file"]:
                Path(o["source_file"]).unlink(missing_ok=True)
            shutil.rmtree(DATA / "orders" / o["id"], ignore_errors=True)
            c.execute("UPDATE orders SET source_file=NULL, output_dir=NULL, "
                      "status=CASE status WHEN 'done' THEN 'expired' ELSE status END "
                      "WHERE id=?", (o["id"],))


def worker_loop(stop: threading.Event) -> None:
    # One job at a time: dubbing and lip-sync saturate the GPU.
    with db() as c:
        c.execute("UPDATE orders SET status='paid' WHERE status='processing'")
    last_purge = 0.0
    while not stop.is_set():
        if time.time() - last_purge > 3600:
            purge_expired()
            last_purge = time.time()
        with db() as c:
            order = c.execute("SELECT * FROM orders WHERE status='paid' "
                              "ORDER BY created LIMIT 1").fetchone()
        if not order:
            stop.wait(5)
            continue
        set_status(order["id"], "processing")
        try:
            run_order(order)
        except Exception as e:  # keep the queue alive; admin sees the error
            set_status(order["id"], "failed", error=str(e)[:500])


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    stop = threading.Event()
    if os.environ.get("PORTAL_WORKER", "1") == "1":
        threading.Thread(target=worker_loop, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.exception_handler(HTTPException)
async def friendly_errors(request: Request, exc: HTTPException):
    if exc.status_code == 401 or request.url.path.startswith("/stripe"):
        return await http_exception_handler(request, exc)
    return templates.TemplateResponse(
        request, "error.html", {"message": exc.detail or "حدث خطأ."},
        status_code=exc.status_code)
security = HTTPBasic()


def admin(creds: HTTPBasicCredentials = Depends(security)) -> None:
    if not ADMIN_PASSWORD or not hmac.compare_digest(
            creds.password.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


# ------------------------------------------------------------------- routes
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    prices = {"base": PRICE_BASE, "per_min": PRICE_PER_MIN,
              "lipsync": PRICE_LIPSYNC_PER_MIN, "shorts": PRICE_SHORTS,
              "max_minutes": MAX_MINUTES, "max_mb": MAX_UPLOAD_MB,
              "cover": PRICE_COVER if public_voices() else None}
    return templates.TemplateResponse(request, "index.html", {"prices": prices})


def check_common(email: str, rights: bool) -> None:
    if not rights:
        raise HTTPException(400, "يجب تأكيد امتلاكك حقوق المحتوى أو إذن صاحبه.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        raise HTTPException(400, "البريد الإلكتروني غير صالح.")


async def intake(order_id: str, url: str, upload: UploadFile | None,
                 max_minutes: float) -> tuple[str | None, str | None, str, float]:
    """Store the upload or probe the URL; return (url, file, title, minutes)."""
    url = url.strip()
    if upload and upload.filename:
        dest = DATA / "uploads" / f"{order_id}{Path(upload.filename).suffix.lower()[:8]}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with open(dest, "wb") as f:
            while chunk := await upload.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD_MB << 20:
                    f.close()
                    dest.unlink()
                    raise HTTPException(413, f"الحد الأقصى {MAX_UPLOAD_MB} ميغابايت.")
                f.write(chunk)
        minutes = probe_file(dest)
        result = (None, str(dest), upload.filename, minutes)
    elif url.startswith(("http://", "https://")):
        title, minutes = probe_url(url)
        result = (url, None, title, minutes)
    else:
        raise HTTPException(400, "أرسل رابطًا أو ارفع ملفًا.")
    if minutes <= 0 or minutes > max_minutes:
        if result[1]:
            Path(result[1]).unlink(missing_ok=True)
        raise HTTPException(400, f"المدة يجب ألا تتجاوز {max_minutes:g} دقيقة.")
    return result


def insert_order(order_id: str, *, name: str, email: str, src_url, src_file,
                 title: str, minutes: float, price: float, service: str = "dub",
                 dialect: str = "msa", lipsync: bool = False, shorts: bool = False,
                 voice: str | None = None, pitch: int = 0) -> str:
    token = secrets.token_urlsafe(16)
    with db() as c:
        c.execute(
            "INSERT INTO orders (id, token, created, name, email, source_url, "
            "source_file, title, minutes, dialect, lipsync, shorts, price, status, "
            "service, voice, pitch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, token, time.time(), name.strip()[:100], email.strip(),
             src_url, src_file, title[:200], round(minutes, 2), dialect,
             int(lipsync), int(shorts), price, "awaiting_payment", service,
             voice, pitch))
    return token


@app.post("/order")
async def create_order(
    email: str = Form(...), name: str = Form(""), url: str = Form(""),
    video: UploadFile | None = File(None), dialect: str = Form("msa"),
    lipsync: bool = Form(False), shorts: bool = Form(False),
    rights: bool = Form(False),
):
    check_common(email, rights)
    if dialect not in DIALECTS:
        raise HTTPException(400, "لهجة غير مدعومة.")
    order_id = secrets.token_hex(4)
    src_url, src_file, title, minutes = await intake(order_id, url, video, MAX_MINUTES)
    token = insert_order(order_id, name=name, email=email, src_url=src_url,
                         src_file=src_file, title=title, minutes=minutes,
                         price=quote(minutes, lipsync, shorts), dialect=dialect,
                         lipsync=lipsync, shorts=shorts)
    return RedirectResponse(f"/order/{token}", status_code=303)


@app.get("/cover", response_class=HTMLResponse)
def cover_page(request: Request):
    return templates.TemplateResponse(request, "cover.html", {
        "voices": public_voices(), "price": PRICE_COVER,
        "max_minutes": MAX_SONG_MINUTES, "max_mb": MAX_UPLOAD_MB})


@app.post("/cover")
async def create_cover(
    email: str = Form(...), name: str = Form(""), url: str = Form(""),
    song: UploadFile | None = File(None), voice: str = Form(...),
    pitch: int = Form(0), rights: bool = Form(False),
):
    check_common(email, rights)
    if voice not in public_voices():
        raise HTTPException(400, "الصوت المختار غير متاح.")
    if pitch not in (-12, 0, 12):
        raise HTTPException(400, "طبقة غير مدعومة.")
    order_id = secrets.token_hex(4)
    src_url, src_file, title, minutes = await intake(order_id, url, song,
                                                     MAX_SONG_MINUTES)
    token = insert_order(order_id, name=name, email=email, src_url=src_url,
                         src_file=src_file, title=title, minutes=minutes,
                         price=quote(minutes, False, False, "cover"),
                         service="cover", voice=voice, pitch=pitch)
    return RedirectResponse(f"/order/{token}", status_code=303)


@app.get("/order/{token}", response_class=HTMLResponse)
def order_page(request: Request, token: str):
    order = get_order(token)
    files = []
    if order["status"] == "done" and order["output_dir"]:
        root = Path(order["output_dir"])
        files = sorted(p.name for p in [*root.iterdir(), *(root / "shorts").glob("*")]
                       if DOWNLOADABLE.match(p.name))
    voice = public_voices().get(order["voice"] or "")
    return templates.TemplateResponse(request, "order.html", {
        "o": order, "files": files, "payment": PAYMENT,
        "voice_name": voice["name"] if voice else order["voice"],
        "instructions": MANUAL_INSTRUCTIONS})


@app.post("/order/{token}/pay")
def pay(token: str):
    order = get_order(token)
    if order["status"] != "awaiting_payment":
        return RedirectResponse(f"/order/{token}", status_code=303)
    if PAYMENT == "stripe" and STRIPE_SECRET:
        return RedirectResponse(stripe_checkout(order), status_code=303)
    return RedirectResponse(f"/order/{token}", status_code=303)


@app.get("/order/{token}/file/{name}")
def download(token: str, name: str):
    order = get_order(token)
    if order["status"] != "done" or not DOWNLOADABLE.match(name):
        raise HTTPException(404)
    root = Path(order["output_dir"])
    path = root / name if not name.startswith("short_") else root / "shorts" / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=f"{order['id']}_{name}")


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(404)
    event = verify_stripe(await request.body(),
                          request.headers.get("stripe-signature", ""),
                          STRIPE_WEBHOOK_SECRET)
    if event.get("type") == "checkout.session.completed":
        session = event["data"]["object"]
        oid = (session.get("metadata") or {}).get("order_id")
        if oid and session.get("payment_status") == "paid":
            with db() as c:
                c.execute("UPDATE orders SET status='paid', payment_ref=? "
                          "WHERE id=? AND status='awaiting_payment'",
                          (session.get("id"), oid))
    return {"ok": True}


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(admin)])
def admin_page(request: Request):
    with db() as c:
        orders = c.execute("SELECT * FROM orders ORDER BY created DESC LIMIT 200").fetchall()
        revenue = c.execute("SELECT COALESCE(SUM(price),0) FROM orders WHERE status "
                            "IN ('paid','processing','done')").fetchone()[0]
    return templates.TemplateResponse(request, "admin.html",
                                      {"orders": orders, "revenue": revenue})


@app.post("/admin/{order_id}/{action}", dependencies=[Depends(admin)])
def admin_action(order_id: str, action: str, ref: str = Form("")):
    allowed = {"mark_paid": ("awaiting_payment",), "retry": ("failed",),
               "cancel": ("awaiting_payment", "paid", "failed")}
    if action not in allowed:
        raise HTTPException(400)
    new = "cancelled" if action == "cancel" else "paid"
    with db() as c:
        changed = c.execute(f"UPDATE orders SET status=?, payment_ref=COALESCE(NULLIF(?,''), "
                  f"payment_ref), error=NULL WHERE id=? AND status IN "
                  f"({','.join('?' * len(allowed[action]))})",
                  (new, ref, order_id, *allowed[action])).rowcount
    if changed and action == "cancel":
        with db() as c:
            row = c.execute("SELECT source_file FROM orders WHERE id=?",
                            (order_id,)).fetchone()
        if row and row["source_file"]:
            Path(row["source_file"]).unlink(missing_ok=True)
        shutil.rmtree(DATA / "orders" / order_id, ignore_errors=True)
    return RedirectResponse("/admin", status_code=303)
