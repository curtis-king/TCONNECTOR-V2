# Limites & suspicions — Vues du Dashboard (T-CONNECTOR)

> **Périmètre audité** : dashboard uniquement (routes `/`, `/invoices`, `/pending`, `/certified`, `/certified/<id>/print`, `/sales`, `/health`, `/ready` + APIs appelées par ces vues).
> **Méthode** : lecture statique du code réel (`app/web/routes/*`, `app/web/templates/*`, `app/sync/*`, `app/integration/sfec/*`, `app/integration/sage/*`). Aucun code modifié, aucune hypothèse « produit » — uniquement des constats et suspicions à vérifier.
> **Date** : 2026-10-19
> **Fichiers de référence** : `app/web/routes/dashboard.py`, `app/web/routes/sync_api.py`, `app/web/routes/config.py`, `app/web/templates/dashboard/*.html`, `app/web/templates/directory/sales.html`, `app/web/static/js/app.js`, `app/web/common.py`, `app/sync/engine.py`, `app/sync/connectivity.py`, `app/integration/sfec/client.py`, `app/integration/sfec/endpoints.py`.

## Légende sévérité

- 🔴 **Bloquant / fonctionnel cassé** : comportement faux ou fonctionnalité qui ne fait pas ce qu'elle annonce.
- 🟠 **Risque réel** : peut induire en erreur l'exploitant, XSS, faux positifs, perte de données.
- 🟡 **Limite ergo/perf** : gêne l'usage ou dégrade les temps de réponse.
- 🔵 **Suspicion / à vérifier** : dépend du comportement réel de Sage/SFEC, non tranchable en statique.
- ⚪ **Dette / cosmétique** : incohérence, code mort, détail.

## Synthèse

| Vue | Constats | 🔴 | 🟠 | 🟡/🔵/⚪ |
|---|---|---|---|---|
| `/` (index) | 14 | 1 | 4 | 9 |
| `/invoices` | 11 | 2 | 1 | 8 |
| `/pending` | 15 | 3 | 3 | 9 |
| `/certified` | 12 | 0 | 2 | 10 |
| `/certified/<id>/print` | 9 | 0 | 2 | 7 |
| `/sales` | 9 | 1 | 2 | 6 |
| `/health` & `/ready` | 4 | 0 | 1 | 3 |

---

## 1. Vue `/` — Tableau de bord

Fichiers : `app/web/routes/dashboard.py:27-58`, `app/web/templates/dashboard/index.html`, `app/sync/engine.py:853-877`, `app/sync/connectivity.py`, `app/integration/sfec/endpoints.py:517-520`.

### DB-01 🔴 « Factures Vente » inclut les avoirs
`dashboard.py:50` passe `m.get("sales_invoices", 0)`, or `get_metrics()` (`engine.py:864`) renvoie `len(_cache["sales_invoices"])` et `fetch_sales_invoices` sélectionne `DO_Type IN (facture_type, avoir_type)` (`database.py:638-651`). La carte « Factures Vente » compte donc ventes **+ avoirs**. Aucun compteur d'avoirs séparé à l'écran.

### DB-02 🔴 « A comptabiliser » (= `val_count`) ne mesure pas `DO_Statut=2`
`dashboard.py:36` calcule `sum(... if inv.get("valide",0)==1)`. `valide` = `DO_Valide` (drapeau de validation Sage), **pas** le statut comptable. Or la page `/pending` affiche explicitement « …a comptabiliser (DO_Statut=2) » (`pending.html:3`), et l'auto-certification filtre bien sur `statut_code == 2` (`engine.py:542`). Trois définitions différentes pour un même libellé : incohérence directe.

### DB-03 🟠 `check_health()` bloquant à chaque chargement
`dashboard.py:31` appelle `check_health()` → `SfecClient.ping()` → `list_invoices()` (requête HTTP réelle). En l'absence de cache, un SFEC lent fait attendre la page jusqu'à **30 s** (`client.py:25`, `client.py:60`). Aucun timeout court dédié à un simple indicateur de santé.

### DB-04 🟠 `get_status()` bloquant à chaque chargement
`dashboard.py:32` appelle `get_status()` qui déclenche `_do_check()` si le cache TTL (10 s) est expiré (`connectivity.py:98-104`). Hors ligne, `_check_dns()` teste 3 hôtes à 2 s **avant** d'abandonner (`connectivity.py:17,25-33`) → page potentiellement figée plusieurs secondes.

