# Limites & suspicions — Authentification (`/login`, `/logout`, `/compte/mot-de-passe`)

> **Périmètre audité** : `/login`, `/logout`, `/compte/mot-de-passe`, `/api/compte/password`, hooks `before_request` / permissions / CSRF / cookies.
> **Méthode** : lecture statique du code réel. Aucun code modifié.
> **Date** : 2026-10-20
> **Fichiers de référence** : `app/web/routes/auth.py`, `app/web/auth/security.py`, `app/web/auth/user_auth.py`, `app/web/templates/auth/{login,password,deny}.html`, `app/config/manager.py`.

## Légende

🔴 bloquant/faux · 🟠 risque réel · 🟡 limite ergo/perf · 🔵 suspicion à vérifier · ⚪ dette/cosmétique

## Synthèse

| Zone | Constats | 🔴 | 🟠 | 🟡/🔵/⚪ |
|---|---|---|---|---|
| Connexion / déconnexion | 10 | 1 | 4 | 5 |
| Mot de passe | 8 | 0 | 0 | 8 |
| Permissions / autorisation | 10 | 3 | 0 | 7 |
| Session / en-têtes / audit | 7 | 0 | 0 | 7 |

---

## 1. Connexion / déconnexion

Fichiers : `app/web/routes/auth.py:28-88`, `app/web/auth/user_auth.py:385-439`.

### AU-01 🟠 Énumération des comptes par les messages et le timing
`authenticate` renvoie « Identifiants incorrects » pour un email inconnu (avec `time.sleep(0.4)`), « Compte desactive » pour un compte inactif et « Compte temporairement verrouille » pour un compte verrouillé (`user_auth.py:390-404,438`). Un attaquant distingue donc les emails existants, actifs et verrouillés. Le sleep de 0,4 s sur email inconnu ne compense pas exactement le temps PBKDF2 d'un mot de passe faux, ce qui ajoute un canal temporel.

### AU-02 🟠 Verrouillage de compte exploitable en déni de service
Le verrouillage est uniquement par compte (`user_auth.py:424-438`), sans limitation par IP, par plage réseau ou globale. Cinq tentatives suffisent à verrouiller le compte admin connu (`max_attempts=5` par défaut) pendant 15 minutes, répétable indéfiniment.

### AU-03 🟠 Recréation automatique d'un admin par défaut
`authenticate` appelle `migrate_admin_from_config()` à chaque tentative (`user_auth.py:387`). Si la table `utilisateurs` est vide, un admin est créé depuis `dashboard.login_email/login_password` (`user_auth.py:137-160`) ; les valeurs par défaut sont `admin@example.com` / `CHANGE-ME` (`manager.py:114-116`). Après une purge de la table, un attaquant connaissant ces défauts retrouve un accès admin.

### AU-04 🟡 Déconnexion par `GET` sans jeton CSRF
`/logout` est une route GET (`auth.py:82-88`) : un simple `<img src="/logout">` sur un site tiers déconnecte l'utilisateur. Impact limité (déni de service local), mais contraire aux bonnes pratiques.

### AU-05 🟡 Pas de MFA, CAPTCHA ni limitation par IP
Le login ne propose ni second facteur, ni CAPTCHA, ni throttling par adresse IP (`auth.py:28-80`). Seul le compteur par compte protège, avec le défaut AU-02.

### AU-06 🟠 Le rôle en session n'est jamais rafraîchi
`_login_checked=True` est posé au login (`auth.py:64`) ; `_auth_before_request` ne recharge le rôle que si ce drapeau est absent (`security.py:190-203`), et `_login_required` vérifie seulement `est_actif` (`security.py:98-112`). Un admin rétrogradé ou promu conserve donc ses anciens droits jusqu'à déconnexion/reconnexion.

### AU-07 🟡 Liaison `vendeur_id` fragile et non contrôlée
Après login, `vendeur_id` est cherché par égalité stricte sur l'email (`auth.py:70-79`) : casse différente, doublon ou email modifié → aucun vendeur associé. Aucune vérification que le vendeur correspond bien au compte.

### AU-08 ⚪ Page de connexion
Pied de page « © 2025 » obsolète (`login.html:footer`), accents non systématiques, absence d'`autocomplete="username"`/`"current-password"`, pas de bouton afficher/masquer le mot de passe.

### AU-09 🟡 L'authentification peut être désactivée globalement
`_auth_enabled()` lit `dashboard.auth_enabled` (`security.py:45-46`), modifiable depuis `/config` (CF-03). Passer l'option à `false` ouvre **toutes les routes** sans login ; aucun garde-fou, aucune confirmation, aucun audit.

