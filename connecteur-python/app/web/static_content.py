"""Lecture du contenu statique (CSS/JS/HTML) extrait de dashboard.py.

Déplace le gros volume de CSS hors des fichiers Python vers
app/web/static/, tout en préservant un rendu identique (le CSS reste
injecté inline comme avant).
"""
import os
import threading

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_cache = {}
_cache_lock = threading.Lock()


def read_static(rel_path):
    """Renvoie le contenu d'un fichier sous static/, en le mettant en cache."""
    with _cache_lock:
        if rel_path in _cache:
            return _cache[rel_path]
    full = os.path.join(_STATIC_DIR, rel_path)
    with open(full, "r", encoding="utf-8") as f:
        content = f.read()
    with _cache_lock:
        _cache[rel_path] = content
    return content
