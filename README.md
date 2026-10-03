# GCA – Pointage des salariés

Application web autonome : les salariés pointent avec **un simple code** (ex. `LRH001`), **sans aucun compte**.
Le gérant se connecte avec un mot de passe, gère les salariés, corrige les pointages et télécharge les exports CSV (lisibles dans Excel).
Aucune mention de Claude : le nom et le logo GCA s'affichent partout.

## Fonctionnement

- **Page salarié** (`/`) : le salarié saisit son code et appuie sur **OK**. L'heure est enregistrée par le **serveur** (l'heure du téléphone n'est pas utilisée). Le système détermine seul s'il s'agit d'une arrivée ou d'un départ.
- **Espace gérant** (`/manager`), protégé par mot de passe :
  - qui est présent en ce moment ;
  - filtres par période et par salarié, récapitulatif des heures, détail des pointages ;
  - exports CSV (détail et récapitulatif) ;
  - ajout manuel / suppression d'un pointage (départ oublié, erreur) ;
  - gestion des salariés : ajout avec code (LRH001, LRH002… proposé automatiquement), changement de code, désactivation (l'historique est conservé).
- Fuseau horaire : Europe/Paris (modifiable avec `TZ_NAME`).

## Variables d'environnement

| Variable | Rôle |
|---|---|
| `MANAGER_PASSWORD` | **Obligatoire.** Mot de passe de l'espace gérant (8 caractères ou plus). |
| `SECRET_KEY` | Recommandée : longue chaîne aléatoire (signe les sessions). |
| `DB_PATH` | Chemin du fichier SQLite, sur un disque **persistant** (ex. `/data/pointage.db`). |
| `PORT` | Port d'écoute (fourni par l'hébergeur). |
| `TZ_NAME` | Fuseau horaire, défaut `Europe/Paris`. |

## Lancer sur un ordinateur (test)

```bash
pip install -r requirements.txt
export MANAGER_PASSWORD="choisissez-un-mot-de-passe"
python app.py          # http://localhost:5000
```

## Mise en ligne sans nom de domaine (exemple : Render)

Vous n'avez pas besoin d'acheter un domaine : l'hébergeur fournit une adresse du type `gca-pointage.onrender.com`.
Les écrans et libellés des sites changent parfois : suivez l'esprit des étapes. Vérifiez aussi les tarifs sur le site de l'hébergeur.

1. **Créer un compte GitHub** (gratuit) puis un dépôt **privé** nommé par exemple `gca-pointage`.
2. Dans ce dépôt : « Add file → Upload files », puis déposez **tout le contenu** de ce dossier (`app.py`, `templates_html.py`, `Dockerfile`, `requirements.txt`, `Procfile`, et le dossier `static` avec `logo.png`). Validez (« Commit »).
3. **Créer un compte sur render.com**, puis « New → Web Service » et choisissez ce dépôt GitHub.
4. Réglages :
   - Language / Runtime : **Docker**.
   - Instance : un **service payant** (la conservation des données sur disque n'est pas disponible sur l'offre gratuite).
   - **Disk** : ajoutez un disque, « Mount path » = `/data`, taille 1 Go suffit.
   - **Environment** : ajoutez
     - `MANAGER_PASSWORD` = votre mot de passe gérant
     - `SECRET_KEY` = une longue suite de caractères au hasard
     - `DB_PATH` = `/data/pointage.db`
5. Lancez le déploiement. À la fin, Render affiche l'adresse publique (HTTPS inclus).
6. Ouvrez cette adresse : c'est la **page des salariés**. Ajoutez `/manager` à la fin pour l'espace gérant.
7. Dans l'espace gérant → « Salariés », créez vos salariés avec leurs codes.

Autres hébergeurs possibles (Railway, Fly.io, un petit serveur…) : le principe est le même, il faut un **disque persistant** monté sur `/data`, du **HTTPS**, et les variables ci-dessus.
Avec Docker : `docker build -t gca-pointage .` puis
`docker run -p 8000:8000 -v pointage-data:/data -e MANAGER_PASSWORD=... -e SECRET_KEY=... gca-pointage`.

Gardez **un seul worker** (déjà réglé dans le `Dockerfile`) : la limitation des essais de code est gardée en mémoire.

## Sécurité et limites

- Un code de pointage n'est pas un mot de passe : un salarié peut pointer pour un collègue s'il connaît son code. Ne publiez pas les codes et affichez la page sur un écran fixe à l'entrée ou en salle de pause.
- Après 10 codes erronés en 5 minutes, l'adresse IP est temporairement bloquée.
- **Sauvegardez** régulièrement le fichier `pointage.db` (toute la base) et téléchargez l'export CSV chaque mois.
- Les exports neutralisent les cellules qui commencent par `=`, `+`, `-` ou `@`.
- Cette application remplace la page publiée sur Claude : les pointages saisis là-bas ne sont pas transférés automatiquement. Si besoin, exportez-les en CSV avant de changer.
