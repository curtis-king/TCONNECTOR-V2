# Limites & suspicions — Vues Facturation (`/billing`)

> **Périmètre audité** : `/billing`, `/billing/invoice/<id>`, `/billing/invoice/new`, `/billing/invoice/<id>/edit` + APIs `/api/invoices*`, `/api/sync/bi`, PDF.
> **Méthode** : lecture statique du code réel. Aucun code modifié.
> **Date** : 2026-10-19
> **Fichiers de référence** : `app/web/routes/billing.py`, `app/web/templates/billing/{list,detail,form}.html`, `app/domain/invoices.py`, `app/domain/pdf.py`, `app/storage/db.py` (`recalc_invoice_totals`), `app/sync/bidirectional.py`, `app/web/static/js/app.js`.

## Légende

🔴 bloquant/faux · 🟠 risque réel · 🟡 limite ergo/perf · 🔵 suspicion à vérifier · ⚪ dette/cosmétique

## Synthèse

| Écran | Constats | 🔴 | 🟠 | 🟡/🔵/⚪ |
|---|---|---|---|---|
| Liste `/billing` | 18 | 0 | 6 | 12 |
| Détail `/billing/invoice/<id>` | 10 | 0 | 4 | 6 |
| Formulaire (new/edit) | 18 | 0 | 6 | 12 |
| API `/api/invoices*` | 8 | 0 | 1 | 7 |
| PDF | 8 | 0 | 1 | 7 |

---

## 1. Liste `/billing`

Fichiers : `app/web/routes/billing.py:24-77`, `app/web/templates/billing/list.html`.

### BL-01 🟠 « CA Total TTC » additionne les avoirs au lieu de les soustraire
`count_invoices()` calcule `SUM(montant_ttc) WHERE statut != 'brouillon'` (`invoices.py:297-298`) sans filtrer/soustraire `type_doc='avoir'`, et `billing_page` l'expose en `ca` (`billing.py:70`). Le CA affiché est **brut + avoirs** : surestimation systématique du chiffre d'affaires.

### BL-02 🟠 « Mois en cours » (HT/TVA/TTC) inclut les avoirs positivement
Requête `billing.py:49-59` : `SUM(...) FROM invoices WHERE statut != 'brouillon'` sans distinction `type_doc`. Même biais que BL-01 sur les totaux mensuels.

### BL-03 🟠 Les cartes de statistiques ignorent totalement les filtres
`billing_page` calcule les stats une fois au chargement serveur (`billing.py:27-59`). Les filtres JS (`list.html:50-60,117-118`) ne rechargent que le tableau via `/api/invoices/list`. Filtrer une période/source/type laisse donc les cartes sur les valeurs globales, sans mention.

### BL-04 🟡 `non_certifiees` calculé mais jamais affiché
`billing.py:37-38` calcule `stats["non_certifiees"]`, absent du `render_template` (`billing.py:67-77`) et du template. Code mort.

### BL-05 🟡 `impayes_nb` calculé mais jamais utilisé
`billing.py:47-48` renvoie `impayes_nb`, non passé au template ; la carte « Impayés (FCFA) » affiche en réalité `impayes_total` (montant), pas un nombre (`list.html:9`). Libellé ambigu.

### BL-06 🟡 Statuts proposés incohérents avec le formulaire
`statut_opts` (`billing.py:61-63`) propose `a_comptabilise` alors que le formulaire n'offre que `brouillon`, `valide`, `a_comptabiliser` (`form.html:88-94`). Deux orthographes voisines (`a_comptabiliser` / `a_comptabilise`) sans clarification métier.

### BL-07 🟡 « Sync Sage OK » = compteur mémoire de pushes, pas un état
`pushed=bi_stats.get("pushed",0)` (`billing.py:70`) vient de `_sync_stats` en RAM (`bidirectional.py:14`), cumul des pushes de la session. Ce n'est ni le nombre de factures actuellement dans Sage, ni persistant (reset au redémarrage). Libellé trompeur.

### BL-08 🟠 `syncBi()` exécute une sync complète synchrone dans la requête HTTP
`syncBi` (`list.html:113`) appelle `/api/sync/bi`, qui exécute `sync_bidirectional.full_sync()` **dans le thread de la requête** (`billing.py:101-108`) : certification POS + push contacts + push factures + pull Sage. Sur un gros volume, risque de timeout HTTP/waitress et de blocage de l'UI. Le bouton ne se désactive pas.

