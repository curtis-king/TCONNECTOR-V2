"""Non-regressions SFEC : dates, paiements, validation et avoirs sur compte."""
import pytest

from app.integration.sfec import client as sfec_client
from app.integration.sfec import endpoints as sfec_endpoints
from app.integration.sfec.endpoints import (
    db_invoice_to_sfec,
    map_payment_method,
    validate_sfec_payload,
)
from app.sync import engine


def _line(**kw):
    line = {
        "designation": "Produit",
        "quantite": 1,
        "prix_unitaire": 1000,
        "montant_ht": 1000,
        "taux_tva": 18,
        "montant_tva": 180,
        "montant_ttc": 1189,
    }
    line.update(kw)
    return line


def _invoice(**kw):
    inv = {
        "numero": "FA-TEST-1",
        "type_doc": "vente",
        "reference": "",
        "nom_tiers": "Client Test",
        "recipient_niu": "M0000000000000001",
        "montant_ht": 1000,
        "montant_tva": 180,
        "montant_ttc": 1189,
        "montant_restant": 1189,
        "lignes": [_line()],
    }
    inv.update(kw)
    return inv


def test_dates_date_only_sont_conservees_en_rfc3339():
    payload = db_invoice_to_sfec(_invoice(
        date_echeance="2026-12-31",
        payment_date="31/12/2026",
    ))

    assert payload["invoice_due_date"] == "2026-12-31T00:00:00Z"
    assert payload["payment_date"] == "2026-12-31T00:00:00Z"


def test_date_absente_ou_1900_est_ignoree():
    payload = db_invoice_to_sfec(_invoice(date_echeance="1900-01-01"))

    assert "invoice_due_date" not in payload


def test_paiement_cheque_et_virement():
    assert map_payment_method("cheque") == "cheque"
    assert map_payment_method("chèque") == "cheque"
    assert map_payment_method("virement") == "bank_transfer"
    assert map_payment_method("especes") == "cash"
    assert map_payment_method("inconnu") is None


def test_paiement_numerique_configure(monkeypatch):
    monkeypatch.setattr(
        sfec_endpoints, "_sfec_config",
        lambda: {"payment_method_map": {"1": "cash"}},
    )
    assert map_payment_method("1") == "cash"


def test_paiement_inconnu_est_rejete():
    payload = db_invoice_to_sfec(_invoice(payment_method="inconnu"))

    assert payload["payment_method"] == "inconnu"
    assert any("payment_method inconnu" in e
               for e in validate_sfec_payload(payload))


def test_ligne_quantite_et_prix_nuls_rejetes():
    payload = db_invoice_to_sfec(_invoice(lignes=[
        _line(quantite=0),
        _line(prix_unitaire=0),
    ]))

    errors = validate_sfec_payload(payload)
    assert any("quantity doit etre >= 1" in e for e in errors)
    assert any("unit_price doit etre >= 1" in e for e in errors)


def test_taux_tva_20_rejete():
    payload = db_invoice_to_sfec(_invoice(lignes=[_line(taux_tva=20)]))

    assert any("tax_rate invalide" in e for e in validate_sfec_payload(payload))


def test_total_tva_doit_egal_t_plus_r():
    payload = db_invoice_to_sfec(_invoice())
    payload["total_tax_amount"] = 999

    assert any("total_tax_t_amount + total_tax_r_amount" in e
               for e in validate_sfec_payload(payload))


def test_credit_note_on_account_sans_parent():
    payload = db_invoice_to_sfec(_invoice(
        numero="AV-COMPTE-1",
        type_doc="avoir",
        credit_note_on_account=True,
        reference_invoice_id="",
        reference="",
        montant_ht=-1000,
        montant_tva=-180,
        montant_ttc=-1189,
        montant_restant=-1189,
        lignes=[_line(
            quantite=-1, prix_unitaire=1000, montant_ht=-1000,
            montant_tva=-180, montant_ttc=-1189,
        )],
    ))

    assert payload["invoice_type"] == "creditNoteOnAccount"
    assert payload["reference_invoice_id"] == "AV-COMPTE-1"
    assert "external_credit_note_number" not in payload
    assert "original_sfec_invoice_number" not in payload
    assert validate_sfec_payload(payload) == []


