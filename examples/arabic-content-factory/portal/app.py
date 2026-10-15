"""Customer portal: order -> quote -> pay -> automatic Arabic dub -> download.

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
DUB_EXTRA_ARGS = os.environ.get("DUB_EXTRA_ARGS", "").split()
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "30"))

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
DOWNLOADABLE = re.compile(r"^(dubbed_ar(_lipsync)?\.mp4|segments\.json|short_\d{2}\.mp4)$")

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
def quote(minutes: float, lipsync: bool, shorts: bool) -> float:
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
            f"Arabic dubbing ({DIALECTS[order['dialect']]}) #{order['id']}",
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
def run_order(order: sqlite3.Row) -> None:
    out = DATA / "orders" / order["id"]
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "worker.log", "a", encoding="utf-8")
    src = (["--url", order["source_url"]] if order["source_url"]
           else ["--file", order["source_file"]])
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
              "max_minutes": MAX_MINUTES, "max_mb": MAX_UPLOAD_MB}
    return templates.TemplateResponse(request, "index.html", {"prices": prices})


@app.post("/order")
async def create_order(
    email: str = Form(...), name: str = Form(""), url: str = Form(""),
    video: UploadFile | None = File(None), dialect: str = Form("msa"),
    lipsync: bool = Form(False), shorts: bool = Form(False),
    rights: bool = Form(False),
):
    if not rights:
        raise HTTPException(400, "يجب تأكيد امتلاكك حقوق الفيديو أو إذن صاحبه.")
    if dialect not in DIALECTS:
        raise HTTPException(400, "لهجة غير مدعومة.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        raise HTTPException(400, "البريد الإلكتروني غير صالح.")
    order_id = secrets.token_hex(4)
    url = url.strip()
    source_file = None
    if video and video.filename:
        dest = DATA / "uploads" / f"{order_id}{Path(video.filename).suffix.lower()[:8]}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with open(dest, "wb") as f:
            while chunk := await video.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD_MB << 20:
                    f.close()
                    dest.unlink()
                    raise HTTPException(413, f"الحد الأقصى {MAX_UPLOAD_MB} ميغابايت.")
                f.write(chunk)
        title, minutes = video.filename, probe_file(dest)
        source_file, url = str(dest), ""
    elif url.startswith(("http://", "https://")):
        title, minutes = probe_url(url)
    else:
        raise HTTPException(400, "أرسل رابط فيديو أو ارفع ملفًا.")
    if minutes <= 0 or minutes > MAX_MINUTES:
        raise HTTPException(400, f"مدة الفيديو يجب ألا تتجاوز {MAX_MINUTES:g} دقيقة.")

    token = secrets.token_urlsafe(16)
    with db() as c:
        c.execute(
            "INSERT INTO orders (id, token, created, name, email, source_url, "
            "source_file, title, minutes, dialect, lipsync, shorts, price, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, token, time.time(), name.strip()[:100], email.strip(),
             url or None, source_file, title[:200], round(minutes, 2), dialect,
             int(lipsync), int(shorts), quote(minutes, lipsync, shorts),
             "awaiting_payment"))
    return RedirectResponse(f"/order/{token}", status_code=303)


@app.get("/order/{token}", response_class=HTMLResponse)
def order_page(request: Request, token: str):
    order = get_order(token)
    files = []
    if order["status"] == "done" and order["output_dir"]:
        root = Path(order["output_dir"])
        files = sorted(p.name for p in [*root.iterdir(), *(root / "shorts").glob("*")]
                       if DOWNLOADABLE.match(p.name))
    return templates.TemplateResponse(request, "order.html", {
        "o": order, "files": files, "payment": PAYMENT,
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
