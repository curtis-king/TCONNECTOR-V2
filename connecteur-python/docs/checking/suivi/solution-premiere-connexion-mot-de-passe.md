# Solution — Changement de mot de passe obligatoire à la première connexion

> **Projet** : T-CONNECTOR (`connecteur-python/`)
> **Dossier** : `docs/checking/suivi/`
> **Statut** : IMPLÉMENTÉ — 2026-11-06
> **Branche** : `bayo/niu-individual`
> **Périmètre** : comptes admin par défaut, création/réinitialisation de comptes,
> mode `auth.provider=sage`.

---

## 1. Objectif

Éviter qu'un compte admin conserve durablement le mot de passe par défaut.

Règles retenues (option A) :

1. tout **nouveau compte** créé depuis l'interface admin doit changer son mot
   de passe à la première connexion ;
2. toute **réinitialisation** de mot de passe par un admin impose le changement
   à la prochaine connexion ;
3. les **comptes admin par défaut déjà existants** peuvent être marqués
   explicitement, sans changer leur mot de passe ;
4. en mode `auth.provider=sage`, le blocage local est désactivé : le mot de
   passe de référence est géré par Sage.

Aucune donnée Sage ni SQLite n'est modifiée automatiquement par cette
fonctionnalité.

---

## 2. Mécanisme existant réutilisé

La colonne `utilisateurs.must_change_pwd` existait déjà :

- chargée dans la session à la connexion (`app/web/routes/auth.py`) ;
- rafraîchie à chaque requête depuis la base (`app/web/auth/security.py`) ;
- garde globale : pages redirigées vers `/compte/mot-de-passe`, APIs en `403`
  avec `must_change_password: true` ;
- seules restent accessibles la page de changement, son API, `/logout` et
  `/api/csrf` ;
- `set_password()` remet le flag à `0`, incrémente `session_version` et révoque
  les autres sessions.

Le socle était donc présent ; il n'était simplement pas alimenté
automatiquement.

---

## 3. Changements implémentés

### 3.1 Script CLI admin

`scripts/create_admin.py` :

- `--must-change` devient le comportement **par défaut** ;
- ajout de `--no-must-change` pour un mot de passe déjà personnel ;
- ajout de `--require-password-change` : marque un compte existant
  (`must_change_pwd = 1`, `session_version + 1`) **sans lire ni modifier son
  mot de passe**, avec une entrée d'audit
  `admin.changement_mdp_requis`.

Exemples :

```powershell
# Création : changement obligatoire à la première connexion (défaut)
.\scripts\create_admin.ps1 -Email admin@client.cg

# Création avec un mot de passe déjà personnel
.\scripts\create_admin.ps1 -Email admin@client.cg -NoMustChange

# Marquer un compte admin existant sans changer son mot de passe
.\scripts\create_admin.ps1 -Email admin@client.cg -RequirePasswordChange
```

`scripts/create_admin.ps1` expose les mêmes options :
`-NoMustChange`, `-RequirePasswordChange` (et `-MustChange` conservé pour
compatibilité).

### 3.2 Création et réinitialisation depuis l'interface

- `app/web/auth/user_auth.py` : `create_user(..., must_change=True)` par
  défaut ; la valeur est écrite dans l'`INSERT`.
- `app/web/routes/directory.py` :
  - `POST /api/utilisateurs` force `must_change=True` ;
  - `PUT /api/utilisateurs/<id>` avec un mot de passe force aussi
    `must_change=True` ;
  - `POST /api/utilisateurs/<id>/password` le faisait déjà.
- `app/web/templates/directory/utilisateurs.html` : libellés et messages
  indiquent qu'un mot de passe provisoire devra être remplacé à la prochaine
  connexion.

### 3.3 Écran de première connexion

`app/web/routes/auth.py` transmet `must_change` au template.
`app/web/templates/auth/password.html` affiche alors :

```text
Première connexion — définissez votre mot de passe
Le mot de passe par défaut doit être remplacé avant d'accéder à l'application.
```

Le champ « mot de passe actuel » est conservé : une session volée ne peut pas
changer le mot de passe sans connaître l'ancien.

### 3.4 Mode Sage

`app/web/auth/security.py` ignore le blocage local lorsque
`auth.provider = "sage"`. Le flag peut rester en base, mais il ne redirige plus
vers une page dont l'API refuse la modification. Le changement de mot de passe
reste géré par Sage.

---

## 4. Marquer les comptes admin par défaut existants (option A)

Vérifier d'abord les comptes concernés :

```sql
SELECT id, email, role, est_actif, must_change_pwd
FROM utilisateurs
WHERE role = 'admin'
ORDER BY id;
```

Puis, pour chaque compte **par défaut** à marquer :

```powershell
.\scripts\create_admin.ps1 -Email admin@example.com -RequirePasswordChange
```

Répéter pour chaque adresse concernée. Cette commande :

- ne change ni le mot de passe, ni le rôle, ni l'état du compte ;
- incrémente `session_version` (les sessions ouvertes sont révoquées) ;
- impose le changement à la prochaine connexion.

> Ne pas appliquer cette commande aux comptes nominatifs dont le mot de passe
> est déjà personnel, sauf volonté explicite.

---

## 5. Tests

Ajout / mise à jour :

- `tests/test_create_admin_script.py` :
  - création par défaut ⇒ `must_change_pwd = 1` ;
  - `--no-must-change` ⇒ `0` ;
  - `--require-password-change` ⇒ flag à `1`, mot de passe inchangé,
    `session_version` incrémenté, audit présent ;
  - compte inconnu ou base absente ⇒ échec propre.
- `tests/test_security.py` :
  - compte créé via l'interface ⇒ redirection vers `/compte/mot-de-passe`,
    APIs en `403`, page « Première connexion », accès normal après changement ;
  - `provider=sage` ⇒ le flag ne bloque pas l'accès ;
  - réinitialisation admin ⇒ changement forcé (test existant conservé).

Exécution ciblée :

```bash
.venv/bin/python -m pytest -q \
  tests/test_create_admin_script.py \
  tests/test_security.py \
  tests/test_directory_security.py
```

Résultat : **tous les tests ciblés passent**.

---

## 6. Procédure de test manuel

1. Arrêter le connecteur.
2. Sauvegarder puis supprimer `data/tconnector.db` (test utilisateur).
3. Démarrer avec `TCONNECTOR_ALLOW_SEED_ADMIN=1` pour créer l'admin initial.
4. Se connecter avec `dashboard.login_email` / `dashboard.login_password`.
5. Vérifier la redirection vers `/compte/mot-de-passe` et le titre
   « Première connexion ».
6. Définir un nouveau mot de passe, puis vérifier l'accès normal.
7. Créer un autre compte depuis `/utilisateurs` et vérifier le même parcours.

---

## 7. Limites

- Le flag est local à SQLite ; il ne modifie pas Sage.
- En mode Sage, le changement local est volontairement désactivé.
- La commande `--require-password-change` est unitaire (un email à la fois) :
  elle évite une migration globale involontaire de tous les admins.
