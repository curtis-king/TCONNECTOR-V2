# Phase 2 — Writer utilise le `CT_Num` Sage : TERMINÉE

**Date** : 2026-09-22. **Fichier** : `app/integration/sage/writer.py` (lignes 153-161).

## Réalisé
- `write_invoice_to_sage` : `tiers_val = ensure_contact_in_sage(tiers_code)["ct_num"]` au lieu du code local brut.
- Contact inconnu ou création impossible → `return {"success": False}` + `log_sync status="error"` explicite. Aucun remap silencieux vers `COMPTOIR`.
- C'est ce qui lève le `82019` : `DO_Tiers` vaut désormais toujours un `CT_Num` existant `CT_Type=0`.

## Gate
- `py_compile` OK. Gate live : `DO_Tiers=CLI-001` accepté par `TG_INS_CPTAF_DOCENTETE` (Phase 5, avec redémarrage).
