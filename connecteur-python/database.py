"""Passerelle (bridge) vers app.integration.sage.database.

Conservée à la racine le temps de la migration. Sera supprimée une fois
tous les modules migrés (étape à laquelle database.py sera découpé en
connection.py / schema.py / queries).
"""
from app.integration.sage.database import (  # noqa: F401
    ensure_sfec_columns,
    get_pool,
    close_pool,
    get_cursor,
    ping_database,
    discover_tables,
    get_table_map,
    get_domain_config,
    fetch_sales_invoices,
    fetch_doc_lines,
    fetch_purchase_invoices,
    fetch_contacts,
    fetch_tax_rates,
    fetch_ledger_accounts,
    fetch_articles,
    write_sfec_to_invoice,
    reset_stuck_en_cours,
    mark_certifying,
    mark_to_monitor,
    mark_certified,
    mark_certification_failed,
    fetch_certified_invoices,
    list_all_tables,
)
