import os
from datetime import date, datetime, timedelta
from functools import wraps
from datetime import time
from flask_login import current_user
import calendar
import json
from sqlalchemy import text
from flask import Flask, request, redirect, url_for
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

from sqlalchemy import (
    create_engine, text, Integer, String, Date, DateTime, Numeric, Boolean, ForeignKey
)
from sqlalchemy.orm import (
    declarative_base, relationship, Session, mapped_column, joinedload
)

# ---------------- App + DB ----------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Base = declarative_base()

# ---------------- Auth (Flask-Login) ----------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if getattr(current_user, "role", None) != "admin":
            return redirect(url_for("kitchen_dashboard"))
        return fn(*args, **kwargs)
    return wrapper


# ---------------- Models ----------------
class User(Base, UserMixin):
    __tablename__ = "users"
    id = mapped_column(Integer, primary_key=True)
    username = mapped_column(String(80), unique=True, nullable=False)
    password_hash = mapped_column(String(255), nullable=False)
    role = mapped_column(String(20), nullable=False, default="kitchen")  # admin / kitchen
    active = mapped_column(Boolean, nullable=False, default=True)

    def get_id(self):
        return str(self.id)


class Crop(Base):
    __tablename__ = "crops"
    id = mapped_column(Integer, primary_key=True)
    name = mapped_column(String(120), nullable=False, unique=True)
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
    crop_id = mapped_column(ForeignKey("crops.id"), nullable=False)

    qty = mapped_column(Numeric(10, 2), nullable=False, default=0)
    note = mapped_column(String(255), nullable=True)

    # open/approved/rejected/applied
    status = mapped_column(String(20), nullable=False, default="open")

    created_by_user_id = mapped_column(ForeignKey("users.id"), nullable=True)
    created_by = relationship("User", foreign_keys=[created_by_user_id])


    # 🆕 NOVO: audit za "primijenjeno u dostupnost"
    applied_at = mapped_column(DateTime, nullable=True)
    applied_by_user_id = mapped_column(ForeignKey("users.id"), nullable=True)
    applied_by = relationship("User", foreign_keys=[applied_by_user_id])

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


# init on startup
def ensure_columns():
    print(">>> ensure_columns RUNNING")
    print(">>> DATABASE_URL =", DATABASE_URL)

    with engine.connect() as conn:
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

        conn.commit()

        cols = conn.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'kitchen_requests'
            ORDER BY ordinal_position;
        """)).fetchall()

        print(">>> kitchen_requests columns:", [c[0] for c in cols])

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
    with Session(engine) as s:
        return s.get(User, int(user_id))


# ---------------- UI helpers ----------------


def html_page(title: str, body: str) -> str:
    nav = ""
    if current_user.is_authenticated:
        if current_user.role == "admin":
            nav = """
            <nav class="nav">
              <a class="nav__link" href="/admin">Dashboard</a>
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
            # kitchen / garden / other roles
            nav = """
            <nav class="nav">
              <a class="nav__link" href="/kitchen">Kuhinja</a>
              <span class="nav__spacer"></span>
              <a class="nav__link nav__link--muted" href="/logout">Logout</a>
            </nav>
            """

    return f"""
    <!doctype html>
    <html lang="hr">
    <head>
      <meta charset="utf-8"/>
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
        body {{
          margin: 0;
          background: var(--bg);
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

        /* ---------------- Tables ---------------- */
        table {{
          width: 100%;
          border-collapse: collapse;
          overflow: hidden;
          border-radius: 12px;
        }}

        th {{
          text-align: left;
          font-size: 12px;
          letter-spacing: 0.6px;
          text-transform: uppercase;
          color: rgba(0,0,0,0.55);
          padding: 12px 10px;
          border-bottom: 1px solid var(--line);
          background: rgba(0,0,0,0.02);
        }}

        td {{
          padding: 12px 10px;
          border-bottom: 1px solid rgba(0,0,0,0.06);
          color: rgba(0,0,0,0.82);
          vertical-align: top;
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
        .status.open {{ background:#fff6df; border-color:#f0d28a; }}
        .status.approved {{ background:#e7f0ff; border-color:#b6d0ff; }}
        .status.applied {{ background:#eaf7ea; border-color:#bfe6bf; }}
        .status.rejected {{ background:#ffe7e7; border-color:#f0b3b3; }}

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

        /* ---------------- Footer (subtle) ---------------- */
        .footer {{
          margin: 24px 0 10px 0;
          color: rgba(0,0,0,0.45);
          font-size: 12.5px;
        }}
      </style>
    </head>

    <body>
      <header class="topbar">
        <div class="brand">
          <div class="brand__left">
            <p class="brand__title">Meneghetti Garden</p>
            <p class="brand__subtitle">v2.0 — operativa • berba • troškovi • zahtjevi</p>
          </div>
          <div class="pill pill--accent">{current_user.role if current_user.is_authenticated else "guest"}</div>
        </div>
        {nav}
      </header>

      <main class="container">
        <div class="pagehead">
          <h2>{title}</h2>
          <div class="hint">Profinjena jednostavnost, bez šarenila.</div>
        </div>

        {body}

        <div class="footer">
          Meneghetti Garden App • interna verzija
        </div>
      </main>
    </body>
    </html>
    """



