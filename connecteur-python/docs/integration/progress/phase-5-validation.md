# Phase 5 — Validation : CODE OK, GATES LIVE EN ATTENTE RESTART

**Date** : 2026-09-22.

## Fait ici
- `python3 -m py_compile writer.py bidirectional.py` → OK.
- Ancres vérifiées par grep (lignes exactes ci-dessus, Phases 1-4).
- Miroir WSL resynchronisé (`writer.py`, `bidirectional.py` copiés depuis Windows).

## À exécuter côté Windows (redémarrage serveur OBLIGATOIRE — le process garde l'ancien code)
1. Redémarrer le connecteur.
2. Attendre 2 cycles de sync, puis :
```bash
grep "Push contacts Sage" data/output.log | tail -n 2   # attendu : "14 ecrits, 0 erreurs"
grep "Push Sage" data/output.log | tail -n 2            # attendu : "N ecrites, 0 erreurs"
grep -c "82019" data/output.log                          # ne doit plus augmenter
```
3. Gates SQL (`plan-correction-push-sage.md` §Phase 5 : G1 G3 G4), dont :
```sql
SELECT DO_Piece,DO_Tiers,DO_CodeTaxe1 FROM F_DOCENTETE WHERE DO_Piece='FA000026';
```
**Succès indiscutable = G1+G2+G3+G4 verts sur 2 cycles consécutifs.** Si `CT_Type` existant bloque un contact (erreur explicite `CT_Num ... existe avec CT_Type=X`), traiter au cas par cas (mapping manuel, jamais de `DELETE` aveugle).
