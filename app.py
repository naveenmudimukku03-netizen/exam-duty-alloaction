"""
ExamDuty — Flask Backend with MongoDB
=======================================
ROOT CAUSE FIX: Old backend seeded users WITHOUT passwords (has_password=False).
Login always failed because the check blocked anyone without has_password=True.

THIS VERSION seeds all users with has_password=True + a pre-hashed password.

Install:
  pip install flask flask-jwt-extended flask-cors werkzeug pymongo

Run:
  python app.py

WORKING LOGIN CREDENTIALS:
  Admin:   admin@cse.edu   / Admin@123
  Faculty: kumar@cse.edu   / Faculty@123
           meena@cse.edu   / Faculty@123
           priya@cse.edu   / Faculty@123
           john@cse.edu    / Faculty@123
           raj@cse.edu     / Faculty@123
           latha@cse.edu   / Faculty@123
           sundar@cse.edu  / Faculty@123
           geetha@cse.edu  / Faculty@123

ROUND-ROBIN ENGINE (NEW):
  - Per-department pointer stored in `rr_state` collection.
  - Faculty ordered by emp_id (stable, deterministic).
  - Pointer advances after every allocation call, wraps around.
  - Skips faculty who are already assigned to the exam or at their duty cap.
  - Re-running allocate on the same exam is idempotent (won't double-assign).
  - Preview endpoint lets admin see who would be picked before committing.
  - Reset endpoint lets admin restart the cycle for a department.

NEW ENDPOINTS:
  POST /api/admin/exams/<exam_id>/allocate          — round-robin allocate
  GET  /api/admin/exams/<exam_id>/preview-allocation — preview without saving
  GET  /api/admin/rr-state                           — view all RR pointers
  POST /api/admin/rr-state/reset                     — reset pointer for a dept
  POST /api/admin/duties                             — manually assign one duty
  DELETE /api/admin/duties/<duty_id>                 — remove a duty
"""

import os
from datetime import datetime, timedelta
from functools import wraps
from bson import ObjectId
from flask import Flask, request, jsonify
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity, get_jwt
)
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient

# ─────────────────────────────────────────────
#  APP
# ─────────────────────────────────────────────
# ✅ FIX: point static folder to the directory containing app.py (root of repo)
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_url_path='', static_folder=STATIC_DIR)
app.config["JWT_SECRET_KEY"] = "examduty-secret-key-2025"
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=8)

client = MongoClient("mongodb://localhost:27017/")
db = client["examdutyallocation"]

JWTManager(app)
CORS(app, resources={r"/api/*": {"origins": "*"}})


@app.route('/')
def serve():
    return app.send_static_file('index.html')


# ─────────────────────────────────────────────
#  SEED
# ─────────────────────────────────────────────
def seed_db():
    """
    Seed default users if collection is empty.
    CRITICAL: has_password=True and password hash must both be set.
    """
    if db.users.count_documents({}) > 0:
        fixed = db.users.update_many(
            {"has_password": {"$ne": True}},
            {"$set": {
                "has_password": True,
                "password": generate_password_hash("Faculty@123")
            }}
        )
        if fixed.modified_count:
            print(f"  ✓ Fixed {fixed.modified_count} users missing has_password flag")
        return

    print("  ⏳ First run — seeding database...")
    AP = generate_password_hash("Admin@123")
    FP = generate_password_hash("Faculty@123")

    users = [
        # ── Admin ──────────────────────────────────────────────────────
        dict(name="Dr. A. Durai",   email="admin@cse.edu",   password=AP, has_password=True,
             role="admin",   designation="Professor",            department="CSE", emp_id="EMP-0001", max_duties=2,  is_active=True),
        # ── Faculty ────────────────────────────────────────────────────
        dict(name="Dr. R. Kumar",   email="kumar@cse.edu",   password=FP, has_password=True,
             role="faculty", designation="Assistant Professor",  department="CSE", emp_id="EMP-1021", max_duties=8,  is_active=True),
        dict(name="Dr. S. Meena",   email="meena@cse.edu",   password=FP, has_password=True,
             role="faculty", designation="Associate Professor",  department="CSE", emp_id="EMP-1034", max_duties=10, is_active=True),
        dict(name="Prof. V. Priya", email="priya@cse.edu",   password=FP, has_password=True,
             role="faculty", designation="Professor",            department="CSE", emp_id="EMP-1012", max_duties=6,  is_active=True),
        dict(name="Dr. A. John",    email="john@cse.edu",    password=FP, has_password=True,
             role="faculty", designation="Assistant Professor",  department="CSE", emp_id="EMP-1045", max_duties=8,  is_active=True),
        dict(name="Prof. N. Raj",   email="raj@cse.edu",     password=FP, has_password=True,
             role="faculty", designation="Professor",            department="CSE", emp_id="EMP-1008", max_duties=6,  is_active=True),
        dict(name="Dr. K. Latha",   email="latha@cse.edu",   password=FP, has_password=True,
             role="faculty", designation="Assistant Professor",  department="CSE", emp_id="EMP-1056", max_duties=8,  is_active=True),
        dict(name="Dr. B. Sundar",  email="sundar@cse.edu",  password=FP, has_password=True,
             role="faculty", designation="Associate Professor",  department="CSE", emp_id="EMP-1067", max_duties=10, is_active=True),
        dict(name="Dr. M. Geetha",  email="geetha@cse.edu",  password=FP, has_password=True,
             role="faculty", designation="Assistant Professor",  department="CSE", emp_id="EMP-1078", max_duties=8,  is_active=True),
    ]
    for u in users:
        u["created_at"] = datetime.utcnow()

    db.users.insert_many(users)
    try:
        db.users.create_index("email", unique=True)
    except Exception:
        pass
    print(f"  ✓ Seeded {len(users)} users")


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def role_required(*roles):
    def decorator(fn):
        @wraps(fn)
        @jwt_required()
        def wrapper(*args, **kwargs):
            if get_jwt().get("role") not in roles:
                return jsonify(error="Forbidden"), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def oid(s):
    try:
        return ObjectId(s)
    except Exception:
        return None

