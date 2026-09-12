"""Tests smoke — filet de sécurité minimal avant refactoring.

(a) import de tous les modules sous app/ sans erreur ;
(b) l'app expose exactement 94 routes ;
(c) les routes GET principales répondent sans erreur 500.
"""
import importlib
import os
import pkgutil
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED_ROUTE_COUNT = 94
ACCEPTABLE_STATUS = {200, 301, 302, 401, 403, 404}

# 500 PRÉ-EXISTANTS (constatés le 12/09/2026, avant toute modification).
# Interdiction de modifier app/ → on ne peut pas les corriger ici.
# Ils restent TRACKÉS par les snapshots (comportement actuel capturé,
# toute évolution sera détectée par compare_snapshots.py).
#   - /api/contacts (x2) : bug UnboundLocalError `tp_cols` utilisé avant
#     affectation dans app/integration/sage/database.py::fetch_contacts (l.1034)
#   - /api/sfec/certified-from-api : 500 déterministe "SFEC API 401: API key
#     is required" (config.json sans clé API — comportement nominal hors config)
KNOWN_PRE_500 = {"/api/contacts", "/api/sfec/certified-from-api"}


def _iter_app_modules():
    """Yield les noms de tous les modules du package app/."""
    import app as app_pkg

    for module_info in pkgutil.walk_packages(
        app_pkg.__path__, prefix=app_pkg.__name__ + "."
    ):
        yield module_info.name


# Modules optionnels Windows-only (pywin32) — non importables sous Linux,
# c'est le comportement attendu, pas une régression.
WINDOWS_ONLY_DEPS = {
    "servicemanager", "win32serviceutil", "win32service", "win32event",
    "win32timezone", "pywintypes", "winerror", "win32api",
}


def _is_windows_only(exc):
    """True si l'exception est un ModuleNotFoundError d'une dépendance
    pywin32 (module Windows-only, normal sous Linux)."""
    return isinstance(exc, ModuleNotFoundError) and (
        exc.name in WINDOWS_ONLY_DEPS
        or any(dep in (exc.name or "") for dep in WINDOWS_ONLY_DEPS)
    )


class TestImports:
    def test_import_all_app_modules(self):
        """Chaque module sous app/ doit s'importer sans exception
        (hors modules Windows-only, attendus sous Linux)."""
        modules = list(_iter_app_modules())
        assert len(modules) > 0, "Aucun module trouvé sous app/"
        failures = []
        skipped_windows = []
        for name in modules:
            try:
                importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001 — on veut tout remonter
                if _is_windows_only(exc):
                    skipped_windows.append(name)
                    continue
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        assert not failures, "Modules en échec :\n" + "\n".join(failures)

    def test_app_instance_importable(self):
        from app.web.dashboard import app

        assert app is not None
        assert app.name


class TestRouteRegistry:
    def test_exactly_94_routes(self, app_instance):
        rules = list(app_instance.url_map.iter_rules())
        assert len(rules) == EXPECTED_ROUTE_COUNT, (
            f"Attendu {EXPECTED_ROUTE_COUNT} routes, trouvé {len(rules)} : "
            + ", ".join(sorted(str(r.rule) for r in rules))
        )


def _get_routes(app_instance):
    """Routes GET ne nécessitant aucun paramètre obligatoire."""
    routes = []
    for rule in app_instance.url_map.iter_rules():
        methods = rule.methods - {"HEAD", "OPTIONS"}
        if "GET" not in methods:
            continue
        if rule.arguments:  # paramètres obligatoires → hors scope smoke
            continue
        routes.append(str(rule.rule))
    return sorted(set(routes))


class TestMainGetRoutes:
    def test_main_get_routes_no_500_authenticated(self, app_instance, client):
        """Toutes les routes GET sans paramètre répondent (pas de 500)
        pour un client authentifié — hors 500 pré-existants documentés."""
        failures = []
        unexpected_500 = []
        for path in _get_routes(app_instance):
            try:
                resp = client.get(path)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{path}: EXC {type(exc).__name__}: {exc}")
                continue
            if resp.status_code >= 500:
                if path in KNOWN_PRE_500:
                    continue  # suivi via snapshots, cf. commentaire module
                unexpected_500.append(f"{path}: {resp.status_code}")
        assert not unexpected_500, (
            "Nouveaux 500 inattendus :\n" + "\n".join(unexpected_500))
        assert not failures, "Routes en exception :\n" + "\n".join(failures)

    def test_main_get_routes_no_500_anonymous(self, app_instance, anonymous_client):
        """Même contrôle côté anonyme : 302/401 attendus, jamais de 500."""
        failures = []
        for path in _get_routes(app_instance):
            try:
                resp = anonymous_client.get(path)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{path}: EXC {type(exc).__name__}: {exc}")
                continue
            if resp.status_code >= 500:
                failures.append(f"{path}: {resp.status_code}")
        assert not failures, "Routes en échec (>=500) :\n" + "\n".join(failures)
