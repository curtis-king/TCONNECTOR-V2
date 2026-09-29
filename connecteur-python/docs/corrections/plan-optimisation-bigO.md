# Plan d'optimisation — Big-O, batch, index (audit 2026-09-24)

> Référence : `C:\Users\tp_m2\Documents\projects\TCONNECTOR-V2\connecteur-python` (copie qui tourne).
> Méthode : audit statique en 2 agents (hotspots algo + requêtes/index SQLite), aucune modification.
> Principe : le Big-O paie où **n grandit** (sync Sage, certif, listes, polling) — pas sur les micro-détails.

---

## 0. Synthèse chiffrée

| # | Point chaud | Coût actuel | Cible | Gain |
|---|---|---|---|---|
| O1 | N+1 Sage : 1 requête lignes **par facture** (`database.py:677,960`) + `_db_lock` global | 1001 requêtes / 1000 factures, séquentiel | 1 requête groupée `IN (...)` + `dict`, `TOP` sur ventes | ÷500 requêtes |
| O2 | Auto-certif quadratique (`engine.py:570,577` + double `append` `:589/603`) | O(n²) + POST SFEC en double | 3 index `dict` + dédupe | O(n), 0 doublon |
| O3 | `/billing` ≈ 15 scans (`count_invoices` 8 + 7, `billing.py:27-59`) | 15 full-scans / affichage | 1 agrégation `COUNT FILTER / SUM CASE` | ÷15 |
| O4 | Index composites manquants (polling 15-60s) | full-scans à chaque tick | pack §4 (idempotent) | ticks ×10 plus légers |
| O5 | `columns.index()` en boucle (`database.py:661-674`) | ~175k scans / 1000 factures | `idx={nom:i}` une fois | 3 lignes, quasi-gratuit |

---

## Phase O1 — Batch lignes Sage + hoist index + TOP ventes

**Fichier** : `app/integration/sage/database.py`.
1. `fetch_sales_invoices` (`:659-678`), fallback (`:741-756`), `fetch_purchase_invoices` (`:954-961`), fallback (`:1021-1028`) :
   remplacer l'appel `fetch_doc_lines(domaine, type, piece)` **dans** la boucle par **1 requête** groupée
   `WHERE (DO_Domaine,DO_Type,DO_Piece) IN (...)` (ou `JOIN` sur le set de clés), puis `dict` par `DO_Piece`.
   Garder l'appel unitaire en repli sur miss uniquement.
2. Même fonctions (`:661-674` etc.) : hoister `idx = {c:i for i,c in enumerate(columns)}` avant la boucle ;
   remplacer les 7 `columns.index(...)` par `row[idx[...]]`.
3. `fetch_sales_invoices(limit=None)` (`:508-531`) : imposer `TOP` + pagination comme les achats (`TOP 1000`).
4. Option : libérer `_db_lock` (`:269`) autour des I/O réseau ou passer au pool (sinon `sync_all` reste sérialisé).

**Gate** : sync ventes 1000 factures : `1001 → ~3` requêtes ODBC (compter via log `t-connector.db`) ; temps sync ÷10 visé.

## Phase O2 — Auto-certif O(n) + dédupe + batch articles

**Fichiers** : `app/sync/engine.py`, `app/sync/article_sync.py`.
1. `auto_certify_invoices` (`:541-603`) : indexer une fois —
   `by_numero={i['numero']:i}`, `certified_set`, `avoir_refs` — remplacer `next(...)` (`:570`) et `any(...)` (`:577-581`) ;
   supprimer le bloc dupliqué (`:591-601`, 2ᵉ `append` `:603`) ; tri `:611` après dédupe par `id`.
2. `_upsert_products` (`article_sync.py:11-53`) : précharger `{ref:id, sage_ar_ref:id}` par `SELECT ... WHERE ref IN (...)` chunké (500-1000),
   puis `executemany()` ; scinder le `OR` en 2 lookups (ou `UNION`).
3. `_merge_article_stock` (`database.py:1289-1317`) : `by_ref={...}` puis 1 passe → `O(S+A)` au lieu de `O(S*A)`.

**Gate** : 0 doublon `to_certify` (log) ; import catalogue 10k articles sans `SELECT` par article.

## Phase O3 — Pack index SQLite + sargable + LIMIT