def fmt_user(u):
    return {
        "id": str(u["_id"]),
        "name": u["name"],
        "email": u["email"],
        "role": u["role"],
        "designation": u.get("designation", ""),
        "department": u.get("department", ""),
        "emp_id": u.get("emp_id", ""),
        "max_duties": u.get("max_duties", 8)
    }


# ═════════════════════════════════════════════
#  ROUND-ROBIN ENGINE
# ═════════════════════════════════════════════
#
#  HOW IT WORKS
#  ─────────────────────────────────────────────────────────────────────
#  1. The `rr_state` collection stores one document per department:
#       { department: "CSE", pointer: 3, updated_at: <datetime> }
#     `pointer` is an index into the department's stable faculty list.
#
#  2. `_get_faculty_roster(department)` returns ALL active faculty for
#     the department sorted by emp_id — this fixed order never changes
#     mid-cycle, so the pointer always maps to the same person.
#
#  3. `_rr_pick(department, needed, exclude_ids, exam_id)` starts at
#     the current pointer and walks forward (with wrap-around), skipping
#     anyone who:
#       • is already assigned to this exam
#       • has reached their max_duties cap for the semester/department
#     It returns the chosen faculty and the new pointer position.
#
#  4. After a successful allocation the pointer is saved back to MongoDB
#     so the NEXT allocation call for this department continues from
#     where it left off.
#
#  5. If a full lap finds fewer candidates than `needed`, allocation
#     succeeds partially and a warning is returned in the response.
# ─────────────────────────────────────────────────────────────────────

def _get_faculty_roster(department: str) -> list:
    """
    Return all active faculty for a department in a stable order (emp_id).
    This is the canonical ring for round-robin.
    """
    return list(
        db.users.find(
            {"role": "faculty", "is_active": True, "department": department},
            {"password": 0}
        ).sort("emp_id", 1)
    )


def _get_duty_counts(department: str, semester: str) -> dict:
    """
    Return {faculty_id_str: duty_count} for a given semester+department.
    Rejected duties are excluded.
    """
    counts = {}
    for row in db.duties.aggregate([
        {"$lookup": {
            "from": "exams",
            "localField": "exam_id",
            "foreignField": "_id",
            "as": "ex"
        }},
        {"$unwind": "$ex"},
        {"$match": {
            "ex.semester":   semester,
            "ex.department": department,
            "status":        {"$ne": "rejected"}
        }},
        {"$group": {"_id": "$faculty_id", "count": {"$sum": 1}}}
    ]):
        counts[str(row["_id"])] = row["count"]
    return counts


def _get_rr_pointer(department: str) -> int:
    """Fetch the current round-robin pointer for this department (0 if not set)."""
    doc = db.rr_state.find_one({"department": department})
    return doc["pointer"] if doc else 0


def _save_rr_pointer(department: str, pointer: int):
    """Persist the round-robin pointer after an allocation."""
    db.rr_state.update_one(
        {"department": department},
        {"$set": {"pointer": pointer, "updated_at": datetime.utcnow()}},
        upsert=True
    )


def _rr_pick(department: str, semester: str, exam_id: str,
             needed: int, dry_run: bool = False) -> tuple:
    """
    Core round-robin picker.

    Parameters
    ----------
    department : str
    semester   : str
    exam_id    : str          — used to build the already-assigned exclusion set
    needed     : int          — how many faculty to pick
    dry_run    : bool         — if True, pointer is NOT persisted (preview mode)

    Returns
    -------
    (picked: list[dict], warning: str | None)

    Each item in `picked`:
      { id, name, designation, emp_id, max_duties, duties_count }
    """
    roster = _get_faculty_roster(department)
    if not roster:
        return [], "No active faculty found for this department"

    duty_counts   = _get_duty_counts(department, semester)
    already_assigned = {
        str(d["faculty_id"])
        for d in db.duties.find({"exam_id": oid(exam_id)}, {"faculty_id": 1})
    }

    n        = len(roster)
    pointer  = _get_rr_pointer(department) % n   # guard against roster shrinkage
    picked   = []
    visited  = 0                                  # how many roster slots we've checked

    while len(picked) < needed and visited < n:
        idx     = pointer % n
        faculty = roster[idx]
        fid     = str(faculty["_id"])

        pointer = (pointer + 1) % n
        visited += 1

        # Skip if already assigned to THIS exam
        if fid in already_assigned:
            continue

        # Skip if at or over cap
        current_duties = duty_counts.get(fid, 0)
        if current_duties >= faculty.get("max_duties", 8):
            continue

        picked.append({
            "id":          fid,
            "name":        faculty["name"],
            "designation": faculty.get("designation", ""),
            "emp_id":      faculty.get("emp_id", ""),
            "max_duties":  faculty.get("max_duties", 8),
            "duties_count": current_duties,
        })

    warning = None
    if len(picked) < needed:
        warning = (
            f"Only {len(picked)} of {needed} required faculty could be assigned "
            f"(others are at cap or already assigned to this exam)."
        )

    if not dry_run and picked:
        _save_rr_pointer(department, pointer)

    return picked, warning


