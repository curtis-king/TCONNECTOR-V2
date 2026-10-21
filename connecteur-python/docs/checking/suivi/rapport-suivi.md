# Suivi de remediation des 327 constats — TCONNECTOR V2

**Derniere mise a jour** : 2026-10-20 (Vagues 1-2 quasi terminees, V3 quasi terminee, V4 en cours).
**Source** : `docs/checking/all-vue/{dashboard,billing,pos,directory,config,auth}.md` (327 constats).
**Regle** : pas de commit/push/redemarrage sans demande explicite.

## 1. Etat global

| Vague | Perimetre | Constats | Etat |
|---|---|---:|---|
| V1 | Securite transverse (auth, RBAC, secrets, CSRF, XSS) | auth 35 + config 39 + transverse | **Terminee** |
| V2 | POS + Facturation | pos 66 + billing 64 | **En cours** |
| V3 | Annuaire / contacts | directory 49 | **Quasi termine** |
| V4 | Dashboard / stats / PDF | dashboard 79 | **En cours** |
| V5 | Integrations SFEC / Sage / sync | transverse | A faire |
| V6 | Durcissement final + snapshots | restes | A faire |

**Tests** : `53 passed` (`23` baseline V1, 96 routes + `5` POS V2 + `9` facturation V2 + `11` annuaire V3 + `5` dashboard V4), warnings `datetime.utcnow` non bloquants.
Commande : `cd connecteur-python && .venv/Scripts/python.exe -m pytest`.

## 2. Vague 1 — terminee

### Auth / RBAC
- RBAC **fail-closed** et method-aware dans `app/web/auth/security.py`.
- Refresh du role a chaque requete ; secret de session aleatoire si absent/faible.
- `/logout` **POST-only** + CSRF + audit + `session.clear()` ; formulaires sidebar/topbar adaptes.
- `authenticate()` : throttle **email+IP** via `audit_log`, erreurs generiques, reset sur succes.
- Compte desactive et matrice de roles couverts par tests.

### Config / secrets
- `CONFIG_FILE` et `DB_PATH` surchargeables pour les tests.
- `save_config()` atomique ; cle SFEC d'environnement non persistee.
- `_redact_config()` recursif ; `/api/config` et export masquent les secrets.
- `/config` n'affiche plus `db.password`, `sfec.api_key`, `login_password`, `session_secret`.
- Import fusionne, ignore `***`, retire `session_secret`, refuse `auth_enabled=false`.
- `api_config_db` / `api_config_sfec` conservent les secrets si champ vide.
- `/api/config/system` preserve `login_password` vide et interdit la desactivation de l'auth.

### XSS
- JSON injectes via `|tojson` : POS, annuaire, facturation.
- Lignes/options echappees : contacts, vendeurs, clients POS.
- Tickets/PDF dashboard : `logo_html`, QR, messages, options vendeurs echappes.
- Audit des `|safe` restants note pour V6.

### Tests
- `tests/conftest.py` : base/config temporaires isoles, fixture `role_client`.
- `tests/test_security.py` : RBAC, refresh role, lockout, secrets, logout, XSS.
- `tests/test_avoirs.py` : attente contact corrigee (`200/201`).

## 3. Vague 2 — POS / Facturation (en cours)

### POS — termine
- `app/domain/pos.py` :
  - validation stricte des lignes (`quantite > 0`, prix `>= 0`, TVA `0/5/18`, remises bornees, `math.isfinite`) ;
  - validation mode de paiement, montant recu (especes), remise globale ;
  - hydratation des tiers depuis le contact en base (`NIU`, `RCCM`, type, assujettissement) ;
  - refus **B2B/gouvernement sans NIU** ou NIU invalide (16-17 alphanumeriques) ;
  - creation ticket + facture locale **atomique** : rollback ticket/lignes/stock si la facture echoue ;
  - facture POS porte `contact_id`, `tiers_niu`, `recipient_rccm`, `is_recipient_taxable`, `tiers_type`.
- `app/web/routes/pos.py` + `templates/pos/index.html` :
  - options clients par **code simple** ; lookup `posClients` ;
  - envoi de `tiers_niu`, `tiers_rccm`, `tiers_type`, `is_recipient_taxable` ;
  - quantites/prix negatifs refuses cote UI ; prix manuel valide.
- Stock : decrement atomique (`stock_reel >= qte`), plus de deficit perdu, restauration si echec ; `mixte` retire (non implemente).
- Controle du stock reserve a `config.gerer` ; audit des changements de stock et des tickets.
- APIs produits : `prix_achat` masque, `limit` borne, listes clients bornees a 500.
- `tests/test_pos_security.py` : lignes invalides, mode/montant especes, B2B sans NIU, facture B2B avec champs fiscaux, immuabilite.

