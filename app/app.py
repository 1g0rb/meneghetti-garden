import os
from datetime import date, datetime, timedelta
from functools import wraps
from datetime import time
import calendar
import json
import uuid
from typing import Tuple
from flask import Flask, request, redirect, url_for
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

from sqlalchemy import (
    create_engine, text, Integer, String, Date, DateTime, Numeric, Boolean, ForeignKey
)
from sqlalchemy.orm import (
    declarative_base, relationship, Session, mapped_column, joinedload
)
from sqlalchemy.exc import ArgumentError

# ---------------- App + DB ----------------
load_dotenv()

DEFAULT_LOCAL_DATABASE_URL = "postgresql+psycopg2://garden:gardenpass@localhost:5432/garden"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["JSON_AS_ASCII"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True

DATABASE_URL = os.environ.get("DATABASE_URL") or DEFAULT_LOCAL_DATABASE_URL

try:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
except ArgumentError as exc:
    raise RuntimeError(
        "DATABASE_URL nije ispravan. Postavi ga u okruzenju ili koristi docker-compose setup."
    ) from exc


@app.after_request
def ensure_utf8_html_response(response):
    if response.mimetype == "text/html":
        response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response

Base = declarative_base()

# ---------------- Auth (Flask-Login) ----------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

VALID_USER_ROLES = {"admin", "kitchen", "worker"}


def home_endpoint_for_role(role: str) -> str:
    if role == "admin":
        return "admin_dashboard"
    if role == "worker":
        return "garden_worker_dashboard"
    return "kitchen_dashboard"


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if getattr(current_user, "role", None) != "admin":
            return redirect(url_for(home_endpoint_for_role(getattr(current_user, "role", None))))
        return fn(*args, **kwargs)
    return wrapper


def kitchen_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if getattr(current_user, "role", None) not in {"admin", "kitchen"}:
            return redirect(url_for(home_endpoint_for_role(getattr(current_user, "role", None))))
        return fn(*args, **kwargs)
    return wrapper


def worker_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if getattr(current_user, "role", None) not in {"admin", "worker"}:
            return redirect(url_for(home_endpoint_for_role(getattr(current_user, "role", None))))
        return fn(*args, **kwargs)
    return wrapper


# ---------------- Models ----------------
class User(Base, UserMixin):
    __tablename__ = "users"
    id = mapped_column(Integer, primary_key=True)
    username = mapped_column(String(80), unique=True, nullable=False)
    password_hash = mapped_column(String(255), nullable=False)
    role = mapped_column(String(20), nullable=False, default="kitchen")  # admin / kitchen / worker
    active = mapped_column(Boolean, nullable=False, default=True)

    def get_id(self):
        return str(self.id)


class Crop(Base):
    __tablename__ = "crops"
    id = mapped_column(Integer, primary_key=True)
    name = mapped_column(String(120), nullable=False, unique=True)
    name_hr = mapped_column(String(120), nullable=True)
    name_en = mapped_column(String(120), nullable=True)
    category = mapped_column(String(60), nullable=True)
    unit = mapped_column(String(20), nullable=False, default="kg")  # kg/kom/vezica
    active = mapped_column(Boolean, nullable=False, default=True)


class Availability(Base):
    __tablename__ = "availability"
    id = mapped_column(Integer, primary_key=True)
    date = mapped_column(Date, nullable=False, default=date.today)
    crop_id = mapped_column(ForeignKey("crops.id"), nullable=False)
    qty = mapped_column(Numeric(10, 2), nullable=False, default=0)
    note = mapped_column(String(255), nullable=True)

    crop = relationship("Crop")


class Harvest(Base):
    __tablename__ = "harvests"
    id = mapped_column(Integer, primary_key=True)
    datetime = mapped_column(DateTime, nullable=False, default=datetime.now)
    crop_id = mapped_column(ForeignKey("crops.id"), nullable=False)
    qty = mapped_column(Numeric(10, 2), nullable=False, default=0)
    destination = mapped_column(String(30), nullable=False, default="kitchen")  # kitchen/staff/waste/other
    note = mapped_column(String(255), nullable=True)

    crop = relationship("Crop")

class DayLock(Base):
    __tablename__ = "day_locks"
    id = mapped_column(Integer, primary_key=True)
    day = mapped_column(Date, unique=True, nullable=False)
    locked = mapped_column(Boolean, nullable=False, default=True)

class KitchenRequest(Base):
    __tablename__ = "kitchen_requests"
    id = mapped_column(Integer, primary_key=True)

    created_at = mapped_column(DateTime, nullable=False, default=datetime.now)
    requested_for = mapped_column(Date, nullable=False)  # npr. sutra
    request_group_id = mapped_column(String(36), nullable=True)
    crop_id = mapped_column(ForeignKey("crops.id"), nullable=False)
    unit = mapped_column(String(20), nullable=True)

    qty = mapped_column(Numeric(10, 2), nullable=False, default=0)
    note = mapped_column(String(255), nullable=True)

    # open/approved/rejected/applied
    status = mapped_column(String(20), nullable=False, default="open")

    created_by_user_id = mapped_column(ForeignKey("users.id"), nullable=True)
    created_by = relationship("User", foreign_keys=[created_by_user_id])


    # NOVO: audit za "primijenjeno u dostupnost"
    applied_at = mapped_column(DateTime, nullable=True)
    applied_by_user_id = mapped_column(ForeignKey("users.id"), nullable=True)
    applied_by = relationship("User", foreign_keys=[applied_by_user_id])
    assigned_to = mapped_column(String(120), nullable=True)
    received_at = mapped_column(DateTime, nullable=True)
    delivered_at = mapped_column(DateTime, nullable=True)

    crop = relationship("Crop")


def init_db():
    Base.metadata.create_all(engine)

def is_day_locked(d: date) -> bool:
    with Session(engine) as s:
        row = s.query(DayLock).filter(DayLock.day == d, DayLock.locked == True).first()
        return row is not None


def ensure_admin_user():
    admin_user = os.environ.get("ADMIN_USERNAME", "igor")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "promijeni-ovo")

    with Session(engine) as s:
        exists = s.query(User).filter(User.username == admin_user).first()
        if not exists:
            s.add(User(
                username=admin_user,
                password_hash=generate_password_hash(admin_pass),
                role="admin",
                active=True
            ))
            s.commit()


def seed_crop_name_en(conn):
    defaults = {
        "Krumpir": "Potato",
        "Salata": "Lettuce",
        "Patlidžan": "Eggplant",
        "Rotkvica": "Radish",
        "Lubenica": "Watermelon",
    }
    for name_hr, name_en in defaults.items():
        conn.execute(
            text("""
            UPDATE crops
            SET name_en = :name_en
            WHERE name_hr = :name_hr
              AND (name_en IS NULL OR name_en = '')
            """),
            {"name_hr": name_hr, "name_en": name_en},
        )


# init on startup
def ensure_columns():
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            crop_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(crops)")).fetchall()
            }
            if "name_hr" not in crop_columns:
                conn.execute(text("ALTER TABLE crops ADD COLUMN name_hr VARCHAR(120)"))
            if "name_en" not in crop_columns:
                conn.execute(text("ALTER TABLE crops ADD COLUMN name_en VARCHAR(120)"))
            conn.execute(text("""
            UPDATE crops
            SET name_hr = name
            WHERE name_hr IS NULL OR name_hr = ''
            """))
            seed_crop_name_en(conn)
        else:
            conn.execute(text("""
            ALTER TABLE crops
            ADD COLUMN IF NOT EXISTS name_hr VARCHAR(120)
            """))
            conn.execute(text("""
            ALTER TABLE crops
            ADD COLUMN IF NOT EXISTS name_en VARCHAR(120)
            """))
            conn.execute(text("""
            UPDATE crops
            SET name_hr = name
            WHERE name_hr IS NULL OR name_hr = ''
            """))
            seed_crop_name_en(conn)

        conn.execute(text("""
        ALTER TABLE kitchen_requests
        ADD COLUMN IF NOT EXISTS request_group_id VARCHAR(36)
        """))
        conn.execute(text("""
        ALTER TABLE kitchen_requests
        ADD COLUMN IF NOT EXISTS unit VARCHAR(20)
        """))
        conn.execute(text("""
        ALTER TABLE kitchen_requests
        ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER
        """))

        conn.execute(text("""
        ALTER TABLE kitchen_requests
        ADD COLUMN IF NOT EXISTS applied_at TIMESTAMP
        """))
        conn.execute(text("""
        ALTER TABLE kitchen_requests
        ADD COLUMN IF NOT EXISTS applied_by_user_id INTEGER
        """))
        if engine.dialect.name == "sqlite":
            existing_columns = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(kitchen_requests)")).fetchall()
            }
            if "assigned_to" not in existing_columns:
                conn.execute(text("ALTER TABLE kitchen_requests ADD COLUMN assigned_to VARCHAR(120)"))
            if "received_at" not in existing_columns:
                conn.execute(text("ALTER TABLE kitchen_requests ADD COLUMN received_at TIMESTAMP"))
            if "delivered_at" not in existing_columns:
                conn.execute(text("ALTER TABLE kitchen_requests ADD COLUMN delivered_at TIMESTAMP"))
        else:
            conn.execute(text("""
            ALTER TABLE kitchen_requests
            ADD COLUMN IF NOT EXISTS assigned_to VARCHAR(120)
            """))
            conn.execute(text("""
            ALTER TABLE kitchen_requests
            ADD COLUMN IF NOT EXISTS received_at TIMESTAMP
            """))
            conn.execute(text("""
            ALTER TABLE kitchen_requests
            ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP
            """))
        conn.execute(text("""
        UPDATE kitchen_requests kr
        SET unit = c.unit
        FROM crops c
        WHERE kr.crop_id = c.id
          AND (kr.unit IS NULL OR kr.unit = '')
        """))
        conn.execute(text("""
        UPDATE kitchen_requests
        SET request_group_id = id::text
        WHERE request_group_id IS NULL OR request_group_id = ''
        """))

        conn.commit()

