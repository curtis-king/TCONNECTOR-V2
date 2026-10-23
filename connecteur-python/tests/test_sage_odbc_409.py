"""Proposition 08 : ODBC Sage, 409 et ecritures differees.

Regles verifiees :
  - les erreurs ODBC transitoires sont retentees (1/3 s), pas les 42000 ;
  - une certification SFEC non ecrite dans Sage est persistee puis rejouee ;
  - un 409 sans GET exploitable ne marque JAMAIS DEJA_CERTIFIE ;
  - le numero SFEC (`invoice_number`) est persiste pour les avoirs ;
  - le GET de verification SFEC retente les timeouts/5xx mais pas les 404.
"""
import queue

import pytest

from app.integration.sage import database as sage_db
from app.integration.sfec import client as sfec_client
from app.integration.sfec import endpoints as sfec_endpoints
from app.integration.sfec import pending_writes
from app.storage import db as sqlite_db
from app.sync import engine

PREFIX = "test08-"


@pytest.fixture(autouse=True)
def _clean_state():
    sqlite_db.init_database()
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "DELETE FROM sfec_pending_writes WHERE invoice_id LIKE ?",
            (PREFIX + "%",),
        )
    engine._retry_attempts.clear()
    engine._retry_after.clear()
    engine._logged_errors.clear()
    yield
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "DELETE FROM sfec_pending_writes WHERE invoice_id LIKE ?",
            (PREFIX + "%",),
        )
    engine._retry_attempts.clear()
    engine._retry_after.clear()
    engine._logged_errors.clear()


# ── Pool ODBC ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("message,expected", [
    ("[HYT00] Login timeout expired", True),
    ("[08001] Communication link failure", True),
    ("[42000] Cet element est en cours d'utilisation.", True),
    ("80003 - lock", True),
    ("Configuration base de donnees incomplete", True),
    ("[42000] Invalid column name", False),
    ("permission denied", False),
])
def test_is_transient(message, expected):
    assert sage_db._is_transient(Exception(message)) is expected


def test_connect_with_retry_deux_echecs_puis_succes(monkeypatch):
    sentinel = object()
    calls = []
    sleeps = []

    def fake_connect():
        calls.append(1)
        if len(calls) < 3:
            raise Exception("Login timeout expired")
        return sentinel

    monkeypatch.setattr(sage_db, "_connect", fake_connect)
    monkeypatch.setattr(sage_db.time, "sleep", lambda delay: sleeps.append(delay))

    assert sage_db._connect_with_retry() is sentinel
    assert len(calls) == 3
    assert sleeps == [1, 3]


def test_connect_with_retry_ne_retente_pas_42000(monkeypatch):
    calls = []
    sleeps = []

    def fake_connect():
        calls.append(1)
        raise Exception("[42000] Invalid column name")

    monkeypatch.setattr(sage_db, "_connect", fake_connect)
    monkeypatch.setattr(sage_db.time, "sleep", lambda delay: sleeps.append(delay))

    with pytest.raises(Exception, match="42000"):
        sage_db._connect_with_retry()
    assert len(calls) == 1
    assert sleeps == []


def test_get_pool_utilise_la_connexion_avec_retry(monkeypatch):
    sentinel = object()
    calls = []
    monkeypatch.setattr(sage_db, "_pool", None)
    monkeypatch.setattr(sage_db, "get_db_config",
                        lambda: {"server": "srv", "database": "db"})
    monkeypatch.setattr(sage_db, "_connect_with_retry",
                        lambda: calls.append(1) or sentinel)

    assert sage_db.get_pool() is sentinel
    assert calls == [1]


def test_checkout_utilise_la_connexion_avec_retry(monkeypatch):
    sentinel = object()
    fake_queue = queue.Queue()
    calls = []
    monkeypatch.setattr(sage_db, "_pool_queue", fake_queue)
    monkeypatch.setattr(sage_db, "_pool_size", 0)
    monkeypatch.setattr(sage_db, "_connect_with_retry",
                        lambda: calls.append(1) or sentinel)

    assert sage_db._checkout(2) is sentinel
    assert calls == [1]
    assert sage_db._pool_size == 1


# ── File d'ecriture differee ────────────────────────────────────────────────
def _pending_row(invoice_id):
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM sfec_pending_writes WHERE invoice_id = ?",
            (invoice_id,),
        )
        return cur.fetchone()