def crop_options() -> str:
    with Session(engine) as s:
        crops = s.query(Crop).filter(Crop.active == True).order_by(Crop.name.asc()).all()
    if not crops:
        return '<option value="" disabled selected>Nema kultura - dodaj prvo u "Kulture"</option>'
    return "\n".join([f'<option value="{c.id}">{c.name} ({c.unit})</option>' for c in crops])


def availability_table(items) -> str:
    if not items:
        return "<p>Još ništa nije uneseno za danas.</p>"
    rows = "".join([
        f"<tr><td>{a.crop.name}</td><td>{a.qty} {a.crop.unit}</td><td>{a.note or ''}</td></tr>"
        for a in items
    ])
    return f"<table><tr><th>Kultura</th><th>Količina</th><th>Napomena</th></tr>{rows}</table>"

def status_badge(qty: float) -> str:
    if qty <= 0:
        return '<span class="badge bad">🔴 NEMA</span>'
    if qty < 1:
        return '<span class="badge warn">🟡 MALO</span>'
    return '<span class="badge ok">🟢 IMA</span>'


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
            <div class="kname">{a.crop.name}</div>
            {status_badge(q)}
          </div>
          <div class="kqty">{q:g} {a.crop.unit}</div>
          <div class="knote">{(a.note or "")}</div>
        </div>
        """)

    return '<div class="kgrid">' + "\n".join(cards) + "</div>"

def crops_table(items) -> str:
    if not items:
        return "<p>Nema kultura. Dodaj prvu gore.</p>"
    rows = "".join([f"<tr><td>{c.name}</td><td>{c.category or ''}</td><td>{c.unit}</td></tr>" for c in items])
    return f"<table><tr><th>Naziv</th><th>Kategorija</th><th>Jedinica</th></tr>{rows}</table>"


def harvest_table(items) -> str:
    if not items:
        return "<p>Nema unosa berbe.</p>"
    rows = "".join([
        f"<tr><td>{h.datetime.strftime('%Y-%m-%d %H:%M')}</td><td>{h.crop.name}</td>"
        f"<td>{h.qty} {h.crop.unit}</td><td>{h.destination}</td><td>{h.note or ''}</td></tr>"
        for h in items
    ])
    return f"<table><tr><th>Vrijeme</th><th>Kultura</th><th>Količina</th><th>Gdje</th><th>Napomena</th></tr>{rows}</table>"


def report_table(rows) -> str:
    if not rows:
        return "<p>Nema podataka za zadnjih 30 dana.</p>"
    html_rows = "".join([f"<tr><td>{r[0]}</td><td>{r[2] or 0} {r[1]}</td></tr>" for r in rows])
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

    def status_badge_admin(st: str) -> str:
        st = (st or "open").lower()
        label = {
            "open": "OPEN",
            "approved": "APPROVED",
            "applied": "APPLIED",
            "rejected": "REJECTED",
        }.get(st, st.upper())
        return f'<span class="status {st}">{label}</span>'

    rows = ""
    for kr in items:
        ordered_by = kr.created_by.username if kr.created_by else "-"
        applied_by = kr.applied_by.username if kr.applied_by else "-"
        applied_at = kr.applied_at.strftime("%Y-%m-%d %H:%M") if getattr(kr, "applied_at", None) else "-"

        st = (kr.status or "open").lower()

        if st in ("applied", "rejected"):
            actions = '<span class="pill">✅ Primijenjeno</span>' if st == "applied" else '<span class="pill">❌ Odbijeno</span>'
        else:
            actions = f"""
            <div class="actions">
              <form method="post" action="/admin/requests/{kr.id}/approve">
                <button type="submit">Odobri</button>
              </form>
              <form method="post" action="/admin/requests/{kr.id}/reject">
                <button type="submit">Odbij</button>
              </form>
              <form method="post" action="/admin/requests/{kr.id}/apply_to_availability">
                <button type="submit">U dostupnost</button>
              </form>
            </div>
            """

        rows += (
            f"<tr>"
            f"<td>{kr.created_at.strftime('%Y-%m-%d %H:%M')}</td>"
            f"<td>{kr.requested_for.isoformat()}</td>"
            f"<td>{ordered_by}</td>"
            f"<td>{kr.crop.name}</td>"
            f"<td>{kr.qty} {kr.crop.unit}</td>"
            f"<td>{status_badge_admin(kr.status)}</td>"
            f"<td>{kr.note or ''}</td>"
            f"<td>{applied_by}</td>"
            f"<td>{applied_at}</td>"
            f"<td>{actions}</td>"
            f"</tr>"
        )

    return (
        "<table>"
        "<tr>"
        "<th>Kreirano</th>"
        "<th>Za datum</th>"
        "<th>Naručio</th>"
        "<th>Kultura</th>"
        "<th>Količina</th>"
        "<th>Status</th>"
        "<th>Napomena</th>"
        "<th>Primijenio</th>"
        "<th>Primijenjeno</th>"
        "<th>Akcije</th>"
        "</tr>"
        f"{rows}</table>"
    )

def kitchen_status_label(st: str) -> str:
    st = (st or "").lower()
    return {
        "open": "🕒 u obradi",
        "approved": "✅ potvrđeno",
        "applied": "📦 isporučeno",
        "rejected": "❌ odbijeno",
    }.get(st, st)


def kitchen_requests_table_kitchen(items) -> str:
    if not items:
        return "<p>Nema poslanih zahtjeva.</p>"

    rows = "".join([
        f"<tr>"
        f"<td>{kr.created_at.strftime('%Y-%m-%d %H:%M')}</td>"
        f"<td>{kr.requested_for.isoformat()}</td>"
        f"<td>{kr.crop.name}</td>"
        f"<td>{kr.qty} {kr.crop.unit}</td>"
        f"<td>{kitchen_status_label(kr.status)}</td>"
        f"<td>{kr.note or ''}</td>"
        f"</tr>"
        for kr in items
    ])

    return (
        "<table>"
        "<tr><th>Kad</th><th>Za datum</th><th>Kultura</th><th>Količina</th><th>Status</th><th>Napomena</th></tr>"
        f"{rows}</table>"
    )


# ---------------- Routes: Auth ----------------
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
    username = request.form["username"].strip()
    password = request.form["password"]

    with Session(engine) as s:
        u = s.query(User).filter(User.username == username, User.active == True).first()

    if not u or not check_password_hash(u.password_hash, password):
        return html_page("Login", "<p>Pogrešan username ili lozinka.</p><p><a href='/login'>Pokušaj ponovo</a></p>")

    login_user(u)
    return redirect(url_for("root"))


@app.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------- Routes: Root redirect ----------------
@app.get("/")
def root():
    if not current_user.is_authenticated:
        return redirect(url_for("login"))
    if current_user.role == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("kitchen_dashboard"))


# ---------------- Routes: Kitchen (read-only) ----------------
@app.get("/kitchen")
@login_required
def kitchen_dashboard():
    today = date.today()
    tomorrow = today + timedelta(days=1)

    with Session(engine) as s:
        offer_today = (
            s.query(Availability)
            .options(joinedload(Availability.crop))
            .filter(Availability.date == today)
            .all()
        )

        my_requests = (
            s.query(KitchenRequest)
            .options(
                joinedload(KitchenRequest.crop),
                joinedload(KitchenRequest.created_by),
                joinedload(KitchenRequest.applied_by),
            )
            .filter(KitchenRequest.created_by_user_id == current_user.id)
            .order_by(KitchenRequest.created_at.desc())
            .limit(30)
            .all()
        )

    # info blokade + pravilo 13:00
    locked_today = is_day_locked(today)
    locked_tomorrow = is_day_locked(tomorrow)

    now = datetime.now()
    can_order_today = (now.time() < time(13, 0)) and (not locked_today)
    can_order_tomorrow = (not locked_tomorrow)

    # forme
    if can_order_today:
        request_form_today = f"""
        <div class="card">
          <h3>Naruči za danas (do 13:00)</h3>
          <form method="post" action="/kitchen/request">
            <input type="hidden" name="requested_for" value="{today.isoformat()}"/>

            <label>Kultura</label>
            <select name="crop_id" required>{crop_options()}</select>

            <label>Količina</label>
            <input name="qty" type="number" step="0.01" min="0" required/>

            <label>Napomena (opcionalno)</label>
            <input name="note" />

            <button type="submit">Pošalji zahtjev</button>
          </form>
        </div>
        """
    else:
        request_form_today = """
        <div class="card">
          <h3>Naruči za danas</h3>
          <p class="pill">Zatvoreno (nakon 13:00) ili zaključano.</p>
        </div>
        """

    if can_order_tomorrow:
        request_form_tomorrow = f"""
        <div class="card">
          <h3>Naruči za sutra</h3>
          <form method="post" action="/kitchen/request">
            <input type="hidden" name="requested_for" value="{tomorrow.isoformat()}"/>

            <label>Kultura</label>
            <select name="crop_id" required>{crop_options()}</select>

            <label>Količina</label>
            <input name="qty" type="number" step="0.01" min="0" required/>

            <label>Napomena (opcionalno)</label>
            <input name="note" />

            <button type="submit">Pošalji zahtjev</button>
          </form>
        </div>
        """
    else:
        request_form_tomorrow = """
        <div class="card">
          <h3>Naruči za sutra</h3>
          <p class="pill">Sutra je zaključano — naručivanje nije moguće.</p>
        </div>
        """

    requests_card = f"""
    <div class="card">
      <h3>Poslani zahtjevi (zadnjih 30)</h3>
      {kitchen_requests_table_kitchen(my_requests)}
    </div>
    """

    body = f"""
    <div class="card">
      <div class="tabs">
        <button class="tabbtn active" type="button" onclick="showTab('offer')">Ponuda</button>
        <button class="tabbtn" type="button" onclick="showTab('order')">Narudžba</button>
      </div>

      <div id="offer" class="daywrap active">
        {day_section(f"Ponuda danas ({today.isoformat()})", offer_today)}
        <p class="pill">Ponuda je informativna. Zahtjevi se šalju kroz “Narudžba”.</p>
      </div>

      <div id="order" class="daywrap">
        {request_form_today}
        {request_form_tomorrow}
        {requests_card}
      </div>
    </div>

    <script>
      function showTab(which) {{
        const wraps = document.querySelectorAll('.daywrap');
        wraps.forEach(w => w.classList.remove('active'));
        document.getElementById(which).classList.add('active');

        const btns = document.querySelectorAll('.tabbtn');
        btns.forEach(b => b.classList.remove('active'));
        if (which === 'offer') btns[0].classList.add('active');
        else btns[1].classList.add('active');
      }}
    </script>
    """
    return html_page("Kuhinja", body)



@app.post("/kitchen/request")
@login_required
def kitchen_request_post():
    if getattr(current_user, "role", None) != "kitchen":
        return redirect(url_for("admin_dashboard"))

    crop_id = int(request.form["crop_id"])
    qty = float(request.form["qty"])
    note = (request.form.get("note") or "").strip() or None

    requested_for_str = (request.form.get("requested_for") or "").strip()
    if not requested_for_str:
        return redirect(url_for("kitchen_dashboard"))

    requested_for = date.fromisoformat(requested_for_str)

    # blokada: zaključan dan
    if is_day_locked(requested_for):
        return html_page(
            "Zaključano",
            f"<p>Datum {requested_for.isoformat()} je zaključan.</p><p><a href='/kitchen'>Nazad</a></p>"
        )

    # pravilo 13:00 samo za danas
    now = datetime.now()
    if requested_for == date.today() and now.time() >= time(13, 0):
        return html_page(
            "Zatvoreno",
            "<p>Naručivanje za danas je zatvoreno nakon 13:00. Pošalji za sutra.</p><p><a href='/kitchen'>Nazad</a></p>"
        )

    with Session(engine) as s:
        s.add(KitchenRequest(
            requested_for=requested_for,
            crop_id=crop_id,
            qty=qty,
            note=note,
            status="open",
            created_by_user_id=current_user.id
        ))
        s.commit()

    return redirect(url_for("kitchen_dashboard"))




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

    body = f"""
    <div class="card">
      <h3>Status dana</h3>

      <div class="row">
        <div class="card">
          <h3>Danas</h3>
          <p class="pill">{today.isoformat()} — { "ZAKLJUČANO" if locked_today else "OTVORENO" }</p>
          <form method="post" action="/admin/lock_today">
  <button type="submit" { "disabled" if locked_today else "" }>Zatvori danas</button>
