import os
import sys
import win32serviceutil

# Ajoute la racine du projet au sys.path (2 niveaux au-dessus de scripts/windows/).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from service import TConnectorService

win32serviceutil.HandleCommandLine(TConnectorService)