"""Tests perf sync : full, incremental, anti-doublons (mocks actifs, 0 reseau)."""
import pytest

from .helpers import assert_under

pytestmark = pytest.mark.perf


def test_full_sync_duration(thresholds):
    import time
    from app.sync.bidirectional import full_sync

    t0 = time.perf_counter()
    full_sync()
    assert_under((time.perf_counter() - t0) * 1000,
                 thresholds["sync"]["full_sec"] * 1000, "full_sync")


def test_incremental_sync_duration(thresholds):
    import time
    from app.sync.engine import sync_incremental

    t0 = time.perf_counter()
    sync_incremental()
    assert_under((time.perf_counter() - t0) * 1000,
                 thresholds["sync"]["incremental_sec"] * 1000, "sync_incremental")


def test_auto_certify_no_duplicates(monkeypatch):
    import app.sync.engine as engine

    rows = []
    for i in range(1, 21):
        rows.append({"id": "0-6-P{}".format(i), "numero": "P-{:04d}".format(i),
                     "statut_code": 2, "sfec_statut": "", "type_doc": "vente",
                     "date_facture": "2026-10-01"})
    rows.append({"id": "0-6-REF", "numero": "REF-0001", "statut_code": 2,
                 "sfec_statut": "CERTIFIE", "type_doc": "vente", "date_facture": "2026-10-01"})
    rows.append({"id": "0-7-AV1", "numero": "AV-0001", "statut_code": 2,
                 "sfec_statut": "", "type_doc": "avoir", "reference_invoice_id": "REF-0001",
                 "reference": "", "date_facture": "2026-10-01"})
    rows.append({"id": "0-7-AV2", "numero": "AV-0002", "statut_code": 2,
                 "sfec_statut": "", "type_doc": "avoir", "reference_invoice_id": "",
                 "reference": "", "date_facture": "2026-10-01"})
    rows.append({"id": "0-6-ENC", "numero": "ENC-0001", "statut_code": 2,
                 "sfec_statut": "EN_COURS", "type_doc": "vente", "date_facture": "2026-10-01"})
    engine._cache["sales_invoices"] = rows

    seen = []

    def counting_certify(invoice, sfec_lookup=None):
        seen.append(invoice.get("id"))
        return {"certification_number": "MOCK-{}".format(invoice.get("numero")),
                "signature": "s", "qr_code": "", "certification_date": "2026-10-06T12:00:00Z"}

    monkeypatch.setattr(engine, "certify_invoice", counting_certify)
    engine.auto_certify_invoices()

    # 20 ventes + REF-0001 (CERTIFIE mais hors lookup mocke) + AV-0001 ; sans-ref et EN_COURS exclus
    assert len(seen) == 22, seen
    assert len(set(seen)) == len(seen), "doublons de certification !"
    assert "0-7-AV2" not in seen and "0-6-ENC" not in seen
