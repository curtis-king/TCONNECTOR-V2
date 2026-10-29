"""Proposition 06 : statut A_SURVEILLER, motif, relance et export.

Les 12 factures non reconstructibles doivent etre identifiables, leur motif
conserve dans SQLite, et une relance manuelle doit remettre uniquement
A_SURVEILLER a NULL. Un 422 de validation locale doit etre classe
A_SURVEILLER sans aucun appel HTTP.
"""
import pytest

from app.integration.sfec.client import SfecClient
from app.integration.sfec.endpoints import certify_invoice
from app.storage import db as sqlite_db
from app.sync import engine


def _invoice(lignes, **kw):
    inv = {
        "numero": "TEST-ANOM",
        "type_doc": "vente",
        "date_facture": "2026-10-20",
        "nom_tiers": "Client Test",
        "recipient_niu": "M0000000000000001",
        "montant_ht": sum(l["montant_ht"] for l in lignes),
        "montant_tva": sum(l["montant_tva"] for l in lignes),
        "montant_ttc": sum(l["montant_ttc"] for l in lignes),
        "lignes": lignes,
    }
    inv.update(kw)
    return inv


def _line(ht, tva, ttc, taux=18):
    return {
        "designation": "Produit",
        "quantite": 1,
        "prix_unitaire": ht,
        "montant_ht": ht,
        "taux_tva": taux,
        "montant_tva": tva,
        "montant_ttc": ttc,
    }


def test_export_anomalies_ne_garde_que_ttc_non_reconstructibles():
    from scripts.export_anomalies_sfec import build_rows

    invoices = [
        # A : 18 % + centimes, reconstructible.
        _invoice([_line(10000, 1800, 11890)], numero="OK-CENT"),
        # C : 18 % + 3 %, reconstructible.
        _invoice([_line(10000, 1800, 12190)], numero="OK-TAXE"),
        # B : TTC = HT, aucune TVA.
        _invoice([_line(5000, 0, 5000, taux=0)], numero="ANOM-TTC-HT"),
        # D : 18 % + centimes annonces, mais TTC Sage = HT x 1,1825.
        _invoice([_line(150000, 27000, 177375)], numero="ANOM-RATIO"),
    ]

    rows = build_rows(invoices)
    by_numero = {row["numero"]: row for row in rows}

    # Une facture exoneree (TTC = HT, taux 0 %) est valide : elle ne doit
    # plus apparaitre dans les anomalies.
    assert set(by_numero) == {"ANOM-RATIO"}
    assert "taxe non ventilable" in by_numero["ANOM-RATIO"]["motif"]
    assert by_numero["ANOM-RATIO"]["ecart"] == pytest.approx(975.0, abs=0.01)


def test_validation_422_avant_tout_appel_http(monkeypatch):
    calls = []

    def fake_certify(self, payload):
        calls.append(payload)
        raise AssertionError("aucun appel HTTP ne doit partir en 422")

    monkeypatch.setattr(SfecClient, "certify", fake_certify)
    inv = _invoice([_line(150000, 27000, 177375)], numero="ANOM-422")

    with pytest.raises(Exception) as exc:
        certify_invoice(inv)

    assert getattr(exc.value, "status_code", None) == 422
    assert calls == []


def test_certify_single_422_marque_a_surveiller(monkeypatch):
    marked = []
    monkeypatch.setitem(engine._cache, "sales_invoices", [{
        "id": "0-6-ANOM-422B", "numero": "ANOM-422B", "type_doc": "vente",
        "statut_code": 2, "montant_ht": 5000, "montant_tva": 0,
        "montant_ttc": 5000, "lignes": [],
    }])
    monkeypatch.setattr(engine, "get_config",
                        lambda: {"sfec": {"enabled": True, "api_key": "test"}})
    monkeypatch.setattr(engine, "is_online", lambda: True)
    monkeypatch.setattr(engine, "mark_certifying", lambda invoice_id: None)
    monkeypatch.setattr(engine, "mark_to_monitor",
                        lambda invoice_id, reason="": marked.append((invoice_id, reason)))

    class FakeClient:
        def verify_by_invoice_number(self, numero):
            return None

    monkeypatch.setattr(engine, "SfecClient", FakeClient)

    def reject(invoice):
        exc = Exception("Validation SFEC echouee: TTC = HT")
        exc.status_code = 422
        raise exc

    monkeypatch.setattr(engine, "certify_invoice", reject)
    monkeypatch.setattr(engine, "_reset_retry", lambda numero: None)

    result = engine.certify_single("0-6-ANOM-422B")

    assert result["success"] is False
    assert marked and marked[0][0] == "0-6-ANOM-422B"
    assert "422" in marked[0][1]


def test_mark_to_monitor_conserve_le_motif_sans_sage(monkeypatch):
    from app.integration.sage import database as sage_db

    # Ce test n'utilise pas la fixture app_instance : s'assurer que le
    # schema SQLite (dont sfec_anomalies) existe.
    sqlite_db.init_database()
    numero = "ANOM-MOTIF-TEST"
    sqlite_db.clear_sfec_anomaly(numero)
    monkeypatch.setattr(sage_db, "get_table_map", lambda: {"documents": ""})

    sage_db.mark_to_monitor("0-6-" + numero, "TTC = HT : exoneration a confirmer")

    anomaly = sqlite_db.get_sfec_anomaly(numero)
    assert anomaly is not None
    assert "exoneration" in anomaly["motif"]
    sqlite_db.clear_sfec_anomaly(numero)


def test_page_pending_affiche_motif_et_bouton_relancer(client, monkeypatch):
    from app.web.routes import dashboard

    numero = "ANOM-PAGE-TEST"
    sqlite_db.record_sfec_anomaly(numero, "TTC = HT : exoneration a confirmer")
    cache = {
        "sales_invoices": [{
            "id": "0-6-" + numero, "numero": numero, "statut_code": 2,
            "sfec_statut": "A_SURVEILLER", "type_doc": "vente",
            "montant_ttc": 5000, "date_facture": "2026-10-20",
            "nom_tiers": "Client Test",
        }],
        "sfec_invoices": [],
    }
    monkeypatch.setattr(dashboard, "get_cache", lambda: cache)
    try:
        resp = client.get("/pending")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert numero in html
        assert "TTC = HT" in html
        assert "Relancer" in html
    finally:
        sqlite_db.clear_sfec_anomaly(numero)


def test_relance_remet_a_surveiller_a_zero_pour_une_piece(client, monkeypatch):
    from app.web.routes import dashboard

    numero = "ANOM-RELANCE-TEST"
    calls = []
    cache = {
        "sales_invoices": [{
            "id": "0-6-" + numero, "numero": numero, "statut_code": 2,
            "sfec_statut": "A_SURVEILLER", "type_doc": "vente",
            "montant_ht": 5000, "montant_tva": 0, "montant_ttc": 5000,
            "lignes": [],
        }],
        "sfec_invoices": [],
    }
    sqlite_db.record_sfec_anomaly(numero, "TTC = HT")
    monkeypatch.setattr(dashboard, "reset_sfec_monitor",
                        lambda piece: calls.append(piece) or 1)
    monkeypatch.setattr(dashboard, "certify_single",
                        lambda invoice_id: {"success": True,
                                            "certification_number": "CERT-TEST"})
    monkeypatch.setattr(dashboard, "get_cache", lambda: cache)
    token = client.get("/api/csrf").get_json()["token"]
    try:
        resp = client.post("/api/sfec/relancer", json={"numero": numero},
                           headers={"X-CSRF-Token": token})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert calls == [numero]
    finally:
        sqlite_db.clear_sfec_anomaly(numero)
