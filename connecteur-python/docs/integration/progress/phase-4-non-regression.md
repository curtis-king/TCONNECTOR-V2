# Phase 4 — Non-régression : VÉRIFIÉE

**Date** : 2026-09-22 (relecture + `py_compile` des 2 fichiers édités : OK).

## Acquis intacts
- `DO_CodeTaxe1=code_entete` (`writer.py:171`) + `DL_CodeTaxe1=_code_taxe(...)` (`:193,202`), `_code_taxe` (`:125`), fallback TVA `18` (`:203`), `avoir_type=7` (`:21`).
- `database.py` non touché : `avoir_type=7`, devise `CAST`, NIU→`CT_Identifiant`, `tp_cols` déjà OK (relectures du jour).
- `F_TAXE` : `C18/C05/D18/D05` en base, historique `C20` intact.

## Gate
- Aucune suppression/modification de logique existante (diff = ajouts + 1 substitution `tiers_val`). `py_compile` OK sur `writer.py` + `bidirectional.py`.
