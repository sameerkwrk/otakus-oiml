from fastapi import FastAPI, HTTPException, Depends, Response, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from decimal import Decimal, InvalidOperation
from typing import List, Generator, Optional, Literal
import sqlite3
import os
import shutil
from app.mpe_engine import MetrologyEngine, MetrologyValidationError, MPE_RULE_VERSION
from app.auth import (
    TokenUser,
    VALID_ROLES,
    create_access_token,
    get_current_user,
    hash_password,
    require_role,
    verify_password,
)

DATABASE = "nawi_system.db"
UPLOAD_DIR = "static/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="NAWI Metrology Platform")

# =====================================================================
# CLEAN ERROR HANDLERS
# =====================================================================
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    messages = []
    for err in exc.errors():
        msg = err.get("msg", "")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        messages.append(msg)
    
    clean_msg = "; ".join(messages) if messages else "Invalid input data."
    return JSONResponse(status_code=422, content={"detail": clean_msg})

@app.exception_handler(MetrologyValidationError)
async def metrology_validation_exception_handler(request: Request, exc: MetrologyValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})

# =====================================================================
# CORS & DB Setup
# =====================================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('TESTER', 'APPROVER', 'ADMIN')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS instruments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manufacturer TEXT NOT NULL,
                model TEXT NOT NULL,
                serial_number TEXT UNIQUE NOT NULL,
                capacity TEXT NOT NULL,
                unit TEXT NOT NULL,
                d TEXT NOT NULL,
                e TEXT NOT NULL,
                accuracy_class TEXT NOT NULL,
                n TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id INTEGER NOT NULL,
                technician_name TEXT NOT NULL,
                technician_user_id INTEGER,
                test_location TEXT,
                reference_equipment TEXT,
                status TEXT DEFAULT 'DRAFT',
                overall_result TEXT DEFAULT 'PENDING',
                approved_by TEXT,
                approver_user_id INTEGER,
                rejection_reason TEXT,
                rule_version TEXT NOT NULL,
                parent_report_id INTEGER,
                revision_number INTEGER DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                approved_at DATETIME,
                FOREIGN KEY(instrument_id) REFERENCES instruments(id),
                FOREIGN KEY(parent_report_id) REFERENCES test_reports(id),
                FOREIGN KEY(technician_user_id) REFERENCES users(id),
                FOREIGN KEY(approver_user_id) REFERENCES users(id)
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL,
                test_type TEXT NOT NULL,
                reference_load TEXT NOT NULL,
                observed_value TEXT NOT NULL,
                position TEXT,
                direction TEXT,
                temperature_c TEXT,
                tilt_degrees TEXT,
                humidity_pct TEXT,
                calculated_error TEXT NOT NULL,
                mpe TEXT NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY(report_id) REFERENCES test_reports(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(report_id) REFERENCES test_reports(id)
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                record_type TEXT NOT NULL,
                record_id INTEGER NOT NULL,
                detail TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def bootstrap_admin():
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        existing = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing > 0:
            return
        
        username = os.environ.get("NAWI_ADMIN_USERNAME", "admin")
        password = os.environ.get("NAWI_ADMIN_PASSWORD")
        if not password:
            password = "change-me-immediately"
        
        cursor.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?, ?, ?, 'ADMIN')",
            (username, hash_password(password), os.environ.get("NAWI_ADMIN_FULL_NAME", "System Administrator")),
        )
        conn.commit()

init_db()
bootstrap_admin()

def log_audit(cursor, user_id: str, action: str, record_type: str, record_id: int, detail: str = ""):
    cursor.execute(
        "INSERT INTO audit_logs (user_id, action, record_type, record_id, detail) VALUES (?, ?, ?, ?, ?)",
        (user_id, action, record_type, record_id, detail),
    )

# =====================================================================
# Schemas
# =====================================================================
class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)
    full_name: str = Field(..., min_length=1)
    role: Literal["TESTER", "APPROVER", "ADMIN"]

class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    active: bool

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut

class InstrumentCreate(BaseModel):
    manufacturer: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    serial_number: str = Field(..., min_length=1)
    capacity: Decimal = Field(..., gt=0)
    unit: Literal["g", "kg"]
    d: Decimal = Field(..., gt=0)
    e: Decimal = Field(..., gt=0)
    accuracy_class: Literal["I", "II", "III", "IIII"]

