"""Tests du correctif 02 : fin de la tempete de retentatives SFEC.

Regles verifiees :
  - `reset_stuck_en_cours()` ne libere que les `EN_COURS` (jamais `ERREUR`) ;
  - deux passes d'auto-certification ne peuvent pas se chevaucher ;
  - un snapshot contenant des doublons ne certifie qu'une fois ;
  - une facture `A_SURVEILLER` n'est pas rejouee automatiquement ;
  - 400/422 -> `A_SURVEILLER`, sans ecriture `ERREUR` ;
  - 4 erreurs transitoires -> `A_SURVEILLER`, avec backoff.
"""
import logging
import threading
import time

import pytest

from app.integration.sage import database as sage_db
from app.sync import engine


class _FakeCursor:
    def __init__(self):
        self.sql = ""
        self.params = ()
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params or ()


class _FakeCtx:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *args):
        return False


@pytest.fixture(autouse=True)
def _clean_backoff():
    engine._retry_attempts.clear()
    engine._retry_after.clear()
    engine._logged_errors.clear()
    yield
    engine._retry_attempts.clear()
    engine._retry_after.clear()
    engine._logged_errors.clear()


def test_reset_stuck_only_en_cours(monkeypatch):
    cur = _FakeCursor()
    monkeypatch.setattr(sage_db, "get_table_map",
                        lambda: {"documents": "F_DOCENTETE"})
    monkeypatch.setattr(sage_db, "get_cursor", lambda: _FakeCtx(cur))

    sage_db.reset_stuck_en_cours()
    assert "IN (?)" in cur.sql
    assert "ERREUR" not in cur.params
    assert cur.params == ("EN_COURS",)

    sage_db.reset_stuck_en_cours(reset_errors=True)
    assert "IN (?, ?)" in cur.sql
    assert set(cur.params) == {"EN_COURS", "ERREUR"}


def test_concurrent_pass_ignored(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="t-connector.sync")
    calls = []
    started = threading.Event()

    def fake_locked():
        calls.append(1)
        started.set()
        time.sleep(0.2)

    monkeypatch.setattr(engine, "_auto_certify_invoices_locked", fake_locked)

    t1 = threading.Thread(target=engine.auto_certify_invoices)
    t1.start()
    assert started.wait(1)
    t2 = threading.Thread(target=engine.auto_certify_invoices)
    t2.start()
    t1.join()
    t2.join()

    assert calls == [1]
    assert any("deja en cours" in r.message for r in caplog.records)


def _setup_fake_pass(monkeypatch, invoices):
    monkeypatch.setattr(engine, "_cache", {
        **engine._cache,
        "sales_invoices": invoices,
    })
    monkeypatch.setattr(engine, "get_config", lambda: {
        "sfec": {"enabled": True, "api_key": "test"},
        "queue": {"max_concurrent": 1},
    })
    monkeypatch.setattr(engine, "is_online", lambda: True)
    monkeypatch.setattr(engine, "_startup_reset_done", True)
    monkeypatch.setattr(engine, "_build_sfec_lookup", lambda: {})


def test_duplicate_snapshot_one_certification(monkeypatch):
    inv = {
        "id": "0-6-F1", "numero": "F1", "statut_code": 2,
        "sfec_statut": "", "type_doc": "vente",
        "date_facture": "2026-01-01",
    }
    _setup_fake_pass(monkeypatch, [dict(inv), dict(inv)])

    calls = []
    monkeypatch.setattr(
        engine, "_certify_single_worker",
        lambda invoice, lookup: calls.append(invoice["id"]) or ("ok", "F1", "CERT"),
    )

    engine._auto_certify_invoices_locked()
    assert calls == ["0-6-F1"]


def test_a_surveiller_not_retried(monkeypatch):
    inv = {
        "id": "0-6-F2", "numero": "F2", "statut_code": 2,
        "sfec_statut": "A_SURVEILLER", "type_doc": "vente",
        "date_facture": "2026-01-01",
    }
    _setup_fake_pass(monkeypatch, [inv])

    calls = []
    monkeypatch.setattr(
        engine, "_certify_single_worker",
        lambda invoice, lookup: calls.append(invoice["id"]) or ("ok", "F2", "CERT"),
    )

    engine._auto_certify_invoices_locked()
    assert calls == []


def test_startup_reset_only_once(monkeypatch):
    _setup_fake_pass(monkeypatch, [])
    monkeypatch.setattr(engine, "_startup_reset_done", False)

    calls = []
    monkeypatch.setattr(engine, "reset_stuck_en_cours", lambda: calls.append(1))

    engine._auto_certify_invoices_locked()
    engine._auto_certify_invoices_locked()
    assert calls == [1]


@pytest.mark.parametrize("code", [400, 422])
def test_definitive_reject_marks_to_monitor(monkeypatch, code):
    class Reject(Exception):
        def __init__(self, status_code, message):
            super().__init__(message)
            self.status_code = status_code
            self.error_body = "bad payload"

    def raise_reject(_invoice):
        raise Reject(code, "rejet SFEC")

    monkeypatch.setattr(engine, "mark_certifying", lambda _id: None)
    monkeypatch.setattr(engine, "certify_invoice", raise_reject)
    monitors = []
    monkeypatch.setattr(
        engine, "mark_to_monitor",
        lambda invoice_id, reason="": monitors.append((invoice_id, reason)),
    )

    inv = {"id": "0-6-F3", "numero": "F3", "statut_code": 2, "sfec_statut": ""}
    result = engine._certify_single_worker(inv, {})

    assert result[0] == "error_reject"
    assert monitors and monitors[0][0] == "0-6-F3"
    assert "SFEC {}".format(code) in monitors[0][1]
    # Aucun chemin ne doit plus ecrire ERREUR pour un rejet definitif.
    assert not hasattr(engine, "mark_certification_failed")


def test_transient_errors_backoff_then_monitor(monkeypatch):
    def fail(_invoice):
        raise RuntimeError("Sage lock 80003")

    monkeypatch.setattr(engine, "mark_certifying", lambda _id: None)
    monkeypatch.setattr(engine, "certify_invoice", fail)
    monitors = []
    monkeypatch.setattr(
        engine, "mark_to_monitor",
        lambda invoice_id, reason="": monitors.append((invoice_id, reason)),
    )

    inv = {"id": "0-6-F4", "numero": "F4", "statut_code": 2, "sfec_statut": ""}

    for attempt in range(1, 5):
        result = engine._certify_single_worker(inv, {})
        assert result[0] == "error"
        if attempt < 4:
            assert not engine._should_retry("F4")
        else:
            assert engine._should_retry("F4")

    assert len(monitors) == 1
    assert monitors[0][0] == "0-6-F4"
    assert "80003" in monitors[0][1]
    assert engine._retry_attempts.get("F4") is None