### DB-05 🟠 « Polling auto : Actif » codé en dur
`index.html:19` affiche toujours `<span class="badge badge-ok">Actif</span>`. Rien ne lit `polling_enabled` (`config.json:17`) ni l'état réel du thread `_polling_thread` (`engine.py:821-836`). Si le polling est désactivé ou si le thread est mort, l'interface ment.

### DB-06 🟠 Plusieurs opérations synchrones lourdes par simple GET
`/` exécute : `get_metrics()` (dont `fetch_certified_invoices()` SQL à chaque appel, `engine.py:855-856`), `check_health()` (HTTP), `get_status()` (réseau), en plus des lectures de cache. Aucune agrégation/parallélisation ni mise en cache du rendu. Dashboard = point chaud.

### DB-07 🟡 `certified_count` fait une requête SQL par affichage
`get_metrics()` appelle `fetch_certified_invoices()` à **chaque** requête `/` et `/api/metrics` (`engine.py:854-856`) au lieu d'utiliser le compteur de cache (`_cache["certified_count"]`), pourtant maintenu par `sync_sfec_invoices` (`engine.py:263`).

### DB-08 🟡 Sync manuelle sans retours ni garantie
`syncNow()` (`app.js:92`) POST `/api/sync`, qui lance `sync_all` dans un thread et répond `{ok:true}` immédiatement (`sync_api.py:40-42`), puis **recharge la page après 2 s fixes**. Une sync complète (Sage + SFEC + auto-certif) peut dépasser 2 s : l'utilisateur voit des chiffres périmés et croit que c'est à jour. Aucun état de progression, aucun signalement d'échec.

### DB-09 🟡 Double-clic = syncs concurrentes non bornées
Aucun verrou côté API (`sync_api.py:38-42`) ; cliquer plusieurs fois « Sync » empile des threads `sync_all`. Le `_lock` de `engine.py:37` protège chaque écriture de cache mais pas la réentrance globale (Sage/SFEC peuvent être interrogés N fois en parallèle).

### DB-10 🟡 « Dernière sync » en UTC brut, sans conversion ni âge
`dashboard.py:54` renvoie `last_sync_at` (ISO UTC terminé par `Z`, `engine.py:214`) affiché tel quel. Pas de conversion heure locale (« WAT »), pas de durée écoulée. Peu exploitable pour juger la fraîcheur.

### DB-11 🔵 Indicateurs SFEC/DB non représentatifs de la config
La ligne « SFEC API » (`index.html:16`) reflète un `ping` réseau, pas l'état `sfec.enabled`/`api_key`. SFEC désactivée mais joignable ⇒ « Connectée » ; SFEC activée mais sans clé ⇒ comportement non explicité. Idem « Base de données » = dernier succès de sync (`_cache["db_connected"]`), pas un test live.

### DB-12 🔵 Compteur `syncs` non persistant
`sync_count` vit en mémoire (`engine.py:32`), remis à 0 à chaque redémarrage. Semble être un indicateur historique mais ne l'est pas.

### DB-13 ⚪ Message d'erreur brut non tronqué
`dashboard.py:55` passe `last_error` complet (potentiellement long, multi-ligne) ; affiché tel quel dans une cellule.

### DB-14 ⚪ Permission `dashboard.voir` non appliquée sur `/`
`_ACCESS_RULES` (`security.py:119-147`) ne mappe pas `/` : `_required_permission("/", ...)` retourne `None`. N'importe quel utilisateur authentifié atteint `/`, alors que la sidebar masque l'entrée via `SIDEBAR_PERM["/"]="dashboard.voir"` (`common.py:64-76`). Incohérence RBAC.

---

## 2. Vue `/invoices` — Factures Sage

Fichiers : `app/web/routes/dashboard.py:60-98`, `app/web/templates/dashboard/invoices.html`.

### DB-15 🔴 Onglet Achat : badge « Vente » sur toutes les lignes
`dashboard.py:84-85` : `type_badge_txt = "Avoir" if type_doc=="avoir" else "Vente"`. Pour les achats, `type_doc == "achat"` (`database.py:943`) ⇒ chaque ligne d'achat est étiquetée **« Vente »**.