</form>

<form method="post" action="/admin/unlock_today">
  <button type="submit" { "disabled" if not locked_today else "" }>Otključaj danas</button>
</form>

        </div>

        <div class="card">
          <h3>Sutra</h3>
          <p class="pill">{tomorrow.isoformat()} — { "ZAKLJUČANO" if locked_tomorrow else "OTVORENO" }</p>
          <form method="post" action="/admin/lock_tomorrow">
  <button type="submit" { "disabled" if locked_tomorrow else "" }>Zatvori sutra</button>
</form>

<form method="post" action="/admin/unlock_tomorrow">
  <button type="submit" { "disabled" if not locked_tomorrow else "" }>Otključaj sutra</button>
</form>

        </div>
      </div>

      <p class="pill">Kad zaključaš dan, više se ne može mijenjati dostupnost za taj datum.</p>
    </div>

    <div class="card">
      <h3>Brze radnje</h3>
      <form method="post" action="/admin/availability/copy_today_to_tomorrow">
        <button type="submit" { "disabled" if locked_tomorrow else "" }>Kopiraj danas → sutra</button>
      </form>
      <p class="pill">Kopira sve stavke dostupnosti za danas u sutra (prepisuje postojeće za sutra).</p>
      <p class="pill">{ "⚠️ Sutra je zaključano — kopiranje je onemogućeno." if locked_tomorrow else "" }</p>
    </div>

    <div class="card">
      <h3>Brzi unos dostupnosti (samo admin)</h3>
      <form method="post" action="/admin/availability/add">
        <div class="row">
          <div>
            <label>Datum</label>
            <select name="day" required>
                <option value="today">Danas</option>
                <option value="tomorrow">Sutra</option>
            </select>

            <label>Kultura</label>
            <select name="crop_id" required>
              {crop_options()}
            </select>
          </div>

          <div>
            <label>Količina</label>
            <input name="qty" type="number" step="0.01" min="0" placeholder="npr. 3.5" required/>
          </div>
        </div>

        <label>Napomena (opcionalno)</label>
        <input name="note" placeholder="npr. samo ujutro, sitno, itd."/>
        <button type="submit">Spremi</button>
      </form>
    </div>

    <div class="card">
      <div class="tabs">
        <button class="tabbtn active" type="button" onclick="showAdminDay('today')">Danas</button>
        <button class="tabbtn" type="button" onclick="showAdminDay('tomorrow')">Sutra</button>
      </div>

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
                    "WHEN status='open' THEN 1 "
                    "WHEN status='approved' THEN 2 "
                    "WHEN status='applied' THEN 3 "
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
        if r:
            r.status = "approved"
            s.commit()
    return redirect(url_for("admin_requests"))


