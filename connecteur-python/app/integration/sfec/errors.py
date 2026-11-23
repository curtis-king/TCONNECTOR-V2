"""Taxonomie d'erreurs du client SFEC.

Les exceptions conservent les attributs historiques (`status_code`,
`error_body`, `error_json`, `retry_after`) : les appelants qui inspectent le
statut continuent de fonctionner pendant la migration.
"""


class SfecError(Exception):
    """Erreur du client SFEC (base de la taxonomie)."""

    def __init__(self, message="", status_code=0, error_body="",
                 error_json=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_body = error_body
        self.error_json = error_json if isinstance(error_json, dict) else {}
        if retry_after is not None:
            self.retry_after = retry_after


class SfecNetworkError(SfecError):
    """Erreur réseau, timeout ou 5xx/429 : la requête peut être rejouée."""


class SfecRejectedError(SfecError):
    """Rejet métier définitif (400/422) : ne pas rejouer tel quel."""


class SfecAlreadyCertifiedError(SfecError):
    """409 : la facture existe déjà côté SFEC."""


class SfecAuthError(SfecError):
    """401/403 : clé API absente, invalide ou expirée."""


class SfecHttpError(SfecError):
    """Statut HTTP non classé (fallback)."""


class NetworkOfflineError(SfecNetworkError):
    """Pas de connexion internet : certification mise en file d'attente."""


_STATUS_CLASSES = {
    400: SfecRejectedError,
    401: SfecAuthError,
    403: SfecAuthError,
    409: SfecAlreadyCertifiedError,
    422: SfecRejectedError,
}


def build_http_error(status_code, body, error_json=None, retry_after=None):
    """Construit l'exception typée correspondant à un statut HTTP SFEC."""
    try:
        code = int(status_code or 0)
    except (TypeError, ValueError):
        code = 0
    cls = _STATUS_CLASSES.get(code, SfecHttpError)
    return cls(
        "SFEC API {}: {}".format(code, body),
        status_code=code,
        error_body=body,
        error_json=error_json,
        retry_after=retry_after,
    )