def test_enqueue_puis_flush_rejoue_apres_deux_echecs(monkeypatch):
    invoice_id = PREFIX + "0-6-F1"
    attempts = []
    done = []

    def fake_write(**kwargs):
        attempts.append(kwargs["invoice_id"])
        if len(attempts) < 3:
            raise RuntimeError("Cet element est en cours d'utilisation")

    pending_writes.enqueue(
        invoice_id, "CERT1", "SIG1", "QR1", "2026-10-21T10:00:00Z",
        "lock",
    )
    monkeypatch.setattr(pending_writes, "write_sfec_to_invoice", fake_write)

    assert pending_writes.flush() == 0
    assert _pending_row(invoice_id)["attempts"] == 1
    assert pending_writes.flush() == 0
    assert _pending_row(invoice_id)["attempts"] == 2

    assert pending_writes.flush(on_success=lambda i, c: done.append((i, c))) == 1
    assert attempts == [invoice_id] * 3
    assert done == [(invoice_id, "CERT1")]
    assert _pending_row(invoice_id) is None


def test_flush_abandonne_apres_dix_echecs_et_garde_la_ligne(monkeypatch):
    invoice_id = PREFIX + "0-6-F2"
    pending_writes.enqueue(
        invoice_id, "CERT2", "SIG2", "QR2", "2026-10-21T10:00:00Z",
    )
    monkeypatch.setattr(
        pending_writes, "write_sfec_to_invoice",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("lock 80003")),
    )
    monitors = []
    monkeypatch.setattr(
        pending_writes, "mark_to_monitor",
        lambda invoice_id, reason="": monitors.append((invoice_id, reason)),
    )

    for _ in range(10):
        assert pending_writes.flush() == 0

    row = _pending_row(invoice_id)
    assert row is not None
    assert row["attempts"] == 10
    assert len(monitors) == 1
    assert "10 tentatives" in monitors[0][1]

    # La ligne reste pour l'audit, mais elle n'est plus rejouee sans fin.
    assert pending_writes.flush() == 0
    assert len(monitors) == 1


def test_flush_callback_met_a_jour_le_cache(monkeypatch):
    invoice_id = PREFIX + "0-6-F3"
    cache = {"sales_invoices": [
        {"id": invoice_id, "numero": "F3", "sfec_statut": "EN_COURS"},
    ]}
    monkeypatch.setattr(engine, "_cache", cache)
    engine._pending_write_done(invoice_id, "CERT3")

    assert cache["sales_invoices"][0]["sfec_statut"] == "CERTIFIE"
    assert cache["sales_invoices"][0]["sfec_num_certif"] == "CERT3"


# ── 409 : jamais de marquage sans preuve ────────────────────────────────────
def _sfec_inv(**kw):
    inv = {
        "invoice_number": "SFEC-F1",
        "certification_short_signature": "CERT-F1",
        "certification_signature": "SIG-F1",
        "certification_qr_code": "QR-F1",
        "certification_date": "2026-10-21T10:00:00Z",
    }
    inv.update(kw)
    return inv


def test_409_sans_get_ne_marque_pas_deja_certifie(monkeypatch):
    class FakeClient:
        def verify_by_invoice_number(self, _numero):
            return None

    marks = []
    enqueues = []
    monkeypatch.setattr(engine, "SfecClient", lambda: FakeClient())
    monkeypatch.setattr(engine, "mark_certified",
                        lambda *a, **k: marks.append((a, k)))
    monkeypatch.setattr(engine, "enqueue",
                        lambda *a, **k: enqueues.append((a, k)))
    monkeypatch.setattr(engine, "_invalidate_sfec_lookup",
                        lambda: marks.append("invalidate"))

    invoice = {"id": PREFIX + "0-6-F4", "numero": "F4"}
    assert engine._handle_already_certified(invoice, {}) is False
    assert marks == ["invalidate"]
    assert enqueues == []
    assert invoice.get("sfec_statut") is None


def test_409_get_exploitable_ecrit_et_persiste_le_numero(monkeypatch):
    written = []
    monkeypatch.setattr(
        engine, "write_sfec_to_invoice",
        lambda **kwargs: written.append(kwargs),
    )
    monkeypatch.setattr(engine, "record_certified_invoice", lambda *a: True)

    invoice = {"id": PREFIX + "0-6-F5", "numero": "F5"}
    assert engine._handle_already_certified(invoice, {"F5": _sfec_inv()}) is True
    assert written and written[0]["certification_number"] == "CERT-F1"
    assert invoice["sfec_statut"] == "DEJA_CERTIFIE"
    assert invoice["sfec_num_certif"] == "CERT-F1"
    assert invoice["sfec_invoice_number"] == "SFEC-F1"