### BL-09 🟠 Échec de sync invisible
`/api/sync/bi` renvoie `{success:false,error}` en 500 (`billing.py:107-108`), mais `syncBi` lit quand même `d.pushed`/`d.pull_new` (`list.html:113`) → toast « Sync: undefined poussees, undefined tirees ». Les erreurs du `full_sync` (`errors`) ne sont jamais affichées.

### BL-10 🟡 Pas de gestion d'erreur de chargement conservant l'état
En cas d'erreur API, `render()` affiche un toast mais laisse l'ancien tableau (potentiellement obsolète) à l'écran (`list.html:103-108`).

### BL-11 🟡 `fmt()` produit « NaN » sur valeur non numérique
`fmt(x)` fait `Number(x||0).toLocaleString()` sans tester `isNaN` (`list.html:49`). Une donnée corrompue affiche « NaN » dans les colonnes montants.

### BL-12 🟡 Lien « Voir » sans `rel="noopener"` + nouvelle fenêtre
`list.html:84` `target="_blank"` sans `rel`. Tabnabbing mineur ; ouvre un onglet au lieu de naviguer.

### BL-13 🟡 Aucun export ni action de masse
Pas d'export CSV/PDF de la liste, pas de sélection multiple (pousser/certifier/supprimer en lot). Limite opérationnelle pour la facturation.

### BL-14 🔵 `certifiees` incohérent entre la page et l'API stats
La page écrase `stats["certifiees"]` avec `CERTIFIE + DEJA_CERTIFIE` (`billing.py:31-32`), alors que `/api/invoices/stats` renvoie `count_invoices()` qui ne compte que `CERTIFIE` (`invoices.py:289-290`). Deux chiffres différents selon la source.

### BL-15 🔵 Règle « poussable vers Sage » dupliquée SQL/JS
`pending_sage` (SQL, `billing.py:39-44`) et `sageCell`/`pushable` (JS, `list.html:70`) réimplémentent la même règle. Toute évolution doit être faite deux fois, avec risque de divergence.

### BL-16 🔵 11 requêtes SQL séparées par chargement de page
`billing_page` enchaîne ~11 `COUNT`/`SUM` (`billing.py:30-59`) + `count_invoices()` (9 requêtes) + `get_sync_stats`. Acceptable à petite échelle, coûteux sur SQLite chargé.

### BL-17 ⚪ `window.NATIVE_ALERTS = true`
`list.html:46` force `showToast` à utiliser `alert()` bloquant (`app.js:5`). Toute notification interrompt l'utilisateur.

### BL-18 ⚪ Import `functools` inutilisé
`billing.py:9`.

---

## 2. Détail `/billing/invoice/<id>`

Fichiers : `app/web/routes/billing.py:108-158`, `app/web/templates/billing/detail.html`.

### BL-19 🟠 « Certifier SFEC » proposé sans condition de statut
`detail.html:6` affiche toujours le bouton, y compris pour un **brouillon** ou une facture **déjà certifiée**. Or `certify_sqlite_invoice` ne vérifie pas `statut` (`endpoints.py:574-658`) : on peut certifier un brouillon (qui reste brouillon) ou relancer un appel sur une facture certifiée.

### BL-20 🟠 Suppression autorisée sur facture certifiée / poussée Sage
`billing.py:299-314` appelle `delete_invoice` sans aucun garde-fou (`invoices.py:175-186`). Supprimer une facture `synced_sage=1` ou `sfec_statut=CERTIFIE` crée une divergence locale/Sage/SFEC et une facture certifiée orpheline.

### BL-21 🟠 Édition autorisée après certification / push Sage
`invoice_edit_page` + `update_invoice` (`billing.py:161-167`, `invoices.py:120-173`) ne bloquent pas l'édition d'une facture déjà certifiée ou écrite dans Sage. Modifier montants/lignes/type après coup désynchronise SQLite, Sage et SFEC.

### BL-22 🟠 Suppression : redirection inconditionnelle même en échec
`detail.html:8` : `api('DELETE',...).then(function(){location.href='/billing'})` sans lire la réponse. Si l'API renvoie 403/400, l'utilisateur est quand même redirigé et croit à un succès.

### BL-23 🟡 Badge de statut toujours vert
`detail.html:15` : `<span class="badge badge-ok">{{ statut }}</span>` quelle que soit la valeur (brouillon, erreur…). Indicateur trompeur.

### BL-24 🟡 Lien PDF en nouvelle fenêtre sans état d'erreur
`detail.html:7` : si reportlab est absent, l'onglet affiche le JSON `{"error": ...}` (`billing.py:344-345`) au lieu d'un message dans l'application.

