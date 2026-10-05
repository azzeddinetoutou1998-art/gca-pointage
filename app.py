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
# sites dont le pointage se fait sur une pointeuse extérieure : leurs codes sont refusés sur la page de pointage GCA
SITES_SANS_POINTAGE = {x.strip().lower() for x in os.environ.get("SITES_SANS_POINTAGE", "Aytré").split(",") if x.strip()}
# sites où l'on choisit aussi un bâtiment (exception : Aytré)
SITES_SOUS_ACTIVITE = {x.strip().lower() for x in os.environ.get("SITES_SOUS_ACTIVITE", "Aytré").split(",") if x.strip()}
# pause de midi : pour les horaires listés, un départ pointé dans cette plage = pause, le pointage suivant = reprise
def _parse_window(txt):
    try:
        a, b = txt.split("-")
        h1, m1 = map(int, a.split(":"))
        h2, m2 = map(int, b.split(":"))
        return h1 * 60 + m1, h2 * 60 + m2
    except (ValueError, AttributeError):
        return 11 * 60, 14 * 60


PAUSE_FROM, PAUSE_TO = _parse_window(os.environ.get("PAUSE_WINDOW", "11:00-14:00"))
PAUSE_HORAIRES = {x.strip().lower() for x in os.environ.get("PAUSE_HORAIRES", "Journée").split(",") if x.strip()}
DOUBLE_PUNCH_SECONDS = 20          # ignore un 2e clic immédiat
STALE_IN_SECONDS = 16 * 3600       # arrivée oubliée de départ => nouvelle arrivée
IDLE_SECONDS = int(os.environ.get("MANAGER_IDLE_MINUTES", "10")) * 60   # déconnexion gérant
THEORETICAL_SEC = int(float(os.environ.get("THEORETICAL_HOURS", "7")) * 3600)   # heure théorique / jour


def _parse_breaks(txt):
    """PAUSE_AUTO_MIN="Matin=30,Nuit=30" => {'Matin': 1800, 'Nuit': 1800} (pause déduite si un seul passage dans la journée)."""
    out = {}
    for part in (txt or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                if k.strip() and float(v) > 0:
                    out[k.strip()] = int(float(v) * 60)
            except ValueError:
                pass
    return out


AUTO_BREAK = _parse_breaks(os.environ.get("PAUSE_AUTO_MIN", ""))

CONTRATS = ["CDI", "CDD", "Intérim", "Alternant"]
HORAIRES = ["Matin", "Journée", "Après-midi", "Nuit"]
JOUR_FERIE = "Jour férié"
ABSENCES = ["RTT", "Congé ancienneté", "Arrêt de travail", "Arrêt maladie", "Formation",
            "Absence non autorisée non payée", "Absence autorisée non payée",
            "Congé exceptionnel", "Départ anticipé", "Congé sans solde", "Congé payé", "Jour férié"]
OPTION_KINDS = {"activite": "Activité", "sous_activite": "Bâtiment", "site": "Site", "poste": "Poste"}   # listes modifiables
DEFAULT_OPTIONS = {
    "activite": ["Réception", "GDS", "Préparation"],
    "sous_activite": ["CAISSERIE", "BAT 10", "RECEPTION 71", "PREPARATION 71", "RECEPTION 99", "PREPARATION 99", "MANUTENTION", "ZM 134", "ZM 33", "ANDONS"],
    "site": ["Aytré", "Aigrefeuille", "Alloinay"],
    "poste": ["Agent logistique", "Admin", "Chef d'équipe", "QHSE", "Cariste", "Relais",
              "Préparateur", "Contrôleuse de gestion", "Directeur de site",
              "Responsable d'exploitation", "Alternant", "Responsable d'activité"],
}
EMP_FIELDS = ("site", "activite", "sous_activite", "poste", "contrat", "horaire")
EMP_DATES = ("since", "until")      # présence par défaut (sites sans pointage) : depuis le… / jusqu'au…

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
  sous_activite TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS transfers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    ts INTEGER NOT NULL,
    changes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS horaire_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
  from_day TEXT NOT NULL,
  horaire TEXT NOT NULL,
  UNIQUE(employee_id, from_day));
CREATE TABLE IF NOT EXISTS presences(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
  day TEXT NOT NULL,
  horaire TEXT NOT NULL,
  UNIQUE(employee_id, day));
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
        for c in EMP_DATES:
            if c not in cols:
                conn.execute(f"ALTER TABLE employees ADD COLUMN {c} TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE employees SET since=? WHERE since=''", (datetime.now(TZ).date().replace(day=1).isoformat(),))
        if conn.execute("SELECT COUNT(*) FROM options").fetchone()[0] == 0:
            for kind, vals in DEFAULT_OPTIONS.items():
                for v in vals:
                    conn.execute("INSERT OR IGNORE INTO options(kind, value) VALUES(?,?)", (kind, v))
        # base existante : ajoute une seule fois le site Aytré (une suppression ultérieure est respectée)
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            conn.execute("INSERT OR IGNORE INTO options(kind, value) VALUES('site','Aytré')")
            conn.execute("PRAGMA user_version=1")
        if conn.execute("PRAGMA user_version").fetchone()[0] < 2:      # bâtiments (Aytré) ajoutés une seule fois
            for v in ["CAISSERIE", "BAT 10", "RECEPTION 71", "PREPARATION 71", "RECEPTION 99", "PREPARATION 99", "MANUTENTION", "ZM 134", "ZM 33", "ANDONS"]:
                conn.execute("INSERT OR IGNORE INTO options(kind, value) VALUES('sous_activite',?)", (v,))
            conn.execute("PRAGMA user_version=2")


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
                       activite=r["activite"], sous_activite=r["sous_activite"], poste=r["poste"], contrat=r["contrat"],
                       horaire=r["horaire"], start=r["ts"], end=None, in_id=r["id"], out_id=None)
        elif cur:
            cur["end"], cur["out_id"] = r["ts"], r["id"]
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out