def test_409_ecriture_sage_echouee_met_en_file(monkeypatch):
    enqueued = []
    monkeypatch.setattr(
        engine, "write_sfec_to_invoice",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("Cet element est en cours d'utilisation")),
    )
    monkeypatch.setattr(engine, "record_certified_invoice", lambda *a: True)
    monkeypatch.setattr(
        engine, "enqueue",
        lambda invoice_id, cert, sig, qr, date, error="":
            enqueued.append((invoice_id, cert, sig, qr, date, error)),
    )

    invoice = {"id": PREFIX + "0-6-F6", "numero": "F6"}
    assert engine._handle_already_certified(invoice, {"F6": _sfec_inv()}) is True
    assert len(enqueued) == 1
    assert enqueued[0][0] == invoice["id"]
    assert enqueued[0][1] == "CERT-F1"
    assert invoice["sfec_statut"] == "EN_COURS"


def test_worker_409_sans_get_ne_marque_pas(monkeypatch):
    class Reject(Exception):
        status_code = 409
        error_body = "already exists"

    class FakeClient:
        def verify_by_invoice_number(self, _numero):
            return None

    marks = []
    monkeypatch.setattr(engine, "certify_invoice",
                        lambda _invoice: (_ for _ in ()).throw(Reject("409")))
    monkeypatch.setattr(engine, "mark_certifying", lambda _id: None)
    monkeypatch.setattr(engine, "SfecClient", lambda: FakeClient())
    monkeypatch.setattr(engine, "mark_certified",
                        lambda *a, **k: marks.append((a, k)))

    invoice = {"id": PREFIX + "0-6-F7", "numero": "F7"}
    result = engine._certify_single_worker(invoice, {})
    assert result[0] == "skip"
    assert marks == []


def test_certify_single_409_sans_get_ne_marque_pas(monkeypatch):
    class FakeClient:
        def verify_by_invoice_number(self, _numero):
            return None

    invoice = {
        "id": PREFIX + "0-6-F8", "numero": "F8", "type_doc": "vente",
        "statut_code": 2, "montant_ttc": 1189, "lignes": [],
    }
    monkeypatch.setitem(engine._cache, "sales_invoices", [invoice])
    monkeypatch.setattr(engine, "get_config",
                        lambda: {"sfec": {"enabled": True, "api_key": "test"}})
    monkeypatch.setattr(engine, "is_online", lambda: True)
    monkeypatch.setattr(engine, "mark_certifying", lambda _id: None)
    monkeypatch.setattr(engine, "SfecClient", lambda: FakeClient())
    marks = []
    monkeypatch.setattr(engine, "mark_certified",
                        lambda *a, **k: marks.append((a, k)))

    class Reject(Exception):
        status_code = 409
        error_body = "already exists"

    monkeypatch.setattr(engine, "certify_invoice",
                        lambda _invoice: (_ for _ in ()).throw(Reject("409")))

    result = engine.certify_single(invoice["id"])
    assert result["success"] is False
    assert result.get("pending") is True
    assert marks == []


def test_certification_fraiche_ecriture_sage_echouee_met_en_file(monkeypatch):
    enqueued = []
    monkeypatch.setattr(engine, "mark_certifying", lambda _id: None)
    monkeypatch.setattr(engine, "certify_invoice", lambda _invoice: {
        "invoice_number": "SFEC-F9",
        "certification_short_signature": "CERT-F9",
        "certification_signature": "SIG-F9",
        "certification_qr_code": "QR-F9",
        "certification_date": "2026-10-21T10:00:00Z",
    })
    monkeypatch.setattr(
        engine, "write_sfec_to_invoice",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("lock 80003")),
    )
    monkeypatch.setattr(
        engine, "enqueue",
        lambda invoice_id, cert, sig, qr, date, error="":
            enqueued.append((invoice_id, cert, sig, qr, date, error)),
    )

    invoice = {"id": PREFIX + "0-6-F9", "numero": "F9"}
    result = engine._certify_single_worker(invoice, {})
    assert result[0] == "queued"
    assert len(enqueued) == 1
    assert enqueued[0][1] == "CERT-F9"
    assert invoice["sfec_statut"] == "EN_COURS"


def _setup_pass(monkeypatch, invoices):
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
    monkeypatch.setattr(engine, "fetch_sfec_invoice_links", lambda: {})
    monkeypatch.setattr(engine, "_flush_pending_writes", lambda: 0)


def test_deja_certifie_sans_numero_est_rejoue(monkeypatch):
    invoice = {
        "id": PREFIX + "0-6-F15", "numero": "F15", "statut_code": 2,
        "sfec_statut": "DEJA_CERTIFIE", "sfec_num_certif": "",
        "type_doc": "vente", "montant_ttc": 1189,
        "date_facture": "2026-10-21",
    }
    _setup_pass(monkeypatch, [invoice])
    calls = []
    monkeypatch.setattr(
        engine, "_certify_single_worker",
        lambda inv, lookup: calls.append(inv["id"]) or ("skip", inv["numero"]),
    )

    engine._auto_certify_invoices_locked()
    assert calls == [invoice["id"]]


