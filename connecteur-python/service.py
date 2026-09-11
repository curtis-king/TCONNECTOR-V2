"""Passerelle (bridge) vers app.service.windows_service.

Conservée à la racine le temps de la migration. Ré-exporte le module
complet (classe TConnectorService, etc.). L'invocation directe
`python service.py ...` propage la ligne de commande vers la logique
migrée, comme avant.
"""
import sys as _sys
from app.service import windows_service as _module

_sys.modules[__name__] = _module

if __name__ == "__main__":
    _module._run_cli(_sys.argv)