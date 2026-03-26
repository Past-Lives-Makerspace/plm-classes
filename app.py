"""
PLM Classes — Past Lives Makerspace class management system.
Replaces the broken Drupal/Acuity booking flow with a simple,
self-service system for instructors and admins.

Features: Student DB, discount codes, liability waivers, model releases,
iCal sync, Stripe payments (optional), Squarespace embed mode.

Run: python3 app.py
Visit: http://localhost:5001
Admin: http://localhost:5001/?admin=<token>
"""

import sqlite3
import secrets
import json
import os
import uuid
from datetime import datetime, date
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, g, Response, redirect

app = Flask(__name__)
DB_PATH = Path(__file__).parent / "classes.db"
UPLOAD_DIR = Path(__file__).parent / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_TOKEN = os.environ.get("PLM_ADMIN_TOKEN", "plm-admin-2026")
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB

# Stripe (optional)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
STRIPE_ENABLED = bool(STRIPE_SECRET_KEY)
if STRIPE_ENABLED:
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY

# ── Waiver texts ──────────────────────────────────────────────────────────────

LIABILITY_WAIVER_TEXT = """ASSUMPTION OF RISK AND WAIVER OF LIABILITY

I understand that participation in classes, workshops, and activities at Past Lives Makerspace ("PLM") involves inherent risks, including but not limited to: exposure to tools, machinery, and equipment; risk of cuts, burns, eye injury, hearing damage, and other physical harm; and exposure to dust, fumes, chemicals, and other materials.

I voluntarily assume all risks associated with my participation. I hereby release, waive, and discharge PLM, its owners, officers, employees, instructors, volunteers, and agents from any and all liability, claims, demands, or causes of action arising out of or related to my participation, including negligence.

I agree to follow all safety rules, instructions, and guidelines provided by PLM and its instructors. I understand that failure to do so may result in removal from the class without refund.

I confirm that I am at least 18 years of age (or have a parent/guardian signing on my behalf), that I am physically able to participate, and that I carry my own health insurance or accept financial responsibility for any medical treatment I may require.

Past Lives Makerspace LLC, 2808 SE 9th Ave, Portland, OR 97202"""

MODEL_RELEASE_TEXT = """MODEL RELEASE AND CONSENT TO USE OF IMAGE

I grant Past Lives Makerspace ("PLM"), its employees, and agents the right to photograph, video record, and otherwise capture my likeness during classes and events, and to use such images for promotional, educational, and marketing purposes including but not limited to: website, social media, printed materials, and press.

I waive any right to inspect or approve the finished images or the use to which they may be applied. I release PLM from any claims arising from the use of my likeness.

I understand that I may revoke this consent at any time by notifying PLM in writing at info@pastlives.space."""


# ── Database ──────────────────────────────────────────────────────────────────

def db():
    if "db" not in g:
        con = sqlite3.connect(str(DB_PATH))
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        g.db = con
    return g.db


@app.teardown_appcontext
def close_db(exc):
    con = g.pop("db", None)
    if con:
        con.close()


# ── CORS + Embed headers ─────────────────────────────────────────────────────

@app.after_request
def add_headers(response):
    origin = request.headers.get("Origin", "")
    allowed = ["https://pastlives.space", "https://www.pastlives.space"]
    if origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Instructor-Token, X-Admin-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://pastlives.space https://www.pastlives.space"
    return response


@app.route("/api/<path:path>", methods=["OPTIONS"])
def cors_preflight(path):
    return "", 204


# ── Schema & Migration ────────────────────────────────────────────────────────

from migrate import migrate


def audit(table_name, row_id, action, changed_by=None, old_value=None, new_value=None):
    con = db()
    con.execute(
        "INSERT INTO audit_log (table_name, row_id, action, changed_by, old_value, new_value) VALUES (?,?,?,?,?,?)",
        (table_name, row_id, action, changed_by,
         json.dumps(old_value) if old_value else None,
         json.dumps(new_value) if new_value else None),
    )
    con.commit()


# ── Student auto-linking ──────────────────────────────────────────────────────

