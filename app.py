from flask import Flask, request, jsonify, session, send_from_directory, render_template, redirect, url_for, Response, stream_with_context
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from pymongo import MongoClient
import os
import uuid
import json
import time
import csv
from datetime import datetime

app = Flask(__name__)
app.secret_key = "iip-dev-secret-change-in-production-2026"

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "your_db")
client = MongoClient(MONGO_URI) if MONGO_URI else None
db = client[MONGO_DB_NAME] if client is not None else None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {"pdf"}

# ─────────────────────────────────────────────
# In-memory storage (replace with DB in production)
# ─────────────────────────────────────────────

DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_PASSWORD = "1234"

USERS = {}
IDEAS = []
FUNDING_TXNS = []
ACTIVITY_LOG = []
INVESTOR_WALLETS = {}
REVIEWS = []
ANALYTICS_SERIES = []


def _csv_rows(filename):
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _normalize_role(role: str) -> str:
    role = (role or "").strip().lower()
    if role == "mentor":
        return "faculty"
    return role or "student"


def refresh_users_from_csv():
    global USERS

    existing_by_id = {u.get("id"): u for u in USERS.values() if u.get("id")}
    csv_user_ids = set()
    refreshed = {}

    for row in _csv_rows("users.csv"):
        uid = f"u{row.get('user_id')}"
        csv_user_ids.add(uid)
        email = (row.get("email") or "").strip().lower()
        if not email:
            continue

        existing = existing_by_id.get(uid) or USERS.get(email) or {}
        refreshed[email] = {
            "id": uid,
            "name": (row.get("name") or "").strip(),
            "password": existing.get("password", DEFAULT_PASSWORD),
            "role": _normalize_role(row.get("role")),
            "email": email,
            "department": (row.get("department") or "").strip(),
            "created_at": ((row.get("created_at") or "").strip() + "T00:00:00Z") if row.get("created_at") else "2026-01-01T00:00:00Z",
            "active": existing.get("active", True),
        }

    for email, user in USERS.items():
        if user.get("id") not in csv_user_ids:
            refreshed[email] = user

    USERS = refreshed


def _stage_from_status(status: str) -> str:
    status = (status or "").strip().lower()
    mapping = {
        "pending": "Under Review",
        "approved": "Approved",
        "rejected": "Rejected",
        "funded": "Funded",
        "mentored": "Mentored",
    }
    return mapping.get(status, status.title() if status else "Under Review")