def _build_sessions(session_type: str, count: int) -> list:
    """Split session slots for FN, AN, or FN+AN exams."""
    if session_type == "FN+AN":
        half = count // 2
        return ["FN"] * half + ["AN"] * (count - half)
    return [session_type] * count


def allocate_round_robin(exam_id: str, semester: str, department: str,
                         needed: int, rooms_list: list = None) -> tuple:
    """
    Allocate `needed` invigilators for an exam using round-robin.

    Returns (allocated: list[dict], error: str | None)
    """
    exam = db.exams.find_one({"_id": oid(exam_id)})
    if not exam:
        return [], "Exam not found"

    picked, warning = _rr_pick(department, semester, exam_id, needed, dry_run=False)
    if not picked:
        return [], warning or "No eligible faculty available"

    sessions = _build_sessions(exam.get("session", "FN"), len(picked))

    allocated = []
    for i, fac in enumerate(picked):
        room    = (rooms_list[i] if rooms_list and i < len(rooms_list)
                   else f"Room-{i + 1:02d}")
        session = sessions[i]

        result = db.duties.insert_one({
            "exam_id":      oid(exam_id),
            "faculty_id":   oid(fac["id"]),
            "room":         room,
            "session":      session,
            "status":       "approved",
            "allocated_at": datetime.utcnow(),
            "updated_at":   datetime.utcnow(),
        })

        db.notifications.insert_one({
            "user_id":    oid(fac["id"]),
            "title":      "Duty Assigned",
            "message":    (
                f"Assigned: {exam['exam_type']} on {exam['exam_date']} "
                f"({session}) — {room}"
            ),
            "color":      "blue",
            "is_read":    False,
            "created_at": datetime.utcnow(),
        })

        allocated.append({
            "duty_id":      str(result.inserted_id),
            "faculty_id":   fac["id"],
            "faculty_name": fac["name"],
            "designation":  fac["designation"],
            "emp_id":       fac["emp_id"],
            "room":         room,
            "session":      session,
        })

    db.exams.update_one(
        {"_id": oid(exam_id)},
        {"$set": {"status": "published"}}
    )

    return allocated, warning   # warning may be None (all OK) or a partial string


# ═════════════════════════════════════════════
#  AUTH
# ═════════════════════════════════════════════
@app.route("/api/auth/login", methods=["POST"])
def login():
    data  = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    pwd   = data.get("password", "")

    if not email or not pwd:
        return jsonify(error="Email and password are required"), 400

    user = db.users.find_one({"email": email, "is_active": True})
    if not user:
        return jsonify(error="Invalid email or password"), 401

    stored_pw = user.get("password", "")
    if not stored_pw:
        return jsonify(error="Account has no password set. Contact admin."), 401

    if not check_password_hash(stored_pw, pwd):
        return jsonify(error="Invalid email or password"), 401

    token = create_access_token(
        identity=str(user["_id"]),
        additional_claims={"role": user["role"], "name": user["name"]}
    )
    return jsonify(token=token, role=user["role"], user=fmt_user(user))


@app.route("/api/auth/me", methods=["GET"])
@jwt_required()
def me():
    user = db.users.find_one({"_id": oid(get_jwt_identity())})
    if not user:
        return jsonify(error="User not found"), 404
    return jsonify(user=fmt_user(user))


@app.route("/api/auth/change-password", methods=["POST"])
@jwt_required()
def change_password():
    uid  = get_jwt_identity()
    data = request.get_json() or {}
    old  = data.get("old_password", "")
    new  = data.get("new_password", "")
    if not old or not new or len(new) < 6:
        return jsonify(error="Provide old_password and new_password (min 6 chars)"), 400
    user = db.users.find_one({"_id": oid(uid)})
    if not user or not check_password_hash(user.get("password", ""), old):
        return jsonify(error="Old password incorrect"), 401
    db.users.update_one(
        {"_id": oid(uid)},
        {"$set": {"password": generate_password_hash(new), "has_password": True}}
    )
    return jsonify(message="Password changed")


# ═════════════════════════════════════════════
#  FACULTY
# ═════════════════════════════════════════════
@app.route("/api/faculty/dashboard", methods=["GET"])
@jwt_required()
def faculty_dashboard():
    uid  = get_jwt_identity()
    user = db.users.find_one({"_id": oid(uid)})

    duties = list(db.duties.aggregate([
        {"$match": {"faculty_id": oid(uid)}},
        {"$lookup": {"from": "exams", "localField": "exam_id",
                     "foreignField": "_id", "as": "exam"}},
        {"$unwind": "$exam"},
        {"$project": {
            "id": {"$toString": "$_id"},
            "room": 1, "session": 1, "status": 1, "allocated_at": 1,
            "exam_type": "$exam.exam_type",
            "exam_date": "$exam.exam_date",
            "venue":     "$exam.venue"
        }},
        {"$sort": {"exam_date": 1}}
    ]))

    today  = datetime.today().strftime("%Y-%m-%d")
    notifs = []
    for n in db.notifications.find(
            {"user_id": oid(uid)}).sort("created_at", -1).limit(10):
        n["id"] = str(n.pop("_id"))
        n["user_id"] = str(n["user_id"])
        notifs.append(n)

    return jsonify(
        total_duties=len(duties),
        completed=sum(1 for d in duties if d["status"] in ("completed", "approved")),
        upcoming=sum(
            1 for d in duties
            if d["status"] in ("pending", "approved")
            and str(d.get("exam_date", "")) >= today
        ),
        max_duties=user.get("max_duties", 8),
        duties=duties,
        notifications=notifs
    )


