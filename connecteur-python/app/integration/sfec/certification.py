"""Orchestration de la certification SFEC.

Les fonctions sont définies dans `app.integration.sfec.endpoints` et
ré-exportées ici. Elles gèrent l'appel au SfecClient, le polling et le
stockage local des certificats.
"""
from app.integration.sfec.endpoints import (  # noqa: F401
    certify_invoice,
    certify_sqlite_invoice,
    check_health,
    get_certified_by_sage_id,
    get_all_certified,
    clear_failed_certifications,
)
