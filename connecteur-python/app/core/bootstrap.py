"""Bootstrap runtime : vérification des dépendances externes au démarrage.

Version allégée après suppression du module print_hijack (watcher PDF,
impression silencieuse SumatraPDF, multi-sites).

Aucune dépendance externe critique n'est vérifiée ici : le connecteur
démarre quel que soit l'état de l'environnement.
"""
import logging

from app.core.paths import data_dir

logger = logging.getLogger("t-connector.bootstrap")


def ensure_runtime_dependencies(config=None):
    """Point d'entrée conservé pour compatibilité avec main.py.

    Ne fait plus de vérification externe : retourn un rapport vide.
    """
    if config is None:
        from app.config.manager import get_config
        config = get_config() or {}
    report = {}
    logger.info("[BOOTSTRAP] Rapport dependances: neant (print_hijack supprime)")
    return report