### DB-16 🔴 Aucune pagination, cap dur à 200
`dashboard.py:74` : `invoices[:200]`. Le titre affiche « Factures ({{ inv_type }}) » sans total ni indication de troncature. Au-delà de 200 factures, des lignes disparaissent silencieusement.

### DB-17 🟠 Statut comptable `LETTRÉ` non stylé
`dashboard.py:76` : badge `badge-ok` seulement pour `COMPTABILISE` ; `LETTRÉ` (DO_Statut=4) tombe dans le cas `else` → aucun badge. Un statut explicitement mappé en base (`database.py:647`) n'a pas de style.

### DB-18 🟡 Colonnes SFEC affichées même pour les achats
Les colonnes « SFEC » et « N° Certif » restent rendues pour l'onglet Achat, où elles n'ont pas de sens (`sftxt` renvoie `-` seulement dans le fallback). Bruit visuel.

### DB-19 🟡 Format monétaire incohérent
`dashboard.py:89` utilise `{:,.2f}` → séparateur anglo-saxon (`1,234.56`), alors que le reste de l'app a un helper `fmt_money` (`app/core/utils.py`). Devise absente. Incohérent avec `/certified` (`fmtMoney` JS).

### DB-20 🟡 Aucun filtre/recherche/tri
Pas de filtre date, tiers, statut, ni recherche, ni tri serveur. `inv_type` est le seul paramètre (`request.args.get("type")`).

### DB-21 🟡 Aucun lien vers le détail/la facture
Pas de lien vers la fiche facture (billing/ZB) ni vers l'impression certifiée. Vue purement tabulaire en lecture.

### DB-22 🔵 `type` non validé
`dashboard.py:63` accepte n'importe quelle valeur ; une valeur inconnue retombe sur la logique « sale » (`else`) tout en affichant ce type brut dans le titre. Pas d'erreur, comportement implicite.

### DB-23 🔵 Colonnes « valide » binaires
`valide` 0/1 seulement ; si `DO_Valide` absent de la table, tout est « Non valide » (`database.py:597`). Affiché sans nuance.

### DB-24 🔵 Avoirs d'achat non gérés
Le filtre avoir ne travaille que sur le cache vente (`dashboard.py:68-71`) ; un avoir sur achat (`type_doc="achat"` côté achat) n'apparaît dans aucun onglet dédié.

### DB-25 ⚪ Colspan/robustesse numérique
`empty` colspan=9 cohérent avec l'en-tête. Mais `inv.get("montant_ttc", 0)` passé à `{:,.2f}` : une valeur non numérique lève `ValueError` → **500**. Non défensif.

---

## 3. Vue `/pending` — En attente de certification

Fichiers : `app/web/routes/dashboard.py:101-138`, `app/web/templates/dashboard/pending.html`, `app/sync/engine.py:509-654`.

### DB-26 🔴 Dépendance réseau synchrone + échec silencieux ⇒ faux positifs massifs
`dashboard.py:107-115` : appel `SfecClient().list_invoices(page=1, page_size=500)` avec `except Exception: pass`. Si SFEC est injoignable, `sfec_ids` reste vide ⇒ **toutes** les factures validées sont listées « à certifier », y compris celles déjà certifiées. La page devient trompeuse et invite à re-certifier (risque 409/doublons).

### DB-27 🔴 Une seule page de 500 factures SFEC
`dashboard.py:109` : `page=1, page_size=500`, jamais de boucle (contrairement à `sync_sfec_invoices`, `engine.py:249-260`). Au-delà de 500 factures certifiées sur SFEC, les suivantes sont considérées absentes ⇒ faux « en attente » en masse.

### DB-28 🔴 « Surveiller » n'a aucun effet visible dans cette vue
`toMonitor()` (`pending.html:21-31`) appelle `/api/sfec/to-monitor` → `mark_to_monitor` met seulement `SFEC_STATUT='A_SURVEILLER'` en base Sage (`database.py:1439-1456`), sans mettre à jour `_cache["sales_invoices"]`. Et le filtre de `/pending` n'exclut pas `A_SURVEILLER` (il n'exclut que `EN_COURS`/`ERREUR`, `dashboard.py:120`). Après `location.reload()`, la facture réapparaît identique. Le bouton semble « ne rien faire ».