def load_data_from_csv():
    global USERS, IDEAS, FUNDING_TXNS, INVESTOR_WALLETS, REVIEWS, ANALYTICS_SERIES

    users = {}
    user_by_id = {}
    for row in _csv_rows("users.csv"):
        role = _normalize_role(row.get("role"))
        email = (row.get("email") or "").strip().lower()
        user = {
            "id": f"u{row.get('user_id')}",
            "name": (row.get("name") or "").strip(),
            "password": DEFAULT_PASSWORD,
            "role": role,
            "email": email,
            "department": (row.get("department") or "").strip(),
            "created_at": ((row.get("created_at") or "").strip() + "T00:00:00Z") if row.get("created_at") else "2026-01-01T00:00:00Z",
            "active": True,
        }
        if email:
            users[email] = user
            user_by_id[str(row.get("user_id"))] = user

    feedback_by_idea = {}
    reviews = []
    for row in _csv_rows("feedback.csv"):
        mentor = user_by_id.get(str(row.get("mentor_id")))
        idea_id = f"idea-{row.get('idea_id')}"
        feedback = {
            "id": f"feedback-{row.get('feedback_id')}",
            "by": mentor["name"] if mentor else "Mentor",
            "by_id": mentor["id"] if mentor else f"u{row.get('mentor_id')}",
            "role": mentor["role"] if mentor else "faculty",
            "text": (row.get("comments") or "").strip(),
            "ts": ((row.get("date") or "").strip() + "T12:00:00Z") if row.get("date") else "2026-01-01T12:00:00Z"
        }
        feedback_by_idea.setdefault(idea_id, []).append(feedback)
        reviews.append({
            "id": f"review-{row.get('feedback_id')}",
            "idea_id": idea_id,
            "by": feedback["by"],
            "by_id": feedback["by_id"],
            "role": feedback["role"],
            "rating": int(row.get("rating") or 0),
            "title": "Mentor review",
            "text": feedback["text"],
            "ts": feedback["ts"]
        })

    funding_txns = []
    funded_by_idea = {}
    investor_wallets = {}
    for row in _csv_rows("funding.csv"):
        investor = user_by_id.get(str(row.get("investor_id")))
        idea_id = f"idea-{row.get('idea_id')}"
        amount = int(float(row.get("amount") or 0))
        status = (row.get("status") or "").strip().lower()
        if investor and investor["role"] == "investor":
            investor_wallets.setdefault(investor["id"], {"balance": 50000, "currency": "USD"})
            if status == "approved":
                investor_wallets[investor["id"]]["balance"] -= amount
        if amount > 0 and status == "approved":
            funded_by_idea[idea_id] = funded_by_idea.get(idea_id, 0) + amount
            funding_txns.append({
                "id": f"funding-{row.get('funding_id')}",
                "idea_id": idea_id,
                "idea_title": "",
                "investor_id": investor["id"] if investor else f"u{row.get('investor_id')}",
                "investor_name": investor["name"] if investor else "Investor",
                "amount": amount,
                "ts": ((row.get("date") or "").strip() + "T12:00:00Z") if row.get("date") else "2026-01-01T12:00:00Z",
                "note": (row.get("status") or "").strip().title()
            })

    ideas = []
    for row in _csv_rows("ideas.csv"):
        owner = user_by_id.get(str(row.get("user_id")))
        idea_id = f"idea-{row.get('idea_id')}"
        stage = _stage_from_status(row.get("status"))
        verified = stage in ("Approved", "Mentored", "Funded")
        ideas.append({
            "id": idea_id,
            "title": (row.get("title") or "").strip(),
            "category": (row.get("category") or "General").strip(),
            "summary": (row.get("description") or "").strip(),
            "stage": stage,
            "owner_id": owner["id"] if owner else f"u{row.get('user_id')}",
            "owner_name": owner["name"] if owner else "Student",
            "owner_email": owner["email"] if owner else "",
            "created_at": ((row.get("created_at") or "").strip() + "T12:00:00Z") if row.get("created_at") else "2026-01-01T12:00:00Z",
            "updated_at": ((row.get("created_at") or "").strip() + "T12:00:00Z") if row.get("created_at") else "2026-01-01T12:00:00Z",
            "verified": verified,
            "requested_amount": max(0, funded_by_idea.get(idea_id, 0) or (5000 if verified else 3000)),
            "funded_amount": funded_by_idea.get(idea_id, 0),
            "plan_pdf": None,
            "feedback": sorted(feedback_by_idea.get(idea_id, []), key=lambda x: x.get("ts", ""), reverse=True),
            "tags": [],
        })

    idea_title_map = {idea["id"]: idea["title"] for idea in ideas}
    for txn in funding_txns:
        txn["idea_title"] = idea_title_map.get(txn["idea_id"], "Idea")

    analytics = []
    for row in _csv_rows("analytics.csv"):
        analytics.append({
            "month": (row.get("month") or "").strip(),
            "total_ideas": int(row.get("total_ideas") or 0),
            "approved_ideas": int(row.get("approved_ideas") or 0),
            "total_funding": int(row.get("total_funding") or 0),
        })

    USERS = users
    IDEAS = ideas
    FUNDING_TXNS = sorted(funding_txns, key=lambda x: x.get("ts", ""), reverse=True)
    INVESTOR_WALLETS = investor_wallets
    REVIEWS = sorted(reviews, key=lambda x: x.get("ts", ""), reverse=True)
    ANALYTICS_SERIES = analytics


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def now_z():
    return datetime.utcnow().isoformat() + "Z"


def log_activity(type_, message, user_id=None):
    ACTIVITY_LOG.insert(0, {
        "id": str(uuid.uuid4()),
        "type": type_,
        "message": message,
        "user_id": user_id,
        "ts": now_z()
    })
    # Keep log at max 200 entries
    if len(ACTIVITY_LOG) > 200:
        ACTIVITY_LOG.pop()


load_data_from_csv()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper


