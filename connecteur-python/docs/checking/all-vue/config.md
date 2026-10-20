# Limites & suspicions — Configuration (`/config`)

> **Périmètre audité** : `/config`, `/api/config/*`, `/api/db/*`, `/api/tables`, `/api/sfec/*`, `/api/tax-rates*`.
> **Méthode** : lecture statique du code réel. Aucun code modifié.
> **Date** : 2026-10-20
> **Fichiers de référence** : `app/web/routes/config.py`, `app/web/templates/config/index.html`, `app/config/manager.py`, `app/storage/db.py` (`generate_invoice_number`), `app/web/auth/security.py`, `app/web/auth/user_auth.py`.

## Légende

🔴 bloquant/faux · 🟠 risque réel · 🟡 limite ergo/perf · 🔵 suspicion à vérifier · ⚪ dette/cosmétique

## Synthèse

| Zone | Constats | 🔴 | 🟠 | 🟡/🔵/⚪ |
|---|---|---|---|---|
| Page & secrets | 8 | 3 | 5 | 0 |
| Formulaires & validation | 16 | 0 | 5 | 11 |
| Opérations SFEC/Sage | 8 | 0 | 0 | 8 |
| Persistance & système | 7 | 0 | 3 | 4 |

---

## 1. Page `/config` et secrets

Fichiers : `app/web/routes/config.py:31-150`, `app/web/templates/config/index.html`.

### CF-01 🔴 Mots de passe et secret de session affichés en clair dans la page
`login_password`, `session_secret` et le mot de passe SQL sont rendus dans des `value="..."` (`config.py:64,126-127,135-136` ; `index.html:19,154-155`). Toute personne ayant `config.gerer` (ou un XSS sur cette page, ou le cache navigateur) obtient le secret qui signe les cookies : **usurpation de n'importe quel compte**, y compris admin (voir AU-… sur `_current_identity`).

### CF-02 🔴 `/api/config` ne masque que `db.password` et `sfec.api_key`
`api_get_config` copie la config et masque deux clés (`config.py:282-291`). `dashboard.login_password` et `dashboard.session_secret` sont renvoyés en clair, ainsi que `company.logo_base64`. Tout `config.gerer` peut les lire via un simple GET.

### CF-03 🔴 L'authentification peut être désactivée depuis l'UI
Le sélecteur `auth_enabled` (`index.html:152`) est sauvegardé par `api_config_system` (`config.py:464-465`). `_auth_enabled()` lit directement cette valeur (`security.py:45-46`) : passer « Non » supprime **toute authentification** au prochain chargement, sans confirmation, sans double validation et sans audit.

### CF-04 🟠 L'export télécharge tous les secrets en clair
`/api/config/export` sérialise la config complète (`config.py:493-513`) : mot de passe SQL, clé API SFEC, `login_password`, `session_secret`, logo. Le fichier `config.json` est ensuite stocké dans le dossier Téléchargements de l'utilisateur.

### CF-05 🟠 L'import remplace la configuration et accepte les sections sensibles
`/api/config/import` exige `db`, `sfec`, `company` mais accepte aussi `dashboard` (`config.py:515-541`). Un fichier importé peut donc désactiver `auth_enabled`, imposer un `session_secret` connu, changer `host` ou `port`. Aucune réauthentification, aucun diff, aucune confirmation.

### CF-06 🟠 L'import peut effacer des sections absentes
`save_config(data)` remplace le document en mémoire et le fichier (`config.py:538`, `manager.py:163-169`). Les sections non listées (`pos`, `auth`, `cache`, `smtp`, etc.) disparaissent jusqu'au prochain rechargement, et `pos.stock_control` revient à `true` par défaut : comportement caisse modifié sans avertissement.

### CF-07 🟠 Aucune journalisation des changements de configuration
Contrairement aux comptes (`user_auth.audit`), les routes `/api/config/*` n'écrivent pas dans `audit_log`. Impossible de savoir qui a changé la clé SFEC, le mot de passe SQL ou désactivé l'auth.

### CF-08 🟠 `/api/db/test`, `/api/tables`, `/api/tax-rates*` sans permission dédiée
`_ACCESS_RULES` ne contient aucune règle pour `("api","db")`, `("api","tables")`, `("api","tax-rates")` (`security.py:114-146`). `_required_permission` retourne `None` par défaut (`security.py:160-174`) : **tout utilisateur connecté**, même caissière, peut tester la base Sage, lister ses tables et lire les taux de TVA.

---

## 2. Formulaires, valeurs et validation

Fichiers : `app/web/routes/config.py:294-480`, `app/storage/db.py:495-520`.