**Fichier** : `app/storage/db.py` (`init_database`/`_migrate`, tout `IF NOT EXISTS`).
1. Coller le pack (ventes/push/SFEC/listes/lignes/tickets/contacts/produits/auth) :
```sql
CREATE INDEX IF NOT EXISTS idx_invoices_type_doc ON invoices(type_doc);
CREATE INDEX IF NOT EXISTS idx_invoices_reference_invoice_id ON invoices(reference_invoice_id);
CREATE INDEX IF NOT EXISTS idx_invoices_contact_id ON invoices(contact_id);
CREATE INDEX IF NOT EXISTS idx_invoices_vendeur_id ON invoices(vendeur_id);
CREATE INDEX IF NOT EXISTS idx_invoices_tiers_code ON invoices(tiers_code);
CREATE INDEX IF NOT EXISTS idx_invoices_numero_source ON invoices(numero, source);
CREATE INDEX IF NOT EXISTS idx_invoices_push_sage ON invoices(synced_sage, source, statut, date_facture);
CREATE INDEX IF NOT EXISTS idx_invoices_pos_retry ON invoices(source, synced_sage, sfec_statut, date_facture);
CREATE INDEX IF NOT EXISTS idx_invoices_certif_list ON invoices(statut, sfec_statut, date_facture);
CREATE INDEX IF NOT EXISTS idx_invoices_list ON invoices(type_doc, statut, source, date_facture);
CREATE INDEX IF NOT EXISTS idx_invoices_sfec_num ON invoices(sfec_num_certif);
CREATE INDEX IF NOT EXISTS idx_invoice_lines_invoice_ligne ON invoice_lines(invoice_id, numero_ligne);
CREATE INDEX IF NOT EXISTS idx_invoice_lines_product ON invoice_lines(product_id);
CREATE INDEX IF NOT EXISTS idx_pos_ticket_lines_ticket_ligne ON pos_ticket_lines(ticket_id, numero_ligne);
CREATE INDEX IF NOT EXISTS idx_pos_ticket_lines_product ON pos_ticket_lines(product_id);
CREATE INDEX IF NOT EXISTS idx_pos_tickets_statut ON pos_tickets(statut);
CREATE INDEX IF NOT EXISTS idx_pos_tickets_statut_date ON pos_tickets(statut, date_ticket);
CREATE INDEX IF NOT EXISTS idx_pos_tickets_vendeur_statut_date ON pos_tickets(vendeur_id, statut, date_ticket);
CREATE INDEX IF NOT EXISTS idx_pos_tickets_invoice ON pos_tickets(invoice_id);
CREATE INDEX IF NOT EXISTS idx_pos_tickets_contact ON pos_tickets(contact_id);
CREATE INDEX IF NOT EXISTS idx_contacts_synced_code ON contacts(synced_sage, code);
CREATE INDEX IF NOT EXISTS idx_contacts_type_actif_nom ON contacts(type, est_actif, nom);
CREATE INDEX IF NOT EXISTS idx_products_actif_desig ON products(est_actif, designation);
CREATE INDEX IF NOT EXISTS idx_products_famille ON products(famille);
CREATE INDEX IF NOT EXISTS idx_products_barcode_actif ON products(barcode, est_actif);
CREATE INDEX IF NOT EXISTS idx_products_sage_ar_ref ON products(sage_ar_ref);
CREATE INDEX IF NOT EXISTS idx_vendeurs_actif_nom ON vendeurs(est_actif, nom);
CREATE INDEX IF NOT EXISTS idx_utilisateurs_email_lower ON utilisateurs(lower(email));
```
2. Réécrire les prédicats non-sargables en plages : `date(date_ticket)=date('now')` → `date_ticket>=date('now') AND date_ticket<date('now','+1 day')`
   (`pos.py:416,419,425`) ; `strftime('%Y-%m',substr(...))` → plage mois (`billing.py:53`).
3. Ajouter `LIMIT` aux 5 requêtes sans borne : `list_invoices_for_sfec` (`invoices.py:250`), `push_to_sage` + `certify_pending`
   + `push_contacts_to_sage` (`bidirectional.py:55,82,213`, batch 50-200), clients POS (`pos.py:107`).
4. Garder bi-sync ≥ 60s tant que O3 n'est pas en place (floor 15s = amplificateur de re-scans).

**Gate** : `EXPLAIN QUERY PLAN` sur les 3 requêtes du tick bi-sync → `SEARCH ... USING INDEX`, plus de `SCAN` ; ticks < 1s sur 1k factures.

## Phase O4 — Agrégation `/billing` + colonnes explicites

**Fichiers** : `app/domain/invoices.py`, `app/web/routes/billing.py`.
1. `count_invoices()` (`:259-278`, 8 requêtes) + 7 requêtes de `billing_page()` (`:27-59`) → **1 seule**
   `SELECT COUNT(*) FILTER (WHERE ...) / SUM(CASE WHEN ...)` (+ cache 30-60s).
2. `list_invoices()` (`:233`), `push/certify SELECT *` (`bidirectional.py:82,213`) : colonnes explicites, exclure
   `sfec_qr_code/sfec_signature/notes` des listes (1,5 Ko × 200 lignes = ~300 Ko par page évités).
3. HTML : accumuler en `list` + `"".join()` au lieu de `+=` (`directory.py`, `dashboard.py`, `billing.py`, `pos.py`) ;
   réduire les JSON de bootstrap (`contacts/products` 200 → paginé, `limit≤50`).

**Gate** : `/billing` : 15 scans → 1 ; payload page ÷3 ; TTFB < 500ms en local.

---

## Validation globale

```bash
python -m py_compile app/...   # fichiers touchés
pytest tests/                  # non-régression
# restart serveur Windows puis :
# - sync ventes : compter requêtes ODBC (log t-connector.db), viser ~3/1000 factures
# - EXPLAIN QUERY PLAN des 3 requêtes du tick (SEARCH, pas SCAN)
# - /billing TTFB < 500ms, tick bi-sync < 1s
```
Ordre d'exécution : **O1 → O2 → O3 → O4** (ROI décroissant, O1 seul déjà visible dès le prochain sync).