def get_or_create_student(con, first_name, last_name, email, **kwargs):
    row = con.execute("SELECT id FROM students WHERE email=?", (email,)).fetchone()
    if row:
        con.execute("""UPDATE students SET first_name=?, last_name=?,
            phone=COALESCE(?, phone), pronouns=COALESCE(?, pronouns),
            updated_at=datetime('now') WHERE id=?""",
            (first_name, last_name, kwargs.get("phone"), kwargs.get("pronouns"), row[0]))
        return row[0]
    student_id = con.execute("""
        INSERT INTO students (first_name, last_name, pronouns, email, phone,
            address_line1, address_city, address_state, address_zip, is_member)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (first_name, last_name, kwargs.get("pronouns"), email, kwargs.get("phone"),
          kwargs.get("address_line1"), kwargs.get("address_city"),
          kwargs.get("address_state"), kwargs.get("address_zip"),
          kwargs.get("is_member", 0))).lastrowid
    return student_id


# ── Discount code validation ──────────────────────────────────────────────────

def validate_discount_code(con, code):
    if not code:
        return None, None
    row = con.execute("SELECT * FROM discount_codes WHERE code=?", (code.strip().upper(),)).fetchone()
    if not row:
        return None, "Invalid discount code"
    d = dict(row)
    if not d["is_active"]:
        return None, "This code is no longer active"
    today = date.today().isoformat()
    if d["valid_from"] and today < d["valid_from"]:
        return None, "This code is not yet valid"
    if d["valid_until"] and today > d["valid_until"]:
        return None, "This code has expired"
    if d["max_uses"] and d["use_count"] >= d["max_uses"]:
        return None, "This code has reached its maximum uses"
    return d, None


def apply_discount(price_cents, discount):
    if not discount:
        return price_cents
    if discount.get("discount_pct"):
        return int(price_cents * (100 - discount["discount_pct"]) / 100)
    if discount.get("discount_fixed_cents"):
        return max(0, price_cents - discount["discount_fixed_cents"])
    return price_cents


# ── Auth helpers ──────────────────────────────────────────────────────────────

def get_instructor():
    token = request.headers.get("X-Instructor-Token") or request.args.get("token")
    if not token:
        return None
    con = db()
    row = con.execute("SELECT * FROM instructors WHERE login_token=? AND is_active=1", (token,)).fetchone()
    return dict(row) if row else None


def require_instructor():
    inst = get_instructor()
    if not inst:
        return None, (jsonify({"error": "Instructor login required"}), 401)
    return inst, None


def is_admin():
    token = request.headers.get("X-Admin-Token") or request.args.get("admin")
    return token == ADMIN_TOKEN


def require_admin():
    if not is_admin():
        return jsonify({"error": "Admin access required"}), 403
    return None


# ── Public API ────────────────────────────────────────────────────────────────

@app.get("/api/config")
def api_config():
    return jsonify({"stripe_enabled": STRIPE_ENABLED})


@app.get("/api/classes")
def api_classes():
    con = db()
    category = request.args.get("category")
    status_filter = request.args.get("status", "published")

    where = "WHERE c.status = ?"
    params = [status_filter]
    if category:
        where += " AND cat.slug = ?"
        params.append(category)

    rows = con.execute(f"""
        SELECT c.*, cat.name as category_name, cat.slug as category_slug, cat.image_url as category_image_url,
               i.name as instructor_name, i.social_handle as instructor_social
        FROM classes c
        JOIN categories cat ON c.category_id = cat.id
        JOIN instructors i ON c.instructor_id = i.id
        {where}
        ORDER BY c.created_at DESC
    """, params).fetchall()

    classes = []
    for r in rows:
        c = dict(r)
        sessions = con.execute(
            "SELECT * FROM sessions WHERE class_id=? ORDER BY session_date, start_time", (c["id"],)
        ).fetchall()
        c["sessions"] = [dict(s) for s in sessions]
        reg_count = con.execute(
            "SELECT COUNT(*) FROM registrations WHERE class_id=? AND status IN ('pending','confirmed')", (c["id"],)
        ).fetchone()[0]
        c["spots_left"] = max(0, c["capacity"] - reg_count)
        c["registration_count"] = reg_count
        classes.append(c)

    def sort_key(c):
        dates = [s["session_date"] for s in c["sessions"]]
        future = [d for d in dates if d >= date.today().isoformat()]
        if future:
            return (0, min(future))
        if dates:
            return (1, min(dates))
        return (2, c["created_at"])
    classes.sort(key=sort_key)

    return jsonify({"classes": classes})


@app.get("/api/classes/<slug>")
def api_class_detail(slug):
    con = db()
    row = con.execute("""
        SELECT c.*, cat.name as category_name, cat.slug as category_slug, cat.image_url as category_image_url,
               i.name as instructor_name, i.email as instructor_email,
               i.bio as instructor_bio, i.website as instructor_website,
               i.social_handle as instructor_social
        FROM classes c
        JOIN categories cat ON c.category_id = cat.id
        JOIN instructors i ON c.instructor_id = i.id
        WHERE c.slug = ?
    """, (slug,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    c = dict(row)
    c["sessions"] = [dict(s) for s in con.execute(
        "SELECT * FROM sessions WHERE class_id=? ORDER BY session_date, start_time", (c["id"],)
    ).fetchall()]
    reg_count = con.execute(
        "SELECT COUNT(*) FROM registrations WHERE class_id=? AND status IN ('pending','confirmed')", (c["id"],)
    ).fetchone()[0]
    c["spots_left"] = max(0, c["capacity"] - reg_count)
    return jsonify(c)


@app.get("/api/categories")
def api_categories():
    con = db()
    rows = con.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    cats = [dict(r) for r in rows]
    for cat in cats:
        cat["class_count"] = con.execute(
            "SELECT COUNT(*) FROM classes WHERE category_id=? AND status='published'", (cat["id"],)
        ).fetchone()[0]
    return jsonify({"categories": cats})


@app.get("/api/instructors/<slug>")
def api_instructor_profile(slug):
    """Public instructor profile with classes and history."""
    con = db()
    row = con.execute("SELECT id, name, slug, bio, photo_url, website, social_handle FROM instructors WHERE slug=? AND is_active=1", (slug,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    inst = dict(row)

    # Current published classes
    classes = con.execute("""
        SELECT c.*, cat.name as category_name, cat.slug as category_slug, cat.image_url as category_image_url
        FROM classes c JOIN categories cat ON c.category_id = cat.id
        WHERE c.instructor_id = ? AND c.status = 'published'
        ORDER BY c.created_at DESC
    """, (inst["id"],)).fetchall()

    current, past = [], []
    today = date.today().isoformat()
    for r in classes:
        c = dict(r)
        sessions = con.execute("SELECT * FROM sessions WHERE class_id=? ORDER BY session_date", (c["id"],)).fetchall()
        c["sessions"] = [dict(s) for s in sessions]
        last_date = max((s["session_date"] for s in c["sessions"]), default=None)
        if last_date and last_date < today:
            past.append(c)
        else:
            reg_count = con.execute(
                "SELECT COUNT(*) FROM registrations WHERE class_id=? AND status IN ('pending','confirmed')", (c["id"],)
            ).fetchone()[0]
            c["spots_left"] = max(0, c["capacity"] - reg_count)
            current.append(c)

    # Archived classes (history)
    archived = con.execute("""
        SELECT c.title, c.slug, cat.name as category_name
        FROM classes c JOIN categories cat ON c.category_id = cat.id
        WHERE c.instructor_id = ? AND c.status = 'archived'
        ORDER BY c.created_at DESC
    """, (inst["id"],)).fetchall()

    inst["current_classes"] = current
    inst["past_classes"] = past + [dict(a) for a in archived]
    return jsonify(inst)


@app.get("/api/waiver-text")
def api_waiver_text():
    return jsonify({
        "liability": LIABILITY_WAIVER_TEXT,
        "model_release": MODEL_RELEASE_TEXT,
    })


@app.post("/api/validate-discount")
def api_validate_discount():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"valid": False, "error": "No code provided"})
    con = db()
    discount, err = validate_discount_code(con, code)
    if err:
        return jsonify({"valid": False, "error": err})
    return jsonify({
        "valid": True,
        "discount_pct": discount.get("discount_pct"),
        "discount_fixed_cents": discount.get("discount_fixed_cents"),
        "description": discount.get("description"),
    })


# ── Registration ──────────────────────────────────────────────────────────────

@app.post("/api/register/<slug>")
def api_register(slug):
    con = db()
    cls = con.execute(
        "SELECT id, capacity, status, price_cents, member_discount_pct, requires_model_release, title FROM classes WHERE slug=?",
        (slug,)
    ).fetchone()
    if not cls:
        return jsonify({"error": "Class not found"}), 404
    if cls["status"] != "published":
        return jsonify({"error": "Class is not open for registration"}), 400

    data = request.get_json(silent=True) or {}
    first = (data.get("first_name") or "").strip()
    last = (data.get("last_name") or "").strip()
    email = (data.get("email") or "").strip().lower()

    if not first or not last or not email:
        return jsonify({"error": "First name, last name, and email are required"}), 400

    # Waiver validation
    if not data.get("waiver_accepted"):
        return jsonify({"error": "You must accept the liability waiver to register"}), 400
    if not (data.get("waiver_signature") or "").strip():
        return jsonify({"error": "Please type your full name as your electronic signature"}), 400
    if cls["requires_model_release"]:
        if not data.get("model_release_accepted"):
            return jsonify({"error": "This class requires a model release form"}), 400
        if not (data.get("model_release_signature") or "").strip():
            return jsonify({"error": "Please sign the model release form"}), 400

    # Discount code
    discount_code = (data.get("discount_code") or "").strip()
    discount, disc_err = validate_discount_code(con, discount_code) if discount_code else (None, None)
    if disc_err:
        return jsonify({"error": disc_err}), 400

    # Calculate price
    price = cls["price_cents"]
    if discount:
        price = apply_discount(price, discount)
    elif data.get("is_member") and cls["member_discount_pct"]:
        price = int(price * (100 - cls["member_discount_pct"]) / 100)

    # Spots check
    reg_count = con.execute(
        "SELECT COUNT(*) FROM registrations WHERE class_id=? AND status IN ('pending','confirmed')", (cls["id"],)
    ).fetchone()[0]
    status = "waitlisted" if reg_count >= cls["capacity"] else "pending"

    # Create/link student
    student_id = get_or_create_student(con, first, last, email,
        phone=data.get("phone"), pronouns=data.get("pronouns"),
        address_line1=data.get("address_line1"), address_city=data.get("address_city"),
        address_state=data.get("address_state"), address_zip=data.get("address_zip"),
        is_member=1 if data.get("is_member") or discount else 0)

    # Create registration
    reg_id = con.execute("""
        INSERT INTO registrations
            (class_id, student_id, first_name, last_name, pronouns, email, phone,
             address_line1, address_city, address_state, address_zip,
             is_member, prior_experience, looking_for, discount_code,
             amount_paid_cents, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        cls["id"], student_id, first, last, data.get("pronouns"), email, data.get("phone"),
        data.get("address_line1"), data.get("address_city"),
        data.get("address_state"), data.get("address_zip"),
        1 if data.get("is_member") else 0, data.get("prior_experience"), data.get("looking_for"),
        discount_code.upper() if discount_code else None, price, status,
    )).lastrowid
    con.commit()

    # Increment discount code usage
    if discount:
        con.execute("UPDATE discount_codes SET use_count = use_count + 1 WHERE id=?", (discount["id"],))
        con.commit()

    # Create waiver records
    ip = request.remote_addr or "unknown"
    con.execute("""
        INSERT INTO waivers (student_id, registration_id, waiver_type, waiver_text, ip_address, signature_text)
        VALUES (?,?,?,?,?,?)
    """, (student_id, reg_id, "liability", LIABILITY_WAIVER_TEXT, ip, data["waiver_signature"].strip()))
    if cls["requires_model_release"]:
        con.execute("""
            INSERT INTO waivers (student_id, registration_id, waiver_type, waiver_text, ip_address, signature_text)
            VALUES (?,?,?,?,?,?)
        """, (student_id, reg_id, "model_release", MODEL_RELEASE_TEXT, ip, data["model_release_signature"].strip()))
    con.commit()

    audit("registrations", reg_id, "create", changed_by=email)

    # Stripe payment
    if STRIPE_ENABLED and price > 0 and status != "waitlisted":
        try:
            base_url = request.host_url.rstrip("/")
            session = stripe.checkout.Session.create(
                mode="payment",
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": price,
                        "product_data": {"name": cls["title"]},
                    },
                    "quantity": 1,
                }],
                customer_email=email,
                success_url=f"{base_url}/?payment=success&reg={reg_id}",
                cancel_url=f"{base_url}/?payment=cancel&reg={reg_id}",
                metadata={"registration_id": str(reg_id)},
            )
            con.execute("UPDATE registrations SET stripe_session_id=? WHERE id=?", (session.id, reg_id))
            con.commit()
            return jsonify({
                "ok": True, "registration_id": reg_id, "status": status,
                "amount_cents": price, "checkout_url": session.url,
            })
        except Exception as e:
            print(f"[WARN] Stripe checkout creation failed: {e}")
            # Fall through to non-Stripe response

    return jsonify({
        "ok": True, "registration_id": reg_id, "status": status,
        "amount_cents": price, "waitlisted": status == "waitlisted",
    })


