# Plan de dev — Connecteur universel (base v2, BIJOU = adaptateur n°1)

> Base unique : `C:\Users\tp_m2\Documents\projects\TCONNECTOR-V2\connecteur-python` (celle qui tourne).
> Principe : on ne fork pas, on extrait. BIJOU reste en production pendant tout le chantier.
> Références métier : `docs/conception-connecteur-custom/metier-backend.md` + `modele-metier.puml`.

---

## U0. Gel du socle (1 jour, sans coder)

- Figer : 8 entités, formats `FA/AV/TK{:06d}`, règles R1-R11, formules (centime 5%, prorata, monnaie),
  contrat SFEC, `thresholds.json`, fixtures `FA-PERF-*`.
- Règle d'or : **aucun changement de comportement BIJOU** pendant les extractions.
- Gate U0 : `pytest` vert + snapshots 51/51 + suite perf 8/8 → sinon stop avant U1.

---

## U1. Lib calcul pure (risque nul, en premier)

- Créer `app/domain/pricing.py` : `calc_line_totals`, `calc_line_ticket_totals`, prorata remise globale,
  centime additionnel, montant en lettres, mapping paiements → SFEC. **Zéro import DB/réseau.**
- `app/domain/invoices.py` + `app/domain/pos.py` appellent la lib (résultats identiques, prouvés par tests).
- Gate U1 : `pytest` vert + 100% coverage lib (zéros, arrondis `0.005`, quantités négatives rejetées).

---

## U2. `BillingSystemAdapter` + mapping déclaratif (le cœur de l'universel)

- Créer `app/integration/adapter_base.py` :
  `fetch_invoices/fetch_contacts/fetch_tax_rates/fetch_articles/write_contact/write_invoice/mark_*`.
- Créer `app/integration/mappings/<client>.yaml` : correspondance champs (`numero`, `montant_ttc`,
  `statut_confirme`…), taux, statuts, connexion (driver/serveur/base).
- Le moteur (`app/sync/`, `app/domain/`) ne parle **qu'à** l'adaptateur — plus aucun `F_*`, `DO_*`, ODBC en dur.
- Gate U2 : moteur inchangé sur tests mockés (`tests/perf/` + `test_avoirs` verts).

---

## U3. BIJOU devient l'adaptateur n°1 (sans régression)

- Déplacer le code Sage (`app/integration/sage/database.py`, `writer.py`, triggers 82019 digérés,
  `CodeTaxe C18/C05`, pool ODBC) derrière `SageBijouAdapter(BillingSystemAdapter)` + `mappings/bijou.yaml`.
- Gate U3 : sync BIJOU réel identique (mêmes pièces, mêmes montants, `Push Sage: 0 erreur`,
  snapshots 51/51 verts, suite perf verte).

---

## U4. BIOMEDICALE = adaptateur n°2 (bloqué : colonnes `FACTURES` manquantes)

- Prérequis client : `SELECT TOP 1 * FROM FACTURES` + noms des tables liées (lignes ? clients ? TVA ?).
- Connu d'avance : serveur `SERVEUR-DC`, base `FACTURES`, `StatutFacture='2'` = confirmée,
  champ `Entreprise` = n° client.
- Créer `mappings/biomedicale.yaml` + connexion `SERVEUR-DC`.
- Gate U4 : lecture + écriture test sur base de recette client (jamais la prod), `Push: 0 erreur`.

---

## U5. Industrialisation (réutilisée, pas réécrite)

- `tests/perf/` : scénarios par adaptateur (mêmes seuils : TTFB 500/400/800ms, sync 60/15s, DB < 20).
- CI `.github/workflows/` : matrice `adaptateur × tests` (BIJOU mocké, BIOMEDICALE mocké).
- Docs : `metier-backend.md` + `.puml` mis à jour (adaptateur au centre, BIJOU en exemple d'implémentation).

---

## Ordre, effort, gates

| Phase | Effort | Gate de sortie |
|---|---|---|
| U0 gel | 1 jour | pytest + snapshots + perf verts |
| U1 lib calcul | ~2h, risque nul | 100% coverage lib |
| U2 adaptateur + YAML | ~1 jour | moteur inchangé sur mocks |
| U3 BIJOU adaptateur n°1 | ~2 jours | sync réel identique, 0 erreur |
| U4 BIOMEDICALE | ~1 jour (dès colonnes reçues) | lecture/écriture recette OK |
| U5 industrialisation | ~1 jour | CI verte par adaptateur |

On ne passe à la phase suivante que gates verts. En cas de rouge : fix avant d'avancer, jamais de dette reportée.