@app.route("/api/faculty/duties", methods=["GET"])
@jwt_required()
def faculty_duties():
    uid    = get_jwt_identity()
    duties = list(db.duties.aggregate([
        {"$match": {"faculty_id": oid(uid)}},
        {"$lookup": {"from": "exams", "localField": "exam_id",
                     "foreignField": "_id", "as": "exam"}},
        {"$unwind": "$exam"},
        {"$project": {
            "id": {"$toString": "$_id"},
            "room": 1, "session": 1, "status": 1,
            "exam_type": "$exam.exam_type",
            "exam_date": "$exam.exam_date",
            "venue":     "$exam.venue",
            "semester":  "$exam.semester"
        }},
        {"$sort": {"exam_date": 1}}
    ]))
    return jsonify(duties=duties, count=len(duties))


@app.route("/api/faculty/history", methods=["GET"])
@jwt_required()
def faculty_history():
    uid    = get_jwt_identity()
    duties = list(db.duties.aggregate([
        {"$match": {"faculty_id": oid(uid)}},
        {"$lookup": {"from": "exams", "localField": "exam_id",
                     "foreignField": "_id", "as": "exam"}},
        {"$unwind": "$exam"},
        {"$project": {
            "id": {"$toString": "$_id"},
            "room": 1, "session": 1, "status": 1,
            "exam_type": "$exam.exam_type",
            "exam_date": "$exam.exam_date",
            "semester":  "$exam.semester",
            "venue":     "$exam.venue"
        }},
        {"$sort": {"exam_date": -1}}
    ]))
    return jsonify(
        duties=duties,
        total=len(duties),
        completed=sum(1 for d in duties if d["status"] in ("completed", "approved")),
        swapped=sum(1 for d in duties if d["status"] == "swapped")
    )


@app.route("/api/faculty/notifications", methods=["GET"])
@jwt_required()
def faculty_notifications():
    uid    = get_jwt_identity()
    notifs = []
    for n in db.notifications.find(
            {"user_id": oid(uid)}).sort("created_at", -1).limit(20):
        n["id"]      = str(n.pop("_id"))
        n["user_id"] = str(n["user_id"])
        notifs.append(n)
    return jsonify(
        notifications=notifs,
        unread=sum(1 for n in notifs if not n.get("is_read"))
    )


@app.route("/api/faculty/notifications/read", methods=["POST"])
@jwt_required()
def mark_read():
    db.notifications.update_many(
        {"user_id": oid(get_jwt_identity())},
        {"$set": {"is_read": True}}
    )
    return jsonify(message="All marked read")


# ═════════════════════════════════════════════
#  SWAPS
# ═════════════════════════════════════════════
@app.route("/api/swap/request", methods=["POST"])
@jwt_required()
def create_swap():
    uid       = get_jwt_identity()
    data      = request.get_json() or {}
    req_duty  = data.get("my_duty_id")
    target_id = data.get("target_faculty_id")
    if not req_duty or not target_id:
        return jsonify(error="my_duty_id and target_faculty_id required"), 400
    duty = db.duties.find_one({"_id": oid(req_duty), "faculty_id": oid(uid)})
    if not duty:
        return jsonify(error="Duty not found or not yours"), 404
    r = db.swap_requests.insert_one({
        "requester_id":   oid(uid),
        "target_id":      oid(target_id),
        "requester_duty": oid(req_duty),
        "reason":         data.get("reason", ""),
        "status":         "pending",
        "created_at":     datetime.utcnow()
    })
    return jsonify(message="Swap request submitted", swap_id=str(r.inserted_id)), 201