# ── Stripe webhook ────────────────────────────────────────────────────────────

@app.post("/api/stripe/webhook")
def stripe_webhook():
    if not STRIPE_ENABLED:
        return jsonify({"error": "Stripe not configured"}), 400

    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = json.loads(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if event.get("type") == "checkout.session.completed":
        session = event["data"]["object"]
        reg_id = session.get("metadata", {}).get("registration_id")
        if reg_id:
            con = sqlite3.connect(str(DB_PATH))
            con.row_factory = sqlite3.Row
            con.execute("""
                UPDATE registrations SET status='confirmed', stripe_payment_id=?,
                confirmed_at=datetime('now') WHERE id=? AND status='pending'
            """, (session.get("payment_intent"), int(reg_id)))
            con.commit()
            con.close()

    return jsonify({"ok": True})


# ── iCal / Calendar ──────────────────────────────────────────────────────────

def _ical_dt(date_str, time_str):
    """Convert date + time strings to iCal DTSTART format with timezone."""
    d = date_str.replace("-", "")
    t = time_str.replace(":", "") + "00"
    return f"TZID=America/Los_Angeles:{d}T{t}"


def _ical_event(cls_title, instructor, session, uid):
    """Build a single VEVENT string."""
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTART;{_ical_dt(session['session_date'], session['start_time'])}",
        f"DTEND;{_ical_dt(session['session_date'], session['end_time'])}",
        f"SUMMARY:{cls_title}",
        f"DESCRIPTION:with {instructor}",
        "LOCATION:Past Lives Makerspace\\, 2808 SE 9th Ave\\, Portland\\, OR 97202",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        "END:VEVENT",
    ]
    return "\r\n".join(lines)


@app.get("/api/classes/<slug>/ical")
def api_class_ical(slug):
    con = db()
    cls = con.execute("""
        SELECT c.title, c.slug, i.name as instructor_name
        FROM classes c JOIN instructors i ON c.instructor_id = i.id
        WHERE c.slug = ?
    """, (slug,)).fetchone()
    if not cls:
        return "Not found", 404

    sessions = con.execute(
        "SELECT * FROM sessions WHERE class_id=(SELECT id FROM classes WHERE slug=?) ORDER BY session_date",
        (slug,)
    ).fetchall()

    events = []
    for i, s in enumerate(sessions):
        uid = f"{cls['slug']}-session{i+1}@pastlives.space"
        events.append(_ical_event(cls["title"], cls["instructor_name"], dict(s), uid))

    cal = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Past Lives Makerspace//Classes//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        *events,
        "END:VCALENDAR",
    ])

    filename = f"{cls['slug']}.ics"
    return Response(cal, mimetype="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/calendar.ics")
def api_full_calendar():
    con = db()
    rows = con.execute("""
        SELECT c.id, c.title, c.slug, i.name as instructor_name
        FROM classes c JOIN instructors i ON c.instructor_id = i.id
        WHERE c.status = 'published'
    """).fetchall()

    events = []
    for cls in rows:
        sessions = con.execute(
            "SELECT * FROM sessions WHERE class_id=? ORDER BY session_date", (cls["id"],)
        ).fetchall()
        for i, s in enumerate(sessions):
            uid = f"{cls['slug']}-session{i+1}@pastlives.space"
            events.append(_ical_event(cls["title"], cls["instructor_name"], dict(s), uid))

    cal = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Past Lives Makerspace//Classes//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Past Lives Classes",
        *events,
        "END:VCALENDAR",
    ])

    return Response(cal, mimetype="text/calendar")


# ── Instructor API ────────────────────────────────────────────────────────────

@app.get("/api/instructor/me")
def api_instructor_me():
    inst, err = require_instructor()
    if err:
        return err
    return jsonify(inst)


@app.get("/api/instructor/classes")
def api_instructor_classes():
    inst, err = require_instructor()
    if err:
        return err

    con = db()
    rows = con.execute("""
        SELECT c.*, cat.name as category_name, cat.slug as category_slug
        FROM classes c JOIN categories cat ON c.category_id = cat.id
        WHERE c.instructor_id = ? ORDER BY c.created_at DESC
    """, (inst["id"],)).fetchall()

    classes = []
    for r in rows:
        c = dict(r)
        c["sessions"] = [dict(s) for s in con.execute(
            "SELECT * FROM sessions WHERE class_id=? ORDER BY session_date, start_time", (c["id"],)
        ).fetchall()]
        reg_count = con.execute(
            "SELECT COUNT(*) FROM registrations WHERE class_id=? AND status IN ('pending','confirmed')", (c["id"],)
        ).fetchone()[0]
        c["spots_left"] = max(0, c["capacity"] - reg_count)
        c["registration_count"] = reg_count
        classes.append(c)

    return jsonify({"classes": classes})


def _make_slug(title):
    import re
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug).strip("-")
    return slug[:80]


