"""Backoff et reprise des certifications SFEC.

L'etat (dictionnaires de tentatives, horodatages, erreurs deja logguees) est
porte par `app.sync.state` ; ce module ne contient que la logique associee.
"""
import logging
import time

from app.sync.state import (
    _RETRY_BACKOFF_S, _logged_errors, _retry_after, _retry_attempts,
)

logger = logging.getLogger("t-connector.sync.retry")


def _should_retry(numero):
    return time.time() >= _retry_after.get(numero, 0)


def _has_certification_proof(invoice):
    """Vrai si Sage contient deja un numero de certification SFEC."""
    return bool(str(invoice.get("sfec_num_certif") or "").strip())


def _needs_certification_recovery(invoice):
    """Un DEJA_CERTIFIE sans numero est une trace du bug 409 a reparer."""
    return (
        invoice.get("sfec_statut") == "DEJA_CERTIFIE"
        and not _has_certification_proof(invoice)
    )


def _schedule_retry(numero):
    n = _retry_attempts.get(numero, 0)
    delay = _RETRY_BACKOFF_S[min(n, len(_RETRY_BACKOFF_S) - 1)]
    _retry_attempts[numero] = n + 1
    _retry_after[numero] = time.time() + delay
    logger.info("Retry SFEC %s dans %ds (tentative %d)", numero, delay, n + 1)


def _reset_retry(numero):
    _retry_attempts.pop(numero, None)
    _retry_after.pop(numero, None)


def _log_certify_error(numero, message):
    """Loggue une erreur une seule fois par (facture, motif) et par process."""
    key = (numero, str(message)[:120])
    if key in _logged_errors:
        logger.debug("Certification echouee pour %s: %s", numero, message)
    else:
        _logged_errors.add(key)
        logger.error("Certification echouee pour %s: %s", numero, message)