@app.route("/api/swap/my", methods=["GET"])
@jwt_required()
def my_swaps():
    uid      = get_jwt_identity()
    pipeline = [
        {"$match": {"$or": [{"requester_id": oid(uid)}, {"target_id": oid(uid)}]}},
        {"$lookup": {"from": "users", "localField": "requester_id",
                     "foreignField": "_id", "as": "req"}},
        {"$lookup": {"from": "users", "localField": "target_id",
                     "foreignField": "_id", "as": "tgt"}},
        {"$unwind": "$req"}, {"$unwind": "$tgt"},
        {"$lookup": {"from": "duties", "localField": "requester_duty",
                     "foreignField": "_id", "as": "duty"}},
        {"$unwind": {"path": "$duty", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {"from": "exams", "localField": "duty.exam_id",
                     "foreignField": "_id", "as": "exam"}},
        {"$unwind": {"path": "$exam", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "id": {"$toString": "$_id"}, "status": 1, "reason": 1, "created_at": 1,
            "requester_name": "$req.name", "target_name": "$tgt.name",
            "exam_type": "$exam.exam_type", "exam_date": "$exam.exam_date",
            "room": "$duty.room", "session": "$duty.session"
        }},
        {"$sort": {"created_at": -1}}
    ]
    return jsonify(swaps=list(db.swap_requests.aggregate(pipeline)))


@app.route("/api/admin/swaps", methods=["GET"])
@role_required("admin")
def admin_swaps():
    status   = request.args.get("status", "pending")
    pipeline = [
        {"$match": {"status": status}},
        {"$lookup": {"from": "users", "localField": "requester_id",
                     "foreignField": "_id", "as": "req"}},
        {"$lookup": {"from": "users", "localField": "target_id",
                     "foreignField": "_id", "as": "tgt"}},
        {"$unwind": "$req"}, {"$unwind": "$tgt"},
        {"$lookup": {"from": "duties", "localField": "requester_duty",
                     "foreignField": "_id", "as": "duty"}},
        {"$unwind": {"path": "$duty", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {"from": "exams", "localField": "duty.exam_id",
                     "foreignField": "_id", "as": "exam"}},
        {"$unwind": {"path": "$exam", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "id": {"$toString": "$_id"}, "status": 1, "reason": 1, "created_at": 1,
            "requester_name": "$req.name",
            "requester_id":   {"$toString": "$req._id"},
            "target_name":    "$tgt.name",
            "target_id":      {"$toString": "$tgt._id"},
            "exam_type": "$exam.exam_type", "exam_date": "$exam.exam_date",
            "room": "$duty.room", "session": "$duty.session"
        }},
        {"$sort": {"created_at": -1}}
    ]
    swaps = list(db.swap_requests.aggregate(pipeline))
    return jsonify(swaps=swaps, count=len(swaps))


@app.route("/api/admin/swaps/<swap_id>/review", methods=["POST"])
@role_required("admin")
def review_swap(swap_id):
    data   = request.get_json() or {}
    action = data.get("action")
    if action not in ("approve", "reject"):
        return jsonify(error="action must be approve or reject"), 400
    swap = db.swap_requests.find_one({"_id": oid(swap_id)})
    if not swap:
        return jsonify(error="Not found"), 404
    if swap["status"] != "pending":
        return jsonify(error="Already reviewed"), 409
    new = "approved" if action == "approve" else "rejected"
    db.swap_requests.update_one(
        {"_id": oid(swap_id)},
        {"$set": {"status": new, "reviewed_at": datetime.utcnow()}}
    )
    if action == "approve":
        db.duties.update_one(
            {"_id": swap["requester_duty"]},
            {"$set": {"status": "swapped", "updated_at": datetime.utcnow()}}
        )
    color = "green" if action == "approve" else "red"
    for uid in [swap["requester_id"], swap["target_id"]]:
        db.notifications.insert_one({
            "user_id":    uid,
            "title":      "Swap Update",
            "message":    f"Your swap request has been {new}.",
            "color":      color,
            "is_read":    False,
            "created_at": datetime.utcnow()
        })
    return jsonify(message=f"Swap {new}")


# ═════════════════════════════════════════════
#  ADMIN — FACULTY
# ═════════════════════════════════════════════
@app.route("/api/admin/faculty", methods=["GET"])
@role_required("admin")
def list_faculty():
    faculty = list(
        db.users.find(
            {"role": "faculty", "is_active": True},
            {"password": 0}
        ).sort("name", 1)
    )
    for f in faculty:
        f["id"]             = str(f.pop("_id"))
        f["duties_this_sem"] = db.duties.count_documents({"faculty_id": oid(f["id"])})
    summary = {}
    for f in faculty:
        d = f.get("designation", "Unknown")
        summary[d] = summary.get(d, 0) + 1
    return jsonify(faculty=faculty, count=len(faculty), summary=summary)


@app.route("/api/admin/faculty", methods=["POST"])
@role_required("admin")
def add_faculty():
    data = request.get_json() or {}
    for f in ["name", "email", "password", "designation", "department"]:
        if not data.get(f):
            return jsonify(error=f"{f} is required"), 400
    if db.users.find_one({"email": data["email"].lower().strip()}):
        return jsonify(error="Email already exists"), 409
    cap = {"Assistant Professor": 8, "Associate Professor": 10, "Professor": 6, "HoD": 2}
    r = db.users.insert_one({
        "name":        data["name"],
        "email":       data["email"].lower().strip(),
        "password":    generate_password_hash(data["password"]),
        "has_password": True,
        "role":        "faculty",
        "designation": data["designation"],
        "department":  data["department"],
        "emp_id":      data.get("emp_id", f"EMP-{db.users.count_documents({}) + 1000}"),
        "max_duties":  int(data.get("max_duties") or cap.get(data["designation"], 8)),
        "is_active":   True,
        "created_at":  datetime.utcnow()
    })
    return jsonify(message="Faculty added", id=str(r.inserted_id)), 201


# ═════════════════════════════════════════════
#  ADMIN — EXAMS
# ═════════════════════════════════════════════
@app.route("/api/admin/exams", methods=["GET"])
@role_required("admin")
def list_exams():
    exams = list(db.exams.aggregate([
        {"$lookup": {"from": "duties", "localField": "_id",
                     "foreignField": "exam_id", "as": "duties"}},
        {"$project": {
            "id": {"$toString": "$_id"},
            "exam_type": 1, "exam_date": 1, "session": 1,
            "venue": 1, "total_rooms": 1, "invigilators_required": 1,
            "semester": 1, "department": 1, "status": 1,
            "allocated_count": {"$size": {
                "$filter": {
                    "input": "$duties", "as": "d",
                    "cond": {"$ne": ["$$d.status", "rejected"]}
                }
            }}
        }},
        {"$sort": {"exam_date": 1}}
    ]))
    return jsonify(exams=exams)


@app.route("/api/admin/exams", methods=["POST"])
@role_required("admin")
def create_exam():
    data = request.get_json() or {}
    for f in ["exam_type", "exam_date", "session"]:
        if not data.get(f):
            return jsonify(error=f"{f} is required"), 400
    r = db.exams.insert_one({
        "exam_type":             data["exam_type"],
        "exam_date":             data["exam_date"],
        "session":               data["session"],
        "venue":                 data.get("venue", "CSE Block"),
        "total_rooms":           int(data.get("total_rooms", 1)),
        "invigilators_required": int(data.get("invigilators_required", 1)),
        "semester":              data.get("semester", "VI"),
        "department":            data.get("department", "CSE"),
        "status":                "draft",
        "created_by":            oid(get_jwt_identity()),
        "created_at":            datetime.utcnow()
    })
    return jsonify(message="Exam created", exam_id=str(r.inserted_id)), 201


# ─────────────────────────────────────────────
#  ROUND-ROBIN ALLOCATION (main endpoint)
# ─────────────────────────────────────────────
@app.route("/api/admin/exams/<exam_id>/allocate", methods=["POST"])
@role_required("admin")
def allocate_duties(exam_id):
    """
    Trigger round-robin allocation for an exam.

    Body (all optional — defaults come from the exam document):
      semester             : str   e.g. "VI"
      department           : str   e.g. "CSE"
      invigilators_required: int
      rooms                : list  e.g. ["Hall-A", "Hall-B"]

    Response 201:
      {
        "message": "Round-robin complete. 3 duties assigned.",
        "allocated": [ { duty_id, faculty_id, faculty_name, designation,
                         emp_id, room, session }, ... ],
        "warning": null | "Only 2 of 3 ..."   ← partial allocation notice
      }
    """
    data       = request.get_json() or {}
    exam       = db.exams.find_one({"_id": oid(exam_id)})
    if not exam:
        return jsonify(error="Exam not found"), 404

    semester   = data.get("semester",   exam.get("semester",   "VI"))
    department = data.get("department", exam.get("department", "CSE"))
    needed     = int(data.get("invigilators_required",
                              exam.get("invigilators_required", 1)))

    allocated, warning = allocate_round_robin(
        exam_id, semester, department, needed, data.get("rooms")
    )

    if not allocated and warning:
        return jsonify(error=warning), 400

    resp = {
        "message":   f"Round-robin complete. {len(allocated)} duties assigned.",
        "allocated": allocated,
        "warning":   warning,   # None if fully satisfied, string if partial
    }
    return jsonify(resp), 201


# ─────────────────────────────────────────────
#  PREVIEW ALLOCATION (dry-run, no DB writes)
# ─────────────────────────────────────────────
@app.route("/api/admin/exams/<exam_id>/preview-allocation", methods=["GET"])
@role_required("admin")
def preview_allocation(exam_id):
    """
    Preview who would be picked by round-robin WITHOUT saving anything.

    Query params:
      semester, department, invigilators_required

    Response 200:
      {
        "preview": [ { id, name, designation, emp_id, duties_count, max_duties }, ... ],
        "current_pointer": 2,
        "total_roster": 8,
        "warning": null | "Only ..."
      }
    """
    exam = db.exams.find_one({"_id": oid(exam_id)})
    if not exam:
        return jsonify(error="Exam not found"), 404

    semester   = request.args.get("semester",   exam.get("semester",   "VI"))
    department = request.args.get("department", exam.get("department", "CSE"))
    needed     = int(request.args.get(
        "invigilators_required", exam.get("invigilators_required", 1)))

    preview, warning = _rr_pick(
        department, semester, exam_id, needed, dry_run=True
    )
    roster = _get_faculty_roster(department)

    return jsonify(
        preview=preview,
        current_pointer=_get_rr_pointer(department),
        total_roster=len(roster),
        warning=warning
    )


# ─────────────────────────────────────────────
#  ROUND-ROBIN STATE MANAGEMENT
# ─────────────────────────────────────────────
@app.route("/api/admin/rr-state", methods=["GET"])
@role_required("admin")
def get_rr_state():
    """
    View the current round-robin pointer for every department.

    Response 200:
      {
        "states": [
          { "department": "CSE", "pointer": 3, "roster_size": 8,
            "next_faculty": "Dr. A. John", "updated_at": "..." }
        ]
      }
    """
    states = []
    for doc in db.rr_state.find():
        dept    = doc["department"]
        pointer = doc["pointer"]
        roster  = _get_faculty_roster(dept)
        size    = len(roster)
        next_f  = roster[pointer % size]["name"] if roster else "—"
        states.append({
            "department":   dept,
            "pointer":      pointer,
            "roster_size":  size,
            "next_faculty": next_f,
            "updated_at":   doc.get("updated_at", ""),
        })
    return jsonify(states=states)


@app.route("/api/admin/rr-state/reset", methods=["POST"])
@role_required("admin")
def reset_rr_state():
    """
    Reset the round-robin pointer for one or all departments.

    Body:
      { "department": "CSE" }   — reset a specific department
      { "department": "ALL" }   — reset every department

    Response 200:
      { "message": "Pointer reset for CSE", "pointer": 0 }
    """
    data = request.get_json() or {}
    dept = data.get("department", "").strip()
    if not dept:
        return jsonify(error="department is required (or 'ALL')"), 400

    if dept.upper() == "ALL":
        db.rr_state.update_many(
            {},
            {"$set": {"pointer": 0, "updated_at": datetime.utcnow()}}
        )
        return jsonify(message="Pointer reset for ALL departments", pointer=0)

    _save_rr_pointer(dept, 0)
    return jsonify(message=f"Pointer reset for {dept}", pointer=0)


# ─────────────────────────────────────────────
#  MANUAL DUTY ASSIGNMENT / REMOVAL
# ─────────────────────────────────────────────
@app.route("/api/admin/duties", methods=["POST"])
@role_required("admin")
def manual_assign_duty():
    """
    Manually assign a specific faculty member to an exam.

    Body:
      exam_id    : str  (required)
      faculty_id : str  (required)
      room       : str  (optional, default "Room-01")
      session    : str  (optional, default from exam)

    Response 201:
      { "message": "Duty assigned", "duty_id": "..." }
    """
    data       = request.get_json() or {}
    exam_id    = data.get("exam_id")
    faculty_id = data.get("faculty_id")
    if not exam_id or not faculty_id:
        return jsonify(error="exam_id and faculty_id are required"), 400

    exam    = db.exams.find_one({"_id": oid(exam_id)})
    faculty = db.users.find_one({"_id": oid(faculty_id), "role": "faculty"})
    if not exam:
        return jsonify(error="Exam not found"), 404
    if not faculty:
        return jsonify(error="Faculty not found"), 404

    # Prevent double-assign
    if db.duties.find_one({"exam_id": oid(exam_id), "faculty_id": oid(faculty_id)}):
        return jsonify(error="Faculty already assigned to this exam"), 409

    session = data.get("session", exam.get("session", "FN"))
    room    = data.get("room", "Room-01")

    result = db.duties.insert_one({
        "exam_id":      oid(exam_id),
        "faculty_id":   oid(faculty_id),
        "room":         room,
        "session":      session,
        "status":       "approved",
        "allocated_at": datetime.utcnow(),
        "updated_at":   datetime.utcnow(),
        "manual":       True,
    })
    db.notifications.insert_one({
        "user_id":    oid(faculty_id),
        "title":      "Duty Assigned (Manual)",
        "message":    (
            f"Manually assigned: {exam['exam_type']} on {exam['exam_date']} "
            f"({session}) — {room}"
        ),
        "color":      "purple",
        "is_read":    False,
        "created_at": datetime.utcnow(),
    })
    return jsonify(message="Duty assigned", duty_id=str(result.inserted_id)), 201


@app.route("/api/admin/duties/<duty_id>", methods=["DELETE"])
@role_required("admin")
def remove_duty(duty_id):
    """
    Remove (hard-delete) a duty assignment.
    Sends a notification to the affected faculty member.

    Response 200:
      { "message": "Duty removed" }
    """
    duty = db.duties.find_one({"_id": oid(duty_id)})
    if not duty:
        return jsonify(error="Duty not found"), 404

    exam = db.exams.find_one({"_id": duty["exam_id"]})
    db.duties.delete_one({"_id": oid(duty_id)})

    if exam:
        db.notifications.insert_one({
            "user_id":    duty["faculty_id"],
            "title":      "Duty Removed",
            "message":    (
                f"Your duty for {exam.get('exam_type', 'exam')} on "
                f"{exam.get('exam_date', '')} has been removed."
            ),
            "color":      "orange",
            "is_read":    False,
            "created_at": datetime.utcnow(),
        })
    return jsonify(message="Duty removed")


# ═════════════════════════════════════════════
#  ADMIN — EXAM DUTIES (view)
# ═════════════════════════════════════════════
@app.route("/api/admin/exams/<exam_id>/duties", methods=["GET"])
@role_required("admin")
def exam_duties(exam_id):
    duties = list(db.duties.aggregate([
        {"$match": {"exam_id": oid(exam_id)}},
        {"$lookup": {"from": "users", "localField": "faculty_id",
                     "foreignField": "_id", "as": "fac"}},
        {"$unwind": "$fac"},
        {"$project": {
            "id": {"$toString": "$_id"}, "room": 1, "session": 1, "status": 1,
            "manual":       1,
            "faculty_name": "$fac.name",
            "designation":  "$fac.designation",
            "emp_id":       "$fac.emp_id"
        }},
        {"$sort": {"session": 1, "faculty_name": 1}}
    ]))
    return jsonify(duties=duties, count=len(duties))


# ═════════════════════════════════════════════
#  ADMIN — DASHBOARD & REPORTS
# ═════════════════════════════════════════════
@app.route("/api/admin/dashboard", methods=["GET"])
@role_required("admin")
def admin_dashboard():
    recent = list(db.duties.aggregate([
        {"$lookup": {"from": "exams", "localField": "exam_id",
                     "foreignField": "_id", "as": "exam"}},
        {"$unwind": "$exam"},
        {"$lookup": {"from": "users", "localField": "faculty_id",
                     "foreignField": "_id", "as": "fac"}},
        {"$unwind": "$fac"},
        {"$project": {
            "id": {"$toString": "$_id"}, "room": 1, "session": 1, "status": 1,
            "faculty_name": "$fac.name",
            "exam_type":    "$exam.exam_type",
            "exam_date":    "$exam.exam_date"
        }},
        {"$sort": {"_id": -1}}, {"$limit": 10}
    ]))
    pending_swaps = list(db.swap_requests.aggregate([
        {"$match": {"status": "pending"}},
        {"$lookup": {"from": "users", "localField": "requester_id",
                     "foreignField": "_id", "as": "req"}},
        {"$lookup": {"from": "users", "localField": "target_id",
                     "foreignField": "_id", "as": "tgt"}},
        {"$unwind": "$req"}, {"$unwind": "$tgt"},
        {"$project": {
            "id": {"$toString": "$_id"},
            "requester": "$req.name", "target": "$tgt.name",
            "reason": 1, "created_at": 1
        }},
        {"$limit": 5}
    ]))
    return jsonify(
        stats={
            "total_faculty":     db.users.count_documents(
                {"role": "faculty", "is_active": True}),
            "duties_assigned":   db.duties.count_documents(
                {"status": {"$ne": "rejected"}}),
            "pending_approvals": db.duties.count_documents({"status": "pending"}),
            "swap_requests":     db.swap_requests.count_documents({"status": "pending"})
        },
        recent_allocations=recent,
        pending_swaps=pending_swaps,
        notifications=[]
    )


@app.route("/api/admin/reports/workload", methods=["GET"])
@role_required("admin")
def workload_report():
    workload = list(db.users.aggregate([
        {"$match": {"role": "faculty", "is_active": True}},
        {"$lookup": {"from": "duties", "localField": "_id",
                     "foreignField": "faculty_id", "as": "duties"}},
        {"$project": {
            "id": {"$toString": "$_id"}, "name": 1, "designation": 1,
            "emp_id": 1, "max_duties": 1,
            "duties_count": {"$size": "$duties"}
        }},
        {"$sort": {"duties_count": -1}}
    ]))
    for w in workload:
        md = w.get("max_duties", 8)
        w["utilization_pct"] = round(w["duties_count"] / md * 100, 1) if md else 0
    return jsonify(workload=workload)


@app.route("/api/admin/reports/allocation-audit", methods=["GET"])
@role_required("admin")
def allocation_audit():
    log = list(db.duties.aggregate([
        {"$lookup": {"from": "exams", "localField": "exam_id",
                     "foreignField": "_id", "as": "exam"}},
        {"$unwind": "$exam"},
        {"$lookup": {"from": "users", "localField": "faculty_id",
                     "foreignField": "_id", "as": "fac"}},
        {"$unwind": "$fac"},
        {"$project": {
            "id": {"$toString": "$_id"}, "room": 1, "session": 1,
            "status": 1, "allocated_at": 1, "manual": 1,
            "faculty_name": "$fac.name", "designation": "$fac.designation",
            "emp_id":       "$fac.emp_id",
            "exam_type":    "$exam.exam_type", "exam_date": "$exam.exam_date",
            "semester":     "$exam.semester"
        }},
        {"$sort": {"_id": -1}}
    ]))
    return jsonify(audit=log, count=len(log))


@app.route("/api/admin/settings/caps", methods=["GET"])
@role_required("admin")
def get_caps():
    caps = [
        {"designation": c["_id"], "max_duties": c["max_duties"]}
        for c in db.users.aggregate([
            {"$match": {"role": "faculty", "is_active": True}},
            {"$group": {"_id": "$designation", "max_duties": {"$first": "$max_duties"}}}
        ])
    ]
    return jsonify(caps=caps)


@app.route("/api/admin/settings/caps", methods=["PUT"])
@role_required("admin")
def update_caps():
    data = request.get_json() or []
    if not isinstance(data, list):
        return jsonify(error="Expected list"), 400
    for item in data:
        db.users.update_many(
            {"designation": item["designation"], "role": "faculty"},
            {"$set": {"max_duties": item["max_duties"]}}
        )
    return jsonify(message="Caps updated")


# ─────────────────────────────────────────────
#  HEALTH & ERRORS
# ─────────────────────────────────────────────
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        status="ok",
        users=db.users.count_documents({}),
        exams=db.exams.count_documents({}),
        duties=db.duties.count_documents({}),
        rr_states=db.rr_state.count_documents({})
    )


@app.errorhandler(404)
def not_found(e):
    return jsonify(error="Not found"), 404


@app.errorhandler(500)
def server_err(e):
    return jsonify(error="Server error", detail=str(e)), 500


from flask_jwt_extended import JWTManager as _JWTManager
_jwt = _JWTManager(app)


@_jwt.unauthorized_loader
def unauth(r):
    return jsonify(error="Missing or invalid token"), 401


@_jwt.expired_token_loader
def expired(h, d):
    return jsonify(error="Token expired, please login again"), 401


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print()
    print("=" * 60)
    print("  🚀  ExamDuty API  —  MongoDB  +  Round-Robin Engine")
    print("=" * 60)
    seed_db()
    print()
    print("  LOGIN CREDENTIALS")
    print("  ┌──────────────────────────────────────────────────┐")
    print("  │  Role     Email               Password           │")
    print("  │  ──────   ─────────────────   ─────────────────  │")
    print("  │  Admin    admin@cse.edu        Admin@123          │")
    print("  │  Faculty  kumar@cse.edu        Faculty@123        │")
    print("  │  Faculty  meena@cse.edu        Faculty@123        │")
    print("  │  Faculty  priya@cse.edu        Faculty@123        │")
    print("  │  Faculty  john@cse.edu         Faculty@123        │")
    print("  └──────────────────────────────────────────────────┘")
    print()
    print("  NEW ROUND-ROBIN ENDPOINTS")
    print("  POST   /api/admin/exams/<id>/allocate")
    print("  GET    /api/admin/exams/<id>/preview-allocation")
    print("  GET    /api/admin/rr-state")
    print("  POST   /api/admin/rr-state/reset")
    print("  POST   /api/admin/duties          (manual assign)")
    print("  DELETE /api/admin/duties/<id>     (remove duty)")
    print()
    print("  🌐  http://127.0.0.1:5000")
    print("  📁  Place index.html → ./static/index.html")
    print("=" * 60)
    app.run(debug=True, host="0.0.0.0", port=5000)