def api_login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return jsonify({"ok": False, "error": "Not authenticated"}), 401
        return fn(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if "user" not in session:
                return jsonify({"ok": False, "error": "Not authenticated"}), 401
            if session["user"]["role"] not in roles:
                return jsonify({"ok": False, "error": "Forbidden – insufficient role"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco


def get_idea(idea_id):
    return next((i for i in IDEAS if i["id"] == idea_id), None)


def get_user_by_id(uid):
    return next((u for u in USERS.values() if u["id"] == uid), None)


def get_reviews_for_idea(idea_id):
    items = [r for r in REVIEWS if r["idea_id"] == idea_id]
    return sorted(items, key=lambda r: r.get("ts", ""), reverse=True)


def get_auth_presets():
    presets = {}
    role_order = ("student", "faculty", "investor", "admin")
    for role in role_order:
        match = next((u for u in USERS.values() if u.get("role") == role), None)
        if match:
            presets[role] = {
                "email": match.get("email", ""),
                "name": match.get("name", ""),
                "password": DEFAULT_PASSWORD,
            }
    return presets


def _review_metrics(idea_id: str) -> dict:
    reviews = get_reviews_for_idea(idea_id)
    count = len(reviews)
    avg = round(sum(int(r.get("rating") or 0) for r in reviews) / count, 1) if count else 0
    by_role = {}
    for review in reviews:
        role = review.get("role") or "unknown"
        by_role[role] = by_role.get(role, 0) + 1
    return {
        "reviews": reviews,
        "review_count": count,
        "average_rating": avg,
        "review_breakdown": by_role,
        "latest_review_at": reviews[0]["ts"] if reviews else None,
    }


def _role_idea_scope(role: str, user_id: str):
    items = IDEAS[:]
    if role == "student":
        items = [i for i in items if i["owner_id"] == user_id]
    elif role == "investor":
        items = [i for i in items if i.get("verified") is True]
    return items


def _build_realtime_snapshot(role: str, user_id: str) -> dict:
    items = _role_idea_scope(role, user_id)
    for idea in items:
        _normalize_idea_state(idea)

    total_requested = sum(int(i.get("requested_amount") or 0) for i in items)
    total_funded = sum(int(i.get("funded_amount") or 0) for i in items)
    pending_reviews = len([i for i in items if "review" in str(i.get("stage") or "").lower()])
    total_feedback = sum(len(i.get("feedback") or []) for i in items)
    scoped_reviews = [r for r in REVIEWS if any(i["id"] == r["idea_id"] for i in items)]

    return {
        "generated_at": now_z(),
        "role": role,
        "idea_count": len(items),
        "pending_review_count": pending_reviews,
        "total_requested": total_requested,
        "total_funded": total_funded,
        "total_feedback": total_feedback,
        "total_reviews": len(scoped_reviews),
        "activity_count": len(ACTIVITY_LOG),
        "recent_activity": ACTIVITY_LOG[:5],
    }




# ─────────────────────────────────────────────
# Presentation / computed fields
# ─────────────────────────────────────────────

def _normalize_idea_state(idea: dict) -> None:
    requested = int(idea.get("requested_amount") or 0)
    funded = int(idea.get("funded_amount") or 0)
    if requested > 0 and funded >= requested:
        idea["stage"] = "Funded"
    if bool(idea.get("verified")) and idea.get("stage") == "Under Review":
        idea["stage"] = "Approved"


def _funding_metrics(idea: dict) -> dict:
    requested = int(idea.get("requested_amount") or 0)
    funded = int(idea.get("funded_amount") or 0)
    pct = 0
    if requested > 0:
        pct = max(0, min(100, int(round((funded / requested) * 100))))
    remaining = max(0, requested - funded)
    return {
        "funding_progress_pct": pct,
        "funding_remaining": remaining,
        "funding_complete": (requested > 0 and funded >= requested),
    }


def _journey_step(idea: dict) -> int:
    s = str(idea.get("stage") or "").lower()
    if "fund" in s:
        return 5
    has_feedback = bool(idea.get("feedback")) and len(idea.get("feedback", [])) > 0
    if has_feedback and ("approv" in s or "review" in s or "under" in s or "mentor" in s):
        return 4
    if "mentor" in s:
        return 4
    if "approv" in s:
        return 3
    if "review" in s or "under" in s:
        return 2
    return 1


def present_idea(idea: dict) -> dict:
    _normalize_idea_state(idea)
    out = dict(idea)
    out.update(_funding_metrics(idea))
    out["journey_step"] = _journey_step(idea)
    out.update(_review_metrics(idea["id"]))
    # camelCase aliases for frontend
    out["requestedAmount"] = int(out.get("requested_amount") or 0)
    out["fundedAmount"] = int(out.get("funded_amount") or 0)
    out["fundingProgressPct"] = int(out.get("funding_progress_pct") or 0)
    out["fundingRemaining"] = int(out.get("funding_remaining") or 0)
    out["fundingComplete"] = bool(out.get("funding_complete"))
    out["reviewCount"] = int(out.get("review_count") or 0)
    out["averageRating"] = float(out.get("average_rating") or 0)
    out["ownerId"] = out.get("owner_id")
    out["ownerName"] = out.get("owner_name")
    out["createdAt"] = out.get("created_at")
    out["planPdf"] = out.get("plan_pdf")
    return out


# ─────────────────────────────────────────────
# Page Routes
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/users")
def users():
    if db is None:
        return "MongoDB is not configured. Set MONGO_URI in the environment.", 503
    all_users = list(db.users.find())
    return str(all_users)


@app.route("/dashboard")
@login_required
def dashboard_router():
    role = session["user"]["role"]
    routes = {
        "student": "student_dashboard",
        "faculty": "faculty_dashboard",
        "investor": "investor_dashboard",
        "admin": "admin_dashboard",
    }
    return redirect(url_for(routes.get(role, "index")))


@app.route("/dashboard/student")
@login_required
def student_dashboard():
    return render_template("studentdashboard.html")


@app.route("/dashboard/faculty")
@login_required
def faculty_dashboard():
    return render_template("facultydashboard.html")


@app.route("/dashboard/investor")
@login_required
def investor_dashboard():
    return render_template("investordashboard.html")


@app.route("/dashboard/admin")
@login_required
def admin_dashboard():
    return render_template("admindashboard.html")


# ─────────────────────────────────────────────
# Auth APIs
# ─────────────────────────────────────────────

@app.route("/api/me")
def api_me():
    return jsonify({"ok": True, "user": session.get("user")})


@app.route("/api/auth/presets")
def api_auth_presets():
    refresh_users_from_csv()
    return jsonify({"ok": True, "presets": get_auth_presets()})


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    refresh_users_from_csv()
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = USERS.get(email)
    if not user:
        return jsonify({"ok": False, "error": "Invalid email or password"}), 401
    if user["password"] != password:
        return jsonify({"ok": False, "error": "Invalid email or password"}), 401
    if not user.get("active", True):
        return jsonify({"ok": False, "error": "Account is deactivated"}), 403

    session["user"] = {
        "id": user["id"],
        "name": user["name"],
        "email": email,
        "role": user["role"]
    }
    log_activity("auth", f"{user['role']} logged in: {email}", user["id"])
    return jsonify({"ok": True, "user": session["user"], "redirect": "/dashboard"})


@app.route("/api/auth/register", methods=["POST"])
def api_register():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    name = (data.get("name") or "").strip()
    password = data.get("password") or ""
    role = _normalize_role(data.get("role") or "student")

    if not email or not name or not password:
        return jsonify({"ok": False, "error": "Email, name, and password are required"}), 400
    if role not in ("student", "faculty", "investor", "admin"):
        return jsonify({"ok": False, "error": "Invalid role. Choose student, faculty, investor, or admin"}), 400
    if email in USERS:
        return jsonify({"ok": False, "error": "Email already registered"}), 409
    if len(password) < 4:
        return jsonify({"ok": False, "error": "Password must be at least 4 characters"}), 400

    uid = "u" + str(uuid.uuid4())[:8]
    USERS[email] = {
        "id": uid,
        "name": name,
        "password": password,
        "role": role,
        "email": email,
        "created_at": now_z(),
        "active": True
    }
    log_activity("auth", f"New {role} registered: {email}", uid)
    return jsonify({"ok": True, "message": "Registration successful. Please log in."})


@app.route("/api/auth/logout", methods=["POST"])
@api_login_required
def api_logout():
    u = session["user"]
    log_activity("auth", f"{u['role']} logged out: {u['email']}", u["id"])
    session.pop("user", None)
    return jsonify({"ok": True, "redirect": "/"})


# ─────────────────────────────────────────────
# Idea APIs
# ─────────────────────────────────────────────

@app.route("/api/ideas", methods=["GET"])
@api_login_required
def api_list_ideas():
    role = session["user"]["role"]
    user_id = session["user"]["id"]

    q = (request.args.get("q") or "").strip().lower()
    category = (request.args.get("category") or "").strip().lower()
    stage = (request.args.get("stage") or "").strip().lower()
    verified = request.args.get("verified")

    items = IDEAS[:]

    # Role-based visibility
    if role == "student":
        items = [i for i in items if i["owner_id"] == user_id]
    elif role == "investor":
        items = [i for i in items if i.get("verified") is True]

    # Normalize before filtering
    for i in items:
        _normalize_idea_state(i)

    if q:
        items = [i for i in items if
                 q in i.get("title", "").lower() or
                 q in i.get("summary", "").lower() or
                 q in i.get("category", "").lower()]
    if category:
        items = [i for i in items if category in (i.get("category") or "").lower()]
    if stage:
        items = [i for i in items if stage in (i.get("stage") or "").lower()]
    if verified in ("true", "false"):
        v = (verified == "true")
        items = [i for i in items if bool(i.get("verified")) == v]

    return jsonify({"ok": True, "ideas": [present_idea(i) for i in items]})


@app.route("/api/ideas/<idea_id>", methods=["GET"])
@api_login_required
def api_get_idea(idea_id):
    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404
    role = session["user"]["role"]
    user_id = session["user"]["id"]
    if role == "student" and idea["owner_id"] != user_id:
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    if role == "investor" and not idea.get("verified"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    return jsonify({"ok": True, "idea": present_idea(idea)})


@app.route("/api/ideas", methods=["POST"])
@role_required("student")
def api_create_idea():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    category = (data.get("category") or "").strip()
    summary = (data.get("summary") or "").strip()
    requested_amount = max(0, int(data.get("requested_amount") or 0))
    tags = data.get("tags") or []

    if not title:
        return jsonify({"ok": False, "error": "Title is required"}), 400
    if not summary:
        return jsonify({"ok": False, "error": "Summary is required"}), 400
    if len(title) > 120:
        return jsonify({"ok": False, "error": "Title too long (max 120 chars)"}), 400

    u = session["user"]
    idea = {
        "id": "idea-" + str(uuid.uuid4())[:8],
        "title": title,
        "category": category or "General",
        "summary": summary,
        "stage": "Under Review",
        "owner_id": u["id"],
        "owner_name": u["name"],
        "owner_email": u["email"],
        "created_at": now_z(),
        "updated_at": now_z(),
        "verified": False,
        "requested_amount": requested_amount,
        "funded_amount": 0,
        "plan_pdf": None,
        "feedback": [],
        "tags": tags if isinstance(tags, list) else [],
    }
    IDEAS.insert(0, idea)
    log_activity("idea", f"Idea submitted: '{idea['title']}' ({idea['id']}) by {u['email']}", u["id"])
    return jsonify({"ok": True, "idea": present_idea(idea)})


@app.route("/api/ideas/<idea_id>", methods=["PATCH"])
@api_login_required
def api_update_idea(idea_id):
    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404

    u = session["user"]
    # Students can only edit their own ideas; admin/faculty can edit any
    if u["role"] == "student" and idea["owner_id"] != u["id"]:
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    data = request.get_json(force=True)
    editable = ["title", "category", "summary", "requested_amount", "tags"]
    for field in editable:
        if field in data:
            idea[field] = data[field]
    idea["updated_at"] = now_z()

    log_activity("idea", f"Idea updated: {idea_id} by {u['email']}", u["id"])
    return jsonify({"ok": True, "idea": present_idea(idea)})


@app.route("/api/ideas/<idea_id>", methods=["DELETE"])
@api_login_required
def api_delete_idea(idea_id):
    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404

    u = session["user"]
    if u["role"] == "student" and idea["owner_id"] != u["id"]:
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    if u["role"] not in ("student", "admin"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    IDEAS.remove(idea)
    log_activity("idea", f"Idea deleted: {idea_id} by {u['email']}", u["id"])
    return jsonify({"ok": True})


@app.route("/api/ideas/<idea_id>/upload_plan", methods=["POST"])
@role_required("student")
def api_upload_plan(idea_id):
    u = session["user"]
    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404
    if idea["owner_id"] != u["id"]:
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "No file selected"}), 400
    if not allowed_file(f.filename):
        return jsonify({"ok": False, "error": "Only PDF files are allowed"}), 400

    # Remove old file if exists
    if idea.get("plan_pdf"):
        old_path = os.path.join(UPLOAD_DIR, idea["plan_pdf"])
        if os.path.exists(old_path):
            os.remove(old_path)

    filename = secure_filename(f"{idea_id}-{uuid.uuid4().hex}.pdf")
    f.save(os.path.join(UPLOAD_DIR, filename))

    idea["plan_pdf"] = filename
    idea["updated_at"] = now_z()
    log_activity("upload", f"Business plan uploaded for {idea_id} by {u['email']}", u["id"])
    return jsonify({"ok": True, "filename": filename, "idea": present_idea(idea)})


@app.route("/uploads/<path:filename>")
@api_login_required
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


# ─────────────────────────────────────────────
# Feedback / Mentoring
# ─────────────────────────────────────────────

@app.route("/api/ideas/<idea_id>/feedback", methods=["POST"])
@role_required("faculty", "admin")
def api_add_feedback(idea_id):
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Feedback text is required"}), 400
    if len(text) > 2000:
        return jsonify({"ok": False, "error": "Feedback too long (max 2000 chars)"}), 400

    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404

    u = session["user"]
    fb = {
        "id": str(uuid.uuid4()),
        "by": u["name"],
        "by_id": u["id"],
        "role": u["role"],
        "text": text,
        "ts": now_z()
    }
    idea["feedback"].insert(0, fb)
    idea["updated_at"] = now_z()

    # Auto-advance stage
    if idea.get("stage") == "Approved":
        idea["stage"] = "Mentored"

    log_activity("feedback", f"Feedback added to '{idea['title']}' by {u['email']}", u["id"])
    return jsonify({"ok": True, "feedback": fb, "idea": present_idea(idea)})


@app.route("/api/ideas/<idea_id>/feedback/<feedback_id>", methods=["DELETE"])
@role_required("faculty", "admin")
def api_delete_feedback(idea_id, feedback_id):
    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404

    u = session["user"]
    fb = next((f for f in idea["feedback"] if f["id"] == feedback_id), None)
    if not fb:
        return jsonify({"ok": False, "error": "Feedback not found"}), 404
    if u["role"] != "admin" and fb.get("by_id") != u["id"]:
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    idea["feedback"].remove(fb)
    return jsonify({"ok": True})


@app.route("/api/ideas/<idea_id>/reviews", methods=["GET"])
@api_login_required
def api_list_reviews(idea_id):
    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404

    role = session["user"]["role"]
    user_id = session["user"]["id"]
    if role == "student" and idea["owner_id"] != user_id:
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    if role == "investor" and not idea.get("verified"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    return jsonify({
        "ok": True,
        "reviews": get_reviews_for_idea(idea_id),
        "metrics": _review_metrics(idea_id)
    })


@app.route("/api/ideas/<idea_id>/reviews", methods=["POST"])
@role_required("faculty", "investor", "admin")
def api_add_review(idea_id):
    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404

    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    text = (data.get("text") or "").strip()
    try:
        rating = int(data.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0

    if not text:
        return jsonify({"ok": False, "error": "Review text is required"}), 400
    if rating < 1 or rating > 5:
        return jsonify({"ok": False, "error": "Rating must be between 1 and 5"}), 400
    if len(title) > 120:
        return jsonify({"ok": False, "error": "Review title too long (max 120 chars)"}), 400
    if len(text) > 2000:
        return jsonify({"ok": False, "error": "Review text too long (max 2000 chars)"}), 400

    u = session["user"]
    review = {
        "id": str(uuid.uuid4()),
        "idea_id": idea_id,
        "by": u["name"],
        "by_id": u["id"],
        "role": u["role"],
        "rating": rating,
        "title": title or f"{u['role'].title()} review",
        "text": text,
        "ts": now_z()
    }
    REVIEWS.insert(0, review)

    if u["role"] in ("faculty", "admin"):
        fb = {
            "id": f"feedback-{review['id']}",
            "review_id": review["id"],
            "by": review["by"],
            "by_id": review["by_id"],
            "role": review["role"],
            "text": review["text"],
            "ts": review["ts"]
        }
        idea.setdefault("feedback", []).insert(0, fb)
        review["feedback_id"] = fb["id"]
        if idea.get("stage") == "Approved":
            idea["stage"] = "Mentored"

    idea["updated_at"] = now_z()

    log_activity("review", f"Review added to '{idea['title']}' by {u['email']}", u["id"])
    return jsonify({
        "ok": True,
        "review": review,
        "metrics": _review_metrics(idea_id),
        "idea": present_idea(idea)
    })


@app.route("/api/ideas/<idea_id>/reviews/<review_id>", methods=["DELETE"])
@role_required("faculty", "investor", "admin")
def api_delete_review(idea_id, review_id):
    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404

    u = session["user"]
    review = next((r for r in REVIEWS if r["id"] == review_id and r["idea_id"] == idea_id), None)
    if not review:
        return jsonify({"ok": False, "error": "Review not found"}), 404
    if u["role"] != "admin" and review.get("by_id") != u["id"]:
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    REVIEWS.remove(review)
    idea["feedback"] = [
        fb for fb in idea.get("feedback", [])
        if fb.get("review_id") != review_id and fb.get("id") != review.get("feedback_id")
    ]
    log_activity("review", f"Review removed from '{idea['title']}' by {u['email']}", u["id"])
    return jsonify({"ok": True, "metrics": _review_metrics(idea_id)})


# ─────────────────────────────────────────────
# Admin: Approvals & Management
# ─────────────────────────────────────────────

@app.route("/api/ideas/<idea_id>/approve", methods=["POST"])
@role_required("admin")
def api_approve_idea(idea_id):
    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404

    idea["verified"] = True
    if idea["stage"] in ("Under Review", "Submitted"):
        idea["stage"] = "Approved"
    idea["updated_at"] = now_z()

    u = session["user"]
    log_activity("admin", f"Idea approved: '{idea['title']}' ({idea_id})", u["id"])
    return jsonify({"ok": True, "idea": present_idea(idea)})


@app.route("/api/ideas/<idea_id>/reject", methods=["POST"])
@role_required("admin")
def api_reject_idea(idea_id):
    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404

    data = request.get_json(force=True) or {}
    reason = (data.get("reason") or "").strip()

    idea["verified"] = False
    idea["stage"] = "Rejected"
    idea["updated_at"] = now_z()
    if reason:
        idea["rejection_reason"] = reason

    u = session["user"]
    log_activity("admin", f"Idea rejected: '{idea['title']}' ({idea_id})", u["id"])
    return jsonify({"ok": True, "idea": present_idea(idea)})


@app.route("/api/ideas/<idea_id>/verify", methods=["POST"])
@role_required("admin")
def api_verify_idea(idea_id):
    data = request.get_json(force=True)
    verified = bool(data.get("verified", True))
    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404

    idea["verified"] = verified
    if verified and idea["stage"] == "Under Review":
        idea["stage"] = "Approved"
    idea["updated_at"] = now_z()

    u = session["user"]
    log_activity("admin", f"Idea verification set to {verified}: {idea_id}", u["id"])
    return jsonify({"ok": True, "idea": present_idea(idea)})


@app.route("/api/admin/users", methods=["GET"])
@role_required("admin")
def api_list_users():
    q = (request.args.get("q") or "").strip().lower()
    role_filter = _normalize_role(request.args.get("role") or "")

    users = []
    for email, u in USERS.items():
        if q and q not in email.lower() and q not in u["name"].lower():
            continue
        if role_filter and u["role"] != role_filter:
            continue
        users.append({
            "id": u["id"],
            "name": u["name"],
            "email": email,
            "role": u["role"],
            "active": u.get("active", True),
            "created_at": u.get("created_at", "")
        })
    return jsonify({"ok": True, "users": users, "total": len(users)})


@app.route("/api/admin/users/<user_id>/toggle", methods=["POST"])
@role_required("admin")
def api_toggle_user(user_id):
    if user_id == session["user"]["id"]:
        return jsonify({"ok": False, "error": "Cannot deactivate yourself"}), 400

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 404

    # Find in USERS dict
    for email, u in USERS.items():
        if u["id"] == user_id:
            u["active"] = not u.get("active", True)
            log_activity("admin", f"User {'activated' if u['active'] else 'deactivated'}: {email}", session["user"]["id"])
            return jsonify({"ok": True, "active": u["active"]})

    return jsonify({"ok": False, "error": "User not found"}), 404


@app.route("/api/admin/overview", methods=["GET"])
@role_required("admin")
def api_admin_overview():
    total_users = len(USERS)
    total_ideas = len(IDEAS)
    verified_ideas = len([i for i in IDEAS if i.get("verified")])
    pending_ideas = len([i for i in IDEAS if (i.get("stage") or "").lower() in ("under review", "submitted")])
    funded_ideas = len([i for i in IDEAS if "fund" in (i.get("stage") or "").lower() or
                        int(i.get("funded_amount") or 0) >= int(i.get("requested_amount") or 1) > 0])
    total_funding = sum(t["amount"] for t in FUNDING_TXNS)

    roles_count = {}
    for u in USERS.values():
        roles_count[u["role"]] = roles_count.get(u["role"], 0) + 1

    categories = {}
    for i in IDEAS:
        cat = i.get("category") or "General"
        categories[cat] = categories.get(cat, 0) + 1

    return jsonify({
        "ok": True,
        "metrics": {
            "total_users": total_users,
            "total_ideas": total_ideas,
            "verified_ideas": verified_ideas,
            "pending_ideas": pending_ideas,
            "funded_ideas": funded_ideas,
            "total_funding": total_funding,
            "total_reviews": len(REVIEWS),
            "roles": roles_count,
            "categories": categories,
        },
        "activity": ACTIVITY_LOG[:30]
    })


# ─────────────────────────────────────────────
# Funding
# ─────────────────────────────────────────────

@app.route("/api/investor/wallet", methods=["GET"])
@role_required("investor")
def api_wallet():
    u = session["user"]
    wallet = INVESTOR_WALLETS.get(u["id"], {"balance": 25000, "currency": "USD"})
    return jsonify({"ok": True, "wallet": wallet})


@app.route("/api/ideas/<idea_id>/fund", methods=["POST"])
@role_required("investor")
def api_fund_idea(idea_id):
    data = request.get_json(force=True)
    try:
        amount = int(data.get("amount") or 0)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid amount"}), 400

    if amount <= 0:
        return jsonify({"ok": False, "error": "Amount must be greater than 0"}), 400

    idea = get_idea(idea_id)
    if not idea:
        return jsonify({"ok": False, "error": "Idea not found"}), 404
    if not idea.get("verified"):
        return jsonify({"ok": False, "error": "Idea must be verified/approved before funding"}), 400

    u = session["user"]
    # Check wallet balance
    wallet = INVESTOR_WALLETS.setdefault(u["id"], {"balance": 25000, "currency": "USD"})
    if amount > wallet["balance"]:
        return jsonify({"ok": False, "error": f"Insufficient wallet balance. Available: {wallet['balance']}"}), 400

    # Apply funding
    idea["funded_amount"] = int(idea.get("funded_amount") or 0) + amount
    wallet["balance"] -= amount
    _normalize_idea_state(idea)
    idea["updated_at"] = now_z()

    note = (data.get("note") or "").strip()
    txn = {
        "id": str(uuid.uuid4()),
        "idea_id": idea_id,
        "idea_title": idea["title"],
        "investor_id": u["id"],
        "investor_name": u["name"],
        "amount": amount,
        "ts": now_z(),
        "note": note
    }
    FUNDING_TXNS.insert(0, txn)

    log_activity("funding", f"Funding: {amount} USD to '{idea['title']}' by {u['email']}", u["id"])
    return jsonify({
        "ok": True,
        "idea": present_idea(idea),
        "txn": txn,
        "wallet": wallet
    })


@app.route("/api/investor/transactions", methods=["GET"])
@role_required("investor")
def api_investor_txns():
    u = session["user"]
    txns = [t for t in FUNDING_TXNS if t["investor_id"] == u["id"]]
    return jsonify({"ok": True, "transactions": txns})


@app.route("/api/admin/transactions", methods=["GET"])
@role_required("admin")
def api_all_txns():
    return jsonify({"ok": True, "transactions": FUNDING_TXNS})


@app.route("/api/realtime/snapshot", methods=["GET"])
@api_login_required
def api_realtime_snapshot():
    user = session["user"]
    return jsonify({
        "ok": True,
        "snapshot": _build_realtime_snapshot(user["role"], user["id"])
    })


@app.route("/api/realtime/stream", methods=["GET"])
@api_login_required
def api_realtime_stream():
    user = dict(session["user"])

    @stream_with_context
    def generate():
        for _ in range(30):
            payload = {
                "type": "snapshot",
                "snapshot": _build_realtime_snapshot(user["role"], user["id"])
            }
            yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(5)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no"
    })


# ─────────────────────────────────────────────
# Analytics
# ─────────────────────────────────────────────

@app.route("/api/analytics/summary", methods=["GET"])
@api_login_required
def api_analytics_summary():
    role = session["user"]["role"]
    user_id = session["user"]["id"]

    if role == "student":
        my_ideas = [i for i in IDEAS if i["owner_id"] == user_id]
        total_req = sum(int(i.get("requested_amount") or 0) for i in my_ideas)
        total_funded = sum(int(i.get("funded_amount") or 0) for i in my_ideas)
        stages = {}
        for i in my_ideas:
            s = i.get("stage", "Unknown")
            stages[s] = stages.get(s, 0) + 1
        return jsonify({
            "ok": True,
            "total_ideas": len(my_ideas),
            "total_requested": total_req,
            "total_funded": total_funded,
            "total_reviews": len([r for r in REVIEWS if r["idea_id"] in {i["id"] for i in my_ideas}]),
            "stages": stages,
            "series": ANALYTICS_SERIES
        })

    # Faculty/Admin get global analytics
    stages = {}
    categories = {}
    for i in IDEAS:
        s = i.get("stage", "Unknown")
        stages[s] = stages.get(s, 0) + 1
        c = i.get("category", "General")
        categories[c] = categories.get(c, 0) + 1

    return jsonify({
        "ok": True,
        "total_ideas": len(IDEAS),
        "total_funding": sum(t["amount"] for t in FUNDING_TXNS),
        "total_reviews": len(REVIEWS),
        "stages": stages,
        "categories": categories,
        "series": ANALYTICS_SERIES
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
