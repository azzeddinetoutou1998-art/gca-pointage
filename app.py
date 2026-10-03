"""GCA – Pointage des salariés : application web autonome (Flask + SQLite).

Les salariés pointent avec un simple code (ex. LRH001), sans aucun compte.
Le gérant se connecte avec un mot de passe (variable MANAGER_PASSWORD).
L'heure est toujours celle du serveur (pas celle du téléphone du salarié).
"""
import csv
import hmac
import io
import os
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, g, jsonify, redirect,
                   render_template, request, session, url_for)
from jinja2 import DictLoader

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Paris"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "pointage.db"))
MANAGER_PASSWORD = os.environ.get("MANAGER_PASSWORD", "")
DOUBLE_PUNCH_SECONDS = 20          # ignore un 2e clic immédiat
STALE_IN_SECONDS = 16 * 3600       # arrivée oubliée de départ => nouvelle arrivée

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS punches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  ts INTEGER NOT NULL,
  type TEXT NOT NULL CHECK(type IN ('in','out')),
  manual INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_punches_emp_ts ON punches(employee_id, ts);
CREATE INDEX IF NOT EXISTS idx_punches_ts ON punches(ts);
"""


# ---------------------------------------------------------------- base de données
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def _close(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- utilitaires
def norm_code(s):
    return "".join((s or "").split()).upper()


def local(ts):
    return datetime.fromtimestamp(ts, TZ)


def hm(ts):
    return local(ts).strftime("%H:%M")


def dfr(ts):
    return local(ts).strftime("%d/%m/%Y")


def dur_hm(sec):
    m = round(sec / 60)
    return f"{m // 60}h{m % 60:02d}"


def dur_dec(sec):
    return f"{sec / 3600:.2f}".replace(".", ",")


app.jinja_env.filters.update(hm=hm, dfr=dfr, dur_hm=dur_hm, dur_dec=dur_dec)


def day_bounds(d_from, d_to):
    start = datetime.combine(d_from, datetime.min.time(), TZ)
    end = datetime.combine(d_to + timedelta(days=1), datetime.min.time(), TZ)
    return int(start.timestamp()), int(end.timestamp())


def parse_date(s, default):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return default


def csv_safe(v):
    """Neutralise les formules Excel (=, +, -, @) dans les cellules texte."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


def build_sessions(rows):
    """Appaire arrivées/départs par salarié. rows triées par (employee_id, ts)."""
    out, cur, cur_emp = [], None, None
    for r in rows:
        if r["employee_id"] != cur_emp:
            if cur:
                out.append(cur)
            cur, cur_emp = None, r["employee_id"]
        if r["type"] == "in":
            if cur:
                out.append(cur)
            cur = dict(eid=r["employee_id"], name=r["name"], code=r["code"],
                       start=r["ts"], end=None, in_id=r["id"], out_id=None)
        elif cur:
            cur["end"], cur["out_id"] = r["ts"], r["id"]
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out


def query_sessions(d_from, d_to, emp_id=None):
    start, end = day_bounds(d_from, d_to)
    sql = ("SELECT p.id, p.employee_id, p.ts, p.type, e.name, e.code "
           "FROM punches p JOIN employees e ON e.id=p.employee_id WHERE p.ts<?")
    args = [end]
    if emp_id:
        sql += " AND p.employee_id=?"
        args.append(emp_id)
    sql += " ORDER BY p.employee_id, p.ts"
    sess = [s for s in build_sessions(db().execute(sql, args).fetchall())
            if start <= s["start"] < end]
    sess.sort(key=lambda s: s["start"], reverse=True)
    return sess


def totals(sessions):
    t = {}
    for s in sessions:
        o = t.setdefault(s["eid"], dict(name=s["name"], code=s["code"], sec=0,
                                        days=set(), open=0))
        o["days"].add(local(s["start"]).date())
        if s["end"]:
            o["sec"] += s["end"] - s["start"]
        else:
            o["open"] += 1
    return sorted(t.values(), key=lambda o: o["name"].lower())


def next_code():
    best = 0
    for r in db().execute("SELECT code FROM employees"):
        c = r["code"]
        if c.startswith("LRH") and c[3:].isdigit():
            best = max(best, int(c[3:]))
    return f"LRH{best + 1:03d}"


