"""Contrat des façades Sage après le découpage de `database.py` (LOT 2).

`database` reste le point d'entrée public ; les implémentations vivent dans
`connection`, `schema`, `queries.*`, `values`, `errors` et `sql`. Ces tests
verrouillent les noms exposés et l'identité des objets partagés.
"""
import pytest

from app.integration.sage import connection, database, schema
from app.integration.sage import queries

# Noms importés par app/, scripts/ et les tests.
API_DATABASE = [
    # connexion
    "get_pool", "close_pool", "get_cursor", "ping_database",
    "_connect", "_connect_with_retry", "_checkout", "_is_transient",
    "_server_clause", "_build_conn_string",
    # schéma
    "discover_tables", "get_table_map", "get_domain_config",
    "ensure_sfec_columns", "list_all_tables", "_refresh_sfec_columns",
    # lectures
    "fetch_sales_invoices", "fetch_purchase_invoices", "fetch_doc_lines",
    "fetch_doc_lines_batch", "fetch_certified_invoices", "fetch_contacts",
    "fetch_contact_by_code", "fetch_articles", "fetch_tax_rates",
    "fetch_ledger_accounts",
    # écritures et marquages
    "write_sfec_to_invoice", "reset_stuck_en_cours", "reset_sfec_monitor",
    "mark_certifying", "mark_to_monitor", "mark_certified",
    "mark_certification_failed", "PermanentSfecWriteError",
    # helpers partagés
    "_safe_float", "get_devise_map", "_map_devise",
]


@pytest.mark.parametrize("name", API_DATABASE)
def test_database_expose_le_nom(name):
    assert hasattr(database, name), f"database.{name} doit rester exposé"


def test_connection_expose_son_api():
    for name in ("get_pool", "close_pool", "get_cursor", "ping_database"):
        assert hasattr(connection, name)


def test_schema_expose_son_api():
    for name in ("discover_tables", "get_table_map", "get_domain_config",
                 "ensure_sfec_columns"):
        assert hasattr(schema, name)


def test_les_facades_partagent_les_memes_objets():
    assert database.get_table_map is schema.get_table_map
    assert database.get_cursor is connection.get_cursor
    assert database.discover_tables is schema.discover_tables


def test_queries_invoices_rexpose_les_ecritures():
    """Ancienne façade : les écritures sont résolues à la demande."""
    for name in ("write_sfec_to_invoice", "mark_certified"):
        assert getattr(queries.invoices, name) is getattr(database, name)

    with pytest.raises(AttributeError):
        queries.invoices.inexistant  # noqa: B018


def test_aucun_module_ne_depasse_600_lignes():
    """Cible du plan, hors `writer.py` (déjà autonome avant le découpage)."""
    import pathlib

    racine = pathlib.Path(database.__file__).parent
    trop_gros = []
    for chemin in racine.rglob("*.py"):
        if chemin.name == "writer.py":
            continue
        n = len(chemin.read_text(encoding="utf-8").splitlines())
        if n > 600:
            trop_gros.append((str(chemin.relative_to(racine)), n))
    assert trop_gros == [], f"modules > 600 lignes: {trop_gros}"
