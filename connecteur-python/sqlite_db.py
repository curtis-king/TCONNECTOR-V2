"""Passerelle (bridge) vers app.storage.db.

Conservée à la racine le temps de la migration pour ne pas casser les
imports existants (sqlite_db est importé par de nombreux modules).
Sera supprimée une fois tous les modules migrés.
"""
from app.storage.db import (  # noqa: F401
    DATA_DIR,
    DB_PATH,
    SCHEMA,
    calc_line_ticket_totals,
    calc_line_totals,
    close_connection,
    generate_invoice_number,
    generate_ticket_number,
    get_connection,
    get_cursor,
    get_setting,
    init_database,
    log_sync,
    recalc_invoice_totals,
    recalc_ticket_totals,
    row_to_dict,
    rows_to_list,
    set_setting,
    update_invoice_sfec,
)
