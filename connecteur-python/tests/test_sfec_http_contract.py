"""Contrat HTTP du client SFEC (payload envoyé, statuts, idempotence).

Les règles métier du payload sont déjà couvertes par `test_sfec_payload_rules`,
`test_sfec_centimes` et `test_sfec_avoirs`. Ce fichier verrouille la frontière
réseau : ce qui part réellement sur le fil, ce qui revient, et la politique de
reprise par statut.
"""
import json

import pytest

from app.integration.sfec import client as sfec_client
from app.integration.sfec.endpoints import db_invoice_to_sfec
from app.integration.sfec.errors import (
    SfecAlreadyCertifiedError, SfecAuthError, SfecNetworkError,
    SfecRejectedError,
)


# ── Outils ─────────────────────────────────────────────────────────────────
class _Reponse:
    def __init__(self, statut=200, payload=None, texte="", headers=None):
        self.status_code = statut
        self.ok = 200 <= statut < 300
        self._payload = payload if payload is not None else {}
        self.text = texte or json.dumps(self._payload)
        self.headers = headers or {}

    def json(self):
        return self._payload


def _client(monkeypatch):
    """Client SFEC sans config ni réseau (attributs minimaux)."""
    client = sfec_client.SfecClient.__new__(sfec_client.SfecClient)
    client.base_url = "https://sfec.test"
    client.api_key = "cle-de-test"
    client.timeout = 1
    monkeypatch.setattr(client, "_check_connectivity", lambda: True)
    return client


def _facture(**extra):
    facture = {
        "id": "0-6-FA-CONTRAT",
        "numero": "FA-CONTRAT",
        "type_doc": "vente",
        "nom_tiers": "Client Contrat",
        "recipient_niu": "M0000000000000001",
        "montant_ht": 100000,
        "montant_tva": 18000,
        "montant_ttc": 118900,
        "montant_restant": 118900,
        "lignes": [{
            "designation": "Article contrat",
            "quantite": 1,
            "prix_unitaire": 100000,
            "montant_ht": 100000,
            "taux_tva": 18,
            "montant_tva": 18000,
            "montant_ttc": 118900,
        }],
    }
    facture.update(extra)
    return facture


# ── Payload : ce qui part sur le fil ───────────────────────────────────────
@pytest.mark.parametrize("facture,type_attendu", [
    (_facture(), "salesInvoice"),
    (_facture(numero="AV-CONTRAT", type_doc="avoir"), "creditNote"),
    (_facture(
        numero="EXO-CONTRAT",
        montant_ht=250000, montant_tva=0, montant_ttc=250000,
        montant_restant=250000,
        lignes=[{
            "designation": "Prestation exoneree", "quantite": 1,
            "prix_unitaire": 250000, "montant_ht": 250000,
            "taux_tva": 0, "montant_tva": 0, "montant_ttc": 250000,
        }],
    ), "salesInvoice"),
])
def test_le_payload_post_est_le_payload_construit(monkeypatch, facture,
                                                  type_attendu):
    """`certify()` transmet tel quel le payload validé, sans transformation."""
    payload = db_invoice_to_sfec(facture)
    assert payload["invoice_type"] == type_attendu

    envoye = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        envoye.update(url=url, body=json, headers=headers)
        return _Reponse(201, {"id": "sfec-1", "invoice_number": payload["invoice_id"]})

    client = _client(monkeypatch)
    monkeypatch.setattr(sfec_client.requests, "post", fake_post)

    resultat = client.certify(payload)

    assert resultat["id"] == "sfec-1"
    assert envoye["url"] == "https://sfec.test/api/v1/invoices"
    assert envoye["body"] == payload          # aucune clé ajoutée ni retirée
    assert envoye["headers"]["X-API-Key"] == "cle-de-test"


@pytest.mark.parametrize("facture", [
    _facture(),
    _facture(numero="AV-CONTRAT", type_doc="avoir"),
    _facture(
        numero="EXO-CONTRAT",
        montant_ht=250000, montant_tva=0, montant_ttc=250000,
        montant_restant=250000,
        lignes=[{
            "designation": "Prestation exoneree", "quantite": 1,
            "prix_unitaire": 250000, "montant_ht": 250000,
            "taux_tva": 0, "montant_tva": 0, "montant_ttc": 250000,
        }],
    ),
])
def test_cles_obligatoires_du_contrat_sfec(facture):
    """Le contrat minimal attendu par l'API ne doit pas régresser."""
    payload = db_invoice_to_sfec(facture)
    for cle in (
        "invoice_id", "invoice_type", "recipient_type", "recipient_name",
        "subtotal", "total_tax_amount", "total_amount", "amount_due",
        "currency", "payment_method", "items",
    ):
        assert cle in payload, cle
    assert payload["items"], "au moins une ligne est attendue"
    for cle in ("designation", "type", "quantity", "unit_price",
                "net_amount", "tax_rate", "tax_amount", "total_amount"):
        assert cle in payload["items"][0], cle