### BL-25 🟡 Totaux et lignes formatés de façon hétérogène
`billing.py:116-118` : prix unitaire `{:,.2f}`, HT/TVA/TTC `{:,.0f}`, quantité brute. Dans le même tableau, précisions différentes ; devise absente des lignes.

### BL-26 🟡 Pas de lien vers l'impression certifiée SFEC
Le détail affiche N° certif (`sfec_html`) mais pas de bouton vers `/certified/<sfec_id>/print`, ni QR/signature/date de certification. L'utilisateur doit aller sur `/certified`.

### BL-27 🔵 `sfec_num_certif` NULL provoque une 500
`billing.py:133` : `inv.get("sfec_num_certif", "")[:30]`. Si la colonne existe avec `None` (et non `''`), `.get` renvoie `None` → `TypeError: 'NoneType' is not sliceable`. À vérifier sur données réelles.

### BL-28 ⚪ Pas d'audit visuel des modifications
Les audits sont en base (`user_auth.audit`), mais le détail n'affiche pas l'historique des modifications/certifications, ni qui a fait quoi.

---

## 3. Formulaire `/billing/invoice/new` et `/edit`

Fichiers : `app/web/routes/billing.py:170-235`, `app/web/templates/billing/form.html`, `app/domain/invoices.py`.

### BL-29 🟠 XSS stockée via JSON injecté dans `<script>`
`billing.py:206-211` injecte `contacts_json`, `products_json`, `tax_rates_json`, `lignes_json`, `certified_sales_json` via `json.dumps(..., ensure_ascii=False)` puis `{{ ... | safe }}` dans un `<script>` (`form.html:282-286`). `json.dumps` n'échappe **pas** `</script>` : un nom de client/produit/désignation contenant `</script><script>…` exécute du JS arbitraire. Vecteur stocké (contacts/produits viennent de la base).

### BL-30 🟠 XSS stockée via `addLine()` et les attributs HTML
`form.html:309-310` concatène `data.designation`, `data.code_article`, `data.famille` directement dans des attributs `value="..."` sans échappement. Une désignation contenant `"` casse l'attribut et permet l'injection HTML (données d'une facture existante, potentiellement importées).

### BL-31 🟠 La devise sélectionnée est ignorée en édition
`form.html:39-43` code en dur `<option value="XAF" selected>`. La variable `devise` est pourtant passée (`billing.py:214`) mais jamais appliquée. Éditer une facture USD/EUR la repasse silencieusement en XAF au premier enregistrement.

### BL-32 🟠 Le mode de paiement sélectionné est ignoré en édition
`form.html:29-35` ne pose aucun `selected` sur `payment_method`. `billing.py:214` passe bien `payment_method`, mais le formulaire repart sur « Virement ». Toute facture cash/carte/mobile money est écrasée en `bank_transfer` à l'édition.

### BL-33 🟠 `is_recipient_taxable` n'est jamais pré-rempli
`form.html:192-196` : `Oui` est toujours la valeur par défaut, et `billing.py:213-235` ne passe pas la valeur existante. Éditer un client non assujetti le rend assujetti TVA sans que l'utilisateur le voie.

### BL-34 🟠 `saveAndCertify()` n'affiche aucun message en cas d'échec de sauvegarde
`form.html:374` : `if(r.id){...}` seulement ; un `{success:false,error}` n'a pas d'`id`, donc rien ne se passe (pas de toast, pas de redirection). L'utilisateur croit que le clic est ignoré.

### BL-35 🟠 « Enregistrer & Certifier » peut certifier un brouillon
`saveAndCertify` envoie le statut courant (par défaut `brouillon`), puis appelle `/api/sfec/certify` sans changer le statut (`form.html:374`). On obtient une facture certifiée SFEC restant en statut `brouillon` en base. Même problème que BL-19.

### BL-36 🟡 Aucune validation « au moins une ligne »
`gatherData` ne pousse que les lignes avec désignation (`form.html:370`) ; `create_invoice` n'exige pas de ligne (`invoices.py:110-112`). Une facture à 0 ligne / 0 TTC est créée, puis échouera à la validation SFEC (`total_amount > 0`).

### BL-37 🟡 Taux de TVA personnalisés rangés dans le seau « 18 % »
`form.html:343-345` ne distingue que 5 %, 0 %, « sinon 18 % ». `recalc_invoice_totals` fait de même (`db.py:581-583` : seuls 18/5/0). Un taux 10 % alimente `tva18` côté UI mais pas `total_tax_t_amount` en base → **incohérence des totaux SFEC** (`validation` échoue pour écart de totaux).

### BL-38 🟡 Le centime additionnel est une règle codée en dur (5 % de la TVA 18 %)
`form.html:347` et `db.py:594` : `cent = tva18 * 0.05`. Aucune configuration ni justification métier visible ; tout changement légal impose de modifier deux endroits.

### BL-39 🟡 Édition : `montant_restant` remis au TTC total
`recalc_invoice_totals` force `montant_restant = montant_ttc` (`db.py:591,598`). Toute édition de lignes efface un paiement partiel déjà enregistré.

### BL-40 🟡 Statut `a_comptabilise` non représenté dans le select
`form.html:88-94` ne propose pas `a_comptabilise`. Éditer une facture dans cet état n'a aucune option sélectionnée → le navigateur retombe sur `brouillon`, dégradant le statut au save.

### BL-41 🟡 Changement de type vente↔avoir autorisé, même certifié
`form.html:21-25` permet de changer `type_doc` en édition ; `update_invoice` valide l'avoir mais ne bloque pas la bascule sur une facture déjà certifiée/poussée (`invoices.py:126-134`). Désynchronisation réglementaire.

### BL-42 🟡 Liste des avoirs cibles limitée et non recherchable
`billing.py:195-204` : `LIMIT 200`, pas de recherche, pas de filtre date. Au-delà de 200 ventes certifiées, une ancienne facture ne peut plus être référencée dans un avoir via l'UI.

### BL-43 🟡 `numero` non contrôlé côté API
`create_invoice` accepte `data.get("numero")` tel quel (`invoices.py:62`) ; seule la contrainte `UNIQUE` protège. L'API permet donc des numéros hors séquence (le formulaire ne l'expose pas, mais `/api/invoices` est accessible).