class AccuracyPoint(BaseModel):
    reference_load: Decimal = Field(..., ge=0)
    observed_value: Decimal
    direction: Literal["INCREASING", "DECREASING"] = "INCREASING"

class EccentricityPoint(BaseModel):
    position: Literal["Centre", "Front-Left", "Front-Right", "Rear-Left", "Rear-Right"]
    reference_load: Decimal = Field(..., ge=0)
    observed_value: Decimal

class TarePoint(BaseModel):
    tare_load: Decimal = Field(..., ge=0)
    net_load: Decimal = Field(..., ge=0)
    observed_value: Decimal

class DiscriminationPoint(BaseModel):
    reference_load: Decimal = Field(..., ge=0)
    initial_indication: Decimal
    final_indication: Decimal

class EnvironmentalPoint(BaseModel):
    condition_label: str = Field(..., min_length=1)
    reference_load: Decimal = Field(..., ge=0)
    observed_value: Decimal
    temperature_c: Optional[Decimal] = None
    tilt_degrees: Optional[Decimal] = None
    humidity_pct: Optional[Decimal] = None

class FullTestSubmission(BaseModel):
    instrument_id: int
    report_id: Optional[int] = None
    test_location: Optional[str] = None
    reference_equipment: Optional[str] = None
    accuracy_points: List[AccuracyPoint] = []
    eccentricity_points: List[EccentricityPoint] = []
    tare_points: List[TarePoint] = []
    discrimination_points: List[DiscriminationPoint] = []
    environmental_points: List[EnvironmentalPoint] = []
    repeatability_load: Optional[Decimal] = None
    repeatability_readings: List[Decimal] = []

    @model_validator(mode="after")
    def _at_least_one_test(self):
        if not (self.accuracy_points or self.eccentricity_points or self.tare_points or 
                self.discrimination_points or self.environmental_points or self.repeatability_readings):
            raise ValueError("At least one observation is required.")
        if self.repeatability_readings and self.repeatability_load is None:
            raise ValueError("repeatability_load is required when repeatability_readings are supplied.")
        if self.repeatability_readings and len(self.repeatability_readings) < 2:
            raise ValueError("At least 2 repeatability readings are required to evaluate repeatability.")
        return self

class ApprovalRequest(BaseModel):
    report_id: int

class RejectionRequest(BaseModel):
    report_id: int
    reason: str = Field(..., min_length=1)

class RevisionRequest(BaseModel):
    report_id: int

# =====================================================================
# Auth
# =====================================================================
@app.post("/api/auth/login", response_model=TokenResponse)
def login(form: OAuth2PasswordRequestForm = Depends(), db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    row = cursor.execute(
        "SELECT id, username, password_hash, full_name, role, active FROM users WHERE username = ?",
        (form.username,),
    ).fetchone()
    
    invalid = HTTPException(status_code=401, detail="Incorrect username or password.")
    if not row or not verify_password(form.password, row["password_hash"]):
        raise invalid
    if not row["active"]:
        raise HTTPException(status_code=403, detail="This account has been deactivated.")
    
    user = TokenUser(id=row["id"], username=row["username"], full_name=row["full_name"], role=row["role"])
    token = create_access_token(user)
    log_audit(cursor, user.username, "LOGIN", "users", user.id)
    db.commit()
    return TokenResponse(access_token=token, user=UserOut(**user.model_dump(), active=True))

@app.get("/api/auth/me", response_model=UserOut)
def read_current_user(user: TokenUser = Depends(get_current_user)):
    return UserOut(**user.model_dump(), active=True)

@app.post("/api/auth/register", response_model=UserOut)
def register_user(
    data: UserCreate,
    db: sqlite3.Connection = Depends(get_db),
    admin: TokenUser = Depends(require_role("ADMIN")),
):
    cursor = db.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?, ?, ?, ?)",
            (data.username, hash_password(data.password), data.full_name, data.role),
        )
        new_id = cursor.lastrowid
        log_audit(cursor, admin.username, "CREATE_USER", "users", new_id, f"role={data.role}")
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Username already exists.")
    
    return UserOut(id=new_id, username=data.username, full_name=data.full_name, role=data.role, active=True)