def test_credit_note_classique_exige_les_champs_parents():
    payload = db_invoice_to_sfec(_invoice(
        numero="AV-1",
        type_doc="avoir",
        reference_invoice_id="FA-PARENT",
        montant_ht=-1000,
        montant_tva=-180,
        montant_ttc=-1189,
        montant_restant=-1189,
        lignes=[_line(
            quantite=-1, prix_unitaire=1000, montant_ht=-1000,
            montant_tva=-180, montant_ttc=-1189,
        )],
    ))

    errors = validate_sfec_payload(payload)
    assert any("external_credit_note_number requis" in e for e in errors)
    assert any("original_sfec_invoice_number requis" in e for e in errors)


def test_sqlite_payload_utilise_le_numero_pas_l_id_sqlite():
    from app.integration.sfec.endpoints import sqlite_invoice_to_sfec

    payload = sqlite_invoice_to_sfec(_invoice(
        id=42,
        numero="26FAZ07297",
        source="sage",
    ))

    assert payload["invoice_id"] == "26FAZ07297"


def test_get_invoice_encode_le_numero(monkeypatch):
    client = sfec_client.SfecClient.__new__(sfec_client.SfecClient)
    calls = []
    monkeypatch.setattr(
        client, "_request",
        lambda method, path: calls.append((method, path)) or {"ok": True},
    )

    client.get_invoice("FA/2026 001")

    assert calls == [("GET", "/api/v1/invoices/FA%2F2026%20001")]


def test_verify_certification_lit_les_bons_champs(monkeypatch):
    client = sfec_client.SfecClient.__new__(sfec_client.SfecClient)
    monkeypatch.setattr(
        client, "get_invoice",
        lambda _id: {"certification_short_signature": "ABC123"},
    )

    assert client.verify_certification("FA-1") is True


def test_verify_certification_faux_si_champ_absent(monkeypatch):
    client = sfec_client.SfecClient.__new__(sfec_client.SfecClient)
    monkeypatch.setattr(client, "get_invoice", lambda _id: {"invoice_number": "FA-1"})

    assert client.verify_certification("FA-1") is False


def test_lookup_utilise_page_size_20_et_total_pages(monkeypatch):
    calls = []

    class FakeClient:
        def list_invoices(self, page=1, page_size=20, **_kw):
            calls.append((page, page_size))
            if page == 1:
                return {
                    "invoices": [{"seller_invoice_number": "FA-1",
                                  "invoice_number": "SFEC-1"}],
                    "totalPages": 2,
                }
            return {
                "invoices": [{"seller_invoice_number": "FA-2",
                              "invoice_number": "SFEC-2"}],
                "totalPages": 2,
            }

    monkeypatch.setattr(engine, "SfecClient", FakeClient)
    monkeypatch.setattr(engine, "is_online", lambda: True)
    monkeypatch.setattr(engine, "get_config", lambda: {"sfec": {}})
    monkeypatch.setattr(engine, "_sfec_lookup_cache", None)
    monkeypatch.setattr(engine, "_sfec_lookup_ts", 0)

    lookup = engine._build_sfec_lookup()

    assert calls == [(1, 20), (2, 20)]
    assert lookup["FA-1"]["invoice_number"] == "SFEC-1"
    assert lookup["FA-2"]["invoice_number"] == "SFEC-2"
    assert lookup["SFEC-1"]["invoice_number"] == "SFEC-1"
    assert lookup["SFEC-2"]["invoice_number"] == "SFEC-2"


def test_overcredit_400_est_classe_metier():
    exc = Exception("bad request")
    exc.status_code = 400
    exc.error_json = {
        "error": "credit note exceeds invoice",
        "already_credited": 500,
        "invoice_net": 1189,
        "remaining": 689,
    }

    assert engine.overcredit_details(exc) == exc.error_json
    assert "deja credite 500" in engine.overcredit_message(exc)


def test_409_on_account_reference_est_distingue():
    exc = Exception("conflict")
    exc.status_code = 409
    exc.error_json = {
        "error": "credit note on account reference already used",
        "reference": "AV-COMPTE-1",
    }

    assert engine.on_account_reference_conflict(exc) == "AV-COMPTE-1"
