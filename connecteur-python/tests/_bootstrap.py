"""Isole les tests de la configuration et de la base SQLite de développement.

Importé en tout premier par `tests/conftest.py`, avant tout import de `app.*`.
Les modules `app.storage.db` et `app.config.manager` lisent leurs chemins à
l'import : les variables d'environnement doivent donc être positionnées ici.

La base de test est une COPIE (backup SQLite cohérent, WAL inclus) de la base
de dev afin de conserver les snapshots actuels, mais toutes les écritures des
tests restent confinées au répertoire temporaire.
"""
import atexit
import os
import shutil
import sqlite3
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)

_TMP_DIR = None


def _copy_sqlite(source, target):
    """Copie une base SQLite (y compris le WAL) dans target."""
    src = sqlite3.connect(source, timeout=10)
    try:
        dst = sqlite3.connect(target, timeout=10)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def setup():
    """Prépare les chemins temporaires et exporte les variables d'env."""
    global _TMP_DIR
    if os.environ.get("TCONNECTOR_TEST_TMP"):
        _TMP_DIR = os.environ["TCONNECTOR_TEST_TMP"]
        return _TMP_DIR

    _TMP_DIR = tempfile.mkdtemp(prefix="tconnector-tests-")
    config_src = os.path.join(PROJECT_ROOT, "config.json")
    config_dst = os.path.join(_TMP_DIR, "config.json")
    if os.path.exists(config_src):
        shutil.copy2(config_src, config_dst)

    db_src = os.path.join(PROJECT_ROOT, "data", "tconnector.db")
    db_dst = os.path.join(_TMP_DIR, "tconnector.db")
    if os.path.exists(db_src):
        _copy_sqlite(db_src, db_dst)

    os.environ["TCONNECTOR_TEST_TMP"] = _TMP_DIR
    os.environ["TCONNECTOR_CONFIG_FILE"] = config_dst
    os.environ["TCONNECTOR_DB_PATH"] = db_dst

    atexit.register(shutil.rmtree, _TMP_DIR, True)
    return _TMP_DIR


setup()