@app.post("/admin/requests/<int:req_id>/reject")
@admin_required
def reject_request(req_id: int):
    with Session(engine) as s:
        r = s.get(KitchenRequest, req_id)
        if r:
            r.status = "rejected"
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
            return redirect(url_for("admin_requests"))

        # sigurnost: ne mijenjamo zaključan dan
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

        # upiši u availability (add ili update)
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

        # označi zahtjev kao approved (jer smo ga “primijenili”)
        r.status = "applied"
        r.applied_at = datetime.now()
        r.applied_by_user_id = current_user.id

        
        s.commit()

    return redirect(url_for("admin_requests"))


@app.post("/admin/availability/add")
@admin_required
def availability_add():
    crop_id = int(request.form["crop_id"])
    qty = float(request.form["qty"])
    note = request.form.get("note") or None
    day = request.form.get("day", "today")
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

    # ✅ blokiraj kopiranje ako je sutra zaključano
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
        items = s.query(Crop).order_by(Crop.name.asc()).all()
    body = f"""
    <div class="card">
      <h3>Dodaj kulturu</h3>
      <form method="post" action="/admin/crops/add">
        <div class="row">
          <div>
            <label>Naziv</label>
            <input name="name" placeholder="npr. Rajčica" required/>
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
    name = request.form["name"].strip()
    category = (request.form.get("category") or "").strip() or None
    unit = request.form.get("unit") or "kg"

    with Session(engine) as s:
        existing = s.query(Crop).filter(Crop.name.ilike(name)).first()
        if not existing:
            s.add(Crop(name=name, category=category, unit=unit, active=True))
            s.commit()
    return redirect(url_for("crops"))

@app.get("/admin/users")
@admin_required
def admin_users():
    with Session(engine) as s:
        users = s.query(User).order_by(User.username.asc()).all()

    body = f"""
    <div class="card">
      <h3>Dodaj korisnika (kitchen)</h3>
      <form method="post" action="/admin/users/add">
        <label>Username</label>
        <input name="username" required placeholder="npr. kitchen1"/>

        <label>Lozinka</label>
        <input name="password" type="password" required placeholder="npr. Kuhinja2026!"/>

        <label>Uloga</label>
        <select name="role">
          <option value="kitchen" selected>kitchen</option>
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
            "d": d,
            "category": category,
            "item": item,
            "amount_eur": amount,
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
    username = request.form["username"].strip()
    password = request.form["password"]
    role = request.form.get("role", "kitchen")
    active_str = request.form.get("active", "true")
    active = True if active_str.lower() == "true" else False

    if not username or not password:
        return redirect(url_for("admin_users"))

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
    crop_id = int(request.form["crop_id"])
    qty = float(request.form["qty"])
    destination = request.form.get("destination") or "kitchen"
    note = request.form.get("note") or None

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
            "d": d,
            "category": category,
            "item": item,
            "amount_eur": amount,
            "note": note
        })
        conn.commit()

    return redirect("/admin/expenses")



    # --- 5) Filter UI ---
    def opt(selected, value):
        return "selected" if str(selected) == str(value) else ""

    month_options = ['<option value="all" ' + opt(month_raw, "all") + '>Svi mjeseci</option>']
    for m in range(1, 13):
        month_options.append(f'<option value="{m}" {opt(month_raw, m)}>{m:02d}</option>')

    year_options = []
    for y in years:
        year_options.append(f'<option value="{y}" {opt(year, y)}>{y}</option>')

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


# ---------------- Run ----------------
# init on startup
init_db()
ensure_columns()
ensure_admin_user()
ensure_expenses_table()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
