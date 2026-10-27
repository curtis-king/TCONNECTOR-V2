"""Tests de la sync complete manuelle lancee depuis /billing.

La sync complete doit etre asynchrone : la requete HTTP repond 202, puis
l'interface interroge /api/sync/bi/stats pour suivre l'execution.
"""
import threading

import pytest

from app.sync import bidirectional as bidi


def _csrf(client):
    return client.get("/api/csrf").get_json()["token"]


@pytest.fixture(autouse=True)
def _reset_manual_sync_state():
    with bidi._sync_lock:
        bidi._sync_active = False
        bidi._manual_sync_state.update({
            "running": False,
            "mode": "",
            "started_at": None,
            "finished_at": None,
            "last_result": None,
            "last_error": None,
        })
    yield
    thread = bidi._manual_sync_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    with bidi._sync_lock:
        bidi._sync_active = False
        bidi._manual_sync_state.update({
            "running": False,
            "mode": "",
            "started_at": None,
            "finished_at": None,
            "last_result": None,
            "last_error": None,
        })


def test_billing_page_affiche_les_deux_syncs(client, monkeypatch):
    monkeypatch.setattr(
        "app.web.routes.billing.get_config",
        lambda: {"db": {"sage_pull_days": 7}},
    )
    resp = client.get("/billing")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Sync Sage (7 j)" in html
    assert "Sync complète" in html
    assert "Pull auto: 7 jours" in html


def test_config_page_expose_sage_pull_days(client):
    resp = client.get("/config")
    assert resp.status_code == 200
    assert 'name="sage_pull_days"' in resp.get_data(as_text=True)


def test_start_manual_sync_refuse_si_auto_en_cours():
    with bidi._sync_lock:
        bidi._sync_active = True

    result = bidi.start_manual_sync(force_all=True)

    assert result["success"] is False
    assert "deja en cours" in result["error"]


def test_post_sync_bi_force_all_repond_202_puis_409(client, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def _slow_full_sync(force_all=False):
        calls.append(force_all)
        started.set()
        assert release.wait(5), "le test n'a pas libere la sync"
        return {
            "pushed": 0,
            "pushed_contacts": 0,
            "pull_new": 0,
            "pull_updated": 0,
            "certified": 0,
            "errors": 0,
        }

    monkeypatch.setattr(bidi, "full_sync", _slow_full_sync)
    token = _csrf(client)

    try:
        resp = client.post(
            "/api/sync/bi",
            json={"force_all": True},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 202
        payload = resp.get_json()
        assert payload["success"] is True
        assert payload["started"] is True
        assert payload["mode"] == "full"
        assert started.wait(2), "la sync asynchrone n'a pas demarre"

        stats = client.get("/api/sync/bi/stats").get_json()
        assert stats["running"] is True
        assert stats["mode"] == "full"
        assert stats["last_result"] is None

        resp = client.post(
            "/api/sync/bi",
            json={"force_all": False},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 409
        assert resp.get_json()["success"] is False
    finally:
        release.set()
        if bidi._manual_sync_thread:
            bidi._manual_sync_thread.join(timeout=5)

    assert calls == [True]
    stats = client.get("/api/sync/bi/stats").get_json()
    assert stats["running"] is False
    assert stats["finished_at"]
    assert stats["last_result"]["errors"] == 0
    assert stats["last_error"] is None
