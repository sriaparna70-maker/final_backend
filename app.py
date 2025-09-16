import os, re, csv, ssl, smtplib, sqlite3, threading, base64
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from flask import Flask, request, jsonify
from flask_cors import CORS

# ---------- Writable storage (Render’s code dir is read-only) ----------
DATA_DIR = os.environ.get("DATA_DIR", "/var/tmp/sree_sastha_data")
os.makedirs(DATA_DIR, exist_ok=True)  # Mount a Render Disk & set DATA_DIR=/data if you want persistence
DB_PATH  = os.path.join(DATA_DIR, "leads.db")
CSV_PATH = os.path.join(DATA_DIR, "leads.csv")
# ----------------------------------------------------------------------

EMAIL_REGEX = re.compile(r"^[^@]+@[^@]+\.[^@]+$")
app = Flask(__name__)

# ---------- CORS ----------
def _get_allowed_origins():
    ao = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if ao:
        return [o.strip() for o in ao.split(",") if o.strip()]
    fo = os.environ.get("FRONTEND_ORIGIN", "").strip()
    return [fo] if fo else ["*"]

ALLOWED = _get_allowed_origins()
CORS(app,
     resources={r"/api/*": {"origins": ALLOWED}},
     methods=["GET","POST","OPTIONS"],
     allow_headers=["Content-Type","Authorization"])
# --------------------------

# ---------- DB helpers ----------
def _init_db():
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            con.commit()
        if not os.path.exists(CSV_PATH):
            with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["id","topic","name","email","message","created_at"])
    except Exception as e:
        app.logger.error(f"[DB init] {e}")

def _save_lead(topic, name, email, message):
    now = datetime.utcnow().isoformat(timespec="seconds")+"Z"
    lead_id = None
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("INSERT INTO leads (topic,name,email,message,created_at) VALUES (?,?,?,?,?)",
                        (topic, name, email, message, now))
            lead_id = cur.lastrowid
            con.commit()
    except Exception as e:
        app.logger.error(f"[DB insert] {e}")
    try:
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([lead_id or "", topic, name, email, message, now])
    except Exception as e:
        app.logger.error(f"[CSV append] {e}")
    return lead_id, now
# -------------------------------

# ---------- Email (Zoho SMTP) ----------
def _send_email_sync(subject: str, body: str, reply_to: str = "", attachments: list = None) -> bool:
    sender   = (os.environ.get("ZOHO_EMAIL") or "").strip()
    app_pass = (os.environ.get("ZOHO_APP_PASSWORD") or "").strip()
    to_addr  = (os.environ.get("ZOHO_TO_EMAIL") or sender).strip() or sender
    host     = (os.environ.get("ZOHO_SMTP_HOST") or "smtp.zoho.in").strip()
    port     = int(os.environ.get("ZOHO_SMTP_PORT") or "465")

    if not sender or not app_pass or not to_addr:
        app.logger.warning("[SMTP] Missing env vars; skip send")
        return False

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = to_addr
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(body, _charset="utf-8"))

    for att in (attachments or []):
        try:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(base64.b64decode(att.get("b64","")))
            encoders.encode_base64(part)
            fname = att.get("filename","attachment")
            ctype = att.get("content_type","application/octet-stream")
            part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
            part.add_header("Content-Type", ctype)
            msg.attach(part)
        except Exception as e:
            app.logger.error(f"[SMTP attach] {e}")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=12) as s:
            s.login(sender, app_pass)
            s.send_message(msg)
        return True
    except Exception as e:
        app.logger.error(f"[SMTP send] {e}")
        return False

def _send_email_background(subject, body, reply_to="", attachments=None):
    threading.Thread(target=_send_email_sync, args=(subject, body, reply_to, attachments), daemon=True).start()
# ---------------------------------------

# ---------- Routes ----------
@app.get("/api/health")
def health():
    try:
        return jsonify({
            "ok": True,
            "ts": datetime.utcnow().isoformat(timespec="seconds")+"Z",
            "data_dir": DATA_DIR,
            "db_exists": os.path.exists(DB_PATH),
            "csv_exists": os.path.exists(CSV_PATH),
            "allowed_origins": ALLOWED,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/contact")
def contact():
    _init_db()
    try:
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip()
        message = (data.get("message") or "").strip()

        if not name or not EMAIL_REGEX.match(email) or not message:
            return jsonify({"ok": False, "error": "Invalid input"}), 400
        if len(name) > 120 or len(email) > 200 or len(message) > 8000:
            return jsonify({"ok": False, "error": "Input too long"}), 413

        lead_id, created_at = _save_lead("contact", name, email, message)
        subject = f"New website inquiry from {name}"
        body    = f"Topic: CONTACT\nName: {name}\nEmail: {email}\n\n{message}\n(Lead #{lead_id} at {created_at})"
        _send_email_background(subject, body, reply_to=email)

        return jsonify({"ok": True, "id": lead_id, "created_at": created_at, "email_queued": True})
    except Exception as e:
        app.logger.error(f"[contact] {e}")
        return jsonify({"ok": False, "error": "server_error"}), 500

@app.post("/api/openaccess")
def openaccess():
    """
    Expected JSON:
    {
      "name": "Acme Ltd / John",
      "email": "x@y.z",
      "phone": "+91...",
      "sanctioned_load": "1000",     # kVA (string or number)
      "monthly_kwh": "350000",       # kWh (string or number)
      "callback": true,              # optional bool
      "eb_bill": {                   # optional file
        "filename": "bill.pdf",
        "content_type": "application/pdf",
        "b64": "<base64 data>"
      }
    }
    """
    _init_db()
    try:
        data = request.get_json(silent=True) or {}
        name  = (data.get("name")  or "").strip()
        email = (data.get("email") or "").strip()
        phone = (data.get("phone") or "").strip()
        sanctioned = str(data.get("sanctioned_load") or "").strip()
        monthly   = str(data.get("monthly_kwh")   or "").strip()
        callback  = bool(data.get("callback"))
        eb_bill   = data.get("eb_bill")

        if not name or not EMAIL_REGEX.match(email) or not sanctioned or not monthly:
            return jsonify({"ok": False, "error": "Invalid input"}), 400

        msg = (f"[Open Access Inquiry]\nPhone: {phone}\n"
               f"Sanctioned Load (kVA): {sanctioned}\nMonthly (kWh): {monthly}\n"
               f"Callback: {'Yes' if callback else 'No'}")
        lead_id, created_at = _save_lead("openaccess", name, email, msg)

        subject = "Open Access Inquiry"
        body    = (f"Topic: OPEN ACCESS\nName: {name}\nEmail: {email}\nPhone: {phone}\n"
                   f"Sanctioned Load (kVA): {sanctioned}\nMonthly Consumption (kWh): {monthly}\n"
                   f"Callback: {'Yes' if callback else 'No'}\n\n(Lead #{lead_id} at {created_at})")
        attachments = [eb_bill] if isinstance(eb_bill, dict) and eb_bill.get("b64") else []
        _send_email_background(subject, body, reply_to=email, attachments=attachments)

        return jsonify({"ok": True, "id": lead_id, "created_at": created_at, "email_queued": True})
    except Exception as e:
        app.logger.error(f"[openaccess] {e}")
        return jsonify({"ok": False, "error": "server_error"}), 500
# ---------------------------

# Init on import (for all gunicorn workers)
def _maybe_init():
    _init_db()
_maybe_init()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)
