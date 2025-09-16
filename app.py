import os, re, sqlite3, csv, ssl, smtplib, json, threading
from email.mime.text import MIMEText
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime

APP_VER = "2025-09-15"

DB_PATH  = os.path.join(os.path.dirname(__file__), "leads.db")
CSV_PATH = os.path.join(os.path.dirname(__file__), "leads.csv")

app = Flask(__name__)

# CORS: allow your site; while testing you can keep "*"
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "*")
CORS(app, resources={r"/api/*": {"origins": FRONTEND_ORIGIN}})

EMAIL_REGEX = re.compile(r"^[^@]+@[^@]+\.[^@]+$")

# ---------- SQLite helpers ----------
_db_lock = threading.Lock()

def _connect():
    # timeout helps avoid 'database is locked'; WAL reduces writer blocking
    con = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _connect() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT,
            name TEXT,
            email TEXT,
            phone TEXT,
            company TEXT,
            sanctioned_load TEXT,
            monthly_kwh TEXT,
            message TEXT,
            created_at TEXT
        )
        """)
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["id","topic","name","email","phone","company",
                 "sanctioned_load","monthly_kwh","message","created_at"]
            )

def save_lead(**kw):
    now = datetime.utcnow().isoformat(timespec="seconds")+"Z"
    fields = ("topic","name","email","phone","company","sanctioned_load","monthly_kwh","message")
    row = [kw.get(k,"") for k in fields]
    with _db_lock:
        with _connect() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO leads (topic,name,email,phone,company,sanctioned_load,monthly_kwh,message,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (*row, now)
            )
            lead_id = cur.lastrowid
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([lead_id, *row, now])
    return lead_id, now

# ---------- SMTP (Zoho) ----------
def send_email_via_zoho(subject: str, body: str) -> bool:
    sender   = (os.environ.get("ZOHO_EMAIL") or "").strip()
    app_pass = (os.environ.get("ZOHO_APP_PASSWORD") or "").strip()
    to_addr  = (os.environ.get("ZOHO_TO_EMAIL") or sender).strip() or sender
    host     = (os.environ.get("ZOHO_SMTP_HOST") or "smtp.zoho.in").strip()
    port     = int(os.environ.get("ZOHO_SMTP_PORT") or "465")

    if not sender or not app_pass:
        # Don’t fail the request if mail isn’t configured
        app.logger.warning("ZOHO_EMAIL/ZOHO_APP_PASSWORD missing; skipping SMTP")
        return False

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_addr

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=25) as s:
            s.login(sender, app_pass)
            s.send_message(msg)
        return True
    except Exception as e:
        app.logger.error("SMTP failed: %s", e)
        return False

# ---------- Helpers ----------
def ok_response(data=None, code=200):
    payload = {"ok": True, **(data or {})}
    return jsonify(payload), code

def err_response(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code

# ---------- Routes ----------
@app.get("/api/health")
def health():
    return ok_response({"status":"up", "ver": APP_VER})

@app.post("/api/contact")
def contact():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    message = (data.get("message") or "").strip()

    if not name or not EMAIL_REGEX.match(email) or not message:
        return err_response("Invalid input", 400)

    lead_id, created_at = save_lead(
        topic="contact", name=name, email=email, message=message,
        phone="", company="", sanctioned_load="", monthly_kwh=""
    )

    body = f"New Contact Lead\n\nName: {name}\nEmail: {email}\n\nMessage:\n{message}\n\nID:{lead_id}  Time:{created_at}"
    sent = send_email_via_zoho(subject=f"Contact from {name}", body=body)

    # IMPORTANT: even if email fails, still return ok so the UI doesn’t scare users
    return ok_response({"id": lead_id, "created_at": created_at, "email_sent": bool(sent)})

@app.post("/api/oa-inquiry")
def oa_inquiry():
    data = request.get_json(silent=True) or {}
    name  = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    company = (data.get("company") or "").strip()
    phone   = (data.get("phone") or "").strip()
    sanctioned_load = (data.get("sanctioned_load") or "").strip()
    monthly_kwh     = (data.get("monthly_kwh") or "").strip()

    if not name or not company or not EMAIL_REGEX.match(email):
        return err_response("Invalid input", 400)

    lead_id, created_at = save_lead(
        topic="open-access", name=name, email=email, phone=phone, company=company,
        sanctioned_load=sanctioned_load, monthly_kwh=monthly_kwh, message=""
    )

    # Email summary (skip large attachments on free tier)
    lines = [
        "Open Access Inquiry",
        f"Name: {name}",
        f"Company: {company}",
        f"Email: {email}",
        f"Phone: {phone}",
        f"Sanctioned Load: {sanctioned_load}",
        f"Monthly kWh: {monthly_kwh}",
        f"ID:{lead_id}  Time:{created_at}"
    ]
    sent = send_email_via_zoho(subject=f"OA Inquiry — {company} ({name})",
                               body="\n".join(lines))

    return ok_response({"id": lead_id, "created_at": created_at, "email_sent": bool(sent)})

# ---------- Always JSON for errors on /api ----------
@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return err_response("Not found", 404)
    return e, 404

@app.errorhandler(500)
def server_error(e):
    app.logger.exception("Unhandled 500: %s", e)
    if request.path.startswith("/api/"):
        return err_response("Server error", 500)
    return e, 500

# ---------- Startup ----------
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)
