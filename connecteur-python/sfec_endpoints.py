"""Passerelle (bridge) vers app.integration.sfec.endpoints.

Conservée à la racine le temps de la migration. Sera supprimée une fois
tous les modules migrés.
"""
from app.integration.sfec.endpoints import (  # noqa: F401
    DATA_DIR,
    CERTIFIED_FILE,
    db_invoice_to_sfec,
    sqlite_invoice_to_sfec,
    validate_sfec_payload,
    preview_sfec_payload,
    certify_invoice,
    certify_sqlite_invoice,
    check_health,
    get_certified_by_sage_id,
    get_all_certified,
    clear_failed_certifications,
    _extract_cert_data,
)