@app.post("/api/instructor/classes")
def api_instructor_create_class():
    inst, err = require_instructor()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    category_id = data.get("category_id")
    price = data.get("price_cents") or data.get("price")

    if not title:
        return jsonify({"error": "Title is required"}), 400
    if not category_id:
        return jsonify({"error": "Category is required"}), 400
    if not price:
        return jsonify({"error": "Price is required"}), 400

    if isinstance(price, float) or (isinstance(price, (int, float)) and price < 1000):
        price = int(price * 100)
    else:
        price = int(price)

    slug = _make_slug(title)
    con = db()
    base_slug = slug
    counter = 1
    while con.execute("SELECT 1 FROM classes WHERE slug=?", (slug,)).fetchone():
        slug = f"{base_slug}-{counter}"
        counter += 1

    class_id = con.execute("""
        INSERT INTO classes
            (title, slug, category_id, instructor_id, description, prerequisites,
             materials_included, materials_to_bring, safety_requirements,
             age_minimum, age_guardian_note, price_cents, member_discount_pct,
             capacity, scheduling_model, flexible_note, is_private, private_for_name,
             recurring_pattern, requires_model_release, status, created_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?)
    """, (
        title, slug, category_id, inst["id"],
        data.get("description"), data.get("prerequisites"),
        data.get("materials_included"), data.get("materials_to_bring"),
        data.get("safety_requirements"),
        data.get("age_minimum"), data.get("age_guardian_note"),
        price, data.get("member_discount_pct", 10),
        data.get("capacity", 6),
        data.get("scheduling_model", "fixed"),
        data.get("flexible_note"),
        1 if data.get("is_private") else 0,
        data.get("private_for_name"),
        data.get("recurring_pattern"),
        1 if data.get("requires_model_release") else 0,
        inst["id"],
    )).lastrowid
    con.commit()

    sessions = data.get("sessions") or []
    for i, s in enumerate(sessions):
        con.execute("""
            INSERT INTO sessions (class_id, session_date, start_time, end_time, duration_minutes, sort_order)
            VALUES (?,?,?,?,?,?)
        """, (class_id, s["date"], s["start_time"], s["end_time"], s.get("duration_minutes", 0), i))
    con.commit()

    audit("classes", class_id, "create", changed_by=inst["name"])
    return jsonify({"ok": True, "class_id": class_id, "slug": slug})


@app.put("/api/instructor/classes/<int:class_id>")
def api_instructor_update_class(class_id):
    inst, err = require_instructor()
    if err:
        return err

    con = db()
    cls = con.execute("SELECT * FROM classes WHERE id=? AND instructor_id=?", (class_id, inst["id"])).fetchone()
    if not cls:
        return jsonify({"error": "Class not found or not yours"}), 404
    if cls["status"] not in ("draft", "pending"):
        return jsonify({"error": "Can only edit draft or pending classes"}), 400

    data = request.get_json(silent=True) or {}
    fields = ["title", "description", "prerequisites", "materials_included",
              "materials_to_bring", "safety_requirements", "age_minimum",
              "age_guardian_note", "capacity", "scheduling_model", "flexible_note",
              "is_private", "private_for_name", "recurring_pattern", "requires_model_release"]

    updates, params = [], []
    for f in fields:
        if f in data:
            updates.append(f"{f}=?")
            params.append(data[f])
    if "category_id" in data:
        updates.append("category_id=?")
        params.append(data["category_id"])
    if "price_cents" in data or "price" in data:
        price = data.get("price_cents") or data.get("price")
        if isinstance(price, float) or (isinstance(price, (int, float)) and price < 1000):
            price = int(price * 100)
        updates.append("price_cents=?")
        params.append(int(price))

    if updates:
        updates.append("updated_at=datetime('now')")
        params.append(class_id)
        con.execute(f"UPDATE classes SET {', '.join(updates)} WHERE id=?", params)
        con.commit()

    if "sessions" in data:
        con.execute("DELETE FROM sessions WHERE class_id=?", (class_id,))
        for i, s in enumerate(data["sessions"]):
            con.execute("""
                INSERT INTO sessions (class_id, session_date, start_time, end_time, duration_minutes, sort_order)
                VALUES (?,?,?,?,?,?)
            """, (class_id, s["date"], s["start_time"], s["end_time"], s.get("duration_minutes", 0), i))
        con.commit()

    audit("classes", class_id, "update", changed_by=inst["name"])
    return jsonify({"ok": True})


@app.post("/api/instructor/classes/<int:class_id>/submit")
def api_instructor_submit_class(class_id):
    inst, err = require_instructor()
    if err:
        return err
    con = db()
    cls = con.execute("SELECT * FROM classes WHERE id=? AND instructor_id=?", (class_id, inst["id"])).fetchone()
    if not cls:
        return jsonify({"error": "Class not found or not yours"}), 404
    if cls["status"] != "draft":
        return jsonify({"error": "Only draft classes can be submitted"}), 400
    con.execute("UPDATE classes SET status='pending', updated_at=datetime('now') WHERE id=?", (class_id,))
    con.commit()
    audit("classes", class_id, "status_change", changed_by=inst["name"], old_value="draft", new_value="pending")
    return jsonify({"ok": True, "status": "pending"})


@app.post("/api/instructor/classes/<int:class_id>/image")
def api_instructor_upload_image(class_id):
    inst, err = require_instructor()
    if err:
        return err
    con = db()
    cls = con.execute("SELECT id FROM classes WHERE id=? AND instructor_id=?", (class_id, inst["id"])).fetchone()
    if not cls:
        return jsonify({"error": "Class not found or not yours"}), 404

    if "image" not in request.files:
        return jsonify({"error": "No image file provided"}), 400

    f = request.files["image"]
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXT:
        return jsonify({"error": f"Invalid file type. Allowed: {', '.join(ALLOWED_IMAGE_EXT)}"}), 400

    data = f.read()
    if len(data) > MAX_IMAGE_SIZE:
        return jsonify({"error": "Image too large (max 5MB)"}), 400

    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = UPLOAD_DIR / filename
    filepath.write_bytes(data)

    image_url = f"/static/uploads/{filename}"
    con.execute("UPDATE classes SET image_url=?, updated_at=datetime('now') WHERE id=?", (image_url, class_id))
    con.commit()

    return jsonify({"ok": True, "image_url": image_url})


@app.get("/api/instructor/registrations")
def api_instructor_registrations():
    inst, err = require_instructor()
    if err:
        return err
    con = db()
    rows = con.execute("""
        SELECT r.*, c.title as class_title, c.slug as class_slug
        FROM registrations r JOIN classes c ON r.class_id = c.id
        WHERE c.instructor_id = ? ORDER BY r.registered_at DESC
    """, (inst["id"],)).fetchall()
    return jsonify({"registrations": [dict(r) for r in rows]})


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.get("/api/admin/pending")
def api_admin_pending():
    err = require_admin()
    if err:
        return err
    con = db()
    rows = con.execute("""
        SELECT c.*, cat.name as category_name, i.name as instructor_name, i.email as instructor_email
        FROM classes c JOIN categories cat ON c.category_id = cat.id
        JOIN instructors i ON c.instructor_id = i.id
        WHERE c.status = 'pending' ORDER BY c.updated_at ASC
    """).fetchall()
    classes = []
    for r in rows:
        c = dict(r)
        c["sessions"] = [dict(s) for s in con.execute(
            "SELECT * FROM sessions WHERE class_id=? ORDER BY session_date", (c["id"],)
        ).fetchall()]
        classes.append(c)
    return jsonify({"classes": classes})


