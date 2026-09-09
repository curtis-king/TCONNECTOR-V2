"""Passerelle (bridge) vers app.web.auth.user_auth.

Conservée à la racine le temps de la migration. Ré-exporte le module tel
quel : `import user_auth` donne bien le module migré, et
`from user_auth import <symbole>` fonctionne aussi.
"""
import sys as _sys
from app.web.auth import user_auth as _module

_sys.modules[__name__] = _module