### Facturation — termine
- Immuabilite : `update_invoice()` et `delete_invoice()` refusent toute facture **certifiee SFEC** (`CERTIFIE`/`DEJA_CERTIFIE`, `sfec_num_certif`) ou **synchronisee Sage** (`synced_sage`, `sage_piece`).
- `update_invoice_sfec()` ecrit directement les champs SFEC (flux de certification) et contourne volontairement l'immuabilite.
- Les champs `sfec_*` ne sont plus modifiables via l'API generique (fin de la forge de certification, BL-49).
- CA total et totaux du mois **soustrayent les avoirs** (`type_doc='avoir'`).
- Formulaire :
  - `payment_method`, `devise`, `is_recipient_taxable` et statuts `a_comptabilise`/`comptabilise` resélectionnes ;
  - XSS de `addLine()` corrige (`escHtml`) et options TVA creees via `new Option` ;
  - `saveAndCertify()` affiche les erreurs au lieu d'ignorer un echec ;
  - refus client d'une facture sans ligne.
- Route : variables de selection paiement/devise/statut ajoutees.
- Validation serveur des lignes de facture : au moins une ligne, quantite > 0, prix >= 0, remise 0-100, TVA 0/5/18.
- Paiement partiel preserve lors du recalcul (`montant_restant` ne repart plus au TTC total).
- Certification : refusee sur brouillon (422) et sur facture deja certifiee/synchronisee (409) ; boutons masques.
- Detail facture : statut colore, `None` SFEC gere, suppression avec retour d'erreur, `rel=noopener`.
- Liste : sync avec etat/erreur, `fmt` tolerant, badges de statut, mention « stats globales ».
- PDF : echappement XML des champs utilisateur, date de facture au lieu de `created_at`, devise en lettres, fallback RCCM.
- API : `limit`/`offset` bornes, alias `type`/`type_doc`, erreurs de validation en 422.
- Stats filtrées : `filtered_invoices_stats()` retourne total/statuts/CA/impayes/« a pousser Sage » selon les filtres ; cartes mises a jour par JS avec mention global/filtre (BL-03).
- Export CSV `/api/invoices/export` (5 000 lignes max, memes filtres que la liste), separateur `;` + BOM UTF-8, neutralisation injection CSV (`= + - @`	) via `_csv_safe()` ; bouton avec filtres courants.
- `tests/test_billing_security.py` : lignes invalides, paiement partiel, CA net des avoirs, immuabilite, limites, stats filtres, PDF avec `&`/`<`.

### Facturation — restant
- BL-03 : cartes statistiques recalculees sur le perimetre filtre via `/api/invoices/list` (`filtered_invoices_stats()`) ; mention dynamique « global / filtre ».
- BL-43 : numeros API valides (`^[A-Za-z0-9][A-Za-z0-9._/-]{0,49}$`) ; unicite SQLite conservee.
- BL-45/48 : champs non exposes (contact_id, montant HT brut) et regle taxable/type de tiers a affiner.
- BL-54 : push Sage/PDF synchrones ; a passer en tache de fond si volume.
- BL-62 : QR SFEC non decodable signale dans le PDF (« verifier sur le portail SFEC »), plus de regeneration locale.


## 4. Vague 3 — Annuaire / comptes (en cours)

### Termine
- Validation NIU (16-17 alphanumeriques), email, telephone, type de contact.
- `total` de `/api/contacts` calcule en base (plus la taille de la page) ; `limit` borne.
- Suppression d'un contact reference par une facture ou un ticket POS refusee (409, desactivation conseillee) + audit.
- Creation/modification de vendeur : code unique et borne, email/PIN valides, PIN **hashe** et masque dans les APIs.
- `ca_jour` vendeur corrige (plus de CA total affiche comme CA du jour).
- Admin : impossible de se desactiver, de changer son propre role ou de retirer le dernier admin actif.
- Parametres de securite valides cote serveur (provider, bornes, booleen https) ; `https_only` deja applique dynamiquement.
- Audit : IP remplie automatiquement depuis la requete ; `limit` de `/api/audit` borne.
- Journal d'audit `/utilisateurs` : pagination serveur 50/page avec nav Prec/Suiv ; `list_audit(limit, offset)` borne (1-500) et `count_audit()` (DI-35 partiel).
- `provider` n'est plus rendu avec `|safe`.
- Page `/clients` : compteurs globaux par type (plus de faux total a 500), code obligatoire a la creation et verrouille en edition, doublon de succes retire.
- Utilisateurs : role par defaut `responsable` (plus de role vide), reset de mot de passe par **modale masquee** au lieu de `prompt()`.
- `tests/test_directory_security.py` : NIU/email, total, reference, PIN, auto-desactivation admin, config invalide.

### Restant
- DI-03/44/45 : compteurs corriges, mais liste `/clients` toujours plafonnee a 500 lignes (pagination complete a faire).
- DI-10/48 : sync Sage des contacts/vendeurs (synchrone, non bornee).
- DI-26 : PIN toujours inutilise pour l'authentification (stockage hashe seulement).
- DI-35 : partiellement resolu (50/page serveur) ; reste rafraichissement auto (DB-10/13).
- DI-43/47 : certaines erreurs et suppressions restent a uniformiser.


## 5. Vague 4 — Dashboard / historique (en cours)

