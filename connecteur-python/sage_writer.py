"""Passerelle (bridge) vers app.integration.sage.writer.

Conservée à la racine le temps de la migration. Ré-exporte le module
complet tel quel.
"""
import sys as _sys
from app.integration.sage import writer as _module

_sys.modules[__name__] = _module