### BL-44 🟡 `notes` accumule des retours à la ligne
`form.html:232-234` : le contenu du `<textarea>` est encadré de sauts de ligne de mise en forme. Chaque chargement/sauvegarde ajoute des blancs au début/fin des notes.

### BL-45 🟡 Champs non éditables/injectés
`gatherData` n'envoie ni `numero`, ni `contact_id`, ni `montant_ht_brut` (`form.html:362-370`). Certains champs du modèle sont inaccessibles depuis l'UI et jamais mis à jour via le formulaire.

### BL-46 🔵 Pas de gestion d'erreur sur `certified_sales_json` vide
`initRefInvoiceSelect` suppose `certifiedSales` itérable (`form.html:292`). Si le JSON est `null`/mal formé, le script casse silencieusement et le formulaire n'est plus fonctionnel.

### BL-47 ⚪ `window.NATIVE_ALERTS = true`
`form.html:281` : alertes bloquantes.

### BL-48 ⚪ Widget `is_recipient_taxable` sans lien avec le type de tiers
Aucune règle UI ne désactive/force ce champ selon `tiers_type` (étranger, gouvernement…), alors que SFEC impose des contraintes (cf. `validate_sfec_payload`).

---

## 4. APIs `/api/invoices*`

Fichiers : `app/web/routes/billing.py:238-359`, `app/domain/invoices.py`, `app/web/auth/security.py:145-170`.

### BL-49 🟠 Un PUT peut forger une certification locale
`update_invoice` accepte `sfec_statut`, `sfec_num_certif`, `sfec_signature`, `sfec_qr_code`, `sfec_id` parmi les champs modifiables (`invoices.py:138-147`). Un utilisateur avec `factures.creer` peut donc marquer une facture `CERTIFIE` sans passer par SFEC. Intégrité réglementaire en jeu.

### BL-50 🟡 Incohérence de nommage `type` vs `type_doc`
`GET /api/invoices` lit `request.args.get("type")` (`billing.py:242`), alors que `GET /api/invoices/list` lit `type_doc` (`billing.py:86`). Deux conventions pour la même donnée, source d'erreurs d'intégration.

### BL-51 🟡 `limit`/`offset` non bornés robustement
`billing.py:248-249` : `int(request.args.get("limit",200))` et `int(offset)` sans `try`. `?limit=abc` lève une 500. De plus `offset` négatif n'est pas borné.

### BL-52 🟡 Pas de paramètre de tri sur `/api/invoices`
Contrairement à `/api/invoices/list`, `GET /api/invoices` n'expose ni `sort_by` ni `sort_dir` (tri figé par défaut dans `invoices.py:213-215`).