### Termine
- Metriques : « Factures Vente » exclut desormais les avoirs ; compteurs **Avoirs** et **A comptabiliser** separes ; `pending_accounting` base sur `DO_Statut=2` (et non `DO_Valide`).
- `/invoices` : type valide (`sale/purchase/avoir`), badge **Achat** correct, statut `LETTRE`, montants via `fmt_money`, pagination 100 avec total, page et liens.
- `/pending` : plus d'appel SFEC live bloquant ; utilise le cache SFEC complet, exclut `A_SURVEILLER`/deja certifie, filtre `statut_code=2`, avertit si SFEC indisponible/non configuree, pagination 100.
- « Surveiller » met aussi a jour le cache : la ligne disparait de `/pending` sans attendre une sync.
- « Certifier » manuel applique les regles avoirs (reference obligatoire, facture d'origine certifiee, anti-doublon).
- Impression certifiee : `items_json` accepte en chaine JSON, quantites/montants tolerants, date de repli, RCCM entreprise, remise sans double signe, lignes TVA nulles masquees.
- `/sales` : stats serveur filtrees (total et CA), echappement HTML des lignes JS, montants 2 decimales, `rel=noopener`, date `None` tolerante.
- Export CSV `/sales/export` tickets POS (meme filtres date/vendeur/search), bouton cote UI synchronise sur les filtres.
- `/health` et `/ready` : verif SQLite reelle, details des composants et HTTP 503 en panne.
- Dashboard : etat polling reel (config + thread), SFEC « Desactivee » si `enabled=false`, derniere sync en heure locale, erreur tronquee.
- Sync manuelle : verrou anti-concurrence (409), polling cote UI jusqu'a changement de `last_sync_at` au lieu d'un reload fixe a 2 s.
- `api()` redirige vers `/login` sur 401 au lieu d'afficher une liste vide.
- `/api/certified/list` : repli local Sage si le cache SFEC est vide.
- `/certified` : montants echappes, colspan 7, types inconnus affiches sans les assimiler a une vente ; `applyCerts()` mutualise le chargement, sync SFEC avec poll a etat.
- `tests/test_dashboard_security.py` : metriques, pending filtre/echappe, limites API, health/ready.

### Restant
- DB-03/04/06 : `/` et `get_metrics()` n'appellent plus le reseau ni Sage, mais le compteur certifiees ne se rafraichit qu'a la sync complete.
- DB-10/13 : pagination audit serveur faite (50/page) ; rafraichissement auto a definir.
- DB-20/24/47/49 : exports CSV faits (factures + tickets) ; filtres cote client restants a migrer cote serveur.
- DB-26/30 : validation SFEC depend encore de donnees Sage/cache ; cas hors-ligne a durcir.
- DB-34/65 : `/pending` et `/sales` ont une pagination limitee, pas encore de navigation complete partout.
- DB-43/52 : sync SFEC : bouton desactive, libelle d'avancement, poll toutes les 2 s jusqu'a changement de `last_sfec` (max 60 s), avertissement explicite si SFEC indisponible.
- DB-54 : impression certifiee sans repli local complet.

## 6. Tests et snapshots

- Baseline : `23 passed` apres V1.
- Apres POS/immuabilite : `28 passed`.
- Apres validation/immuabilite/PDF facturation : `35 passed`.
- Apres annuaire/comptes : `42 passed`.
- Apres dashboard/historique : `49 passed`.
- Apres stats filtrees facturation (BL-03) : `50 passed`.
- Apres pagination audit annuaire : `51 passed`.
- Apres exports CSV (factures + ventes POS) : `53 passed` (baseline routes 94 -> 96).
- Snapshots : **non regeneres volontairement** tant que le code bouge. Anciens diffs attendus (`15/51 OK`) ; `/logout` est desormais POST-only et `/api/contacts` repond `200`.

## 7. Prochains pas immediats

1. Verifier le parcours POS frontend complet (creation ticket B2B, erreur NIU, ticket imprime).
2. Traiter les restes facturation (stats filtrees, numerotation API, QR SFEC, push asynchrone).
3. Regenerer les snapshots seulement quand V2 est figee.
4. Passer a la Vague 3 (annuaire).

## 8. Fichiers touches (V1 + V2 + V3 + V4)

- `app/domain/pos.py`, `app/web/routes/pos.py`, `app/web/templates/pos/index.html`
- `app/domain/invoices.py`, `app/web/routes/billing.py`, `app/web/templates/billing/form.html`
- `app/web/auth/security.py`, `user_auth.py`, `app/web/routes/auth.py`
- `app/web/routes/config.py`, `templates/config/index.html`
- `app/web/common.py`, `app/web/static/css/app.css`
- `app/config/manager.py`, `app/storage/db.py`
- `app/web/routes/directory.py`, `templates/directory/clients.html`
- `app/web/routes/dashboard.py`
- `app/web/routes/directory.py`, `app/web/routes/pos.py`, `app/domain/pos.py`,
  `app/web/auth/user_auth.py`, `app/web/auth/security.py`, `app/web/routes/sync_api.py`
- `tests/conftest.py`, `tests/test_security.py`, `tests/test_avoirs.py`,
  `tests/test_pos_security.py`, `tests/test_billing_security.py`,
  `tests/test_directory_security.py`, `tests/test_dashboard_security.py`
