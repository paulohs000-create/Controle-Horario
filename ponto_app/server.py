import os
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Flask, render_template, request, redirect, url_for, flash
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
    select,
    func,
    desc,
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
    if total_minutes < 0:
        total_minutes = 0
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{h:02d}:{m:02d}"

def dt_range_utc(start_d: date, end_d: date):
    # inclusive start, exclusive end+1 day in local time converted to UTC
    start_local = datetime.combine(start_d, datetime.min.time(), tzinfo=APP_TZ)
    end_local = datetime.combine(end_d + timedelta(days=1), datetime.min.time(), tzinfo=APP_TZ)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

DATABASE_URL = os.environ.get("DATABASE_URL")  # Railway injecta
if not DATABASE_URL:
    # fallback local
    DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"

# SQLAlchemy expects postgresql+psycopg
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
    __tablename__ = "admin_users"
    id = Column(Integer, primary_key=True)
    username = Column(String(120), unique=True, nullable=False)
    password = Column(String(255), nullable=False)  # simples (ver nota abaixo)
    is_active = Column(Boolean, default=True)

class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)

    # carga padrão (minutos)
    daily_minutes = Column(Integer, default=8 * 60)    # 08:00
    weekly_minutes = Column(Integer, default=40 * 60)  # 40:00

    punches = relationship("Punch", back_populates="employee", cascade="all, delete-orphan")

class Punch(Base):
    __tablename__ = "punches"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    # IN / OUT
    kind = Column(String(10), nullable=False)

    # armazenar em UTC
    at_utc = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    employee = relationship("Employee", back_populates="punches")

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    db = SessionLocal()
    try:
        u = db.get(AdminUser, int(user_id))
        return u
    finally:
        db.close()

# -----------------------------------------------------------------------------
# DB init helpers
# -----------------------------------------------------------------------------
def ensure_db():
    Base.metadata.create_all(engine)

def seed_default_admin_and_employees():
    """
    Cria admin e 4 funcionárias se ainda não existirem.
    NOTA: senha em texto puro para ficar simples.
    Se você quiser, eu troco para hash (recomendado).
    """
    admin_user = os.environ.get("ADMIN_USER", "admin")
    admin_pass = os.environ.get("ADMIN_PASS", "admin123")

    default_employees = [
        "Funcionária 1",
        "Funcionária 2",
        "Funcionária 3",
        "Funcionária 4",
    ]

    db = SessionLocal()
    try:
        # admin
        existing_admin = db.execute(select(AdminUser).where(AdminUser.username == admin_user)).scalar_one_or_none()
        if not existing_admin:
            db.add(AdminUser(username=admin_user, password=admin_pass, is_active=True))

        # employees
        for n in default_employees:
            e = db.execute(select(Employee).where(Employee.name == n)).scalar_one_or_none()
            if not e:
                db.add(Employee(name=n))

        db.commit()
    finally:
        db.close()

# -----------------------------------------------------------------------------
# Core calculations
# -----------------------------------------------------------------------------
def get_last_punch(db, emp_id: int) -> Punch | None:
    return db.execute(
        select(Punch).where(Punch.employee_id == emp_id).order_by(desc(Punch.at_utc)).limit(1)
    ).scalar_one_or_none()

def can_punch_in(last: Punch | None) -> bool:
    return (last is None) or (last.kind == "OUT")

def can_punch_out(last: Punch | None) -> bool:
    return (last is not None) and (last.kind == "IN")

def worked_minutes_in_range(db, emp_id: int, start_utc: datetime, end_utc: datetime) -> int:
    """
    Soma pares IN->OUT dentro do período.
    Regra simples:
      - Considera apenas intervalos fechados (com OUT).
      - Se existir IN sem OUT, ignora (fica como "em aberto").
    """
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

def week_start(d: date) -> date:
    # segunda-feira como início da semana
    return d - timedelta(days=d.weekday())

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/setup")
def setup():
    ensure_db()
    seed_default_admin_and_employees()
    return {
        "ok": True,
        "message": "DB pronta e dados iniciais criados. Use /login",
        "admin_user_env": "ADMIN_USER (default admin)",
        "admin_pass_env": "ADMIN_PASS (default admin123)",
    }

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
        login_user(u)
        return redirect(url_for("dashboard"))
    finally:
        db.close()

@app.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.get("/")
@login_required
def dashboard():
    ensure_db()
    db = SessionLocal()
    try:
        employees = db.execute(select(Employee).order_by(Employee.name.asc())).scalars().all()

        status = []
        for e in employees:
            last = get_last_punch(db, e.id)
            last_local = to_local(last.at_utc) if last else None
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
                }
            )

        today_local = datetime.now(APP_TZ).date()
        start_week = week_start(today_local)
        end_week = start_week + timedelta(days=6)

        # resumo do dia e da semana
        start_today_utc, end_today_utc = dt_range_utc(today_local, today_local)
        start_week_utc, end_week_utc = dt_range_utc(start_week, end_week)

        summaries = []
        for e in employees:
            today_min = worked_minutes_in_range(db, e.id, start_today_utc, end_today_utc)
            week_min = worked_minutes_in_range(db, e.id, start_week_utc, end_week_utc)

            overtime_today = max(0, today_min - e.daily_minutes)
            overtime_week = max(0, week_min - e.weekly_minutes)

            summaries.append(
                {
                    "id": e.id,
                    "today": minutes_to_hhmm(today_min),
                    "week": minutes_to_hhmm(week_min),
                    "ot_today": minutes_to_hhmm(overtime_today),
                    "ot_week": minutes_to_hhmm(overtime_week),
                }
            )

        return render_template(
            "dashboard.html",
            status=status,
            summaries=summaries,
            today_local=today_local,
            start_week=start_week,
            end_week=end_week,
        )
    finally:
        db.close()

@app.post("/punch/<int:emp_id>/<kind>")
@login_required
def punch(emp_id: int, kind: str):
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

@app.get("/employees")
@login_required
def employees():
    db = SessionLocal()
    try:
        emps = db.execute(select(Employee).order_by(Employee.name.asc())).scalars().all()
        return render_template("employees.html", employees=emps)
    finally:
        db.close()

@app.post("/employees/update/<int:emp_id>")
@login_required
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

        start_utc, end_utc = dt_range_utc(start_d, end_d)

        rows = []
        for e in employees:
            total_min = worked_minutes_in_range(db, e.id, start_utc, end_utc)

            # estimativa de "esperado" (simples):
            # conta dias úteis (Seg-Sex) * daily_minutes
            expected = 0
            d = start_d
            while d <= end_d:
                if d.weekday() < 5:
                    expected += e.daily_minutes
                d += timedelta(days=1)

            overtime = max(0, total_min - expected)
            deficit = max(0, expected - total_min)

            rows.append(
                {
                    "name": e.name,
                    "worked": minutes_to_hhmm(total_min),
                    "expected": minutes_to_hhmm(expected),
                    "overtime": minutes_to_hhmm(overtime),
                    "deficit": minutes_to_hhmm(deficit),
                }
            )

        return render_template(
            "report.html",
            rows=rows,
            start=start_d.strftime("%Y-%m-%d"),
            end=end_d.strftime("%Y-%m-%d"),
        )
    finally:
        db.close()

if __name__ == "__main__":
    ensure_db()
    seed_default_admin_and_employees()
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