### DB-29 🟠 Critère `valide==1` ≠ périmètre réel d'auto-certification (`statut_code==2`)
`dashboard.py:119` filtre `inv.get("valide", 0)==1`, alors que `auto_certify_invoices` ne traite que `statut_code == 2` (`engine.py:542`). La liste « à certifier » de cette page diffère donc de ce que le moteur va réellement certifier (faux positifs/faux négatifs).

### DB-30 🟠 La certification manuelle contourne les règles avoirs
Le bouton « Certifier » (`pending.html:127`) appelle `/api/sfec/certify` avec l'id Sage ⇒ `certify_single` (`engine.py:680-763`) → `certify_invoice`, qui ne valide qu'un `reference_invoice_id` non vide (`endpoints.py:347`). Les règles plus strictes de l'auto-certification (facture d'origine certifiée, anti double-avoir, avoir sans référence, `engine.py:562-587`) **ne s'appliquent pas** au chemin manuel. Un avoir peut être certifié à tort depuis cette page.

### DB-31 🟠 « A surveiller » / « Déjà certifié » non exclus du filtre
`dashboard.py:120` n'exclut que `EN_COURS` et `ERREUR`. Une facture locale `DEJA_CERTIFIE` absente de SFEC (cf. DB-27) réapparaît en attente, avec un bouton « Certifier ».

### DB-32 🟡 Appel SFEC à chaque rechargement de page (pas de cache/TTL)
Contrairement à `_build_sfec_lookup` (TTL 300 s, `engine.py:44-46`), `/pending` refait un appel complet à chaque affichage. Risque de rate-limit SFEC et de lenteur, en plus de l'absence de bouton « rafraîchir » explicite.

### DB-33 🟡 « Surveiller » et « Certifier » sans distinction de droits
Les deux POST passent par la même règle `("api","sfec")→sfec.certifier` (`security.py:130`). Pas de granularité.

### DB-34 🟡 Pas de pagination (>200 masquées), pas de montants totaux, pas de filtre
`dashboard.py:125` `uncertified[:200]` alors que `count` = total réel (`dashboard.py:136`). En-tête et liste désynchronisés au-delà de 200.

### DB-35 🔵 Correspondance des numéros fragile
`dashboard.py:111` compare `numero` local à `seller_invoice_number` **ou** `invoice_number` (fallback). Toute divergence de format (zéros, préfixe, casse) provoque un faux « en attente ». Non testable en statique.

### DB-36 🔵 Avertissement SFEC incomplet
`dashboard.py:131-133` n'affiche « SFEC désactivée » que si `not enabled` ; le cas `enabled=true` mais `api_key` vide (pourtant bloquant côté moteur, `engine.py:517`) n'est pas signalé.

### DB-37 ⚪ `sfec_warn` avec classes `badge badge-err` sur un `<p>`
Rendu approximatif (le `.badge` est pensé pour inline). Cosmétique.

### DB-38 ⚪ Avoir : simple badge, pas de référence d'origine
`dashboard.py:126` ajoute un badge « Avoir » mais n'affiche ni la facture d'origine ni son statut, informations pourtant déterminantes pour la certification d'un avoir.

### DB-39 ⚪ Absence de confirmation avant certification en masse
Chaque ligne a son bouton individuel ; c'est cohérent avec l'auto-certif, mais aucune indication du volume réellement concerné (cf. DB-29).

### DB-40 🟡 `count` et `val_count` désignent des choses différentes
`pending.html:2` affiche `count` (non certifiées via SFEC) et `pending.html:3` affiche `val_count` (valide==1, cf. DB-02) sous une même phrase « à comptabiliser ». Les deux nombres se contredisent souvent.

---

## 4. Vue `/certified` — Factures certifiées SFEC

Fichiers : `app/web/routes/dashboard.py:141-147`, `app/web/templates/dashboard/certified.html`, `app/web/routes/sync_api.py:71-92`.

### DB-41 🟠 Source unique = cache SFEC, aucun repli local
`/api/certified/list` lit `_cache["sfec_invoices"]` (`sync_api.py:74-76`). Si SFEC est désactivé/indisponible au démarrage, le cache est vide et la page affiche « Aucune facture » alors que Sage contient des certifiées (`fetch_certified_invoices`, `database.py:1498`). Aucun fallback.

