"""Passerelle (bridge) vers app.config.manager.

Conservée à la racine le temps de la migration pour ne pas casser les
imports existants. Sera supprimée une fois tous les modules migrés.
"""
from app.config.manager import (  # noqa: F401
    load_config,
    save_config,
    get_config,
    get_db_config,
    get_sfec_config,
    get_company_config,
    get_dashboard_config,
    update_section,
    CONFIG_FILE,
    CONFIG_DIR,
)