def ensure_expenses_table():
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS expenses (
              id SERIAL PRIMARY KEY,
              date DATE NOT NULL,
              category VARCHAR(40) NOT NULL,
              item VARCHAR(200) NOT NULL,
              amount_eur NUMERIC(12,2) NOT NULL,
              note TEXT,
              created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """))
        conn.commit()

        





@login_manager.user_loader
def load_user(user_id: str):
    try:
        parsed_user_id = int(user_id)
    except (TypeError, ValueError):
        return None

    with Session(engine) as s:
        return s.get(User, parsed_user_id)


# ---------------- UI helpers ----------------

REQUEST_STATUS_LABELS = {
    "zaprimljeno": "Zaprimljeno",
    "u_pripremi": "U pripremi",
    "dostavljeno": "Dostavljeno",
    "nije_moguce": "Nije moguće",
    "open": "Otvoreno",
    "approved": "U radu",
    "applied": "Gotovo",
    "rejected": "Odbijeno",
}

STATUS_CANONICAL = {
    "open": "zaprimljeno",
    "approved": "u_pripremi",
    "applied": "dostavljeno",
    "rejected": "nije_moguce",
}


def canonical_request_status(status: str) -> str:
    normalized = (status or "zaprimljeno").lower()
    return STATUS_CANONICAL.get(normalized, normalized)


def html_message_page(title: str, message: str, back_href: str, back_label: str = "Natrag") -> str:
    body = f"""
    <div class="card">
      <p>{message}</p>
      <a class="pill" href="{back_href}">{back_label}</a>
    </div>
    """
    return html_page(title, body)


def parse_int_field(raw_value, field_label: str, back_href: str):
    try:
        return int(raw_value), None
    except (TypeError, ValueError):
        return None, html_message_page("Greška", f"Polje '{field_label}' nije ispravno.", back_href)


def parse_float_field(raw_value, field_label: str, back_href: str, min_value: float = 0):
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None, html_message_page("Greška", f"Polje '{field_label}' mora biti broj.", back_href)

    if value < min_value:
        return None, html_message_page("Greška", f"Polje '{field_label}' nije u dozvoljenom rasponu.", back_href)

    return value, None


def parse_date_field(raw_value, field_label: str, back_href: str):
    try:
        return date.fromisoformat((raw_value or "").strip()), None
    except (TypeError, ValueError):
        return None, html_message_page("Greška", f"Polje '{field_label}' nije ispravan datum.", back_href)


def request_status_badge(status: str) -> str:
    normalized = canonical_request_status(status)
    label = REQUEST_STATUS_LABELS.get(normalized, normalized.upper())
    return f'<span class="status {normalized}">{label}</span>'


def request_transition_allowed(current_status: str, action: str) -> bool:
    normalized = canonical_request_status(current_status)
    allowed = {
        "approve": {"zaprimljeno"},
        "reject": {"zaprimljeno", "u_pripremi"},
        "apply": {"zaprimljeno", "u_pripremi"},
    }
    return normalized in allowed.get(action, set())


def html_page(title: str, body: str) -> str:
    nav = ""
    topbar_class = "topbar"
    brand_subtitle = "v2.0 — operativa • berba • troškovi • zahtjevi"
    role_badge = f'<div class="pill pill--accent">{current_user.role if current_user.is_authenticated else "guest"}</div>'
    pagehead_html = f"""
        <div class="pagehead">
          <h2>{title}</h2>
          <div class="hint">Profinjena jednostavnost, bez šarenila.</div>
        </div>
    """
    container_class = "container"

    if current_user.is_authenticated:
        if current_user.role == "admin":
            nav = """
            <nav class="nav">
              <a class="nav__link" href="/admin">Dashboard</a>
              <a class="nav__link" href="/garden-worker">Worker</a>
              <a class="nav__link" href="/admin/users">Korisnici</a>
              <a class="nav__link" href="/admin/crops">Kulture</a>
              <a class="nav__link" href="/admin/harvest">Berba</a>
              <a class="nav__link" href="/admin/expenses">Troškovi</a>
              <a class="nav__link" href="/admin/report">Izvještaj</a>
              <a class="nav__link" href="/admin/requests">Zahtjevi</a>
              <span class="nav__spacer"></span>
              <a class="nav__link nav__link--muted" href="/logout">Logout</a>
            </nav>
            """
        else:
            topbar_class = "topbar topbar--kitchen"
            if title == "Kuhinja":
                topbar_class += " topbar--hidden"
                container_class = "container container--kitchen container--kitchen-home"
            elif title in ("Danas", "Sutra"):
                container_class = "container container--kitchen container--kitchen-dayview"
            else:
                container_class = "container container--kitchen"
            brand_subtitle = ""
            role_badge = ""
            pagehead_html = ""
            nav = """
            <nav class="nav nav--kitchen">
              <span class="nav__spacer"></span>
              <a class="nav__link nav__link--muted nav__link--quiet" href="/logout">Odjava</a>
            </nav>
            """

    return f"""
    <!doctype html>
    <html lang="hr">
    <head>
      <meta charset="UTF-8"/>
      <meta name="viewport" content="width=device-width, initial-scale=1"/>

      <title>{title}</title>

      <style>
        /* ---------------- Theme (Meneghetti-ish: warm, calm, premium) ---------------- */
        :root {{
          --bg: #fbfaf7;
          --card: #ffffff;
          --ink: #1b1b1b;
          --muted: #5f5f5f;
          --line: rgba(0,0,0,0.10);

          --accent: #1f3b2d;         /* deep olive */
          --accent-2: #8a6b3b;       /* warm gold (subtle) */
          --accent-soft: rgba(31,59,45,0.10);

          --danger: #8b2d2d;
          --danger-soft: rgba(139,45,45,0.10);

          --radius: 14px;
          --radius-sm: 10px;
          --shadow: 0 10px 24px rgba(0,0,0,0.06);
          --shadow-sm: 0 6px 14px rgba(0,0,0,0.06);

          --maxw: 980px;
        }}

        /* ---------------- Base ---------------- */
        * {{ box-sizing: border-box; }}
        html, body {{
          min-height: 100%;
        }}
        body {{
          margin: 0;
          background: transparent !important;
          color: var(--ink);
          font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
          line-height: 1.45;
        }}

        a {{ color: inherit; text-decoration: none; }}
        a:hover {{ text-decoration: none; }}

        .container {{
          max-width: var(--maxw);
          margin: 0 auto;
          padding: 18px;
        }}

        /* ---------------- Header ---------------- */
        .topbar {{
          position: sticky;
          top: 0;
          z-index: 30;
          background: rgba(251,250,247,0.85);
          backdrop-filter: blur(10px);
          border-bottom: 1px solid var(--line);
        }}

        .brand {{
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 12px;
          padding: 14px 18px 10px 18px;
          max-width: var(--maxw);
          margin: 0 auto;
        }}

        .brand__left {{
          display:flex;
          flex-direction: column;
          gap: 2px;
        }}

        .brand__title {{
          font-family: ui-serif, Georgia, "Times New Roman", Times, serif;
          letter-spacing: 0.2px;
          font-weight: 700;
          font-size: 20px;
          margin: 0;
        }}

        .brand__subtitle {{
          color: var(--muted);
          font-size: 12.5px;
          margin: 0;
        }}

        .topbar--kitchen {{
          position: static;
          background: transparent;
          backdrop-filter: none;
          border-bottom: 0;
          padding-top: 10px;
        }}

        .topbar--hidden {{
          display: none;
        }}

        .topbar--kitchen .brand {{
          align-items: center;
          justify-content: flex-start;
          padding: 10px 24px 0 24px;
          max-width: 430px;
        }}

        .topbar--kitchen .brand__title {{
          font-size: 11px;
          letter-spacing: 0.16em;
          text-transform: uppercase;
          color: rgba(31,59,45,0.78);
        }}

        .container--kitchen {{
          max-width: 430px;
          padding: 0 24px 18px 24px;
        }}

        .container--kitchen-home {{
          max-width: none;
          padding: 0;
          background: transparent !important;
        }}

        /* ---------------- Nav ---------------- */
        .nav {{
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 10px;
          padding: 0 18px 14px 18px;
          max-width: var(--maxw);
          margin: 0 auto;
        }}

        .nav__link {{
          padding: 8px 10px;
          border-radius: 999px;
          border: 1px solid transparent;
          color: rgba(0,0,0,0.78);
          font-weight: 650;
          font-size: 13px;
        }}

        .nav__link:hover {{
          background: rgba(0,0,0,0.03);
          border-color: var(--line);
        }}

        .nav__link--active {{
          background: var(--accent-soft);
          border-color: rgba(31,59,45,0.20);
          color: var(--accent);
        }}

        .nav__link--muted {{
          color: rgba(0,0,0,0.55);
        }}

        .nav--kitchen {{
          justify-content: flex-end;
          gap: 0;
          padding: 0 24px 0 24px;
          max-width: 430px;
          margin-top: -12px;
        }}
        .container--kitchen-dayview .footer {{
          display: none;
        }}

        .nav__link--quiet {{
          padding: 2px 0;
          border: 0;
          font-size: 11px;
          font-weight: 600;
          color: rgba(27,27,27,0.62);
          letter-spacing: 0.02em;
        }}

        .nav__link--quiet:hover {{
          background: transparent;
          border-color: transparent;
          color: rgba(27,27,27,0.72);
        }}

        .nav__spacer {{ flex: 1; }}

        /* ---------------- Page title ---------------- */
        .pagehead {{
          display:flex;
          align-items: flex-end;
          justify-content: space-between;
          gap: 12px;
          margin: 16px 0 10px 0;
        }}

        .pagehead h2 {{
          margin: 0;
          font-family: ui-serif, Georgia, "Times New Roman", Times, serif;
          font-size: 26px;
          letter-spacing: 0.2px;
        }}

        .pagehead .hint {{
          color: var(--muted);
          font-size: 13px;
        }}

        /* ---------------- Cards ---------------- */
        .card {{
          background: var(--card);
          border: 1px solid var(--line);
          border-radius: var(--radius);
          padding: 16px 16px;
          margin: 14px 0;
          box-shadow: var(--shadow-sm);
        }}

        .card h3 {{
          margin: 0 0 10px 0;
          font-size: 16px;
          letter-spacing: 0.2px;
          color: var(--accent);
        }}

        .muted {{ color: var(--muted); }}
        .hr {{ border: 0; border-top: 1px solid var(--line); margin: 12px 0; }}

        /* ---------------- Pills / badges ---------------- */
        .pill {{
          display:inline-flex;
          align-items:center;
          gap: 8px;
          padding: 7px 12px;
          border-radius: 999px;
          border: 1px solid var(--line);
          background: rgba(255,255,255,0.7);
          font-size: 12.5px;
          font-weight: 650;
          color: rgba(0,0,0,0.75);
        }}

        .pill--accent {{
          background: var(--accent-soft);
          border-color: rgba(31,59,45,0.20);
          color: var(--accent);
        }}

        .pill--danger {{
          background: var(--danger-soft);
          border-color: rgba(139,45,45,0.20);
          color: var(--danger);
        }}

        /* ---------------- Buttons ---------------- */
        button, .btn {{
          display:inline-flex;
          align-items:center;
          justify-content:center;
          gap: 8px;
          padding: 10px 14px;
          border-radius: 12px;
          border: 1px solid rgba(31,59,45,0.35);
          background: var(--accent);
          color: #fff;
          font-weight: 750;
          cursor: pointer;
          text-decoration: none;
        }}

        button:hover, .btn:hover {{
          filter: brightness(0.95);
        }}

        .btn--ghost {{
          background: transparent;
          color: var(--accent);
          border-color: rgba(31,59,45,0.25);
        }}

        .btn--danger {{
          background: var(--danger);
          border-color: rgba(139,45,45,0.40);
        }}

        button[disabled] {{
          opacity: 0.55;
          cursor: not-allowed;
          filter: none;
        }}

        /* ---------------- Forms ---------------- */
        label {{
          font-weight: 700;
          font-size: 12.5px;
          letter-spacing: 0.2px;
          color: rgba(0,0,0,0.72);
        }}

        input, select, textarea {{
          width: 100%;
          padding: 11px 12px;
          border-radius: 12px;
          border: 1px solid var(--line);
          background: rgba(255,255,255,0.85);
          outline: none;
          font-size: 14.5px;
        }}

        input:focus, select:focus, textarea:focus {{
          border-color: rgba(31,59,45,0.35);
          box-shadow: 0 0 0 4px rgba(31,59,45,0.10);
        }}

        .formrow {{
          display: grid;
          grid-template-columns: 1fr;
          gap: 12px;
        }}

        @media (min-width: 760px) {{
          .formrow--2 {{ grid-template-columns: 1fr 1fr; }}
          .formrow--3 {{ grid-template-columns: 1fr 1fr 1fr; }}
        }}

        .actions {{
          display:flex;
          gap:10px;
          flex-wrap:wrap;
          align-items:center;
        }}

        .actions form {{ margin:0; }}
        /*------------------grid2-------------------*/
        .grid2 {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 14px;
        }}
        @media (min-width: 760px) {{
            .grid2 {{
            grid-template-columns: 1fr 1fr;
            }}
        }}

        /* ---------------- Tables ---------------- */
        table {{
          width: 100%;
          border-collapse: collapse;
          overflow: hidden;
          border-radius: 12px;
        }}

        .tablewrap {{
          width: 100%;
          overflow-x: auto;
          border-radius: 14px;
          border: 1px solid rgba(31,59,45,0.06);
          background: rgba(255,255,255,0.55);
        }}

        th {{
          text-align: left;
          font-size: 12px;
          letter-spacing: 0.6px;
          text-transform: uppercase;
          color: rgba(0,0,0,0.55);
          padding: 13px 12px;
          border-bottom: 1px solid var(--line);
          background: rgba(0,0,0,0.02);
        }}

        td {{
          padding: 14px 12px;
          border-bottom: 1px solid rgba(0,0,0,0.06);
          color: rgba(0,0,0,0.82);
          vertical-align: top;
          line-height: 1.45;
        }}

        tr:hover td {{
          background: rgba(0,0,0,0.015);
        }}

        /* ---------------- Status chips (your existing mapping fits) ---------------- */
        .status {{
          display:inline-flex;
          align-items:center;
          gap: 8px;
          padding: 6px 10px;
          border-radius: 999px;
          font-weight: 800;
          font-size: 12px;
          border: 1px solid var(--line);
          background: rgba(255,255,255,0.7);
        }}
        .status.open {{ background:#fff6df; border-color:#f0d28a; color:#7b5900; }}
        .status.approved {{ background:#e7f0ff; border-color:#b6d0ff; color:#204a87; }}
        .status.applied {{ background:#eaf7ea; border-color:#bfe6bf; color:#255d25; }}
        .status.rejected {{ background:#ffe7e7; border-color:#f0b3b3; color:#8b2d2d; }}
        .status.zaprimljeno {{ background:#fff6df; border-color:#f0d28a; color:#7b5900; }}
        .status.u_pripremi {{ background:#e7f0ff; border-color:#b6d0ff; color:#204a87; }}
        .status.dostavljeno {{ background:#eaf7ea; border-color:#bfe6bf; color:#255d25; }}
        .status.nije_moguce {{ background:#ffe7e7; border-color:#f0b3b3; color:#8b2d2d; }}

        .availability-quick-form {{
          display: grid;
          gap: 12px;
          max-width: 720px;
        }}
        .availability-quick-form__grid {{
          display: grid;
          grid-template-columns: minmax(0, 1.15fr) minmax(150px, 0.85fr);
          gap: 12px;
          align-items: end;
        }}
        .availability-quick-form__left {{
          display: grid;
          grid-template-columns: minmax(120px, 0.8fr) minmax(180px, 1.2fr);
          gap: 12px;
        }}
        .availability-quick-form__field {{
          display: grid;
          gap: 6px;
        }}
        .availability-quick-form__note {{
          display: grid;
          gap: 6px;
        }}
        .availability-quick-form .actions button {{
          width: auto;
          padding: 10px 18px;
        }}
        .admin-availability-table td:nth-child(2) {{
          font-weight: 800;
          color: var(--accent);
          white-space: nowrap;
        }}
        .admin-availability-table td:first-child {{
          font-weight: 750;
        }}

        /* ---------------- Kitchen cards you already have ---------------- */
        .kgrid {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
        @media (min-width: 760px) {{ .kgrid {{ grid-template-columns: 1fr 1fr; }} }}
        .kcard {{ border: 1px solid var(--line); border-radius: var(--radius); padding: 16px; background: #fff; box-shadow: var(--shadow-sm); }}
        .krow {{ display:flex; justify-content: space-between; align-items:center; gap: 12px; }}
        .kname {{ font-size: 16px; font-weight: 800; }}
        .kqty {{ margin-top: 8px; font-size: 22px; font-weight: 850; }}
        .knote {{ margin-top: 8px; color: var(--muted); }}
        .badge {{ padding: 6px 10px; border-radius: 999px; font-weight: 850; font-size: 12px; border: 1px solid var(--line); }}
        .badge.ok {{ background: #eaf7ea; border-color: #bfe6bf; }}
        .badge.warn {{ background: #fff6df; border-color: #f0d28a; }}
        .badge.bad {{ background: #ffe7e7; border-color: #f0b3b3; }}

        /* ---------------- Tabs (your current JS works) ---------------- */
        .tabs {{ display:flex; gap:10px; margin: 10px 0 14px 0; flex-wrap: wrap; }}
        .tabbtn {{
          padding: 10px 14px;
          border: 1px solid var(--line);
          border-radius: 999px;
          background:#fff;
          font-weight: 800;
          cursor:pointer;
        }}
        .tabbtn.active {{ border-color: rgba(31,59,45,0.35); box-shadow: 0 0 0 4px rgba(31,59,45,0.08); }}
        .daywrap {{ display:none; }}
        .daywrap.active {{ display:block; }}
        /* ---------------- Tabs ---------------- */
.tabs {{
  display:flex;
  gap:10px;
  margin: 10px 0 14px 0;
  flex-wrap: wrap;
}}

.tabbtn {{
  padding: 10px 14px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background:#fff;
  font-weight: 800;
  cursor:pointer;

  /* OVO JE KLJUČ */
  color: #1f3b2d;
  font-size: 13px;
}}

.tabbtn.active {{
  border-color: rgba(31,59,45,0.35);
  box-shadow: 0 0 0 4px rgba(31,59,45,0.08);
  background: rgba(31,59,45,0.08);
}}

.daywrap {{ display:none; }}
.daywrap.active {{ display:block; }}


        /* ---------------- Clean Kitchen Home ---------------- */
        .kitchen-home-page {{
          position: relative;
          width: 100%;
          min-height: 100vh !important;
          padding: 0;
          background-image: url('/static/images/background.jpg?v=2') !important;
          background-size: cover !important;
          background-position: center top !important;
          background-repeat: no-repeat !important;
        }}
        .kitchen-home-page::before {{
          content: "";
          position: absolute;
          inset: 0;
          background: linear-gradient(to bottom, rgba(245,242,235,0.08) 0%, rgba(245,242,235,0.05) 40%, rgba(245,242,235,0.10) 100%);
          pointer-events: none;
        }}
        .kitchen-home-page > * {{
          position: relative;
          z-index: 1;
        }}
        .kitchen-home-clean {{
          position: relative;
          width: 100%;
          max-width: 390px;
          margin: 0 auto;
          min-height: 100vh;
          padding: 18px 18px 160px 18px;
          display: flex;
          flex-direction: column;
          justify-content: flex-start;
          background: transparent;
        }}
        .kitchen-home-clean__toprow {{
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 12px;
          padding: 0 2px;
          margin-bottom: 46px;
        }}
        .kitchen-home-clean__brandlockup {{
          display: inline-flex;
          align-items: center;
          gap: 10px;
          min-width: 0;
        }}
        .kitchen-home-clean__leaf-icon {{
          width: 20px;
          height: 20px;
          object-fit: contain;
          flex: 0 0 20px;
        }}
        .kitchen-home-clean__leaf-placeholder {{
          width: 20px;
          height: 20px;
          border-radius: 999px;
          border: 1px solid rgba(31,59,45,0.35);
          background: rgba(255,255,255,0.34);
          flex: 0 0 20px;
        }}
        .kitchen-home-clean__brandtext {{
          font-size: 11px;
          line-height: 1.2;
          letter-spacing: 0.11em;
          text-transform: uppercase;
          font-weight: 700;
          color: rgba(31,59,45,0.84);
        }}
        .kitchen-home-clean__logout {{
          font-size: 11px;
          line-height: 1.2;
          color: rgba(31,59,45,0.78);
          text-decoration: none;
          padding-top: 2px;
        }}
        .kitchen-home-clean__header {{
          display: flex;
          flex-direction: column;
          align-items: center;
          text-align: center;
          gap: 12px;
          max-width: 340px;
          margin: 0 auto 30px auto;
        }}
        .kitchen-home-clean__title {{
          margin: 0;
          font-family: Georgia, "Times New Roman", Times, serif;
          font-size: clamp(50px, 14vw, 60px);
          font-weight: 600;
          line-height: 0.9;
          color: #203c2d;
          letter-spacing: -0.035em;
          text-shadow: 0 1px 0 rgba(255,250,243,0.35);
        }}
        .kitchen-home-clean__subtitle {{
          margin: 0;
          max-width: 18ch;
          color: rgba(29,44,35,0.80);
          font-size: 17px;
          line-height: 1.56;
        }}
        .kitchen-home-status {{
          width: 100%;
          max-width: 358px;
          margin: 0 auto 6px auto;
          padding: 14px 16px;
          border-radius: 18px;
          border: 1px solid rgba(255,255,255,0.34);
          background: rgba(255, 250, 242, 0.70);
          box-shadow: 0 12px 22px rgba(24, 37, 28, 0.08);
          backdrop-filter: blur(8px);
          -webkit-backdrop-filter: blur(8px);
          text-align: center;
          display: grid;
          gap: 4px;
        }}
        .kitchen-home-status strong {{
          color: var(--accent);
          font-size: 14px;
          line-height: 1.35;
        }}
        .kitchen-home-status span {{
          color: rgba(27,27,27,0.68);
          font-size: 12.5px;
          line-height: 1.45;
        }}
        .kitchen-home-status--closed strong {{
          color: #8d5b26;
        }}
        .kitchen-action-group {{
          position: relative;
          display: grid;
          gap: 14px;
          width: 100%;
          max-width: 374px;
          margin: 0 auto 18px auto;
          padding: 16px;
          border-radius: 28px;
          background: rgba(255, 255, 255, 0.35);
          border: 1px solid rgba(255,255,255,0.4);
          box-shadow: 0 20px 40px rgba(0,0,0,0.15);
          backdrop-filter: blur(10px) saturate(140%);
          -webkit-backdrop-filter: blur(10px) saturate(140%);
        }}
        .kitchen-action-card {{
          display: flex;
          align-items: center;
          gap: 16px;
          width: 100%;
          min-height: 100px;
          padding: 18px 18px;
          border-radius: 22px;
          border: 1px solid rgba(31,59,45,0.08);
          background: rgba(255,253,248,0.96);
          color: var(--accent);
          text-decoration: none;
          box-shadow: none;
          transition: transform 0.15s ease, border-color 0.15s ease;
        }}
        .kitchen-action-card:hover {{
          transform: translateY(-1px);
          border-color: rgba(31,59,45,0.18);
          box-shadow: none;
        }}
        .kitchen-action-card--primary {{
          background: linear-gradient(180deg, #24553a 0%, #1b4730 100%);
          color: #fffdf8;
          border-color: rgba(24,50,34,0.24);
        }}
        .kitchen-action-card--secondary {{
          background: rgba(255,251,245,0.96);
          color: var(--accent);
        }}
        .kitchen-action-card__icon {{
          position: relative;
          width: 54px;
          height: 54px;
          border-radius: 18px;
          flex: 0 0 54px;
          background: rgba(255,248,235,0.16);
          border: 1px solid rgba(255,255,255,0.18);
        }}
        .kitchen-action-card--secondary .kitchen-action-card__icon {{
          background: rgba(31,59,45,0.08);
          border-color: rgba(31,59,45,0.08);
        }}
        .kitchen-action-card__icon::before,
        .kitchen-action-card__icon::after {{
          content: "";
          position: absolute;
          left: 50%;
          top: 50%;
          transform: translate(-50%, -50%);
        }}
        .kitchen-action-card__icon--today::before {{
          width: 18px;
          height: 18px;
          border-radius: 999px;
          background: #ffcf60;
          box-shadow: 0 0 0 6px rgba(255,207,96,0.18);
        }}
        .kitchen-action-card__icon--today::after {{
          width: 30px;
          height: 30px;
          border-radius: 999px;
          border: 2px dashed rgba(255,223,163,0.8);
        }}
        .kitchen-action-card__icon--tomorrow::before {{
          width: 24px;
          height: 12px;
          border-radius: 999px 999px 0 0;
          border: 3px solid #2a5c3f;
          border-bottom: 0;
          top: 46%;
        }}
        .kitchen-action-card__icon--tomorrow::after {{
          width: 24px;
          height: 3px;
          background: #2a5c3f;
          top: 68%;
        }}
        .kitchen-action-card__content {{
          display: flex;
          flex-direction: column;
          gap: 4px;
          min-width: 0;
        }}
        .kitchen-action-card__title {{
          font-size: 16px;
          line-height: 1.05;
          font-weight: 900;
          letter-spacing: 0.04em;
        }}
        .kitchen-action-card__hint {{
          font-size: 12px;
          line-height: 1.35;
          opacity: 0.9;
        }}
        .kitchen-info-card {{
          position: relative;
          z-index: 1;
          align-self: center;
          max-width: 286px;
          padding: 12px 16px;
          border-radius: 18px;
          border: 1px solid rgba(31,59,45,0.05);
          background: rgba(250,245,236,0.92);
          color: rgba(27,27,27,0.62);
          font-size: 13px;
          line-height: 1.42;
          text-align: center;
          box-shadow: 0 8px 18px rgba(29,45,35,0.05);
        }}
        .kitchen-home-clean__basket {{
          position: absolute;
          left: 50%;
          bottom: -8px;
          transform: translateX(-50%);
          width: min(92%, 330px);
          z-index: 0;
          pointer-events: none;
          filter: drop-shadow(0 20px 30px rgba(22,33,25,0.22));
        }}
        .kitchen-day-view, .kitchen-success-view {{
          max-width: 560px;
          margin: 0 auto;
          display: grid;
          gap: 16px;
        }}
        .kitchen-back-link {{
          display: inline-flex;
          align-items: center;
          gap: 8px;
          width: fit-content;
          color: rgba(31,59,45,0.82);
          font-size: 14px;
          font-weight: 700;
          text-decoration: none;
          margin-top: 2px;
        }}
        .kitchen-day-view__header {{
          display: flex;
          align-items: flex-end;
          justify-content: center;
          padding-top: 2px;
          text-align: center;
        }}
        .kitchen-day-view__header > div {{
          width: 100%;
        }}
        .kitchen-day-view__title {{
          margin: 0;
          font-family: Georgia, "Times New Roman", Times, serif;
          font-size: clamp(34px, 8vw, 44px);
          font-weight: 600;
          line-height: 0.94;
          color: var(--accent);
          letter-spacing: -0.03em;
        }}
        .kitchen-day-view__date {{
          margin: 9px 0 0 0;
          color: rgba(27,27,27,0.70);
          font-size: 13px;
          font-weight: 500;
          line-height: 1.35;
        }}
        .kitchen-day-hero {{
          border-radius: 24px;
          overflow: hidden;
          box-shadow: 0 16px 32px rgba(29,45,35,0.12);
        }}
        .kitchen-day-hero__image {{
          display: block;
          width: 100%;
          height: 150px;
          object-fit: cover;
          object-position: center center;
        }}
        .kitchen-status-card {{
          padding: 18px 18px;
          border-radius: 16px;
          border: 1px solid rgba(31,59,45,0.10);
          box-shadow: 0 8px 18px rgba(29,45,35,0.06);
          text-align: center;
        }}
        .kitchen-status-card.is-open {{
          background: linear-gradient(180deg, #eef7e7 0%, #e3f0dc 100%);
          color: #206036;
        }}
        .kitchen-status-card.is-closed {{
          background: linear-gradient(180deg, #f8efe1 0%, #f3e7d5 100%);
          color: #94652a;
        }}
        .kitchen-status-card__title {{
          font-size: 25px;
          font-weight: 800;
          letter-spacing: -0.02em;
        }}
        .kitchen-status-card__hint {{
          margin-top: 6px;
          font-size: 14px;
          line-height: 1.45;
          opacity: 0.88;
        }}
        .kitchen-section-card {{
          background: #fffdf9;
          border: 1px solid rgba(31,59,45,0.06);
          border-radius: 22px;
          padding: 18px;
          box-shadow: 0 8px 18px rgba(29,45,35,0.04);
        }}
        .kitchen-section-card__head {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          margin-bottom: 14px;
        }}
        .kitchen-section-card__head h3 {{
          margin: 0;
          font-size: 17px;
          letter-spacing: 0.02em;
          color: var(--accent);
        }}
        .kitchen-section-card__chip {{
          display: inline-flex;
          padding: 4px 9px;
          border-radius: 999px;
          background: rgba(31,59,45,0.05);
          color: rgba(31,59,45,0.74);
          font-size: 10.5px;
          font-weight: 700;
        }}
        .offer-list {{
          display: grid;
          gap: 8px;
        }}
        .offer-item {{
          min-height: 58px;
          padding: 11px 12px;
          border-radius: 14px;
          background: linear-gradient(180deg, #fbf8f1 0%, #f6f0e6 100%);
          border: 1px solid rgba(31,59,45,0.06);
          box-shadow: 0 4px 10px rgba(28,39,31,0.025);
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          align-items: center;
          justify-content: space-between;
          gap: 10px;
        }}
        .offer-item__left {{
          display: flex;
          align-items: center;
          gap: 10px;
          min-width: 0;
        }}
        .offer-item__icon, .recent-request__icon {{
          width: 30px;
          height: 30px;
          object-fit: contain;
          flex: 0 0 30px;
          padding: 4px;
          border-radius: 10px;
          background: rgba(151, 177, 122, 0.14);
        }}
        .offer-item__name {{
          font-weight: 800;
          color: #1f3b2d;
          line-height: 1.2;
        }}
        .offer-item__qty {{
          color: rgba(27,27,27,0.58);
          font-size: 12px;
          font-weight: 650;
          line-height: 1.35;
        }}
        .offer-item__note {{
          color: rgba(27,27,27,0.62);
          font-size: 12.5px;
          line-height: 1.45;
          grid-column: 1 / -1;
          padding-left: 40px;
        }}
        .offer-item__toggle,
        .kitchen-request-summary__actions button {{
          border: 1px solid rgba(31,59,45,0.14);
          background: rgba(255,255,255,0.78);
          color: var(--accent);
          border-radius: 999px;
          padding: 10px 14px;
          font-size: 13px;
          font-weight: 800;
          box-shadow: none;
          white-space: nowrap;
        }}
        .offer-item__toggle.is-added,
        .offer-item__toggle[disabled] {{
          opacity: 0.62;
          background: rgba(231,241,224,0.92);
        }}
        .kitchen-empty--soft {{
          display: flex;
          flex-direction: column;
          gap: 5px;
          padding: 18px 16px;
          border-radius: 16px;
          background: linear-gradient(180deg, #eef5e4 0%, #e8f0dc 100%);
          border: 1px solid rgba(116,145,89,0.16);
          color: rgba(27,27,27,0.70);
          text-align: center;
        }}
        .kitchen-form {{
          display: grid;
          gap: 16px;
        }}
        .kitchen-form h3 {{
          margin: 0 0 2px 0;
          color: var(--accent);
          font-size: 18px;
          letter-spacing: 0.01em;
        }}
        .kitchen-form label {{
          font-size: 12px;
          color: rgba(27,27,27,0.62);
          font-weight: 700;
        }}
        .kitchen-form select,
        .kitchen-form input,
        .kitchen-form textarea {{
          border-radius: 12px;
          border: 1px solid rgba(31,59,45,0.10);
          background: #fafaf8;
        }}
        .kitchen-form__row {{
          display: grid;
          grid-template-columns: 1.2fr 0.8fr;
          gap: 12px;
          align-items: end;
        }}
        .kitchen-form__row--builder {{
          grid-template-columns: 1.15fr 0.85fr;
        }}
        .kitchen-form__unit {{
          display: flex;
          align-items: center;
          min-height: 46px;
          padding: 0 12px;
          border-radius: 12px;
          border: 1px solid rgba(31,59,45,0.10);
          background: #fafaf8;
          color: rgba(27,27,27,0.62);
        }}
        .kitchen-builder__intro {{
          color: rgba(27,27,27,0.68);
          line-height: 1.5;
          margin-bottom: 2px;
        }}
        .kitchen-request-summary {{
          display: grid;
          gap: 12px;
          padding: 16px;
          border-radius: 18px;
          background: linear-gradient(180deg, #f6f1e8 0%, #f2ebe0 100%);
          border: 1px solid rgba(31,59,45,0.06);
        }}
        .kitchen-section-card__head--summary {{
          margin-bottom: 0;
        }}
        .kitchen-request-summary__empty {{
          color: rgba(27,27,27,0.60);
          line-height: 1.45;
        }}
        .kitchen-request-summary__list {{
          display: grid;
          gap: 7px;
        }}
        .kitchen-request-summary__item {{
          display: grid;
          grid-template-columns: minmax(96px, 1fr) minmax(104px, 132px) 74px auto;
          gap: 9px;
          align-items: center;
          padding: 9px 10px;
          border-radius: 12px;
          background: rgba(255,255,255,0.64);
          border: 1px solid rgba(31,59,45,0.06);
        }}
        .kitchen-request-summary__name {{
          color: var(--accent);
          font-weight: 800;
          line-height: 1.25;
          min-width: 0;
        }}
        .kitchen-request-summary__unit {{
          color: rgba(27,27,27,0.62);
          font-size: 13px;
          font-weight: 750;
        }}
        .kitchen-request-summary__note,
        .recent-request__details {{
          color: rgba(27,27,27,0.62);
          font-size: 13px;
          line-height: 1.45;
        }}
        .kitchen-request-summary__actions {{
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          justify-content: flex-end;
        }}
        .kitchen-request-summary__request-note {{
          display: grid;
          gap: 6px;
          margin-top: 4px;
        }}
        .kitchen-request-summary__request-note label {{
          color: rgba(31,59,45,0.78);
        }}
        .kitchen-request-summary__request-note textarea {{
          min-height: 76px;
          resize: vertical;
          line-height: 1.45;
        }}
        .kitchen-submit {{
          width: 100%;
          margin-top: 4px;
          padding: 16px 16px;
          border-radius: 14px;
          border: 1px solid rgba(31,59,45,0.30);
          background: linear-gradient(180deg, #255c39 0%, #1f4f32 100%);
          box-shadow: 0 10px 20px rgba(27,52,38,0.16);
          color: #fffdf8;
          font-size: 15px;
          font-weight: 900;
          letter-spacing: 0.04em;
          transition: transform 120ms ease, box-shadow 120ms ease, filter 120ms ease;
        }}
        .kitchen-submit[disabled] {{
          opacity: 0.5;
          cursor: not-allowed;
          box-shadow: none;
        }}
        .kitchen-submit:active {{
          transform: translateY(1px);
          box-shadow: 0 6px 12px rgba(27,52,38,0.14);
          filter: saturate(0.96);
        }}
        .kitchen-submit--home {{
          display: inline-flex;
          justify-content: center;
          text-decoration: none;
        }}
        .kitchen-submit--secondary {{
          margin-top: 12px;
          background: rgba(255,253,248,0.96);
          color: var(--accent);
          border-color: rgba(31,59,45,0.12);
          box-shadow: 0 8px 16px rgba(29,45,35,0.06);
        }}
        .kitchen-closed-note__actions {{
          margin-top: 14px;
        }}
        .kitchen-closed-note {{
          padding: 16px;
          border-radius: 16px;
          background: #f4ecde;
          color: rgba(27,27,27,0.70);
          line-height: 1.45;
        }}
        .recent-request-list {{
          display: grid;
          gap: 11px;
        }}
        .recent-request {{
          display: grid;
          grid-template-columns: 74px minmax(0, 1fr) auto;
          align-items: center;
          gap: 12px;
          padding: 13px 14px;
          border-radius: 14px;
          background: #f8f6f1;
          border: 1px solid rgba(31,59,45,0.045);
          box-shadow: 0 3px 8px rgba(28,39,31,0.022);
        }}
        .recent-request__date {{
          font-size: 12px;
          color: rgba(31,59,45,0.72);
          font-weight: 800;
          white-space: nowrap;
          line-height: 1.25;
        }}
        .recent-request__main {{
          display: grid;
          gap: 3px;
          min-width: 0;
        }}
        .recent-request__crop {{
          display: inline-flex;
          align-items: center;
          gap: 8px;
          min-width: 0;
          color: var(--accent);
          font-weight: 700;
        }}
        .recent-request__meta {{
          color: rgba(27,27,27,0.52);
          font-size: 11px;
          font-weight: 600;
        }}
        .recent-request__details {{
          color: rgba(27,27,27,0.66);
          font-size: 12px;
          line-height: 1.4;
        }}
        .recent-request__status {{
          display: inline-flex;
          align-items: center;
          justify-content: center;
          align-self: center;
          padding: 5px 9px;
          border-radius: 999px;
          font-size: 10px;
          font-weight: 800;
          white-space: nowrap;
        }}
        .recent-request__status--open {{
          color: #b96800;
          background: rgba(245, 158, 11, 0.12);
        }}
        .recent-request__status--approved {{
          color: #1d7a43;
          background: rgba(34, 197, 94, 0.12);
        }}
        .recent-request__status--applied {{
          color: #1d7a43;
          background: rgba(34, 197, 94, 0.12);
        }}
        .recent-request__status--rejected {{
          color: #b42318;
          background: rgba(239, 68, 68, 0.12);
        }}
        .recent-request__status--zaprimljeno {{
          color: #b96800;
          background: rgba(245, 158, 11, 0.12);
        }}
        .recent-request__status--u_pripremi {{
          color: #24508f;
          background: rgba(59, 130, 246, 0.12);
        }}
        .recent-request__status--dostavljeno {{
          color: #1d7a43;
          background: rgba(34, 197, 94, 0.12);
        }}
        .recent-request__status--nije_moguce {{
          color: #b42318;
          background: rgba(239, 68, 68, 0.12);
        }}
        .worker-page {{
          max-width: 520px;
          margin: 0 auto;
          display: grid;
          gap: 16px;
        }}
        .worker-hero {{
          padding: 20px 18px;
          border-radius: 24px;
          background: linear-gradient(180deg, #fffdf8 0%, #f3eadb 100%);
          border: 1px solid rgba(31,59,45,0.08);
          box-shadow: 0 14px 28px rgba(29,45,35,0.07);
        }}
        .worker-hero__brand {{
          color: rgba(31,59,45,0.72);
          font-size: 11px;
          font-weight: 850;
          letter-spacing: 0.14em;
        }}
        .worker-hero h1 {{
          margin: 8px 0 6px 0;
          color: var(--accent);
          font-family: Georgia, "Times New Roman", Times, serif;
          font-size: 34px;
          line-height: 1;
        }}
        .worker-hero p {{
          margin: 0;
          color: rgba(27,27,27,0.66);
        }}
        .worker-hero__actions {{
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          margin-top: 14px;
        }}
        .worker-hero__actions a {{
          display: inline-flex;
          align-items: center;
          justify-content: center;
          min-height: 38px;
          padding: 9px 12px;
          border-radius: 999px;
          border: 1px solid rgba(31,59,45,0.14);
          background: rgba(255,255,255,0.72);
          color: var(--accent);
          font-size: 12px;
          font-weight: 850;
          letter-spacing: 0.04em;
        }}
        .worker-section {{
          display: grid;
          gap: 10px;
        }}
        .worker-section__title {{
          margin: 0;
          color: var(--accent);
          font-size: 15px;
          letter-spacing: 0.03em;
        }}
        .worker-card {{
          display: grid;
          gap: 12px;
          padding: 15px;
          border-radius: 18px;
          background: rgba(255,253,248,0.94);
          border: 1px solid rgba(31,59,45,0.08);
          box-shadow: 0 8px 18px rgba(29,45,35,0.045);
        }}
        .worker-card__top {{
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 12px;
        }}
        .worker-card__target {{
          color: var(--accent);
          font-size: 13px;
          font-weight: 900;
          letter-spacing: 0.05em;
          text-transform: uppercase;
        }}
        .worker-card__sent {{
          color: rgba(27,27,27,0.56);
          font-size: 12px;
          margin-top: 3px;
        }}
        .worker-card__label {{
          color: rgba(31,59,45,0.60);
          font-size: 10px;
          font-weight: 900;
          letter-spacing: 0.08em;
          text-transform: uppercase;
        }}
        .worker-card__meta {{
          color: rgba(27,27,27,0.64);
          font-size: 12px;
          line-height: 1.45;
        }}
        .worker-card__status {{
          flex: 0 0 auto;
          padding: 6px 9px;
          border-radius: 999px;
          font-size: 10px;
          font-weight: 900;
          text-transform: uppercase;
          background: rgba(31,59,45,0.08);
          color: var(--accent);
        }}
        .worker-card__items {{
          margin: 0;
          padding-left: 18px;
          color: rgba(27,27,27,0.84);
          line-height: 1.55;
        }}
        .worker-card__note {{
          padding: 10px 11px;
          border-radius: 12px;
          background: #f7f1e7;
          color: rgba(27,27,27,0.70);
          font-size: 13px;
          line-height: 1.45;
        }}
        .worker-card__actions {{
          display: grid;
          gap: 8px;
        }}
        .worker-card__actions form {{
          margin: 0;
        }}
        .worker-card__actions button {{
          width: 100%;
          min-height: 48px;
          border-radius: 14px;
          font-size: 13px;
          letter-spacing: 0.06em;
        }}
        .worker-card__actions .btn--ghost {{
          background: rgba(255,255,255,0.76);
        }}
        .worker-empty {{
          padding: 14px;
          border-radius: 16px;
          background: rgba(255,255,255,0.62);
          color: rgba(27,27,27,0.60);
          border: 1px solid rgba(31,59,45,0.06);
        }}
        .kitchen-success-banner {{
          border-radius: 28px;
          overflow: hidden;
          box-shadow: 0 18px 36px rgba(29,45,35,0.12);
        }}
        .kitchen-success-banner__image {{
          display: block;
          width: 100%;
          height: 168px;
          object-fit: cover;
        }}
        .kitchen-success-card {{
          background: #fffdf9;
          border: 1px solid rgba(31,59,45,0.08);
          border-radius: 24px;
          padding: 20px;
          box-shadow: 0 14px 28px rgba(29,45,35,0.08);
          text-align: center;
          display: grid;
          gap: 14px;
        }}
        .kitchen-success-card__check {{
          width: 96px;
          height: 96px;
          margin: -60px auto 0 auto;
          border-radius: 999px;
          background: linear-gradient(180deg, #eaf7ea 0%, #d8efd8 100%);
          color: #2b6b36;
          font-size: 54px;
          line-height: 96px;
          font-weight: 800;
          box-shadow: 0 10px 22px rgba(29,45,35,0.12);
          border: 8px solid #fffdf9;
        }}
        .kitchen-success-card h1 {{
          margin: 0;
          color: var(--accent);
          font-size: 34px;
          line-height: 1.02;
        }}
        .kitchen-success-card__meta {{
          margin: 0;
          color: rgba(27,27,27,0.62);
        }}
        .kitchen-success-summary {{
          text-align: left;
          padding: 16px;
          border-radius: 18px;
          background: #f7f1e7;
          border: 1px solid rgba(31,59,45,0.06);
          display: grid;
          gap: 12px;
        }}
        .kitchen-success-summary__label {{
          font-size: 12px;
          font-weight: 900;
          color: rgba(31,59,45,0.72);
          letter-spacing: 0.10em;
          text-transform: uppercase;
        }}
        .kitchen-success-summary__row {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }}
        .kitchen-success-summary__crop {{
          display: inline-flex;
          align-items: center;
          gap: 8px;
          font-weight: 800;
          color: var(--accent);
        }}
        .kitchen-success-summary__note {{
          color: rgba(27,27,27,0.64);
          font-size: 14px;
        }}
        @media (max-width: 520px) {{
          .pagehead {{
            align-items: flex-start;
            flex-direction: column;
          }}
          .tablewrap--responsive {{
            border: 0;
            background: transparent;
            overflow: visible;
          }}
          .tablewrap--responsive table,
          .tablewrap--responsive tbody,
          .tablewrap--responsive tr,
          .tablewrap--responsive td {{
            display: block;
            width: 100%;
          }}
          .tablewrap--responsive tr:first-child {{
            display: none;
          }}
          .tablewrap--responsive tr {{
            padding: 11px 12px;
            margin-bottom: 9px;
            border-radius: 14px;
            background: rgba(255,255,255,0.78);
            border: 1px solid rgba(31,59,45,0.07);
          }}
          .tablewrap--responsive td {{
            display: grid;
            grid-template-columns: 92px minmax(0, 1fr);
            gap: 10px;
            padding: 6px 0;
            border-bottom: 0;
          }}
          .tablewrap--responsive td::before {{
            content: attr(data-label);
            color: rgba(31,59,45,0.62);
            font-size: 11px;
            font-weight: 850;
            letter-spacing: 0.04em;
            text-transform: uppercase;
          }}
          .availability-quick-form__grid,
          .availability-quick-form__left {{
            grid-template-columns: 1fr;
          }}
          .kitchen-home-clean {{
            padding-top: 4px;
          }}
          .kitchen-home-clean--asseted {{
            min-height: calc(100vh - 84px);
            padding: 30px 16px 138px 16px;
            border-radius: 24px;
          }}
          .kitchen-day-hero__image {{
            height: 140px;
          }}
          .kitchen-home-clean--asseted {{
            min-height: calc(100vh - 24px);
            padding: 18px 14px 144px 14px;
            border-radius: 26px;
          }}
          .kitchen-home-clean__toprow {{
            margin-bottom: 40px;
          }}
          .kitchen-home-clean__header {{
            margin-bottom: 28px;
          }}
          .kitchen-action-group {{
            max-width: 360px;
            padding: 14px;
            border-radius: 26px;
          }}
          .kitchen-action-card {{
            min-height: 96px;
            padding: 18px 16px;
            gap: 14px;
            border-radius: 20px;
          }}
          .kitchen-action-card__icon {{
            width: 50px;
            height: 50px;
            flex-basis: 50px;
          }}
          .kitchen-home-clean__basket {{
            width: min(94%, 318px);
            bottom: -12px;
          }}
          .kitchen-form__row {{
            grid-template-columns: 1fr;
          }}
          .kitchen-request-summary__item {{
            grid-template-columns: minmax(0, 1fr) 64px auto;
            gap: 6px;
            padding: 8px 9px;
          }}
          .kitchen-request-summary__name {{
            grid-column: 1 / -1;
          }}
          .kitchen-request-summary__item input {{
            grid-column: 1 / 2;
          }}
          .recent-request {{
            grid-template-columns: minmax(0, 1fr) auto;
            align-items: flex-start;
          }}
          .recent-request__date {{
            grid-column: 1 / -1;
          }}
          .recent-request__main {{
            flex-direction: column;
            align-items: flex-start;
          }}
          .kitchen-success-card h1 {{
            font-size: 30px;
          }}
        }}

        /* ---------------- Footer (subtle) ---------------- */
        .footer {{
          margin: 24px 0 10px 0;
          color: rgba(0,0,0,0.45);
          font-size: 12.5px;
        }}
      </style>
    </head>

    <body>
      <header class="{topbar_class}">
        <div class="brand">
          <div class="brand__left">
            <p class="brand__title">Meneghetti Garden</p>
            {f'<p class="brand__subtitle">{brand_subtitle}</p>' if brand_subtitle else ''}
          </div>
          {role_badge}
        </div>
        {nav}
      </header>

      <main class="{container_class}">
        {pagehead_html}

        {body}

        <div class="footer">
          Meneghetti Garden App • interna verzija
        </div>
      </main>
    </body>
    </html>
    """



def crop_options(selected_crop_id=None) -> str:
    with Session(engine) as s:
        crops = s.query(Crop).filter(Crop.active == True).order_by(Crop.name_hr.asc(), Crop.name.asc()).all()
    if not crops:
        return '<option value="" disabled selected>Nema kultura - dodaj prvo u "Kulture"</option>'

    options = []
    for c in crops:
        selected = " selected" if selected_crop_id is not None and str(c.id) == str(selected_crop_id) else ""
        options.append(f'<option value="{c.id}"{selected}>{crop_name_hr(c)} ({c.unit})</option>')
    return "\n".join(options)


def crop_name_hr(crop: Crop) -> str:
    return (getattr(crop, "name_hr", None) or getattr(crop, "name", None) or "").strip()


def crop_name_en(crop: Crop) -> str:
    return (getattr(crop, "name_en", None) or crop_name_hr(crop)).strip()


def format_qty(value) -> str:
    return f"{float(value or 0):g}"


def format_qty_with_unit(value, unit: str) -> str:
    amount = format_qty(value)
    return f"{amount} {unit}".strip() if amount != "0" else (unit and f"0 {unit}" or "0")


def availability_table(items) -> str:
    if not items:
        return "<p>Još ništa nije uneseno za danas.</p>"
    rows = "".join([
        "<tr>"
        f"<td data-label='Kultura'>{crop_name_hr(a.crop)}</td>"
        f"<td data-label='Količina'>{format_qty_with_unit(a.qty, a.crop.unit)}</td>"
        f"<td data-label='Napomena'>{a.note or ''}</td>"
        "</tr>"
        for a in items
    ])
    return (
        '<div class="tablewrap tablewrap--responsive">'
        f"<table class='admin-availability-table'><tr><th>Kultura</th><th>Količina</th><th>Napomena</th></tr>{rows}</table>"
        "</div>"
    )

def status_badge(qty: float) -> str:
    if qty <= 0:
        return '<span class="badge bad">NEMA</span>'
    if qty < 1:
        return '<span class="badge warn">MALO</span>'
    return '<span class="badge ok">IMA</span>'


def day_section(title: str, items) -> str:
    return f"""
    <div class="card">
      <h3>{title}</h3>
      {availability_cards(items)}
    </div>
    """

def availability_cards(items) -> str:
    if not items:
        return "<p>Još ništa nije uneseno za danas.</p>"

    cards = []
    for a in items:
        # qty iz Numeric zna biti Decimal -> pretvori u float za usporedbu
        q = float(a.qty or 0)
        cards.append(f"""
        <div class="kcard">
          <div class="krow">
            <div class="kname">{crop_name_hr(a.crop)}</div>
            {status_badge(q)}
          </div>
          <div class="kqty">{format_qty_with_unit(q, a.crop.unit)}</div>
          <div class="knote">{(a.note or "")}</div>
        </div>
        """)

    return '<div class="kgrid">' + "\n".join(cards) + "</div>"

def crops_table(items) -> str:
    if not items:
        return "<p>Nema kultura. Dodaj prvu gore.</p>"
    rows = "".join([
        f"<tr><td>{crop_name_hr(c)}</td><td>{c.name_en or ''}</td><td>{c.category or ''}</td><td>{c.unit}</td></tr>"
        for c in items
    ])
    return f"<table><tr><th>Croatian name</th><th>English name</th><th>Kategorija</th><th>Jedinica</th></tr>{rows}</table>"


def harvest_table(items) -> str:
    if not items:
        return "<p>Nema unosa berbe.</p>"
    rows = "".join([
        f"<tr><td>{h.datetime.strftime('%Y-%m-%d %H:%M')}</td><td>{crop_name_hr(h.crop)}</td>"
        f"<td>{format_qty_with_unit(h.qty, h.crop.unit)}</td><td>{h.destination}</td><td>{h.note or ''}</td></tr>"
        for h in items
    ])
    return f"<table><tr><th>Vrijeme</th><th>Kultura</th><th>Količina</th><th>Gdje</th><th>Napomena</th></tr>{rows}</table>"


def report_table(rows) -> str:
    if not rows:
        return "<p>Nema podataka za zadnjih 30 dana.</p>"
    html_rows = "".join([f"<tr><td>{r[0]}</td><td>{format_qty_with_unit(r[2] or 0, r[1])}</td></tr>" for r in rows])
    return f"<table><tr><th>Kultura</th><th>Ukupno</th></tr>{html_rows}</table>"



def users_table(items) -> str:
    if not items:
        return "<p>Nema korisnika.</p>"
    rows = "".join([
        f"<tr><td>{u.username}</td><td>{u.role}</td><td>{'DA' if u.active else 'NE'}</td></tr>"
        for u in items
    ])
    return f"<table><tr><th>Username</th><th>Role</th><th>Aktivan</th></tr>{rows}</table>"

def kitchen_requests_table(items) -> str:
    if not items:
        return "<p>Nema zahtjeva.</p>"

    rows = ""
    for kr in items:
        ordered_by = kr.created_by.username if kr.created_by else "-"
        applied_by = kr.applied_by.username if kr.applied_by else "-"
        applied_at = kr.applied_at.strftime("%Y-%m-%d %H:%M") if getattr(kr, "applied_at", None) else "-"
        st = canonical_request_status(kr.status)
        group_label = request_group_key(kr)[:8]

        if st == "zaprimljeno":
            actions = f"""
            <div class="actions cell-actions">
              <form method="post" action="/admin/requests/{kr.id}/approve">
                <button type="submit">Preuzmi</button>
              </form>
              <form method="post" action="/admin/requests/{kr.id}/reject">
                <button type="submit" class="btn btn--ghost">Nije moguće</button>
              </form>
            </div>
            """
        elif st == "u_pripremi":
            actions = f"""
            <div class="actions cell-actions">
              <form method="post" action="/admin/requests/{kr.id}/reject">
                <button type="submit" class="btn btn--ghost">Nije moguće</button>
              </form>
            </div>
            """
        elif st == "dostavljeno":
            actions = '<span class="pill pill--accent">Dostavljeno</span>'
        else:
            actions = '<span class="pill pill--danger">Nije moguće</span>'

        rows += (
            f"<tr>"
            f"<td>{kr.created_at.strftime('%Y-%m-%d %H:%M')}</td>"
            f"<td>{kr.requested_for.isoformat()}</td>"
            f"<td><span class='pill'>{group_label}</span></td>"
            f"<td>{ordered_by}</td>"
            f"<td>{crop_name_hr(kr.crop)}</td>"
            f"<td>{request_item_amount_text(kr)}</td>"
            f"<td>{request_status_badge(kr.status)}</td>"
            f"<td>{kr.note or ''}</td>"
            f"<td>{applied_by}</td>"
            f"<td>{applied_at}</td>"
            f"<td>{actions}</td>"
            f"</tr>"
        )

    return (
        '<div class="tablewrap"><table>'
        "<tr>"
        "<th>Kreirano</th>"
        "<th>Za datum</th>"
        "<th>Grupa</th>"
        "<th>Naručio</th>"
        "<th>Kultura</th>"
        "<th>Količina</th>"
        "<th>Status</th>"
        "<th>Napomena</th>"
        "<th>Primijenio</th>"
        "<th>Primijenjeno</th>"
        "<th>Akcije</th>"
        "</tr>"
        f"{rows}</table></div>"
    )


def kitchen_status_label(st: str) -> str:
    st = canonical_request_status(st)
    return REQUEST_STATUS_LABELS.get(st, st)


def kitchen_requests_table_kitchen(items) -> str:
    grouped_requests = group_kitchen_requests(items)
    if not grouped_requests:
        return "<p>Nema poslanih zahtjeva.</p>"

    rows = ""
    for group in grouped_requests:
        details = ", ".join([
            f"{crop_name_hr(item.crop)} {request_item_amount_text(item)}"
            for item in group["items"]
        ])
        rows += (
            f"<tr>"
            f"<td>{group['created_at'].strftime('%Y-%m-%d %H:%M')}</td>"
            f"<td>{group['requested_for'].isoformat()}</td>"
            f"<td>{details}</td>"
            f"<td>{request_status_badge(group['status'])}</td>"
            f"<td>{len(group['items'])} stavki</td>"
            f"</tr>"
        )

    return (
        '<div class="tablewrap"><table>'
        "<tr><th>Kad</th><th>Za datum</th><th>Stavke</th><th>Status</th><th>Broj stavki</th></tr>"
        f"{rows}</table></div>"
    )


def kitchen_back_href(for_date: date) -> str:
    if for_date == date.today():
        return "/kitchen/today"
    return "/kitchen/tomorrow"


def today_order_cutoff_passed(now: datetime = None) -> bool:
    current_time = now or datetime.now()
    return current_time.time() > time(13, 0)


def kitchen_status_message(for_date: date) -> Tuple[str, str]:
    locked = is_day_locked(for_date)
    now = datetime.now()
    if for_date == date.today():
        if locked:
            return ("ZATVORENO", "Današnji datum je zaključan za nove zahtjeve.")
        if not today_order_cutoff_passed(now):
            return ("OTVORENO", "Možeš poslati zahtjev za današnju pripremu do 13:00")
        return ("ZATVORENO", "Narudžba za danas je bila moguća do 13:00. Možeš poslati zahtjev za sutra.")

    if locked:
        return ("ZATVORENO", "Sutra je zaključano za nove zahtjeve.")
    return ("OTVORENO", "Možeš poslati zahtjev za sutrašnju pripremu")


def kitchen_can_order(for_date: date) -> bool:
    return kitchen_status_message(for_date)[0] == "OTVORENO"


def kitchen_request_target_label(for_date: date) -> str:
    today = date.today()
    if for_date == today:
        return "Za danas"
    if for_date == today + timedelta(days=1):
        return "Za sutra"
    return f"Za {for_date.strftime('%d.%m.')}"


def kitchen_landing_context():
    today = date.today()
    tomorrow = today + timedelta(days=1)
    today_open = kitchen_can_order(today)
    tomorrow_open = kitchen_can_order(tomorrow)

    if today_open:
        return {
            "message": "Danas možeš poslati zahtjev do 13:00.",
            "detail": "Sutra je i dalje dostupna za planiranje i slanje zahtjeva.",
            "tone": "is-open",
            "primary_day": "today",
            "today_hint": "Ponuda i narudžbe za danas",
            "tomorrow_hint": "Planiraj i pošalji zahtjev za sutra",
        }

    tomorrow_hint = "Pošalji zahtjev za sutra" if tomorrow_open else "Provjeri sutrašnju ponudu"
    detail = "Možeš poslati zahtjev za sutra." if tomorrow_open else "Sutrašnji datum trenutno nije otvoren za nove zahtjeve."
    return {
        "message": "Narudžba za danas je zatvorena.",
        "detail": detail,
        "tone": "is-closed",
        "primary_day": "tomorrow",
        "today_hint": "Današnja narudžba je zatvorena",
        "tomorrow_hint": tomorrow_hint,
    }


def request_group_key(kr: KitchenRequest) -> str:
    return ((kr.request_group_id or "").strip() or str(kr.id))


def format_request_qty(value) -> str:
    return format_qty(value)


def request_item_unit(kr: KitchenRequest) -> str:
    if getattr(kr, "unit", None):
        return kr.unit
    if getattr(kr, "crop", None):
        return kr.crop.unit
    return ""


def request_item_amount_text(kr: KitchenRequest) -> str:
    if kr.qty is None or float(kr.qty or 0) <= 0:
        return "bez količine"
    unit = request_item_unit(kr)
    return f"{format_request_qty(kr.qty)} {unit}".strip()


def request_item_summary_text(kr: KitchenRequest) -> str:
    if kr.qty is None or float(kr.qty or 0) <= 0:
        return crop_name_hr(kr.crop)
    return f"{crop_name_hr(kr.crop)} {request_item_amount_text(kr)}"


def worker_item_summary_text(kr: KitchenRequest) -> str:
    if kr.qty is None or float(kr.qty or 0) <= 0:
        return crop_name_en(kr.crop)
    return f"{crop_name_en(kr.crop)} {request_item_amount_text(kr)}"


WORKER_STATUS_LABELS = {
    "zaprimljeno": "Received",
    "u_pripremi": "In preparation",
    "dostavljeno": "Delivered",
    "nije_moguce": "Not possible",
}


def worker_target_label(for_date: date) -> str:
    if for_date == date.today():
        return f"For today – {for_date.strftime('%d.%m.')}"
    if for_date == date.today() + timedelta(days=1):
        return f"For tomorrow – {for_date.strftime('%d.%m.')}"
    return f"For {for_date.strftime('%d.%m.')}"


def group_note_text(group) -> str:
    notes = []
    for item in group["items"]:
        note = (item.note or "").strip()
        if note and note not in notes:
            notes.append(note)
    return " | ".join(notes)


def worker_card(group) -> str:
    status = group["status"]
    group_id = group["group_id"]
    items_html = "".join([f"<li>{worker_item_summary_text(item)}</li>" for item in group["items"]])
    note = group_note_text(group)
    note_html = f'<div class="worker-card__note"><strong>Note:</strong><br>{note}</div>' if note else ""
    first_item = group["items"][0]
    delivered_at = getattr(first_item, "delivered_at", None)
    received_by = getattr(first_item, "assigned_to", None)
    delivered_text = f"Delivered at {delivered_at.strftime('%d.%m. %H:%M')}" if delivered_at else "Delivered"
    handled_html = f'<div class="worker-card__meta"><span class="worker-card__label">Handled by</span><br>{received_by}</div>' if received_by else ""
    delivered_html = f'<div class="worker-card__meta"><span class="worker-card__label">Delivered at</span><br>{delivered_at.strftime("%d.%m. %H:%M")}</div>' if delivered_at else ""

    if status == "zaprimljeno":
        actions = f"""
        <div class="worker-card__actions">
          <form method="post" action="/garden-worker/request/{group_id}/start">
            <button type="submit">START PREPARING</button>
          </form>
        </div>
        """
    elif status == "u_pripremi":
        actions = f"""
        <div class="worker-card__actions">
          <form method="post" action="/garden-worker/request/{group_id}/delivered">
            <button type="submit">MARK DELIVERED</button>
          </form>
          <form method="post" action="/garden-worker/request/{group_id}/not-possible">
            <button class="btn--ghost" type="submit">NOT POSSIBLE</button>
          </form>
        </div>
        """
    elif status == "dostavljeno":
        actions = f'<div class="pill pill--accent">{delivered_text}</div>'
    else:
        actions = '<div class="pill pill--danger">Not possible</div>'

    return f"""
    <article class="worker-card">
      <div class="worker-card__top">
        <div>
          <div class="worker-card__target">{worker_target_label(group["requested_for"])}</div>
          <div class="worker-card__sent"><span class="worker-card__label">Sent</span><br>{group["created_at"].strftime('%d.%m. %H:%M')}</div>
        </div>
        <div class="worker-card__status" title="Status">{WORKER_STATUS_LABELS.get(status, status)}</div>
      </div>
      <div class="worker-card__label">Items</div>
      <ul class="worker-card__items">{items_html}</ul>
      {note_html}
      {handled_html}
      {delivered_html}
      {actions}
    </article>
    """


def worker_section(title: str, groups, empty_text: str) -> str:
    if not groups:
        content = f'<div class="worker-empty">{empty_text}</div>'
    else:
        content = "".join(worker_card(group) for group in groups)
    return f"""
    <section class="worker-section">
      <h2 class="worker-section__title">{title}</h2>
      {content}
    </section>
    """


def request_group_status(items) -> str:
    statuses = {canonical_request_status(item.status) for item in items}
    if len(statuses) == 1:
        return next(iter(statuses))
    if "u_pripremi" in statuses:
        return "u_pripremi"
    if "zaprimljeno" in statuses:
        return "zaprimljeno"
    if "dostavljeno" in statuses:
        return "dostavljeno"
    return "nije_moguce"


def group_kitchen_requests(items):
    grouped = []
    lookup = {}
    for kr in items:
        key = request_group_key(kr)
        if key not in lookup:
            lookup[key] = {
                "group_id": key,
                "created_at": kr.created_at,
                "requested_for": kr.requested_for,
                "items": [],
            }
            grouped.append(lookup[key])
        lookup[key]["items"].append(kr)

    for group in grouped:
        group["status"] = request_group_status(group["items"])

    grouped.sort(key=lambda group: (group["created_at"], group["group_id"]), reverse=True)
    return grouped


def parse_kitchen_items(raw_items_json: str, back_href: str):
    try:
        parsed_items = json.loads(raw_items_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, html_message_page("Greška", "Zahtjev nije ispravno pripremljen. Odaberi stavke ponovno.", back_href)

    if not isinstance(parsed_items, list) or not parsed_items:
        return None, html_message_page("Greška", "Odaberi barem jednu kulturu za zahtjev.", back_href)

    merged_by_crop = {}
    for index, raw_item in enumerate(parsed_items, start=1):
        if not isinstance(raw_item, dict):
            return None, html_message_page("Greška", "Jedna od stavki zahtjeva nije ispravna.", back_href)

        crop_id, error_response = parse_int_field(raw_item.get("crop_id"), f"Kultura #{index}", back_href)
        if error_response:
            return None, error_response

        raw_qty = str(raw_item.get("qty") or "").strip()
        qty = 0
        if raw_qty:
            qty, error_response = parse_float_field(raw_qty, f"Količina #{index}", back_href, min_value=0)
            if error_response:
                return None, error_response
            if qty <= 0:
                return None, html_message_page("Greška", "Količina mora biti veća od nule ili ostavljena prazna.", back_href)

        merged_by_crop[crop_id] = {
            "crop_id": crop_id,
            "qty": qty,
            "note": str(raw_item.get("note") or "").strip() or None,
            "unit": str(raw_item.get("unit") or "").strip() or None,
        }

    return list(merged_by_crop.values()), None


def normalize_crop_name(value: str) -> str:
    name = (value or "").strip().lower()
    replacements = {
        "š": "s",
        "đ": "d",
        "č": "c",
        "ć": "c",
        "ž": "z",
    }
    for src, dst in replacements.items():
        name = name.replace(src, dst)
    return name


def crop_icon_src(crop_name: str) -> str:
    name = normalize_crop_name(crop_name)
    mapping = [
        (("rikola",), "leaf.png"),
        (("blitva", "spinat", "špinat"), "spinach.png"),
        (("salata kristalka", "salata", "kristalka"), "lettuce.png"),
        (("rotk", "radish"), "radish.png"),
        (("mladi luk", "luk", "onion", "poriluk"), "green-onion.png"),
        (("mrkva", "mrkv", "carrot"), "carrot.png"),
        (("rajcica", "rajc", "rajčica", "paradajz", "tomato"), "tomato.png"),
        (("krastavac", "cucumber"), "cucumber.png"),
        (("paprika", "pepper"), "pepper.png"),
        (("cikla", "beet"), "beet.png"),
        (("tikvica", "zucchini"), "zucchini.png"),
        (("krumpir", "potato"), "potato.png"),
        (("persin", "peršin", "parsley"), "parsley.png"),
        (("bosiljak", "basil"), "basil.png"),
    ]
    for tokens, icon in mapping:
        if any(token in name for token in tokens):
            return f"/static/icons/vegetables/{icon}"
    return "/static/icons/vegetables/leaf.png"


def kitchen_offer_list(items, can_order: bool) -> str:
    if not items:
        return (
            '<div class="kitchen-empty kitchen-empty--soft">'
            '<strong>Trenutno nema dostupnih kultura.</strong>'
            '<span>Provjeri ponudu kasnije ili se javi vrtu.</span>'
            '</div>'
        )

    rows = []
    for item in items:
        name_hr = crop_name_hr(item.crop)
        qty = float(item.qty or 0)
        availability_text = f"Dostupno: {format_qty_with_unit(qty, item.crop.unit)}" if qty > 0 else "Dostupno"
        add_label = "Dodaj" if can_order else "Zatvoreno"
        button_attrs = '' if can_order else ' disabled aria-disabled="true"'
        note_html = f'<div class="offer-item__note">{item.note}</div>' if item.note else ''
        rows.append(
            f'''
            <article class="offer-item offer-item--interactive" data-offer-card data-crop-id="{item.crop_id}" data-crop-name="{name_hr}" data-crop-unit="{item.crop.unit}">
              <div class="offer-item__left">
                <img class="offer-item__icon" src="{crop_icon_src(name_hr)}" alt="{name_hr}">
                <div>
                  <div class="offer-item__name">{name_hr}</div>
                  <div class="offer-item__qty">{availability_text}</div>
                </div>
              </div>
              <button class="offer-item__toggle" type="button" data-offer-toggle{button_attrs}>{add_label}</button>
              {note_html}
            </article>
            '''
        )
    return '<div class="offer-list">' + ''.join(rows) + '</div>'


def kitchen_request_form(for_date: date, offer_items, status_hint: str) -> str:
    if not kitchen_can_order(for_date):
        tomorrow_cta = ''
        if for_date == date.today():
            tomorrow_cta = (
                '<div class="kitchen-closed-note__actions">'
                '<a class="kitchen-submit kitchen-submit--secondary" href="/kitchen/tomorrow">Naruči za sutra</a>'
                '</div>'
            )
        readonly_offer = kitchen_offer_list(offer_items, False) if offer_items else ''
        return f'<div class="kitchen-closed-note"><div>{status_hint}</div>{tomorrow_cta}</div>{readonly_offer}'

    if not offer_items:
        if for_date == date.today() + timedelta(days=1):
            message = 'Trenutno nema ponuđenih kultura za sutra. Pokušaj kasnije ili kontaktiraj vrt.'
        else:
            message = 'Trenutno nema ponuđenih kultura za odabir, pa nije mogu?e sastaviti zahtjev.'
        return f'<div class="kitchen-closed-note">{message}</div>'

    builder_id = f"builder-{for_date.isoformat()}"
    return f'''
    <form class="kitchen-form kitchen-form--builder" method="post" action="/kitchen/request" data-kitchen-builder id="{builder_id}">
      <input type="hidden" name="requested_for" value="{for_date.isoformat()}" />
      <input type="hidden" name="items_json" value="[]" data-kitchen-items />

      <div class="kitchen-builder__intro">
        Dodaj kulture iz ponude, a količine upiši samo ako su važne za vrt.
      </div>

      {kitchen_offer_list(offer_items, True)}

      <section class="kitchen-request-summary" data-request-summary>
        <div class="kitchen-section-card__head kitchen-section-card__head--summary">
          <h3>Odabrano za zahtjev</h3>
          <span class="kitchen-section-card__chip" data-request-count>0 stavki</span>
        </div>
        <div class="kitchen-request-summary__empty" data-request-empty>
          Još nema odabranih kultura. Dodaj ih iz ponude iznad.
        </div>
        <div class="kitchen-request-summary__list" data-request-list></div>
        <div class="kitchen-request-summary__request-note">
          <label for="{builder_id}-request-note">Napomena za vrt (opcionalno)</label>
          <textarea id="{builder_id}-request-note" name="request_note" data-request-note placeholder="npr. ako može do 10h, za lunch, za večeru..."></textarea>
        </div>
      </section>

      <button class="kitchen-submit" type="submit" data-request-submit disabled>POŠALJI ZAHTJEV</button>
    </form>
    <script>
      (function () {{
        const root = document.getElementById({json.dumps(builder_id)});
        if (!root) return;

        const hiddenInput = root.querySelector('[data-kitchen-items]');
        const listEl = root.querySelector('[data-request-list]');
        const emptyEl = root.querySelector('[data-request-empty]');
        const countEl = root.querySelector('[data-request-count]');
        const submitBtn = root.querySelector('[data-request-submit]');
        const cards = Array.from(root.querySelectorAll('[data-offer-card]'));
        const state = new Map();

        function escapeHtml(value) {{
          return String(value || '').replace(/[&<>"']/g, function (char) {{
            return {{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }}[char] || char;
          }});
        }}

        function formatQty(value) {{
          const raw = String(value || '').trim();
          if (!raw) return '';
          const number = Number(raw);
          return Number.isFinite(number) ? String(number).replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1') : raw;
        }}

        function syncCard(card, item) {{
          const toggle = card.querySelector('[data-offer-toggle]');
          if (!toggle) return;

          if (item) {{
            toggle.textContent = 'Dodano';
            toggle.disabled = true;
            toggle.classList.add('is-added');
          }} else {{
            toggle.textContent = 'Dodaj';
            toggle.disabled = false;
            toggle.classList.remove('is-added');
          }}
        }}

        function renderSummary() {{
          const items = Array.from(state.values());
          hiddenInput.value = JSON.stringify(items.map(function (item) {{
            return {{ crop_id: item.crop_id, qty: item.qty, unit: item.unit }};
          }}));

          countEl.textContent = items.length + (items.length === 1 ? ' stavka' : items.length < 5 ? ' stavke' : ' stavki');
          emptyEl.hidden = items.length > 0;
          if (items.length === 0) emptyEl.textContent = 'Još nema odabranih kultura. Dodaj ih iz ponude iznad.';
          listEl.innerHTML = items.map(function (item) {{
            const cropId = escapeHtml(String(item.crop_id));
            return '<div class="kitchen-request-summary__item">'
              + '<div class="kitchen-request-summary__name">' + escapeHtml(item.name) + '</div>'
              + '<input data-summary-qty="' + cropId + '" type="number" step="0.01" min="0.01" placeholder="opcionalno" value="' + escapeHtml(formatQty(item.qty)) + '" />'
              + '<span class="kitchen-request-summary__unit">' + escapeHtml(item.unit || '') + '</span>'
              + '<div class="kitchen-request-summary__actions">'
              + '<button type="button" data-summary-remove="' + cropId + '">Ukloni</button>'
              + '</div>'
              + '</div>';
          }}).join('');

          submitBtn.disabled = items.length === 0;

          cards.forEach(function (card) {{
            const cropId = Number(card.dataset.cropId);
            syncCard(card, state.get(cropId));
          }});
        }}

        cards.forEach(function (card) {{
          const cropId = Number(card.dataset.cropId);
          const toggle = card.querySelector('[data-offer-toggle]');
          if (!toggle) return;

          toggle.addEventListener('click', function () {{
            state.set(cropId, {{
              crop_id: cropId,
              name: card.dataset.cropName,
              unit: card.dataset.cropUnit,
              qty: '',
            }});
            renderSummary();
          }});
        }});

        listEl.addEventListener('input', function (event) {{
          const qtyInput = event.target.closest('[data-summary-qty]');
          if (!qtyInput) return;

          const cropId = Number(qtyInput.getAttribute('data-summary-qty'));
          const item = state.get(cropId);
          if (!item) return;

          item.qty = qtyInput.value.trim();
          hiddenInput.value = JSON.stringify(Array.from(state.values()).map(function (entry) {{
            return {{ crop_id: entry.crop_id, qty: entry.qty, unit: entry.unit }};
          }}));
        }});

        listEl.addEventListener('click', function (event) {{
          const removeBtn = event.target.closest('[data-summary-remove]');
          if (removeBtn) {{
            const cropId = Number(removeBtn.getAttribute('data-summary-remove'));
            state.delete(cropId);
            renderSummary();
          }}
        }});

        root.addEventListener('submit', function (event) {{
          if (state.size === 0) {{
            event.preventDefault();
            emptyEl.hidden = false;
            emptyEl.textContent = 'Odaberi barem jednu kulturu prije slanja zahtjeva.';
            return;
          }}
          hiddenInput.value = JSON.stringify(Array.from(state.values()).map(function (entry) {{
            return {{ crop_id: entry.crop_id, qty: entry.qty, unit: entry.unit }};
          }}));
        }});

        renderSummary();
      }})();
    </script>
    '''


def kitchen_recent_requests(items) -> str:
    grouped_requests = group_kitchen_requests(items)[:3]
    if not grouped_requests:
        return '<p class="subtle-note">Još nema poslanih zahtjeva.</p>'

    rows = []
    for group in grouped_requests:
        first_item = group["items"][0]
        first_name = crop_name_hr(first_item.crop)
        target_label = kitchen_request_target_label(group["requested_for"])
        details = ", ".join([
            request_item_summary_text(item)
            for item in group["items"]
        ])
        sent_label = f'Poslano {group["created_at"].strftime("%d.%m.")}'
        rows.append(
            f'<div class="recent-request">'
            f'  <div class="recent-request__date">{target_label}</div>'
            f'  <div class="recent-request__main">'
            f'    <span class="recent-request__crop"><img class="recent-request__icon" src="{crop_icon_src(first_name)}" alt="{first_name}">{first_name}</span>'
            f'    <span class="recent-request__meta">{sent_label}</span>'
            f'    <span class="recent-request__details">{details}</span>'
            f'  </div>'
            f'  <div class="recent-request__status recent-request__status--{group["status"]}">{REQUEST_STATUS_LABELS.get(group["status"], group["status"])}</div>'
            f'</div>'
        )
    return '<div class="recent-request-list">' + ''.join(rows) + '</div>'

def kitchen_day_screen(for_date: date, day_key: str, heading: str, hero_image: str) -> str:
    status_title, status_hint = kitchen_status_message(for_date)
    status_class = "is-open" if status_title == "OTVORENO" else "is-closed"

    with Session(engine) as s:
        offer_items = (
            s.query(Availability)
            .options(joinedload(Availability.crop))
            .filter(Availability.date == for_date)
            .order_by(Availability.qty.desc(), Availability.id.asc())
            .all()
        )
        recent_requests = (
            s.query(KitchenRequest)
            .options(joinedload(KitchenRequest.crop))
            .filter(KitchenRequest.created_by_user_id == current_user.id)
            .order_by(KitchenRequest.created_at.desc(), KitchenRequest.id.desc())
            .limit(18)
            .all()
        )

    offer_heading = "Ponuda danas" if day_key == "today" else "Ponuda za sutra"
    order_block = kitchen_request_form(for_date, offer_items, status_hint)

    body = f"""
    <div class="kitchen-day-view">
      <a class="kitchen-back-link" href="/kitchen">← Natrag</a>

      <header class="kitchen-day-view__header">
        <div>
          <h1 class="kitchen-day-view__title">{heading}</h1>
          <p class="kitchen-day-view__date">{for_date.strftime('%A, %d.%m.%Y.').capitalize()}</p>
        </div>
      </header>

      <section class="kitchen-status-card {status_class}">
        <div class="kitchen-status-card__title">{status_title}</div>
        <div class="kitchen-status-card__hint">{status_hint}</div>
      </section>

      <section class="kitchen-section-card kitchen-section-card--form">
        <div class="kitchen-section-card__head">
          <h3>{offer_heading}</h3>
          <span class="kitchen-section-card__chip">{for_date.strftime('%d.%m.')}</span>
        </div>
        {order_block}
      </section>

      <section class="kitchen-section-card">
        <div class="kitchen-section-card__head">
          <h3>Zadnji zahtjevi</h3>
        </div>
        {kitchen_recent_requests(recent_requests)}
      </section>
    </div>
    """
    return html_page(heading, body)


def kitchen_success_screen(request_group_id: str):
    with Session(engine) as s:
        request_items = (
            s.query(KitchenRequest)
            .options(joinedload(KitchenRequest.crop))
            .filter(
                KitchenRequest.request_group_id == request_group_id,
                KitchenRequest.created_by_user_id == current_user.id,
            )
            .order_by(KitchenRequest.id.asc())
            .all()
        )

    if not request_items:
        return html_message_page("Zahtjev", "Traženi zahtjev nije pronađen.", "/kitchen")

    first_item = request_items[0]
    body_rows = []
    for item in request_items:
        name_hr = crop_name_hr(item.crop)
        note_html = f'<div class="kitchen-success-summary__note">Napomena: {item.note}</div>' if item.note else ''
        body_rows.append(
            f'''
            <div class="kitchen-success-summary__row kitchen-success-summary__row--stacked">
              <span class="kitchen-success-summary__crop"><img class="offer-item__icon" src="{crop_icon_src(name_hr)}" alt="{name_hr}">{name_hr}</span>
              <strong>{request_item_amount_text(item)}</strong>
            </div>
            {note_html}
            '''
        )

    body = f"""
    <div class="kitchen-success-view">
      <div class="kitchen-success-banner">
        <img class="kitchen-success-banner__image" src="/static/images/success-garden-bg.png" alt="Garden success banner">
      </div>

      <section class="kitchen-success-card">
        <div class="kitchen-success-card__check">✓</div>
        <h1>Zahtjev je poslan!</h1>
        <p class="kitchen-success-card__meta">{first_item.requested_for.strftime('%d.%m.%Y.')} • {first_item.created_at.strftime('%H:%M')} • {len(request_items)} stavke</p>

        <div class="kitchen-success-summary">
          <div class="kitchen-success-summary__label">Što ste naručili</div>
          {''.join(body_rows)}
        </div>

        <a class="kitchen-submit kitchen-submit--home" href="/kitchen">Natrag na početnu</a>
      </section>
    </div>
    """
    return html_page("Zahtjev poslan", body)


@app.get("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("root"))
    body = """
    <div class="card">
      <form method="post" action="/login">
        <label>Korisničko ime</label>
        <input name="username" required />
        <label>Lozinka</label>
        <input name="password" type="password" required />
        <button type="submit">Login</button>
      </form>
      <p class="pill">Admin user se automatski kreira iz docker-compose env varijabli.</p>
    </div>
    """
    return html_page("Login", body)


@app.post("/login")
def login_post():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not username or not password:
        return html_message_page("Login", "Upiši korisničko ime i lozinku.", "/login")

    with Session(engine) as s:
        u = s.query(User).filter(User.username == username, User.active == True).first()

    if not u or not check_password_hash(u.password_hash, password):
        return html_message_page("Login", "Pogrešno korisničko ime ili lozinka.", "/login", "Pokušaj ponovo")

    login_user(u)
    return redirect(url_for(home_endpoint_for_role(u.role)))


@app.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.get("/")
def root():
    if not current_user.is_authenticated:
        return redirect(url_for("login"))
    return redirect(url_for(home_endpoint_for_role(current_user.role)))


@app.get("/kitchen")
@kitchen_required
def kitchen_dashboard():
    hero_image = "/static/images/background.jpg"
    hero_style = ""
    if os.path.exists(os.path.join(app.root_path, "app", "static", "images", "background.jpg")):
        hero_style = f" style=\"background-image:url('{hero_image}');\""

    landing = kitchen_landing_context()
    today_card_class = "primary" if landing["primary_day"] == "today" else "secondary"
    tomorrow_card_class = "primary" if landing["primary_day"] == "tomorrow" else "secondary"

    leaf_html = '<span class="kitchen-home-clean__leaf-placeholder" aria-hidden="true"></span>'
    if os.path.exists(os.path.join(app.static_folder, "images", "leaf.png")):
        leaf_html = '<img class="kitchen-home-clean__leaf-icon" src="/static/images/leaf.png" alt="">'

    basket_html = ""
    if os.path.exists(os.path.join(app.root_path, "app", "static", "images", "basket-hero-reference.png")):
        basket_html = '<img class="kitchen-home-clean__basket" src="/static/images/basket-hero-reference.png" alt="Košara s povr?em">'

    body = f"""
    <div class="kitchen-home-page"{hero_style}>
      <div class="kitchen-home-clean">
        <div class="kitchen-home-clean__toprow">
          <div class="kitchen-home-clean__brandlockup">
            {leaf_html}
            <span class="kitchen-home-clean__brandtext">MENEGHETTI GARDEN</span>
          </div>
          <a class="kitchen-home-clean__logout" href="/logout">Odjava</a>
        </div>

        <header class="kitchen-home-clean__header">
          <h1 class="kitchen-home-clean__title">Kuhinja</h1>
          <p class="kitchen-home-clean__subtitle">Jednostavno slanje zahtjeva prema vrtu.</p>
        </header>

        <section class="kitchen-home-status kitchen-home-status--{landing['tone']}" aria-label="Status naručivanja">
          <strong>{landing['message']}</strong>
          <span>{landing['detail']}</span>
        </section>

        <section class="kitchen-action-group" aria-label="Odabir dana">
          <a class="kitchen-action-card kitchen-action-card--{today_card_class}" href="/kitchen/today">
            <span class="kitchen-action-card__icon kitchen-action-card__icon--today" aria-hidden="true"></span>
            <span class="kitchen-action-card__content">
              <span class="kitchen-action-card__title">DANAS</span>
              <span class="kitchen-action-card__hint">{landing['today_hint']}</span>
            </span>
          </a>

          <a class="kitchen-action-card kitchen-action-card--{tomorrow_card_class}" href="/kitchen/tomorrow">
            <span class="kitchen-action-card__icon kitchen-action-card__icon--tomorrow" aria-hidden="true"></span>
            <span class="kitchen-action-card__content">
              <span class="kitchen-action-card__title">SUTRA</span>
              <span class="kitchen-action-card__hint">{landing['tomorrow_hint']}</span>
            </span>
          </a>
        </section>

        <section class="kitchen-info-card" aria-label="Informacija">
          Odaberi dan kako bi vidio ponudu, status i sljedeći korak za kuhinju.
        </section>

        {basket_html}
      </div>
    </div>
    """
    return html_page("Kuhinja", body)


@app.get("/kitchen/today")
@kitchen_required
def kitchen_today():
    return kitchen_day_screen(date.today(), "today", "Danas", "/static/images/landing-reference.png")


@app.get("/kitchen/tomorrow")
@kitchen_required
def kitchen_tomorrow():
    return kitchen_day_screen(date.today() + timedelta(days=1), "tomorrow", "Sutra", "/static/images/hero-garden-bg.png")


@app.get("/kitchen/request-sent/<request_group_id>")
@kitchen_required
def kitchen_request_sent(request_group_id: str):
    return kitchen_success_screen(request_group_id)


@app.get("/garden-worker")
@worker_required
def garden_worker_dashboard():
    with Session(engine) as s:
        items = (
            s.query(KitchenRequest)
            .options(joinedload(KitchenRequest.crop), joinedload(KitchenRequest.created_by))
            .order_by(KitchenRequest.created_at.desc(), KitchenRequest.id.asc())
            .limit(120)
            .all()
        )

    groups = group_kitchen_requests(items)
    today = date.today()
    new_groups = [group for group in groups if group["status"] == "zaprimljeno"]
    in_progress_groups = [group for group in groups if group["status"] == "u_pripremi"]
    delivered_groups = [
        group for group in groups
        if group["status"] == "dostavljeno"
        and any(getattr(item, "delivered_at", None) and item.delivered_at.date() == today for item in group["items"])
    ]
    not_possible_groups = [group for group in groups if group["status"] == "nije_moguce"]

    body = f"""
    <div class="worker-page">
      <header class="worker-hero">
        <div class="worker-hero__brand">MENEGHETTI GARDEN</div>
        <h1>Kitchen Requests</h1>
        <p>Pick up, prepare, and mark as delivered.</p>
        <div class="worker-hero__actions">
          <a href="javascript:history.back()">BACK</a>
          <a href="/kitchen">OPEN APP</a>
        </div>
      </header>
      {worker_section("New requests", new_groups, "No new requests.")}
      {worker_section("In preparation", in_progress_groups, "No requests in preparation.")}
      {worker_section("Delivered today", delivered_groups, "No delivered requests today.")}
      {worker_section("Not possible", not_possible_groups, "No requests marked not possible.")}
    </div>
    """
    return html_page("Kitchen Requests", body)


def update_worker_group_status(group_id: str, target_status: str):
    now = datetime.now()
    with Session(engine) as s:
        items = (
            s.query(KitchenRequest)
            .filter(KitchenRequest.request_group_id == group_id)
            .all()
        )
        if not items:
            return html_message_page("Zahtjev", "Zahtjev nije pronađen.", "/garden-worker")

        worker_name = getattr(current_user, "username", None) or "garden"
        for item in items:
            item.status = target_status
            if target_status == "u_pripremi":
                item.assigned_to = worker_name
                item.received_at = now
            elif target_status == "dostavljeno":
                item.delivered_at = now
            elif target_status == "nije_moguce":
                item.assigned_to = item.assigned_to or worker_name
        s.commit()

    return redirect(url_for("garden_worker_dashboard"))


@app.post("/garden-worker/request/<path:group_id>/start")
@worker_required
def garden_worker_start(group_id: str):
    return update_worker_group_status(group_id, "u_pripremi")


@app.post("/garden-worker/request/<path:group_id>/delivered")
@worker_required
def garden_worker_delivered(group_id: str):
    return update_worker_group_status(group_id, "dostavljeno")


@app.post("/garden-worker/request/<path:group_id>/not-possible")
@worker_required
def garden_worker_not_possible(group_id: str):
    return update_worker_group_status(group_id, "nije_moguce")


@app.post("/kitchen/request")
@kitchen_required
def kitchen_request_post():
    if getattr(current_user, "role", None) != "kitchen":
        return redirect(url_for("admin_dashboard"))

    requested_for_str = (request.form.get("requested_for") or "").strip()
    if not requested_for_str:
        return html_message_page("Greška", "Nedostaje datum za koji se šalje zahtjev.", "/kitchen")

    requested_for, error_response = parse_date_field(requested_for_str, "Datum", "/kitchen")
    if error_response:
        return error_response

    back_href = kitchen_back_href(requested_for)
    raw_items_json = (request.form.get("items_json") or "").strip()
    request_note = (request.form.get("request_note") or "").strip() or None

    if raw_items_json:
        request_items, error_response = parse_kitchen_items(raw_items_json, back_href)
        if error_response:
            return error_response
    else:
        crop_id, error_response = parse_int_field(request.form.get("crop_id"), "Kultura", back_href)
        if error_response:
            return error_response

        raw_qty = (request.form.get("qty") or "").strip()
        qty = 0
        if raw_qty:
            qty, error_response = parse_float_field(raw_qty, "Količina", back_href, min_value=0)
            if error_response:
                return error_response
            if qty <= 0:
                return html_message_page("Greška", "Količina mora biti veća od nule ili ostavljena prazna.", back_href)

        request_items = [{
            "crop_id": crop_id,
            "qty": qty,
            "note": (request.form.get("note") or "").strip() or None,
            "unit": (request.form.get("unit") or "").strip() or None,
        }]

    if request_note:
        # KitchenRequest nema zaseban request-level note stupac, pa se zajednička
        # napomena privremeno sprema u note svake stavke iste grupe.
        for item in request_items:
            item["note"] = request_note

    if is_day_locked(requested_for):
        return html_message_page("Zaključano", f"Datum {requested_for.isoformat()} je zaključan.", back_href)

    if requested_for == date.today() and today_order_cutoff_passed():
        return html_message_page("Zatvoreno", "Narudžba za danas je bila moguća do 13:00. Možeš poslati zahtjev za sutra.", "/kitchen/today")

    crop_ids = [item["crop_id"] for item in request_items]
    with Session(engine) as s:
        crops = (
            s.query(Crop)
            .filter(Crop.id.in_(crop_ids), Crop.active == True)
            .all()
        )
        crops_by_id = {crop.id: crop for crop in crops}
        missing_crop_ids = [crop_id for crop_id in crop_ids if crop_id not in crops_by_id]
        if missing_crop_ids:
            return html_message_page("Greška", "Jedna od odabranih kultura više nije dostupna za narudžbu.", back_href)

        request_group_id = str(uuid.uuid4())
        for item in request_items:
            crop = crops_by_id[item["crop_id"]]
            s.add(KitchenRequest(
                requested_for=requested_for,
                request_group_id=request_group_id,
                crop_id=crop.id,
                unit=item.get("unit") or crop.unit,
                qty=item["qty"],
                note=item["note"],
                status="zaprimljeno",
                created_by_user_id=current_user.id
            ))
        s.commit()

    return redirect(url_for("kitchen_request_sent", request_group_id=request_group_id))


# ---------------- Routes: Admin ----------------
@app.get("/admin")
@admin_required
def admin_dashboard():
    today = date.today()
    tomorrow = today + timedelta(days=1)

    with Session(engine) as s:
        today_rows = (
            s.query(Availability)
            .options(joinedload(Availability.crop))
            .filter(Availability.date == today)
            .all()
        )
        tomorrow_rows = (
            s.query(Availability)
            .options(joinedload(Availability.crop))
            .filter(Availability.date == tomorrow)
            .all()
        )

    locked_today = is_day_locked(today)
    locked_tomorrow = is_day_locked(tomorrow)

    # Tabs (Danas/Sutra) – definirano OVDJE, prije body
    tabs_html = f"""
    <div class="tabs">
      <button class="tabbtn active" type="button" onclick="showAdminDay('today')">Danas</button>
      <button class="tabbtn" type="button" onclick="showAdminDay('tomorrow')">Sutra</button>
      <span class="pill" style="margin-left:auto;">{today.isoformat()} / {tomorrow.isoformat()}</span>
    </div>
    """

    body = f"""
    <div class="card">
      <h3>Status dana</h3>

      <div class="grid2">
        <!-- DANAS -->
        <div class="card">
          <h3>Danas</h3>
          <p class="pill pill--accent">
            {today.isoformat()} — {"ZAKLJUČANO" if locked_today else "OTVORENO"}
          </p>

          <div class="actions" style="margin-top:12px;">
            <form method="post" action="/admin/lock_today">
              <button type="submit" {"disabled" if locked_today else ""}>Zatvori danas</button>
            </form>

            <form method="post" action="/admin/unlock_today">
              <button type="submit" class="btn btn--ghost" {"disabled" if not locked_today else ""}>Otključaj danas</button>
            </form>
          </div>
        </div>

        <!-- SUTRA -->
        <div class="card">
          <h3>Sutra</h3>
          <p class="pill pill--accent">
            {tomorrow.isoformat()} — {"ZAKLJUČANO" if locked_tomorrow else "OTVORENO"}
          </p>

          <div class="actions" style="margin-top:12px;">
            <form method="post" action="/admin/lock_tomorrow">
              <button type="submit" {"disabled" if locked_tomorrow else ""}>Zatvori sutra</button>
            </form>

            <form method="post" action="/admin/unlock_tomorrow">
              <button type="submit" class="btn btn--ghost" {"disabled" if not locked_tomorrow else ""}>Otključaj sutra</button>
            </form>
          </div>
        </div>
      </div>

      <p class="pill muted" style="margin-top:12px;">
        Kada zaključaš dan, više se ne može mijenjati dostupnost za taj datum.
      </p>
    </div>

    <div class="card">
      <h3>Brze radnje</h3>
      <div class="actions">
        <form method="post" action="/admin/availability/copy_today_to_tomorrow">
          <button type="submit" {"disabled" if locked_tomorrow else ""}>Kopiraj danas → sutra</button>
        </form>
        <span class="pill" style="margin-left:auto;">
          {"Upozorenje: Sutra je zaključano — kopiranje onemogućeno." if locked_tomorrow else " "}
        </span>
      </div>
      <p class="pill">Kopira sve stavke dostupnosti za danas u sutra (prepisuje postojeće za sutra).</p>
    </div>

    <div class="card">
      <h3>Brzi unos dostupnosti</h3>
      <form class="availability-quick-form" method="post" action="/admin/availability/add">
        <div class="availability-quick-form__grid">
          <div class="availability-quick-form__left">
            <div class="availability-quick-form__field">
              <label>Datum</label>
              <select name="day" required>
                <option value="today">Danas</option>
                <option value="tomorrow">Sutra</option>
              </select>
            </div>

            <div class="availability-quick-form__field">
              <label>Kultura</label>
              <select name="crop_id" required>
                {crop_options()}
              </select>
            </div>
          </div>

          <div class="availability-quick-form__field">
            <label>Količina</label>
            <input name="qty" type="number" step="0.01" min="0" placeholder="npr. 3.5" required/>
          </div>
        </div>

        <div class="availability-quick-form__note">
          <label>Napomena (opcionalno)</label>
          <input name="note" placeholder="npr. samo ujutro, sitno, itd."/>
        </div>
        <div class="actions">
          <button type="submit">Spremi</button>
        </div>
      </form>
    </div>

    <div class="card">
      <h3>Dostupnost</h3>
      {tabs_html}

      <div id="today" class="daywrap active">
        <h3>Danas dostupno ({today.isoformat()})</h3>
        {availability_table(today_rows)}
      </div>

      <div id="tomorrow" class="daywrap">
        <h3>Sutra dostupno ({tomorrow.isoformat()})</h3>
        {availability_table(tomorrow_rows)}
      </div>
    </div>

    <script>
      function showAdminDay(which) {{
        const wraps = document.querySelectorAll('.daywrap');
        wraps.forEach(w => w.classList.remove('active'));
        document.getElementById(which).classList.add('active');

        const btns = document.querySelectorAll('.tabbtn');
        btns.forEach(b => b.classList.remove('active'));
        if (which === 'today') btns[0].classList.add('active');
        else btns[1].classList.add('active');
      }}
    </script>
    """
    return html_page("Admin", body)


@app.get("/admin/expenses")
@admin_required
def expenses_list():
    today = date.today()
    year = request.args.get("year", type=int) or today.year
    month_raw = request.args.get("month", default="all")  # default: svi mjeseci

    # normalize month
    if month_raw == "all":
        month = "all"
    else:
        try:
            m = int(month_raw)
            month = m if 1 <= m <= 12 else "all"
        except ValueError:
            month = "all"

    # build date range [start, end)
    if month == "all":
        start_d = date(year, 1, 1)
        end_d = date(year + 1, 1, 1)
        period_label = f"{year} (svi mjeseci)"
    else:
        start_d = date(year, month, 1)
        if month == 12:
            end_d = date(year + 1, 1, 1)
        else:
            end_d = date(year, month + 1, 1)
        period_label = f"{year}-{month:02d}"

    with engine.connect() as conn:
        # years range from expenses
        yr = conn.execute(text("""
            SELECT
              MIN(EXTRACT(YEAR FROM e.date))::int AS min_year,
              MAX(EXTRACT(YEAR FROM e.date))::int AS max_year
            FROM expenses e;
        """)).fetchone()

        min_year = (yr.min_year if yr and yr.min_year else today.year)
        max_year = (yr.max_year if yr and yr.max_year else today.year)
        years = list(range(min_year, max_year + 1))

        rows = conn.execute(text("""
            SELECT id, date, category, item, amount_eur, COALESCE(note,'') AS note
            FROM expenses
            WHERE date >= :start_d
              AND date <  :end_d
            ORDER BY date DESC, id DESC;
        """), {"start_d": start_d, "end_d": end_d}).fetchall()

        total = conn.execute(text("""
            SELECT COALESCE(SUM(amount_eur), 0) AS total
            FROM expenses
            WHERE date >= :start_d
              AND date <  :end_d;
        """), {"start_d": start_d, "end_d": end_d}).scalar()


    def opt(selected, value):
        return "selected" if str(selected) == str(value) else ""

    year_options = "".join([f'<option value="{y}" {opt(year, y)}>{y}</option>' for y in years])

    month_options = [f'<option value="all" {opt(month_raw,"all")}>Svi mjeseci</option>']
    for mm in range(1, 13):
        month_options.append(f'<option value="{mm}" {opt(month_raw, mm)}>{mm:02d}</option>')
    month_options = "".join(month_options)

    filters_html = f"""
    <div class="card">
      <form method="get" style="display:flex; gap:10px; align-items:end; flex-wrap:wrap;">
        <div>
          <label>Godina</label><br>
          <select name="year">{year_options}</select>
        </div>
        <div>
          <label>Mjesec</label><br>
          <select name="month">{month_options}</select>
        </div>
        <div>
          <button type="submit">Filtriraj</button>
          <a class="pill" href="/admin/expenses">Reset</a>
        </div>
        <div class="pill" style="margin-left:auto;">Period: {period_label}</div>
      </form>
      <div style="margin-top:10px; display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
        <a class="pill" href="/admin/expenses/new">+ Novi trošak</a>
        <span class="pill">Ukupno: {float(total):.2f} €</span>
      </div>
    </div>
    """

    # --- table html ---
    if not rows:
        table_html = "<p>Nema troškova za odabrani period.</p>"
    else:
        trs = []
        for r in rows:
            trs.append(f"""
              <tr>
                <td>{r[1]}</td>
                <td>{r[2]}</td>
                <td>{r[3]}</td>
                <td style="text-align:right;">{float(r[4]):.2f} €</td>
                <td>{r[5]}</td>
                <td>
                  <div class="actions">
                    <a class="pill" href="/admin/expenses/{r[0]}/edit">Uredi</a>
                    <form method="post" action="/admin/expenses/{r[0]}/delete"
                          onsubmit="return confirm('Obrisati ovaj trošak?');">
                      <button type="submit">Obriši</button>
                    </form>
                  </div>
                </td>
              </tr>
            """)

        table_html = f"""
        <table>
          <tr>
            <th>Datum</th>
            <th>Kategorija</th>
            <th>Opis</th>
            <th style="text-align:right;">Iznos</th>
            <th>Napomena</th>
            <th>Akcije</th>
          </tr>
          {''.join(trs)}
        </table>
        """



    body = f"""
    {filters_html}
    <div class="card">
      <h3>Troškovi</h3>
      {table_html}
    </div>
    """
    return html_page("Troškovi", body)




@app.post("/admin/lock_today")
@admin_required
def lock_today():
    d = date.today()
    with Session(engine) as s:
        row = s.query(DayLock).filter(DayLock.day == d).first()
        if not row:
            s.add(DayLock(day=d, locked=True))
        else:
            row.locked = True
        s.commit()
    return redirect(url_for("admin_dashboard"))

@app.post("/admin/lock_tomorrow")
@admin_required
def lock_tomorrow():
    d = date.today() + timedelta(days=1)
    with Session(engine) as s:
        row = s.query(DayLock).filter(DayLock.day == d).first()
        if not row:
            s.add(DayLock(day=d, locked=True))
        else:
            row.locked = True
        s.commit()
    return redirect(url_for("admin_dashboard"))

@app.post("/admin/unlock_today")
@admin_required
def unlock_today():
    d = date.today()
    with Session(engine) as s:
        row = s.query(DayLock).filter(DayLock.day == d).first()
        if row:
            row.locked = False
            s.commit()
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/unlock_tomorrow")
@admin_required
def unlock_tomorrow():
    d = date.today() + timedelta(days=1)
    with Session(engine) as s:
        row = s.query(DayLock).filter(DayLock.day == d).first()
        if row:
            row.locked = False
            s.commit()
    return redirect(url_for("admin_dashboard"))

@app.get("/admin/requests")
@admin_required
def admin_requests():
    with Session(engine) as s:
        items = (
            s.query(KitchenRequest)
            .options(
                joinedload(KitchenRequest.crop),
                joinedload(KitchenRequest.created_by),
                joinedload(KitchenRequest.applied_by),
            )
            .order_by(
                text("CASE "
                    "WHEN status='zaprimljeno' THEN 1 "
                    "WHEN status='open' THEN 1 "
                    "WHEN status='u_pripremi' THEN 2 "
                    "WHEN status='approved' THEN 2 "
                    "WHEN status='dostavljeno' THEN 3 "
                    "WHEN status='applied' THEN 3 "
                    "WHEN status='nije_moguce' THEN 4 "
                    "WHEN status='rejected' THEN 4 "
                    "ELSE 9 END"),
        KitchenRequest.created_at.desc()
    )
            .limit(50)
            .all()
        )

    body = f"""
    <div class="card">
      <h3>Kuhinjski zahtjevi (zadnjih 50)</h3>
      {kitchen_requests_table(items)}
      <p class="pill">U sljedećem koraku dodajemo: odobri/odbij + quick-fill u dostupnost.</p>
    </div>
    """
    return html_page("Zahtjevi", body)

@app.post("/admin/requests/<int:req_id>/approve")
@admin_required
def approve_request(req_id: int):
    with Session(engine) as s:
        r = s.get(KitchenRequest, req_id)
        if not r:
            return html_message_page("Zahtjev", "Zahtjev nije pronađen.", "/admin/requests")
        if not request_transition_allowed(r.status, "approve"):
            return html_message_page("Nedozvoljena radnja", "Ovaj zahtjev se više ne može odobriti iz trenutnog statusa.", "/admin/requests")

        r.status = "u_pripremi"
        r.assigned_to = getattr(current_user, "username", None) or "garden"
        r.received_at = datetime.now()
        s.commit()
    return redirect(url_for("admin_requests"))


@app.post("/admin/requests/<int:req_id>/reject")
@admin_required
def reject_request(req_id: int):
    with Session(engine) as s:
        r = s.get(KitchenRequest, req_id)
        if not r:
            return html_message_page("Zahtjev", "Zahtjev nije pronađen.", "/admin/requests")
        if not request_transition_allowed(r.status, "reject"):
            return html_message_page("Nedozvoljena radnja", "Ovaj zahtjev se više ne može odbiti iz trenutnog statusa.", "/admin/requests")

        r.status = "nije_moguce"
        s.commit()
    return redirect(url_for("admin_requests"))


@app.post("/admin/requests/<int:req_id>/apply_to_availability")
@admin_required
def apply_request_to_availability(req_id: int):
    with Session(engine) as s:
        r = (
            s.query(KitchenRequest)
            .options(joinedload(KitchenRequest.crop))
            .filter(KitchenRequest.id == req_id)
            .first()
        )
        if not r:
            return html_message_page("Zahtjev", "Zahtjev nije pronađen.", "/admin/requests")

        if canonical_request_status(r.status) == "dostavljeno":
            return html_message_page("Već dostavljeno", "Ovaj zahtjev je već označen kao dostavljen.", "/admin/requests")
        if not request_transition_allowed(r.status, "apply"):
            return html_message_page("Nedozvoljena radnja", "Zahtjev nije u statusu koji dopušta primjenu u dostupnost.", "/admin/requests")

        if is_day_locked(r.requested_for):
            return html_page(
                "Zaključano",
                f"""
                <div class="card">
                  <p>Datum {r.requested_for.isoformat()} je zaključan i ne može se mijenjati.</p>
                  <p><a href="/admin/requests">Nazad</a></p>
                </div>
                """
            )

        existing = (
            s.query(Availability)
            .filter(Availability.date == r.requested_for, Availability.crop_id == r.crop_id)
            .first()
        )
        if existing:
            existing.qty = float(existing.qty or 0) + float(r.qty or 0)
            if r.note:
                existing.note = (existing.note or "")
                existing.note = (existing.note + " | " + r.note) if existing.note else r.note
        else:
            s.add(Availability(date=r.requested_for, crop_id=r.crop_id, qty=r.qty, note=r.note))

        r.status = "dostavljeno"
        r.applied_at = datetime.now()
        r.applied_by_user_id = current_user.id
        r.delivered_at = datetime.now()

        s.commit()

    return redirect(url_for("admin_requests"))


@app.post("/admin/availability/add")
@admin_required
def availability_add():
    crop_id, error_response = parse_int_field(request.form.get("crop_id"), "Kultura", "/admin")
    if error_response:
        return error_response

    qty, error_response = parse_float_field(request.form.get("qty"), "Količina", "/admin")
    if error_response:
        return error_response

    note = request.form.get("note") or None
    day = request.form.get("day", "today")
    if day not in {"today", "tomorrow"}:
        return html_message_page("Greška", "Odabrani datum nije ispravan.", "/admin")

    base = date.today()
    selected_date: date = base if day == "today" else (base + timedelta(days=1))
    if is_day_locked(selected_date):
        return html_page("Zaključano", f"""
            <p>Datum {selected_date.isoformat()} je zaključan i ne može se mijenjati.</p>
            <p><a href="/admin">Nazad</a></p>
        """)



    with Session(engine) as s:
        existing = s.query(Availability).filter(
            Availability.date == selected_date,
            Availability.crop_id == crop_id
        ).first()
        if existing:
            existing.qty = float(existing.qty or 0) + qty
            if note:
                existing.note = note
        else:
            s.add(Availability(date=selected_date, crop_id=crop_id, qty=qty, note=note))
        s.commit()

    return redirect(url_for("admin_dashboard"))

@app.post("/admin/availability/copy_today_to_tomorrow")
@admin_required
def copy_today_to_tomorrow():
    today = date.today()
    tomorrow = today + timedelta(days=1)



    # blokiraj kopiranje ako je sutra zaključano
    if is_day_locked(tomorrow):
        return html_page("Zaključano", f"""
          <div class="card">
            <p>Sutra ({tomorrow.isoformat()}) je zaključano — kopiranje nije moguće.</p>
            <p><a href="/admin">Nazad</a></p>
          </div>
        """)

    with Session(engine) as s:
        # obriši sve za sutra (da se ne duplira)
        s.query(Availability).filter(Availability.date == tomorrow).delete()

        # uzmi sve za danas
        today_rows = s.query(Availability).filter(Availability.date == today).all()

        # kopiraj u sutra
        for a in today_rows:
            s.add(Availability(date=tomorrow, crop_id=a.crop_id, qty=a.qty, note=a.note))

        s.commit()

    return redirect(url_for("admin_dashboard"))



@app.get("/admin/crops")
@admin_required
def crops():
    with Session(engine) as s:
        items = s.query(Crop).order_by(Crop.name_hr.asc(), Crop.name.asc()).all()
    body = f"""
    <div class="card">
      <h3>Dodaj kulturu</h3>
      <form method="post" action="/admin/crops/add">
        <div class="row">
          <div>
            <label>Croatian name</label>
            <input name="name_hr" placeholder="npr. Rajčica" required/>
          </div>
          <div>
            <label>English name</label>
            <input name="name_en" placeholder="e.g. Tomato"/>
          </div>
          <div>
            <label>Kategorija</label>
            <input name="category" placeholder="npr. plodovito"/>
          </div>
        </div>
        <label>Jedinica</label>
        <select name="unit">
          <option value="kg">kg</option>
          <option value="kom">kom</option>
          <option value="vezica">vezica</option>
        </select>
        <button type="submit">Dodaj</button>
      </form>
    </div>

    <div class="card">
      <h3>Postojeće kulture</h3>
      {crops_table(items)}
    </div>
    """
    return html_page("Kulture", body)


@app.post("/admin/crops/add")
@admin_required
def crops_add():
    name_hr = (request.form.get("name_hr") or request.form.get("name") or "").strip()
    name_en = (request.form.get("name_en") or "").strip() or None
    category = (request.form.get("category") or "").strip() or None
    unit = request.form.get("unit") or "kg"

    if not name_hr:
        return html_message_page("Greška", "Naziv kulture je obavezan.", "/admin/crops")
    if unit not in {"kg", "kom", "vezica"}:
        return html_message_page("Greška", "Jedinica nije ispravna.", "/admin/crops")

    with Session(engine) as s:
        existing = s.query(Crop).filter(Crop.name_hr.ilike(name_hr)).first()
        if not existing:
            s.add(Crop(name=name_hr, name_hr=name_hr, name_en=name_en, category=category, unit=unit, active=True))
            s.commit()
    return redirect(url_for("crops"))

@app.get("/admin/users")
@admin_required
def admin_users():
    with Session(engine) as s:
        users = s.query(User).order_by(User.username.asc()).all()

    body = f"""
    <div class="card">
      <h3>Dodaj korisnika</h3>
      <form method="post" action="/admin/users/add">
        <label>Username</label>
        <input name="username" required placeholder="npr. kitchen1"/>

        <label>Lozinka</label>
        <input name="password" type="password" required placeholder="npr. Kuhinja2026!"/>

        <label>Uloga</label>
        <select name="role">
          <option value="kitchen" selected>kitchen</option>
          <option value="worker">worker</option>
          <option value="admin">admin</option>
        </select>

        <label>Aktivan</label>
        <select name="active">
          <option value="true" selected>DA</option>
          <option value="false">NE</option>
        </select>

        <button type="submit">Kreiraj</button>
      </form>
      <p class="pill">Samo admin može kreirati korisnike. Kuhinja nema unos stanja.</p>
    </div>

    <div class="card">
      <h3>Postojeći korisnici</h3>
      {users_table(users)}
    </div>
    """
    return html_page("Korisnici", body)

@app.get("/admin/expenses/<int:expense_id>/edit")
@admin_required
def expenses_edit_form(expense_id: int):
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id, date, category, item, amount_eur, COALESCE(note,'') AS note
            FROM expenses
            WHERE id = :id;
        """), {"id": expense_id}).fetchone()

    if not row:
        body = """
        <div class="card">
          <p>Trošak nije pronađen.</p>
          <a class="pill" href="/admin/expenses">Natrag</a>
        </div>
        """
        return html_page("Trošak", body)

    opts = "".join([
        f'<option value="{c}" {"selected" if c == row[2] else ""}>{c}</option>'
        for c in EXPENSE_CATEGORIES
    ])

    body = f"""
    <div class="card">
      <h3>Uredi trošak #{row[0]}</h3>
      <form method="post" action="/admin/expenses/{row[0]}/edit">
        <label>Datum</label>
        <input type="date" name="date" value="{row[1].isoformat()}" required>

        <label>Kategorija</label>
        <select name="category" required>
          {opts}
        </select>

        <label>Opis</label>
        <input type="text" name="item" value="{row[3]}" required>

        <label>Iznos (€)</label>
        <input type="number" name="amount_eur" step="0.01" min="0" value="{float(row[4]):.2f}" required>

        <label>Napomena</label>
        <input type="text" name="note" value="{row[5]}">

        <button type="submit">Spremi</button>
        <a class="pill" href="/admin/expenses">Natrag</a>
      </form>
    </div>
    """
    return html_page("Uredi trošak", body)

@app.post("/admin/expenses/<int:expense_id>/edit")
@admin_required
def expenses_edit_save(expense_id: int):
    d = request.form.get("date")
    category = (request.form.get("category") or "").strip()
    item = (request.form.get("item") or "").strip()
    amount = request.form.get("amount_eur")
    note = (request.form.get("note") or "").strip()

    if not d or not category or not item or not amount:
        body = f"""
        <div class="card">
          <p>Molim popuni sva obavezna polja.</p>
          <a class="pill" href="/admin/expenses/{expense_id}/edit">Natrag</a>
        </div>
        """
        return html_page("Greška", body)

    with engine.connect() as conn:
        conn.execute(text("""
            UPDATE expenses
            SET date = :d,
                category = :category,
                item = :item,
                amount_eur = :amount_eur,
                note = :note
            WHERE id = :id;
        """), {
            "d": parsed_date,
            "category": category,
            "item": item,
            "amount_eur": parsed_amount,
            "note": note,
            "id": expense_id
        })
        conn.commit()

    return redirect("/admin/expenses")
@app.post("/admin/expenses/<int:expense_id>/delete")
@admin_required
def expenses_delete(expense_id: int):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM expenses WHERE id = :id;"), {"id": expense_id})
        conn.commit()
    return redirect("/admin/expenses")


@app.post("/admin/users/add")
@admin_required
def admin_users_add():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    role = request.form.get("role", "kitchen")
    active_str = request.form.get("active", "true")
    active = True if active_str.lower() == "true" else False

    if not username or not password:
        return html_message_page("Greška", "Korisničko ime i lozinka su obavezni.", "/admin/users")
    if role not in VALID_USER_ROLES:
        return html_message_page("Greška", "Odabrana uloga nije ispravna.", "/admin/users")

    with Session(engine) as s:
        exists = s.query(User).filter(User.username == username).first()
        if not exists:
            s.add(User(
                username=username,
                password_hash=generate_password_hash(password),
                role=role,
                active=active
            ))
            s.commit()

    return redirect(url_for("admin_users"))


@app.get("/admin/harvest")
@admin_required
def harvest():
    with Session(engine) as s:
        last = (
            s.query(Harvest)
            .options(joinedload(Harvest.crop))
            .order_by(Harvest.datetime.desc())
            .limit(20)
            .all()
        )

    body = f"""
    <div class="card">
      <h3>Unos berbe (samo admin)</h3>
      <form method="post" action="/admin/harvest/add">
        <div class="row">
          <div>
            <label>Kultura</label>
            <select name="crop_id" required>
              {crop_options()}
            </select>
          </div>
          <div>
            <label>Količina</label>
            <input name="qty" type="number" step="0.01" min="0" required/>
          </div>
        </div>

        <label>Destinacija</label>
        <select name="destination">
          <option value="kitchen">Kuhinja</option>
          <option value="staff">Staff</option>
          <option value="waste">Otpis</option>
          <option value="other">Ostalo</option>
        </select>

        <label>Napomena (opcionalno)</label>
        <input name="note" placeholder="npr. druga klasa, oštećeno, itd."/>
        <button type="submit">Spremi berbu</button>
      </form>
    </div>

    <div class="card">
      <h3>Zadnjih 20 berbi</h3>
      {harvest_table(last)}
    </div>
    """
    return html_page("Berba", body)


@app.post("/admin/harvest/add")
@admin_required
def harvest_add():
    crop_id, error_response = parse_int_field(request.form.get("crop_id"), "Kultura", "/admin/harvest")
    if error_response:
        return error_response

    qty, error_response = parse_float_field(request.form.get("qty"), "Količina", "/admin/harvest")
    if error_response:
        return error_response

    destination = request.form.get("destination") or "kitchen"
    note = request.form.get("note") or None
    if destination not in {"kitchen", "staff", "waste", "other"}:
        return html_message_page("Greška", "Destinacija nije ispravna.", "/admin/harvest")

    with Session(engine) as s:
        s.add(Harvest(crop_id=crop_id, qty=qty, destination=destination, note=note))
        s.commit()
    return redirect(url_for("harvest"))


@app.get("/admin/report")
@admin_required
def report():
    # --- 1) Read filters (calendar year + month) ---
    today = date.today()
    year = request.args.get("year", type=int) or today.year
    month_raw = request.args.get("month", default=str(today.month))  # "1".."12" or "all"

    # Normalize month
    month = None
    if month_raw == "all":
        month = "all"
    else:
        try:
            m = int(month_raw)
            month = m if 1 <= m <= 12 else today.month
        except ValueError:
            month = today.month

    # --- 2) Build date range [start, end) ---
    if month == "all":
        start_d = date(year, 1, 1)
        end_d = date(year + 1, 1, 1)
        period_label = f"{year} (svi mjeseci)"
    else:
        start_d = date(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        end_d = date(year, month, last_day)  # inclusive date, but we use end-exclusive datetime next day
        period_label = f"{year}-{month:02d}"

        # make end exclusive = first day of next month
        if month == 12:
            end_d = date(year + 1, 1, 1)
        else:
            end_d = date(year, month + 1, 1)

    start_dt = datetime.combine(start_d, time.min)
    end_dt = datetime.combine(end_d, time.min)

    # --- 3) Years dropdown range (based on existing harvests) ---
    with engine.connect() as conn:
        yr = conn.execute(text("""
            SELECT
              MIN(EXTRACT(YEAR FROM h.datetime))::int AS min_year,
              MAX(EXTRACT(YEAR FROM h.datetime))::int AS max_year
            FROM harvests h;
        """)).fetchone()

        min_year = (yr.min_year if yr and yr.min_year else today.year)
        max_year = (yr.max_year if yr and yr.max_year else today.year)

        years = list(range(min_year, max_year + 1))

        # --- 4) Main report query (ONLY harvests) ---
        q = text("""
            SELECT c.name, c.unit, COALESCE(SUM(h.qty), 0) AS total_qty
            FROM harvests h
            JOIN crops c ON c.id = h.crop_id
            WHERE h.datetime >= :start_dt
              AND h.datetime <  :end_dt
            GROUP BY c.name, c.unit
            ORDER BY total_qty DESC NULLS LAST;
        """)
        rows = conn.execute(q, {"start_dt": start_dt, "end_dt": end_dt}).fetchall()
        labels = [r[0] for r in rows]
        values = [float(r[2] or 0) for r in rows]

        chart_labels = json.dumps(labels, ensure_ascii=False)
        chart_values = json.dumps(values)

        # --- 5) Build dropdown options ---
        def opt(selected, value):
            return "selected" if str(selected) == str(value) else ""

        year_options = []
        for y in years:
            year_options.append(f'<option value="{y}" {opt(year, y)}>{y}</option>')

        month_options = []
        month_options.append(f'<option value="all" {opt(month_raw, "all")}>Svi mjeseci</option>')
        for mm in range(1, 13):
            month_options.append(f'<option value="{mm}" {opt(month_raw, mm)}>{mm:02d}</option>')


    # --- 6) Render HTML (outside DB connection) ---
    filters_html = f"""
    <div class="card">
      <form method="get" style="display:flex; gap:10px; align-items:end; flex-wrap:wrap;">
        <div>
          <label>Godina</label><br>
          <select name="year">
            {''.join(year_options)}
          </select>
        </div>

        <div>
          <label>Mjesec</label><br>
          <select name="month">
            {''.join(month_options)}
          </select>
        </div>

        <div>
          <button type="submit">Filtriraj</button>
          <a class="pill" href="/admin/report">Reset</a>
        </div>

        <div class="pill" style="margin-left:auto;">Period: {period_label}</div>
      </form>
    </div>
    """

    chart_html = f"""
    <div class="card">
      <h3>Graf: Berba po kulturama</h3>
      <canvas id="harvestBar" height="120"></canvas>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
      const labels = {chart_labels};
      const values = {chart_values};

      new Chart(document.getElementById('harvestBar'), {{
        type: 'bar',
        data: {{
          labels: labels,
          datasets: [{{ label: 'Berba', data: values }}]
        }},
        options: {{
          responsive: true,
          plugins: {{ legend: {{ display: false }} }},
          scales: {{ y: {{ beginAtZero: true }} }}
        }}
      }});
    </script>
    """

    body = f"""
    {filters_html}
    {chart_html}
    <div class="card">
      <h3>Berba (sumarno)</h3>
      {report_table(rows)}
      <p class="pill">Sljedeće: trend po mjesecima.</p>
    </div>
    """
    return html_page("Izvještaj", body)

EXPENSE_CATEGORIES = [
    "sjeme", "sadnice", "gnojivo", "repromaterijal",
    "zaštita", "alat", "usluga", "rad", "ostalo"
]

@app.get("/admin/expenses/new")
@admin_required
def expenses_new_form():
    today_str = date.today().isoformat()
    opts = "".join([f'<option value="{c}">{c}</option>' for c in EXPENSE_CATEGORIES])

    body = f"""
    <div class="card">
      <h3>Novi trošak</h3>
      <form method="post" action="/admin/expenses/new">
        <label>Datum</label>
        <input type="date" name="date" value="{today_str}" required>

        <label>Kategorija</label>
        <select name="category" required>
          {opts}
        </select>

        <label>Opis</label>
        <input type="text" name="item" placeholder="npr. NPK 25kg" required>

        <label>Iznos (€)</label>
        <input type="number" name="amount_eur" step="0.01" min="0" required>

        <label>Napomena (opcionalno)</label>
        <input type="text" name="note" placeholder="npr. dobavljač / sati rada">

        <button type="submit">Spremi</button>
        <a class="pill" href="/admin/expenses">Natrag</a>
      </form>
    </div>
    """
    return html_page("Novi trošak", body)

@app.post("/admin/expenses/new")
@admin_required
def expenses_new_save():
    d = request.form.get("date")
    category = (request.form.get("category") or "").strip()
    item = (request.form.get("item") or "").strip()
    amount = request.form.get("amount_eur")
    note = (request.form.get("note") or "").strip()

    # basic validation
    if not d or not category or not item or not amount:
        return html_page("Greška", '<div class="card"><p>Molim popuni sva obavezna polja.</p><a class="pill" href="/admin/expenses/new">Natrag</a></div>')

    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO expenses (date, category, item, amount_eur, note)
            VALUES (:d, :category, :item, :amount_eur, :note);
        """), {
            "d": parsed_date,
            "category": category,
            "item": item,
            "amount_eur": parsed_amount,
            "note": note
        })
        conn.commit()

    return redirect("/admin/expenses")


# ---------------- Run ----------------
# init on startup
init_db()
ensure_columns()
ensure_admin_user()
ensure_expenses_table()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)





















