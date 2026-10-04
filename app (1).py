"""GCA – Pointage des salariés : application web autonome (Flask + SQLite).

Les salariés pointent avec un simple code (ex. LRH001), sans aucun compte.
Le gérant se connecte avec un mot de passe (variable MANAGER_PASSWORD).
L'heure est toujours celle du serveur (pas celle du téléphone du salarié).
"""
import base64
import csv
import hmac
import io
import os
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, g, jsonify, redirect,
                   render_template, request, session, url_for)
from jinja2 import DictLoader

from templates_html import LOGO_B64, TEMPLATES

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Paris"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "pointage.db"))
MANAGER_PASSWORD = os.environ.get("MANAGER_PASSWORD", "")
DOUBLE_PUNCH_SECONDS = 20          # ignore un 2e clic immédiat
STALE_IN_SECONDS = 16 * 3600       # arrivée oubliée de départ => nouvelle arrivée
IDLE_SECONDS = int(os.environ.get("MANAGER_IDLE_MINUTES", "10")) * 60   # déconnexion gérant
THEORETICAL_SEC = int(float(os.environ.get("THEORETICAL_HOURS", "7")) * 3600)   # heure théorique / jour

CONTRATS = ["CDI", "CDD", "Intérim", "Alternant"]
HORAIRES = ["Matin", "Journée", "Après-midi", "Nuit"]
ABSENCES = ["RTT", "Congé ancienneté", "Arrêt de travail", "Arrêt maladie", "Formation",
            "Absence non autorisée non payée", "Absence autorisée non payée",
            "Congé exceptionnel", "Départ anticipé", "Congé sans solde", "Congé payé"]
OPTION_KINDS = {"activite": "Activité", "site": "Site", "poste": "Poste"}   # listes modifiables
DEFAULT_OPTIONS = {
    "activite": ["Réception", "GDS", "Préparation"],
    "site": ["Alloinay", "Aigrefeuille"],
    "poste": ["Agent logistique", "Admin", "Chef d'équipe", "QHSE", "Cariste", "Relais",
              "Préparateur", "Contrôleuse de gestion", "Directeur de site",
              "Responsable d'exploitation", "Alternant", "Responsable d'activité"],
}
EMP_FIELDS = ("site", "activite", "poste", "contrat", "horaire")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  site TEXT NOT NULL DEFAULT '',
  activite TEXT NOT NULL DEFAULT '',
  poste TEXT NOT NULL DEFAULT '',
  contrat TEXT NOT NULL DEFAULT '',
  horaire TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS punches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  ts INTEGER NOT NULL,
  type TEXT NOT NULL CHECK(type IN ('in','out')),
  manual INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_punches_emp_ts ON punches(employee_id, ts);
CREATE INDEX IF NOT EXISTS idx_punches_ts ON punches(ts);
CREATE TABLE IF NOT EXISTS options(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  value TEXT NOT NULL COLLATE NOCASE,
  UNIQUE(kind, value));
CREATE TABLE IF NOT EXISTS absences(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  day TEXT NOT NULL,
  kind TEXT NOT NULL,
  UNIQUE(employee_id, day));
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
        # mise à jour d'une base créée avant l'ajout des nouveaux champs (les données sont conservées)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(employees)")}
        for c in EMP_FIELDS:
            if c not in cols:
                conn.execute(f"ALTER TABLE employees ADD COLUMN {c} TEXT NOT NULL DEFAULT ''")
        if conn.execute("SELECT COUNT(*) FROM options").fetchone()[0] == 0:
            for kind, vals in DEFAULT_OPTIONS.items():
                for v in vals:
                    conn.execute("INSERT OR IGNORE INTO options(kind, value) VALUES(?,?)", (kind, v))


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


def day_fr(d):
    return d.strftime("%d/%m/%Y")


app.jinja_env.filters.update(hm=hm, dfr=dfr, dur_hm=dur_hm, dur_dec=dur_dec, day_fr=day_fr)


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


