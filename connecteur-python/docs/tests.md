# Stratégie de tests

## Suite par défaut (CI)

```bash
cd connecteur-python
python -m pytest tests --ignore=tests/perf \
  -m "not sandbox and not integration" \
  --cov=app.sync --cov=app.integration
```

- Aucun accès réseau, aucun Sage réel : `tests/conftest.py` pointe la config
  de test vers un port fermé et copie `config.json` dans un répertoire
  temporaire.
- La base SQLite est elle aussi isolée (copie de `data/tconnector.db`).
- `config.example.json` doit rester utilisable tel quel : la CI le copie en
  `config.json`.
- La couverture `app/sync` + `app/integration` est mesurée. Plancher CI :
  **50 %** (mesuré 50,6 % au 2026-11-23), cible 60 %. Le rapport XML est
  publié en artefact.

## Marqueurs

| Marqueur | Activation | Contenu |
|----------|-----------|---------|
| `integration` | `TCONNECTOR_TEST_REAL_SAGE=1` | contrat Sage réel (lecture seule + écriture sur facture absente) |
| `sandbox` | `TCONNECTOR_TEST_SFEC_SANDBOX=1` | appels réels à l'API sandbox SFEC |
| `perf` | ignoré par défaut | mesures de performance |

Exemple de contrat Sage local :

```bash
TCONNECTOR_TEST_REAL_SAGE=1 pytest -m integration \
  tests/test_sage_contract_integration.py -v
```

Ces tests ne sont jamais bloquants en CI : ils dépendent d'un tunnel ODBC et
d'une configuration locale. Ils sont exécutés avant livraison lorsqu'un accès
Sage/SFEC est disponible.

## Tests de contrat

| Fichier | Frontière verrouillée |
|---------|-----------------------|
| `tests/test_sfec_http_contract.py` | payload SFEC réellement posté, statuts HTTP → classes d'erreur, idempotence de la vérification par numéro |
| `tests/test_sfec_api_contract.py` | routes `/api/sfec/pending` et `/flush` : auth, CSRF, 202, 409, 500 |
| `tests/test_sage_write_contract.py` | SQL de `write_sfec_to_invoice` (table quotée une fois, 0 ligne, identifiants invalides) |
| `tests/test_sage_contract_integration.py` | schéma et lectures Sage réels, écriture sans effet sur facture absente |
| `tests/test_error_taxonomy.py` | table de correspondance message → classe → politique de reprise |
| `tests/test_engine_public_api.py`, `tests/test_sage_facade_api.py` | symboles ré-exportés par les façades après découpage |

## Lint

`ruff.toml` fige une sélection minimale (`E4`, `E7`, `E9`, `F`). La CI bloque
sur `E9,F63,F7,F82` (syntaxe, nom indéfini, import cassé) et rapporte le style
du legacy sans bloquer. Les nouveaux modules et tests doivent passer
`ruff check` sans erreur.
