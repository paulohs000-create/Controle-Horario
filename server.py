import os
from datetime import datetime, date, timedelta, timezone, time
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
    delete,
    inspect,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
APP_TZ = ZoneInfo("Europe/Lisbon")


def utcnow():
    return datetime.now(timezone.utc)


def to_local(dt_utc: datetime) -> datetime | None:
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


def parse_hhmm(s: str) -> tuple[int, int] | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        hh, mm = s.split(":")
        hh_i = int(hh)
        mm_i = int(mm)
        if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
            return None
        return hh_i, mm_i
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


def dt_range_utc_for_local_day(d: date):
    start_local = datetime.combine(d, time(0, 0), tzinfo=APP_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def local_dt_to_utc(d: date, hh: int, mm: int) -> datetime:
    dt_local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=APP_TZ)
    return dt_local.astimezone(timezone.utc)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def parse_lunch(v: str) -> int:
    try:
        x = int(v)
    except Exception:
        return 60
    if x not in (0, 30, 60):
        return 60
    return x


def parse_money(v: str) -> float:
    v = (v or "").strip().replace(",", ".")
    if not v:
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


# -----------------------------------------------------------------------------
# Template folder resolver (fix TemplateNotFound in mixed repo layouts)
# -----------------------------------------------------------------------------
def _resolve_template_folder() -> str:
    """
    Alguns repositórios têm templates/ no root, outros em ponto_app/templates/.
    Railway pode rodar de /app e o Flask procura relativo ao script.
    Vamos localizar automaticamente para nunca falhar.
    """
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, "templates"),
        os.path.join(base, "ponto_app", "templates"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    # fallback
    return os.path.join(base, "templates")


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
    __tablename__ = "admin_users"
    id = Column(Integer, primary_key=True)
    username = Column(String(120), unique=True, nullable=False)
    password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    role = Column(String(20), default="admin")  # admin / kiosk


class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    daily_minutes = Column(Integer, default=480)
    weekly_minutes = Column(Integer, default=2400)

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
    lunch_minutes = Column(Integer, default=60)
    day_off = Column(Boolean, default=False)

    employee = relationship("Employee", back_populates="adjustments")


# -----------------------------------------------------------------------------
# Schema upgrade
# -----------------------------------------------------------------------------
def ensure_schema_upgrades():
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("admin_users")}

    if "role" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE admin_users ADD COLUMN role VARCHAR(20) DEFAULT 'admin';"))
            conn.execute(text("UPDATE admin_users SET role='admin' WHERE role IS NULL;"))


def ensure_db():
    Base.metadata.create_all(engine)
    ensure_schema_upgrades()


def seed_default_users_and_employees():
    admin_user = os.environ.get("ADMIN_USER", "admin")
    admin_pass = os.environ.get("ADMIN_PASS", "admin123")

    kiosk_user = os.environ.get("KIOSK_USER", "tablet")
    kiosk_pass = os.environ.get("KIOSK_PASS", "tablet123")

    default_employees = ["Luziane", "Marly", "Regina", "Sueli"]

    db = SessionLocal()
    try:
        u_admin = db.execute(select(AdminUser).where(AdminUser.username == admin_user)).scalar_one_or_none()
        if not u_admin:
            db.add(AdminUser(username=admin_user, password=admin_pass, is_active=True, role="admin"))
        else:
            if not u_admin.role:
                u_admin.role = "admin"

        u_kiosk = db.execute(select(AdminUser).where(AdminUser.username == kiosk_user)).scalar_one_or_none()
        if not u_kiosk:
            db.add(AdminUser(username=kiosk_user, password=kiosk_pass, is_active=True, role="kiosk"))
        else:
            if not u_kiosk.role:
                u_kiosk.role = "kiosk"

        for n in default_employees:
            e = db.execute(select(Employee).where(Employee.name == n)).scalar_one_or_none()
            if not e:
                db.add(Employee(name=n, daily_minutes=480, weekly_minutes=2400))

        db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# Flask app (template_folder fixed)