@app.get("/api/auth/users", response_model=List[UserOut])
def list_users(db: sqlite3.Connection = Depends(get_db), admin: TokenUser = Depends(require_role("ADMIN"))):
    cursor = db.cursor()
    rows = cursor.execute("SELECT id, username, full_name, role, active FROM users ORDER BY id").fetchall()
    return [UserOut(**dict(r)) for r in rows]

@app.post("/api/auth/users/{user_id}/deactivate", response_model=UserOut)
def deactivate_user(
    user_id: int,
    db: sqlite3.Connection = Depends(get_db),
    admin: TokenUser = Depends(require_role("ADMIN")),
):
    cursor = db.cursor()
    row = cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found.")
    if row["id"] == admin.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account.")
    
    cursor.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
    log_audit(cursor, admin.username, "DEACTIVATE_USER", "users", user_id)
    db.commit()
    return UserOut(id=row["id"], username=row["username"], full_name=row["full_name"], role=row["role"], active=False)

# =====================================================================
# Instruments
# =====================================================================
@app.post("/api/instruments")
def register_instrument(
    data: InstrumentCreate,
    db: sqlite3.Connection = Depends(get_db),
    user: TokenUser = Depends(require_role("TESTER", "ADMIN")),
):
    n = MetrologyEngine.validate_instrument(data.capacity, data.d, data.e, data.accuracy_class)
    
    cursor = db.cursor()
    try:
        cursor.execute(
            """INSERT INTO instruments (manufacturer, model, serial_number, capacity, unit, d, e, accuracy_class, n)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data.manufacturer, data.model, data.serial_number, str(data.capacity),
             data.unit, str(data.d), str(data.e), data.accuracy_class, str(n)),
        )
        instrument_id = cursor.lastrowid
        log_audit(cursor, user.username, "REGISTER_INSTRUMENT", "instruments", instrument_id,
                  f"serial={data.serial_number}")
        db.commit()
        return {"id": instrument_id, "n": str(n), "message": "Instrument registered successfully"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Serial number already exists.")

@app.get("/api/instruments")
def list_instruments(db: sqlite3.Connection = Depends(get_db), user: TokenUser = Depends(require_role())):
    cursor = db.cursor()
    cursor.execute("SELECT * FROM instruments ORDER BY id DESC")
    return [dict(r) for r in cursor.fetchall()]

@app.get("/api/instruments/{instrument_id}")
def get_instrument(
    instrument_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: TokenUser = Depends(require_role()),
):
    cursor = db.cursor()
    cursor.execute("SELECT * FROM instruments WHERE id = ?", (instrument_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Instrument not found.")
    return dict(row)

# =====================================================================
# Test submission
# =====================================================================
@app.post("/api/tests/submit-full-suite")
def submit_full_test_suite(
    req: FullTestSubmission,
    db: sqlite3.Connection = Depends(get_db),
    user: TokenUser = Depends(require_role("TESTER", "ADMIN")),
):
    cursor = db.cursor()
    
    # 1. ALWAYS fetch the instrument (needed for math)
    cursor.execute("SELECT * FROM instruments WHERE id = ?", (req.instrument_id,))
    inst = cursor.fetchone()
    if not inst:
        raise HTTPException(status_code=404, detail="Instrument not found.")
    
    e_val = Decimal(inst["e"])
    acc_class = inst["accuracy_class"]
    d_val = Decimal(inst["d"])
    
    # 2. Determine report ID (Revision vs New)
    if req.report_id:
        cursor.execute("SELECT * FROM test_reports WHERE id = ? AND status = 'DRAFT'", (req.report_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=400, detail="Draft report not found.")
        report_id = req.report_id
    else:
        cursor.execute(
            """INSERT INTO test_reports
               (instrument_id, technician_name, technician_user_id, test_location, reference_equipment,
                status, overall_result, rule_version)
               VALUES (?, ?, ?, ?, ?, 'DRAFT', 'PENDING', ?)""",
            (req.instrument_id, user.full_name, user.id, req.test_location, req.reference_equipment, MPE_RULE_VERSION),
        )
        report_id = cursor.lastrowid
        
    all_observations = []
    overall_pass = True
    
    for pt in req.accuracy_points:
        res = MetrologyEngine.evaluate_observation(pt.reference_load, pt.observed_value, e_val, acc_class)
        res.update({"test_type": "Accuracy Test", "position": "Centre", "direction": pt.direction})
        if res["status"] == "FAIL":
            overall_pass = False
        all_observations.append(res)
        
    for pt in req.eccentricity_points:
        res = MetrologyEngine.evaluate_observation(pt.reference_load, pt.observed_value, e_val, acc_class)
        res.update({"test_type": "Eccentricity Test", "position": pt.position})
        if res["status"] == "FAIL":
            overall_pass = False
        all_observations.append(res)

    for pt in req.tare_points:
        res = MetrologyEngine.evaluate_observation(pt.net_load, pt.observed_value, e_val, acc_class)
        res.update({"test_type": f"Tare (T={pt.tare_load})", "position": "Centre"})
        if res["status"] == "FAIL":
            overall_pass = False
        all_observations.append(res)
        
    for pt in req.discrimination_points:
        res = MetrologyEngine.evaluate_discrimination(pt.reference_load, d_val, pt.initial_indication, pt.final_indication)
        if res["status"] == "FAIL": 
            overall_pass = False
        all_observations.append({
            "test_type": "Discrimination (+1.4d)",
            "position": "Centre",
            "reference_load": pt.reference_load,
            "observed_value": pt.final_indication,
            "calculated_error": res["calculated_change"],
            "mpe": d_val,
            "status": res["status"]
        })
        
    for pt in req.environmental_points:
        res = MetrologyEngine.evaluate_observation(pt.reference_load, pt.observed_value, e_val, acc_class)
        res.update({
            "test_type": f"Env ({pt.condition_label})",
            "position": "Centre",
            "temperature_c": pt.temperature_c,
            "tilt_degrees": pt.tilt_degrees,
            "humidity_pct": pt.humidity_pct,
        })
        if res["status"] == "FAIL":
            overall_pass = False
        all_observations.append(res)
        
    rep_res = None
    if req.repeatability_readings:
        rep_res = MetrologyEngine.evaluate_repeatability(
            req.repeatability_readings, req.repeatability_load, e_val, acc_class
        )
        if rep_res["status"] == "FAIL":
            overall_pass = False
        for idx, reading in enumerate(req.repeatability_readings):
            all_observations.append({
                "test_type": f"Repeatability (Run #{idx + 1})",
                "position": "Centre",
                "reference_load": req.repeatability_load,
                "observed_value": reading,
                "calculated_error": reading - req.repeatability_load,
                "mpe": rep_res["allowed_mpe"],
                "status": "RECORDED",
            })
        all_observations.append({
            "test_type": "Repeatability (Spread Summary)",
            "position": "Centre",
            "reference_load": req.repeatability_load,
            "observed_value": rep_res["max"] - rep_res["min"],
            "calculated_error": rep_res["difference"],
            "mpe": rep_res["allowed_mpe"],
            "status": rep_res["status"],
        })
        
    db_rows = [
        (
            report_id,
            o["test_type"],
            str(o["reference_load"]),
            str(o["observed_value"]),
            o.get("position"),
            o.get("direction"),
            str(o["temperature_c"]) if o.get("temperature_c") is not None else None,
            str(o["tilt_degrees"]) if o.get("tilt_degrees") is not None else None,
            str(o["humidity_pct"]) if o.get("humidity_pct") is not None else None,
            str(o["calculated_error"]),
            str(o["mpe"]),
            o["status"],
        )
        for o in all_observations
    ]
    
    cursor.executemany(
        """INSERT INTO test_observations
           (report_id, test_type, reference_load, observed_value, position, direction,
            temperature_c, tilt_degrees, humidity_pct, calculated_error, mpe, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        db_rows,
    )
    
    final_status = "PASS" if overall_pass else "FAIL"
    cursor.execute("UPDATE test_reports SET overall_result = ? WHERE id = ?", (final_status, report_id))
    log_audit(cursor, user.username, "CREATE_REPORT", "test_reports", report_id, f"result={final_status}")
    db.commit()
    
    return {
        "report_id": report_id,
        "overall_result": final_status,
        "rule_version": MPE_RULE_VERSION,
        "repeatability_summary": {k: str(v) for k, v in rep_res.items()} if rep_res else None,
        "observations": [
            {k: (str(v) if isinstance(v, Decimal) else v) for k, v in o.items()} for o in all_observations
        ],
    }

# =====================================================================
# Reports: listing, review, approval, rejection, revision
# =====================================================================
@app.get("/api/reports")
def list_reports(db: sqlite3.Connection = Depends(get_db), user: TokenUser = Depends(require_role())):
    cursor = db.cursor()
    cursor.execute("""
        SELECT r.id, r.status, r.overall_result, r.technician_name, r.approved_by,
               r.parent_report_id, r.revision_number, r.created_at, r.approved_at,
               i.manufacturer, i.model, i.serial_number
        FROM test_reports r JOIN instruments i ON r.instrument_id = i.id
        ORDER BY r.id DESC
    """)
    return [dict(r) for r in cursor.fetchall()]

@app.get("/api/reports/{report_id}")
def get_report(
    report_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: TokenUser = Depends(require_role()),
):
    cursor = db.cursor()
    cursor.execute("""
        SELECT r.*, i.manufacturer, i.model, i.serial_number, i.capacity, i.unit, i.d, i.e, i.accuracy_class, i.n
        FROM test_reports r JOIN instruments i ON r.instrument_id = i.id WHERE r.id = ?
    """, (report_id,))
    report = cursor.fetchone()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    
    cursor.execute("SELECT * FROM test_observations WHERE report_id = ? ORDER BY id", (report_id,))
    obs = cursor.fetchall()
    return {"report": dict(report), "observations": [dict(o) for o in obs]}

@app.post("/api/reports/approve")
def approve_report(
    req: ApprovalRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: TokenUser = Depends(require_role("APPROVER", "ADMIN")),
):
    cursor = db.cursor()
    cursor.execute("SELECT technician_user_id, status FROM test_reports WHERE id = ?", (req.report_id,))
    report = cursor.fetchone()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    if report["status"] == "APPROVED":
        raise HTTPException(status_code=400, detail="Report is already approved.")
        
    if report["technician_user_id"] is not None and report["technician_user_id"] == user.id:
        raise HTTPException(status_code=400, detail="You cannot approve your own report.")
        
    cursor.execute(
        "UPDATE test_reports SET status = 'APPROVED', approved_by = ?, approver_user_id = ?, "
        "approved_at = CURRENT_TIMESTAMP WHERE id = ?",
        (user.full_name, user.id, req.report_id),
    )
    log_audit(cursor, user.username, "APPROVE_REPORT", "test_reports", req.report_id)
    db.commit()
    return {"message": f"Report #{req.report_id} successfully approved."}

@app.post("/api/reports/reject")
def reject_report(
    req: RejectionRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: TokenUser = Depends(require_role("APPROVER", "ADMIN")),
):
    cursor = db.cursor()
    cursor.execute("SELECT technician_user_id, status FROM test_reports WHERE id = ?", (req.report_id,))
    report = cursor.fetchone()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    if report["status"] == "APPROVED":
        raise HTTPException(status_code=400, detail="An approved report cannot be rejected.")
    if report["technician_user_id"] is not None and report["technician_user_id"] == user.id:
        raise HTTPException(status_code=400, detail="You cannot reject your own report.")
        
    cursor.execute(
        "UPDATE test_reports SET status = 'REJECTED', approved_by = ?, approver_user_id = ?, "
        "rejection_reason = ? WHERE id = ?",
        (user.full_name, user.id, req.reason, req.report_id),
    )
    log_audit(cursor, user.username, "REJECT_REPORT", "test_reports", req.report_id, req.reason)
    db.commit()
    return {"message": f"Report #{req.report_id} rejected. Create a revised report to correct it."}

@app.post("/api/reports/revise")
def revise_report(
    req: RevisionRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: TokenUser = Depends(require_role("TESTER", "ADMIN")),
):
    cursor = db.cursor()
    cursor.execute("SELECT * FROM test_reports WHERE id = ?", (req.report_id,))
    original = cursor.fetchone()
    if not original:
        raise HTTPException(status_code=404, detail="Report not found.")
    if original["status"] == "DRAFT":
        raise HTTPException(status_code=400, detail="A DRAFT report does not need a revision; submit corrections directly.")
        
    cursor.execute(
        """INSERT INTO test_reports
           (instrument_id, technician_name, technician_user_id, test_location, reference_equipment,
            status, overall_result, rule_version, parent_report_id, revision_number)
           VALUES (?, ?, ?, ?, ?, 'DRAFT', 'PENDING', ?, ?, ?)""",
        (
            original["instrument_id"], user.full_name, user.id, original["test_location"],
            original["reference_equipment"], MPE_RULE_VERSION, original["id"],
            (original["revision_number"] or 1) + 1,
        ),
    )
    new_report_id = cursor.lastrowid
    log_audit(cursor, user.username, "REVISE_REPORT", "test_reports", new_report_id,
              f"supersedes report #{original['id']}")
    db.commit()
    return {"message": f"Revision created as report #{new_report_id}. Submit new observations against it.",
            "report_id": new_report_id, "parent_report_id": original["id"]}

# =====================================================================
# Attachments & Exports
# =====================================================================
@app.post("/api/reports/{report_id}/attachments")
async def upload_attachment(
    report_id: int, 
    file: UploadFile = File(...), 
    db: sqlite3.Connection = Depends(get_db), 
    user: TokenUser = Depends(require_role())
):
    cursor = db.cursor()
    cursor.execute("SELECT id FROM test_reports WHERE id = ?", (report_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail="Report not found.")
    
    filepath = os.path.join(UPLOAD_DIR, f"{report_id}_{file.filename}")
    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    cursor.execute(
        "INSERT INTO report_attachments (report_id, filename, filepath) VALUES (?, ?, ?)", 
        (report_id, file.filename, filepath)
    )
    db.commit()
    return {"message": "Attachment uploaded successfully", "filename": file.filename}

@app.get("/api/reports/{report_id}/attachments")
def list_attachments(report_id: int, db: sqlite3.Connection = Depends(get_db), user: TokenUser = Depends(require_role())):
    cursor = db.cursor()
    cursor.execute("SELECT id, filename, uploaded_at FROM report_attachments WHERE report_id = ?", (report_id,))
    return [dict(r) for r in cursor.fetchall()]


@app.get("/api/reports/{report_id}/pdf")
def download_pdf(
    report_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: TokenUser = Depends(require_role()),
):
    from app.report_generator import generate_pdf_certificate
    
    cursor = db.cursor()
    cursor.execute("""
        SELECT r.*, i.manufacturer, i.model, i.serial_number, i.capacity, i.unit, i.d, i.e, i.accuracy_class, i.n
        FROM test_reports r
        JOIN instruments i ON r.instrument_id = i.id
        WHERE r.id = ?
    """, (report_id,))
    report = cursor.fetchone()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
        
    cursor.execute("SELECT * FROM test_observations WHERE report_id = ? ORDER BY id", (report_id,))
    obs_rows = cursor.fetchall()
    
    pdf_bytes = generate_pdf_certificate(dict(report), [dict(o) for o in obs_rows])
    
    return Response(content=pdf_bytes, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=Verification_Certificate_{report_id}.pdf"
    })

@app.get("/api/reports/{report_id}/docx")
def download_docx(
    report_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: TokenUser = Depends(require_role()),
):
    from app.docx_generator import generate_docx_certificate
    
    cursor = db.cursor()
    cursor.execute("""
        SELECT r.*, i.manufacturer, i.model, i.serial_number, i.capacity, i.unit, i.d, i.e, i.accuracy_class, i.n
        FROM test_reports r JOIN instruments i ON r.instrument_id = i.id WHERE r.id = ?
    """, (report_id,))
    report = cursor.fetchone()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
        
    cursor.execute("SELECT * FROM test_observations WHERE report_id = ? ORDER BY id", (report_id,))
    docx_bytes = generate_docx_certificate(dict(report), [dict(o) for o in cursor.fetchall()])
    
    return Response(content=docx_bytes, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={
        "Content-Disposition": f"attachment; filename=Verification_Certificate_{report_id}.docx"
    })

if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")