@app.post("/api/admin/classes/<int:class_id>/approve")
def api_admin_approve(class_id):
    err = require_admin()
    if err:
        return err
    con = db()
    cls = con.execute("SELECT status FROM classes WHERE id=?", (class_id,)).fetchone()
    if not cls:
        return jsonify({"error": "Not found"}), 404
    con.execute("""
        UPDATE classes SET status='published', approved_by='admin',
        published_at=datetime('now'), updated_at=datetime('now') WHERE id=?
    """, (class_id,))
    con.commit()
    audit("classes", class_id, "status_change", changed_by="admin", old_value=cls["status"], new_value="published")
    return jsonify({"ok": True, "status": "published"})


@app.post("/api/admin/classes/<int:class_id>/reject")
def api_admin_reject(class_id):
    err = require_admin()
    if err:
        return err
    con = db()
    data = request.get_json(silent=True) or {}
    con.execute("UPDATE classes SET status='draft', updated_at=datetime('now') WHERE id=?", (class_id,))
    con.commit()
    audit("classes", class_id, "status_change", changed_by="admin",
          old_value="pending", new_value=f"draft (rejected: {data.get('reason', '')})")
    return jsonify({"ok": True, "status": "draft"})


@app.post("/api/admin/classes/<int:class_id>/archive")
def api_admin_archive(class_id):
    err = require_admin()
    if err:
        return err
    con = db()
    con.execute("UPDATE classes SET status='archived', updated_at=datetime('now') WHERE id=?", (class_id,))
    con.commit()
    audit("classes", class_id, "status_change", changed_by="admin", old_value="published", new_value="archived")
    return jsonify({"ok": True})


@app.get("/api/admin/classes")
def api_admin_all_classes():
    err = require_admin()
    if err:
        return err
    con = db()
    rows = con.execute("""
        SELECT c.*, cat.name as category_name, cat.slug as category_slug,
               i.name as instructor_name, i.email as instructor_email
        FROM classes c JOIN categories cat ON c.category_id = cat.id
        JOIN instructors i ON c.instructor_id = i.id
        ORDER BY CASE c.status WHEN 'pending' THEN 0 WHEN 'published' THEN 1
                 WHEN 'draft' THEN 2 WHEN 'archived' THEN 3 END, c.updated_at DESC
    """).fetchall()
    classes = []
    for r in rows:
        c = dict(r)
        c["sessions"] = [dict(s) for s in con.execute(
            "SELECT * FROM sessions WHERE class_id=? ORDER BY session_date", (c["id"],)
        ).fetchall()]
        reg_count = con.execute(
            "SELECT COUNT(*) FROM registrations WHERE class_id=? AND status IN ('pending','confirmed')", (c["id"],)
        ).fetchone()[0]
        c["spots_left"] = max(0, c["capacity"] - reg_count)
        c["registration_count"] = reg_count
        classes.append(c)
    return jsonify({"classes": classes})


@app.get("/api/admin/registrations")
def api_admin_registrations():
    err = require_admin()
    if err:
        return err
    con = db()
    class_id = request.args.get("class_id")
    where, params = "", []
    if class_id:
        where = "WHERE r.class_id = ?"
        params = [class_id]
    rows = con.execute(f"""
        SELECT r.*, c.title as class_title, c.slug as class_slug, i.name as instructor_name
        FROM registrations r JOIN classes c ON r.class_id = c.id
        JOIN instructors i ON c.instructor_id = i.id {where}
        ORDER BY r.registered_at DESC
    """, params).fetchall()
    return jsonify({"registrations": [dict(r) for r in rows]})


@app.post("/api/admin/registrations/<int:reg_id>/confirm")
def api_admin_confirm_reg(reg_id):
    err = require_admin()
    if err:
        return err
    con = db()
    con.execute("UPDATE registrations SET status='confirmed', confirmed_at=datetime('now') WHERE id=?", (reg_id,))
    con.commit()
    audit("registrations", reg_id, "status_change", changed_by="admin", old_value="pending", new_value="confirmed")
    return jsonify({"ok": True})


@app.post("/api/admin/registrations/<int:reg_id>/cancel")
def api_admin_cancel_reg(reg_id):
    err = require_admin()
    if err:
        return err
    con = db()
    con.execute("UPDATE registrations SET status='cancelled', cancelled_at=datetime('now') WHERE id=?", (reg_id,))
    con.commit()
    audit("registrations", reg_id, "status_change", changed_by="admin", old_value="pending", new_value="cancelled")
    return jsonify({"ok": True})


# ── Admin: Students ───────────────────────────────────────────────────────────

@app.get("/api/admin/students")
def api_admin_students():
    err = require_admin()
    if err:
        return err
    con = db()
    rows = con.execute("""
        SELECT s.*, COUNT(r.id) as registration_count
        FROM students s LEFT JOIN registrations r ON r.student_id = s.id
        GROUP BY s.id ORDER BY s.created_at DESC
    """).fetchall()
    return jsonify({"students": [dict(r) for r in rows]})


@app.get("/api/admin/students/<int:student_id>")
def api_admin_student_detail(student_id):
    err = require_admin()
    if err:
        return err
    con = db()
    student = con.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        return jsonify({"error": "Not found"}), 404

    regs = con.execute("""
        SELECT r.*, c.title as class_title FROM registrations r
        JOIN classes c ON r.class_id = c.id WHERE r.student_id = ?
        ORDER BY r.registered_at DESC
    """, (student_id,)).fetchall()

    waivers = con.execute("SELECT * FROM waivers WHERE student_id=? ORDER BY signed_at DESC",
                          (student_id,)).fetchall()

    return jsonify({
        "student": dict(student),
        "registrations": [dict(r) for r in regs],
        "waivers": [dict(w) for w in waivers],
    })


# ── Admin: Instructors ────────────────────────────────────────────────────────

@app.post("/api/admin/instructors")
def api_admin_create_instructor():
    err = require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    token = secrets.token_urlsafe(32)
    con = db()
    inst_id = con.execute("""
        INSERT INTO instructors (name, email, phone, bio, website, social_handle, login_token)
        VALUES (?,?,?,?,?,?,?)
    """, (name, email or None, data.get("phone"), data.get("bio"),
          data.get("website"), data.get("social_handle"), token)).lastrowid
    con.commit()
    audit("instructors", inst_id, "create", changed_by="admin")
    return jsonify({"ok": True, "instructor_id": inst_id, "login_token": token,
                    "login_url": f"{request.host_url}?token={token}"})


@app.get("/api/admin/instructors")
def api_admin_list_instructors():
    err = require_admin()
    if err:
        return err
    con = db()
    rows = con.execute("SELECT * FROM instructors ORDER BY name").fetchall()
    instructors = []
    for r in rows:
        inst = dict(r)
        inst["class_count"] = con.execute(
            "SELECT COUNT(*) FROM classes WHERE instructor_id=?", (inst["id"],)
        ).fetchone()[0]
        instructors.append(inst)
    return jsonify({"instructors": instructors})


# ── Admin: Categories ─────────────────────────────────────────────────────────

@app.post("/api/admin/categories")
def api_admin_create_category():
    err = require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    slug = _make_slug(name)
    con = db()
    try:
        cat_id = con.execute(
            "INSERT INTO categories (name, slug, sort_order) VALUES (?,?,?)",
            (name, slug, data.get("sort_order", 99))
        ).lastrowid
        con.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Category already exists"}), 409
    audit("categories", cat_id, "create", changed_by="admin")
    return jsonify({"ok": True, "category_id": cat_id})