### AU-10 🔴 Le secret de session est exposé par la configuration
`/config` et `/api/config` renvoient `dashboard.session_secret` en clair (CF-01/CF-02). Avec ce secret, un attaquant peut forger un cookie Flask contenant `user_id`, `user_role`, `_login_checked=true` et devenir admin sans mot de passe, car `_current_identity` fait confiance à la session (`security.py:81-90`).

---

## 2. Mot de passe

Fichiers : `app/web/routes/auth.py:90-111`, `app/web/templates/auth/password.html`, `app/web/auth/user_auth.py:301-318`.

### AU-11 🟡 Politique de mot de passe minimale
Seule règle : 8 caractères (`user_auth.py:303`). Pas de complexité, pas de vérification contre les mots de passe compromis, pas d'expiration, pas d'historique.

### AU-12 🟡 Changement de mot de passe sans limitation
`/api/compte/password` vérifie le mot de passe actuel (`auth.py:98-106`) mais sans throttling : un utilisateur déjà connecté peut brute-forcer le mot de passe courant, ou un XSS peut le faire à sa place.

### AU-13 🟡 Les autres sessions ne sont pas révoquées
`set_password` met à jour le hash (`user_auth.py:301-318`) sans invalider les sessions existantes ni changer le secret. Un cookie volé reste valide jusqu'à expiration.

### AU-14 🟡 Repli silencieux Sage → hash local
Si `provider = "sage"` et que `_sage_verify_password` échoue ou retourne `None` (Sage injoignable, table absente), `authenticate` retombe sur le hash local (`user_auth.py:409-413`). Le mode d'authentification effectif peut donc différer de la configuration, sans trace.

### AU-15 🟡 Changement de mot de passe inopérant en mode Sage
En mode `sage`, `api_compte_password` écrit un hash local (`auth.py:104-106`) mais le prochain login vérifie d'abord Sage (`user_auth.py:410-413`). Le nouveau mot de passe local peut être ignoré, ou créer deux secrets divergents.

### AU-16 ⚪ La page mot de passe promet plus que la réalité
« Le mot de passe est hache (PBKDF2) et n'est jamais stocke en clair » (`password.html`) est vrai pour `utilisateurs`, mais faux pour `dashboard.login_password` conservé en clair dans `config.json` (CF-01, DI-39).

### AU-17 🟡 Réinitialisation admin sans changement forcé
`resetPwd` permet à un admin de définir un mot de passe (`utilisateurs.html:107-115`), mais rien n'oblige l'utilisateur à le changer à la prochaine connexion, et aucune notification ne lui est envoyée.

### AU-18 🟡 Le jeton CSRF n'est pas renouvelé après changement de mot de passe
`/api/compte/password` ne régénère pas `session["_csrf"]` (`auth.py:96-110`). Un jeton observé avant le changement reste utilisable.

---

## 3. Permissions / autorisation

Fichiers : `app/web/auth/security.py:114-174`, `app/web/auth/user_auth.py:43-72`.

### AU-19 🔴 Modèle de permissions « fail-open »
`_required_permission` retourne `None, None` par défaut si aucun préfixe ne correspond (`security.py:174`). Toute nouvelle route (ou route oubliée) est donc accessible à **tout utilisateur connecté**. Exemples actuels : `/api/db/test`, `/api/tables`, `/api/tax-rates*` (CF-08), `PUT/DELETE /api/contacts` et `/api/vendeurs` (DI-06/DI-20).

### AU-20 🔴 `PUT`/`DELETE /api/invoices/<id>` exigent seulement `factures.creer`
`security.py:170` mappe ces méthodes vers `factures.creer`. Un profil autorisé à créer des factures mais pas à les supprimer peut donc les modifier ou les supprimer. La permission `factures.supprimer` n'est **jamais** utilisée dans le code (seulement déclarée dans `user_auth.py:48`).

### AU-21 🔴 `PUT`/`DELETE /api/contacts` et `/api/vendeurs` retombent sur la permission de lecture
`_MUTATE_PERMS` ne couvre que les `POST` (`security.py:149-155`) ; les écritures passent par `clients.voir` / `vendeurs.voir` (`security.py:137-140`). Un caissier peut modifier/supprimer contacts et vendeurs via l'API.

### AU-22 🟡 Contournement par slash final ou casse
La correspondance des `_MUTATE_PERMS` est une égalité stricte `(path, method)` (`security.py:162-164`). Une URL avec slash final (`/api/contacts/`) ou une variante de casse échappe à la règle et retombe sur la permission de lecture.

### AU-23 🟡 Permission `factures.supprimer` morte
Déclarée dans la matrice (`user_auth.py:48`) et affichée dans l'éditeur de droits, elle n'est consommée nulle part. Les administrateurs croient configurer un droit qui n'a aucun effet.

