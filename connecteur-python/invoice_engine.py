"""Passerelle (bridge) vers app.domain.invoices.

Conservée à la racine le temps de la migration. Sera supprimée une fois
tous les modules migrés.
"""
from app.domain.invoices import (  # noqa: F401
    count_invoices,
    create_invoice,
    delete_invoice,
    get_invoice,
    get_invoice_by_numero,
    list_invoices,
    list_invoices_for_sfec,
    update_invoice,
    update_invoice_sfec,
)
