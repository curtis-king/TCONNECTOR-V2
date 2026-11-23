"""Reprise automatique des ecritures SFEC -> Sage.

Regles verifiees :
  - le rejeu des ecritures en attente ne depend pas de la reussite du pull ;
  - une erreur definitive bloque la ligne (plus de rejeu infini) ;
  - une erreur de connexion interrompt le lot au lieu de tenter 20 lignes ;
  - le point d'entree public force le rejeu.
"""
import pytest

from app.integration.sage import database as sage_db
from app.integration.sfec import pending_writes
from app.storage import db as sqlite_db
from app.sync import engine

PREFIX = "test-recovery-"


@pytest.fixture(autouse=True)
def _clean_state():
    sqlite_db.init_database()
    with sqlite_db.get_cursor() as cur:
        cur.execute("DELETE FROM sfec_pending_writes")
    yield
    with sqlite_db.get_cursor() as cur:
        cur.execute("DELETE FROM sfec_pending_writes")


def _pending_row(invoice_id):
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM sfec_pending_writes WHERE invoice_id = ?",
            (invoice_id,),
        )
        return cur.fetchone()


def test_sync_all_rejoue_meme_si_le_pull_echoue(monkeypatch):
    """Une erreur de lecture Sage ne doit plus sauter le rejeu."""
    monkeypatch.setattr(engine, "_cache", {
        **engine._cache, "last_sync_at": None,
    })
    flushes = []
    monkeypatch.setattr(engine, "_flush_pending_writes",
                        lambda force=False: flushes.append(force) or 0)

    def boom(*_args, **_kwargs):
        raise RuntimeError("Communication link failure")

    monkeypatch.setattr(engine, "fetch_sales_invoices", boom)

    assert engine.sync_all() is False
    assert flushes == [False]


def test_sync_incremental_rejoue_meme_si_le_pull_echoue(monkeypatch):
    monkeypatch.setattr(engine, "_cache", {
        **engine._cache, "last_sync_at": "2026-11-01T00:00:00Z",
    })
    flushes = []
    monkeypatch.setattr(engine, "_flush_pending_writes",
                        lambda force=False: flushes.append(force) or 0)

    def boom(*_args, **_kwargs):
        raise RuntimeError("Communication link failure")

    monkeypatch.setattr(engine, "fetch_sales_invoices", boom)

    assert engine.sync_incremental() is False
    assert flushes == [False]


def test_erreur_definitive_bloque_la_ligne(monkeypatch):
    invoice_id = PREFIX + "0-6-F1"
    pending_writes.enqueue(
        invoice_id, "CERT1", "SIG1", "QR1", "2026-11-01T10:00:00Z",
    )

    calls = []

    def permanent(**_kwargs):
        calls.append(1)
        raise sage_db.PermanentSfecWriteError("WRITE FAILED: 0 ligne")

    monitors = []
    monkeypatch.setattr(pending_writes, "write_sfec_to_invoice", permanent)
    monkeypatch.setattr(
        pending_writes, "mark_to_monitor",
        lambda invoice_id, reason="": monitors.append((invoice_id, reason)),
    )

    assert pending_writes.flush() == 0
    row = _pending_row(invoice_id)
    assert row is not None
    assert row["attempts"] == 1
    assert row["blocked"] == 1
    assert monitors and monitors[0][0] == invoice_id
    assert "0 ligne" in monitors[0][1]

    # La ligne est conservee pour l'audit mais n'est plus rejouee.
    assert pending_writes.flush() == 0
    assert len(calls) == 1
    assert _pending_row(invoice_id)["attempts"] == 1


def test_erreur_de_connexion_interrompt_le_lot(monkeypatch):
    ids = [PREFIX + "0-6-C{}".format(i) for i in range(3)]
    for invoice_id in ids:
        pending_writes.enqueue(
            invoice_id, "CERT", "SIG", "QR", "2026-11-01T10:00:00Z",
        )

    calls = []

    def offline(**_kwargs):
        calls.append(1)
        raise RuntimeError("Communication link failure")

    monkeypatch.setattr(pending_writes, "write_sfec_to_invoice", offline)

    assert pending_writes.flush() == 0
    # Une seule tentative : inutile d'epuiser le pool sur les autres lignes.
    assert len(calls) == 1
    assert _pending_row(ids[0])["attempts"] == 1
    assert _pending_row(ids[1])["attempts"] == 0


def test_point_entree_public_force_le_rejeu(monkeypatch):
    invoice_id = PREFIX + "0-6-F2"
    pending_writes.enqueue(
        invoice_id, "CERT2", "SIG2", "QR2", "2026-11-01T10:00:00Z",
    )
    monkeypatch.setattr(pending_writes, "write_sfec_to_invoice",
                        lambda **_kwargs: None)
    engine._last_flush_ts = None

    assert engine.flush_pending_writes(force=True) == 1
    assert _pending_row(invoice_id) is None