def site_match(site, emp_site, emp_sub=None, sub=None):
    """Filtre par site (et bâtiment) : None / '' = tous ; '__none__' = sans site / sans bâtiment ;
    sinon le nom (sans tenir compte de la casse)."""
    if site:
        es = (emp_site or "").strip().lower()
        if not (es == "" if site == "__none__" else es == site.strip().lower()):
            return False
    if sub:
        eu = (emp_sub or "").strip().lower()
        if not (eu == "" if sub == "__none__" else eu == sub.strip().lower()):
            return False
    return True


def uses_sub(site):
    return bool(site) and site.strip().lower() in SITES_SOUS_ACTIVITE


def req_site():
    v = request.args.get("site") or None
    return None if v == "__all__" else v


def req_sub(site):
    """Bâtiment choisi : uniquement pour les sites concernés (Aytré)."""
    v = request.args.get("sub") or None
    return v if (v and v != "__all__" and uses_sub(site)) else None


def site_tag(site, sub=None):
    """Fragment de nom de fichier (ASCII) pour un site (et son bâtiment)."""
    if not site:
        return ""
    import unicodedata
    if sub:
        return site_tag(site) + site_tag(sub if sub != "__none__" else "sans-batiment")
    base = "sans-site" if site == "__none__" else site
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    return "_" + ("".join(ch for ch in base if ch.isalnum()) or "site")


def query_sessions(d_from, d_to, emp_id=None, site=None, sub=None):
    start, end = day_bounds(d_from, d_to)
    sql = ("SELECT p.id, p.employee_id, p.ts, p.type, e.name, e.code, e.site, e.activite, e.sous_activite, "
           "e.poste, e.contrat, e.horaire "
           "FROM punches p JOIN employees e ON e.id=p.employee_id WHERE p.ts<?")
    args = [end]
    if emp_id:
        sql += " AND p.employee_id=?"
        args.append(emp_id)
    sql += " ORDER BY p.employee_id, p.ts"
    sess = [s for s in build_sessions(db().execute(sql, args).fetchall())
            if start <= s["start"] < end and site_match(site, s["site"], s["sous_activite"], sub)]
    sess.sort(key=lambda s: s["start"], reverse=True)
    return sess


def with_pauses(sessions):
    """Insère une ligne « pause » entre deux passages consécutifs d'un même salarié le même jour (non comptée)."""
    out, prev = [], None
    for s in sessions:                       # triées par (nom, début)
        if (prev and prev["end"] and prev["eid"] == s["eid"]
                and local(prev["start"]).date() == local(s["start"]).date() and s["start"] > prev["end"]):
            p = dict(s)
            p.update(start=prev["end"], end=s["start"], is_pause=True)
            out.append(p)
        out.append(s)
        prev = s
    return out


def day_rows(d_from, d_to, emp_id=None, site=None, sub=None):
    """Une ligne par salarié et par jour (plusieurs passages possibles dans la journée), avec :
    heures travaillées, heure théorique, heures supplémentaires, paniers, quart, tickets, absence.
    Les jours d'absence sans pointage sont inclus."""
    emps = {r["id"]: r for r in db().execute("SELECT * FROM employees")}
    days = {}
    hist = {}
    for h in db().execute("SELECT employee_id, from_day, horaire FROM horaire_history ORDER BY from_day"):
        hist.setdefault(h["employee_id"], []).append((h["from_day"], h["horaire"]))

    def hor_on(eid, d):
        """Horaire du salarié à la date d (historique des changements ; sinon horaire actuel)."""
        cur = emps[eid]["horaire"]
        for from_day, hv in hist.get(eid, ()):
            if from_day <= d.isoformat():
                cur = hv
            else:
                break
        return cur

    def row_for(eid, d):
        e = emps[eid]
        return days.setdefault((eid, d), dict(
            eid=eid, name=e["name"], code=e["code"], site=e["site"], activite=e["activite"], sous_activite=e["sous_activite"],
            poste=e["poste"], contrat=e["contrat"], horaire=hor_on(eid, d), day=d, n=0,
            first=None, last=None, sec=0, open=0, absence=""))

    ordered = sorted(query_sessions(d_from, d_to, emp_id, site, sub), key=lambda s: (s["eid"], s["start"]))
    prev_s = None
    for s in ordered:
        o = row_for(s["eid"], local(s["start"]).date())
        o["n"] += 1
        if (prev_s and prev_s["eid"] == s["eid"] and prev_s["end"] and s["start"] > prev_s["end"]
                and local(prev_s["start"]).date() == local(s["start"]).date()):
            o["pause"] = o.get("pause", 0) + (s["start"] - prev_s["end"])      # pause entre deux passages : non comptée
        prev_s = s
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
    # jours travaillés saisis par le gérant (sites avec pointeuse externe) : horaire du jour => paniers/quart/tickets
    sql = "SELECT employee_id, day, horaire FROM presences WHERE day>=? AND day<=?"
    args = [d_from.isoformat(), d_to.isoformat()]
    if emp_id:
        sql += " AND employee_id=?"
        args.append(emp_id)
    for p in db().execute(sql, args):
        if p["employee_id"] in emps:
            o = row_for(p["employee_id"], date.fromisoformat(p["day"]))
            o["manual_horaire"] = p["horaire"]
            o["explicit"] = True
    # sites sans pointage : présent du lundi au vendredi avec son horaire, sauf absence saisie
    today_d = datetime.now(TZ).date()
    for eid, e in emps.items():
        if emp_id and eid != emp_id:
            continue
        if (e["site"] or "").strip().lower() not in SITES_SANS_POINTAGE:
            continue
        a = max(d_from, date.fromisoformat(e["since"])) if e["since"] else d_from
        b = min(d_to, today_d)
        if e["until"]:
            b = min(b, date.fromisoformat(e["until"]))
        elif not e["active"]:
            continue
        d = a
        while d <= b:
            if d.weekday() < 5 and (eid, d) not in days:
                row_for(eid, d)["manual_horaire"] = hor_on(eid, d) or ""
            d += timedelta(days=1)
    out = []
    for o in days.values():
        o["manual"] = False
        # présence saisie sans pointage : une absence l'emporte, sauf « Jour férié » travaillé (jour saisi explicitement)
        if (o["n"] == 0 and o.get("manual_horaire") is not None
                and (not o["absence"] or (o["absence"] == JOUR_FERIE and o.get("explicit")))):
            o["manual"] = True
            o["horaire"] = o["manual_horaire"]
        elif o["n"] > 0 and o.get("manual_horaire"):
            o["horaire"] = o["manual_horaire"]
        o["worked"] = o["n"] > 0 or o["manual"]
        brk = AUTO_BREAK.get(o["horaire"], 0)
        if o["worked"] and brk and o["n"] == 1 and o["sec"] > brk:
            o["sec"] -= brk            # un seul passage = pause non pointée : déduction automatique
        if o["worked"]:
            o["theo"] = THEORETICAL_SEC
            o["over"] = max(0, o["sec"] - THEORETICAL_SEC)       # ex. 7h50 travaillées => 50 min
            b = shift_bonus(o["horaire"])
            o["panier"], o["quart"], o["ticket"] = b if b else (None, None, None)
            if o["absence"] and o["absence"] != JOUR_FERIE:   # absence saisie : ni panier, ni quart, ni ticket (tous sites) ; un jour férié travaillé garde ses droits
                o["panier"] = o["quart"] = o["ticket"] = 0
        else:
            o["theo"] = o["over"] = 0
            o["panier"] = o["quart"] = o["ticket"] = 0
        out.append(o)
    out = [o for o in out if site_match(site, o["site"], o["sous_activite"], sub)]
    out.sort(key=lambda o: (o["name"].lower(), o["day"]))
    return out


