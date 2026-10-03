"""Gabarits HTML (Jinja) de l'application de pointage."""

BASE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}GCA{% endblock %}</title>
<link rel="icon" type="image/png" href="/static/logo.png">
<style>
:root{--bg:#f6f7f9;--card:#fff;--tx:#1b1f24;--mu:#6b7380;--bd:#e2e5ea;--ac:#1f6feb;--ok:#1a8f4c;--ko:#c93c37}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--card:#1d2228;--tx:#e8eaed;--mu:#9aa3af;--bd:#2c333b;--ac:#58a6ff;--ok:#3fb868;--ko:#f0716b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0}h2{font-size:16px;margin:0 0 10px}
header{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:14px;flex-wrap:wrap}
nav{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
nav a,nav button{border:1px solid var(--bd);background:var(--card);color:var(--tx);padding:7px 14px;border-radius:99px;text-decoration:none;font:inherit;cursor:pointer}
nav a.on{background:var(--ac);border-color:var(--ac);color:#fff}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:14px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
label{display:flex;flex-direction:column;font-size:12px;color:var(--mu);gap:3px}
input,select,button{font:inherit;color:var(--tx)}
input,select{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:8px 10px;max-width:100%}
.b{background:var(--ac);color:#fff;border:0;padding:9px 16px;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block}
.g{background:transparent;border:1px solid var(--bd);padding:8px 14px;border-radius:8px;cursor:pointer;color:var(--tx);text-decoration:none;display:inline-block}
.x{background:none;border:0;color:var(--ko);cursor:pointer;padding:0 4px}
.sc{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:520px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--bd);white-space:nowrap}
th{font-size:12px;color:var(--mu);font-weight:600}
.mu{color:var(--mu)}.err{color:var(--ko);font-weight:600}.okc{color:var(--ok);font-weight:600}
form.inline{display:inline}
</style>
</head>
<body><main>{% block body %}{% endblock %}</main></body></html>"""

KIOSK = r"""{% extends "base.html" %}
{% block title %}GCA – Pointage{% endblock %}
{% block body %}
<div class="card" style="text-align:center;max-width:460px;margin:24px auto">
  <img src="/static/logo.png" alt="GCA" style="height:80px;width:auto"><h1 style="margin-top:6px">Pointage</h1>
  <div id="clk" style="font-size:44px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:8px">--:--:--</div>
  <div id="dte" class="mu"></div>
  <form id="f" autocomplete="off" style="margin-top:18px;text-align:left">
    <label>Code salarié
      <input id="code" name="code" placeholder="Ex. LRH001" autocomplete="off" autocapitalize="characters"
             autocorrect="off" spellcheck="false" inputmode="text"
             style="font-size:24px;text-align:center;letter-spacing:2px;text-transform:uppercase" autofocus>
    </label>
    <button id="ok" class="b" type="submit" style="width:100%;margin-top:14px;padding:20px;font-size:22px;font-weight:600;border-radius:14px;background:var(--ok)">OK</button>
  </form>
  <div id="res" style="margin-top:14px;font-size:17px;min-height:26px"></div>
  <p class="mu" style="font-size:13px;margin:10px 0 0">Saisissez votre code puis appuyez sur OK. L'arrivée ou le départ est détecté automatiquement.</p>
  <p style="margin:14px 0 0"><a class="mu" style="font-size:13px" href="/manager">Espace gérant</a></p>
</div>
<script>
const clk=document.getElementById("clk"),dte=document.getElementById("dte"),res=document.getElementById("res"),code=document.getElementById("code"),ok=document.getElementById("ok");
function tick(){const d=new Date();clk.textContent=d.toLocaleTimeString("fr-FR");dte.textContent=d.toLocaleDateString("fr-FR",{weekday:"long",day:"numeric",month:"long",year:"numeric"})}
tick();setInterval(tick,1000);
let timer;
document.getElementById("f").addEventListener("submit",async e=>{
  e.preventDefault();
  const v=code.value.trim();
  if(!v){show("Saisissez votre code salarié.",false);return}
  ok.disabled=true;
  try{
    const r=await fetch("/api/punch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:v})});
    const j=await r.json();
    if(j.ok){show("✓ "+j.message,true);code.value=""}else show(j.error||"Erreur.",false);
  }catch(err){show("Connexion impossible. Réessayez.",false)}
  ok.disabled=false;code.focus();
});
function show(t,good){res.textContent=t;res.className=good?"okc":"err";clearTimeout(timer);timer=setTimeout(()=>{res.textContent=""},8000)}
</script>
{% endblock %}"""

LOGIN = r"""{% extends "base.html" %}
{% block title %}GCA – Connexion gérant{% endblock %}
{% block body %}
<div class="card" style="max-width:380px;margin:40px auto">
  <img src="/static/logo.png" alt="GCA" style="height:64px;width:auto"><h1 style="margin-top:6px">Espace gérant</h1>
  <form method="post" style="margin-top:14px">
    <label>Mot de passe<input type="password" name="password" autofocus required></label>
    {% if error %}<p class="err">{{ error }}</p>{% endif %}
    <button class="b" type="submit" style="margin-top:12px;width:100%">Se connecter</button>
  </form>
  <p style="margin:14px 0 0"><a class="mu" style="font-size:13px" href="/">← Page de pointage</a></p>
</div>
{% endblock %}"""

NAV = r"""<header><div style="display:flex;align-items:center;gap:12px"><img src="/static/logo.png" alt="GCA" style="height:48px;width:auto"><h1>Espace gérant</h1></div>
<nav>
  <a href="{{ url_for('dashboard') }}" class="{{ 'on' if active=='dash' }}">Pointages & exports</a>
  <a href="{{ url_for('employees') }}" class="{{ 'on' if active=='emps' }}">Salariés</a>
  <a href="{{ url_for('kiosk') }}">Page de pointage</a>
  <form class="inline" method="post" action="{{ url_for('logout') }}"><button type="submit">Déconnexion</button></form>
</nav></header>"""

DASHBOARD = r"""{% extends "base.html" %}
{% block title %}GCA – Pointages{% endblock %}
{% block body %}
{% set active='dash' %}""" + NAV + r"""
<div class="card"><h2>Présents en ce moment ({{ present|length }})</h2>
  {% if present %}<div>{% for p in present %}<span style="display:inline-block;margin:2px 10px 2px 0"><span style="color:var(--ok)">●</span> {{ p.name }} <span class="mu">depuis {{ p.ts|hm }}</span></span>{% endfor %}</div>
  {% else %}<span class="mu">Personne.</span>{% endif %}
</div>
<div class="card">
  <form method="get" class="row">
    <label>Du<input type="date" name="from" value="{{ d_from }}"></label>
    <label>Au<input type="date" name="to" value="{{ d_to }}"></label>
    <label>Salarié<select name="emp"><option value="">Tous</option>
      {% for e in emps %}<option value="{{ e.id }}" {{ 'selected' if e.id==emp_id }}>{{ e.name }} ({{ e.code }})</option>{% endfor %}</select></label>
    <button class="b" type="submit">Filtrer</button>
    <a class="g" href="{{ url_for('export', kind='detail') }}?from={{ d_from }}&to={{ d_to }}&emp={{ emp_id or '' }}">⬇ Export détail (CSV)</a>
    <a class="g" href="{{ url_for('export', kind='recap') }}?from={{ d_from }}&to={{ d_to }}&emp={{ emp_id or '' }}">⬇ Export récap. heures (CSV)</a>
  </form>
</div>
<div class="card"><h2>Récapitulatif des heures</h2><div class="sc"><table>
  <tr><th>Code</th><th>Salarié</th><th>Jours</th><th>Total</th><th>Heures décimales</th><th>Sans départ</th></tr>
  {% for o in totals %}<tr><td>{{ o.code }}</td><td>{{ o.name }}</td><td>{{ o.days|length }}</td><td>{{ o.sec|dur_hm }}</td><td>{{ o.sec|dur_dec }}</td><td>{{ o.open or '' }}</td></tr>
  {% else %}<tr><td colspan="6" class="mu">Aucune donnée sur la période.</td></tr>{% endfor %}
</table></div></div>
<div class="card"><h2>Détail des pointages ({{ sessions|length }})</h2><div class="sc"><table>
  <tr><th>Code</th><th>Salarié</th><th>Date</th><th>Arrivée</th><th>Départ</th><th>Durée</th></tr>
  {% for s in sessions %}<tr><td>{{ s.code }}</td><td>{{ s.name }}</td><td>{{ s.start|dfr }}</td>
    <td>{{ s.start|hm }}
      <form class="inline" method="post" action="{{ url_for('punch_delete', pid=s.in_id) }}" onsubmit="return confirm('Supprimer ce pointage d\'arrivée ?')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="x" title="Supprimer">×</button></form></td>
    <td>{% if s.end %}{{ s.end|hm }}
      <form class="inline" method="post" action="{{ url_for('punch_delete', pid=s.out_id) }}" onsubmit="return confirm('Supprimer ce pointage de départ ?')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="x" title="Supprimer">×</button></form>
      {% else %}<span class="err">en cours / oublié</span>{% endif %}</td>
    <td>{% if s.end %}{{ (s.end - s.start)|dur_hm }}{% endif %}</td></tr>
  {% else %}<tr><td colspan="6" class="mu">Aucune donnée.</td></tr>{% endfor %}
</table></div></div>
<div class="card"><h2>Ajouter / corriger un pointage manuellement</h2>
  <form method="post" action="{{ url_for('punch_add') }}" class="row">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label>Salarié<select name="emp" required>{% for e in emps if e.active %}<option value="{{ e.id }}">{{ e.name }} ({{ e.code }})</option>{% endfor %}</select></label>
    <label>Date<input type="date" name="date" value="{{ today }}" required></label>
    <label>Heure<input type="time" name="time" required></label>
    <label>Type<select name="type"><option value="in">Arrivée</option><option value="out">Départ</option></select></label>
    <button class="b" type="submit">Ajouter</button>
  </form>
  <p class="mu" style="font-size:13px;margin:8px 0 0">Utile pour un départ oublié. Les heures saisies sont celles du fuseau Europe/Paris.</p>
</div>
{% endblock %}"""

EMPLOYEES = r"""{% extends "base.html" %}
{% block title %}GCA – Salariés{% endblock %}
{% block body %}
{% set active='emps' %}""" + NAV + r"""
<div class="card"><h2>Ajouter un salarié</h2>
  <form method="post" action="{{ url_for('employee_add') }}" class="row">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label>Nom complet<input name="name" placeholder="Ex. Marie Dupont" required></label>
    <label>Code de pointage<input name="code" value="{{ suggested }}" required style="text-transform:uppercase"></label>
    <button class="b" type="submit">Ajouter</button>
  </form>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
</div>
<div class="card"><h2>Salariés ({{ emps|length }})</h2><div class="sc"><table>
  <tr><th>Code de pointage</th><th>Nom</th><th>Statut</th><th></th></tr>
  {% for e in emps %}<tr>
    <td><form method="post" action="{{ url_for('employee_code', eid=e.id) }}" class="inline">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input name="code" value="{{ e.code }}" size="9" style="text-transform:uppercase;font-weight:600">
      <button class="g" type="submit" style="padding:5px 10px">Changer</button></form></td>
    <td>{{ e.name }}</td>
    <td>{% if e.active %}<span class="okc">Actif</span>{% else %}<span class="mu">Désactivé</span>{% endif %}</td>
    <td style="text-align:right"><form method="post" action="{{ url_for('employee_toggle', eid=e.id) }}" class="inline">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button class="g" type="submit" style="padding:5px 10px">{{ 'Désactiver' if e.active else 'Réactiver' }}</button></form></td>
  </tr>{% else %}<tr><td colspan="4" class="mu">Aucun salarié.</td></tr>{% endfor %}
</table></div>
<p class="mu" style="font-size:13px;margin:8px 0 0">Communiquez à chaque salarié son code personnel. Un salarié désactivé ne peut plus pointer, mais son historique est conservé.</p></div>
{% endblock %}"""

TEMPLATES = {
    "base.html": BASE,
    "kiosk.html": KIOSK,
    "login.html": LOGIN,
    "dashboard.html": DASHBOARD,
    "employees.html": EMPLOYEES,
}