# ── Admin: Discount Codes ─────────────────────────────────────────────────────

@app.get("/api/admin/discount-codes")
def api_admin_list_codes():
    err = require_admin()
    if err:
        return err
    con = db()
    rows = con.execute("SELECT * FROM discount_codes ORDER BY created_at DESC").fetchall()
    return jsonify({"codes": [dict(r) for r in rows]})


@app.post("/api/admin/discount-codes")
def api_admin_create_code():
    err = require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "Code is required"}), 400
    con = db()
    try:
        code_id = con.execute("""
            INSERT INTO discount_codes (code, description, discount_pct, discount_fixed_cents,
                valid_from, valid_until, max_uses)
            VALUES (?,?,?,?,?,?,?)
        """, (code, data.get("description"), data.get("discount_pct"),
              data.get("discount_fixed_cents"), data.get("valid_from"),
              data.get("valid_until"), data.get("max_uses"))).lastrowid
        con.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Code already exists"}), 409
    audit("discount_codes", code_id, "create", changed_by="admin")
    return jsonify({"ok": True, "code_id": code_id})


@app.put("/api/admin/discount-codes/<int:code_id>")
def api_admin_update_code(code_id):
    err = require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    con = db()
    row = con.execute("SELECT * FROM discount_codes WHERE id=?", (code_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    updates, params = [], []
    for f in ["is_active", "max_uses", "valid_until", "description"]:
        if f in data:
            updates.append(f"{f}=?")
            params.append(data[f])
    if updates:
        params.append(code_id)
        con.execute(f"UPDATE discount_codes SET {', '.join(updates)} WHERE id=?", params)
        con.commit()
    return jsonify({"ok": True})


# ── Admin: Waivers ────────────────────────────────────────────────────────────

@app.get("/api/admin/waivers")
def api_admin_waivers():
    err = require_admin()
    if err:
        return err
    con = db()
    student_id = request.args.get("student_id")
    where, params = "", []
    if student_id:
        where = "WHERE w.student_id = ?"
        params = [int(student_id)]
    rows = con.execute(f"""
        SELECT w.*, s.first_name, s.last_name, s.email,
               c.title as class_title
        FROM waivers w
        JOIN students s ON w.student_id = s.id
        LEFT JOIN registrations r ON w.registration_id = r.id
        LEFT JOIN classes c ON r.class_id = c.id
        {where} ORDER BY w.signed_at DESC
    """, params).fetchall()
    return jsonify({"waivers": [dict(r) for r in rows]})


# ── Admin: Audit Log ──────────────────────────────────────────────────────────

@app.get("/api/admin/audit")
def api_admin_audit():
    err = require_admin()
    if err:
        return err
    con = db()
    limit = int(request.args.get("limit", 100))
    rows = con.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return jsonify({"entries": [dict(r) for r in rows]})


# ── Static files ──────────────────────────────────────────────────────────────

@app.get("/static/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(str(UPLOAD_DIR), filename)


@app.get("/")
def index():
    resp = send_from_directory(str(Path(__file__).parent), "classes.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Seed data ─────────────────────────────────────────────────────────────────

def seed():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")

    if con.execute("SELECT COUNT(*) FROM categories").fetchone()[0] > 0:
        # Seed discount code if missing
        if con.execute("SELECT COUNT(*) FROM discount_codes").fetchone()[0] == 0:
            con.execute("""
                INSERT INTO discount_codes (code, description, discount_pct, is_active)
                VALUES ('PLM-MEMBER', 'Past Lives member discount', 10, 1)
            """)
            con.commit()
        con.close()
        return

    # ── Categories with images ──
    SQ = "https://images.squarespace-cdn.com/content/v1/634b3a44e68be653dc708619"
    categories = [
        ("Art Framing", "art-framing", 1, None),
        ("Ceramics", "ceramics", 2, f"{SQ}/8b91ac2c-b95e-41a3-bf64-216a5ec2ac18/glazing-pottery-kilns.PNG?format=500w"),
        ("Creative Business", "creative-business", 3, None),
        ("Education", "education", 4, None),
        ("Food Independence", "food-independence", 5, None),
        ("Glass", "glass", 6, f"{SQ}/df102107-83d9-42e6-b0a5-b31cb5ee0fba/Lampworking-Station-Ez-Fire-Kiln-and-Microwave-Kiln%E2%80%94+rainbow-glass-tubing-and-grit.jpg?format=500w"),
        ("Jewelry", "jewelry", 7, None),
        ("Leather", "leather", 8, None),
        ("Metalworking", "metalworking", 9, f"{SQ}/792cc2e8-3c33-4114-81c9-70163eea532f/steel+shop+anvil+1.JPG?format=500w"),
        ("Tech", "tech", 10, None),
        ("Textiles", "textiles", 11, f"{SQ}/986e8c87-0e31-44f4-957b-2cc047f6617b/Textiles-CNC-Embroidery.jpg?format=500w"),
        ("Visual Arts", "visual-arts", 12, None),
        ("Woodworking", "woodworking", 13, f"{SQ}/96667f86-dfd4-4981-8eaa-92cc0557b70c/artisan-woodshop.JPG?format=500w"),
        ("Writing", "writing", 14, None),
    ]
    con.executemany("INSERT INTO categories (name, slug, sort_order, image_url) VALUES (?,?,?,?)", categories)

    # ── Instructors (name, slug, email, bio, photo_url, website, social) ──
    SQ = "https://images.squarespace-cdn.com/content/v1/634b3a44e68be653dc708619"
    instructors = [
        ("Anjali", "anjali", "curlyclaybug@pastlives.space",
         "Anjali is a ceramicist, jewelry maker, and creative content producer at Past Lives Makerspace. She creates video content for PLM's social media, including the popular 'Treasure out of Trash' series. In her ceramics classes, she brings a hands-on, playful approach — guiding students through hand-building techniques to create functional, beautiful pieces they can take home.",
         f"{SQ}/8b91ac2c-b95e-41a3-bf64-216a5ec2ac18/glazing-pottery-kilns.PNG?format=500w",
         None, "@curlyclaybug"),
        ("Deenie Wallace", "deenie-wallace", "deenie@pastlives.space",
         "Deenie Wallace is a borosilicate lampwork artist with a BFA in glass arts. She specializes in torchwork — shaping hard glass with a flame to create pendants, marbles, and sculptural pieces. Her classes at Past Lives range from beginner-friendly introductions to focused technique sessions in frit layering, implosion, and sculptural forms. Every student leaves with finished pieces they made themselves.",
         f"{SQ}/df102107-83d9-42e6-b0a5-b31cb5ee0fba/Lampworking-Station-Ez-Fire-Kiln-and-Microwave-Kiln%E2%80%94+rainbow-glass-tubing-and-grit.jpg?format=500w",
         None, None),
        ("Billy Ottaviani", "billy-ottaviani", "billy@pastlives.space",
         "Billy Ottaviani is the Metal Guild Lead and Metalshop Facilities Manager at Past Lives Makerspace. A skilled blacksmith and metal fabricator, Billy brings decades of hands-on experience in forging, welding, CNC plasma cutting, and gate design. His Blacksmithing 101 classes offer flexible daytime scheduling — students work at their own pace across 9 hours of instruction, learning the fundamentals of drawing, tapering, twisting, bending, and more.",
         f"{SQ}/1a41d43d-afe8-429d-9827-0af4f76da58d/5+-+Billy+M1.jpg?format=500w",
         None, None),
        ("Glen Dahl", "glen-dahl", "glen@pastlives.space",
         "Glen Dahl is a co-founder of Past Lives Makerspace and an experienced blacksmith who teaches evening and weekend forging classes. A serial entrepreneur who co-founded Dave's Killer Bread, Glen brings the same hands-on mentality to the forge — his three-session Blacksmithing 101 courses walk students through the complete foundations of the craft. He also teaches Intro to Knife Making, where students forge a full-tang knife from raw steel.",
         f"{SQ}/25f5bba2-3816-402f-b6fa-0b4bd1cd9b58/couples-blacksmithing.JPG?format=500w",
         None, None),
        ("Jenelle Giordano", "jenelle-giordano", "artframingguild@pastlives.space",
         "Jenelle Giordano is the Art Framing Guild Lead and Board Secretary at Past Lives Makerspace. A professional framer and upholsterer with her own shop, Jenelle's Shadowbox Framing classes guide students through the art of 3D collage — arranging personal objects into custom-framed shadowboxes over two sessions. She brings a meticulous eye for composition and a warm teaching style that makes the craft accessible to beginners.",
         None, None, "@jeglives"),
        ("Amy Stewart", "amy-stewart", "amy@pastlives.space",
         "Amy Stewart is a stained glass artist and illustrator known for her campy, vibrant aesthetic. Her work ranges from sun catchers to large architectural panels, with pieces like 'The Balancing Act' and 'Dagger Fairy' showcasing her distinctive style. At Past Lives, Amy teaches Introduction to Stained Glass — intimate sessions where students learn copper foil technique and leave with a finished sun catcher.",
         f"{SQ}/21a14f5c-bd3d-4c59-8420-9f558f198e87/members-stained-glass.jpeg?format=500w",
         "https://amyintheaetherart.com", "@amyintheaetherart"),
        ("Kate Reed", "kate-reed", "visualartsguild@pastlives.space",
         "Kate Reed is the Visual Arts Guild Lead at Past Lives Makerspace. A multi-disciplinary artist working across drawing, graphic design, ceramics, and stained glass, Kate organizes PLM's monthly Figure Drawing sessions — welcoming artists of all levels to draw from a live model in a supportive studio environment. She designed Past Lives' branded postcards and has coordinated installations including the Winter Lights Festival.",
         None, None, None),
    ]
    for name, slug, email, bio, photo_url, website, social in instructors:
        token = secrets.token_urlsafe(32)
        con.execute(
            "INSERT INTO instructors (name, slug, email, bio, photo_url, website, social_handle, login_token) VALUES (?,?,?,?,?,?,?,?)",
            (name, slug, email, bio, photo_url, website, social, token))
    con.commit()

    def cat_id(slug):
        return con.execute("SELECT id FROM categories WHERE slug=?", (slug,)).fetchone()[0]
    def inst_id(name):
        return con.execute("SELECT id FROM instructors WHERE name=?", (name,)).fetchone()[0]

    # ── Classes ──
    classes_data = [
        ("Make a Sushi Set with Anjali", "make-sushi-set-anjali-mar26", "ceramics", "Anjali",
         "Hand-build a ceramic sushi serving set (plate, sauce bowl, chopstick rest). Glaze selection after build; instructor fires and notifies for pickup.",
         None, "All clay and glazes included", 5500, 6, "fixed", 0, [("2026-03-27", "17:30", "19:30", 120)]),
        ("Boro Basics: Intro to Lampworking with Deenie", "boro-basics-deenie-mar28", "glass", "Deenie Wallace",
         "Intro to borosilicate hard-glass torchworking. Covers flame control, shaping, color application, tool use. Create 3 pendants.",
         "No prior glass experience required", "All tools, materials, safety equipment provided", 32500, 2, "fixed", 0,
         [("2026-03-28", "15:00", "20:00", 300)]),
        ("Blacksmithing 101 with Billy - April Daytimes", "blacksmithing-101-billy-apr", "metalworking", "Billy Ottaviani",
         "Basic forging techniques: drawing, tapering, twisting, bending, scrolling, upsetting, splitting, cutting, punching, drifting holes, rounding square stock.",
         None, None, 37500, 4, "flexible", 0, []),
        ("Blacksmithing 101 with Glen - April Fri Evenings", "blacksmithing-101-glen-apr-fri-eve", "metalworking", "Glen Dahl",
         "Three-part intro to forging.", None, None, 37500, 4, "fixed", 0,
         [("2026-04-03", "18:00", "21:00", 180), ("2026-04-17", "18:00", "21:00", 180), ("2026-04-24", "18:00", "21:00", 180)]),
        ("Blacksmithing 101 with Glen - April Sat Mornings", "blacksmithing-101-glen-apr-sat-am", "metalworking", "Glen Dahl",
         "Three-part intro to forging. Saturday morning sessions.", None, None, 37500, 4, "fixed", 0,
         [("2026-04-04", "08:00", "11:00", 180), ("2026-04-18", "08:00", "11:00", 180), ("2026-04-25", "08:00", "11:00", 180)]),
        ("Boro Basics: Intro to Lampworking with Deenie", "boro-basics-deenie-apr4", "glass", "Deenie Wallace",
         "Intro to borosilicate hard-glass torchworking. Create 3 pendants.",
         "No prior glass experience required", "All provided", 32500, 2, "fixed", 0,
         [("2026-04-04", "15:00", "20:00", 300)]),
        ("Mesmerizing Marbles: Borosilicate Lampwork with Deenie", "marbles-deenie-apr7", "glass", "Deenie Wallace",
         "Opaque and vortex marble making with fire.", None, None, 16500, 2, "fixed", 0,
         [("2026-04-07", "18:00", "20:30", 150)]),
        ("Blacksmithing 101 with Glen - April Tue Evenings", "blacksmithing-101-glen-apr-tue-eve", "metalworking", "Glen Dahl",
         "Three-part intro to forging. Tuesday evening sessions.", None, None, 37500, 3, "fixed", 0,
         [("2026-04-07", "18:00", "21:00", 180), ("2026-04-14", "18:00", "21:00", 180), ("2026-04-21", "18:00", "21:00", 180)]),
        ("Hearts! Borosilicate Lampwork Pendants with Deenie", "hearts-deenie-apr9", "glass", "Deenie Wallace",
         "Heart pendant making with borosilicate glass.", None, None, 16500, 2, "fixed", 0,
         [("2026-04-09", "11:00", "13:30", 150)]),
        ("Shadowbox Framing Class with Jenelle", "shadowbox-jenelle-apr", "art-framing", "Jenelle Giordano",
         "3D collage and framing. Two finished shadowbox projects. Max frame: 12\"x18\"x1.5\".",
         "Must email photos/dimensions of items to artframingguild@pastlives.space beforehand",
         "Premium backings and frames included. Bring up to 5 objects.", 30000, 6, "fixed", 0,
         [("2026-04-11", "10:00", "12:00", 120), ("2026-04-18", "10:00", "12:00", 120)]),
        ("Boro Basics: Intro to Lampworking with Deenie", "boro-basics-deenie-apr11", "glass", "Deenie Wallace",
         "Intro to borosilicate hard-glass torchworking. Create 3 pendants.",
         "No prior glass experience required", "All provided", 32500, 2, "fixed", 0,
         [("2026-04-11", "15:00", "20:00", 300)]),
        ("Introduction to Stained Glass with Amy (Private)", "stained-glass-amy-apr19", "glass", "Amy Stewart",
         "Copper foil stained glass sun catcher. Covers cutting, grinding, foiling, soldering.",
         "Solo must be 18+; ages 15+ with guardian", "All tools and materials included. Closed-toe shoes mandatory.", 40000, 2, "fixed", 0,
         [("2026-04-19", "10:00", "15:00", 300)]),
        ("April Figure Drawing Session", "figure-drawing-apr26", "visual-arts", "Kate Reed",
         "Live figure drawing with model. Pose sequence: 5x1min, 1x5min, 1x10min, 1x15min, 2x30min. No photography; tipping model encouraged.",
         "All levels welcome. Non-members sign waiver.", "Basic paper/pencils available. Tables, chairs, easels first-come.", 1200, 12, "fixed", 1,
         [("2026-04-26", "14:00", "16:00", 120)]),
        ("Fun With Frit: Lampwork Pendants with Deenie", "frit-deenie-apr28", "glass", "Deenie Wallace",
         "Layering color and implosion techniques with borosilicate glass.", None, None, 16500, 2, "fixed", 0,
         [("2026-04-28", "18:00", "20:30", 150)]),
        ("Mushroom Magic: Borosilicate Lampwork with Deenie", "mushroom-deenie-apr30", "glass", "Deenie Wallace",
         "Mushroom pendant making, great for beginners.", None, None, 16500, 2, "fixed", 0,
         [("2026-04-30", "18:00", "20:30", 150)]),
        ("Blacksmithing 101 with Billy - May Daytimes", "blacksmithing-101-billy-may", "metalworking", "Billy Ottaviani",
         "Basic forging techniques. 9 hours self-paced within the month.", None, None, 37500, 4, "flexible", 0, []),
        ("Blacksmithing 101 with Glen - May Fri Evenings", "blacksmithing-101-glen-may-fri", "metalworking", "Glen Dahl",
         "Three-part intro to forging.", None, None, 37500, 4, "fixed", 0,
         [("2026-05-01", "18:00", "21:00", 180), ("2026-05-08", "18:00", "21:00", 180), ("2026-05-15", "18:00", "21:00", 180)]),
        ("Blacksmithing 101 with Glen - May Sat Mornings", "blacksmithing-101-glen-may-sat", "metalworking", "Glen Dahl",
         "Three-part intro to forging. Saturday morning sessions.", None, None, 37500, 4, "fixed", 0,
         [("2026-05-02", "08:00", "11:00", 180), ("2026-05-09", "08:00", "11:00", 180), ("2026-05-16", "08:00", "11:00", 180)]),
        ("Intro to Knife Making with Glen", "knife-making-glen-may", "metalworking", "Glen Dahl",
         "Full tang knife from 1084 steel blank. Forging blade, heat treating, grinding bevels, handle assembly.",
         "Blacksmithing 101 or equivalent experience", None, 50000, 1, "fixed", 0,
         [("2026-05-05", "18:00", "21:00", 180), ("2026-05-07", "18:00", "21:00", 180),
          ("2026-05-12", "18:00", "21:00", 180), ("2026-05-14", "18:00", "21:00", 180)]),
        ("Shadowbox Framing Class with Jenelle", "shadowbox-jenelle-may", "art-framing", "Jenelle Giordano",
         "3D collage and framing. Two finished shadowbox projects.",
         "Must email photos/dimensions beforehand", "Premium backings and frames included.", 30000, 6, "fixed", 0,
         [("2026-05-09", "10:00", "12:00", 120), ("2026-05-16", "10:00", "12:00", 120)]),
        ("Introduction to Stained Glass with Amy (Private)", "stained-glass-amy-may15", "glass", "Amy Stewart",
         "Copper foil stained glass sun catcher.",
         "Solo must be 18+; ages 15+ with guardian", "All tools and materials included.", 40000, 2, "fixed", 0,
         [("2026-05-15", "10:00", "15:00", 300)]),
        ("Blacksmithing 101 with Billy - June Daytimes", "blacksmithing-101-billy-jun", "metalworking", "Billy Ottaviani",
         "Basic forging techniques. 9 hours self-paced within the month.", None, None, 37500, 4, "flexible", 0, []),
        ("Blacksmithing 101 with Glen - June Tue Evenings", "blacksmithing-101-glen-jun-tue", "metalworking", "Glen Dahl",
         "Three-part intro to forging.", None, None, 37500, 4, "fixed", 0,
         [("2026-06-02", "18:00", "21:00", 180), ("2026-06-09", "18:00", "21:00", 180), ("2026-06-16", "18:00", "21:00", 180)]),
        ("Blacksmithing 101 with Glen - June Fri Evenings", "blacksmithing-101-glen-jun-fri", "metalworking", "Glen Dahl",
         "Three-part intro to forging.", None, None, 37500, 4, "fixed", 0,
         [("2026-06-05", "18:00", "21:00", 180), ("2026-06-12", "18:00", "21:00", 180), ("2026-06-19", "18:00", "21:00", 180)]),
        ("Blacksmithing 101 with Glen - June Sat Mornings", "blacksmithing-101-glen-jun-sat", "metalworking", "Glen Dahl",
         "Three-part intro to forging.", None, None, 37500, 4, "fixed", 0,
         [("2026-06-06", "08:00", "11:00", 180), ("2026-06-13", "08:00", "11:00", 180), ("2026-06-20", "08:00", "11:00", 180)]),
        ("Shadowbox Framing Class with Jenelle", "shadowbox-jenelle-jun", "art-framing", "Jenelle Giordano",
         "3D collage and framing. Two finished shadowbox projects.",
         "Must email photos/dimensions beforehand", "Premium backings and frames included.", 30000, 6, "fixed", 0,
         [("2026-06-06", "10:00", "12:00", 120), ("2026-06-13", "10:00", "12:00", 120)]),
    ]

    for title, slug, cat_slug, inst_name, desc, prereqs, materials, price, cap, model, needs_release, sessions in classes_data:
        cid = cat_id(cat_slug)
        iid = inst_id(inst_name)
        is_private = 1 if "Private" in title else 0
        class_id = con.execute("""
            INSERT INTO classes
                (title, slug, category_id, instructor_id, description, prerequisites,
                 materials_included, price_cents, capacity, scheduling_model,
                 is_private, requires_model_release, status, created_by, approved_by, published_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'published',?,'admin',datetime('now'))
        """, (title, slug, cid, iid, desc, prereqs, materials, price, cap, model,
              is_private, needs_release, iid)).lastrowid
        for i, (sdate, stime, etime, dur) in enumerate(sessions):
            con.execute("""
                INSERT INTO sessions (class_id, session_date, start_time, end_time, duration_minutes, sort_order)
                VALUES (?,?,?,?,?,?)
            """, (class_id, sdate, stime, etime, dur, i))

    # ── Seed discount codes ──
    con.execute("""
        INSERT INTO discount_codes (code, description, discount_pct, is_active)
        VALUES ('PLM-MEMBER', 'Past Lives member discount — 10% off all classes', 10, 1)
    """)

    con.commit()
    con.close()
    print(f"  Seeded {len(classes_data)} classes, {len(instructors)} instructors, {len(categories)} categories")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    migrate()
    seed()
    print("\n  PLM Classes")
    print(f"  → http://localhost:5001")
    print(f"  → Admin: http://localhost:5001/?admin={ADMIN_TOKEN}")
    if STRIPE_ENABLED:
        print("  → Stripe: ENABLED")
    else:
        print("  → Stripe: disabled (set STRIPE_SECRET_KEY to enable)")
    print()
    app.run(debug=False, port=5001, host="127.0.0.1")
