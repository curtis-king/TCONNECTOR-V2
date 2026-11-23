"""Constantes d'exploitation partagées.

Valeurs par défaut uniquement : les paramètres réellement ajustables en
exploitation vivent dans `config.json` (sections `db` et `sfec`).
"""

# Supervision (`app/core/health.py`).
MAX_PENDING_WRITES = 50
MAX_MINUTES_WITHOUT_SFEC = 60

# Reconnexion ODBC (`app/integration/sage/database.py`) : backoff en secondes
# entre deux tentatives sur erreur transitoire.
CONNECT_RETRY_DELAYS_S = (1, 3, 7)
