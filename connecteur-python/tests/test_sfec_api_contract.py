"""Contrat HTTP des routes d'exploitation de la file SFEC.

Couvre `/api/sfec/pending` et `/api/sfec/pending/flush` : authentification,
CSRF, forme de la réponse, 202, 409 de concurrence et 500 si le thread ne
démarre pas. Aucun rejeu réel n'est exécuté.
"""
import pytest

from app.web.routes import sync_api


@pytest.fixture(autouse=True)
def _guard_release():
    """Aucun test ne doit laisser le verrou de rejeu acquis."""
    yield
    if sync_api._flush_guard.locked():
        sync_api._flush_guard.release()


@pytest.fixture
def csrf(client):
    return client.get("/api/csrf").get_json()["token"]


class _ThreadSynchrone:
    """Thread qui s'exécute immédiatement : rend le test déterministe."""

    def __init__(self, target=None, daemon=None, **_kwargs):
        self._target = target

    def start(self):
        self._target()


class _ThreadEnEchec:
    def __init__(self, target=None, daemon=None, **_kwargs):
        pass

    def start(self):
        raise RuntimeError("impossible de demarrer le thread")


# ── Authentification ───────────────────────────────────────────────────────
def test_pending_exige_une_session(anonymous_client):
    resp = anonymous_client.get("/api/sfec/pending")

    # Les routes /api renvoient 401 (pas de redirection HTML).
    assert resp.status_code == 401
    assert resp.get_json()["error"]


def test_flush_exige_une_session(anonymous_client):
    resp = anonymous_client.post("/api/sfec/pending/flush")

    assert resp.status_code == 401
    assert resp.get_json()["error"]


# ── GET /api/sfec/pending ──────────────────────────────────────────────────
def test_pending_expose_les_trois_compteurs(client):
    resp = client.get("/api/sfec/pending")

    assert resp.status_code == 200
    corps = resp.get_json()
    assert set(corps) == {"total", "blocked", "retrying"}
    assert all(isinstance(v, int) and v >= 0 for v in corps.values())
    assert corps["retrying"] == corps["total"] - corps["blocked"]


# ── POST /api/sfec/pending/flush ───────────────────────────────────────────
def test_flush_exige_le_jeton_csrf(client):
    resp = client.post("/api/sfec/pending/flush")

    assert resp.status_code == 400
    assert "CSRF" in resp.get_json()["error"]


def test_flush_repond_202_et_rejoue_en_force(client, csrf, monkeypatch):
    appels = []
    monkeypatch.setattr(sync_api.threading, "Thread", _ThreadSynchrone)
    monkeypatch.setattr(
        sync_api, "flush_pending_writes",
        lambda force=False: appels.append(force) or 2,
    )

    resp = client.post("/api/sfec/pending/flush",
                       headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 202
    assert resp.get_json()["ok"] is True
    assert appels == [True]
    assert not sync_api._flush_guard.locked()


def test_flush_concurrent_repond_409(client, csrf):
    assert sync_api._flush_guard.acquire(blocking=False)

    resp = client.post("/api/sfec/pending/flush",
                       headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False


def test_flush_thread_en_echec_repond_500_et_libere_le_verrou(
        client, csrf, monkeypatch):
    monkeypatch.setattr(sync_api.threading, "Thread", _ThreadEnEchec)

    resp = client.post("/api/sfec/pending/flush",
                       headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 500
    assert resp.get_json()["ok"] is False
    assert not sync_api._flush_guard.locked()
