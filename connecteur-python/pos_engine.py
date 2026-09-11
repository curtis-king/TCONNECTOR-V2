"""Passerelle (bridge) vers app.domain.pos.

Conservée à la racine le temps de la migration. Sera supprimée une fois
tous les modules migrés.
"""
from app.domain.pos import (  # noqa: F401
    check_stock_available,
    count_tickets,
    create_contact,
    create_product,
    create_ticket,
    create_vendeur,
    find_product_by_barcode,
    get_ticket,
    get_ticket_by_numero,
    get_ticket_tax_breakdown,
    get_vendeur,
    get_vendeur_stats,
    list_contacts,
    list_products,
    list_tax_rates,
    list_tickets,
    list_vendeurs,
    search_products,
    stock_control_enabled,
    top_selling_products,
    update_product,
    update_vendeur,
)
