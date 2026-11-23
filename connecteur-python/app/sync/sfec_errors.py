"""Lecture des rejets SFEC (400 sur-solde, 409 creditNoteOnAccount).

Fonctions pures : elles ne font que normaliser le corps d'erreur renvoye par
le client SFEC, quel que soit le type d'exception (`status_code`,
`error_body` ou `error_json`).
"""
import json


def _sfec_error_json(exc):
    body = getattr(exc, "error_json", None)
    if isinstance(body, dict):
        return body
    raw = getattr(exc, "error_body", "") or ""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def overcredit_details(exc):
    """Retourne le corps d'un 400 'avoir sur-solde', sinon None.

    SFEC renvoie `already_credited`, `invoice_net` et `remaining` sur ce cas.
    C'est un rejet metier definitif, pas une erreur transitoire.
    """
    try:
        status = int(getattr(exc, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status != 400:
        return None
    body = _sfec_error_json(exc)
    if any(key in body for key in ("already_credited", "invoice_net", "remaining")):
        return body
    return None


def overcredit_message(exc):
    body = overcredit_details(exc) or {}
    return (
        "SFEC 400: avoir sur-solde: deja credite {}, facture {}, reste {} ({})"
    ).format(
        body.get("already_credited", "?"),
        body.get("invoice_net", "?"),
        body.get("remaining", "?"),
        body.get("error", ""),
    ).strip()


def on_account_reference_conflict(exc):
    """Retourne la reference si un 409 vient d'un creditNoteOnAccount."""
    try:
        status = int(getattr(exc, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status != 409:
        return None
    body = _sfec_error_json(exc)
    reference = str(body.get("reference") or "").strip()
    if not reference:
        return None
    message = str(body.get("error") or body.get("message") or "").lower()
    if "account" in message or "reference" in message:
        return reference
    return None