def shift_bonus(horaire):
    """(panier, quart, tickets restau) par jour travaillé, selon l'horaire. None si horaire non défini."""
    if horaire == "Journée":
        return (0, 0, 1)
    if horaire in ("Matin", "Après-midi", "Nuit"):
        return (1, 1, 0)
    return None


app.jinja_env.globals["shift_bonus"] = shift_bonus
app.jinja_env.globals["THEORETICAL_SEC"] = THEORETICAL_SEC


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
            cur = dict(eid=r["employee_id"], name=r["name"], code=r["code"], site=r["site"],
                       activite=r["activite"], poste=r["poste"], contrat=r["contrat"],
                       horaire=r["horaire"], start=r["ts"], end=None, in_id=r["id"], out_id=None)
        elif cur:
            cur["end"], cur["out_id"] = r["ts"], r["id"]
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out


def query_sessions(d_from, d_to, emp_id=None):
    start, end = day_bounds(d_from, d_to)
    sql = ("SELECT p.id, p.employee_id, p.ts, p.type, e.name, e.code, e.site, e.activite, "
           "e.poste, e.contrat, e.horaire "
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


def day_rows(d_from, d_to, emp_id=None):
    """Une ligne par salarié et par jour (plusieurs passages possibles dans la journée), avec :
    heures travaillées, heure théorique, heures supplémentaires, paniers, quart, tickets, absence.
    Les jours d'absence sans pointage sont inclus."""
    emps = {r["id"]: r for r in db().execute("SELECT * FROM employees")}
    days = {}

    def row_for(eid, d):
        e = emps[eid]
        return days.setdefault((eid, d), dict(
            eid=eid, name=e["name"], code=e["code"], site=e["site"], activite=e["activite"],
            poste=e["poste"], contrat=e["contrat"], horaire=e["horaire"], day=d, n=0,
            first=None, last=None, sec=0, open=0, absence=""))

    for s in query_sessions(d_from, d_to, emp_id):
        o = row_for(s["eid"], local(s["start"]).date())
        o["n"] += 1
        o["first"] = s["start"] if o["first"] is None else min(o["first"], s["start"])
        if s["end"]:
            o["sec"] += s["end"] - s["start"]
            o["last"] = max(o["last"] or 0, s["end"])
        else:
            o["open"] += 1
    sql = "SELECT employee_id, day, kind FROM absences WHERE day>=? AND day<=?"
    args = [d_from.isoformat(), d_to.isoformat()]
    if emp_id:
        sql += " AND employee_id=?"
        args.append(emp_id)
    for a in db().execute(sql, args):
        if a["employee_id"] in emps:
            row_for(a["employee_id"], date.fromisoformat(a["day"]))["absence"] = a["kind"]
    out = []
    for o in days.values():
        o["worked"] = o["n"] > 0
        if o["worked"]:
            o["theo"] = THEORETICAL_SEC
            o["over"] = max(0, o["sec"] - THEORETICAL_SEC)       # ex. 7h50 travaillées => 50 min
            b = shift_bonus(o["horaire"])
            o["panier"], o["quart"], o["ticket"] = b if b else (None, None, None)
        else:
            o["theo"] = o["over"] = 0
            o["panier"] = o["quart"] = o["ticket"] = 0
        out.append(o)
    out.sort(key=lambda o: (o["name"].lower(), o["day"]))
    return out


def recap(rows, seed=()):
    """Totaux par salarié à partir des lignes par jour. `seed` : salariés à lister même sans données."""
    t = {}

    def base(eid, m):
        return dict(eid=eid, name=m["name"], code=m["code"], site=m["site"], activite=m["activite"],
                    poste=m["poste"], contrat=m["contrat"], horaire=m["horaire"], days=0, sec=0,
                    theo=0, over=0, panier=0, quart=0, ticket=0, abs_days=0, abs_kinds={}, open=0)

    for e in seed:
        t[e["id"]] = base(e["id"], e)
    for r in rows:
        o = t.get(r["eid"])
        if o is None:
            o = t[r["eid"]] = base(r["eid"], r)
        if r["worked"]:
            o["days"] += 1
        o["sec"] += r["sec"]
        o["theo"] += r["theo"]
        o["over"] += r["over"]
        o["panier"] += r["panier"] or 0
        o["quart"] += r["quart"] or 0
        o["ticket"] += r["ticket"] or 0
        o["open"] += r["open"]
        if r["absence"]:
            o["abs_days"] += 1
            o["abs_kinds"][r["absence"]] = o["abs_kinds"].get(r["absence"], 0) + 1
    return sorted(t.values(), key=lambda o: o["name"].lower())


def seed_employees(emp_id=None):
    sql, args = "SELECT * FROM employees WHERE active=1", []
    if emp_id:
        sql += " AND id=?"
        args.append(emp_id)
    return db().execute(sql, args).fetchall()


def next_code():
    best = 0
    for r in db().execute("SELECT code FROM employees"):
        c = r["code"]
        if c.startswith("LRH") and c[3:].isdigit():
            best = max(best, int(c[3:]))
    return f"LRH{best + 1:03d}"


# ---------------------------------------------------------------- listes déroulantes
def option_lists():
    return {k: [r["value"] for r in db().execute(
        "SELECT value FROM options WHERE kind=? ORDER BY id", (k,))] for k in OPTION_KINDS}


def managed_options():
    out = {}
    for k in OPTION_KINDS:       # k vient de constantes : pas d'injection possible
        out[k] = [dict(id=r["id"], value=r["value"], used=db().execute(
            f"SELECT COUNT(*) FROM employees WHERE {k}=? COLLATE NOCASE", (r["value"],)).fetchone()[0])
            for r in db().execute("SELECT id, value FROM options WHERE kind=? ORDER BY id", (k,))]
    return out


def ensure_option(kind, value):
    """Ajoute la valeur à la liste si elle n'y est pas encore et renvoie la valeur retenue."""
    value = " ".join((value or "").split())[:60]
    if not value:
        return ""
    r = db().execute("SELECT value FROM options WHERE kind=? AND value=?", (kind, value)).fetchone()
    if r:
        return r["value"]
    db().execute("INSERT INTO options(kind, value) VALUES(?,?)", (kind, value))
    return value


def employee_fields():
    """Lit les champs activité/site/poste/contrat/horaire du formulaire (création d'une valeur possible)."""
    f = {}
    for k in OPTION_KINDS:
        f[k] = ensure_option(k, request.form.get(k + "_new") or request.form.get(k))
    c = request.form.get("contrat", "").strip()
    h = request.form.get("horaire", "").strip()
    f["contrat"] = c if c in CONTRATS else ""
    f["horaire"] = h if h in HORAIRES else ""
    return f


def get_employee(eid):
    e = db().execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not e:
        abort(404)
    return e


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
        now = time.time()
        if not session.get("mgr") or now - session.get("last", 0) > IDLE_SECONDS:
            session.clear()
            return redirect(url_for("login", next=request.path))
        session["last"] = now
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


@app.get("/logo.png")
def logo():
    return Response(base64.b64decode(LOGO_B64), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


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
        label = "Arrivée déjà pointée" if last["type"] == "in" else "Départ déjà pointé"
        return jsonify(ok=True, duplicate=True, name=emp["name"], type=last["type"],
                       time=hm(last["ts"]), label=label,
                       message=f"{emp['name']} : {label.lower()} à {hm(last['ts'])}.")
    ptype = "out" if (last and last["type"] == "in" and now - last["ts"] < STALE_IN_SECONDS) else "in"
    db().execute("INSERT INTO punches(employee_id, ts, type) VALUES(?,?,?)", (emp["id"], now, ptype))
    db().commit()
    label = "Arrivée pointée" if ptype == "in" else "Départ pointé"
    return jsonify(ok=True, name=emp["name"], type=ptype, time=hm(now), label=label,
                   message=f"{emp['name']} : {label.lower()} à {hm(now)}.")


# ---------------------------------------------------------------- espace gérant
@app.route("/manager/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "GET" and request.args.get("new"):
        session.clear()          # un clic sur « Espace gérant » redemande toujours le mot de passe
    if request.method == "POST":
        ip = client_ip()
        if too_many_failures(ip):
            error = "Trop d'essais. Réessayez dans quelques minutes."
        elif not MANAGER_PASSWORD:
            error = "Aucun mot de passe gérant n'est configuré sur le serveur (MANAGER_PASSWORD)."
        elif hmac.compare_digest(request.form.get("password", ""), MANAGER_PASSWORD):
            session.clear()
            session["mgr"] = True
            session["last"] = time.time()
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
    rows = day_rows(d_from, d_to, emp_id)
    emps = db().execute("SELECT * FROM employees ORDER BY name COLLATE NOCASE").fetchall()
    present = db().execute(
        "SELECT e.name, e.code, p.ts FROM employees e JOIN punches p ON p.id="
        "(SELECT id FROM punches WHERE employee_id=e.id ORDER BY ts DESC, id DESC LIMIT 1) "
        "WHERE e.active=1 AND p.type='in' ORDER BY e.name COLLATE NOCASE").fetchall()
    return render_template("dashboard.html", sessions=sess, totals=recap(rows, seed_employees(emp_id)),
                           emps=emps, d_from=d_from.isoformat(), d_to=d_to.isoformat(), emp_id=emp_id,
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


# ---- salariés
@app.get("/manager/employees")
@manager_required
def employees():
    rows = db().execute("SELECT * FROM employees ORDER BY active DESC, name COLLATE NOCASE").fetchall()
    return render_template("employees.html", emps=rows, suggested=next_code(), opts=option_lists(),
                           managed=managed_options(), kinds=list(OPTION_KINDS.items()),
                           contrats=CONTRATS, horaires=HORAIRES,
                           error=request.args.get("error"), info=request.args.get("info"))


@app.post("/manager/employees/add")
@manager_required
def employee_add():
    name = " ".join(request.form.get("name", "").split())[:80]
    code = norm_code(request.form.get("code"))[:30]
    if not name or not code:
        return redirect(url_for("employees", error="Le nom et le code sont obligatoires."))
    f = employee_fields()
    try:
        db().execute("INSERT INTO employees(code, name, site, activite, poste, contrat, horaire) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (code, name, f["site"], f["activite"], f["poste"], f["contrat"], f["horaire"]))
        db().commit()
    except sqlite3.IntegrityError:
        db().rollback()
        return redirect(url_for("employees", error=f"Le code {code} est déjà attribué."))
    return redirect(url_for("employees", info=f"Salarié ajouté : {name} ({code})."))


@app.get("/manager/employees/<int:eid>")
@manager_required
def employee_page(eid):
    emp = get_employee(eid)
    today = datetime.now(TZ).date()
    d_from = parse_date(request.args.get("from"), today.replace(day=1))
    d_to = parse_date(request.args.get("to"), today)
    rows = day_rows(d_from, d_to, eid)
    tot = recap(rows, [emp])[0]
    opts = option_lists()
    for k in OPTION_KINDS:       # garde la valeur actuelle même si elle a été retirée de la liste
        if emp[k] and emp[k].lower() not in [v.lower() for v in opts[k]]:
            opts[k].append(emp[k])
    absences = db().execute("SELECT * FROM absences WHERE employee_id=? ORDER BY day DESC LIMIT 100",
                            (eid,)).fetchall()
    return render_template("employee.html", e=emp, rows=list(reversed(rows)), tot=tot, opts=opts,
                           contrats=CONTRATS, horaires=HORAIRES, absence_kinds=ABSENCES,
                           absences=[dict(id=a["id"], day=date.fromisoformat(a["day"]), kind=a["kind"])
                                     for a in absences],
                           d_from=d_from.isoformat(), d_to=d_to.isoformat(), today=today.isoformat(),
                           error=request.args.get("error"), info=request.args.get("info"))


@app.post("/manager/employees/<int:eid>/update")
@manager_required
def employee_update(eid):
    get_employee(eid)
    name = " ".join(request.form.get("name", "").split())[:80]
    code = norm_code(request.form.get("code"))[:30]
    if not name or not code:
        return redirect(url_for("employee_page", eid=eid, error="Le nom et le code sont obligatoires."))
    f = employee_fields()
    try:
        db().execute("UPDATE employees SET code=?, name=?, site=?, activite=?, poste=?, contrat=?, "
                     "horaire=? WHERE id=?",
                     (code, name, f["site"], f["activite"], f["poste"], f["contrat"], f["horaire"], eid))
        db().commit()
    except sqlite3.IntegrityError:
        db().rollback()
        return redirect(url_for("employee_page", eid=eid, error=f"Le code {code} est déjà attribué."))
    return redirect(url_for("employee_page", eid=eid, info="Dossier enregistré."))


@app.post("/manager/employees/<int:eid>/toggle")
@manager_required
def employee_toggle(eid):
    db().execute("UPDATE employees SET active=1-active WHERE id=?", (eid,))
    db().commit()
    return redirect(url_for("employees"))


# ---- justificatifs d'absence
@app.post("/manager/employees/<int:eid>/absence")
@manager_required
def absence_add(eid):
    get_employee(eid)
    kind = request.form.get("kind", "")
    d1 = parse_date(request.form.get("from"), None)
    d2 = parse_date(request.form.get("to"), d1)
    if kind not in ABSENCES or not d1 or not d2 or d2 < d1 or (d2 - d1).days > 365:
        return redirect(url_for("employee_page", eid=eid,
                                error="Choisissez un justificatif et des dates valides (1 an maximum)."))
    weekdays_only = request.form.get("weekdays") == "1" and d1 != d2
    d, n = d1, 0
    while d <= d2:
        if not (weekdays_only and d.weekday() >= 5):
            db().execute("INSERT OR REPLACE INTO absences(employee_id, day, kind) VALUES(?,?,?)",
                         (eid, d.isoformat(), kind))
            n += 1
        d += timedelta(days=1)
    db().commit()
    return redirect(url_for("employee_page", eid=eid, info=f"{kind} : {n} jour(s) enregistré(s)."))


@app.post("/manager/absence/<int:aid>/delete")
@manager_required
def absence_delete(aid):
    a = db().execute("SELECT employee_id FROM absences WHERE id=?", (aid,)).fetchone()
    if not a:
        abort(404)
    db().execute("DELETE FROM absences WHERE id=?", (aid,))
    db().commit()
    return redirect(url_for("employee_page", eid=a["employee_id"], info="Absence supprimée."))


# ---- listes déroulantes (activité, site, poste)
@app.post("/manager/options/add")
@manager_required
def option_add():
    kind = request.form.get("kind", "")
    if kind not in OPTION_KINDS:
        abort(400)
    value = ensure_option(kind, request.form.get("value"))
    if not value:
        return redirect(url_for("employees", error="Saisissez un nom.", _anchor="listes"))
    db().commit()
    return redirect(url_for("employees", info=f"{OPTION_KINDS[kind]} « {value} » ajouté(e) à la liste.",
                            _anchor="listes"))


@app.post("/manager/options/<int:oid>/delete")
@manager_required
def option_delete(oid):
    db().execute("DELETE FROM options WHERE id=?", (oid,))
    db().commit()
    return redirect(url_for("employees", info="Retiré de la liste (les salariés concernés gardent leur valeur).",
                            _anchor="listes"))


# ---- remise à zéro
@app.post("/manager/reset")
@manager_required
def reset_data():
    scope = request.form.get("scope")
    if request.form.get("confirm", "").strip().upper() != "EFFACER" or scope not in ("punches", "all"):
        return redirect(url_for("employees", error="Confirmation incorrecte : tapez EFFACER pour valider."))
    conn = db()
    conn.execute("DELETE FROM absences")
    conn.execute("DELETE FROM punches")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('punches','absences')")
    if scope == "all":
        conn.execute("DELETE FROM employees")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='employees'")
    conn.commit()
    conn.execute("VACUUM")
    msg = "Tous les pointages et absences ont été effacés." if scope == "punches" else \
        "Toutes les données ont été effacées (salariés, pointages et absences)."
    return redirect(url_for("employees", info=msg))


# ---------------------------------------------------------------- exports CSV
ATTR_HEAD = ["Code", "Salarié", "Site", "Activité", "Poste", "Contrat", "Horaire"]


def attrs(o):
    return [csv_safe(o["code"]), csv_safe(o["name"]), csv_safe(o["site"]), csv_safe(o["activite"]),
            csv_safe(o["poste"]), csv_safe(o["contrat"]), csv_safe(o["horaire"])]


def blank_none(v):
    return "" if v is None else v


@app.get("/manager/export/<kind>.csv")
@manager_required
def export(kind):
    if kind not in ("detail", "jour", "recap"):
        abort(404)
    today = datetime.now(TZ).date()
    d_from = parse_date(request.args.get("from"), today.replace(day=1))
    d_to = parse_date(request.args.get("to"), today)
    emp_id = request.args.get("emp", type=int)
    who = ""
    if emp_id:
        r = db().execute("SELECT code FROM employees WHERE id=?", (emp_id,)).fetchone()
        who = "_" + "".join(ch for ch in (r["code"] if r else str(emp_id)) if ch.isalnum())
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    if kind == "detail":
        w.writerow(ATTR_HEAD + ["Date", "Arrivée", "Départ", "Durée (hh:mm)", "Durée (heures)"])
        sess = sorted(query_sessions(d_from, d_to, emp_id), key=lambda s: (s["name"].lower(), s["start"]))
        for s in sess:
            sec = (s["end"] - s["start"]) if s["end"] else None
            w.writerow(attrs(s) + [dfr(s["start"]), hm(s["start"]), hm(s["end"]) if s["end"] else "",
                                   dur_hm(sec) if sec is not None else "",
                                   dur_dec(sec) if sec is not None else ""])
    elif kind == "jour":
        w.writerow(ATTR_HEAD + ["Date", "Nombre de passages", "Première arrivée", "Dernier départ",
                                "Heures théoriques (hh:mm)", "Heures travaillées (hh:mm)",
                                "Heures travaillées (heures)", "Heures supplémentaires (hh:mm)",
                                "Heures supplémentaires (heures)", "Paniers", "Quart",
                                "Tickets restau", "Justificatif d'absence"])
        for o in day_rows(d_from, d_to, emp_id):
            wk = o["worked"]
            w.writerow(attrs(o) + [day_fr(o["day"]), o["n"] or "", hm(o["first"]) if o["first"] else "",
                                   hm(o["last"]) if o["last"] else "",
                                   dur_hm(o["theo"]) if wk else "", dur_hm(o["sec"]) if wk else "",
                                   dur_dec(o["sec"]) if wk else "", dur_hm(o["over"]) if wk else "",
                                   dur_dec(o["over"]) if wk else "", blank_none(o["panier"]),
                                   blank_none(o["quart"]), blank_none(o["ticket"]),
                                   csv_safe(o["absence"])])
    else:
        w.writerow(ATTR_HEAD + ["Jours travaillés", "Total heures travaillées (hh:mm)",
                                "Total heures travaillées (heures)", "Heures théoriques (hh:mm)",
                                "Heures supplémentaires (hh:mm)", "Heures supplémentaires (heures)",
                                "Paniers", "Quart", "Tickets restau", "Jours d'absence",
                                "Détail des absences", "Pointages sans départ"])
        rows = day_rows(d_from, d_to, emp_id)
        for o in recap(rows, seed_employees(emp_id)):
            detail = " ; ".join(f"{k} : {n}" for k, n in sorted(o["abs_kinds"].items()))
            w.writerow(attrs(o) + [o["days"], dur_hm(o["sec"]), dur_dec(o["sec"]), dur_hm(o["theo"]),
                                   dur_hm(o["over"]), dur_dec(o["over"]), o["panier"], o["quart"],
                                   o["ticket"], o["abs_days"], csv_safe(detail), o["open"]])
    label = {"detail": "pointages", "jour": "par_jour", "recap": "recap_heures"}[kind]
    name = f"{label}{who}_{d_from}_{d_to}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- démarrage
app.jinja_loader = DictLoader(TEMPLATES)
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
