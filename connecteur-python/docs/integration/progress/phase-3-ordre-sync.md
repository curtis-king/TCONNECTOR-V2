# Phase 3 — Ordre de sync (contacts avant factures) : TERMINÉE

**Date** : 2026-09-22. **Fichier** : `app/sync/bidirectional.py` (lignes 6, 48-70, 244, total enrichi).

## Réalisé
- `push_contacts_to_sage()` : `SELECT * FROM contacts WHERE synced_sage=0 ORDER BY code` → `write_contact_to_sage` par contact, stats `{pushed, errors}`, log `Push contacts Sage: X ecrits, Y erreurs`.
- `full_sync()` : `certify → push_contacts_to_sage → push_to_sage → pull_from_sage` ; `total` enrichi (`pushed_contacts`, erreurs contacts incluses).
- Filet double : ordre global + `ensure inline` par facture (Phase 2).

## Gate
- `py_compile` OK. Gate live : ligne `Push contacts Sage:` visible AVANT `Push Sage:` dans `data/output.log` après redémarrage (Phase 5).