### CF-09 🟠 Le préfixe et le numéro de facture de la config sont sans effet
`api_config_company` écrit `company.invoice_prefix` / `company.invoice_next_number` (`config.py:344-356`), mais `generate_invoice_number()` lit les clés `invoice_prefix` / `invoice_next_number` de la table `settings` SQLite (`db.py:501-516`). Modifier ces champs dans `/config` ne change donc **jamais** la numérotation réelle (défaut config `INV-` vs base `FA`).

### CF-10 🟠 Le toggle « QR Code activé » est mort
`qrcode_enabled` est lu/écrit par la page (`config.py:104-105,353`) mais aucun autre module ne le consulte. Le PDF et le ticket génèrent leur QR SFEC indépendamment : désactiver l'option n'a aucun effet.

### CF-11 🟠 Durée de session non bornée
`session_max_age_hours` accepte 0 ou une valeur négative (`config.py:466-470`). Selon Flask, cela peut invalider immédiatement les sessions ou les rendre permanentes, sans message.

### CF-12 🟠 Le secret de session peut être vidé ou raccourci via l'API
`session_secret` est copié tel quel (`config.py:456-458`). Un secret vide/faible permet de forger les cookies ; le changer invalide toutes les sessions actives sans prévenir.

### CF-13 🟠 Aucune validation des plages numériques
Ports, timeouts, pool, file d'attente, limites de débit et seuils acceptent négatifs, zéros et valeurs arbitraires (`config.py:305-415`). Exemples : `pool_max=0`, `job_timeout_ms=-1`, `daily_max=0` bloquent la production.

### CF-14 🟠 URLs SFEC non validées (SSRF)
`base_url` et `sandbox_url` acceptent n'importe quelle chaîne (`config.py:318-324`). Le serveur peut être pointé vers un hôte interne ou un serveur malveillant qui recevra la clé API et les factures.

### CF-15 🟡 `certify_status` non validé
La valeur est libre (`config.py:318-324`), alors que seuls `SAISI`, `CONFIRME`, `A COMPTABILISER`, `COMPTABILISE`, `LETTRÉ` ont un sens. Une faute de frappe désactive silencieusement la sélection des factures.

### CF-16 🟡 `log_level` non validé
`cfg["log_level"] = data["log_level"]` (`config.py:462-463`) accepte n'importe quelle chaîne ; le module de logging peut ignorer le niveau ou échouer.

### CF-17 🟡 `host`/`port` modifiables mais appliqués au redémarrage
L'UI ne signale pas que le changement est différé (`config.py:456-460`). L'utilisateur croit le serveur déplacé alors qu'il écoute toujours sur l'ancien port.

### CF-18 🟡 Champs de login legacy sans effet sur les comptes réels
`login_email`/`login_password` sont modifiables (`config.py:456-458`) mais l'authentification locale utilise la table `utilisateurs` (`user_auth.py`). Le formulaire entretient la confusion avec un ancien mécanisme, et conserve un mot de passe en clair.

### CF-19 🟡 Le logo n'est pas uploadable
`logo_file_name` est affiché mais absent de `str_fields` (`config.py:340-347`) ; `logo_base64` n'est pas exposé. Le logo ne peut être défini que par import de config ou édition manuelle du fichier, et un chemin local invalide fait disparaître le logo des PDF.

### CF-20 🟡 Clés entreprise hétérogènes et champs cachés
`website`, `slogan`, `default_payment_terms`, `legal_form`, `capital` sont acceptés par l'API mais absents de l'UI (ou vice-versa). Le PDF lit `rc_number`/`tax_number` sous d'autres noms selon les écrans (`pdf.py`), d'où des mentions légales incomplètes.

### CF-21 🟡 Devise libre via l'API
`currency` est dans `str_fields` (`config.py:342-346`) alors que l'UI ne propose que XAF/USD. Changer la devise ne migre aucune facture ; les PDF mélangent alors symbole et montants historiques.

### CF-22 🟡 Tolérance et limites sans cohérence
`tolerance_amount` peut être négatif, `alert_threshold_percent` > 100, `per_minute_max` > `daily_max` (`config.py:390-415`). Aucun contrôle croisé.

### CF-23 🟡 `pool_min`/`pool_max`/timeouts sans cohérence
`pool_min` peut dépasser `pool_max`, les timeouts peuvent être supérieurs au timeout de la requête HTTP (`config.py:305-313`). Le pool peut ne jamais démarrer ou bloquer les workers.

### CF-24 ⚪ Formulaires sans état « modifié »
Chaque onglet sauvegarde silencieusement ; changer d'onglet ou recharger perd les saisies non enregistrées. Pas d'indicateur « non sauvegardé », pas de bouton « Annuler ».

---

## 3. Opérations SFEC / Sage