### AU-24 🟡 Incohérence page / API sur les annuaires
`/clients` exige `clients.gerer` mais `/api/contacts` GET se contente de `clients.voir` (`security.py:136-137`) ; idem `/vendeurs` (`security.py:138-139`). Un profil « lecture » ne peut pas ouvrir la page mais peut lire les données par API.

### AU-25 🟡 Les admins ignorent totalement la matrice
`_required_permission` retourne `None` si `role == "admin"` (`security.py:160-161`). C'est probablement voulu, mais l'éditeur de droits laisse croire qu'on peut restreindre un admin ; toute case décochée pour `admin` est sans effet.

### AU-26 🟡 Aucune trace des refus de permission
`_deny` renvoie 403 (`security.py:177-182`) sans écrire dans `audit_log`, contrairement aux échecs CSRF et de connexion. Impossible de détecter une tentative d'accès non autorisé.

### AU-27 🔵 Matrice relue en base à chaque requête
`has_perm` → `role_permissions` → `get_matrix` lit le réglage SQLite `auth_matrix` à chaque contrôle (`user_auth.py:181-190`). Coût faible mais évitable, et couplage fort au stockage.

### AU-28 🟡 Le découpage de chemin est limité à 3 segments
`parts = path.strip("/").lower().split("/")[:3]` (`security.py:158`). Les routes plus profondes sont comparées sur leurs 3 premiers segments : une future route `/api/invoices/123/action` hérite de la règle générique, parfois trop permissive.

---

## 4. Session, en-têtes, audit

Fichier : `app/web/auth/security.py:36-269`, `app/web/routes/auth.py:52-79`.

### AU-29 🟡 Secret de session de repli déterministe
Si `session_secret` est vide, `_get_secret_key` calcule un SHA-256 de `"t-connector-{COMPUTERNAME|HOSTNAME}"` (`security.py:36-43`). Le nom de machine est souvent devinable : les cookies deviennent forgeables.

### AU-30 🟡 CSP permissive pour les scripts inline
La politique autorise `'unsafe-inline'` pour `script-src` et `style-src` (`security.py:254-258`), ce qui neutralise la protection contre les XSS stockées documentées ailleurs (BL-33, DI-01, PO-10, CF-01). `img-src data:` est nécessaire aux QR, mais élargit la surface.

### AU-31 🟡 Pas de HSTS et cookies non sécurisés par défaut
`https_only` vaut `false` par défaut (`config/manager.py`, `user_auth.py:84-96`) et aucun en-tête `Strict-Transport-Security` n'est ajouté (`security.py:250-260`). En HTTP, le cookie de session circule en clair.

### AU-32 🟡 Exclusion de `/api/connectivity` du contrôle d'inactivité
`_auth_before_request` saute entièrement le calcul d'idle pour ce chemin (`security.py:213`). Un onglet qui ne fait que poller la connectivité ne rafraîchit pas `_last_activity` et ne déclenche pas l'expiration ; le comportement dépend des autres requêtes.

### AU-33 🟡 Audit non borné et IP partielle
`audit_log` grossit sans purge (`user_auth.py:442-462`) ; l'IP n'est fournie que par login/logout (`auth.py:48,85`), pas par les modifications de comptes ou de configuration. Le journal peut devenir volumineux et peu exploitable.

### AU-34 🔵 `_apply_session_security` modifie la config Flask à chaque requête
Appelée dans `init_security` puis à chaque `before_request` (`security.py:184-185,262-267`), elle réécrit `app.config` (durée de session, cookies) en contexte de requête. Fonctionne, mais crée une mutation globale par requête et un couplage difficile à tester.

### AU-35 ⚪ Aucun test de sécurité
Pas de test couvrant l'énumération de comptes, le verrouillage, la révocation de session, les permissions `PUT/DELETE`, le CSRF ou la forgery de cookie. Les régressions de sécurité ne sont détectées qu'en production.

---

## 5. Suspicions prioritaires à trancher

1. **AU-10/CF-01/CF-02** : vérifier qui a réellement accès à `/config` et si `session_secret` a déjà fuité (export, sauvegarde, support).
2. **AU-03** : confirmer que `migrate_admin_from_config` recrée bien un admin `admin@example.com / CHANGE-ME` sur une base utilisateurs vide.
3. **AU-19/AU-20/AU-21** : tester avec un compte caissière les `PUT/DELETE` sur contacts, vendeurs et factures.
4. **AU-06** : vérifier qu'un admin rétrogradé conserve ses droits jusqu'à reconnexion.
5. **AU-02** : reproduire le verrouillage DoS du compte admin depuis une IP externe.
6. **AU-14/AU-15** : tester le mode `provider=sage` avec Sage arrêté, puis un changement de mot de passe.
7. **AU-29** : vérifier si `session_secret` est vide sur une installation fraîche (repli déterministe).
