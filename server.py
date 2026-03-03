import os
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, abort
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user,
)
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    ForeignKey,
    UniqueConstraint,
    select,
    desc,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
APP_TZ = ZoneInfo("Europe/Lisbon")

def utcnow():
    return datetime.now(timezone.utc)

def to_local(dt_utc: datetime) -> datetime:
    if dt_utc is None:
        return None
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(APP_TZ)

def parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def minutes_to_hhmm(total_minutes: int) -> str:
    sign = ""
    if total_minutes < 0:
        sign = "-"
        total_minutes = abs(total_minutes)
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{sign}{h:02d}:{m:02d}"

def dt_range_utc(start_d: date, end_d: date):
    start_local = datetime.combine(start_d, datetime.min.time(), tzinfo=APP_TZ)
    end_local = datetime.combine(end_d + timedelta(days=1), datetime.min.time(), tzinfo=APP_TZ)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class AdminUser(Base, UserMixin):
    """
    Vamos reutilizar esta tabela para:
      - admin (você)
      - kiosk (usuário do tablet)
    """
    __tablename__ = "admin_users"
    id = Column(Integer, primary_key=True)
    username = Column(String(120), unique=True, nullable=False)
    password = Column(String(255), nullable=False)  # texto puro por enquanto
    is_active = Column(Boolean, default=True)

    # NOVO: role (admin/kiosk). Em bancos antigos, a coluna não existirá.
    # Vamos criar via ALTER TABLE na inicialização.
    role = Column(String(20), default="admin")

class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    daily_minutes = Column(Integer, default=8 * 60)    # 480
    weekly_minutes = Column(Integer, default=40 * 60)  # 2400

    punches = relationship("Punch", back_populates="employee", cascade="all, delete-orphan")
    adjustments = relationship("DailyAdjustment", back_populates="employee", cascade="all, delete-orphan")

class Punch(Base):
    __tablename__ = "punches"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    kind = Column(String(10), nullable=False)  # IN / OUT
    at_utc = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    employee = relationship("Employee", back_populates="punches")

class DailyAdjustment(Base):
    __tablename__ = "daily_adjustments"
    __table_args__ = (UniqueConstraint("employee_id", "day_local", name="uq_employee_day"),)

    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    day_local = Column(String(10), nullable=False)  # YYYY-MM-DD
    lunch_minutes = Column(Integer, default=60)      # 0/30/60
    day_off = Column(Boolean, default=False)

    employee = relationship("Employee", back_populates="adjustments")

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

# Dica: mantém sessão por mais tempo (tablet)
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    db = SessionLocal()
    try:
        return db.get(AdminUser, int(user_id))
    finally:
        db.close()

