"""Hydratation de la reponse POST SFEC apres certification.

Certains environnements renvoient dans `identifier` la signature courte
(`certification_short_signature`) au lieu de l'UUID de la facture. Le GET sur
cette signature courte renvoie 404 ; le GET par numero de facture fonctionne.
Le connecteur doit donc interroger le numero en premier et conserver l'UUID.
"""
import pytest

from app.integration.sfec import endpoints as sfec_endpoints
from app.storage import db as sqlite_db


SHORT_SIG = "73260A78CEB3CD200E40"
SFEC_UUID = "3b9e0a6d-6808-45bf-a28f-b71bb3cf1fe3"


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
        "id": 123,
        "numero": "TEST-HYDRATE-1",
        "type_doc": "vente",
        "reference": "",
        "nom_tiers": "Client Test",
        "tiers_code": "CLI-001",
        "tiers_nom": "Client Test",
        "tiers_type": "individual",
        "tiers_niu": "",
        "recipient_type_raw": "individual",
        "recipient_niu": "",
        "montant_ht": 1000,
        "montant_tva": 180,
        "montant_ttc": 1189,
        "montant_restant": 1189,
        "payment_method": "cash",
        "devise": "XAF",
        "lignes": [_line()],
    }
    inv.update(kw)
    return inv


class _FakeClient:
    def __init__(self, by_number=True):
        self.by_number = by_number
        self.calls = []

    def certify(self, payload):
        self.calls.append(("certify", payload["invoice_id"]))
        return {
            "identifier": SHORT_SIG,
            "invoice_number": payload["invoice_id"],
            "certification_date": "2026-10-29T05:50:21Z",
        }

    def get_invoice(self, key):
        self.calls.append(("get", key))
        if self.by_number and key == "TEST-HYDRATE-1":
            return {
                "id": SFEC_UUID,
                "invoice_number": key,
                "invoice_status": "certified",
                "certification_status": "certified",
                "certification_short_signature": SHORT_SIG,
                "certification_signature": "SIGNATURE-COMPLETE",
                "certification_date": "2026-10-29T05:50:21.589Z",
            }
        if not self.by_number and key == SHORT_SIG:
            return {
                "id": SFEC_UUID,
                "invoice_number": "TEST-HYDRATE-1",
                "certification_short_signature": SHORT_SIG,
                "certification_signature": "SIGNATURE-COMPLETE",
                "certification_date": "2026-10-29T05:50:21.589Z",
            }
        raise Exception("SFEC API error 404: invoice not found")


def test_hydratation_par_numero_avant_signature_courte(monkeypatch):
    client = _FakeClient(by_number=True)

    result, cert_number, sig, qr, cert_date, sfec_id = (
        sfec_endpoints._hydrate_certification_result(
            client, "TEST-HYDRATE-1",
            {"identifier": SHORT_SIG, "invoice_number": "TEST-HYDRATE-1"},
            attempts=1,
        )
    )

    assert cert_number == SHORT_SIG
    assert sfec_id == SFEC_UUID
    assert result["identifier"] == SFEC_UUID
    assert result["sfec_id"] == SFEC_UUID
    assert sig == "SIGNATURE-COMPLETE"
    assert cert_date == "2026-10-29T05:50:21.589Z"
    assert client.calls == [
        ("get", "TEST-HYDRATE-1"),
    ]


def test_hydratation_repli_sur_identifiant_si_numero_indisponible():
    client = _FakeClient(by_number=False)

    _result, cert_number, _sig, _qr, _date, sfec_id = (
        sfec_endpoints._hydrate_certification_result(
            client, "TEST-HYDRATE-1",
            {"identifier": SHORT_SIG, "invoice_number": "TEST-HYDRATE-1"},
            attempts=1,
        )
    )

    assert cert_number == SHORT_SIG
    assert sfec_id == SFEC_UUID
    assert client.calls == [
        ("get", "TEST-HYDRATE-1"),
        ("get", SHORT_SIG),
    ]


def test_certify_sqlite_invoice_conserve_l_uuid_et_la_signature(monkeypatch):
    client = _FakeClient(by_number=True)
    updates = []

    monkeypatch.setattr(
        sfec_endpoints, "SfecClient", lambda: client)
    monkeypatch.setattr(
        sqlite_db, "update_invoice_sfec",
        lambda invoice_id, **kwargs: updates.append((invoice_id, kwargs)),
    )

    result = sfec_endpoints.certify_sqlite_invoice(_invoice())

    assert result["success"] is True
    assert result["identifier"] == SFEC_UUID
    assert result["certification_number"] == SHORT_SIG
    assert result["signature"] == "SIGNATURE-COMPLETE"
    assert updates
    assert updates[-1][0] == 123
    assert updates[-1][1]["sfec_id"] == SFEC_UUID
    assert updates[-1][1]["statut"] == "CERTIFIE"
    assert client.calls[0] == ("certify", "TEST-HYDRATE-1")
    assert ("get", SHORT_SIG) not in client.calls


def test_certify_sqlite_invoice_marque_en_cours_si_detail_absent(monkeypatch):
    class _NoDetailClient:
        def certify(self, payload):
            return {
                "identifier": SHORT_SIG,
                "invoice_number": payload["invoice_id"],
            }

        def get_invoice(self, _key):
            raise Exception("SFEC API error 404: invoice not found")

    updates = []
    monkeypatch.setattr(
        sfec_endpoints, "SfecClient", lambda: _NoDetailClient())
    monkeypatch.setattr(
        sqlite_db, "update_invoice_sfec",
        lambda invoice_id, **kwargs: updates.append((invoice_id, kwargs)),
    )
    monkeypatch.setattr(sfec_endpoints.time, "sleep", lambda _seconds: None)

    result = sfec_endpoints.certify_sqlite_invoice(_invoice())

    assert result["success"] is True
    assert result["certification_number"] == ""
    assert result["identifier"] == SHORT_SIG
    assert updates[-1][1]["statut"] == "EN_COURS"
    assert updates[-1][1]["sfec_id"] == SHORT_SIG


def test_hydratation_recupere_l_uuid_meme_si_le_post_contient_deja_la_signature():
    class _CertifiedPostClient:
        def __init__(self):
            self.calls = []

        def get_invoice(self, key):
            self.calls.append(("get", key))
            return {
                "id": SFEC_UUID,
                "invoice_number": key,
                "invoice_status": "certified",
                "certification_short_signature": SHORT_SIG,
                "certification_signature": "SIGNATURE-COMPLETE",
                "certification_date": "2026-10-29T05:50:21.589Z",
            }

    client = _CertifiedPostClient()
    post_result = {
        "identifier": SHORT_SIG,
        "invoice_number": "TEST-HYDRATE-1",
        "certification_short_signature": SHORT_SIG,
        "certification_signature": "SIGNATURE-COMPLETE",
        "certification_date": "2026-10-29T05:50:21Z",
    }

    result, cert_number, _sig, _qr, _date, sfec_id = (
        sfec_endpoints._hydrate_certification_result(
            client, "TEST-HYDRATE-1", post_result, attempts=1)
    )

    assert cert_number == SHORT_SIG
    assert sfec_id == SFEC_UUID
    assert result["identifier"] == SFEC_UUID
    assert result["invoice_status"] == "certified"
    assert client.calls == [("get", "TEST-HYDRATE-1")]
