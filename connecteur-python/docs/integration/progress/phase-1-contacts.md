# Phase 1 — Push contacts → `F_COMPTET` : TERMINÉE

**Date** : 2026-09-22. **Fichier** : `app/integration/sage/writer.py` (Windows, lignes 24-100).

## Réalisé
- `write_contact_to_sage(contact)` : `IF NOT EXISTS` sur `CT_Num` puis `INSERT F_COMPTET (CT_Num≤17, CT_Intitule≤69, CT_Type, CT_Identifiant≤25, CT_Telephone≤21, CT_EMail≤69, CT_Adresse/Ville/Pays≤35)`, `CT_Raccourci`/`CT_NumPayeur`/`CG_NumPrinc`/`CO_No` laissés vides (contraintes `TG_INS_F_COMPTET` lues : `81003/81004/81035/81058/81263`).
- Mapping `client→0, fournisseur→1`, jamais `2` (trigger pièces exige `CT_Type=0`).
- Ligne existante avec mauvais type → erreur explicite, pas d'écrasement.
- Succès → `UPDATE contacts SET synced_sage=1, sage_ct_num` + `log_sync("sage_contact",...,"ok")`.
- `ensure_contact_in_sage(tiers_code)` : fast-path `synced_sage=1`, sinon création, sinon erreur explicite.

## Gate
- `py_compile` OK. Gate live G1 (10 `CLI-*` type 0 + 3 `FOU-*` type 1) : en attente redémarrage serveur + `full_sync` (Phase 5).
