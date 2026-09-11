"""Passerelle (bridge) vers app.domain.pdf.

Conservée à la racine le temps de la migration. Sera supprimée une fois
tous les modules migrés.
"""
from app.domain.pdf import (  # noqa: F401
    HAS_REPORTLAB,
    generate_invoice_pdf,
    generate_ticket_pdf,
)