### DB-42 🟠 XSS potentielle via `montant` non échappé
`certified.html:83` insère `fmtMoney(v.montant)` dans `innerHTML` sans `escC`. `fmtMoney` (`certified.html:39`) retourne la valeur brute quand elle n'est pas numérique. Donnée issue de l'API SFEC ⇒ injection HTML possible si le champ est détourné. Idem `id` dans le href (encodeURIComponent, OK) et `buyer` (escC, OK).

### DB-43 🟡 Sync SFEC asynchrone, délai fixe, erreurs avalées
`refreshCert()` (`certified.html:126-133`) POST `/api/sfec/sync` (thread, réponse immédiate `sync_api.py:247-254`) puis recharge après **2,5 s fixes**. La pagination complète de `sync_sfec_invoices` peut durer plus longtemps ⇒ liste encore périmée. De plus l'endpoint renvoie toujours `ok:true` en 200 même si la sync échoue, donc `.catch` ne se déclenche jamais.

### DB-44 🟡 Colspan incorrect sur les lignes vides/erreur
En-tête = 7 colonnes (`certified.html:23-29`), mais les lignes « Chargement… », « Aucune… » et « Erreur… » utilisent `colspan="6"` (`certified.html:31,85,147`). Rendu décalé.

### DB-45 🟡 « Type » binaire Vente/Avoir
`certified.html:82` : `Avoir` si `invoice_type==="creditNote"`, sinon `Vente`. Tout autre `invoice_type` SFEC est mal étiqueté.

### DB-46 🟡 `montant` souvent vide
`/api/certified/list` mappe `montant` sur `inv.get("total_ttc","0")` (`sync_api.py:85`), champ absent de la réponse SFEC de listing ⇒ affichage « 0 » fréquent.

### DB-47 🟡 Recherche limitée + pas de filtre type/montant
Recherche seulement sur `numero` + `buyer` (`certified.html:52`), pas de NIU ni de montant, pas de filtre Avoir/Vente, pas d'export.

### DB-48 🔵 `statut` non fiable
`status` est dérivé de `invoice_status`/`certification_status`/`status` (`sync_api.py:82`) ; peut être vide ⇒ badge « - » (`certified.html:42`). La sémantique dépend de la réponse SFEC.

### DB-49 🔵 Filtrage/tri full-client sur des pages entières
Tout le filtrage/tri/pagination se fait en JS sur la liste chargée en mémoire (`certified.html:49-118`). OK pour ~500 lignes, problématique au-delà (pas de pagination serveur).

### DB-50 🔵 Gestion d'erreur partielle
Si l'API renvoie 401 (session expirée) ou un objet d'erreur, `d.invoices` est `undefined` ⇒ liste vide silencieuse au lieu d'une redirection login (`certified.html:134-151`).

### DB-51 ⚪ Pas de rafraîchissement automatique
`last_sfec` figé tant que l'utilisateur ne clique pas ; pas de polling de la page.

### DB-52 ⚪ Bouton « Sync SFEC » jamais réactivé en cas de succès partiel
`bt.disabled=true` puis réactivé seulement dans `loadCerts` (`certified.html:144`). Si `loadCerts` ne tourne pas (timing), le bouton reste grisé.

---

## 5. Vue `/certified/<invoice_id>/print` — Impression certifiée

Fichiers : `app/web/routes/dashboard.py:150-232`, `app/web/templates/dashboard/print.html`, `app/integration/sfec/client.py:107-108`.