# -----------------------------------------------------------------------------
# Schema upgrade (para adicionar role sem migration tool)
# -----------------------------------------------------------------------------
def ensure_schema_upgrades():
    """
    Se o banco já existe (você já rodou /setup), a tabela admin_users existe
    mas pode não ter a coluna role.
    Vamos adicionar com ALTER TABLE de forma segura.
    """
    with engine.begin() as conn:
        # checa se coluna role existe
        col = conn.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name='admin_users' AND column_name='role'
        """)).fetchone()
        if not col:
            conn.execute(text("ALTER TABLE admin_users ADD COLUMN role VARCHAR(20) DEFAULT 'admin';"))
            # garante que registros antigos virem admin
            conn.execute(text("UPDATE admin_users SET role='admin' WHERE role IS NULL;"))

# -----------------------------------------------------------------------------
# DB init helpers
# -----------------------------------------------------------------------------
def ensure_db():
    Base.metadata.create_all(engine)
    ensure_schema_upgrades()

def seed_default_users_and_employees():
    """
    - cria admin (role=admin)
    - cria kiosk (role=kiosk) para tablet
    - cria 4 funcionárias padrão se não existirem
    """
    admin_user = os.environ.get("ADMIN_USER", "admin")
    admin_pass = os.environ.get("ADMIN_PASS", "admin123")

    kiosk_user = os.environ.get("KIOSK_USER", "tablet")
    kiosk_pass = os.environ.get("KIOSK_PASS", "tablet123")

    default_employees = [
        "Luziane",
        "Marly",
        "Regina",
        "Sueli",
    ]

    db = SessionLocal()
    try:
        # admin
        u_admin = db.execute(select(AdminUser).where(AdminUser.username == admin_user)).scalar_one_or_none()
        if not u_admin:
            db.add(AdminUser(username=admin_user, password=admin_pass, is_active=True, role="admin"))
        else:
            # garante role admin
            if not u_admin.role:
                u_admin.role = "admin"

        # kiosk
        u_kiosk = db.execute(select(AdminUser).where(AdminUser.username == kiosk_user)).scalar_one_or_none()
        if not u_kiosk:
            db.add(AdminUser(username=kiosk_user, password=kiosk_pass, is_active=True, role="kiosk"))
        else:
            if not u_kiosk.role:
                u_kiosk.role = "kiosk"

        # employees
        for n in default_employees:
            e = db.execute(select(Employee).where(Employee.name == n)).scalar_one_or_none()
            if not e:
                db.add(Employee(name=n, daily_minutes=480, weekly_minutes=2400))

        db.commit()
    finally:
        db.close()

# -----------------------------------------------------------------------------
# Auth helpers
# -----------------------------------------------------------------------------
def role_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            user_role = getattr(current_user, "role", "admin") or "admin"
            if user_role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def is_kiosk():
    return current_user.is_authenticated and (getattr(current_user, "role", "admin") == "kiosk")

# -----------------------------------------------------------------------------
# Core helpers
# -----------------------------------------------------------------------------
def get_last_punch(db, emp_id: int) -> Punch | None:
    return db.execute(
        select(Punch).where(Punch.employee_id == emp_id).order_by(desc(Punch.at_utc)).limit(1)
    ).scalar_one_or_none()

def can_punch_in(last: Punch | None) -> bool:
    return (last is None) or (last.kind == "OUT")

def can_punch_out(last: Punch | None) -> bool:
    return (last is not None) and (last.kind == "IN")

def get_or_create_adjustment(db, emp_id: int, day_local: date) -> DailyAdjustment:
    key = day_local.strftime("%Y-%m-%d")
    adj = db.execute(
        select(DailyAdjustment)
        .where(DailyAdjustment.employee_id == emp_id)
        .where(DailyAdjustment.day_local == key)
    ).scalar_one_or_none()
    if adj:
        return adj
    adj = DailyAdjustment(employee_id=emp_id, day_local=key, lunch_minutes=60, day_off=False)
    db.add(adj)
    db.flush()
    return adj

def worked_minutes_gross_in_range(db, emp_id: int, start_utc: datetime, end_utc: datetime) -> int:
    punches = db.execute(
        select(Punch)
        .where(Punch.employee_id == emp_id)
        .where(Punch.at_utc >= start_utc)
        .where(Punch.at_utc < end_utc)
        .order_by(Punch.at_utc.asc())
    ).scalars().all()

    total = 0
    current_in = None
    for p in punches:
        if p.kind == "IN":
            current_in = p.at_utc
        elif p.kind == "OUT":
            if current_in is not None and p.at_utc > current_in:
                delta = p.at_utc - current_in
                total += int(delta.total_seconds() // 60)
            current_in = None
    return total

def expected_minutes_for_day(employee: Employee, day_local: date, day_off_flag: bool) -> int:
    if day_off_flag:
        return 0
    if day_local.weekday() >= 5:
        return 0
    return int(employee.daily_minutes or 0)

def net_minutes_for_day(gross_minutes: int, lunch_minutes: int) -> int:
    return max(0, gross_minutes - max(0, lunch_minutes))

def parse_lunch(v: str) -> int:
    try:
        x = int(v)
    except Exception:
        return 60
    if x not in (0, 30, 60):
        return 60
    return x

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/setup")
def setup():
    ensure_db()
    seed_default_users_and_employees()
    return {
        "ok": True,
        "message": "DB pronta e dados iniciais criados. Use /login (admin) ou /kiosk (tablet)",
        "admin_user_env": "ADMIN_USER (default admin)",
        "admin_pass_env": "ADMIN_PASS (default admin123)",
        "kiosk_user_env": "KIOSK_USER (default tablet)",
        "kiosk_pass_env": "KIOSK_PASS (default tablet123)",
    }

# ---------------- Admin login ----------------
@app.get("/login")
def login():
    return render_template("login.html")

@app.post("/login")
def login_post():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    db = SessionLocal()
    try:
        u = db.execute(select(AdminUser).where(AdminUser.username == username)).scalar_one_or_none()
        if not u or u.password != password or not u.is_active:
            flash("Login inválido", "error")
            return redirect(url_for("login"))
        # Admin entra aqui
        login_user(u, remember=True)
        # kiosk vai para /kiosk automaticamente
        if getattr(u, "role", "admin") == "kiosk":
            return redirect(url_for("kiosk"))
        return redirect(url_for("dashboard"))
    finally:
        db.close()

@app.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# ---------------- Kiosk routes ----------------
@app.get("/kiosk")
@login_required
@role_required("kiosk")
def kiosk():
    ensure_db()
    db = SessionLocal()
    try:
        employees = db.execute(select(Employee).order_by(Employee.name.asc())).scalars().all()
        today_local = datetime.now(APP_TZ).date()

        items = []
        for e in employees:
            last = get_last_punch(db, e.id)
            adj = get_or_create_adjustment(db, e.id, today_local)

            items.append({
                "id": e.id,
                "name": e.name,
                "last_kind": last.kind if last else None,
                "last_at_local": to_local(last.at_utc) if last else None,
                "can_in": can_punch_in(last),
                "can_out": can_punch_out(last),
                "lunch_minutes": int(adj.lunch_minutes or 0),
                "day_off": bool(adj.day_off),
            })

        db.commit()
        return render_template("kiosk.html", items=items, today_local=today_local)
    finally:
        db.close()

@app.post("/kiosk/punch/<int:emp_id>/<kind>")
@login_required
@role_required("kiosk")
def kiosk_punch(emp_id: int, kind: str):
    kind = kind.upper()
    if kind not in ("IN", "OUT"):
        flash("Tipo inválido", "error")
        return redirect(url_for("kiosk"))

    db = SessionLocal()
    try:
        emp = db.get(Employee, emp_id)
        if not emp:
            flash("Funcionária não encontrada", "error")
            return redirect(url_for("kiosk"))

        last = get_last_punch(db, emp_id)

        if kind == "IN" and not can_punch_in(last):
            flash("Já existe uma entrada em aberto (falta saída).", "error")
            return redirect(url_for("kiosk"))

        if kind == "OUT" and not can_punch_out(last):
            flash("Não existe entrada para fechar (faça entrada primeiro).", "error")
            return redirect(url_for("kiosk"))

        db.add(Punch(employee_id=emp_id, kind=kind, at_utc=utcnow()))
        db.commit()
        flash(f"Marcado {('ENTRADA' if kind=='IN' else 'SAÍDA')} para {emp.name}", "success")
        return redirect(url_for("kiosk"))
    finally:
        db.close()

@app.post("/kiosk/adjust/<int:emp_id>")
@login_required
@role_required("kiosk")
def kiosk_adjust(emp_id: int):
    today_local = datetime.now(APP_TZ).date()
    lunch = (request.form.get("lunch_minutes") or "").strip()
    day_off = request.form.get("day_off") == "on"

    db = SessionLocal()
    try:
        emp = db.get(Employee, emp_id)
        if not emp:
            flash("Funcionária não encontrada", "error")
            return redirect(url_for("kiosk"))

        adj = get_or_create_adjustment(db, emp_id, today_local)
        adj.lunch_minutes = parse_lunch(lunch)
        adj.day_off = bool(day_off)

        db.commit()
        flash(f"Ajustes de hoje salvos para {emp.name}.", "success")
        return redirect(url_for("kiosk"))
    finally:
        db.close()

# ---------------- Admin dashboard/report ----------------
@app.get("/")
@login_required
@role_required("admin")
def dashboard():
    ensure_db()
    db = SessionLocal()
    try:
        employees = db.execute(select(Employee).order_by(Employee.name.asc())).scalars().all()
        today_local = datetime.now(APP_TZ).date()

        start_today_utc, end_today_utc = dt_range_utc(today_local, today_local)
        start_week = week_start(today_local)
        end_week = start_week + timedelta(days=6)

        status = []
        for e in employees:
            last = get_last_punch(db, e.id)
            last_local = to_local(last.at_utc) if last else None

            adj = get_or_create_adjustment(db, e.id, today_local)

            gross_today = worked_minutes_gross_in_range(db, e.id, start_today_utc, end_today_utc)
            net_today = net_minutes_for_day(gross_today, adj.lunch_minutes)

            expected_today = expected_minutes_for_day(e, today_local, adj.day_off)
            balance_today = net_today - expected_today

            net_week = 0
            expected_week = 0
            d = start_week
            while d <= end_week:
                d_start_utc, d_end_utc = dt_range_utc(d, d)
                gross_d = worked_minutes_gross_in_range(db, e.id, d_start_utc, d_end_utc)
                adj_d = get_or_create_adjustment(db, e.id, d)
                net_d = net_minutes_for_day(gross_d, adj_d.lunch_minutes)
                exp_d = expected_minutes_for_day(e, d, bool(adj_d.day_off))
                net_week += net_d
                expected_week += exp_d
                d += timedelta(days=1)

            week_balance = net_week - expected_week

            status.append(
                {
                    "id": e.id,
                    "name": e.name,
                    "daily_minutes": e.daily_minutes,
                    "weekly_minutes": e.weekly_minutes,
                    "last_kind": last.kind if last else None,
                    "last_at_local": last_local,
                    "can_in": can_punch_in(last),
                    "can_out": can_punch_out(last),
                    "lunch_minutes": int(adj.lunch_minutes or 0),
                    "day_off": bool(adj.day_off),
                    "gross_today": minutes_to_hhmm(gross_today),
                    "net_today": minutes_to_hhmm(net_today),
                    "expected_today": minutes_to_hhmm(expected_today),
                    "balance_today": minutes_to_hhmm(balance_today),
                    "net_week": minutes_to_hhmm(net_week),
                    "expected_week": minutes_to_hhmm(expected_week),
                    "week_balance": minutes_to_hhmm(week_balance),
                }
            )

        db.commit()
        return render_template(
            "dashboard.html",
            status=status,
            today_local=today_local,
            start_week=start_week,
            end_week=end_week,
        )
    finally:
        db.close()

@app.post("/punch/<int:emp_id>/<kind>")
@login_required
@role_required("admin")
def punch(emp_id: int, kind: str):
    # admin usa o mesmo mecanismo, mas fica no dashboard
    kind = kind.upper()
    if kind not in ("IN", "OUT"):
        flash("Tipo de marcação inválido", "error")
        return redirect(url_for("dashboard"))

    db = SessionLocal()
    try:
        emp = db.get(Employee, emp_id)
        if not emp:
            flash("Funcionária não encontrada", "error")
            return redirect(url_for("dashboard"))

        last = get_last_punch(db, emp_id)

        if kind == "IN" and not can_punch_in(last):
            flash("Já existe uma entrada em aberto (falta saída).", "error")
            return redirect(url_for("dashboard"))

        if kind == "OUT" and not can_punch_out(last):
            flash("Não existe entrada para fechar (faça entrada primeiro).", "error")
            return redirect(url_for("dashboard"))

        db.add(Punch(employee_id=emp_id, kind=kind, at_utc=utcnow()))
        db.commit()
        flash(f"Marcado {('ENTRADA' if kind=='IN' else 'SAÍDA')} para {emp.name}", "success")
        return redirect(url_for("dashboard"))
    finally:
        db.close()

@app.post("/adjustments/today/<int:emp_id>")
@login_required
@role_required("admin")
def set_today_adjustments(emp_id: int):
    today_local = datetime.now(APP_TZ).date()
    lunch = (request.form.get("lunch_minutes") or "").strip()
    day_off = request.form.get("day_off") == "on"

    db = SessionLocal()
    try:
        emp = db.get(Employee, emp_id)
        if not emp:
            flash("Funcionária não encontrada", "error")
            return redirect(url_for("dashboard"))

        adj = get_or_create_adjustment(db, emp_id, today_local)
        adj.lunch_minutes = parse_lunch(lunch)
        adj.day_off = bool(day_off)

        db.commit()
        flash(f"Ajustes de hoje salvos para {emp.name}.", "success")
        return redirect(url_for("dashboard"))
    finally:
        db.close()

@app.get("/employees")
@login_required
@role_required("admin")
def employees():
    db = SessionLocal()
    try:
        emps = db.execute(select(Employee).order_by(Employee.name.asc())).scalars().all()
        return render_template("employees.html", employees=emps)
    finally:
        db.close()

@app.post("/employees/update/<int:emp_id>")
@login_required
@role_required("admin")
def employees_update(emp_id: int):
    name = (request.form.get("name") or "").strip()
    daily = (request.form.get("daily_minutes") or "").strip()
    weekly = (request.form.get("weekly_minutes") or "").strip()

    def to_int(v, fallback):
        try:
            return int(v)
        except Exception:
            return fallback

    db = SessionLocal()
    try:
        emp = db.get(Employee, emp_id)
        if not emp:
            flash("Funcionária não encontrada", "error")
            return redirect(url_for("employees"))

        if name:
            emp.name = name

        emp.daily_minutes = to_int(daily, emp.daily_minutes)
        emp.weekly_minutes = to_int(weekly, emp.weekly_minutes)

        db.commit()
        flash("Funcionária atualizada.", "success")
        return redirect(url_for("employees"))
    finally:
        db.close()

@app.get("/report")
@login_required
@role_required("admin")
def report():
    db = SessionLocal()
    try:
        employees = db.execute(select(Employee).order_by(Employee.name.asc())).scalars().all()

        today_local = datetime.now(APP_TZ).date()
        start_s = request.args.get("start") or today_local.replace(day=1).strftime("%Y-%m-%d")
        end_s = request.args.get("end") or today_local.strftime("%Y-%m-%d")

        start_d = parse_date(start_s) or today_local.replace(day=1)
        end_d = parse_date(end_s) or today_local
        if end_d < start_d:
            start_d, end_d = end_d, start_d

        rows = []
        for e in employees:
            total_gross = 0
            total_net = 0
            total_expected = 0
            total_lunch = 0

            d = start_d
            while d <= end_d:
                d_start_utc, d_end_utc = dt_range_utc(d, d)
                gross_d = worked_minutes_gross_in_range(db, e.id, d_start_utc, d_end_utc)

                adj_d = get_or_create_adjustment(db, e.id, d)
                lunch_d = int(adj_d.lunch_minutes or 0)
                net_d = net_minutes_for_day(gross_d, lunch_d)

                exp_d = expected_minutes_for_day(e, d, bool(adj_d.day_off))

                total_gross += gross_d
                total_lunch += lunch_d
                total_net += net_d
                total_expected += exp_d

                d += timedelta(days=1)

            balance = total_net - total_expected

            rows.append(
                {
                    "name": e.name,
                    "gross": minutes_to_hhmm(total_gross),
                    "lunch": minutes_to_hhmm(total_lunch),
                    "net": minutes_to_hhmm(total_net),
                    "expected": minutes_to_hhmm(total_expected),
                    "balance": minutes_to_hhmm(balance),
                }
            )

        db.commit()
        return render_template(
            "report.html",
            rows=rows,
            start=start_d.strftime("%Y-%m-%d"),
            end=end_d.strftime("%Y-%m-%d"),
        )
    finally:
        db.close()

@app.errorhandler(403)
def forbidden(_):
    return "Acesso negado.", 403

if __name__ == "__main__":
    ensure_db()
    seed_default_users_and_employees()
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