Fichier : `app/web/routes/config.py:152-277`.

### CF-25 🟡 Tests DB/SFEC synchrones dans la requête HTTP
`api_db_test` (`ping_database`) et `api_sfec_test` (`check_health`) s'exécutent dans le thread de la requête (`config.py:152-162`). Un serveur lent bloque le worker et le bouton reste sans retour.

### CF-26 🟡 Sync SFEC en thread sans statut ni verrou
`/api/sfec/sync` démarre un thread et répond immédiatement (`config.py:247-254`). Aucun état d'avancement, aucune protection contre les clics multiples, aucune erreur remontée.

### CF-27 🟡 `/api/sfec/certified-from-api` synchrone et tronqué
L'appel externe `list_invoices(page=1, page_size=100)` est fait dans la requête (`config.py:257-266`), sans pagination ni timeout applicatif. Au-delà de 100 factures, le résultat est incomplet sans indication.

### CF-28 🟡 `/api/sfec/debug` et `/api/sfec/validate` dépendent d'un cache périmé
Les deux routes cherchent la facture dans `get_cache()["sales_invoices"]` (`config.py:210-243`). Si la sync n'a pas tourné ou si le cache est vide, elles renvoient 404, sans proposer de recharger.

### CF-29 🟡 `/api/sfec/to-monitor` écrit dans Sage sans contrôle
`mark_to_monitor(invoice_id)` (`config.py:194-206`) accepte n'importe quel identifiant, sans vérifier qu'il existe, qu'il est une facture de vente ou qu'il n'est pas déjà certifié.

### CF-30 🟡 `/api/sfec/certify` mélange deux espaces d'identifiants
Le préfixe `INV-` route vers une facture SQLite, tout le reste vers `certify_single` (Sage) (`config.py:164-184`). Aucune validation du statut certifiable ; les erreurs sont renvoyées brutes en 500.

### CF-31 🟡 `applyConfig` bloque et ne détaille pas les échecs
`/api/config/reload` appelle `apply_config()` dans la requête (`config.py:483-491`) puis affiche `d.message`. Un rechargement de connexion Sage/SFEC peut prendre du temps, et les échecs partiels ne sont pas listés.

### CF-32 🟡 Messages d'exception bruts
Presque toutes les routes renvoient `str(e)` (`config.py:182,204,264,540`), exposant chemins, SQL et détails d'infrastructure.

---

## 4. Persistance & système

Fichier : `app/config/manager.py`.

### CF-33 🟠 Écriture non atomique de `config.json`
`save_config` ouvre le fichier en écriture directe (`manager.py:163-169`), sans fichier temporaire ni `os.replace`. Une coupure pendant l'écriture laisse un JSON tronqué ; au prochain démarrage, `load_config` retombe silencieusement sur les valeurs par défaut (`manager.py:135-148`).

### CF-34 🟠 La clé API d'environnement est persistée dans `config.json`
`load_config` injecte `SFEC_API_KEY` dans `_config` (`manager.py:150-159`) ; n'importe quel `save_config` ultérieur réécrit tout `_config`, donc **la clé d'environnement est copiée en clair dans le fichier**. Cela contredit le commentaire et l'export la divulgue aussi (CF-04).

### CF-35 🟠 Config mutable partagée entre requêtes
`get_config()` renvoie l'objet global `_config` (`manager.py:171-174`) ; les routes font `cfg = get_config()` puis mutent `cfg` hors verrou avant `save_config`. Deux sauvegardes concurrentes peuvent se perdre ou produire un mélange incohérent.

### CF-36 🟡 Aucune sauvegarde/version/rollback
Pas de copie de `config.json` avant import, pas d'historique, pas de bouton « restaurer ». Un import erroné est difficile à annuler.

### CF-37 🔵 Valeurs par défaut dangereuses
`session_secret = "CHANGE-ME-IN-PRODUCTION"`, `login_password = "CHANGE-ME"`, `host = "0.0.0.0"` (`manager.py:114-116,108`). Une installation non configurée expose un secret connu et écoute sur toutes les interfaces.

### CF-38 ⚪ Aucun test de la configuration
Pas de test couvrant le masquage des secrets, les bornes numériques, l'import ou l'effet réel de `invoice_prefix`.

### CF-39 🟡 Le message « redémarrer pour appliquer https_only » est faux
L'UI de `/utilisateurs` indique qu'un redémarrage est nécessaire (`utilisateurs.html:22`), alors que `_apply_session_security()` réécrit `SESSION_COOKIE_SECURE` à chaque requête (`security.py:184-185,262-267`). Le changement s'applique donc immédiatement — et peut déconnecter l'utilisateur s'il est encore en HTTP, sans avertissement.
