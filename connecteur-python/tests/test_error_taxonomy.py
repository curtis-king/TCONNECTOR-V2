"""Taxonomie d'erreurs Sage et SFEC (LOT 3).

Vérifie la table de correspondance message → classe → politique de reprise,
sans accès réel à Sage ni à SFEC.
"""
import pytest

from app.integration.sage import connection as sage_conn
from app.integration.sage.errors import (
    PermanentSfecWriteError, SageError, SageLockedError, SageSchemaError,
    SageUnavailableError, SageWriteConflictError, classify_sage_error,
    is_transient,
)
from app.integration.sfec import client as sfec_client
from app.integration.sfec.errors import (
    NetworkOfflineError, SfecAlreadyCertifiedError, SfecAuthError,
    SfecHttpError, SfecNetworkError, SfecRejectedError, build_http_error,
)


# ── Sage : classification ──────────────────────────────────────────────────
@pytest.mark.parametrize("message,classe,transitoire", [
    ("[HYT00] Login timeout expired", SageUnavailableError, True),
    ("[08001] Communication link failure", SageUnavailableError, True),
    ("Configuration base de donnees incomplete", SageUnavailableError, True),
    ("80003 - lock", SageLockedError, True),
    ("[42000] Cet element est en cours d'utilisation.", SageLockedError, True),
    ("[42000] Invalid column name", SageSchemaError, False),
    ("Invalid object name 'F_DOCENTETE'", SageSchemaError, False),
    ("permission denied", Exception, False),
])
def test_classify_sage_error(message, classe, transitoire):
    exc = Exception(message)
    typed = classify_sage_error(exc)
    assert isinstance(typed, classe)
    assert is_transient(typed) is transitoire


def test_une_erreur_deja_typee_n_est_pas_retransformee():
    exc = SageLockedError("verrou")
    assert classify_sage_error(exc) is exc


def test_classes_conservent_la_compatibilite_valueerror():
    # Du code existant attrape ValueError : la compatibilité est conservée.
    assert issubclass(SageSchemaError, ValueError)
    assert issubclass(PermanentSfecWriteError, ValueError)
    assert issubclass(PermanentSfecWriteError, SageWriteConflictError)
    assert issubclass(PermanentSfecWriteError, SageError)


def test_connect_with_retry_leve_une_erreur_typee_non_transitoire(monkeypatch):
    def fake_connect():
        raise Exception("[42000] Invalid column name")

    monkeypatch.setattr(sage_conn, "_connect", fake_connect)
    with pytest.raises(SageSchemaError, match="42000"):
        sage_conn._connect_with_retry(attempts=3)


def test_connect_with_retry_leve_une_erreur_typee_apres_epuisement(monkeypatch):
    calls = []

    def fake_connect():
        calls.append(1)
        raise Exception("[08001] Communication link failure")

    monkeypatch.setattr(sage_conn, "_connect", fake_connect)
    monkeypatch.setattr(sage_conn.time, "sleep", lambda _d: None)

    with pytest.raises(SageUnavailableError):
        sage_conn._connect_with_retry(attempts=2)
    assert len(calls) == 2


# ── SFEC : classification HTTP ─────────────────────────────────────────────
@pytest.mark.parametrize("statut,classe", [
    (400, SfecRejectedError),
    (422, SfecRejectedError),
    (401, SfecAuthError),
    (403, SfecAuthError),
    (409, SfecAlreadyCertifiedError),
    (500, SfecHttpError),
    (418, SfecHttpError),
])
def test_build_http_error(statut, classe):
    exc = build_http_error(statut, "corps", {"error": "detail"})
    assert isinstance(exc, classe)
    assert exc.status_code == statut
    assert exc.error_body == "corps"
    assert exc.error_json == {"error": "detail"}


def test_network_offline_est_une_erreur_reseau():
    exc = NetworkOfflineError("hors ligne")
    assert isinstance(exc, SfecNetworkError)
    assert exc.status_code == 0


def test_client_leve_une_erreur_typee_sur_409(monkeypatch):
    class Reponse:
        ok = False
        status_code = 409
        text = "deja certifiee"
        headers = {"Retry-After": "7"}

        def json(self):
            return {"error": "already certified"}

    client = sfec_client.SfecClient.__new__(sfec_client.SfecClient)
    client.base_url = "https://example.invalid"
    client.api_key = "test"
    client.timeout = 1
    monkeypatch.setattr(client, "_check_connectivity", lambda: True)
    monkeypatch.setattr(sfec_client.requests, "post", lambda *a, **k: Reponse())

    with pytest.raises(SfecAlreadyCertifiedError) as info:
        client._request("POST", "/api/v1/invoices", {"invoice_id": "1"})
    assert info.value.status_code == 409
    assert info.value.retry_after == "7"
    assert info.value.error_json == {"error": "already certified"}


def test_client_leve_une_erreur_reseau_sur_timeout(monkeypatch):
    client = sfec_client.SfecClient.__new__(sfec_client.SfecClient)
    client.base_url = "https://example.invalid"
    client.api_key = "test"
    client.timeout = 1
    monkeypatch.setattr(client, "_check_connectivity", lambda: True)

    def timeout(*_a, **_k):
        raise sfec_client.requests.exceptions.Timeout("trop long")

    monkeypatch.setattr(sfec_client.requests, "get", timeout)

    with pytest.raises(SfecNetworkError):
        client._request("GET", "/api/v1/invoices")