def test_deja_certifie_avec_numero_n_est_pas_rejoue(monkeypatch):
    invoice = {
        "id": PREFIX + "0-6-F16", "numero": "F16", "statut_code": 2,
        "sfec_statut": "DEJA_CERTIFIE", "sfec_num_certif": "CERT-F16",
        "type_doc": "vente", "montant_ttc": 1189,
        "date_facture": "2026-10-21",
    }
    _setup_pass(monkeypatch, [invoice])
    calls = []
    monkeypatch.setattr(
        engine, "_certify_single_worker",
        lambda inv, lookup: calls.append(inv["id"]) or ("ok", inv["numero"]),
    )

    engine._auto_certify_invoices_locked()
    assert calls == []


def test_record_certified_invoice_persiste_invoice_number(monkeypatch, tmp_path):
    target = tmp_path / "sfec-certified.json"
    monkeypatch.setattr(sfec_endpoints, "CERTIFIED_FILE", str(target))

    assert sfec_endpoints.record_certified_invoice(
        "0-6-PARENT", _sfec_inv()) is True
    saved = sfec_endpoints.get_certified_by_sage_id("0-6-PARENT")
    assert saved["invoice_number"] == "SFEC-F1"
    assert saved["certification_number"] == "CERT-F1"

    # Un deuxieme GET ne cree pas de doublon et met a jour les champs.
    assert sfec_endpoints.record_certified_invoice(
        "0-6-PARENT", _sfec_inv(certification_short_signature="CERT-F1B")) is True
    all_saved = sfec_endpoints.get_all_certified()
    assert len(all_saved) == 1
    assert all_saved[0]["certification_number"] == "CERT-F1B"


# ── Retry du GET SFEC ───────────────────────────────────────────────────────
class _HttpError(Exception):
    def __init__(self, status_code, message="erreur"):
        super().__init__(message)
        self.status_code = status_code


def test_verify_by_invoice_number_retente_5xx(monkeypatch):
    client = sfec_client.SfecClient.__new__(sfec_client.SfecClient)
    calls = []
    sleeps = []
    expected = {"invoice_number": "F10"}

    def fake_list(page=1, page_size=500):
        calls.append((page, page_size))
        if len(calls) < 3:
            raise _HttpError(503, "unavailable")
        return {"invoices": [expected], "total": 1}

    monkeypatch.setattr(client, "list_invoices", fake_list)
    monkeypatch.setattr(sfec_client.time, "sleep",
                        lambda delay: sleeps.append(delay))

    assert client.verify_by_invoice_number("F10") == expected
    assert len(calls) == 3
    assert sleeps == [2, 5]


def test_verify_by_invoice_number_404_retour_immediat(monkeypatch):
    client = sfec_client.SfecClient.__new__(sfec_client.SfecClient)
    calls = []
    sleeps = []

    def fake_list(page=1, page_size=500):
        calls.append(1)
        raise _HttpError(404, "not found")

    monkeypatch.setattr(client, "list_invoices", fake_list)
    monkeypatch.setattr(sfec_client.time, "sleep",
                        lambda delay: sleeps.append(delay))

    assert client.verify_by_invoice_number("F11") is None
    assert len(calls) == 1
    assert sleeps == []


def test_verify_by_invoice_number_epuise_puis_none(monkeypatch):
    client = sfec_client.SfecClient.__new__(sfec_client.SfecClient)
    calls = []
    sleeps = []

    def fake_list(page=1, page_size=500):
        calls.append(1)
        raise sfec_client.NetworkOfflineError("offline")

    monkeypatch.setattr(client, "list_invoices", fake_list)
    monkeypatch.setattr(sfec_client.time, "sleep",
                        lambda delay: sleeps.append(delay))

    assert client.verify_by_invoice_number("F12") is None
    assert len(calls) == 3
    assert sleeps == [2, 5]


def test_verify_by_invoice_number_400_non_retente(monkeypatch):
    client = sfec_client.SfecClient.__new__(sfec_client.SfecClient)
    calls = []
    sleeps = []

    def fake_list(page=1, page_size=500):
        calls.append(1)
        raise _HttpError(400, "bad request")

    monkeypatch.setattr(client, "list_invoices", fake_list)
    monkeypatch.setattr(sfec_client.time, "sleep",
                        lambda delay: sleeps.append(delay))

    assert client.verify_by_invoice_number("F13") is None
    assert len(calls) == 1
    assert sleeps == []


def test_invalidate_sfec_lookup(monkeypatch):
    monkeypatch.setattr(engine, "_sfec_lookup_cache", {"F14": {}})
    monkeypatch.setattr(engine, "_sfec_lookup_ts", 123)
    engine._invalidate_sfec_lookup()
    assert engine._sfec_lookup_cache is None
    assert engine._sfec_lookup_ts == 0