# -----------------------------------------------------------------------------
app = Flask(__name__, template_folder=_resolve_template_folder(), static_folder=None)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)

ensure_db()
seed_default_users_and_employees()

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
                total += int((p.at_utc - current_in).total_seconds() // 60)
            current_in = None
    return total


def worked_minutes_gross_for_day(db, emp_id: int, d_local: date) -> int:
    s_utc, e_utc = dt_range_utc_for_local_day(d_local)
    return worked_minutes_gross_in_range(db, emp_id, s_utc, e_utc)


def expected_minutes_for_day(employee: Employee, day_local: date, day_off_flag: bool) -> int:
    if day_off_flag:
        return 0
    if day_local.weekday() >= 5:
        return 0
    return int(employee.daily_minutes or 0)


def net_minutes_for_day(gross_minutes: int, lunch_minutes: int) -> int:
    return max(0, gross_minutes - max(0, lunch_minutes))


def get_day_first_in_and_last_out(db, emp_id: int, d_local: date) -> tuple[datetime | None, datetime | None]:
    s_utc, e_utc = dt_range_utc_for_local_day(d_local)
    punches = db.execute(
        select(Punch)
        .where(Punch.employee_id == emp_id)
        .where(Punch.at_utc >= s_utc)
        .where(Punch.at_utc < e_utc)
        .order_by(Punch.at_utc.asc())
    ).scalars().all()

    first_in = None
    last_out = None
    for p in punches:
        if p.kind == "IN" and first_in is None:
            first_in = p.at_utc
        if p.kind == "OUT":
            last_out = p.at_utc
    return first_in, last_out


def replace_day_punches(db, emp_id: int, d_local: date, entry_hhmm: str, exit_hhmm: str):
    s_utc, e_utc = dt_range_utc_for_local_day(d_local)

    db.execute(
        delete(Punch)
        .where(Punch.employee_id == emp_id)
        .where(Punch.at_utc >= s_utc)
        .where(Punch.at_utc < e_utc)
    )

    ent = parse_hhmm(entry_hhmm)
    exi = parse_hhmm(exit_hhmm)

    if ent is None and exi is None:
        return

    if ent is not None:
        in_utc = local_dt_to_utc(d_local, ent[0], ent[1])
        db.add(Punch(employee_id=emp_id, kind="IN", at_utc=in_utc))

    if ent is not None and exi is not None:
        out_utc = local_dt_to_utc(d_local, exi[0], exi[1])
        if out_utc > local_dt_to_utc(d_local, ent[0], ent[1]):
            db.add(Punch(employee_id=emp_id, kind="OUT", at_utc=out_utc))


# -----------------------------------------------------------------------------
# Routes (mínimas para week + login)
# -----------------------------------------------------------------------------
@app.get("/setup")
def setup():
    ensure_db()
    seed_default_users_and_employees()
    return {"ok": True, "message": "DB pronta. Use /login."}


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
        login_user(u, remember=True)
        return redirect(url_for("week"))
    finally:
        db.close()


@app.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.get("/week")
@login_required
@role_required("admin")
def week():
    db = SessionLocal()
    try:
        employees = db.execute(select(Employee).order_by(Employee.name.asc())).scalars().all()
        if not employees:
            flash("Nenhuma funcionária cadastrada.", "error")
            return redirect(url_for("login"))

        emp_id = request.args.get("employee_id")
        selected_emp = db.get(Employee, int(emp_id)) if emp_id else employees[0]
        if not selected_emp:
            selected_emp = employees[0]

        today = datetime.now(APP_TZ).date()
        ws = parse_date(request.args.get("week_start") or "") or week_start(today)
        ws = week_start(ws)
        we = ws + timedelta(days=6)

        days = []
        total_net = 0
        total_expected = 0

        d = ws
        while d <= we:
            adj = get_or_create_adjustment(db, selected_emp.id, d)
            gross = worked_minutes_gross_for_day(db, selected_emp.id, d)
            lunch_effective = int(adj.lunch_minutes or 0) if gross > 0 else 0
            net = net_minutes_for_day(gross, lunch_effective)
            exp = expected_minutes_for_day(selected_emp, d, bool(adj.day_off))
            bal = net - exp

            first_in, last_out = get_day_first_in_and_last_out(db, selected_emp.id, d)
            in_local = to_local(first_in) if first_in else None
            out_local = to_local(last_out) if last_out else None

            days.append({
                "date": d,
                "label": ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"][d.weekday()],
                "entry": in_local.strftime("%H:%M") if in_local else "",
                "exit": out_local.strftime("%H:%M") if out_local else "",
                "lunch_minutes": int(adj.lunch_minutes or 0),
                "day_off": bool(adj.day_off),
                "worked": minutes_to_hhmm(net),
                "balance": minutes_to_hhmm(bal),
            })

            total_net += net
            total_expected += exp
            d += timedelta(days=1)

        week_balance = total_net - total_expected

        rate = parse_money(request.args.get("rate") or "")
        extra_minutes = max(0, week_balance)
        total_pay = (extra_minutes / 60.0) * rate if rate > 0 else 0.0

        db.commit()
        return render_template(
            "week.html",
            employees=employees,
            selected_emp=selected_emp,
            week_start_date=ws,
            week_end_date=we,
            days=days,
            total_net=minutes_to_hhmm(total_net),
            week_balance=minutes_to_hhmm(week_balance),
            rate=str(rate).rstrip("0").rstrip(".") if rate else "",
            total_pay=f"{total_pay:.2f}".replace(".", ","),
        )
    finally:
        db.close()


@app.post("/week/save")
@login_required
@role_required("admin")
def week_save():
    emp_id = int(request.form.get("employee_id"))
    ws = parse_date(request.form.get("week_start")) or week_start(datetime.now(APP_TZ).date())
    ws = week_start(ws)
    we = ws + timedelta(days=6)

    db = SessionLocal()
    try:
        d = ws
        while d <= we:
            key = d.strftime("%Y-%m-%d")
            entry = request.form.get(f"entry_{key}", "")
            exit_ = request.form.get(f"exit_{key}", "")
            lunch = request.form.get(f"lunch_{key}", "60")
            day_off = request.form.get(f"off_{key}") == "on"

            replace_day_punches(db, emp_id, d, entry, exit_)

            adj = get_or_create_adjustment(db, emp_id, d)
            adj.lunch_minutes = parse_lunch(lunch)
            adj.day_off = bool(day_off)

            d += timedelta(days=1)

        db.commit()
        flash("Semana salva com sucesso.", "success")
        return redirect(url_for("week", employee_id=emp_id, week_start=ws.strftime("%Y-%m-%d")))
    finally:
        db.close()


@app.post("/week/reset")
@login_required
@role_required("admin")
def week_reset():
    emp_id = int(request.form.get("employee_id"))
    ws = parse_date(request.form.get("week_start")) or week_start(datetime.now(APP_TZ).date())
    ws = week_start(ws)
    we = ws + timedelta(days=6)

    db = SessionLocal()
    try:
        d = ws
        while d <= we:
            s_utc, e_utc = dt_range_utc_for_local_day(d)
            db.execute(
                delete(Punch)
                .where(Punch.employee_id == emp_id)
                .where(Punch.at_utc >= s_utc)
                .where(Punch.at_utc < e_utc)
            )
            key = d.strftime("%Y-%m-%d")
            adj = db.execute(
                select(DailyAdjustment)
                .where(DailyAdjustment.employee_id == emp_id)
                .where(DailyAdjustment.day_local == key)
            ).scalar_one_or_none()
            if adj:
                adj.lunch_minutes = 60
                adj.day_off = False
            d += timedelta(days=1)

        db.commit()
        flash("Semana zerada.", "success")
        return redirect(url_for("week", employee_id=emp_id, week_start=ws.strftime("%Y-%m-%d")))
    finally:
        db.close()


@app.errorhandler(403)
def forbidden(_):
    return "Acesso negado.", 403


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