### DB-53 🟠 `qr_code` injecté sans échappement (XSS / casse HTML)
`dashboard.py:164-169` : `qr_display` construit avec la valeur brute `qr_code` (issue de l'API SFEC) dans `src="{}"` ou `<pre>{}</pre>`, puis rendu avec `{{ qr_display | safe }}` (`print.html:80`). Une valeur contenant `"` ou `<` casse l'attribut / injecte du HTML.

### DB-54 🟠 Aucun repli local : impression impossible hors ligne
`dashboard.py:154-161` fait un `get_invoice` SFEC live ; en cas d'échec → page 404 « Facture non trouvee », alors que la certification peut exister localement (Sage `SFEC_*` ou `data/sfec-certified.json`, `endpoints.py:25`). Pas de cache.

### DB-55 🟡 `items_json` traité comme une liste
`dashboard.py:171` : `items = inv.get("items") or inv.get("items_json") or []`. Si SFEC renvoie `items_json` sous forme de chaîne JSON, la boucle `for item in items` itère sur les caractères → `item.get(...)` lève `AttributeError` (500). À vérifier selon la réponse SFEC réelle.

### DB-56 🟡 TVA 5 % / 18 % codées en dur dans l'impression
`print.html:58-59` affiche « TVA 18% » / « TVA 5% » en dur, indépendamment des taux réellement appliqués (le payload SFEC peut utiliser d'autres taux, `endpoints.py:48-54`). Le pied de facture peut être faux.

### DB-57 🟡 `discount_display` toujours préfixé d'un « - »
`print.html:61` écrit `-{{ discount_display }}` alors que `discount_display` porte déjà le signe (`dashboard.py:205`). Une remise nulle affiche « -0 » ; un montant négatif devient « --… ».

### DB-58 🔵 Champs de la société : clés de config incertaines
`dashboard.py:199-201` lit `legal_form`, `tax_regime`, `capital` de la section `company` ; ces clés ne sont pas garanties présentes (`config.json` réel) ⇒ sections vides silencieusement.

### DB-59 🔵 `invoice_type` repris de `invoice_status` en secours
`dashboard.py:181` : `inv.get("invoice_type") or inv.get("invoice_status")`. Un statut textuel peut être mal interprété comme type de document (impacte `doc_label` et le bandeau avoir).

### DB-60 ⚪ Pas de pagination des lignes d'article
`item_rows` génère toutes les lignes ; acceptable mais non borné.

### DB-61 ⚪ Format de date sans contrôle
`cert_date` tronqué et `T`→espace (`dashboard.py:230`) ; pas de conversion de fuseau ni de gestion d'absence explicite.

---

## 6. Vue `/sales` — Historique des ventes (POS)

Fichiers : `app/web/routes/dashboard.py:235-276`, `app/web/templates/directory/sales.html`, `app/domain/pos.py:353-425`, `app/web/routes/pos.py:264-275`.

### DB-62 🔴 Statistiques « CA » non filtrées par les filtres de la page
`count_tickets()` (`pos.py:407-425`) calcule `total`, `ca_jour`, `ca_total` globalement, et le serveur les rend au chargement (`dashboard.py:272-273`). Les filtres client (`filterSales`, `sales.html:39-58`) ne mettent à jour que le tableau, **jamais** les cartes de stats. L'utilisateur filtre une période mais lit des totaux globaux.

### DB-63 🟠 XSS dans le rendu JS des ventes filtrées
`sales.html:53` construit `innerHTML` avec `t.numero`, `t.tiers_nom`, `t.mode_paiement`, `v` **sans échappement**. Ces valeurs viennent de SQLite (`pos.py:384-389`) elles-mêmes issues de saisies POS. Injection HTML possible.

### DB-64 🟠 Statut SFEC non recalculé côté client (cellule dupliquée)
Le serveur génère les cellules SFEC (`dashboard.py:244-259`) et le client a sa propre fonction `sfecCell` (`sales.html:30-38`) : deux logiques parallèles peuvent diverger (libellés, boutons). Toute évolution devra être faite deux fois.

### DB-65 🟡 Cap à 200 + pas de pagination/tri
`dashboard.py:241` `list_tickets(limit=200)` et filtre client `limit=200` (`sales.html:44`). `count` (total réel, `dashboard.py:274`) ne correspond pas au nombre affiché sans le dire.

### DB-66 🟡 Montants arrondis sans décimales
`dashboard.py:262` `{:,.0f}` et `sales.html:53` `.toLocaleString()` : perte des décimales. Incohérent avec `/invoices` (2 décimales) et `/certified` (0 décimale). Aucune devise affichée alors que le libellé des stats dit « FCFA ».

### DB-67 🟡 Filtres date peu démonstratifs
`filterSales` envoie `date_from`/`date_to` comparés en SQL `>=`/`<=` sur `date_ticket` (texte, `pos.py:360-364`). Si `date_ticket` inclut l'heure, `date_to` sans heure exclut toute la journée de fin. Suspicion de borne haute manquante (`<=` sur un datetime).

### DB-68 🔵 `t.get("date_ticket","")[:16]` non défensif
`dashboard.py:261` : si `date_ticket` vaut `None` (possible en base), l'indexation lève `TypeError` → 500.

### DB-69 🔵 Lien « Voir » sans `rel="noopener"`
`sales.html:53` et `dashboard.py:260` ouvrent `target='_blank'` sans `rel="noopener noreferrer"`. Tabnabbing mineur.

### DB-70 ⚪ Nom de vue trompeur
Route `/sales` classée dans le blueprint dashboard mais son template est `directory/sales.html` ; la sidebar la libelle « Historique ». Risque de confusion lors des évolutions (cf. DB-64).

---

## 7. Endpoints `/health` et `/ready`

Fichiers : `app/web/routes/dashboard.py:279-286`, `app/web/auth/security.py:184-247`.

### DB-71 🟠 Health/readiness factices
`/health` renvoie toujours `{"status":"ok"}` (`dashboard.py:281`) et `/ready` toujours `{"ready":true}` (`dashboard.py:286`), sans vérifier DB Sage, SQLite, SFEC ni connectivité. Inutilisables pour un vrai monitoring/supervision.

### DB-72 🟡 Protection dépendante de `auth_enabled`
Ces routes n'ont pas `@_login_required`. Elles ne sont protégées que par le hook global `_auth_before_request` (`security.py:240-243`) **si** `dashboard.auth_enabled` est vrai (`security.py:46,185`). Auth désactivée ⇒ `/health` et `/ready` publics.

### DB-73 🔵 Aucun code HTTP dégradé
Même en panne totale, ces endpoints répondent 200, empêchant tout orchestrateur de détecter l'indisponibilité.

### DB-74 ⚪ Doublon de sémantique
`ready` et `health` renvoient la même info utile (rien) ; aucun consommateur identifié dans le code.

---

## 8. Constats transverses (impactent plusieurs vues)

- **DB-75 🟠 Le cache est la seule source de vérité des pages serveur.** `/`, `/invoices`, `/pending`, `/sales` lisent `_cache` (`engine.py:21-35`). Si la sync initiale n'a jamais abouti, toutes ces vues affichent 0/vide sans le distinguer d'une base vide. Aucun indicateur de fraîcheur/erreur global.
- **DB-76 🟡 Aucune gestion d'expiration de session dans les appels `api()`.** `app.js:91` renvoie l'objet JSON sans vérifier `r.status` (401/403) : les vues JS (`/certified`, filtres `/sales`) affichent un état vide au lieu de rediriger vers le login.
- **DB-77 🟡 Sécurité : `_esc` Python côté serveur mais rendu `|safe`.** Convention fragile : tout oubli d'échappement devient une XSS (cf. DB-53). Les listes HTML construites par concaténation de chaînes (`rows`, `retry_html`, `item_rows`) accentuent le risque.
- **DB-78 🔵 Confiance aveugle dans les formats numériques/texte de Sage et SFEC.** Plusieurs `format`/index/`.get` supposent des valeurs non nulles et bien typées (DB-25, DB-55, DB-68) : données réelles non conformes ⇒ 500.
- **DB-79 ⚪ Code mort / imports inutilisés.** `dashboard.py:11` (`functools`) et `sync_api.py:12` (`functools`) importés sans usage ; `dashboard.py:21` importe `_login_required, logger` dont `logger` peu servi — détail.

---

## 9. Suspicions prioritaires à faire trancher (comportement Sage/SFEC)

1. **DB-27** : combien de factures certifiées sur SFEC (dépassement du cap 500) ? Conditionne l'ampleur des faux « en attente ».
2. **DB-26** : comportement produit attendu quand SFEC est injoignable — doit-on afficher « état inconnu » plutôt que « tout à certifier » ?
3. **DB-13/DB-02/DB-29** : quelle est la définition métier officielle de « à comptabiliser » (`DO_Valide` vs `DO_Statut=2`) ?
4. **DB-30** : la certification manuelle d'un avoir depuis `/pending` doit-elle appliquer les mêmes règles que l'auto-certification ?
5. **DB-55** : la réponse `get_invoice` SFEC renvoie-t-elle `items` (liste) ou `items_json` (chaîne) ?
6. **DB-54** : faut-il imprimer une facture certifiée hors ligne (repli Sage/local) ?
7. **DB-67** : format exact de `date_ticket` en base (avec/sans heure) pour corriger la borne haute.