### BL-53 🔵 Recherche `LIKE` non échappée
`invoices.py:228-231` : `%` et `_` fournis par l'utilisateur sont interprétés comme jokers (recherche `%` = tout). Pas d'injection SQL (requête paramétrée) mais comportement surprenant.

### BL-54 🔵 `push-sage` et `pdf` synchrones
`api_invoice_push_sage` (`billing.py:317-328`) et `api_invoice_pdf` (`billing.py:337-359`) s'exécutent dans le thread HTTP. Push Sage peut être lent → timeout UI.

### BL-55 ⚪ `send_file` importé deux fois
`billing.py:11` puis `billing.py:352`.

### BL-56 ⚪ `/api/invoices/stats` et page ne partagent pas la même définition
Voir BL-14 ; l'API stats ne renvoie pas les compteurs enrichis (`certif_en_cours`, `certif_erreur`, `pending_sage`, impayés, mois) utilisés par la page.

---

## 5. PDF (`/api/invoices/<id>/pdf`)

Fichier : `app/domain/pdf.py`, route `billing.py:337-359`.

### BL-57 🟠 Champs utilisateurs injectés dans des `Paragraph` ReportLab sans échappement
ReportLab interprète le mini-XML (`<b>`, `&amp;`…). Or `company.name`, `invoice.tiers_nom`, `invoice.notes`, `l.designation`, `company.invoice_footer` sont passés bruts (ex. `pdf.py:192,257,281,342,377`). Un `&` ou `<` dans un nom/note fait planter la génération (exception de parsing → 500), et permet un balisage non désiré.

### BL-58 🟡 La date imprimée est la date de création, pas la date de facture
`pdf.py:235` : `date_heure = invoice.get("created_at") or invoice.get("date_facture", "")`. `created_at` existe presque toujours → le PDF affiche l'horodatage de création SQLite au lieu de `date_facture`. Sur une facture antidatée/importée, la date légale est fausse.

### BL-59 🟡 Libellés TVA 18 % / 5 % codés en dur
`pdf.py:316-317` affiche « TVA 18% » et « TVA 5% » quel que soit le taux réel. Un taux exotique (cf. BL-37) est mal représenté.

### BL-60 🟡 Montant en lettres toujours en « francs CFA »
`pdf.py:112` (`"francs CFA"`) et `pdf.py:336`. Une facture en USD/EUR est libellée en francs CFA.

### BL-61 🟡 Titre « FACTURE DE VENTE » pour tout document non-avoir
`pdf.py:226` : seul `avoir` est distingué. Tout autre `type_doc` (achat, import…) est imprimé « FACTURE DE VENTE ».

### BL-62 🔵 QR non vérifié : régénération possible d'un QR non-SFEC
`pdf.py:361-363` : si le QR SFEC n'est pas décodable en image, le code **régénère** un QR à partir du texte. Le PDF peut donc afficher un QR qui n'est pas celui émis par SFEC, sans avertissement.

### BL-63 🔵 Clés de configuration hétérogènes
`pdf.py:202` lit `company.rc_number`, alors que d'autres écrans utilisent `tax_number`/`rccm` (ex. `dashboard.py:214-215`). Suspicion de RCCM absent du PDF si la config utilise une autre clé.

### BL-64 ⚪ Pas de pagination/contrôle de taille des notes
Notes longues ou nombreuses lignes : `SimpleDocTemplate` gère la pagination, mais le tableau d'items n'a pas de contrôle de colonnes très larges / texte long.

---

## 6. Suspicions prioritaires à trancher

1. **BL-49** : faut-il interdire toute mise à jour des champs `sfec_*` via l'API générique (séparation des privilèges) ?
2. **BL-20/BL-21/BL-41** : quelle est la règle métier d'immutabilité d'une facture certifiée/poussée Sage ?
3. **BL-37/BL-38** : les taux personnalisés et le centime additionnel sont-ils réellement supportés, ou le système est-il volontairement limité à 0/5/18 % ?
4. **BL-57** : valider la génération PDF avec des noms clients contenant `&`, `<`, `/` (données Sage réelles).
5. **BL-29/BL-30** : confirmer la présence éventuelle de `</script>` ou `"` dans les noms clients/produits importés de Sage.
6. **BL-31/BL-32/BL-33** : vérifier sur une facture réelle en USD et en espèces que l'édition ne réécrit pas devise/paiement/fiscalité.
7. **BL-39** : vérifier si `montant_restant` est utilisé pour le suivi des paiements ailleurs (sinon l'écrasement est sans effet, sinon c'est une perte de données).