def recap(rows, seed=()):
    """Totaux par salarié à partir des lignes par jour. `seed` : salariés à lister même sans données."""
    t = {}

    def base(eid, m):
        return dict(eid=eid, name=m["name"], code=m["code"], site=m["site"], activite=m["activite"], sous_activite=m["sous_activite"],
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


def seed_employees(emp_id=None, site=None, sub=None):
    sql, args = "SELECT * FROM employees WHERE active=1", []
    if emp_id:
        sql += " AND id=?"
        args.append(emp_id)
    return [e for e in db().execute(sql, args).fetchall() if site_match(site, e["site"], e["sous_activite"], sub)]


NO_CODE_PREFIX = "SC-"      # code interne des salariés sans code de pointage (sites sans pointage, ex. Aytré)


def sans_pointage(site):
    return (site or "").strip().lower() in SITES_SANS_POINTAGE


def has_code(code):
    return bool(code) and not code.startswith(NO_CODE_PREFIX)


def show_code(code):
    return code if has_code(code) else ""


def new_internal_code():
    best = 0
    for r in db().execute("SELECT code FROM employees WHERE code LIKE ?", (NO_CODE_PREFIX + "%",)):
        tail = r["code"][len(NO_CODE_PREFIX):]
        if tail.isdigit():
            best = max(best, int(tail))
    return f"{NO_CODE_PREFIX}{best + 1:04d}"


app.jinja_env.filters["cd"] = show_code
app.jinja_env.filters["cdp"] = lambda c: f" ({c})" if has_code(c) else ""


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


FIELD_LABELS = {"site": "Site", "activite": "Activité", "sous_activite": "Bâtiment", "poste": "Poste", "contrat": "Contrat", "horaire": "Horaire"}


def log_changes(eid, old, new):
    """Enregistre dans l'historique les changements de site / activité / poste / contrat / horaire."""
    parts = [f"{FIELD_LABELS[k]} : {old[k] or '—'} → {new[k] or '—'}"
             for k in FIELD_LABELS if (old[k] or "") != (new[k] or "")]
    if parts:
        db().execute("INSERT INTO transfers(employee_id, ts, changes) VALUES(?,?,?)",
                     (eid, int(time.time()), " · ".join(parts)))
    return parts


def set_horaire(eid, old, new, from_day):
    """Change l'horaire à partir de from_day en gardant l'ancien pour les jours précédents."""
    if old == new:
        return
    if not db().execute("SELECT 1 FROM horaire_history WHERE employee_id=?", (eid,)).fetchone():
        db().execute("INSERT INTO horaire_history(employee_id, from_day, horaire) VALUES(?,?,?)",
                     (eid, "0000-00-00", old or ""))
    db().execute("INSERT OR REPLACE INTO horaire_history(employee_id, from_day, horaire) VALUES(?,?,?)",
                 (eid, from_day.isoformat(), new or ""))


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


def is_pause_out(emp, ts):
    """Départ pointé en plage de pause (midi) par un salarié dont l'horaire pointe sa pause."""
    if (emp["horaire"] or "").strip().lower() not in PAUSE_HORAIRES:
        return False
    t = local(ts)
    return PAUSE_FROM <= t.hour * 60 + t.minute < PAUSE_TO


@app.post("/api/punch")
def api_punch():
    ip = client_ip()
    if too_many_failures(ip):
        return jsonify(ok=False, error="Trop d'essais. Réessayez dans quelques minutes."), 429
    code = norm_code((request.get_json(silent=True) or {}).get("code"))
    emp = db().execute("SELECT * FROM employees WHERE code=?", (code,)).fetchone() if code and has_code(code) else None
    if not emp or not emp["active"]:
        note_failure(ip)
        return jsonify(ok=False, error="Code inconnu. Vérifiez votre code salarié."), 404
    if (emp["site"] or "").strip().lower() in SITES_SANS_POINTAGE:
        return jsonify(ok=False, error=f"{emp['name']} : le pointage du site {emp['site']} se fait sur la pointeuse du site, pas ici."), 403
    now = int(time.time())
    last = db().execute("SELECT * FROM punches WHERE employee_id=? ORDER BY ts DESC, id DESC LIMIT 1",
                        (emp["id"],)).fetchone()
    if last and now - last["ts"] < DOUBLE_PUNCH_SECONDS:
        if last["type"] == "in":
            label = "Arrivée déjà pointée"
        else:
            label = "Pause déjà pointée" if is_pause_out(emp, last["ts"]) else "Départ déjà pointé"
        return jsonify(ok=True, duplicate=True, name=emp["name"], type=last["type"],
                       time=hm(last["ts"]), label=label,
                       message=f"{emp['name']} : {label.lower()} à {hm(last['ts'])}.")
    ptype = "out" if (last and last["type"] == "in" and now - last["ts"] < STALE_IN_SECONDS) else "in"
    kind, note = None, ""
    if ptype == "out" and is_pause_out(emp, now):
        kind = "pause"
        note = "Vous êtes en pause. Pensez à pointer à votre retour."
    elif (ptype == "in" and last and last["type"] == "out" and is_pause_out(emp, last["ts"])
          and local(last["ts"]).date() == local(now).date()):
        kind = "resume"
        note = f"Fin de pause (durée {dur_hm(now - last['ts'])}). Bon après-midi !"
    db().execute("INSERT INTO punches(employee_id, ts, type) VALUES(?,?,?)", (emp["id"], now, ptype))
    db().commit()
    label = {"pause": "Pause pointée", "resume": "Reprise pointée"}.get(kind) or (
        "Arrivée pointée" if ptype == "in" else "Départ pointé")
    return jsonify(ok=True, name=emp["name"], type=ptype, kind=kind, note=note, time=hm(now), label=label,
                   message=f"{emp['name']} : {label.lower()} à {hm(now)}." + (f" {note}" if note else ""))


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
    site = req_site()
    sub = req_sub(site)
    emps = [e for e in db().execute("SELECT * FROM employees ORDER BY name COLLATE NOCASE").fetchall()
            if site_match(site, e["site"], e["sous_activite"], sub)]
    emp_id = request.args.get("emp", type=int)
    if emp_id and emp_id not in {e["id"] for e in emps}:
        emp_id = None                        # le salarié choisi n'est pas du site sélectionné
    sess = query_sessions(d_from, d_to, emp_id, site, sub)
    rows = day_rows(d_from, d_to, emp_id, site, sub)
    present = [p for p in db().execute(
        "SELECT e.name, e.code, e.site, e.sous_activite, p.ts FROM employees e JOIN punches p ON p.id="
        "(SELECT id FROM punches WHERE employee_id=e.id ORDER BY ts DESC, id DESC LIMIT 1) "
        "WHERE e.active=1 AND p.type='in' ORDER BY e.name COLLATE NOCASE").fetchall()
        if site_match(site, p["site"], p["sous_activite"], sub)]
    site_names = [o["value"] for o in db().execute("SELECT value FROM options WHERE kind='site' ORDER BY id")]
    return render_template("dashboard.html", sessions=sess, totals=recap(rows, seed_employees(emp_id, site, sub)),
                           site=site or "", sub=sub or "", site_names=site_names,
                           sub_names=[o["value"] for o in db().execute("SELECT value FROM options WHERE kind='sous_activite' ORDER BY id")],
                           uses_sub=uses_sub(site),
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
    allrows = db().execute("SELECT * FROM employees ORDER BY active DESC, name COLLATE NOCASE").fetchall()
    site = request.args.get("site")           # None = écran de choix du site ; "__all__" ; "__none__" ; ou un nom de site
    lows = lambda e: (e["site"] or "").strip().lower()
    names = [o["value"] for o in db().execute("SELECT value FROM options WHERE kind='site' ORDER BY id")]
    known = {n.lower() for n in names}
    sites = [dict(name=n, n=sum(1 for e in allrows if lows(e) == n.lower() and e["active"]),
                  total=sum(1 for e in allrows if lows(e) == n.lower())) for n in names]
    orphan = [e for e in allrows if lows(e) not in known]       # sans site, ou site retiré de la liste
    if site == "__all__":
        rows, site_label = allrows, "Tous les sites"
    elif site == "__none__":
        rows, site_label = orphan, "Sans site"
    elif site is not None:
        rows = [e for e in allrows if lows(e) == site.strip().lower()]
        site_label = site
        if site.strip().lower() not in known and not rows:
            site = None
    else:
        rows, site_label = [], ""
    sub, sub_label, subs = None, "", []
    if site and uses_sub(site):                # Aytré : choisir aussi le bâtiment
        sub = request.args.get("sub") or None
        subnames = [o["value"] for o in db().execute("SELECT value FROM options WHERE kind='sous_activite' ORDER BY id")]
        subs = [dict(name=n, n=sum(1 for e in rows if (e["sous_activite"] or "").lower() == n.lower() and e["active"]),
                     total=sum(1 for e in rows if (e["sous_activite"] or "").lower() == n.lower())) for n in subnames]
        known_subs = {n.lower() for n in subnames}
        n_nosub = sum(1 for e in rows if (e["sous_activite"] or "").lower() not in known_subs)
        subs_extra = n_nosub
        if sub == "__all__":
            sub_label = "Tous les bâtiments"
        elif sub == "__none__":
            rows = [e for e in rows if (e["sous_activite"] or "").lower() not in known_subs]
            sub_label = "Sans bâtiment"
        elif sub:
            rows = [e for e in rows if (e["sous_activite"] or "").strip().lower() == sub.strip().lower()]
            sub_label = sub
        else:
            rows = []
    else:
        subs_extra = 0
    return render_template("employees.html", emps=rows, site=site, site_label=site_label, sites=sites,
                           sub=sub, sub_label=sub_label, subs=subs, n_nosub=subs_extra,
                           need_sub=bool(site and uses_sub(site) and not sub),
                           n_orphan=len(orphan), n_all=len(allrows), suggested=next_code(), opts=option_lists(),
                           managed=managed_options(), kinds=list(OPTION_KINDS.items()),
                           contrats=CONTRATS, horaires=HORAIRES,
                           error=request.args.get("error"), info=request.args.get("info"))


@app.post("/manager/employees/add")
@manager_required
def employee_add():
    name = " ".join(request.form.get("name", "").split())[:80]
    code = norm_code(request.form.get("code"))[:30]
    f = employee_fields()
    if code and code.startswith(NO_CODE_PREFIX):
        code = ""
    if name and not code and sans_pointage(f["site"]):
        code = new_internal_code()            # code facultatif pour un site sans pointage (Aytré)
    if not name or not code:
        return redirect(url_for("employees", site=request.form.get("back_site") or None,
                                sub=request.form.get("back_sub") or None,
                                error="Le nom est obligatoire, ainsi que le code de pointage (sauf pour un site sans pointage comme Aytré)."))
    try:
        db().execute("INSERT INTO employees(code, name, site, activite, sous_activite, poste, contrat, horaire, since) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     (code, name, f["site"], f["activite"], f["sous_activite"], f["poste"], f["contrat"], f["horaire"],
                      datetime.now(TZ).date().replace(day=1).isoformat()))
        db().commit()
    except sqlite3.IntegrityError:
        db().rollback()
        return redirect(url_for("employees", site=request.form.get("back_site") or None,
                                sub=request.form.get("back_sub") or None,
                                error=f"Le code {code} est déjà attribué."))
    return redirect(url_for("employees", site=f["site"] or request.form.get("back_site") or "__none__",
                            sub=(f["sous_activite"] or "__none__") if uses_sub(f["site"]) else None,
                            info=f"Salarié ajouté : {name}{f' ({code})' if has_code(code) else ' (sans code de pointage)'}."))


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
    pres = db().execute("SELECT * FROM presences WHERE employee_id=? ORDER BY day DESC LIMIT 100", (eid,)).fetchall()
    moves = [dict(day=local(m["ts"]), changes=m["changes"]) for m in db().execute(
        "SELECT ts, changes FROM transfers WHERE employee_id=? ORDER BY ts DESC, id DESC LIMIT 50", (eid,))]
    return render_template("employee.html", e=emp, rows=list(reversed(rows)), tot=tot, opts=opts, moves=moves,
                           presences=[dict(id=p["id"], day=date.fromisoformat(p["day"]), horaire=p["horaire"]) for p in pres],
                           contrats=CONTRATS, horaires=HORAIRES, absence_kinds=ABSENCES,
                           absences=[dict(id=a["id"], day=date.fromisoformat(a["day"]), kind=a["kind"])
                                     for a in absences],
                           d_from=d_from.isoformat(), d_to=d_to.isoformat(), today=today.isoformat(),
                           error=request.args.get("error"), info=request.args.get("info"))


@app.post("/manager/employees/<int:eid>/update")
@manager_required
def employee_update(eid):
    old = get_employee(eid)
    name = " ".join(request.form.get("name", "").split())[:80]
    code = norm_code(request.form.get("code"))[:30]
    f = employee_fields()
    if code.startswith(NO_CODE_PREFIX):
        code = ""
    if name and not code and sans_pointage(f["site"]):
        code = old["code"] if not has_code(old["code"]) else new_internal_code()   # code facultatif (Aytré)
    if not name or not code:
        return redirect(url_for("employee_page", eid=eid,
                                error="Le nom est obligatoire, ainsi que le code de pointage (sauf pour un site sans pointage comme Aytré)."))
    try:
        log_changes(eid, old, f)
        set_horaire(eid, old["horaire"], f["horaire"], datetime.now(TZ).date())
        db().execute("UPDATE employees SET code=?, name=?, site=?, activite=?, sous_activite=?, poste=?, contrat=?, "
                     "horaire=? WHERE id=?",
                     (code, name, f["site"], f["activite"], f["sous_activite"], f["poste"], f["contrat"], f["horaire"], eid))
        db().commit()
    except sqlite3.IntegrityError:
        db().rollback()
        return redirect(url_for("employee_page", eid=eid, error=f"Le code {code} est déjà attribué."))
    return redirect(url_for("employee_page", eid=eid, info="Dossier enregistré."))


@app.post("/manager/employees/<int:eid>/transfer")
@manager_required
def employee_transfer(eid):
    """Transfert : nouvelle activité / site / poste / horaire. Le code de pointage et l'historique ne changent pas."""
    old = get_employee(eid)
    new = {k: old[k] for k in FIELD_LABELS}
    for k in OPTION_KINDS:
        v = ensure_option(k, request.form.get(k + "_new") or request.form.get(k))
        if v:
            new[k] = v
    h = request.form.get("horaire", "").strip()
    if h in HORAIRES:
        new["horaire"] = h
    if not has_code(old["code"]) and not sans_pointage(new["site"]):
        return redirect(url_for("employee_page", eid=eid, _anchor="transfert",
                                error="Ce salarié n'a pas de code de pointage : saisissez-en un dans son dossier avant de le transférer vers ce site."))
    eff = parse_date(request.form.get("horaire_from"), None) or datetime.now(TZ).date()
    changes = log_changes(eid, old, new)
    if old["horaire"] != new["horaire"]:
        set_horaire(eid, old["horaire"], new["horaire"], eff)
        changes = [c + f" (à partir du {eff.strftime('%d/%m/%Y')})" if c.startswith("Horaire") else c for c in changes]
        db().execute("UPDATE transfers SET changes=? WHERE id=(SELECT MAX(id) FROM transfers WHERE employee_id=?)",
                     (" · ".join(changes), eid))
    if not changes:
        db().rollback()
        return redirect(url_for("employee_page", eid=eid, error="Aucun changement : choisissez une nouvelle activité, un nouveau site, un nouveau poste ou un nouvel horaire.",
                                _anchor="transfert"))
    db().execute("UPDATE employees SET site=?, activite=?, sous_activite=?, poste=?, horaire=? WHERE id=?",
                 (new["site"], new["activite"], new["sous_activite"], new["poste"], new["horaire"], eid))
    db().commit()
    return redirect(url_for("employee_page", eid=eid, _anchor="transfert",
                            info="Transfert effectué (" + " ; ".join(changes) + ")." + (f" Le code de pointage {old['code']} est inchangé." if has_code(old["code"]) else "")))


@app.post("/manager/employees/<int:eid>/delete")
@manager_required
def employee_delete(eid):
    """Suppression définitive d'un salarié et de toutes ses données (pointages, absences, présences, historique)."""
    e = get_employee(eid)
    expected = e["code"] if has_code(e["code"]) else "SUPPRIMER"
    if norm_code(request.form.get("confirm")) != expected:
        return redirect(url_for("employee_page", eid=eid, _anchor="supprimer",
                                error=f"Confirmation incorrecte : tapez {expected} pour supprimer."))
    for table in ("punches", "absences", "presences", "transfers", "horaire_history"):
        db().execute(f"DELETE FROM {table} WHERE employee_id=?", (eid,))      # noms de table constants
    db().execute("DELETE FROM employees WHERE id=?", (eid,))
    db().commit()
    site = e["site"] or "__none__"
    sub = (e["sous_activite"] or "__none__") if uses_sub(e["site"]) else None
    return redirect(url_for("employees", site=site, sub=sub,
                            info="Salarié supprimé définitivement : " + e["name"] + (f" ({e['code']})" if has_code(e["code"]) else "") + "."))


@app.post("/manager/employees/<int:eid>/toggle")
@manager_required
def employee_toggle(eid):
    e = get_employee(eid)
    until = "" if not e["active"] else datetime.now(TZ).date().isoformat()   # désactivé => plus de présence par défaut après
    db().execute("UPDATE employees SET active=1-active, until=? WHERE id=?", (until, eid))
    db().commit()
    return redirect(url_for("employees", site=request.form.get("back_site") or None,
                            sub=request.form.get("back_sub") or None))


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


@app.post("/manager/employees/<int:eid>/presence")
@manager_required
def presence_add(eid):
    """Jours travaillés saisis par le gérant (ex. site avec pointeuse externe) : horaire du jour, sans heures."""
    get_employee(eid)
    horaire = request.form.get("horaire", "")
    d1 = parse_date(request.form.get("from"), None)
    d2 = parse_date(request.form.get("to"), d1)
    if horaire not in HORAIRES or not d1 or not d2 or d2 < d1 or (d2 - d1).days > 366:
        return redirect(url_for("employee_page", eid=eid, _anchor="presences",
                                error="Choisissez un horaire et des dates valides (1 an maximum)."))
    weekdays_only = request.form.get("weekdays") == "1" and d1 != d2
    absent = {r["day"] for r in db().execute("SELECT day FROM absences WHERE employee_id=? AND day>=? AND day<=? AND kind<>?",
                                             (eid, d1.isoformat(), d2.isoformat(), JOUR_FERIE))}
    d, n, skipped = d1, 0, 0
    while d <= d2:
        if not (weekdays_only and d.weekday() >= 5):
            if d.isoformat() in absent:
                skipped += 1
            else:
                db().execute("INSERT OR REPLACE INTO presences(employee_id, day, horaire) VALUES(?,?,?)",
                             (eid, d.isoformat(), horaire))
                n += 1
        d += timedelta(days=1)
    db().commit()
    msg = f"{horaire} : {n} jour(s) travaillé(s) enregistré(s)."
    if skipped:
        msg += f" {skipped} jour(s) ignoré(s) car une absence est déjà saisie."
    return redirect(url_for("employee_page", eid=eid, _anchor="presences", info=msg))


@app.post("/manager/presence/<int:pid>/delete")
@manager_required
def presence_delete(pid):
    p = db().execute("SELECT employee_id FROM presences WHERE id=?", (pid,)).fetchone()
    if not p:
        abort(404)
    db().execute("DELETE FROM presences WHERE id=?", (pid,))
    db().commit()
    return redirect(url_for("employee_page", eid=p["employee_id"], _anchor="presences", info="Jour supprimé."))


@app.post("/manager/employees/<int:eid>/since")
@manager_required
def employee_since(eid):
    get_employee(eid)
    d = parse_date(request.form.get("since"), None)
    if not d:
        return redirect(url_for("employee_page", eid=eid, _anchor="presences", error="Date invalide."))
    db().execute("UPDATE employees SET since=? WHERE id=?", (d.isoformat(), eid))
    db().commit()
    return redirect(url_for("employee_page", eid=eid, _anchor="presences",
                            info=f"Présent par défaut à partir du {d.strftime('%d/%m/%Y')}."))


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
ATTR_HEAD = ["Code", "Salarié", "Site", "Activité", "Bâtiment", "Poste", "Contrat", "Horaire"]


def attrs(o):
    return [csv_safe(show_code(o["code"])), csv_safe(o["name"]), csv_safe(o["site"]), csv_safe(o["activite"]),
            csv_safe(o["sous_activite"]), csv_safe(o["poste"]), csv_safe(o["contrat"]), csv_safe(o["horaire"])]


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
    site = req_site()
    sub = req_sub(site)
    who = ""
    if emp_id:
        r = db().execute("SELECT code FROM employees WHERE id=?", (emp_id,)).fetchone()
        who = "_" + ("".join(ch for ch in (r["code"] if r else str(emp_id)) if ch.isalnum()) if r and has_code(r["code"]) else "salarie" + str(emp_id))
    else:
        who = site_tag(site, sub)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    if kind == "detail":
        w.writerow(ATTR_HEAD + ["Date", "Arrivée", "Départ", "Durée (hh:mm)", "Durée (heures)", "Type",
                                "Pause déduite (hh:mm)"])
        sess = sorted(query_sessions(d_from, d_to, emp_id, site, sub), key=lambda s: (s["eid"], s["start"]))
        for s in with_pauses(sess):
            sec = (s["end"] - s["start"]) if s["end"] else None
            if s.get("is_pause"):
                w.writerow(attrs(s) + [dfr(s["start"]), hm(s["start"]), hm(s["end"]), "", "",
                                       "Pause (non comptée)", dur_hm(sec)])
            else:
                w.writerow(attrs(s) + [dfr(s["start"]), hm(s["start"]), hm(s["end"]) if s["end"] else "",
                                       dur_hm(sec) if sec is not None else "",
                                       dur_dec(sec) if sec is not None else "", "Travail", ""])
    elif kind == "jour":
        w.writerow(ATTR_HEAD + ["Date", "Nombre de passages", "Première arrivée", "Dernier départ",
                                "Heures théoriques (hh:mm)", "Heures travaillées (hh:mm)",
                                "Heures travaillées (heures)", "Heures supplémentaires (hh:mm)",
                                "Heures supplémentaires (heures)", "Paniers", "Quart",
                                "Tickets restau", "Justificatif d'absence", "Pause déduite (hh:mm)"])
        for o in day_rows(d_from, d_to, emp_id, site, sub):
            wk = o["worked"]
            w.writerow(attrs(o) + [day_fr(o["day"]), o["n"] or "", hm(o["first"]) if o["first"] else "",
                                   hm(o["last"]) if o["last"] else "",
                                   dur_hm(o["theo"]) if wk else "", dur_hm(o["sec"]) if wk else "",
                                   dur_dec(o["sec"]) if wk else "", dur_hm(o["over"]) if wk else "",
                                   dur_dec(o["over"]) if wk else "", blank_none(o["panier"]),
                                   blank_none(o["quart"]), blank_none(o["ticket"]),
                                   csv_safe(o["absence"]), dur_hm(o["pause"]) if o.get("pause") else ""])
    else:
        w.writerow(ATTR_HEAD + ["Jours travaillés", "Total heures travaillées (hh:mm)",
                                "Total heures travaillées (heures)", "Heures théoriques (hh:mm)",
                                "Heures supplémentaires (hh:mm)", "Heures supplémentaires (heures)",
                                "Paniers", "Quart", "Tickets restau", "Jours d'absence",
                                "Détail des absences", "Pointages sans départ"])
        rows = day_rows(d_from, d_to, emp_id, site, sub)
        for o in recap(rows, seed_employees(emp_id, site, sub)):
            detail = " ; ".join(f"{k} : {n}" for k, n in sorted(o["abs_kinds"].items()))
            w.writerow(attrs(o) + [o["days"], dur_hm(o["sec"]), dur_dec(o["sec"]), dur_hm(o["theo"]),
                                   dur_hm(o["over"]), dur_dec(o["over"]), o["panier"], o["quart"],
                                   o["ticket"], o["abs_days"], csv_safe(detail), o["open"]])
    label = {"detail": "pointages", "jour": "par_jour", "recap": "recap_heures"}[kind]
    name = f"{label}{who}_{d_from}_{d_to}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- export Excel (.xlsx)
@app.get("/manager/export/pointage.xlsx")
@manager_required
def export_xlsx():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return Response("L'export Excel n'est pas disponible : ajoutez « openpyxl » dans requirements.txt "
                        "(voir le README), puis redéployez.", status=503, mimetype="text/plain; charset=utf-8")
    today = datetime.now(TZ).date()
    d_from = parse_date(request.args.get("from"), today.replace(day=1))
    d_to = parse_date(request.args.get("to"), today)
    emp_id = request.args.get("emp", type=int)
    only_staff = request.args.get("only") == "salaries"
    site = req_site()
    sub = req_sub(site)
    who = site_tag(site, sub) if not emp_id else ""
    if emp_id:
        r = db().execute("SELECT code FROM employees WHERE id=?", (emp_id,)).fetchone()
        who = "_" + ("".join(ch for ch in (r["code"] if r else str(emp_id)) if ch.isalnum()) if r and has_code(r["code"]) else "salarie" + str(emp_id))

    wb = Workbook()
    wb.remove(wb.active)
    head_fill = PatternFill("solid", fgColor="1F2A37")
    total_fill = PatternFill("solid", fgColor="E8EDF3")

    def sheet(title, headers, rows, formats=None, total=None):
        """formats : {numéro de colonne (0..) : format Excel}. total : ligne de totaux (liste) ou None."""
        ws = wb.create_sheet(title)
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for row in rows:
            ws.append(row)
        data_end = ws.max_row
        if total:
            ws.append(total)
            for c in ws[ws.max_row]:
                c.font = Font(bold=True)
                c.fill = total_fill
        formats = formats or {}
        for row in ws.iter_rows(min_row=2):
            for i, c in enumerate(row):
                if isinstance(c.value, str):
                    c.data_type = "s"            # texte pur : jamais interprété comme une formule
                if i in formats and c.value is not None:
                    c.number_format = formats[i]
        for i, h in enumerate(headers, 1):
            longest = max([len(str(h))] + [len(str(r[i - 1])) for r in rows if r[i - 1] is not None] + [0])
            ws.column_dimensions[get_column_letter(i)].width = min(max(10, longest + 2), 42)
        ws.freeze_panes = "C2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{data_end}"   # filtre : sans la ligne de total
        ws.row_dimensions[1].height = 32
        return ws

    def dh(sec):                                   # durée : fraction de jour => affichée en [h]:mm, additionnable
        return sec / 86400

    def hrs(sec):
        return round(sec / 3600, 2)

    def tm(ts):
        return local(ts).time().replace(second=0, microsecond=0) if ts else None

    A = ["Code", "Salarié", "Site", "Activité", "Bâtiment", "Poste", "Contrat", "Horaire"]

    K = len(A)                                   # nombre de colonnes d'identité (les formats suivent)

    def at(o):
        return [show_code(o["code"]), o["name"], o["site"], o["activite"], o["sous_activite"], o["poste"], o["contrat"], o["horaire"]]

    # 1. Salariés (fiches)
    sql, args = "SELECT * FROM employees", []
    if emp_id:
        sql += " WHERE id=?"
        args.append(emp_id)
    staff = [e for e in db().execute(sql + " ORDER BY name COLLATE NOCASE", args).fetchall()
             if site_match(site, e["site"], e["sous_activite"], sub)]
    rows = []
    for e in staff:
        b = shift_bonus(e["horaire"])
        rows.append(at(e) + [b[0] if b else None, b[1] if b else None, b[2] if b else None,
                             "Actif" if e["active"] else "Désactivé"])
    sheet("Salariés", A + ["Panier / jour", "Quart / jour", "Tickets restau / jour", "Statut"], rows)

    if not only_staff:
        day = day_rows(d_from, d_to, emp_id, site, sub)
        # 2. Récapitulatif
        rows, sums = [], [0] * 8
        for o in recap(day, seed_employees(emp_id, site, sub)):
            detail = " ; ".join(f"{k} : {n}" for k, n in sorted(o["abs_kinds"].items()))
            vals = [o["days"], dh(o["sec"]), hrs(o["sec"]), dh(o["theo"]), dh(o["over"]), hrs(o["over"]),
                    o["panier"], o["quart"], o["ticket"], o["abs_days"]]
            rows.append(at(o) + vals + [detail, o["open"]])
        tot = ["TOTAL"] + [""] * (K - 1)
        if rows:
            for i in range(K, K + 10):
                tot.append(sum(r[i] for r in rows))
            tot += ["", sum(r[K + 11] for r in rows)]
        sheet("Récapitulatif", A + ["Jours travaillés", "Total heures travaillées (hh:mm)",
                                    "Total heures travaillées (heures)", "Heures théoriques (hh:mm)",
                                    "Heures supplémentaires (hh:mm)", "Heures supplémentaires (heures)",
                                    "Paniers", "Quart", "Tickets restau", "Jours d'absence",
                                    "Détail des absences", "Pointages sans départ"],
              rows, {K + 1: "[h]:mm", K + 2: "0.00", K + 3: "[h]:mm", K + 4: "[h]:mm", K + 5: "0.00"},
              tot if rows else None)
        # 3. Par jour
        rows = []
        for o in day:
            wk = o["worked"]
            rows.append(at(o) + [o["day"], o["n"] or None, tm(o["first"]), tm(o["last"]),
                                 dh(o["theo"]) if wk else None, dh(o["sec"]) if wk else None,
                                 hrs(o["sec"]) if wk else None, dh(o["over"]) if wk else None,
                                 hrs(o["over"]) if wk else None, o["panier"], o["quart"], o["ticket"],
                                 o["absence"] or None, dh(o["pause"]) if o.get("pause") else None])
        sheet("Par jour", A + ["Date", "Nombre de passages", "Première arrivée", "Dernier départ",
                               "Heures théoriques (hh:mm)", "Heures travaillées (hh:mm)",
                               "Heures travaillées (heures)", "Heures supplémentaires (hh:mm)",
                               "Heures supplémentaires (heures)", "Paniers", "Quart", "Tickets restau",
                               "Justificatif d'absence", "Pause déduite (hh:mm)"],
              rows, {K: "dd/mm/yyyy", K + 2: "hh:mm", K + 3: "hh:mm", K + 4: "[h]:mm", K + 5: "[h]:mm",
                     K + 6: "0.00", K + 7: "[h]:mm", K + 8: "0.00", K + 13: "[h]:mm"})
        # 4. Détail des pointages
        rows = []
        for s in with_pauses(sorted(query_sessions(d_from, d_to, emp_id, site, sub), key=lambda s: (s["eid"], s["start"]))):
            sec = (s["end"] - s["start"]) if s["end"] else None
            if s.get("is_pause"):
                rows.append(at(s) + [local(s["start"]).date(), tm(s["start"]), tm(s["end"]), None, None,
                                     "Pause (non comptée)", dh(sec)])
            else:
                rows.append(at(s) + [local(s["start"]).date(), tm(s["start"]), tm(s["end"]),
                                     dh(sec) if sec is not None else None, hrs(sec) if sec is not None else None,
                                     "Travail", None])
        sheet("Détail des pointages", A + ["Date", "Arrivée", "Départ", "Durée (hh:mm)", "Durée (heures)", "Type",
                                           "Pause déduite (hh:mm)"],
              rows, {K: "dd/mm/yyyy", K + 1: "hh:mm", K + 2: "hh:mm", K + 3: "[h]:mm", K + 4: "0.00", K + 6: "[h]:mm"})

    buf = io.BytesIO()
    wb.save(buf)
    name = (f"GCA_salaries{who}.xlsx" if only_staff else f"GCA_pointage{who}_{d_from}_{d_to}.xlsx")
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- démarrage
app.jinja_loader = DictLoader(TEMPLATES)
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
