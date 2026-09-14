"""Fixtures pytest pour le filet de sécurité de la refactorisation Flask.

CHOIX D'AUTH (documenté) :
    L'auth est activée (`dashboard.auth_enabled: true` dans config.json).
    Plutôt que de la désactiver, on exécute un VRAI login via le client de
    test, avec les identifiants par défaut de config.json
    (`login_email` / `login_password`), migrés en base SQLite par
    `user_auth.migrate_admin_from_config()` — exactement comme le fait
    main.py au démarrage. Avantages :
      - les snapshots capturent les vraies pages HTML authentifiées
        (c'est ce qui doit rester identique après refactoring des templates) ;
      - le flux login/CSRF réel reste exercé (test smoke /login).

    L'initialisation SQLite (init_database + ensure_schema +
    migrate_admin_from_config) reproduit le démarrage de main.py afin que
    les routes puissent interroger la base sans 500.
"""
import logging
import os
import re
import sys

import pytest

# Garantir l'import du projet quelle que soit la CWD de pytest
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Réduire le bruit des logs applicatifs pendant les tests
logging.disable(logging.CRITICAL)


def _init_sqlite():
    """Initialise la base SQLite comme le fait main.py au démarrage."""
    from app.storage.db import init_database

    init_database()
    from app.web.auth import user_auth

    user_auth.ensure_schema()
    user_auth.migrate_admin_from_config()


@pytest.fixture(scope="session")
def app_instance():
    """Instance Flask globale (import side-effect, état actuel du code)."""
    _init_sqlite()
    from app.web.app import create_app

    # NOTE : TESTING=False volontairement — on veut le comportement PRODUCTION
    # (Flask renvoie une vraie réponse 500 au lieu de propager l'exception),
    # ce qui permet de capturer dans les snapshots le comportement actuel,
    # y compris les 500 pré-existants (bug connu fetch_contacts, par ex.).
    return create_app()


@pytest.fixture(scope="session")
def admin_credentials():
    """Identifiants admin lus depuis la config (défauts de config.json)."""
    from app.config.manager import get_config

    dash = get_config().get("dashboard", {})
    return {
        "email": dash.get("login_email", "admin@example.com"),
        "password": dash.get("login_password", "CHANGE-ME"),
    }


def _login_client(client, email, password):
    """Authentifie un test_client via le vrai POST /login (avec CSRF)."""
    page = client.get("/login")
    m = re.search(r'name="_csrf" value="([^"]+)"', page.get_data(as_text=True))
    csrf = m.group(1) if m else ""
    resp = client.post(
        "/login",
        data={"email": email, "password": password, "_csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), (
        f"Échec du login de test (status {resp.status_code}). "
        "Vérifiez login_email/login_password dans config.json."
    )
    return client


@pytest.fixture(scope="session")
def client(app_instance, admin_credentials):
    """Client de test AUTENTIFIÉ (admin) — le cas nominal de l'app."""
    c = app_instance.test_client()
    return _login_client(c, admin_credentials["email"], admin_credentials["password"])


@pytest.fixture(scope="session")
def anonymous_client(app_instance):
    """Client de test NON authentifié (pour tester les redirections /login)."""
    return app_instance.test_client()