# ---------------------------------------------------------------- sécurité
_fails = {}   # ip -> [timestamps d'échecs]


def too_many_failures(ip):
    now = time.time()
    lst = [t for t in _fails.get(ip, []) if now - t < 300]
    _fails[ip] = lst
    return len(lst) >= 10


def note_failure(ip):
    _fails.setdefault(ip, []).append(time.time())


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "?")


def manager_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("mgr"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def _csrf_check():
    if request.method == "POST" and request.path.startswith("/manager") \
            and request.path != "/manager/login":
        sent = request.form.get("csrf_token", "")
        if not session.get("csrf") or not hmac.compare_digest(sent, session["csrf"]):
            abort(400, "Jeton de sécurité invalide, rechargez la page.")


@app.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith(("/manager", "/api")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- page salarié
@app.get("/")
def kiosk():
    return render_template("kiosk.html")


@app.post("/api/punch")
def api_punch():
    ip = client_ip()
    if too_many_failures(ip):
        return jsonify(ok=False, error="Trop d'essais. Réessayez dans quelques minutes."), 429
    code = norm_code((request.get_json(silent=True) or {}).get("code"))
    emp = db().execute("SELECT * FROM employees WHERE code=?", (code,)).fetchone() if code else None
    if not emp or not emp["active"]:
        note_failure(ip)
        return jsonify(ok=False, error="Code inconnu. Vérifiez votre code salarié."), 404
    now = int(time.time())
    last = db().execute("SELECT * FROM punches WHERE employee_id=? ORDER BY ts DESC, id DESC LIMIT 1",
                        (emp["id"],)).fetchone()
    if last and now - last["ts"] < DOUBLE_PUNCH_SECONDS:
        label = "arrivée" if last["type"] == "in" else "départ"
        return jsonify(ok=True, duplicate=True, name=emp["name"], type=last["type"],
                       time=hm(last["ts"]),
                       message=f"{emp['name']} : {label} déjà enregistré(e) à {hm(last['ts'])}.")
    ptype = "out" if (last and last["type"] == "in" and now - last["ts"] < STALE_IN_SECONDS) else "in"
    db().execute("INSERT INTO punches(employee_id, ts, type) VALUES(?,?,?)", (emp["id"], now, ptype))
    db().commit()
    label = "Arrivée" if ptype == "in" else "Départ"
    return jsonify(ok=True, name=emp["name"], type=ptype, time=hm(now),
                   message=f"{label} enregistré(e) à {hm(now)}.")


# ---------------------------------------------------------------- espace gérant
@app.route("/manager/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = client_ip()
        if too_many_failures(ip):
            error = "Trop d'essais. Réessayez dans quelques minutes."
        elif not MANAGER_PASSWORD:
            error = "Aucun mot de passe gérant n'est configuré sur le serveur (MANAGER_PASSWORD)."
        elif hmac.compare_digest(request.form.get("password", ""), MANAGER_PASSWORD):
            session.clear()
            session["mgr"] = True
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/manager") else url_for("dashboard"))
        else:
            note_failure(ip)
            error = "Mot de passe incorrect."
    return render_template("login.html", error=error)


@app.post("/manager/logout")
def logout():
    session.clear()
    return redirect(url_for("kiosk"))


@app.get("/manager")
@manager_required
def dashboard():
    today = datetime.now(TZ).date()
    d_from = parse_date(request.args.get("from"), today.replace(day=1))
    d_to = parse_date(request.args.get("to"), today)
    emp_id = request.args.get("emp", type=int)
    sess = query_sessions(d_from, d_to, emp_id)
    emps = db().execute("SELECT * FROM employees ORDER BY name COLLATE NOCASE").fetchall()
    present = db().execute(
        "SELECT e.name, e.code, p.ts FROM employees e JOIN punches p ON p.id="
        "(SELECT id FROM punches WHERE employee_id=e.id ORDER BY ts DESC, id DESC LIMIT 1) "
        "WHERE e.active=1 AND p.type='in' ORDER BY e.name COLLATE NOCASE").fetchall()
    return render_template("dashboard.html", sessions=sess, totals=totals(sess), emps=emps,
                           d_from=d_from.isoformat(), d_to=d_to.isoformat(), emp_id=emp_id,
                           present=present, today=today.isoformat())


@app.post("/manager/punch/add")
@manager_required
def punch_add():
    try:
        emp_id = int(request.form["emp"])
        day = date.fromisoformat(request.form["date"])
        hh, mm = map(int, request.form["time"].split(":")[:2])
        ts = int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ).timestamp())
        ptype = request.form["type"]
        assert ptype in ("in", "out")
    except (KeyError, ValueError, AssertionError):
        abort(400, "Données de pointage invalides.")
    if not db().execute("SELECT 1 FROM employees WHERE id=?", (emp_id,)).fetchone():
        abort(400, "Salarié inconnu.")
    db().execute("INSERT INTO punches(employee_id, ts, type, manual) VALUES(?,?,?,1)",
                 (emp_id, ts, ptype))
    db().commit()
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/manager/punch/<int:pid>/delete")
@manager_required
def punch_delete(pid):
    db().execute("DELETE FROM punches WHERE id=?", (pid,))
    db().commit()
    return redirect(request.referrer or url_for("dashboard"))


@app.get("/manager/employees")
@manager_required
def employees():
    rows = db().execute("SELECT * FROM employees ORDER BY active DESC, name COLLATE NOCASE").fetchall()
    return render_template("employees.html", emps=rows, suggested=next_code(),
                           error=request.args.get("error"))


@app.post("/manager/employees/add")
@manager_required
def employee_add():
    name = " ".join(request.form.get("name", "").split())
    code = norm_code(request.form.get("code"))
    if not name or not code:
        return redirect(url_for("employees", error="Le nom et le code sont obligatoires."))
    try:
        db().execute("INSERT INTO employees(code, name) VALUES(?,?)", (code, name))
        db().commit()
    except sqlite3.IntegrityError:
        return redirect(url_for("employees", error=f"Le code {code} est déjà attribué."))
    return redirect(url_for("employees"))


@app.post("/manager/employees/<int:eid>/toggle")
@manager_required
def employee_toggle(eid):
    db().execute("UPDATE employees SET active=1-active WHERE id=?", (eid,))
    db().commit()
    return redirect(url_for("employees"))


@app.post("/manager/employees/<int:eid>/code")
@manager_required
def employee_code(eid):
    code = norm_code(request.form.get("code"))
    if not code:
        return redirect(url_for("employees", error="Le code ne peut pas être vide."))
    try:
        db().execute("UPDATE employees SET code=? WHERE id=?", (code, eid))
        db().commit()
    except sqlite3.IntegrityError:
        return redirect(url_for("employees", error=f"Le code {code} est déjà attribué."))
    return redirect(url_for("employees"))


@app.get("/manager/export/<kind>.csv")
@manager_required
def export(kind):
    if kind not in ("detail", "recap"):
        abort(404)
    today = datetime.now(TZ).date()
    d_from = parse_date(request.args.get("from"), today.replace(day=1))
    d_to = parse_date(request.args.get("to"), today)
    sess = query_sessions(d_from, d_to, request.args.get("emp", type=int))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    if kind == "detail":
        w.writerow(["Code", "Salarié", "Date", "Arrivée", "Départ", "Durée (hh:mm)", "Durée (heures)"])
        for s in sess:
            sec = (s["end"] - s["start"]) if s["end"] else None
            w.writerow([csv_safe(s["code"]), csv_safe(s["name"]), dfr(s["start"]), hm(s["start"]),
                        hm(s["end"]) if s["end"] else "", dur_hm(sec) if sec is not None else "",
                        dur_dec(sec) if sec is not None else ""])
    else:
        w.writerow(["Code", "Salarié", "Jours travaillés", "Total (hh:mm)", "Total (heures)",
                    "Pointages sans départ"])
        for o in totals(sess):
            w.writerow([csv_safe(o["code"]), csv_safe(o["name"]), len(o["days"]), dur_hm(o["sec"]),
                        dur_dec(o["sec"]), o["open"]])
    name = f"{'pointages' if kind == 'detail' else 'recap_heures'}_{d_from}_{d_to}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- gabarits
from templates_html import TEMPLATES  # noqa: E402

app.jinja_loader = DictLoader(TEMPLATES)
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