def test_avoir_est_envoye_en_montants_positifs():
    payload = db_invoice_to_sfec(_facture(
        numero="AV-CONTRAT", type_doc="avoir",
        montant_ht=-100000, montant_tva=-18000, montant_ttc=-118900,
        montant_restant=-118900,
        lignes=[{
            "designation": "Article contrat", "quantite": -1,
            "prix_unitaire": 100000, "montant_ht": -100000,
            "taux_tva": 18, "montant_tva": -18000, "montant_ttc": -118900,
        }],
    ))
    assert payload["invoice_type"] == "creditNote"
    assert payload["total_amount"] > 0
    assert payload["items"][0]["total_amount"] > 0


def test_exoneration_n_a_pas_de_tva():
    payload = db_invoice_to_sfec(_facture(
        numero="EXO-CONTRAT",
        montant_ht=250000, montant_tva=0, montant_ttc=250000,
        montant_restant=250000,
        lignes=[{
            "designation": "Prestation exoneree", "quantite": 1,
            "prix_unitaire": 250000, "montant_ht": 250000,
            "taux_tva": 0, "montant_tva": 0, "montant_ttc": 250000,
        }],
    ))
    assert payload["total_tax_amount"] == 0
    assert payload["total_exempt_amount"] == 250000


# ── Statuts HTTP → classes d'erreur ────────────────────────────────────────
@pytest.mark.parametrize("statut,classe", [
    (400, SfecRejectedError),
    (422, SfecRejectedError),
    (401, SfecAuthError),
    (403, SfecAuthError),
    (409, SfecAlreadyCertifiedError),
])
def test_statut_http_leve_la_classe_attendue(monkeypatch, statut, classe):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        sfec_client.requests, "post",
        lambda *a, **k: _Reponse(statut, {"error": "refus"},
                                 headers={"Retry-After": "3"}),
    )

    with pytest.raises(classe) as info:
        client.certify({"invoice_id": "FA-CONTRAT"})

    assert info.value.status_code == statut
    assert info.value.error_json == {"error": "refus"}
    assert info.value.retry_after == "3"


def test_erreur_reseau_devient_sfec_network_error(monkeypatch):
    client = _client(monkeypatch)

    def coupure(*_a, **_k):
        raise sfec_client.requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(sfec_client.requests, "post", coupure)
    monkeypatch.setattr(sfec_client, "is_online", lambda: True)

    with pytest.raises(SfecNetworkError):
        client.certify({"invoice_id": "FA-CONTRAT"})


# ── Idempotence de la vérification par numéro ─────────────────────────────
def test_verification_par_numero_ne_recree_rien(monkeypatch):
    appels = []

    def fake_get(url, headers=None, timeout=None):
        appels.append(url)
        return _Reponse(200, {"id": "sfec-1", "invoice_number": "FA-CONTRAT"})

    client = _client(monkeypatch)
    monkeypatch.setattr(sfec_client.requests, "get", fake_get)
    monkeypatch.setattr(sfec_client.requests, "post",
                        lambda *a, **k: pytest.fail("aucun POST attendu"))

    detail = client.verify_by_invoice_number("FA-CONTRAT")

    assert detail["id"] == "sfec-1"
    assert appels == ["https://sfec.test/api/v1/invoices/FA-CONTRAT"]


def test_verification_404_est_definitive_sans_retry(monkeypatch):
    appels = []

    def fake_get(url, headers=None, timeout=None):
        appels.append(url)
        return _Reponse(404, {"error": "not found"})

    client = _client(monkeypatch)
    monkeypatch.setattr(sfec_client.requests, "get", fake_get)

    assert client.verify_by_invoice_number("INCONNUE") is None
    assert len(appels) == 1, "un 404 ne doit pas être rejoué"


def test_verification_rejoue_un_5xx_puis_reussit(monkeypatch):
    reponses = [
        _Reponse(503, {"error": "indisponible"}),
        _Reponse(200, {"id": "sfec-2", "invoice_number": "FA-RETRY"}),
    ]
    appels = []

    def fake_get(url, headers=None, timeout=None):
        appels.append(url)
        return reponses.pop(0)

    client = _client(monkeypatch)
    monkeypatch.setattr(sfec_client.requests, "get", fake_get)
    monkeypatch.setattr(sfec_client.time, "sleep", lambda _d: None)

    assert client.verify_by_invoice_number("FA-RETRY")["id"] == "sfec-2"
    assert len(appels) == 2
